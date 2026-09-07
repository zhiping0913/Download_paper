"""ACS Publications handler (pubs.acs.org, DOI prefix 10.1021).

ACS runs on the Silverchair platform, so the article page is ordinary
server-rendered HTML: no body API to chase, everything comes out of the DOM.

    <meta name="citation_*">                bibliographic metadata + PDF URL
    section.abstract                        abstract text
    div.graphical-abstract                  "Visual Abstract" -> key image
    div.content-metadata-keywords           Keywords
    div.content-metadata                    Subjects
    div.article-body div.content            body, h2-delimited
    div.fig.fig-section                     figures (+ full-size link)
    div.formula-wrap                        display math, with its (N) label
    a[href*="article-supplement"]           Supporting Information

Two things need care:

*Math.* Formulas live in ``span.mathFormula``. When MathJax has run it
empties that span and leaves an ``<mjx-container>`` whose only recoverable
source is the ``<mjx-assistive-mml>`` MathML; when ``block_mathjax()`` did its
job the span still holds the original markup. :meth:`_formula_latex` handles
both, so the output is real LaTeX either way. Display formulas carry their
number in a sibling ``span.label.title-label`` -- kept, since a paper that
says "substituting into eq 1" needs it.

*Figures.* The inline ``<img>`` points at the ``m_``-prefixed medium
rendition. The full-size PNG is the ``image=`` parameter of the "Download to
Slide" link (what the UI calls "View Large"); dropping the ``m_`` prefix is
the fallback.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

from html_to_md_converter import (
    cleanup_markdown,
    convert_html_to_markdown,
    mathml_to_latex_pandoc,
    remove_newlines_in_paragraph,
)
from publisher.base import PublisherHandler
from publisher.wildcard import (
    format_as_bibtex,
    init_extract_all_page,
    render_heading_md,
    set_actual_base_url,
)


# Headings whose section is emitted elsewhere in the markdown, so the body
# walk must drop the section itself, not merely its title. ACS lays each one
# out as an <h2> followed by a single unclassed <div> holding the content.
_ACS_SECTION_DROP = frozenset({
    'references',
    'supporting information',
})

# h2 headings that belong to the page furniture rather than the paper.
_ACS_H2_SKIP = frozenset({
    'article metrics',
    'cited by',
    'subscribe to email alerts',
    'recommended articles',
    'sharing unavailable',
    'visual abstract',          # rendered with the abstract instead
    'abstract',                 # emitted as its own section
    'references',               # emitted from metadata['references']
    'supporting information',   # emitted as the supplemental section
    'partners',
    'about',
    'resources and information',
    'support & contact',
})

# Elements inside the body that carry no article content.
#
# NOT in this list: mjx-assistive-mml. MathJax's assistive MathML is the only
# faithful copy of a formula left in a rendered page, so dropping it here
# would silently empty every equation.
_ACS_DROP_SELECTORS = (
    'div.fig-orig',             # "View Large" / "Download to Slide" buttons
    'div.fig-label:not(.label)',  # the heading copy of "Figure N"; the
                                  # caption's div.label.fig-label is kept,
                                  # since _float_label() reads it
    'span.screenreader-text',   # "Close Figure 1." and friends
    'span.hidden',
    'div.content-metadata',     # Subjects — emitted with the abstract
    'div.content-metadata-keywords',
    'div.toolbar-wrap',
    'div.figure-viewer',        # the lightbox's copy of every figure
    'div.fig-modal',            # ditto: div.fig.fig-modal repeats the
                                # caption and footnotes of every float
    'div.table-modal',          # ditto for tables
    'script',
    'style',
    'button',
)

# Body text that is UI chrome rather than prose. Matched on the whole
# rendered line, so it cannot eat a sentence that merely contains the words.
_ACS_NOISE_LINES = frozenset({
    'open figure viewer',
    'view large',
    'download to slide',
    'close',
})

# Silverchair leaves its unrendered template markers in the served HTML
# (``/foreach``, ``/.widget-items``); they are comments to the templating
# engine, not article text.
_ACS_TEMPLATE_ARTIFACT_RE = re.compile(r'^/[.\w-]+$')


class ACSHandler(PublisherHandler):
    """Full-text handler for ACS Publications."""

    ACS_BASE = 'https://pubs.acs.org'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.ACS_BASE

    # ==================================================================
    # Math -- one pipeline for every element
    # ==================================================================

    @staticmethod
    def _mathml_to_latex(mathml: str) -> str:
        if not mathml:
            return ''
        try:
            latex = (mathml_to_latex_pandoc(mathml) or '').strip()
        except Exception:
            return ''
        # pandoc hands back the delimiters; the caller decides inline vs display.
        latex = re.sub(r'^\\[\(\[]|\\[\)\]]$', '', latex).strip()
        if latex.startswith('$$') and latex.endswith('$$'):
            latex = latex[2:-2].strip()
        elif latex.startswith('$') and latex.endswith('$'):
            latex = latex[1:-1].strip()
        return latex

    @classmethod
    def _formula_latex(cls, node: Tag) -> str:
        """Recover the LaTeX for one formula, rendered or not.

        Order matters. ``span.mathFormula`` is the source ACS ships; MathJax
        empties it in place, so a non-empty span means the interceptor kept
        MathJax out and the original markup survived. Otherwise fall back to
        the assistive MathML MathJax leaves behind for screen readers, which
        is the only faithful copy of the formula in a rendered page.
        """
        source = node.find('span', class_='mathFormula')
        if source is not None:
            raw = source.decode_contents().strip()
            if raw:
                if '<math' in raw.lower():
                    latex = cls._mathml_to_latex(raw)
                    if latex:
                        return latex
                text = source.get_text().strip()
                text = re.sub(r'^\\[\(\[]|\\[\)\]]$', '', text).strip()
                if text:
                    return text

        assistive = node.find('mjx-assistive-mml')
        if assistive is not None:
            math = assistive.find('math')
            if math is not None:
                latex = cls._mathml_to_latex(str(math))
                if latex:
                    return latex

        math = node.find('math')
        if math is not None:
            latex = cls._mathml_to_latex(str(math))
            if latex:
                return latex

        # Last resort: MathJax's rendered glyphs. Not LaTeX, but better than
        # dropping the formula silently -- and visibly wrong, so it gets noticed.
        return re.sub(r'\s+', ' ', node.get_text(' ', strip=True))

    @classmethod
    def _inline_md(cls, node) -> str:
        """Render an inline subtree to markdown, formulas included.

        Every text-bearing element in this handler goes through here --
        paragraphs, captions, list items, headings, table cells -- so a
        formula can never be lost to a bare ``get_text()``.
        """
        if node is None:
            return ''
        if isinstance(node, NavigableString):
            return re.sub(r'\s+', ' ', str(node))
        if not isinstance(node, Tag):
            return ''

        name = node.name.lower()
        classes = node.get('class') or []

        if name in ('script', 'style', 'button'):
            return ''
        if name == 'mjx-assistive-mml':
            return ''
        if name == 'mjx-container':
            # Resolve against the container itself: its own assistive MathML
            # is a child, whereas span.mathFormula is a *sibling*, so walking
            # to the parent would look in the wrong place.
            latex = cls._formula_latex(node)
            return f"${latex}$" if latex else ''
        if 'mathFormula' in classes:
            # Non-empty only when MathJax was blocked; when it ran, the
            # sibling mjx-container carries the formula and this is empty.
            latex = cls._formula_latex(node)
            return f"${latex}$" if latex else ''
        if 'inline-formula' in classes:
            latex = cls._formula_latex(node)
            return f"${latex}$" if latex else ''
        if name == 'math':
            latex = cls._mathml_to_latex(str(node))
            return f"${latex}$" if latex else ''

        inner = ''.join(cls._inline_md(c) for c in node.children)

        if name in ('b', 'strong'):
            return cls._wrap(inner, '**')
        if name in ('i', 'em'):
            return cls._wrap(inner, '*')
        if name == 'sup':
            return cls._wrap(inner, '^')
        if name == 'sub':
            return cls._wrap(inner, '~')
        if name == 'a':
            href = (node.get('href') or '').strip()
            text = inner.strip()
            # Cross-references to figures/equations/citations stay as plain
            # text: their targets are anchors in a page we are not shipping.
            if (not href or href.startswith('#') or href.startswith('javascript:')
                    or 'link-ref' in ' '.join(classes)):
                return inner
            resolved = urljoin(ACSHandler.ACS_BASE, href)
            # ACS sometimes ships a bare scheme ("http://") as the href; a
            # link to nowhere is worse than plain text.
            if not urlparse(resolved).netloc:
                return inner
            return f"[{text}]({resolved})" if text else ''

        return inner

    @staticmethod
    def _wrap(inner: str, mark: str) -> str:
        """Apply an emphasis marker while leaving surrounding spaces outside."""
        stripped = inner.strip()
        if not stripped:
            return inner
        lead = inner[:len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()):]
        return f"{lead}{mark}{stripped}{mark}{trail}"

    @classmethod
    def _text_md(cls, node) -> str:
        return re.sub(r'\s+', ' ', cls._inline_md(node)).strip()

    # ==================================================================
    # Metadata
    # ==================================================================

    @staticmethod
    def _meta(soup: BeautifulSoup, name: str) -> str:
        tag = soup.find('meta', attrs={'name': name})
        return (tag.get('content') or '').strip() if tag else ''

    @staticmethod
    def _meta_all(soup: BeautifulSoup, name: str) -> List[str]:
        return [(t.get('content') or '').strip()
                for t in soup.find_all('meta', attrs={'name': name})
                if (t.get('content') or '').strip()]

    @classmethod
    def _extract_keywords(cls, soup: BeautifulSoup) -> Tuple[List[str], List[str]]:
        """Return ``(keywords, subjects)`` from the article-metadata blocks."""
        keywords, subjects = [], []
        kw_div = soup.find('div', class_='content-metadata-keywords')
        if kw_div is not None:
            keywords = [a.get_text(' ', strip=True)
                        for a in kw_div.find_all('a')
                        if a.get_text(strip=True)]
        for div in soup.find_all('div', class_='content-metadata'):
            title = div.find(class_=re.compile('taxonomies-title'))
            if title is None:
                continue
            subjects = [a.get_text(' ', strip=True)
                        for a in div.find_all('a')
                        if a.get_text(strip=True)]
            break
        return keywords, subjects

    @classmethod
    def _extract_abstract(cls, soup: BeautifulSoup) -> str:
        """Abstract text, excluding the graphical-abstract figure block."""
        for section in soup.find_all('section', class_='abstract'):
            if 'graphicalAbstract' in (section.get('class') or []):
                continue
            parts = []
            for p in section.find_all('p'):
                text = cls._text_md(p)
                if text:
                    parts.append(text)
            if parts:
                return '\n\n'.join(parts)
        return cls._meta(soup, 'description')

    @classmethod
    def _extract_key_image(cls, soup: BeautifulSoup) -> str:
        """The "Visual Abstract" graphic, full-size where possible."""
        wrapper = soup.find('div', class_='graphical-abstract')
        if wrapper is None:
            return ''
        img = wrapper.find('img')
        if img is None:
            return ''
        url = (img.get('src') or img.get('data-src') or '').strip()
        return cls._full_size_url(url)

    @staticmethod
    def _full_size_url(url: str) -> str:
        """Turn a ``m_``-prefixed medium rendition into the full-size one.

        ACS names the inline image ``m_nl8b05070_0001.png`` and the full-size
        one ``nl8b05070_0001.png`` in the same directory. The CloudFront
        signature covers the whole path, so it has to travel with the URL --
        which it does, since only the basename changes.
        """
        if not url:
            return ''
        if url.startswith('//'):
            url = 'https:' + url
        return re.sub(r'/m_([^/?]+)(\?|$)', r'/\1\2', url)

    async def extract_metadata(self, page) -> dict:
        try:
            html = await page.content()
        except Exception:
            html = ''
        return self.extract_metadata_from_html(html)

    @classmethod
    def extract_metadata_from_html(cls, html: str) -> dict:
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')

        authors = []
        for raw in cls._meta_all(soup, 'citation_author'):
            # ACS writes "Surname, Given"; flip to reading order.
            if ',' in raw:
                surname, given = raw.split(',', 1)
                raw = f"{given.strip()} {surname.strip()}".strip()
            authors.append(raw)

        first = cls._meta(soup, 'citation_firstpage')
        last = cls._meta(soup, 'citation_lastpage')
        pages = f"{first}-{last}" if first and last else first

        date = cls._meta(soup, 'citation_publication_date')
        year = ''
        m = re.search(r'(19|20|21)\d{2}', date)
        if m:
            year = m.group(0)

        keywords, subjects = cls._extract_keywords(soup)

        title = cls._meta(soup, 'citation_title')
        if not title:
            h1 = soup.find('h1')
            title = h1.get_text(' ', strip=True) if h1 else ''

        return {
            'title': re.sub(r'\s+', ' ', title).strip(),
            'doi': cls._meta(soup, 'citation_doi'),
            'authors': authors,
            'journal': cls._meta(soup, 'citation_journal_title'),
            'volume': cls._meta(soup, 'citation_volume'),
            'issue': cls._meta(soup, 'citation_issue'),
            'pages': pages,
            'year': year,
            'publication_date': date,
            'issn': cls._meta(soup, 'citation_issn'),
            'publisher': cls._meta(soup, 'citation_publisher') or 'American Chemical Society',
            'abstract': cls._extract_abstract(soup),
            'corresponding_author_emails': [],
            '_keywords': keywords,
            '_subjects': subjects,
            '_pdf_url': cls._meta(soup, 'citation_pdf_url'),
            'key_image_url': cls._extract_key_image(soup),
        }

    # ==================================================================
    # References
    # ==================================================================

    # Link furniture ACS appends to every citation. Removed by container so a
    # reference whose *title* happens to contain one of these words survives.
    _REF_LINK_SELECTORS = (
        'div.crossref-doi',
        'div.adsDoiReference',
        'div.xslopenurl',
        'div.citation-links',
        'div.ref-links',
        'span.inst-open-url-holders',
    )
    _REF_LINK_WORDS = re.compile(
        r'\s*(?:Crossref|Search ADS|OpenURL|Google Scholar|PubMed|CAS|'
        r'Web of Science|View Article)\s*', re.IGNORECASE)

    @classmethod
    def extract_references_from_html(cls, html: str) -> list:
        """Extract the reference list, preferring the rendered one.

        The ``citation_reference`` meta tags look tempting -- one tag per
        reference, already split into fields -- but they are lossy for the
        multi-part references chemistry journals use constantly. A reference
        reading "For selected reviews: (a) Kagan ... (b) Corey ... (c) Merino
        ... (d) Jiang ..." collapses into a single tag holding every author
        from all four works and only the first work's journal, volume and
        pages, which is worse than useless: it reads like one citation that
        never existed.

        The rendered list keeps each sub-citation as its own
        ``div.citation.mixed-citation`` inside the numbered
        ``div.ref-content``, so it is walked instead and the meta tags are
        kept only as a fallback.
        """
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')

        refs = []
        for entry in soup.select('div.ref-content'):
            text = cls._render_reference(entry)
            if text:
                refs.append(text)
        if refs:
            return refs

        for raw in cls._meta_all(soup, 'citation_reference'):
            text = cls._format_reference(cls._parse_reference_meta(raw))
            if text:
                refs.append(text)
        return refs

    @classmethod
    def _render_reference(cls, entry: Tag) -> str:
        """One numbered reference, sub-citations and all, as flat text.

        Layout is not preserved on purpose -- what matters is that every
        sub-citation's text survives. Any lead-in ("For selected reviews:")
        is kept, since it says what the group of works is for.
        """
        clone = BeautifulSoup(str(entry), 'html.parser')

        # Drop the number: the caller re-numbers the list.
        label = clone.find('span', class_='label')
        if label is not None and label.find_parent('div', class_='citation') is None:
            label.decompose()

        for selector in cls._REF_LINK_SELECTORS:
            for el in clone.select(selector):
                el.decompose()

        text = cls._text_md(clone)
        text = cls._REF_LINK_WORDS.sub(' ', text)
        # ACS separates every field with its own element, so the flattened
        # text arrives with spaces before the punctuation that joins them.
        text = re.sub(r'\s+([,.;:])', r'\1', text)
        text = re.sub(r'\s*-\s*(?=\d)', '-', text)
        # Sub-citation markers lose their surrounding space when the elements
        # are flattened: "reviews:(a)Kagan" -> "reviews: (a) Kagan". The
        # single-letter-in-parens shape avoids touching "[4 + 2]" and the
        # like.
        text = re.sub(r'\s*\(([a-z])\)\s*', r' (\1) ', text)
        text = re.sub(r'\s{2,}', ' ', text).strip(' ,;')
        return text

    # ==================================================================
    # Figures
    # ==================================================================

    @classmethod
    def extract_figures_from_html(cls, html: str) -> dict:
        """``{'fig_N': {'url', 'original_url', 'caption', 'label'}}``."""
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')
        figures = {}
        for index, fig in enumerate(soup.select('div.fig.fig-section'), 1):
            large, medium = cls._figure_urls(fig)
            if not (large or medium):
                continue
            caption_el = fig.find('div', class_='caption')
            caption = cls._text_md(caption_el) if caption_el is not None else ''
            figures[f'fig_{index}'] = {
                'url': large or medium,
                'original_url': medium or large,
                'caption': caption,
                'label': cls._float_label(fig) or f'Figure {index}',
            }
        return figures

    @staticmethod
    def _float_label(fig: Tag) -> str:
        """The float's own label, e.g. "Figure 2" or "Scheme 11".

        ACS puts schemes, charts and figures in the same
        ``div.fig.fig-section`` wrapper and numbers each series separately, so
        a JACS paper has both a "Figure 1" and a "Scheme 1". Reading the
        rendered label keeps the two apart -- deriving it from the class or
        assuming "Figure" would relabel every scheme.
        """
        label_el = fig.find('div', class_='label')
        if label_el is None:
            return ''
        return label_el.get_text(' ', strip=True).rstrip('. ')

    @classmethod
    def _figure_urls(cls, fig: Tag) -> Tuple[str, str]:
        """``(full_size, inline)`` URLs for one figure block.

        The full-size PNG is the ``image=`` parameter of the "Download to
        Slide" link -- that is what the "View Large" button shows. The
        ``/view-large/figure/...`` href is a viewer page, not an image, so it
        is deliberately not used.
        """
        medium = ''
        img = fig.find('img')
        if img is not None:
            medium = (img.get('src') or img.get('data-src') or '').strip()
            if medium.startswith('//'):
                medium = 'https:' + medium

        # Derive the full-size URL from the inline one by dropping the ``m_``
        # prefix. The "Download to Slide" link also carries an ``image=``
        # parameter, but ACS does not always keep it in step with the figure:
        # on this article figure 2's slide link points at an unrelated inline
        # equation GIF (nl-2018-050709_m038.gif) while the figure itself is
        # nl8b05070_0002.png. So the slide link is only trusted when its
        # basename agrees with the inline image's.
        large = cls._full_size_url(medium)

        slide = fig.find('a', class_='download-slide')
        if slide is not None and slide.get('href'):
            href = slide['href']
            if href.startswith('//'):
                href = 'https:' + href
            query = parse_qs(urlparse(href).query)
            candidate = unquote((query.get('image') or [''])[0])
            if candidate and (not medium
                              or cls._basename(candidate) == cls._basename(large)):
                large = candidate

        return large, medium

    @staticmethod
    def _basename(url: str) -> str:
        return urlparse(url).path.rsplit('/', 1)[-1] if url else ''

    # ==================================================================
    # Supplemental
    # ==================================================================

    @classmethod
    def extract_supplemental_from_html(cls, html: str) -> Tuple[List[str], Dict[str, str], str]:
        """``(urls, descriptions, summary_md)`` for Supporting Information."""
        if not html:
            return [], {}, ''
        soup = BeautifulSoup(html, 'html.parser')

        urls: List[str] = []
        descriptions: Dict[str, str] = {}
        for a in soup.find_all('a', href=re.compile('article-supplement')):
            href = (a.get('href') or '').strip()
            if not href:
                continue
            url = urljoin(cls.ACS_BASE, href)
            if url in descriptions:
                continue
            label = a.get_text(' ', strip=True)
            urls.append(url)
            descriptions[url] = label or url

        # The prose describing the files sits between the back-matter
        # "Supporting Information" heading and the next h2. Walking siblings
        # rather than the parent matters: ACS puts that heading inside a
        # div that wraps the whole article, so find_parent() would return a
        # container whose <p> tags are the abstract and body.
        summary = ''
        for h2 in soup.find_all('h2'):
            if 'supporting information' not in h2.get_text(strip=True).lower():
                continue
            parts = []
            for sibling in h2.next_siblings:
                if isinstance(sibling, Tag):
                    if sibling.name and sibling.name.lower() == 'h2':
                        break
                    text = cls._text_md(sibling)
                    if text:
                        parts.append(text)
            if parts:
                summary = '\n\n'.join(parts)
                break
        return urls, descriptions, summary

    # ==================================================================
    # Body
    # ==================================================================

    # Blocks that stand on their own in the markdown.
    _BLOCK_TAGS = frozenset({'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                             'ul', 'ol', 'table', 'figure'})

    @classmethod
    def _is_block(cls, node) -> bool:
        if not isinstance(node, Tag) or not node.name:
            return False
        classes = node.get('class') or []
        return (node.name.lower() in cls._BLOCK_TAGS
                or 'formula-wrap' in classes
                or ('fig' in classes and 'fig-section' in classes))

    @classmethod
    def _has_block_descendant(cls, node: Tag) -> bool:
        return any(cls._is_block(d) for d in node.descendants
                   if isinstance(d, Tag))

    @classmethod
    def _render_block(cls, node, level: int, ctx: dict) -> List[str]:
        """Render one node of the article body to markdown blocks."""
        if isinstance(node, NavigableString):
            text = re.sub(r'\s+', ' ', str(node)).strip()
            if not text or cls._is_noise(text):
                return []
            return [text, '']
        if not isinstance(node, Tag) or not node.name:
            return []

        name = node.name.lower()
        classes = node.get('class') or []

        if name in ('script', 'style', 'button'):
            return []
        if 'fig' in classes and 'fig-section' in classes:
            ctx['fig_seq'] += 1
            return cls._render_figure(node, ctx['fig_seq'])
        if 'table-wrap' in classes:
            return cls._render_table_wrap(node)
        if 'formula-wrap' in classes:
            return cls._render_figure_free_formula(node)

        if name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            if node.get_text(' ', strip=True).lower() in _ACS_H2_SKIP:
                return []
            depth = {'h1': 0, 'h2': 0, 'h3': 1, 'h4': 2, 'h5': 3, 'h6': 4}[name]
            rendered = render_heading_md(node, '#' * (level + depth),
                                         converter=cls._text_md)
            return [rendered, ''] if rendered else []
        if name == 'p':
            text = cls._text_md(node)
            if not text or cls._is_noise(text):
                return []
            return [text, '']
        if name in ('ul', 'ol'):
            lines = cls._render_list(node)
            return lines + [''] if lines else []
        if name == 'table':
            if node.find_parent('div', class_='table-wrap') is not None:
                return []          # emitted by _render_table_wrap
            md = cls._render_table(node)
            return [md, ''] if md else []

        # A wrapper: recurse. A container holding inline content directly
        # (ACS writes some body text straight into a <div>) is rendered as one
        # paragraph -- descending into it would emit every link, formula and
        # text run as its own block and shred the prose into fragments.
        if cls._has_block_descendant(node):
            out: List[str] = []
            for child in node.children:
                out.extend(cls._render_block(child, level, ctx))
            return out

        text = cls._text_md(node)
        if not text or cls._is_noise(text):
            return []
        return [text, '']

    @staticmethod
    def _is_noise(text: str) -> bool:
        """True for UI chrome and leftover template markers."""
        stripped = text.strip()
        return (stripped.lower().rstrip('.') in _ACS_NOISE_LINES
                or bool(_ACS_TEMPLATE_ARTIFACT_RE.match(stripped))
                or stripped.lower().startswith('copyright ©'))

    @classmethod
    def _render_list(cls, node: Tag, depth: int = 0) -> List[str]:
        lines: List[str] = []
        ordered = node.name.lower() == 'ol'
        indent = '  ' * depth
        for index, li in enumerate(node.find_all('li', recursive=False), 1):
            bullet = f"{index}." if ordered else '-'
            own, nested = [], []
            for child in li.children:
                if isinstance(child, Tag) and child.name.lower() in ('ul', 'ol'):
                    nested.extend(cls._render_list(child, depth + 1))
                else:
                    own.append(cls._inline_md(child))
            text = re.sub(r'\s+', ' ', ''.join(own)).strip()
            if text:
                lines.append(f"{indent}{bullet} {text}")
            lines.extend(nested)
        return lines

    @classmethod
    def _render_table_wrap(cls, node: Tag) -> List[str]:
        """A table float: title, the grid itself, then its footnotes.

        ACS renders the table twice -- once in ``div.table-overflow`` and
        again inside ``div.table-modal`` for the lightbox -- so only the first
        grid is emitted. Some tables are shipped as an image instead
        (``div.fig-graphic``), in which case there is no grid to render and
        the title plus footnotes still carry the content.
        """
        out: List[str] = []
        title_el = node.find('div', class_='table-wrap-title')
        if title_el is not None:
            # Emit "**Table 1.** <title>" rather than bolding the whole line:
            # ACS titles contain their own bold runs ("with Ligand **R**"), and
            # wrapping those in another ** produces a stray **** in the output.
            title_copy = BeautifulSoup(str(title_el), 'html.parser')
            label_el = title_copy.find('span', class_='label')
            label = ''
            if label_el is not None:
                label = label_el.get_text(' ', strip=True).rstrip('. ')
                label_el.decompose()
            title = cls._text_md(title_copy)
            if label and title:
                out.extend([f"**{label}.** {title}", ''])
            elif label or title:
                out.extend([f"**{label}.**" if label else title, ''])

        table = node.find('table')
        if table is not None:
            md = cls._render_table(table)
            if md:
                out.extend([md, ''])

        out.extend(cls._render_float_notes(node))
        return out

    @classmethod
    def _render_table(cls, table: Tag) -> str:
        rows = []
        for tr in table.find_all('tr'):
            cells = [cls._text_md(td) for td in tr.find_all(['td', 'th'])]
            if cells:
                rows.append(cells)
        if not rows:
            return ''
        width = max(len(r) for r in rows)
        rows = [r + [''] * (width - len(r)) for r in rows]
        out = ['| ' + ' | '.join(rows[0]) + ' |', '|' + '---|' * width]
        for row in rows[1:]:
            out.append('| ' + ' | '.join(row) + ' |')
        return '\n'.join(out)

    @classmethod
    def _render_figure_free_formula(cls, node: Tag) -> List[str]:
        """Display equation plus its ``(N)`` label."""
        formula = node.find('div', class_='disp-formula') or node
        latex = cls._formula_latex(formula)
        if not latex:
            return []
        label_el = node.find('span', class_='label')
        label = label_el.get_text(' ', strip=True) if label_el is not None else ''
        line = f"$${latex}$$"
        if label:
            line += f" {label}"
        return [line, '']

    @classmethod
    def _render_figure(cls, node: Tag, index: int) -> List[str]:
        """Caption, footnotes and a placeholder for the downloaded image.

        *index* is the float's position in document order, which is what
        :meth:`extract_figures_from_html` keys on and therefore what the
        downloader names the file after. It deliberately is NOT the number in
        the label: schemes and figures are numbered independently, so
        "Scheme 1" and "Figure 1" would fight over the same placeholder and
        one of them would end up showing the other's image.
        """
        label = cls._float_label(node) or f'Figure {index}'

        caption_el = node.find('div', class_='caption')
        caption = ''
        if caption_el is not None:
            caption_copy = BeautifulSoup(str(caption_el), 'html.parser')
            for fn in caption_copy.select('div.fn'):
                fn.decompose()
            caption = cls._text_md(caption_copy)

        out: List[str] = []
        if caption:
            out.extend([f"**{label}.** {caption}", ''])
        else:
            out.extend([f"**{label}.**", ''])
        out.extend([f"![{label}](__ACS_FIG_{index}__)", ''])
        out.extend(cls._render_float_notes(node))
        return out

    @classmethod
    def _render_float_notes(cls, node: Tag) -> List[str]:
        """Footnotes attached to a figure, scheme or table.

        ACS marks each one ``div.fn`` and starts it with its own marker
        ("a Yields determined by ..."), so they are emitted as a list under
        the float rather than renumbered.
        """
        notes = []
        for fn in node.select('div.fn'):
            text = cls._text_md(fn)
            if text:
                notes.append(f"- {text}")
        return notes + [''] if notes else []

    @classmethod
    def extract_body_from_html(cls, html: str, base_level: int = 2) -> str:
        """Walk the article body, keeping every h2 section in order.

        Sections are dropped by removing their markup up front rather than by
        skipping forward from a heading. On this platform the abstract's h2,
        the body prose and every back-matter h2 are *siblings* in one
        ``div.widget-items``, and the body follows the "Abstract" heading with
        no heading of its own -- so "skip until the next h2" would discard the
        entire paper.
        """
        if not html:
            return ''
        soup = BeautifulSoup(html, 'html.parser')
        body = (soup.select_one('div.article-body div.content')
                or soup.select_one('div.article-body'))
        if body is None:
            return ''

        body = BeautifulSoup(str(body), 'html.parser')
        for selector in _ACS_DROP_SELECTORS:
            for el in body.select(selector):
                el.decompose()

        # Emitted as their own markdown sections; must not appear twice.
        for el in body.select('section.abstract, div.graphical-abstract, '
                              'div.widget-ArticleDataSupplements'):
            el.decompose()

        # Each remaining unwanted section is an <h2> plus the single <div>
        # that follows it.
        for h2 in list(body.find_all('h2')):
            if h2.get_text(' ', strip=True).lower() not in _ACS_SECTION_DROP:
                continue
            for sibling in list(h2.next_siblings):
                if isinstance(sibling, Tag) and sibling.name:
                    if sibling.name.lower() == 'h2':
                        break
                    sibling.decompose()
                    break
            h2.decompose()

        # fig_seq counts floats in document order, matching the keys
        # extract_figures_from_html() hands the downloader.
        ctx = {'fig_seq': 0}
        blocks: List[str] = []
        for child in body.children:
            blocks.extend(cls._render_block(child, base_level, ctx))

        md = re.sub(r'\n{3,}', '\n\n', '\n'.join(blocks)).strip()
        return md

    # ==================================================================
    # Contract
    # ==================================================================

    async def get_pdf_url(self, doi: str = None) -> Optional[str]:
        html = ''
        if self.page is not None:
            try:
                html = await self.page.content()
            except Exception:
                html = ''
        if html:
            url = self.extract_metadata_from_html(html).get('_pdf_url')
            if url:
                return url
        doi = (doi or self.doi or '').strip()
        return f"{self.ACS_BASE}/doi/pdf/{doi}" if doi else None

    async def get_supplemental_url(self, doi: str) -> Optional[str]:
        return None

    async def extract_references(self, html: str) -> list:
        return self.extract_references_from_html(html) if html else []

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def get_fulltext_url(self, page) -> str:
        try:
            return page.url or ''
        except Exception:
            return f"{self.ACS_BASE}/doi/{self.doi}" if self.doi else ''

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'ACSHandler'
        )
        doi = self.doi
        set_actual_base_url(self, page)

        try:
            try:
                html = await page.content()
            except Exception:
                html = ''

            metadata = self.extract_metadata_from_html(html)
            metadata['doi'] = doi or metadata.get('doi', '')
            pdf_url = metadata.pop('_pdf_url', None)

            metadata['references'] = self.extract_references_from_html(html)
            if metadata['references']:
                print(f"  ✓ 参考文献: {len(metadata['references'])} 条")

            figure_urls = self.extract_figures_from_html(html)
            if figure_urls:
                print(f"  ✓ 图片: {len(figure_urls)} 个")

            supp_urls, supp_desc, supp_summary = self.extract_supplemental_from_html(html)
            if supp_urls:
                print(f"  ✓ 补充材料: {len(supp_urls)} 个")
            if supp_summary:
                metadata['_supplemental_summary_md'] = supp_summary

            body_md = self.extract_body_from_html(html)
            if body_md:
                metadata['_body_md'] = body_md
                print(f"  ✓ 正文: {len(body_md):,} 字符")

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figure_urls,
                    'supplemental_urls': supp_urls,
                    'supplemental_descriptions': supp_desc,
                },
                'fulltext_data': html,
                'journal_name': 'acs',
            }
        finally:
            if managed_context is not None:
                try:
                    await managed_context.close()
                except Exception:
                    pass
            if managed_browser is not None:
                try:
                    await managed_browser.close()
                except Exception:
                    pass
            if managed_playwright is not None:
                try:
                    await managed_playwright.stop()
                except Exception:
                    pass
            if managed_context is not None:
                self.page = None

    # ==================================================================
    # Markdown
    # ==================================================================

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        md: List[str] = [f"# {metadata.get('title') or 'ACS Article'}", '']

        authors = metadata.get('authors') or []
        if authors:
            md.extend(['**Authors:** ' + ', '.join(authors), ''])

        md.extend(['## Publication', ''])
        for key, label in (('journal', 'Journal'), ('volume', 'Volume'),
                           ('issue', 'Issue'), ('pages', 'Pages'),
                           ('year', 'Year'), ('doi', 'DOI'),
                           ('publisher', 'Publisher')):
            value = (metadata.get(key) or '').strip()
            if value:
                md.extend([f"**{label}:** {value}", ''])

        # Abstract, with the Visual Abstract image and the topical vocabulary
        # alongside it -- a chunker that keeps the abstract keeps these too.
        md.extend(['---', '', '## Abstract', ''])
        md.extend([(metadata.get('abstract') or '[No abstract available.]').strip(), ''])

        key_image = kwargs.get('key_image_filename') or metadata.get('key_image')
        if key_image:
            md.extend(['**Visual Abstract**', '', f"![Visual Abstract]({key_image})", ''])
        elif metadata.get('key_image_url'):
            md.extend(['**Visual Abstract**', '',
                       f"![Visual Abstract]({metadata['key_image_url']})", ''])

        if metadata.get('_subjects'):
            md.extend(['**Subjects:** ' + ', '.join(metadata['_subjects']), ''])
        if metadata.get('_keywords'):
            md.extend(['**Keywords:** ' + ', '.join(metadata['_keywords']), ''])

        body_md = (metadata.get('_body_md') or '').strip()
        if not body_md and isinstance(article_text, str) and article_text.strip():
            body_md = self.extract_body_from_html(article_text)
        if body_md:
            body_md = self._resolve_figures(body_md, kwargs)
            md.extend(['---', '', body_md, ''])

        supp_summary = (metadata.get('_supplemental_summary_md') or '').strip()
        supp_urls = kwargs.get('supplemental_urls') or []
        supp_downloads = kwargs.get('supplemental_downloads') or []
        supp_desc = kwargs.get('supplemental_descriptions') or {}
        if supp_summary or supp_urls or supp_downloads:
            md.extend(['---', '', '## Supporting Information', ''])
            if supp_summary:
                md.extend([supp_summary, ''])
            if supp_downloads:
                for item in supp_downloads:
                    md.append(f"- `{item}`")
                md.append('')
            elif supp_urls:
                for url in supp_urls:
                    md.append(f"- [{supp_desc.get(url, url)}]({url})")
                md.append('')

        references = metadata.get('references') or []
        if references:
            md.extend(['---', '', '## References', ''])
            for index, ref in enumerate(references, 1):
                md.extend([f"[{index}] {ref}", ''])

        return '\n'.join(md).rstrip() + '\n'

    @staticmethod
    def _resolve_figures(body_md: str, kwargs: dict) -> str:
        """Swap ``__ACS_FIG_n__`` for the downloaded filename, else the URL."""
        filenames = kwargs.get('figure_filenames') or {}
        figure_urls = kwargs.get('figure_urls') or {}

        def _sub(match: 're.Match') -> str:
            index = match.group(1)
            local = filenames.get(index) or filenames.get(int(index))
            if local:
                return str(local)
            info = figure_urls.get(f'fig_{index}') or {}
            return info.get('url', '') if isinstance(info, dict) else str(info)

        return re.sub(r'__ACS_FIG_(\d+)__', _sub, body_md)

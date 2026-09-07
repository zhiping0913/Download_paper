"""Wiley Online Library handler (onlinelibrary.wiley.com, DOI prefix 10.1002).

Wiley serves the whole article as server-rendered HTML, so there is no body
API to chase:

    <meta name="citation_*">                 bibliographic metadata
    section.article-section__abstract        abstract
    section.article-section__full            body, h2-delimited
    figure.figure                            figures (+ full-size asset URL)
    div.article-table-content                tables, caption and Note footnote
    div.inline-equation / span.math          display and inline math
    ul.rlist.separator                       references
    section.article-section__supporting      Supporting Information

Math is the reason this handler asks for the page *source* rather than the
rendered DOM. Wiley ships every formula as MathML with a
``<annotation encoding="application/x-tex">`` holding the original LaTeX --
``$$ \\boldsymbol{T}=\\left(\\begin{array}{lll}...\\end{array}\\right) $$`` --
but MathJax replaces the lot once it runs. :func:`fetch_view_source_html`
(the same approach the Optica handler uses) gets the untouched markup, and
the result is cached as ``source.html`` beside ``page.html``. Where an
annotation is missing, the MathML itself is converted, so a formula is never
dropped.

The PDF is not the ``citation_pdf_url`` meta value (``/doi/pdf/{doi}``, a
viewer page) but ``/doi/pdfdirect/{doi}?download=true``, which serves the
file itself.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag

from core.utilities import fetch_view_source_html
from html_to_md_converter import mathml_to_latex_pandoc
from publisher.base import PublisherHandler
from publisher.wildcard import (
    init_extract_all_page,
    render_heading_md,
    set_actual_base_url,
)


# h2 headings that belong to the site chrome rather than the article.
_WILEY_H2_SKIP = frozenset({
    'abstract',                 # emitted as its own section
    'references',               # emitted from metadata['references']
    'supporting information',   # emitted as the supplemental section
    'citing literature',
    'figures',
    'related',
    'information',
    'article metrics',
    'share qr code',
    'export citation',
    'additional links',
    'log in to wiley online library',
    'change password',
    'password changed successfully',
    'create a new account',
    'forgot your password?',
    'request username',
    'recommended',
})

# Elements inside the body carrying no article content.
_WILEY_DROP_SELECTORS = (
    'div.extra-links',          # "Get full text" / Crossref badges
    'span.hidden',
    'div.figure-extra',         # "Open in figure viewer" / "Download PowerPoint"
    'a.open-figure-link',
    'a.ppt-figure-link',
    'span.fallback__mathEquation',   # empty placeholder for the MathJax image
    'script',
    'style',
    'noscript',
)

_WILEY_NOISE_LINES = frozenset({
    'open in figure viewer',
    'download powerpoint',
    'caption',
    'abstract',
})


class WileyHandler(PublisherHandler):
    """Full-text handler for Wiley Online Library."""

    WILEY_BASE = 'https://onlinelibrary.wiley.com'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.WILEY_BASE

    # ==================================================================
    # Math -- one pipeline for every element
    # ==================================================================

    @staticmethod
    def _strip_delims(latex: str) -> str:
        latex = (latex or '').strip()
        latex = re.sub(r'^\\[\(\[]|\\[\)\]]$', '', latex).strip()
        if latex.startswith('$$') and latex.endswith('$$') and len(latex) > 4:
            latex = latex[2:-2].strip()
        elif latex.startswith('$') and latex.endswith('$') and len(latex) > 2:
            latex = latex[1:-1].strip()
        return latex

    @classmethod
    def _math_latex(cls, node: Tag) -> str:
        """LaTeX for one formula.

        Wiley's ``<annotation encoding="application/x-tex">`` carries the
        author's original source, so it is preferred; the MathML is converted
        only when there is none. Both live inside the same ``<math>``, and
        MathJax removes both when it runs -- which is why this handler works
        from the page source.
        """
        math = node if node.name == 'math' else node.find('math')
        if math is None:
            return ''
        annotation = math.find('annotation')
        if annotation is not None:
            latex = cls._strip_delims(annotation.get_text())
            if latex:
                return latex
        try:
            return cls._strip_delims(mathml_to_latex_pandoc(str(math)) or '')
        except Exception:
            return ''

    @classmethod
    def _inline_md(cls, node) -> str:
        """Render an inline subtree to markdown, formulas included.

        Every text-bearing element goes through here -- paragraphs, captions,
        list items, headings, table cells, references -- so a formula can
        never be lost to a bare ``get_text()``.
        """
        if node is None:
            return ''
        if isinstance(node, NavigableString):
            return re.sub(r'\s+', ' ', str(node))
        if not isinstance(node, Tag) or not node.name:
            return ''

        name = node.name.lower()
        classes = node.get('class') or []

        if name in ('script', 'style', 'noscript'):
            return ''
        if name == 'math':
            latex = cls._math_latex(node)
            return f"${latex}$" if latex else ''
        if 'fallback__mathEquation' in classes:
            return ''
        if name == 'img' and 'tex2gif' in (node.get('src') or ''):
            # An inline formula shipped as an image; keep it in the text flow.
            index = cls._asset_index(node)
            if index:
                return f" ![Equation](__WILEY_FIG_{index}__) "
            return f" ![Equation]({cls._abs_asset(node.get('src'))}) "

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
            # Cross-references to figures, tables and citations point at
            # anchors in a page we are not shipping.
            if (not href or href.startswith('#') or href.startswith('javascript:')
                    or 'bibLink' in classes):
                return inner
            resolved = urljoin(cls.WILEY_BASE, href)
            if not urlparse(resolved).netloc:
                return inner
            return f"[{text}]({cls._md_url(resolved)})" if text else ''

        return inner

    @staticmethod
    def _md_url(url: str) -> str:
        """Percent-encode the parentheses that would end a markdown target."""
        return (url or '').replace('(', '%28').replace(')', '%29')

    @staticmethod
    def _wrap(inner: str, mark: str) -> str:
        """Apply an emphasis marker, leaving surrounding spaces outside it."""
        stripped = inner.strip()
        if not stripped:
            return inner
        lead = inner[:len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()):]
        return f"{lead}{mark}{stripped}{mark}{trail}"

    @classmethod
    def _text_md(cls, node) -> str:
        return re.sub(r'\s+', ' ', cls._inline_md(node)).strip()

    @staticmethod
    def _is_noise(text: str) -> bool:
        return text.strip().lower().rstrip('.') in _WILEY_NOISE_LINES

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
    def extract_metadata_from_html(cls, html: str) -> dict:
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')

        authors = []
        for raw in cls._meta_all(soup, 'citation_author'):
            if ',' in raw:
                surname, given = raw.split(',', 1)
                raw = f"{given.strip()} {surname.strip()}".strip()
            authors.append(raw)

        first = cls._meta(soup, 'citation_firstpage')
        last = cls._meta(soup, 'citation_lastpage')
        pages = f"{first}-{last}" if first and last else first

        date = cls._meta(soup, 'citation_publication_date')
        m = re.search(r'(19|20|21)\d{2}', date)
        graphical_md, key_image = cls.extract_graphical_abstract(html)

        return {
            'title': re.sub(r'\s+', ' ', cls._meta(soup, 'citation_title')).strip(),
            'doi': cls._meta(soup, 'citation_doi'),
            'authors': authors,
            'journal': cls._meta(soup, 'citation_journal_title'),
            'volume': cls._meta(soup, 'citation_volume'),
            'issue': cls._meta(soup, 'citation_issue'),
            'pages': pages,
            'year': m.group(0) if m else '',
            'publication_date': date,
            'issn': cls._meta(soup, 'citation_issn'),
            'publisher': cls._meta(soup, 'citation_publisher') or 'Wiley',
            'abstract': cls.extract_abstract_from_html(html),
            'corresponding_author_emails': [],
            '_keywords': cls._extract_keywords(soup),
            '_graphical_abstract_md': graphical_md,
            'key_image_url': key_image,
        }

    @classmethod
    def _extract_keywords(cls, soup: BeautifulSoup) -> List[str]:
        keywords = []
        for section in soup.select('section.keywords, div.keywords'):
            for a in section.find_all(['a', 'li']):
                text = a.get_text(' ', strip=True)
                if text and text.lower() != 'keywords':
                    keywords.append(text)
            if keywords:
                break
        if not keywords:
            raw = cls._meta(soup, 'citation_keywords')
            keywords = [k.strip() for k in re.split(r'[;,]', raw) if k.strip()]
        return keywords

    @classmethod
    def extract_graphical_abstract(cls, html: str) -> Tuple[str, str]:
        """``(text_md, image_url)`` for the Graphical Abstract.

        Wiley puts a short plain-language summary and a key image in
        ``div.graphical-abstract``; the image is the article's cover graphic,
        so it is reported as ``key_image_url`` and the downloader saves it as
        key_image.png like every other publisher's.
        """
        if not html:
            return '', ''
        soup = BeautifulSoup(html, 'html.parser')
        block = soup.select_one('div.graphical-abstract, section.graphical-abstract')
        if block is None:
            return '', ''

        image = ''
        img = block.find('img')
        if img is not None:
            image = cls._abs_asset(img.get('data-lg-src') or img.get('src'))
        anchor_el = block.find('a', href=re.compile('/cms/asset/'))
        if anchor_el is not None:
            image = cls._abs_asset(anchor_el['href'])

        clone = BeautifulSoup(str(block), 'html.parser')
        for el in clone.select('figure, div.figure-extra, script, style'):
            el.decompose()
        parts = []
        for node in clone.find_all(['p', 'div']):
            if node.find(['p', 'div']) is not None:
                continue
            text = cls._text_md(node)
            if text and text not in parts and not cls._is_noise(text):
                parts.append(text)
        if not parts:
            text = cls._text_md(clone)
            if text and not cls._is_noise(text):
                parts.append(text)
        return '\n\n'.join(parts), image

    @classmethod
    def extract_abstract_from_html(cls, html: str) -> str:
        if not html:
            return ''
        soup = BeautifulSoup(html, 'html.parser')
        section = (soup.select_one('section.article-section__abstract')
                   or soup.select_one('div.article-section__content.en.main'))
        if section is None:
            return ''
        parts = []
        for p in section.find_all('p'):
            text = cls._text_md(p)
            if text and not cls._is_noise(text):
                parts.append(text)
        return '\n\n'.join(parts)

    # ==================================================================
    # Figures
    # ==================================================================

    # Everything that becomes a downloadable image, in document order:
    # figures, and the GIFs older Wiley articles use instead of MathML
    # (10.1002/cssc.201000245 renders each equation as tex2gif-eqn-N.gif).
    _ASSET_SELECTOR = 'figure, img[src*="tex2gif"]'

    @classmethod
    def _number_assets(cls, soup: BeautifulSoup) -> None:
        """Stamp every downloadable asset with its document-order index.

        The figure scan and the body walk parse the page separately; numbering
        the nodes in the markup means they cannot disagree about which asset is
        which, however either one is later changed. An equation GIF inside a
        figure is skipped so it is not counted twice.
        """
        index = 0
        for node in soup.select(cls._ASSET_SELECTOR):
            if node.name == 'img' and node.find_parent('figure') is not None:
                continue
            index += 1
            node['data-dp-asset'] = str(index)

    @staticmethod
    def _asset_index(node: Tag) -> str:
        return (node.get('data-dp-asset') or '').strip()

    @classmethod
    def _equation_image(cls, node: Tag) -> Optional[Tag]:
        """The GIF standing in for a formula, if this block uses one."""
        img = node.find('img', src=re.compile('tex2gif'))
        return img

    @classmethod
    def extract_figures_from_html(cls, html: str) -> dict:
        """``{'fig_N': {'url', 'original_url', 'caption', 'label'}}``."""
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')
        cls._number_assets(soup)
        figures = {}
        for node in soup.select(cls._ASSET_SELECTOR):
            index = cls._asset_index(node)
            if not index:
                continue
            if node.name == 'img':
                # An equation GIF: no caption, and its number comes from the
                # article ("((1))"), not from the asset sequence.
                url = cls._abs_asset(node.get('data-lg-src') or node.get('src'))
                if not url:
                    continue
                figures[f'fig_{index}'] = {
                    'url': url,
                    'original_url': url,
                    'caption': '',
                    'label': cls._equation_label(node) or 'Equation',
                }
                continue
            large, inline = cls._figure_urls(node)
            if not (large or inline):
                continue
            label_el = node.find(class_='figure__title')
            label = (label_el.get_text(' ', strip=True)
                     if label_el is not None else f'Figure {index}')
            figures[f'fig_{index}'] = {
                'url': large or inline,
                'original_url': inline or large,
                'caption': cls._figure_caption(node),
                'label': label,
            }
        return figures

    @classmethod
    def _abs_asset(cls, url: str) -> str:
        url = (url or '').strip()
        if not url:
            return ''
        if url.startswith('//'):
            return 'https:' + url
        return urljoin(cls.WILEY_BASE, url)

    @staticmethod
    def _equation_label(img: Tag) -> str:
        """The "(1)" printed beside an equation, if the article numbers it."""
        block = img.find_parent('div', class_='inline-equation')
        if block is None:
            return ''
        label_el = block.find('span', class_='inline-equation__label')
        if label_el is None:
            return ''
        # Wiley writes the number as "((1))".
        text = label_el.get_text(' ', strip=True).strip()
        return re.sub(r'^\((\(.*\))\)$', r'\1', text) or text

    @classmethod
    def _figure_urls(cls, fig: Tag) -> Tuple[str, str]:
        """``(full_size, inline)`` asset URLs for a figure.

        The wrapping ``<a href>`` and the image's ``data-lg-src`` both point
        at the full-size ``/cms/asset/<uuid>/<name>-m.jpg``; ``src`` is often
        a smaller PNG rendition.
        """
        def _abs(u: str) -> str:
            u = (u or '').strip()
            if not u:
                return ''
            if u.startswith('//'):
                return 'https:' + u
            return urljoin(cls.WILEY_BASE, u)

        large = ''
        anchor = fig.find('a', href=True)
        if anchor and '/cms/asset/' in anchor['href']:
            large = anchor['href']
        img = fig.find('img')
        inline = ''
        if img is not None:
            large = large or (img.get('data-lg-src') or '')
            inline = img.get('src') or ''
        return _abs(large), _abs(inline)

    @classmethod
    def _figure_caption(cls, fig: Tag) -> str:
        cap = fig.find('figcaption')
        if cap is None:
            return ''
        clone = BeautifulSoup(str(cap), 'html.parser')
        for selector in ('div.figure-extra', 'a.open-figure-link',
                         'a.ppt-figure-link', 'strong.figure__title'):
            for el in clone.select(selector):
                el.decompose()
        text = cls._text_md(clone)
        return '' if cls._is_noise(text) else text

    # ==================================================================
    # Supporting Information
    # ==================================================================

    @classmethod
    def extract_supplemental_from_html(cls, html: str) -> Tuple[List[str], Dict[str, str], str]:
        """``(urls, descriptions, summary_md)`` for Supporting Information."""
        if not html:
            return [], {}, ''
        soup = BeautifulSoup(html, 'html.parser')

        urls, descriptions = [], {}
        for a in soup.find_all('a', href=re.compile('downloadSupplement')):
            href = (a.get('href') or '').strip()
            if not href:
                continue
            url = urljoin(cls.WILEY_BASE, href)
            if url in descriptions:
                continue
            urls.append(url)
            descriptions[url] = a.get_text(' ', strip=True) or url

        summary = ''
        section = soup.select_one('section.article-section__supporting')
        if section is not None:
            clone = BeautifulSoup(str(section), 'html.parser')
            for el in clone.select('h2, div.extra-links, script, style'):
                el.decompose()
            lines = []
            for node in clone.find_all(['p', 'td', 'div']):
                if node.find(['p', 'td', 'div']) is not None:
                    continue                      # keep the innermost only
                text = cls._text_md(node)
                if text and text not in lines and not cls._is_noise(text):
                    lines.append(text)
            summary = '\n\n'.join(lines)
        return urls, descriptions, summary

    # ==================================================================
    # References
    # ==================================================================

    @classmethod
    def extract_references_from_html(cls, html: str) -> list:
        """One flat string per ``<li>`` of ``ul.rlist.separator``.

        Wiley wraps every field of a citation in its own element (author,
        articleTitle, journalTitle, vol, pubYear) and appends a "get full
        text" widget; stripping the markup and keeping the text is all that
        is wanted, with the DOI link kept as a link.
        """
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        refs = []
        for ul in soup.select('ul.rlist.separator'):
            for li in ul.find_all('li', recursive=False):
                text = cls._render_reference(li)
                if text:
                    refs.append(text)
            if refs:
                break
        return refs

    @classmethod
    def _render_reference(cls, li: Tag) -> str:
        clone = BeautifulSoup(str(li), 'html.parser')
        # The leading number is re-emitted by the caller; the link widgets
        # are UI, not citation text.
        for selector in ('span.bullet', 'div.extra-links', 'span.hidden',
                         'div.getFTR__content', 'script', 'style'):
            for el in clone.select(selector):
                el.decompose()
        text = cls._text_md(clone)
        text = re.sub(r'\s+([,.;:])', r'\1', text)
        return re.sub(r'\s{2,}', ' ', text).strip(' ,;')

    # ==================================================================
    # Body
    # ==================================================================

    _BLOCK_TAGS = frozenset({'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                             'ul', 'ol', 'table', 'figure', 'section'})

    @classmethod
    def _is_block(cls, node) -> bool:
        if not isinstance(node, Tag) or not node.name:
            return False
        classes = node.get('class') or []
        return (node.name.lower() in cls._BLOCK_TAGS
                or 'article-table-content' in classes
                or 'inline-equation' in classes)

    @classmethod
    def _has_block_descendant(cls, node: Tag) -> bool:
        return any(cls._is_block(d) for d in node.descendants if isinstance(d, Tag))

    @classmethod
    def _render_block(cls, node, level: int, ctx: dict) -> List[str]:
        if isinstance(node, NavigableString):
            text = re.sub(r'\s+', ' ', str(node)).strip()
            return [text, ''] if text and not cls._is_noise(text) else []
        if not isinstance(node, Tag) or not node.name:
            return []

        name = node.name.lower()
        classes = node.get('class') or []

        if name in ('script', 'style', 'noscript'):
            return []
        if name == 'figure':
            return cls._render_figure(node, ctx)
        if 'article-table-content' in classes:
            return cls._render_table_block(node)
        if 'inline-equation' in classes:
            return cls._render_equation(node)

        if name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            if node.get_text(' ', strip=True).lower() in _WILEY_H2_SKIP:
                ctx['skip_section'] = True
                return []
            ctx['skip_section'] = False
            depth = {'h1': 0, 'h2': 0, 'h3': 1, 'h4': 2, 'h5': 3, 'h6': 4}[name]
            rendered = render_heading_md(node, '#' * (level + depth),
                                         converter=cls._text_md)
            return [rendered, ''] if rendered else []

        if name == 'p':
            text = cls._text_md(node)
            return [text, ''] if text and not cls._is_noise(text) else []
        if name in ('ul', 'ol'):
            lines = cls._render_list(node)
            return lines + [''] if lines else []
        if name == 'table':
            md = cls._render_table(node)
            return [md, ''] if md else []

        # A wrapper: recurse, buffering runs of inline children into one
        # paragraph so a paragraph containing a display equation does not get
        # shredded into one fragment per child.
        if cls._has_block_descendant(node):
            out: List[str] = []
            buffer: List[str] = []

            def _flush() -> None:
                text = re.sub(r'\s+', ' ', ''.join(buffer)).strip()
                buffer.clear()
                if text and not cls._is_noise(text):
                    out.extend([text, ''])

            for child in node.children:
                if isinstance(child, Tag) and (cls._is_block(child)
                                               or cls._has_block_descendant(child)):
                    _flush()
                    out.extend(cls._render_block(child, level, ctx))
                else:
                    buffer.append(cls._inline_md(child))
            _flush()
            return out

        text = cls._text_md(node)
        return [text, ''] if text and not cls._is_noise(text) else []

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
    def _render_table(cls, table: Tag) -> str:
        """Render a table to markdown, honouring rowspan and colspan.

        Wiley groups repeated values with rowspan -- Table 2 of
        10.1002/eng2.70294 has ``<td rowspan="4">ECP</td>`` -- so the rows
        underneath carry one cell fewer. Reading cells positionally puts every
        value in the wrong column from there on, and the result looks like a
        clean table while quietly reporting each score under the wrong
        heading. Cells are placed on a grid instead, with spans occupying the
        slots they cover.
        """
        grid: List[List[Optional[str]]] = []

        def _place(row_index: int, text: str, rowspan: int, colspan: int) -> None:
            while len(grid) <= row_index + rowspan - 1:
                grid.append([])
            row = grid[row_index]
            col = 0
            while col < len(row) and row[col] is not None:
                col += 1
            for r in range(row_index, row_index + rowspan):
                target = grid[r]
                while len(target) < col + colspan:
                    target.append(None)
                for c in range(col, col + colspan):
                    # A spanned-into slot repeats the value: markdown has no
                    # way to express the span, and leaving it blank would read
                    # as missing data.
                    target[c] = text if (r == row_index and c == col) else text

        def _span(cell: Tag, attr: str) -> int:
            try:
                value = int((cell.get(attr) or '1').strip())
            except (TypeError, ValueError):
                return 1
            return max(1, min(value, 50))

        rows = table.find_all('tr')
        for index, tr in enumerate(rows):
            cells = tr.find_all(['td', 'th'])
            if not cells:
                continue
            while len(grid) <= index:
                grid.append([])
            for cell in cells:
                _place(index, cls._text_md(cell),
                       _span(cell, 'rowspan'), _span(cell, 'colspan'))

        grid = [r for r in grid if any(c for c in r)]
        if not grid:
            return ''
        width = max(len(r) for r in grid)
        table_rows = [[(c or '') for c in r] + [''] * (width - len(r)) for r in grid]

        out = ['| ' + ' | '.join(table_rows[0]) + ' |', '|' + '---|' * width]
        for row in table_rows[1:]:
            out.append('| ' + ' | '.join(row) + ' |')
        return '\n'.join(out)

    @classmethod
    def _render_table_block(cls, node: Tag) -> List[str]:
        """Caption, grid, then the "Note:" footnotes that follow the table."""
        out: List[str] = []
        caption = node.find('header', class_='article-table-caption')
        if caption is not None:
            label_el = caption.find('span', class_='table-caption__label')
            label = ''
            clone = BeautifulSoup(str(caption), 'html.parser')
            if label_el is not None:
                # Wiley writes "TABLE  1." with two spaces.
                label = re.sub(r'\s+', ' ',
                               label_el.get_text(' ', strip=True)).rstrip('. ')
                stale = clone.find('span', class_='table-caption__label')
                if stale is not None:
                    stale.decompose()
            title = cls._text_md(clone)
            if label and title:
                out.extend([f"**{label}.** {title}", ''])
            elif label or title:
                out.extend([f"**{label}.**" if label else title, ''])

        table = node.find('table')
        if table is not None:
            md = cls._render_table(table)
            if md:
                out.extend([md, ''])

        for foot in node.select('div.article-section__table-footnotes'):
            text = cls._text_md(foot)
            if text:
                # Emitted plainly: the note already opens with its own bold
                # "Note", and wrapping the line in italics would nest the two
                # markers into "**Note*: ...*".
                out.extend([text, ''])
        return out

    @classmethod
    def _render_equation(cls, node: Tag) -> List[str]:
        """A display equation: LaTeX where Wiley has it, else its image.

        Articles predating Wiley's MathML rendering (2010-era ChemSusChem,
        say) ship each formula as a pre-rendered GIF with no machine-readable
        source at all. Dropping those would silently lose every equation in
        the paper, so the image is emitted as a figure instead -- downloaded
        like any other asset and kept with its printed number.
        """
        label_el = (node.find('span', class_='inline-equation__label')
                    or node.find(class_=re.compile('equation-label|disp-formula__label')))
        label = ''
        if label_el is not None:
            label = re.sub(r'^\((\(.*\))\)$', r'\1',
                           label_el.get_text(' ', strip=True).strip())

        latex = cls._math_latex(node)
        if latex:
            line = f"$${latex}$$"
            if label:
                line += f" {label}"
            return [line, '']

        img = cls._equation_image(node)
        if img is not None:
            index = cls._asset_index(img)
            alt = f"Equation {label}" if label else 'Equation'
            if index:
                line = f"![{alt}](__WILEY_FIG_{index}__)"
            else:
                line = f"![{alt}]({cls._abs_asset(img.get('src'))})"
            if label:
                line += f" {label}"
            return [line, '']
        return []

    @classmethod
    def _render_figure(cls, node: Tag, ctx: dict) -> List[str]:
        ctx['fig_seq'] += 1
        index = cls._asset_index(node) or ctx['fig_seq']
        label_el = node.find(class_='figure__title')
        label = (label_el.get_text(' ', strip=True)
                 if label_el is not None else f'Figure {index}')
        caption = cls._figure_caption(node)
        out: List[str] = []
        if caption:
            out.extend([f"**{label}.** {caption}", ''])
        else:
            out.extend([f"**{label}.**", ''])
        out.extend([f"![{label}](__WILEY_FIG_{index}__)", ''])
        return out

    @classmethod
    def extract_body_from_html(cls, html: str, base_level: int = 2) -> str:
        if not html:
            return ''
        soup = BeautifulSoup(html, 'html.parser')
        body = soup.select_one('section.article-section__full')
        if body is None:
            return ''

        # Number assets on the *whole* document first, so the indices match
        # the ones extract_figures_from_html() hands the downloader; then
        # narrow to the body.
        cls._number_assets(soup)
        body = soup.select_one('section.article-section__full')
        body = BeautifulSoup(str(body), 'html.parser')
        for selector in _WILEY_DROP_SELECTORS:
            for el in body.select(selector):
                el.decompose()
        # Emitted as their own markdown sections.
        for el in body.select('section.article-section__abstract, '
                              'section.article-section__supporting, '
                              'section.article-section__references'):
            el.decompose()

        ctx = {'fig_seq': 0, 'skip_section': False}
        blocks: List[str] = []
        for child in body.children:
            blocks.extend(cls._render_block(child, base_level, ctx))
        return re.sub(r'\n{3,}', '\n\n', '\n'.join(blocks)).strip()

    # ==================================================================
    # Contract
    # ==================================================================

    async def get_pdf_url(self, doi: str = None) -> Optional[str]:
        """The direct-download PDF URL.

        ``citation_pdf_url`` advertises ``/doi/pdf/{doi}``, which is the
        viewer; ``/doi/pdfdirect/{doi}?download=true`` serves the file.
        """
        doi = (doi or self.doi or '').strip()
        if not doi:
            return None
        return f"{self.WILEY_BASE}/doi/pdfdirect/{doi}?download=true"

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
            return f"{self.WILEY_BASE}/doi/{self.doi}" if self.doi else ''

    async def extract_metadata(self, page) -> dict:
        try:
            html = await page.content()
        except Exception:
            html = ''
        return self.extract_metadata_from_html(html)

    def _save_source(self, html: str) -> None:
        """Keep the un-rendered page beside the other captures."""
        if not self.captured_data_dir or not html:
            return
        try:
            from pathlib import Path
            out = Path(self.captured_data_dir) / 'source.html'
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html, encoding='utf-8')
            print(f"  ✓ source.html 已保存 ({out.stat().st_size:,} bytes)")
        except OSError as exc:
            print(f"  ⚠️  source.html 保存失败: {exc}")

    @staticmethod
    def _count_math_source(html: str) -> int:
        """Formulas still carrying recoverable source in *html*.

        Used to decide whether the re-fetched source beats what we already
        have: MathJax removes both the TeX annotation and the MathML, so a
        rendered copy scores zero.
        """
        if not html:
            return 0
        return len(re.findall(r'<annotation[\s>]', html, re.IGNORECASE))

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'WileyHandler'
        )
        doi = self.doi
        set_actual_base_url(self, page)

        try:
            try:
                rendered = await page.content()
            except Exception:
                rendered = ''

            # Prefer the page source: MathJax strips both the TeX annotation
            # and the MathML from the DOM once it runs, so the rendered copy
            # has no recoverable formulas at all.
            html = rendered
            try:
                source = await fetch_view_source_html(page)
            except Exception as exc:
                print(f"  ⚠️  view-source 抓取失败: {type(exc).__name__}: {exc}")
                source = ''
            if source:
                self._save_source(source)
                src_n = self._count_math_source(source)
                cur_n = self._count_math_source(rendered)
                if src_n >= cur_n:
                    print(f"  ↪ Wiley: 使用 view-source 原始 HTML "
                          f"({len(source):,} 字符, {src_n} 个公式源 → 原有 {cur_n} 个)")
                    html = source

            metadata = self.extract_metadata_from_html(html)
            metadata['doi'] = doi or metadata.get('doi', '')

            metadata['references'] = self.extract_references_from_html(html)
            if metadata['references']:
                print(f"  ✓ 参考文献: {len(metadata['references'])} 条")

            figure_urls = self.extract_figures_from_html(html)
            if figure_urls:
                print(f"  ✓ 图片: {len(figure_urls)} 个")

            supp_urls, supp_desc, supp_summary = self.extract_supplemental_from_html(html)
            if supp_urls:
                print(f"  ✓ 补充材料: {len(supp_urls)} 个")
            if supp_summary and supp_urls:
                metadata['_supplemental_summary_md'] = supp_summary

            body_md = self.extract_body_from_html(html)
            if body_md:
                metadata['_body_md'] = body_md
                print(f"  ✓ 正文: {len(body_md):,} 字符")

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': await self.get_pdf_url(doi),
                    'figure_urls': figure_urls,
                    'supplemental_urls': supp_urls,
                    'supplemental_descriptions': supp_desc,
                },
                'fulltext_data': html,
                'journal_name': 'wiley',
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
        md: List[str] = [f"# {metadata.get('title') or 'Wiley Article'}", '']

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

        abstract = (metadata.get('abstract') or '').strip()
        md.extend(['---', '', '## Abstract', '',
                   abstract or '[No abstract available.]', ''])
        if metadata.get('_keywords'):
            md.extend(['**Keywords:** ' + ', '.join(metadata['_keywords']), ''])

        # Graphical Abstract: the plain-language summary and its cover image.
        graphical = (metadata.get('_graphical_abstract_md') or '').strip()
        key_image = (kwargs.get('key_image_filename')
                     or metadata.get('key_image_url') or '')
        if graphical or key_image:
            md.extend(['## Graphical Abstract', ''])
            if key_image:
                md.extend([f"![Graphical Abstract]({key_image})", ''])
            if graphical:
                md.extend([graphical, ''])

        body_md = (metadata.get('_body_md') or '').strip()
        if not body_md and isinstance(article_text, str) and article_text.strip():
            body_md = self.extract_body_from_html(article_text)
        if body_md:
            md.extend(['---', '', self._resolve_figures(body_md, kwargs), ''])

        supp_summary = (metadata.get('_supplemental_summary_md') or '').strip()
        supp_urls = kwargs.get('supplemental_urls') or []
        supp_downloads = kwargs.get('supplemental_downloads') or []
        supp_desc = kwargs.get('supplemental_descriptions') or {}
        # No files means no section: the summary only describes the files.
        if supp_urls or supp_downloads:
            md.extend(['---', '', '## Supporting Information', ''])
            if supp_summary:
                md.extend([supp_summary, ''])
            if supp_downloads:
                for item in supp_downloads:
                    md.append(f"- `{item}`")
                md.append('')
            else:
                for url in supp_urls:
                    md.append(f"- [{supp_desc.get(url, url)}]({self._md_url(url)})")
                md.append('')

        references = metadata.get('references') or []
        if references:
            md.extend(['---', '', '## References', ''])
            for index, ref in enumerate(references, 1):
                md.extend([f"[{index}] {ref}", ''])

        return '\n'.join(md).rstrip() + '\n'

    @staticmethod
    def _resolve_figures(body_md: str, kwargs: dict) -> str:
        """Swap ``__WILEY_FIG_n__`` for the local file, else the remote URL."""
        filenames = kwargs.get('figure_filenames') or {}
        figure_urls = kwargs.get('figure_urls') or {}

        def _sub(match: 're.Match') -> str:
            index = match.group(1)
            local = filenames.get(index) or filenames.get(int(index))
            if local:
                return str(local)
            info = figure_urls.get(f'fig_{index}') or {}
            return info.get('url', '') if isinstance(info, dict) else str(info)

        return re.sub(r'__WILEY_FIG_(\d+)__', _sub, body_md)

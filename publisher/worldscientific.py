"""World Scientific handler (worldscientific.com, DOI prefix 10.1142).

World Scientific runs on **Atypon** -- but on Atypon's *older* skin, not the
``div.core-container`` one that ACM, PNAS and UChicago serve. The platform is
the same, the markup is not:

    div.article__body                 the whole article
      div.hlFld-Abstract              abstract
      div.tagging-information         keywords
      div.hlFld-Fulltext              body: a FLAT list of h2/h3/h4 +
                                      <p> + floats, with no <section> nesting
    h2/h3/h4.article-section__title   headings (this is why the body is
                                      walked by heading, not by section)
    div.math-formula                  display formula; its number is a
                                      SIBLING span.formulaLabel ("(1)")
    span.inline-formula               inline MathML (+ an EMPTY formulaLabel)
    section.article-section-inline-figure   figure
    div.article-table-content         table, caption in <caption>
    table.listgroup                   a LIST, not a table (see below)
    span.NLM_fn                       one end note, flat under the "Notes" h2
    div.article__references           the reference list

So :class:`WorldScientificHandler` subclasses :class:`ACMHandler` for the
parts that are about Atypon rather than about a skin -- the inline-math
stash (MathML → LaTeX), the attribute hygiene pandoc needs, the Cloudflare
e-mail decoding, the Dublin Core metadata readers, the figure numbering
contract -- and plugs this skin's elements into the walk through
``_walk_extra``. ⚠️ It is deliberately **not** a second copy of the walker:
the alternative was duplicating 300 lines that already know how to keep
LaTeX intact through pandoc.

📌 Everything is in the raw server response: body, figures, tables, display
formulas, lists, end notes and references.

⚠️ **Must be headed.** The site is behind Cloudflare; the measured preload
sat on a "Just a moment…" page with **no clickable target** (iframes=0,
body=125) before being let through.

📌 **No supplemental example yet.** ``extract_supplemental_from_html``
therefore looks for the same ``/doi/suppl/`` links the rest of Atypon uses
and returns nothing when there are none -- it does not pretend to know a
shape it has never seen.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from publisher.acm import ACMHandler
from publisher.wildcard import html_table_to_markdown


class WorldScientificHandler(ACMHandler):
    """Full-text handler for World Scientific Publishing."""

    PUBLISHER = 'worldscientific'

    SITE_BASE = 'https://www.worldscientific.com'

    #: This skin wraps display formulas in ``div.math-formula``.
    DISPLAY_FORMULA_CLASS = 'math-formula'

    #: ``src`` is the medium PNG; the large JPEG is in ``data-lg-src``.
    HIRES_IMG_ATTRS = ('data-lg-src',)

    #: ⚠️ ``<p>`` is a block here. ACM's skin has none (its paragraphs are
    #: ``div[role=paragraph]``), so the base walk treats a ``<p>`` as inline
    #: and would concatenate **every paragraph of a section into one**.
    #:
    #: ⚠️ ``disp-formula-group`` is a **custom element** wrapping a numbered
    #: group of equations ((9a) + (9b)). Unknown tags take the inline path,
    #: and pandoc then printed the formula divs inside it as literal
    #: ``::: {#S0217751X25300133EMQ009}`` fences -- 10 of this article's 32
    #: display formulas sit in one of these.
    _BLOCK_TAGS = ACMHandler._BLOCK_TAGS | {'p', 'disp-formula-group'}

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.SITE_BASE

    # ------------------------------------------------------------------
    # Links
    # ------------------------------------------------------------------

    async def get_pdf_url(self, doi: str) -> Optional[str]:
        """``/doi/pdf/{doi}?download=true`` -- constructed, as the user asked.

        ⚠️ ``?download=true`` is the load-bearing part: without it Atypon
        answers with its in-page reader, the browser never fires a download
        event, and the whole retry budget goes to a viewer.
        """
        doi = (doi or self.doi or '').strip()
        if not doi:
            return None
        return f"{self.SITE_BASE}/doi/pdf/{doi}?download=true"

    async def get_supplemental_url(self, doi: str) -> Optional[str]:
        return None

    # ------------------------------------------------------------------
    # Body
    # ------------------------------------------------------------------

    #: Elements inside the body that are page furniture, not article text.
    _DROP_CLASSES = ('anchor-spacer', 'article__references', 'response',
                     'clt-dropzone', 'tagging-information', 'hlFld-Abstract')

    @classmethod
    def _body_container(cls, soup: BeautifulSoup):
        """``div.hlFld-Fulltext``, with the page furniture taken out.

        ⚠️ The base implementation looks for ``section[id^=sec-]`` and finds
        **none** here -- so without this override the body would be empty and
        the figures would never even be numbered.

        ⚠️ The furniture is **removed from the tree**, not skipped during the
        walk: ``div.anchor-spacer`` (an empty scroll target before every
        heading) is a plain div, so it takes the inline path and pandoc
        printed each one as a literal ``::: {#acS0217751X25300133SEC001}``
        fence -- one per section. Dropping them here also keeps them out of
        the inline runs that _walk builds up.
        """
        container = soup.find('div', class_='hlFld-Fulltext')
        if container is None:
            return None
        for name in cls._DROP_CLASSES:
            for el in container.find_all(class_=name):
                el.decompose()
        cls._drop_orcid_icon(container)
        return container

    @staticmethod
    def _drop_orcid_icon(container) -> None:
        """Remove the ORCID logo from the "ORCID" section.

        ⚠️ It cannot be told apart from a real inline graphic by its markup:
        the logo and the article's three inline figures are all
        ``span.NLM_inline-graphic`` wrapping an ``<img>`` with **empty alt and
        empty title** (measured -- ``figi1.png`` and ``oid.png`` are
        identical in every attribute). The only true difference is where it
        sits, so that is what this matches: between the ORCID heading and the
        next one.

        📌 The section itself stays. "Yao Ma → orcid.org/0000-…" is real
        provenance; the logo is decoration, and left in it put a stray
        ``![](…oid.png)`` pointing at the publisher into the markdown.
        """
        heading = container.find(id='orcid')
        if heading is None:
            return
        for sibling in list(heading.next_siblings):
            if getattr(sibling, 'name', None) in ('h1', 'h2', 'h3', 'h4'):
                break
            if getattr(sibling, 'find_all', None) is None:
                continue
            for icon in sibling.find_all('span', class_='NLM_inline-graphic'):
                icon.decompose()

    @staticmethod
    def _body_sections(soup: BeautifulSoup) -> List:
        return []

    @classmethod
    def _back_matter_nodes(cls, soup: BeautifulSoup) -> List:
        """None: the notes are **inside** the body container already.

        This skin has no separate back-matter container -- "Notes" is just
        another ``h2`` in ``div.hlFld-Fulltext``, followed by the
        ``span.NLM_fn`` notes as siblings.
        """
        return []

    @classmethod
    def _is_block_div(cls, el) -> bool:
        if super()._is_block_div(el):
            return True
        classes = el.get('class') or []
        if getattr(el, 'name', None) == 'div':
            return 'article-table-content' in classes
        # ⚠️ Each end note is a <span>. Left to the inline path all six of
        # them merge into a single paragraph.
        if getattr(el, 'name', None) == 'span':
            return 'NLM_fn' in classes
        return False

    @classmethod
    def _walk_extra(cls, child, level: int, figures: Dict[str, str]):
        name = child.name
        classes = child.get('class') or []

        if any(c in classes for c in cls._DROP_CLASSES):
            return []

        if name == 'disp-formula-group':
            return cls._walk(child, level, figures)

        if name == 'p':
            # A paragraph can hold a display formula or a list mid-sentence
            # ("…expansion of the following form: <EQ> where …"), so it is
            # walked, not flattened -- same reason ACM walks its
            # div[role=paragraph].
            return cls._walk(child, level, figures)

        if name == 'div' and 'article-table-content' in classes:
            return cls._render_ws_table(child)

        if name == 'table' and 'listgroup' in classes:
            return cls._render_listgroup(child)

        if name == 'span' and 'NLM_fn' in classes:
            md = cls._inline_md(child.decode_contents())
            return [cls._escape_block_start(md)] if md else []

        return None

    # -- this skin's floats --------------------------------------------

    @classmethod
    def _render_display_formula(cls, div) -> List[str]:
        """``div.math-formula`` → ``$$ … \\tag{1} $$``.

        📌 The number is **not** inside the formula and not in a
        ``div.label``: it is a sibling ``span.formulaLabel`` holding "(1)".
        ⚠️ The same class is used, EMPTY, next to every inline formula, so
        it is only read here -- where it belongs to a display formula -- and
        dropped everywhere else.
        """
        label_el = div.find('span', class_='formulaLabel')
        label = label_el.get_text('', strip=True) if label_el else ''
        if label_el is not None:
            label_el.extract()
        math_el = div.find('math')
        if math_el is None:
            return super()._render_display_formula(div)
        latex = cls._latex_from_mathml(math_el)
        if not latex:
            return []
        if label:
            latex += f"\\tag{{{cls._bare_label(label)}}}"
        return ['$$\n' + latex + '\n$$']

    @classmethod
    def _render_ws_table(cls, div) -> List[str]:
        """``div.article-table-content`` → bold caption + pipe table.

        The caption is a ``<caption>`` inside the table (``span.captionLabel``
        = "Table 1." plus the prose). It has to come out before the table is
        converted, or pandoc renders it as part of the grid.
        """
        table = div.find('table')
        if table is None:
            return []
        blocks: List[str] = []
        caption_el = table.find('caption')
        if caption_el is not None:
            caption = cls._inline_md(caption_el.decode_contents())
            caption_el.extract()
            if caption:
                blocks.append(f"**{caption}**")
        # ⚠️ Through this handler's own inline pipeline: the cells are full of
        # inline MathML, and get_text() would print nothing for it.
        md = html_table_to_markdown(table, cls._inline_md)
        if md:
            blocks.append(md)
        for foot in div.find_all(class_=('table-footnote', 'NLM_table-wrap-foot')):
            note = cls._inline_md(foot.decode_contents())
            if note:
                blocks.append(note)
        return blocks

    @classmethod
    def _render_listgroup(cls, table) -> List[str]:
        """``table.listgroup`` → a markdown list.

        ❌ **This is a list wearing a table's markup** -- the old NLM-to-HTML
        habit. One ``<tr class="liN">`` per item: the first ``<td>`` holds the
        marker the publisher printed ("(1)", "•"), the second
        (``td.paragraph``) holds the content. Rendered as a table it comes out
        as a two-column grid whose left column is bullets, which is how
        "(1) Is there a rigorous proof…" ends up looking like data.

        📌 The marker is **kept** when it carries information ("(1)", "(a)")
        -- the prose refers to it -- and dropped when it is a plain bullet,
        which would otherwise read as "- • …" (the same call PNAS's div-lists
        make). ``liN`` gives the nesting depth, so an indented sublist stays
        indented.
        """
        items: List[str] = []
        for row in table.find_all('tr'):
            cells = row.find_all('td', recursive=False)
            if not cells:
                continue
            marker, content = '', cells
            if len(cells) > 1:
                marker = cells[0].get_text(' ', strip=True)
                content = cells[1:]
            # ⚠️ Walked, not flattened: a list item can hold a display
            # formula (this article's hidden-region scalings are (9a)/(9b)
            # inside a bullet), and handing that cell to pandoc as inline
            # HTML printed a literal `::: {#…EMQ009}` fence.
            parts: List[str] = []
            for cell in content:
                parts.extend(b for b in cls._walk(cell, 6, {}) if b)
            if not parts:
                continue
            text, rest = parts[0], parts[1:]
            # ⚠️ A bare bullet carries nothing; the list syntax already says
            # "item". Anything else is the publisher's own numbering.
            if marker in ('•', '●', '·', '-', '—', '*'):
                marker = ''
            depth = 0
            for cls_name in (row.get('class') or []):
                match = re.fullmatch(r'li(\d+)', cls_name)
                if match:
                    depth = max(0, int(match.group(1)) - 1)
            indent = '  ' * depth
            body = f"{marker} {text}".strip() if marker else text
            lines = [f"{indent}- {body}"]
            # A block that followed the item's first paragraph stays with the
            # item, indented as a continuation.
            for block in rest:
                lines.append('')
                lines.append('\n'.join(indent + '  ' + line if line else ''
                                        for line in block.split('\n')))
            items.append('\n'.join(lines))
        return ['\n'.join(items)] if items else []

    # ------------------------------------------------------------------
    # Abstract / references / figures
    # ------------------------------------------------------------------

    @classmethod
    def _extract_abstract(cls, soup: BeautifulSoup) -> str:
        """``div.hlFld-Abstract``, through the body pipeline (it has math)."""
        section = soup.find('div', class_='hlFld-Abstract')
        if section is None:
            return ''
        fragment = BeautifulSoup(str(section), 'html.parser')
        for heading in fragment.find_all(['h2', 'h3']):
            heading.decompose()
        blocks = cls._walk(fragment, 2, {})
        return '\n\n'.join(b for b in blocks if b).strip()

    async def extract_references(self, html: str) -> list:
        """``li.references__item`` from ``div.article__references``.

        ⚠️ Each entry ends with a row of linkout buttons that are navigation,
        not citation. ⚠️ They do **not** all share one shape: Crossref/ADS/ISI
        go through ``/servlet/linkout``, while **Google Scholar is a direct
        scholar.google.com link** -- dropping only the servlet ones left a
        300-character search URL on all 87 references (measured). Both the
        host and the servlet path are checked.
        """
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        section = soup.find('div', class_='article__references')
        if section is None:
            return []
        references: List[str] = []
        for item in section.find_all('li', class_='references__item'):
            fragment = BeautifulSoup(str(item), 'html.parser')
            for anchor in fragment.find_all('a', href=True):
                href = anchor['href']
                if ('/servlet/linkout' in href
                        or 'scholar.google.com' in href
                        or 'webofknowledge.com' in href
                        or 'pubmed.ncbi.nlm.nih.gov' in href):
                    anchor.decompose()
            text = self._inline_md(fragment.decode_contents())
            # The buttons left their separating commas behind.
            text = re.sub(r'(?:\s*,)+(?=\s*$)', '', text)
            text = re.sub(r'[\s,]+$', '', text).strip()
            if text:
                references.append(text)
        return references

    @classmethod
    def extract_supplemental_from_html(cls, html_content: str) -> Tuple[List[str], Dict[str, str]]:
        """Atypon's ``/doi/suppl/`` links, if this article has any.

        📌 No World Scientific article with supplemental material has been
        seen yet, so this looks for the platform's usual link shape and
        reports nothing when there is none. ⚠️ "None found" is an answer
        here, not a silent failure -- the caller prints the count.
        """
        if not html_content:
            return [], {}
        soup = BeautifulSoup(html_content, 'html.parser')
        urls: List[str] = []
        descriptions: Dict[str, str] = {}
        for anchor in soup.find_all('a', href=True):
            if '/doi/suppl/' not in anchor['href']:
                continue
            url = cls._absolute(anchor['href'])
            if url in urls:
                continue
            urls.append(url)
            label = anchor.get_text(' ', strip=True)
            if label:
                descriptions[url] = label
        return urls, descriptions

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @classmethod
    def _citation_line(cls, soup: BeautifulSoup) -> tuple:
        """``('40', '29', '2530013', '2025')`` from the breadcrumb.

        📌 Volume, issue and the article number are in no meta tag at all --
        the page prints them once, in ``a.article__tocHeading``:
        "Vol. 40, No. 29, 2530013 (2025)". ``metadata.json`` takes these
        three fields from the handler, so without this they are empty.
        """
        for anchor in soup.find_all('a', class_='article__tocHeading'):
            match = re.search(
                r'Vol\.\s*([^,]+?),\s*No\.\s*([^,]+?),\s*(\S+?)\s*\((\d{4})\)',
                anchor.get_text(' ', strip=True))
            if match:
                return (match.group(1).strip(), match.group(2).strip(),
                        match.group(3).strip(), match.group(4))
        return '', '', '', ''

    async def extract_metadata(self, page) -> dict:
        html = await self.get_page_html(page)
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')

        volume, issue, pages, cite_year = self._citation_line(soup)
        date = self._dc_one(soup, 'dc.Date')
        year_match = re.search(r'\b(19|20|21)\d{2}\b', date)

        keywords = self._dc_one(soup, 'dc.Subject')
        if not keywords:
            meta = soup.find('meta', attrs={'name': 'keywords'})
            keywords = (meta.get('content') or '') if meta else ''

        authors = [self._clean_author(a) for a in self._dc(soup, 'dc.Creator')]

        return {
            'title': self._dc_one(soup, 'dc.Title') or self._extract_title(soup),
            'doi': self._dc_doi(soup) or (self.doi or ''),
            'authors': [a for a in authors if a],
            'year': (year_match.group(0) if year_match else '') or cite_year,
            'journal': (self._dc_one(soup, 'citation_journal_title')
                        or self._extract_journal(soup)),
            'volume': volume,
            'issue': issue,
            'pages': pages,
            'keywords': [k.strip() for k in re.split(r'[;,]', keywords) if k.strip()],
            'abstract': self._extract_abstract(soup),
            'publisher': self._dc_one(soup, 'dc.Publisher'),
        }

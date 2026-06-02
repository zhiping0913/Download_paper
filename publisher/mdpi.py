"""
MDPI publisher handler.

Extracts metadata, body, figures, equations, and references from MDPI
journal article pages (www.mdpi.com). MDPI renders math as MathJax CHTML
with an HTML-escaped MathML twin in the ``data-mathml`` attribute on
``<span class="MathJax">``, so we walk the MathML and run it through
pandoc to get LaTeX.

Headless-friendly: the article HTML is fully rendered server-side and
nothing important is JS-gated, so the page can be visited via the
Phase 0 headless browser without a login.
"""

import re
from html import unescape
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString

from html_to_md_converter import (
    cleanup_markdown,
    convert_html_to_markdown,
    mathml_to_latex_pandoc,
    remove_newlines_in_paragraph,
)
from publisher.base import PublisherHandler
from publisher.wildcard import (
    format_as_bibtex,
    generate_reference_text_from_crossref,
    init_extract_all_page,
    set_actual_base_url,
)


class MDPIHandler(PublisherHandler):
    """Handler for MDPI journal articles (www.mdpi.com)."""

    MDPI_BASE = 'https://www.mdpi.com'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.MDPI_BASE

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_metadata_from_html_meta(html_content: str) -> dict:
        """Pull citation_* meta tags plus MDPI's fulltext_pdf into a dict."""
        if not html_content:
            return {}
        soup = BeautifulSoup(html_content, 'html.parser')
        meta = {}
        authors = []
        for tag in soup.find_all('meta'):
            name = tag.get('name', '')
            content = (tag.get('content') or '').strip()
            if not name or not content:
                continue
            if name == 'citation_author':
                authors.append(content)
            elif name == 'citation_title':
                meta['title'] = re.sub(r'\s+', ' ', content)
            elif name == 'citation_doi':
                meta['doi'] = content
            elif name == 'citation_journal_title':
                meta['journal'] = content
            elif name == 'citation_volume':
                meta['volume'] = content
            elif name in ('citation_issue', 'citation_number'):
                meta['issue'] = content
            elif name == 'citation_firstpage':
                meta['firstpage'] = content
            elif name == 'citation_lastpage':
                meta['lastpage'] = content
            elif name == 'citation_publication_date':
                meta['publication_date'] = content
                m = re.search(r'(\d{4})', content)
                if m:
                    meta['year'] = m.group(1)
            elif name == 'citation_online_date':
                meta.setdefault('publication_date', content)
            elif name in ('fulltext_pdf', 'citation_pdf_url'):
                meta.setdefault('pdf_url', content)
            elif name == 'citation_abstract':
                meta.setdefault('abstract', content)
            elif name == 'citation_keywords':
                meta['keywords'] = [k.strip() for k in content.split(';') if k.strip()]

        if authors:
            seen, unique = set(), []
            for a in authors:
                if a not in seen:
                    seen.add(a)
                    unique.append(a)
            meta['authors'] = unique

        firstpage = meta.pop('firstpage', '')
        lastpage = meta.pop('lastpage', '')
        if firstpage and lastpage:
            meta['pages'] = f"{firstpage}-{lastpage}"
        elif firstpage:
            meta['pages'] = firstpage

        return meta

    @staticmethod
    def _extract_affiliations(html_content: str) -> List[str]:
        """Return the ordered list of ``<div class="affiliation">`` strings."""
        if not html_content:
            return []
        soup = BeautifulSoup(html_content, 'html.parser')
        result = []
        for div in soup.find_all('div', class_='affiliation'):
            text = re.sub(r'\s+', ' ', div.get_text(' ', strip=True)).strip()
            if text:
                result.append(text)
        return result

    # ------------------------------------------------------------------
    # Math / paragraph conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _mathml_to_latex(mathml: str) -> str:
        """MathML (already entity-decoded by BeautifulSoup) → LaTeX body."""
        if not mathml:
            return ''
        latex = mathml_to_latex_pandoc(mathml)
        if not latex:
            return ''
        latex = latex.strip()
        # pandoc wraps display math in $$...$$ and inline in $...$ — strip both
        # so the caller can re-wrap consistently.
        if latex.startswith('$$') and latex.endswith('$$'):
            latex = latex[2:-2].strip()
        if latex.startswith('$') and latex.endswith('$') and not latex.startswith('$$'):
            latex = latex[1:-1].strip()
        return latex

    @staticmethod
    def _is_display_mathml(mathml: str) -> bool:
        """True if the MathML root has ``display="block"`` (a display equation)."""
        if not mathml:
            return False
        head = mathml.lstrip()[:200]
        return bool(re.search(r'<math[^>]*display\s*=\s*["\']block["\']', head))

    @classmethod
    def _prepare_mdpi_fragment(cls, html_fragment: str) -> Tuple[str, List[Tuple[str, str]]]:
        """Replace MathJax markup with placeholders for the HTML→MD pipeline.

        Returns ``(prepared_html, formulas)`` where ``formulas[i]`` is the tuple
        ``('inline'|'display', latex)``. The caller substitutes the
        placeholders ``MDPIMATHnnnMATHEND`` back with the appropriate wrapping
        (``$…$`` for inline, ``\\n$$…$$\\n`` for display).

        MDPI emits each math expression as a hidden ``<span class="MathJax_Preview">``
        anchor (always empty) followed by ``<span class="MathJax" data-mathml="…">``.
        Display equations additionally sit inside ``<div class="f">`` →
        ``<div class="MathJax_Display">`` — and these get nested inside an
        ``<div class="html-p">`` block in the body, so the paragraph converter
        has to be the place that splits them out.
        """
        soup = BeautifulSoup(html_fragment, 'html.parser')
        formulas: List[Tuple[str, str]] = []

        def stash(kind: str, latex: str) -> str:
            formulas.append((kind, latex))
            return f"MDPIMATH{len(formulas) - 1:03d}MATHEND"

        for tag in soup(['script', 'style', 'noscript']):
            tag.decompose()

        # Empty placeholder span MDPI emits before each math expression.
        for span in soup.find_all('span', class_='MathJax_Preview'):
            span.decompose()

        # MDPI wraps each display equation in
        #   <div class="html-disp-formula-info" id="FD1-…">
        #     <div class="f">…</div>
        #   </div>
        # Pandoc would otherwise emit ``::: {#FD1-…}`` fenced-div markers
        # around the equation. Unwrap so the equation block survives clean.
        for wrap in soup.find_all('div', class_='html-disp-formula-info'):
            wrap.unwrap()

        # Iterate MathJax spans in document order.  Each carries the real
        # MathML in data-mathml; the display flag is on the <math> root.
        for span in soup.find_all('span', class_='MathJax'):
            mathml = span.get('data-mathml', '')
            latex = cls._mathml_to_latex(mathml)
            if not latex:
                span.decompose()
                continue
            kind = 'display' if cls._is_display_mathml(mathml) else 'inline'
            placeholder = stash(kind, latex)
            if kind == 'display':
                # Bubble the whole display-equation wrapper out.  We climb to
                # the surrounding <div class="f"> when present so the resulting
                # block doesn't sit inside the paragraph's <p>.
                wrapper = span.find_parent('div', class_='f') or span
                wrapper.replace_with(NavigableString(f"\n\n{placeholder}\n\n"))
            else:
                span.replace_with(NavigableString(f" {placeholder} "))

        # Drop any leftover empty MathJax_Display wrappers (the span we just
        # replaced was nested inside one and the parent will otherwise survive).
        for empty in soup.find_all('div', class_='MathJax_Display'):
            if not empty.get_text(strip=True):
                empty.decompose()

        # Bibliographic xrefs like <a class="html-bibr" href="#B12-...">12</a>
        # become the bare reference number — MDPI's source HTML already wraps
        # them in physical "[" and "]" characters, so adding our own brackets
        # would double them up ([[12]]).
        for a in soup.select('a.html-bibr'):
            text = a.get_text(' ', strip=True)
            if text:
                a.replace_with(NavigableString(text))
            else:
                a.decompose()

        # Figure / equation cross-refs — keep as plain text labels.
        for a in soup.select('a.html-fig, a.html-disp-formula'):
            a.replace_with(NavigableString(a.get_text(' ', strip=True)))

        return str(soup), formulas

    @classmethod
    def _convert_mdpi_fragment_to_md(cls, html_fragment: str) -> str:
        """Convert an HTML fragment (paragraph, list item, caption) to Markdown."""
        if not html_fragment:
            return ''
        prepared, formulas = cls._prepare_mdpi_fragment(html_fragment)
        md = convert_html_to_markdown(prepared)
        md = cleanup_markdown(md)
        # Pandoc may emit ``::: {#id}`` / ``:::`` fenced-div wrappers around
        # ids that survived unwrapping. Strip the markers — we don't use
        # native_div semantics anywhere downstream.
        md = re.sub(r'^:::\s*\{#[^}]+\}\s*$', '', md, flags=re.MULTILINE)
        md = re.sub(r'^:::\s*$', '', md, flags=re.MULTILINE)
        # Inline math is already a single span token; collapse whitespace
        # within each line but keep paragraph / display-equation line breaks.
        md = re.sub(r'[ \t]+', ' ', md)
        md = re.sub(r' *\n *', '\n', md)
        md = re.sub(r'\n{3,}', '\n\n', md)
        # Substitute placeholders AFTER the collapse pass so the surrounding
        # blank lines we add for display equations are preserved.
        for idx, (kind, latex) in enumerate(formulas):
            token = f"MDPIMATH{idx:03d}MATHEND"
            if kind == 'display':
                md = md.replace(token, f"\n\n$$\n{latex}\n$$\n\n")
            else:
                md = md.replace(token, f"${latex}$")
        md = re.sub(r'\n{3,}', '\n\n', md).strip()
        return md

    @classmethod
    def _display_equation_to_md(cls, eq_div) -> str:
        """Convert one ``<div class="f">`` display equation to ``$$...$$``."""
        if eq_div is None:
            return ''
        # Strip the hidden preview placeholder if it's there.
        for span in eq_div.find_all('span', class_='MathJax_Preview'):
            span.decompose()
        mj = eq_div.find('span', class_='MathJax')
        if mj is None:
            return ''
        latex = cls._mathml_to_latex(mj.get('data-mathml', ''))
        if not latex:
            return ''
        return f"\n$$\n{latex}\n$$\n"

    # ------------------------------------------------------------------
    # Figures
    # ------------------------------------------------------------------

    @classmethod
    def _figure_caption_md(cls, fig_wrap) -> str:
        """Extract the description block as a leading-bold caption."""
        desc = fig_wrap.find('div', class_='html-fig_description')
        if desc is None:
            return ''
        md = cls._convert_mdpi_fragment_to_md(str(desc))
        return md

    @classmethod
    def extract_figures_from_html(cls, html_content: str, base_url: str = None) -> Dict[str, dict]:
        """Return ``{'fig_N': {'url': ..., 'caption': ...}}`` for every
        ``<div class="html-fig-wrap">``.

        Prefers ``data-large`` (full resolution) over ``src`` (thumbnail).
        """
        if not html_content:
            return {}
        soup = BeautifulSoup(html_content, 'html.parser')
        figures = {}
        base = base_url or cls.MDPI_BASE

        for idx, fig in enumerate(soup.find_all('div', class_='html-fig-wrap'), 1):
            img = fig.find('img')
            url = ''
            if img is not None:
                # MDPI's order of preference: data-large > data-original > src
                url = (img.get('data-large')
                       or img.get('data-original')
                       or img.get('src')
                       or '')
                url = url.strip()
            if not url:
                continue
            if url.startswith('//'):
                url = 'https:' + url
            elif not url.startswith('http'):
                url = urljoin(base + '/', url.lstrip('/'))

            # Pull a figure number from the wrapper id if present
            # ("photonics-04-00026-f001" → "1"), otherwise use the loop index.
            num_match = re.search(r'f(\d+)\s*$', fig.get('id', ''))
            fig_num = num_match.group(1).lstrip('0') or str(idx) if num_match else str(idx)

            figures[f'fig_{fig_num}'] = {
                'url': url,
                'caption': cls._figure_caption_md(fig),
            }

        return figures

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    @classmethod
    def _build_table_index(cls, soup) -> Dict[str, 'BeautifulSoup']:
        """Map each in-body table wrapper id to the matching ``<table>`` element.

        MDPI splits a table across two places: a placeholder wrapper inside
        ``html-body`` (id like ``particles-03-00018-t001``) that only carries
        the caption and a thumbnail icon, and a separate display block
        (id ``table_body_display_particles-03-00018-t001``) that holds the
        real ``<table>``. We pair them up by id so the walker can fetch the
        matching ``<table>`` when it hits the wrapper.
        """
        index: Dict[str, BeautifulSoup] = {}
        for show in soup.find_all('div', class_='html-table_show'):
            sid = show.get('id', '')
            if not sid.startswith('table_body_display_'):
                continue
            wrapper_id = sid[len('table_body_display_'):]
            tbl = show.find('table')
            if tbl is not None:
                index[wrapper_id] = tbl
        return index

    @classmethod
    def _convert_table_cell(cls, cell) -> str:
        """Convert one <th>/<td> to a Markdown-table-safe single-line string."""
        md = cls._convert_mdpi_fragment_to_md(str(cell))
        # Pipe-table cells cannot contain raw newlines or unescaped pipes.
        md = md.replace('\n', ' ').replace('|', '\\|')
        return re.sub(r'\s+', ' ', md).strip()

    @classmethod
    def _table_element_to_md(cls, table) -> str:
        """Convert a <table> to a Markdown pipe-table."""
        if table is None:
            return ''
        rows = []
        header_cells: List[str] = []

        thead = table.find('thead')
        if thead is not None:
            for tr in thead.find_all('tr'):
                cells = [cls._convert_table_cell(c) for c in tr.find_all(['th', 'td'])]
                if cells:
                    header_cells = cells
                    break  # only the first header row drives the column count

        # Fall back to the first body row if there's no <thead>.
        tbody = table.find('tbody') or table
        body_rows = []
        for tr in tbody.find_all('tr'):
            if thead and tr.find_parent('thead') is not None:
                continue
            cells = [cls._convert_table_cell(c) for c in tr.find_all(['th', 'td'])]
            if cells and not all(c == '' for c in cells):
                body_rows.append(cells)

        if not header_cells and body_rows:
            header_cells = body_rows.pop(0)
        if not header_cells:
            return ''

        col_count = len(header_cells)
        rows.append('| ' + ' | '.join(header_cells) + ' |')
        rows.append('|' + '|'.join(['---'] * col_count) + '|')
        for cells in body_rows:
            if len(cells) < col_count:
                cells = cells + [''] * (col_count - len(cells))
            elif len(cells) > col_count:
                cells = cells[:col_count]
            rows.append('| ' + ' | '.join(cells) + ' |')

        return '\n'.join(rows)

    @classmethod
    def _table_wrap_caption_md(cls, wrap) -> str:
        """Render the caption block (``html-table_wrap_discription``) to MD."""
        desc = wrap.find('div', class_='html-table_wrap_discription')
        if desc is None:
            return ''
        return cls._convert_mdpi_fragment_to_md(str(desc))

    # ------------------------------------------------------------------
    # Body extraction
    # ------------------------------------------------------------------

    # Classes we treat as plain wrappers in the body walker — descend into
    # them, don't emit anything of their own.
    _WRAPPER_CLASSES = ('html-section', 'html-body', 'html-section-section')

    @classmethod
    def _extract_abstract(cls, soup) -> str:
        """Return Markdown rendering of the article abstract section."""
        section = soup.find('section', class_='html-abstract') \
                  or soup.find('div', class_='art-abstract') \
                  or soup.find('section', id='html-abstract')
        if section is None:
            # Fallback to meta tag content (set elsewhere)
            return ''
        paragraphs = []
        for p in section.find_all('div', class_='html-p'):
            md = cls._convert_mdpi_fragment_to_md(str(p))
            if md:
                paragraphs.append(md)
        if paragraphs:
            return '\n\n'.join(paragraphs)
        # Some MDPI templates wrap the abstract in <p> instead of <div class="html-p">.
        ps = section.find_all('p')
        for p in ps:
            text = re.sub(r'\s+', ' ', p.get_text(' ', strip=True)).strip()
            if text and text.lower() != 'abstract':
                paragraphs.append(text)
        return '\n\n'.join(paragraphs)

    @classmethod
    def _list_to_md(cls, list_el) -> str:
        """Convert a ``<ul>``/``<ol>`` (containing ``<li>`` with html-p inside)
        to Markdown."""
        if list_el is None:
            return ''
        ordered = (list_el.name == 'ol')
        lines = []
        for i, li in enumerate(list_el.find_all('li', recursive=False), 1):
            chunks = []
            for child in li.children:
                if not getattr(child, 'name', None):
                    text = str(child).strip()
                    if text:
                        chunks.append(text)
                    continue
                if child.name == 'div' and 'html-p' in (child.get('class') or []):
                    md = cls._convert_mdpi_fragment_to_md(str(child))
                    if md:
                        chunks.append(md)
                elif child.name in ('ul', 'ol'):
                    sub = cls._list_to_md(child)
                    if sub:
                        chunks.append('\n' + sub)
                else:
                    md = cls._convert_mdpi_fragment_to_md(str(child))
                    if md:
                        chunks.append(md)
            content = ' '.join(c for c in chunks if c).strip()
            if not content:
                continue
            marker = f"{i}." if ordered else '-'
            lines.append(f"{marker} {content}")
        return '\n'.join(lines)

    @classmethod
    def _walk_body(cls, container, body_parts: list,
                   table_index: Optional[Dict[str, 'BeautifulSoup']] = None):
        """Walk an MDPI ``<div class="html-body">`` (or any descendant section)
        recursively, emitting Markdown chunks for content elements and
        descending into structural wrappers in document order.

        ``table_index`` maps in-body table-wrap ids to the matching ``<table>``
        element from the document-level ``html-table_show`` display blocks.
        """
        if table_index is None:
            table_index = {}
        for child in container.children:
            if not getattr(child, 'name', None):
                continue
            tag = child.name
            classes = child.get('class') or []

            if tag in ('h2', 'h3', 'h4', 'h5', 'h6'):
                text = re.sub(r'\s+', ' ',
                              child.get_text(' ', strip=True) or '').strip()
                if not text:
                    continue
                level = '#' * (int(tag[1]) + 1)
                body_parts.extend([f"{level} {text}", ''])
                continue

            if tag == 'div':
                if 'html-p' in classes:
                    md = cls._convert_mdpi_fragment_to_md(str(child))
                    if md:
                        body_parts.extend([md, ''])
                    continue
                if 'f' in classes:
                    eq_md = cls._display_equation_to_md(child)
                    if eq_md:
                        body_parts.extend([eq_md, ''])
                    continue
                if 'html-fig-wrap' in classes:
                    caption = cls._figure_caption_md(child)
                    if caption:
                        body_parts.extend([caption, ''])
                    continue
                if 'html-table-wrap' in classes:
                    caption = cls._table_wrap_caption_md(child)
                    if caption:
                        body_parts.extend([caption, ''])
                    wrap_id = child.get('id', '')
                    tbl = table_index.get(wrap_id) if wrap_id else None
                    if tbl is not None:
                        tbl_md = cls._table_element_to_md(tbl)
                        if tbl_md:
                            body_parts.extend([tbl_md, ''])
                    continue
                # Generic wrapper — descend.
                cls._walk_body(child, body_parts, table_index)
                continue

            if tag == 'section':
                # MDPI nests sections within sections — recurse.
                cls._walk_body(child, body_parts, table_index)
                continue

            if tag in ('ul', 'ol'):
                list_md = cls._list_to_md(child)
                if list_md:
                    body_parts.extend([list_md, ''])
                continue

            if tag == 'table':
                # Standalone <table> not inside an html-table-wrap.
                tbl_md = cls._table_element_to_md(child)
                if tbl_md:
                    body_parts.extend([tbl_md, ''])
                continue

            # Skip <a>, <span>, scripts, etc. at top level.

    @classmethod
    def extract_article_text_from_html(cls, html_content: str) -> Tuple[str, str]:
        """Return ``(abstract_md, body_md)``."""
        if not html_content:
            return '', ''
        soup = BeautifulSoup(html_content, 'html.parser')

        abstract_md = cls._extract_abstract(soup)

        body = soup.find('div', class_='html-body')
        if body is None:
            return abstract_md, ''

        # MDPI splits each table into a wrapper inside html-body (caption only)
        # and a separate html-table_show display block holding the real
        # <table>. Build the wrapper→table map once for the walker.
        table_index = cls._build_table_index(soup)

        body_parts: List[str] = []
        cls._walk_body(body, body_parts, table_index)

        body_md = '\n'.join(body_parts).strip()
        body_md = re.sub(r'\n{3,}', '\n\n', body_md)
        return abstract_md, body_md

    # ------------------------------------------------------------------
    # References
    # ------------------------------------------------------------------

    @classmethod
    def extract_references_from_html(cls, html_content: str) -> Tuple[List[str], List[str]]:
        """Return ``(text_refs, dois)`` from MDPI's reference list.

        References live in ``<ol class="html-xxx">`` (the one after the
        ``References`` heading) and each entry is a ``<li id="B{N}-…">`` with
        a class that varies by reference-number range (``html-x`` for 1-9,
        ``html-xx`` for 10-99, ``html-xxx`` for ≥100). Matching by the
        ``B{N}-`` id pattern is robust to that scheme.

        The visible text strips MDPI's ``[Google Scholar]`` and ``[CrossRef]``
        decoration anchors. The DOI is read from the ``a.cross-ref`` href.
        """
        if not html_content:
            return [], []
        soup = BeautifulSoup(html_content, 'html.parser')
        text_refs: List[str] = []
        dois: List[str] = []
        ref_li_iter = soup.find_all('li', id=re.compile(r'^B\d+-'))
        for li in ref_li_iter:
            # Capture DOI before we strip decoration links.
            doi = ''
            cross_a = li.find('a', class_='cross-ref')
            if cross_a:
                href = (cross_a.get('href') or '').strip()
                m = re.search(r'10\.\d{4,9}/[^\s"\'<>]+', href)
                if m:
                    doi = m.group(0).rstrip('.')

            # Clone and strip the trailing "[Google Scholar] [CrossRef]" anchors
            # plus the bracket text around them.
            clone = BeautifulSoup(str(li), 'html.parser')
            for a in clone.find_all('a', class_=re.compile(r'google-scholar|cross-ref|pubmed')):
                a.decompose()

            text = clone.get_text(' ', strip=True)
            # Remove leftover bracket pairs left after the anchors are gone.
            text = re.sub(r'\[\s*\]', '', text)
            text = re.sub(r'\s+', ' ', text).strip()
            text = text.rstrip(' .,;')

            text_refs.append(text)
            dois.append(doi)
        return text_refs, dois

    # ------------------------------------------------------------------
    # PublisherHandler contract
    # ------------------------------------------------------------------

    async def extract_metadata(self, page) -> dict:
        html_content = ''
        if page is not None:
            try:
                html_content = await page.content()
            except Exception:
                html_content = ''

        meta = self._extract_metadata_from_html_meta(html_content)
        affiliations = self._extract_affiliations(html_content)
        author_with_affiliations = []
        if meta.get('authors') and affiliations:
            # MDPI lists affiliations in their own block, not per-author. Attach
            # all of them to the first author so the markdown surfaces them.
            author_with_affiliations.append({
                'author': meta['authors'][0],
                'affiliations': affiliations,
            })
            for a in meta['authors'][1:]:
                author_with_affiliations.append({'author': a, 'affiliations': []})

        abstract = meta.get('abstract', '')
        if not abstract and html_content:
            abstract, _ = self.extract_article_text_from_html(html_content)

        return {
            'title': meta.get('title') or 'MDPI Article',
            'authors': meta.get('authors', []),
            'author_with_affiliations': author_with_affiliations,
            'corresponding_author_emails': [],
            'abstract': abstract,
            'journal': meta.get('journal') or 'MDPI',
            'publication_date': meta.get('publication_date'),
            'doi': meta.get('doi') or self.doi,
            'volume': meta.get('volume'),
            'issue': meta.get('issue'),
            'pages': meta.get('pages'),
            'year': meta.get('year'),
            'references': [],
            '_pdf_url': meta.get('pdf_url'),
            '_keywords': meta.get('keywords', []),
        }

    async def get_fulltext_url(self, page) -> str:
        if page is not None:
            try:
                return page.url
            except Exception:
                pass
        return f"https://doi.org/{self.doi}" if self.doi else None

    async def get_pdf_url(self, doi: str) -> Optional[str]:
        return None

    async def get_supplemental_url(self, doi: str) -> Optional[str]:
        return None

    async def extract_references(self, html: str) -> list:
        if not html:
            return []
        refs, _ = self.extract_references_from_html(html)
        return refs

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        """Run the MDPI handler through the unified publisher contract."""
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'MDPIHandler'
        )
        doi = self.doi
        set_actual_base_url(self, page)

        try:
            metadata = await self.extract_metadata(page)
            metadata['doi'] = doi

            pdf_url = metadata.pop('_pdf_url', None)
            metadata.pop('_keywords', None)

            try:
                fulltext_html = await page.content()
            except Exception:
                fulltext_html = ''

            if fulltext_html:
                text_refs, ref_dois = self.extract_references_from_html(fulltext_html)
                metadata['references'] = text_refs
                metadata['_ref_dois'] = ref_dois

            figure_urls = {}
            if fulltext_html:
                figure_urls = self.extract_figures_from_html(
                    fulltext_html, base_url=self.actual_base_url
                )

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figure_urls,
                    'supplemental_urls': [],
                    'supplemental_descriptions': {},
                },
                'fulltext_data': fulltext_html,
                'journal_name': 'mdpi',
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

    # ------------------------------------------------------------------
    # Markdown rendering
    # ------------------------------------------------------------------

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        title = metadata.get('title') or 'MDPI Article'
        md_parts = [f"# {title}", '']

        author_with_affiliations = metadata.get('author_with_affiliations', [])
        if author_with_affiliations:
            md_parts.extend(['## Authors', ''])
            for entry in author_with_affiliations:
                md_parts.append(f"- **{entry.get('author', '')}**")
                for aff in entry.get('affiliations', []):
                    md_parts.append(f"  {aff}")
                md_parts.append('')
        elif metadata.get('authors'):
            md_parts.extend(['## Authors', ''])
            for a in metadata['authors']:
                md_parts.append(f"- {a}")
            md_parts.append('')

        md_parts.extend(['## Publication', ''])
        if metadata.get('journal'):
            md_parts.extend([f"**Journal:** {metadata['journal']}", ''])
        if metadata.get('year'):
            md_parts.extend([f"**Year:** {metadata['year']}", ''])
        if metadata.get('volume'):
            line = f"**Volume:** {metadata['volume']}"
            if metadata.get('issue'):
                line += f", Issue {metadata['issue']}"
            md_parts.extend([line, ''])
        if metadata.get('pages'):
            md_parts.extend([f"**Pages:** {metadata['pages']}", ''])
        if metadata.get('doi'):
            md_parts.extend([f"**DOI:** {metadata['doi']}", ''])
        md_parts.extend(['---', ''])

        if metadata.get('abstract'):
            md_parts.extend(['## Abstract', '', metadata['abstract'], '', '---', ''])

        body_md = ''
        if isinstance(article_text, str) and article_text.strip():
            if article_text.lstrip().startswith('<'):
                _, body_md = self.extract_article_text_from_html(article_text)
            else:
                body_md = article_text.strip()

        figure_filenames = kwargs.get('figure_filenames') or {}
        figure_urls = kwargs.get('figure_urls') or {}
        if kwargs.get('add_figure_refs') and figure_filenames and body_md:
            # Match by the leading "**Figure N.**" label that comes from
            # html-fig_description ("Figure 1." …).  Iterating figure_urls
            # keeps us in lockstep with the captions actually emitted into
            # the body.
            for fig_id, fig_info in figure_urls.items():
                num_match = re.search(r'(\d+)$', fig_id)
                if not num_match:
                    continue
                filename = figure_filenames.get(num_match.group(1))
                if not filename:
                    continue
                caption = (fig_info.get('caption') or '') if isinstance(fig_info, dict) else ''
                # The MDPI description starts with "**Figure N.**" but pandoc
                # may have wrapped it in **Figure 1.** without trailing dot
                # in label — match the bold-prefix robustly.
                label_match = re.match(r'\*\*([^*]+?)\*\*', caption)
                if label_match:
                    label = label_match.group(1).strip()
                    body_md = re.sub(
                        rf'(\*\*{re.escape(label)}\*\*[^\n]*)',
                        rf'\1\n\n![{label.rstrip(".")}]({filename})',
                        body_md,
                        count=1,
                    )

        if body_md:
            md_parts.extend(['## Article Text', '', body_md, ''])
        else:
            md_parts.extend(['## Article Text', '', '[Article text not found.]', ''])

        # References with BibTeX block per entry that has a DOI.
        text_refs = metadata.get('references', []) or []
        ref_dois = metadata.get('_ref_dois', []) or []
        crossref_refs = metadata.get('_crossref_references', []) or []
        crossref_by_doi = {}
        for cr in crossref_refs:
            d = (cr.get('DOI') or '').strip().lower()
            if d:
                crossref_by_doi[d] = cr

        if text_refs:
            md_parts.extend(['---', '', '## References', ''])
            for idx, text in enumerate(text_refs, 1):
                md_parts.extend([f"[{idx}] {text}", ''])
                doi = (ref_dois[idx - 1] if idx - 1 < len(ref_dois) else '').strip()
                cr = crossref_by_doi.get(doi.lower()) if doi else None
                parts = {}
                if cr:
                    parts['year'] = str(cr.get('year', '')).strip()
                    parts['doi'] = (cr.get('DOI') or doi).strip()
                elif doi:
                    year_match = re.search(r'(\b(?:18|19|20)\d{2}\b)', text)
                    if year_match:
                        parts['year'] = year_match.group(1)
                    parts['doi'] = doi
                parts = {k: v for k, v in parts.items() if v}
                if parts:
                    key = f"ref{idx}"
                    bibtex = format_as_bibtex(parts, key=key)
                    md_parts.extend(['```bibtex', bibtex, '```', ''])
        elif crossref_refs:
            md_parts.extend(['---', '', '## References', ''])
            for idx, cr in enumerate(crossref_refs, 1):
                text = cr.get('unstructured') or generate_reference_text_from_crossref(cr, index=idx)
                if not text.startswith('['):
                    text = f"[{idx}] {text}"
                md_parts.extend([text, ''])
                doi = (cr.get('DOI') or '').strip()
                parts = {'year': str(cr.get('year', '')).strip(), 'doi': doi}
                parts = {k: v for k, v in parts.items() if v}
                if parts:
                    bibtex = format_as_bibtex(parts, key=cr.get('key', f"ref{idx}"))
                    md_parts.extend(['```bibtex', bibtex, '```', ''])

        return '\n'.join(md_parts)


__all__ = ['MDPIHandler']

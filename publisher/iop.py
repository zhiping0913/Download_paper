"""
IOP Publishing handler.

Extracts metadata, body, figures, tables, references, and supplemental materials
from IOP Science article pages (iopscience.iop.org).

IOP stores LaTeX equations directly in <script type="math/tex"> tags (no MathML),
so a dedicated preprocessing pass extracts them before the HTML→Markdown pipeline.
"""

import re
import urllib.request
from urllib.parse import urljoin
import json
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from core.utilities import fetch_html_via_ladder
from publisher.base import PublisherHandler
from publisher.wildcard import (
    convert_html_fragment_to_markdown,
    convert_mathml,
    extract_abstract_with_fallbacks,
    find_generic_article_body,
    format_citation_as_text,
    init_extract_all_page,
    prepare_mathjax_html_fragment,
    render_heading_md,
    set_actual_base_url,
    generate_reference_text_from_crossref,
)


class IOPHandler(PublisherHandler):
    """Handler for IOP Publishing articles (iopscience.iop.org)."""

    # Both extract_metadata and extract_all read get_page_html() (the raw
    # server response), so nothing here looks at a MathJax-rendered DOM --
    # which is what puts IOP in core.utilities.RAW_HTML_PUBLISHERS -- the
    # set that now only decides who gets the view-source rescue.
    PUBLISHER = 'iop'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)

    # ------------------------------------------------------------------
    # Metadata extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_metadata_from_html_meta(html_content: str) -> dict:
        """Extract IOP metadata from citation_* <meta> tags."""
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        meta = {}
        authors = []

        for tag in soup.find_all('meta'):
            name = tag.get('name', '')
            content = tag.get('content', '')
            if not name or not content:
                continue

            if name == 'citation_author':
                authors.append(content.strip())
            elif name == 'citation_title':
                meta['title'] = content.strip()
            elif name == 'citation_doi':
                meta['doi'] = content.strip()
            elif name == 'citation_journal_title':
                meta['journal'] = content.strip()
            elif name == 'citation_volume':
                meta['volume'] = content.strip()
            elif name in ('citation_issue', 'citation_number'):
                meta['issue'] = content.strip()
            elif name == 'citation_firstpage':
                meta['pages'] = content.strip()
            elif name == 'citation_publication_date':
                date_str = content.strip()
                meta['publication_date'] = date_str
                if date_str and '/' in date_str:
                    meta['year'] = date_str.split('/')[0]
                elif date_str:
                    year_match = re.search(r'(\d{4})', date_str)
                    if year_match:
                        meta['year'] = year_match.group(1)
            elif name == 'citation_online_date':
                if not meta.get('publication_date'):
                    meta['publication_date'] = content.strip()
            elif name == 'citation_pdf_url':
                meta['pdf_url'] = content.strip()
            elif name == 'citation_abstract':
                meta['abstract'] = content.strip()
            elif name == 'citation_keywords':
                meta['keywords'] = [k.strip() for k in content.split(';') if k.strip()]

        if authors:
            meta['authors'] = authors

        return meta

    # ------------------------------------------------------------------
    # HTML preprocessing (math extraction, GIF replacement)
    # ------------------------------------------------------------------

    @staticmethod
    def _preprocess_iop_html(html_fragment: str) -> tuple:
        """Preprocess IOP HTML to extract math and replace GIF entities.

        IOP stores LaTeX directly in <script type=\"math/tex\"> tags rather
        than using MathML.  Display equations are extracted into placeholders
        so the surrounding HTML→Markdown pipeline doesn't corrupt them.

        Returns (processed_html, display_eqns) where display_eqns is a
        dict mapping placeholder strings to LaTeX source.
        """
        import re as _re

        display_eqns = {}

        # 1. Extract display math → placeholders
        def _replace_display(m):
            latex = m.group(1).strip()
            key = f"<<<IOP_DISPLAY_MATH_{len(display_eqns)}>>>"
            display_eqns[key] = latex
            return f"\n{key}\n"

        html = _re.sub(
            r'<script\s+type="math/tex;\s*mode=display">(.*?)</script>',
            _replace_display, html_fragment, flags=_re.DOTALL,
        )

        # 2. Inline math → $...$
        html = _re.sub(
            r'<script\s+type="math/tex">(.*?)</script>',
            lambda m: f"${m.group(1).strip()}$",
            html, flags=_re.DOTALL,
        )

        # 2b. Remove MathJax-rendered image wrappers (hidden <span class="texImage">
        #     containing base64 <img role="math">).  The LaTeX source is already
        #     extracted from <script type="math/tex"> above; these renderings
        #     would otherwise leak as cruft through the HTML→Markdown pipeline.
        html = _re.sub(
            r'<span\s+class="texImage"\s+style="display:\s*none;">.*?</span>',
            '', html, flags=_re.DOTALL,
        )
        #     Unwrap the now-empty outer inline-eqn / tex wrappers
        #     (attribute order varies: class may precede xmlns:xlink).
        html = _re.sub(
            r'<span\s+[^>]*class="inline-eqn"[^>]*>\s*<span\s+class="tex">\s*(\$[^<]*?\$)\s*</span>\s*</span>',
            r'\1', html,
        )

        # 3. GIF epsilon → \epsilon
        #    Common pattern: <em><img src="...epsi.gif" .../><sub>r</sub></em>
        html = _re.sub(
            r'<em>\s*<img\s+[^>]*src="https?://cdn\.images\.iop\.org/Entities/epsi\.gif"[^>]*/?>\s*<sub>r</sub>\s*</em>',
            r'<em>\\epsilon_{\\text{r}}</em>',
            html,
        )
        #    Standalone epsilon GIF
        html = _re.sub(
            r'<img\s+[^>]*src="https?://cdn\.images\.iop\.org/Entities/epsi\.gif"[^>]*/?>',
            r'\\epsilon',
            html,
        )

        return html, display_eqns

    # ------------------------------------------------------------------
    # Body text extraction
    # ------------------------------------------------------------------

    @classmethod
    def extract_article_text_from_html(cls, html_content: str):
        """Extract article body, returning (abstract_md, body_md).

        IOP stores LaTeX equations in <script type=\"math/tex\"> tags.
        A preprocessing pass extracts display equations into placeholders
        and converts inline math to $...$ before the HTML→MD pipeline runs.
        """
        if not html_content:
            return '', ''

        soup = BeautifulSoup(html_content, 'html.parser')

        # Abstract via shared fallback (with IOP inline-math preprocessing)
        abstract_md = extract_abstract_with_fallbacks(
            soup,
            paragraph_converter=lambda h: cls._convert_iop_paragraph_to_md(h),
        )

        # Body via shared generic body finder
        body_div = find_generic_article_body(soup)
        if not body_div:
            return abstract_md, ''

        body_parts = []

        for element in body_div.find_all(
            ['h2', 'h3', 'h4', 'h5', 'h6', 'p', 'div', 'figure', 'table', 'ul', 'ol'],
            recursive=False,
        ):
            if element.name in ('h2', 'h3', 'h4', 'h5', 'h6'):
                # h2 → ###, h3 → ####, h4 → #####, h5/h6 → ######
                # (markdown caps at ######, so h5/h6 both map to it)
                level = '#' * min(int(element.name[1]) + 1, 6)
                heading_md = render_heading_md(
                    element, level, converter=cls._convert_iop_paragraph_to_md
                )
                if heading_md:
                    body_parts.extend([heading_md, ""])

            elif element.name == 'figure' and 'boxout' in element.get('class', []):
                # Inline figures within the body — already handled by figure extraction,
                # but add a placeholder caption so body flow isn't broken.
                fig_div = element.find('figcaption')
                if fig_div:
                    strong = fig_div.find('strong')
                    if strong:
                        label = strong.get_text(' ', strip=True)
                        caption_p = fig_div.find('p')
                        if caption_p:
                            caption = cls._convert_iop_paragraph_to_md(str(caption_p))
                            # Strip leading bold label (e.g., **Fig. 1:**) — we add our own
                            caption = re.sub(r'^\*\*Fig\.?\s*\d+[.:]\*\*\s*', '', caption).strip()
                        else:
                            caption = ''
                        body_parts.append(f"**{label}** {caption}")
                        body_parts.append("")

            elif element.name == 'p':
                p_md = cls._convert_iop_paragraph_to_md(str(element))
                if p_md:
                    body_parts.extend([p_md, ""])

            elif element.name == 'div':
                classes = element.get('class', [])
                # Display equations
                if 'display-eqn' in classes:
                    script_tag = element.find('script', type='math/tex; mode=display')
                    if script_tag:
                        latex = script_tag.string.strip()
                        body_parts.extend([f"\n$$\n{latex}\n$$\n", ""])
                    else:
                        # Fallback: equation rendered as GIF image
                        img = element.find('img')
                        if img:
                            alt = img.get('alt', '')
                            body_parts.extend([f"\n$$\n\\text{{{alt}}}\n$$\n", ""])
                # Other divs (e.g., article-text wrapper, display-eqn) — recurse as needed
                elif 'article-text' in classes:
                    cls._walk_iop_body(element, body_parts)
                # ⚠️ div.tableBox holds the front-matter lists ("List of
                # symbols", "List of abbreviations") as plain <table>s with no
                # data-toolbar-type, so neither this walk nor
                # extract_tables_from_html saw them. Measured on
                # 10.1088/2515-7647/ac9e2f: two such tables, 30 and 110 rows,
                # and the md showed both headings with nothing underneath.
                elif 'tableBox' in classes:
                    for plain in element.find_all('table'):
                        tbl_md = cls._table_element_to_md(plain)
                        if tbl_md:
                            body_parts.extend([tbl_md, ""])

            elif element.name == 'table' and element.get('data-toolbar-type') == 'table':
                tbl_md = cls._table_element_to_md(element)
                if tbl_md:
                    body_parts.extend([tbl_md, ""])

            elif element.name in ('ul', 'ol'):
                # Convert each <li> through the IOP math-aware paragraph
                # pipeline so embedded $...$ inline equations survive, then
                # emit as a Markdown bullet / numbered list.
                list_md = cls._convert_iop_list_to_md(element)
                if list_md:
                    body_parts.extend([list_md, ""])

        body_md = ""
        if body_parts:
            # Restore any display equation placeholders (from preprocessed paragraphs)
            body_md = "\n".join(body_parts).strip()

        return abstract_md, body_md

    @classmethod
    def _walk_iop_body(cls, container, body_parts: list):
        """Recursively walk IOP article-text containers, extracting headings,
        paragraphs, and display equations.

        IOP nests article content inside multiple ``div.article-text`` layers;
        this method handles arbitrary nesting depth.
        """
        for child in container.find_all(
            ['p', 'h3', 'h4', 'h5', 'h6', 'div', 'figure', 'table', 'ul', 'ol'],
            recursive=False,
        ):
            if child.name in ('h3', 'h4', 'h5', 'h6'):
                # h3 → ####, h4 → #####, h5/h6 → ######
                level = '#' * min(int(child.name[1]) + 1, 6)
                heading_md = render_heading_md(
                    child, level, converter=cls._convert_iop_paragraph_to_md
                )
                if heading_md:
                    body_parts.extend([heading_md, ""])

            elif child.name == 'p':
                p_md = cls._convert_iop_paragraph_to_md(str(child))
                if p_md:
                    body_parts.extend([p_md, ""])

            elif child.name == 'figure' and 'boxout' in child.get('class', []):
                fig_div = child.find('figcaption')
                if fig_div:
                    strong = fig_div.find('strong')
                    if strong:
                        label = strong.get_text(' ', strip=True)
                        caption_p = fig_div.find('p')
                        if caption_p:
                            caption = cls._convert_iop_paragraph_to_md(str(caption_p))
                            caption = re.sub(r'^\*\*Fig\.?\s*\d+[.:]\*\*\s*', '', caption).strip()
                        else:
                            caption = ''
                        body_parts.append(f"**{label}** {caption}")
                        body_parts.append("")

            elif child.name == 'div':
                classes = child.get('class', [])

                if 'display-eqn' in classes:
                    script_tag = child.find('script', type='math/tex; mode=display')
                    if script_tag:
                        latex = script_tag.string.strip()
                        body_parts.extend([f"\n$$\n{latex}\n$$\n", ""])
                    else:
                        img = child.find('img')
                        if img:
                            alt = img.get('alt', '')
                            body_parts.extend([f"\n$$\n\\text{{{alt}}}\n$$\n", ""])

                elif 'article-text' in classes:
                    cls._walk_iop_body(child, body_parts)

                elif 'boxout' in classes:
                    # IOP wraps tables/figures in <div class="boxout ...">
                    # Extract description from leading <p> (e.g., "Parameter ranges and sweep configurations")
                    desc = ''
                    caption_p = child.find('p')
                    if caption_p:
                        bold = caption_p.find('b')
                        if bold:
                            label = bold.get_text(' ', strip=True)
                            desc_full = caption_p.get_text(' ', strip=True)
                            # Remove the label part to get just the description
                            desc = desc_full[len(label):].strip().lstrip('\xa0').strip()
                    table = child.find('table', {'data-toolbar-type': 'table'})
                    if table:
                        tbl_md = cls._table_element_to_md(table, description=desc)
                        if tbl_md:
                            body_parts.extend([tbl_md, ""])

            elif child.name == 'table' and child.get('data-toolbar-type') == 'table':
                tbl_md = cls._table_element_to_md(child)
                if tbl_md:
                    body_parts.extend([tbl_md, ""])

            elif child.name in ('ul', 'ol'):
                list_md = cls._convert_iop_list_to_md(child)
                if list_md:
                    body_parts.extend([list_md, ""])
                # Ignore other divs (boxout figures are handled by figure extraction)

    @classmethod
    def _convert_iop_list_to_md(cls, list_el) -> str:
        """Convert a body-level ``<ul>`` / ``<ol>`` into a Markdown list.

        Each ``<li>`` is run through the IOP math-aware paragraph pipeline so
        embedded ``$…$`` inline equations and ``<sub>``/``<sup>`` formatting
        survive. Returns ``""`` if the list has no non-empty items.
        """
        if list_el is None:
            return ''
        ordered = (list_el.name == 'ol')
        lines = []
        for i, li in enumerate(list_el.find_all('li', recursive=False), 1):
            item_md = cls._convert_iop_paragraph_to_md(str(li))
            # _convert_iop_paragraph_to_md may emit a multi-line block (e.g.
            # if a display equation is in the item); fold it into a single
            # line for the list view.
            item_md = re.sub(r'\s*\n\s*', ' ', item_md).strip()
            if not item_md:
                continue
            marker = f"{i}." if ordered else '-'
            lines.append(f"{marker} {item_md}")
        return "\n".join(lines)

    @staticmethod
    def _convert_iop_paragraph_to_md(html_fragment: str) -> str:
        """Preprocess IOP math markup then convert the fragment to Markdown."""
        processed, display_eqns = IOPHandler._preprocess_iop_html(html_fragment)
        # convert_html_fragment_to_markdown calls prepare_mathjax_html_fragment internally.
        md = convert_html_fragment_to_markdown(processed) if processed else ""
        # Restore display equations from placeholders
        for placeholder, latex in display_eqns.items():
            md = md.replace(placeholder.strip(), f"\n$$\n{latex}\n$$\n")
        md = md.strip()
        # Collapse excess blank lines
        md = re.sub(r'\n{3,}', '\n\n', md)
        return md

    # ------------------------------------------------------------------
    # Figure extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _figure_is_graphical_abstract(fig_elem) -> bool:
        """True when *fig_elem* lives inside an abstract container.

        IOP places the graphical abstract as
        ``<figure id="njp…ga1" data-toolbar-type="figure">`` under
        ``<div class="article-text wd-jnl-art-abstract">`` →
        ``<div class="article-abstract">``.  Those should not be numbered
        alongside the real article figures, otherwise every body reference
        to "Figure N" embeds the wrong image (off-by-one).
        """
        for parent in fig_elem.parents:
            classes = parent.get('class') or []
            if not classes:
                continue
            if ('article-abstract' in classes
                    or 'wd-jnl-art-abstract' in classes):
                return True
        return False

    @classmethod
    def extract_graphical_abstract_url(cls, html_content: str) -> str:
        """Return the URL of the article's graphical abstract image, if any.

        IOP exposes the graphical abstract as a regular
        ``<figure data-toolbar-type="figure" id="…ga1">`` element nested
        inside the article-abstract container.  The same high-res / std
        download anchors are used as for real figures.
        """
        if not html_content:
            return ''
        soup = BeautifulSoup(html_content, 'html.parser')
        for fig_elem in soup.find_all('figure', {'data-toolbar-type': 'figure'}):
            if not cls._figure_is_graphical_abstract(fig_elem):
                continue
            hr_link = fig_elem.find('a', class_='fig-dwnld-hi-img')
            lr_link = fig_elem.find('a', class_='fig-dwnld-std-img')
            url = ''
            if hr_link:
                url = hr_link.get('href', '').strip()
            if not url and lr_link:
                url = lr_link.get('href', '').strip()
            if not url:
                img = fig_elem.find('img')
                if img:
                    url = (img.get('data-src') or img.get('src') or '').strip()
            if url and 'data:image' not in url:
                return url
        return ''

    @classmethod
    def extract_figures_from_html(cls, html_content: str) -> dict:
        """Extract figure URLs and captions from HTML.

        IOP figures use <figure id=\"dae...\" class=\"boxout\"
        data-toolbar-type=\"figure\"> with hi-res and standard download links
        inside figcaption.  High-resolution images are preferred.

        Figures inside the abstract container (the graphical abstract) are
        excluded — they are returned by ``extract_graphical_abstract_url``
        and downloaded separately as ``key_image.*``.
        """
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        figures = {}
        seen_ids = set()

        for fig_elem in soup.find_all('figure', {'data-toolbar-type': 'figure'}):
            if cls._figure_is_graphical_abstract(fig_elem):
                continue
            fig_id = fig_elem.get('id', '')
            if fig_id in seen_ids:
                continue
            seen_ids.add(fig_id)

            fig_num = len(figures) + 1
            key = f"fig_{fig_num}"

            # Prefer high-resolution download link
            hr_link = fig_elem.find('a', class_='fig-dwnld-hi-img')
            lr_link = fig_elem.find('a', class_='fig-dwnld-std-img')
            img_url = ''
            if hr_link:
                img_url = hr_link.get('href', '')
            if not img_url and lr_link:
                img_url = lr_link.get('href', '')

            # Fallback: find any img with data-src
            if not img_url:
                img = fig_elem.find('img')
                if img:
                    img_url = img.get('data-src') or img.get('src') or ''

            if not img_url or 'data:image' in img_url:
                continue

            # Extract caption
            caption = ''
            fig_div = fig_elem.find('div', class_='figure-caption')
            if fig_div:
                caption_p = fig_div.find('p')
                if caption_p:
                    caption = cls._convert_iop_paragraph_to_md(str(caption_p))
                else:
                    caption = fig_div.get_text(' ', strip=True)
                caption = re.sub(r'\s+', ' ', caption).strip()

            figures[key] = {
                'url': img_url.strip(),
                'caption': caption,
            }

        # Fallback: generic img search for figure-like URLs
        if not figures:
            for img in soup.find_all('img'):
                src = img.get('data-src') or img.get('src') or ''
                if not src or 'data:image' in src:
                    continue
                if 'dae' in src.lower() and '_hr.' in src:
                    fig_num = len(figures) + 1
                    figures[f"fig_{fig_num}"] = {'url': src.strip(), 'caption': ''}

        return figures

    # ------------------------------------------------------------------
    # Table extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _process_table_cell(cell_html: str) -> str:
        """Process a table cell's inner HTML to plain text with formulas preserved.

        ⚠️ Callers pass ``decode_contents()``, not ``str(cell)``. The wrapping
        ``<td>`` changed nothing about the output -- pandoc drops it -- but it
        meant every fragment contained a tag, which defeated the no-markup
        fast path in convert_html_fragment_to_markdown. On a tables-heavy
        article that is the difference between one pandoc process per cell and
        almost none: 10.1088/2515-7647/ac9e2f has 6,428 cells.

        Handles <script type=\"math/tex\"> → $...$, GIF epsilon, and HTML formatting
        (sub/sup/em) using the same IOP preprocessing pipeline as body paragraphs.
        """
        processed, _ = IOPHandler._preprocess_iop_html(cell_html)
        md = convert_html_fragment_to_markdown(processed) if processed else ''
        md = re.sub(r'\s+', ' ', md).strip()
        return md

    @staticmethod
    def _table_element_to_md(table_element, description: str = '') -> str:
        """Convert a single <table data-toolbar-type="table"> to Markdown.

        Returns a markdown string like ``**Title.** description\\n| ... |``
        or ``""`` if the table has no data rows.
        """
        title = table_element.get('data-toolbar-title', '').strip()
        if not title:
            # ⚠️ Search only inside the table's own wrapper. A bare
            # find_previous('strong') walks the whole document backwards and
            # will happily return site chrome -- measured on
            # 10.1088/2515-7647/ac9e2f, the "List of symbols" table came out
            # titled "**Next**", picked up from a pagination button. A table
            # with no title nearby should simply have none: the section
            # heading above it already says what it is.
            box = (table_element.find_parent('div', class_='boxout')
                   or table_element.find_parent('div', class_='tableBox'))
            strong = box.find('strong') if box is not None else None
            if strong is not None and IOPHandler._is_after(strong, table_element):
                title = strong.get_text(' ', strip=True)

        md_rows = []
        thead = table_element.find('thead')
        if thead:
            header_cells = []
            for th in thead.find_all('th'):
                header_cells.append(
                    IOPHandler._process_table_cell(th.decode_contents()))
            if header_cells:
                md_rows.append('| ' + ' | '.join(header_cells) + ' |')
                md_rows.append('|' + '|'.join(['---'] * len(header_cells)) + '|')

        tbody = table_element.find('tbody') or table_element
        for tr in tbody.find_all('tr'):
            if thead and tr.find_parent('thead'):
                continue
            cells = []
            for cell in tr.find_all(['td', 'th']):
                text = IOPHandler._process_table_cell(cell.decode_contents())
                text = re.sub(r'\s+', ' ', text)
                cells.append(text)
            if cells and not all(c == '' for c in cells):
                if md_rows and len(cells) < md_rows[0].count('|') - 1:
                    cells.extend([''] * (md_rows[0].count('|') - 1 - len(cells)))
                md_rows.append('| ' + ' | '.join(cells) + ' |')

        if len(md_rows) <= 1:
            return ''

        heading = title.rstrip('.')
        if description:
            heading = f"{heading} — {description}"
        # ⚠️ No heading line at all when there is no title. Emitting
        # ``****`` (bold with nothing in it) is worse than omitting it: it
        # reads as a rendering bug, and the front-matter tables that have no
        # title already sit under their own <h2>.
        lines = ([f"**{heading}**", ""] if heading else []) + ["\n".join(md_rows)]
        # ⚠️ Footnotes belong here, not in the caller. IOP's tables are
        # reached by two different walks (div.boxout in _walk_iop_body, and
        # the bare <table> branch), and only this function is common to both
        # -- attaching them one level up got them onto a list that nothing
        # reads. See _table_footnotes for where they live in the markup.
        notes = IOPHandler._table_footnotes(table_element)
        if notes:
            lines.extend(["", notes])
        return "\n".join(lines)

    @classmethod
    def extract_tables_from_html(cls, html_content: str) -> list:
        """Extract tables from IOP article body.

        IOP tables use <table cellpadding=\"0\" data-toolbar-type=\"table\">
        with title in the data-toolbar-title attribute.

        Returns a list of (title, markdown_string) tuples.
        """
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        tables = []

        for tbl in soup.find_all('table', {'data-toolbar-type': 'table'}):
            md = cls._table_element_to_md(tbl)
            if md:
                title = tbl.get('data-toolbar-title', '').strip()
                tables.append((title, md))

        return tables

    @classmethod
    def _table_footnotes(cls, table_element) -> str:
        """Footnote lines printed under a table, or ''.

        ⚠️ IOP puts them *outside* the table, as ``<p><small>`` siblings
        inside the wrapping ``div.boxout``. Extracting the table alone drops
        them, and they carry the definitions the numbers mean nothing without
        -- measured on 10.1088/2515-7647/ac9e2f Table 9: "* Kerr coefficient
        is defined in the paper as K = Δn/λE²".
        """
        box = table_element.find_parent('div', class_='boxout')
        if box is None:
            return ''
        lines = []
        for small in box.find_all('small'):
            # Only what follows the table; a caption above it is already the
            # title.
            if not cls._is_after(table_element, small):
                continue
            text = convert_html_fragment_to_markdown(small.decode_contents())
            if text:
                lines.append(text)
        return '\n\n'.join(lines)

    @staticmethod
    def _is_after(anchor, node) -> bool:
        """True when *node* appears after *anchor* in document order."""
        for element in anchor.next_elements:
            if element is node:
                return True
        return False

    # ------------------------------------------------------------------
    # Reference extraction
    # ------------------------------------------------------------------

    @classmethod
    def extract_references_from_html(cls, html_content: str) -> list:
        """Reference strings from citation_reference meta tags, as readable text.

        The meta content is semi-colon separated ``key=value`` pairs. It used
        to be turned into a BibTeX entry; the output now keeps the citation as
        prose, with the DOI link intact, because that is what a reader — human
        or agent — actually wants out of a reference list.
        """
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        references = []

        for tag in soup.find_all('meta', {'name': 'citation_reference'}):
            ref_str = tag.get('content', '')
            if not ref_str:
                continue
            parts = {}
            for segment in ref_str.split(';'):
                if '=' not in segment:
                    continue
                k, v = segment.split('=', 1)
                k = k.strip()
                v = re.sub(r'\s+', ' ', v).strip()
                if k and v:
                    parts[k] = v
            text = format_citation_as_text(parts) if parts else ref_str.strip()
            if text:
                references.append(text)

        return references

    @classmethod
    def _extract_raw_citation_references(cls, html_content: str) -> list:
        """Return raw citation_reference meta tag content strings."""
        if not html_content:
            return []
        soup = BeautifulSoup(html_content, 'html.parser')
        return [tag.get('content', '') for tag in
                soup.find_all('meta', {'name': 'citation_reference'})
                if tag.get('content', '').strip()]

    @classmethod
    def extract_footnotes_from_html(cls, html_content: str) -> list:
        """Extract footnotes from <h2 id=\"footnotes\"> section.

        Returns a list of markdown strings, one per footnote.
        Footnotes may contain LaTeX formulas (handled via IOP math pipeline).
        """
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        footnotes_h2 = soup.find('h2', id='footnotes')
        if not footnotes_h2:
            return []

        footnotes = []
        container = footnotes_h2.find_next_sibling('div')
        if not container:
            return []

        for li in container.find_all('li', class_='indices-list'):
            id_div = li.find('div', class_='indices-id')
            content_div = li.find('div', class_='indices-content')
            if not id_div or not content_div:
                continue

            fn_num = id_div.get_text(' ', strip=True)
            fn_ps = content_div.find_all('p')
            fn_texts = []
            for p in fn_ps:
                p_html = str(p)
                md = cls._convert_iop_paragraph_to_md(p_html)
                if md:
                    fn_texts.append(md)

            combined = ' '.join(fn_texts) if fn_texts else content_div.get_text(' ', strip=True)
            if combined:
                footnotes.append(f"{fn_num}. {combined}")

        return footnotes

    # ------------------------------------------------------------------
    # Supplemental material extraction
    # ------------------------------------------------------------------

    # NB: no _extract_supplemental_links_from_html here. The same-named method
    # is live in aip.py / mdpi.py / cambridge.py; IOP's copy scanned the article
    # page for /article/.../data links but was never called, and extract_all now
    # gates on the supplDataLink anchor instead. Don't re-add it for symmetry.

    @staticmethod
    def _looks_like_data_page(html: str) -> bool:
        """Accept a real /data page; reject a challenge or error page.

        Deliberately *not* "contains #supplementarydata": plenty of IOP
        articles have no supplementary files at all, and treating those as a
        failed fetch would walk the whole ladder -- launching a throwaway
        Chrome -- only to rediscover that there is nothing there. Whether the
        article has supplements is the parser's answer to give, not the
        fetcher's.
        """
        lowered = (html or '').lower()
        if any(marker in lowered for marker in (
                'just a moment', 'checking your browser',
                'cf-browser-verification', 'enable javascript and cookies')):
            return False
        return 'supplementarydata' in lowered or 'iopscience' in lowered

    @staticmethod
    def _parse_supplementary_links(html: str, base_url: str) -> tuple:
        """Pull the supplementary links out of a /data page's markup.

        Parsed from HTML rather than queried from a live DOM: the ladder's
        other rungs (a plain request, a throwaway Chrome) never have one --
        only the browser-tab rung does.

        IOP supplementary links are structured as::

            <div id="supplementarydata">
              <div class="reveal-content" style="display: block;">
                <p class="mb-0">
                  <a class="link--decoration-none" href="S3_URL">Supplementary data N</a>
                </p>
                <div>(size FORMAT) description</div>
        """
        urls = []
        descriptions = {}
        if not html:
            return urls, descriptions

        soup = BeautifulSoup(html, 'html.parser')
        supp_div = soup.find(id='supplementarydata')
        if not supp_div:
            return urls, descriptions

        for anchor in supp_div.find_all('a', class_='link--decoration-none'):
            href = (anchor.get('href') or '').strip()
            if not href:
                continue
            url = urljoin(base_url, href)
            # Self-links and links back into the article are not attachments.
            if url == base_url or '/article/' in url:
                continue
            urls.append(url)
            text = anchor.get_text(strip=True)[:200]
            if text:
                descriptions[url] = text
        return urls, descriptions

    @staticmethod
    async def _extract_supplementary_from_data_page(page, doi: str, captured_data_dir=None,
                                                    force_headed: bool = False) -> tuple:
        """Fetch the IOP supplementary /data page and extract download links.

        The endpoint is uniform for every article:
            https://iopscience.iop.org/article/{doi}/data

        ⚠️ But most articles have nothing behind it. Do NOT call this
        unconditionally -- ``extract_all`` gates it on the ``supplDataLink``
        anchor in the article page, because reaching this endpoint costs the
        whole fetch ladder and, on IOP, a chance of a Radware challenge. The
        docstring used to claim every article "has" a supplementary endpoint,
        which is how the unconditional call got written in the first place.

        The page goes through the shared fetch ladder (plain request with the
        article session's cookies, then a browser tab, then a throwaway
        Chrome). IOP is the reason the last rung exists: the /data page can
        come back as a challenge that expects a human to click, by which point
        the shared browser has been driven by automation long enough to be
        refused.

        If ``captured_data_dir`` is provided, the page is saved as
        ``supp.html`` there — useful for offline re-parsing / debugging.

        Returns (urls, descriptions) tuple.
        """
        # Defensive guard: a missing doi would produce ".../article/None/data",
        # which is not a real IOP URL and just loads a 404 page.
        if not doi or doi == 'None':
            print(f"  ⚠ 跳过补充材料: DOI 缺失 (got {doi!r})")
            return [], {}

        data_url = f"https://iopscience.iop.org/article/{doi}/data"
        print(f"  🔗 访问补充材料页面: {data_url}")

        current_url = (getattr(page, 'url', '') or '') if page is not None else ''

        html = await fetch_html_via_ladder(
            data_url,
            kind='api',
            page=page,
            referer=current_url or None,
            expect=IOPHandler._looks_like_data_page,
            restore_url=current_url or None,
            # The throwaway Chrome has to match the run. Headless on a headed
            # run is the most detectable browser we could show IOP, and its
            # Radware check answers with a captcha instead of the page.
            headless=not force_headed,
        )

        if not html:
            print("  ⚠ 补充材料页面未取到（阶梯各层均失败）")
            return [], {}

        if captured_data_dir is not None:
            try:
                supp_html_file = Path(captured_data_dir) / "supp.html"
                with open(supp_html_file, 'w', encoding='utf-8') as f:
                    f.write(html)
                print(f"  ✓ 补充材料页面HTML已保存: {supp_html_file.name}")
            except Exception as e:
                print(f"  ⚠ 补充材料HTML保存失败: {e}")

        urls, descriptions = IOPHandler._parse_supplementary_links(html, data_url)
        print(f"  ✓ 补充材料: {len(urls)} 个文件")
        return urls, descriptions

    # ------------------------------------------------------------------
    # Publisher contract methods
    # ------------------------------------------------------------------

    async def extract_metadata(self, page) -> dict:
        """Return metadata from HTML meta tags and DOM."""
        # Prefer the raw server body over the live DOM: everything this parser
        # needs (citation_* meta, abstract) is in the pre-JS response, so there
        # is no reason to read the rendered page. get_page_html falls back to
        # page.content() when no raw body was captured.
        html_content = ''
        if page is not None:
            try:
                html_content = await self.get_page_html(page)
            except Exception:
                html_content = ''

        meta = self._extract_metadata_from_html_meta(html_content)

        abstract = meta.get('abstract', '')
        if not abstract and html_content:
            try:
                soup = BeautifulSoup(html_content, 'html.parser')
                abstract = extract_abstract_with_fallbacks(soup)
            except Exception:
                pass

        return {
            'title': meta.get('title') or 'IOP Article',
            'authors': meta.get('authors', []),
            'author_with_affiliations': [],
            'corresponding_author_emails': [],
            'abstract': abstract,
            'journal': meta.get('journal') or 'IOP Publishing',
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

    async def get_pdf_url(self, doi: str) -> str:
        """PDF URL is extracted from citation_pdf_url meta tag in extract_metadata."""
        return None

    async def get_supplemental_url(self, doi: str) -> str:
        """Supplementary links are extracted via _extract_supplementary_from_data_page."""
        return None

    async def extract_references(self, html: str) -> list:
        """Delegates to extract_references_from_html (classmethod)."""
        return self.extract_references_from_html(html) if html else []

    async def get_figures(self, json_data: dict) -> dict:
        """Figure extraction uses extract_figures_from_html (classmethod) in extract_all."""
        return {}

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        """Run the IOP handler through the unified publisher contract."""
        # Initialize page and managed resources using shared function.
        # The workflow often calls extract_all() with doi=None and relies on
        # init_extract_all_page to populate self.doi from elsewhere (e.g. the
        # already-configured handler).  Pick the resolved value back up here
        # so the downstream supplementary URL doesn't become "/article/None/data".
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'IOPHandler'
        )
        doi = self.doi

        # Get the actual page URL for correct base_url resolution
        set_actual_base_url(self, page)

        try:
            metadata = await self.extract_metadata(page)
            metadata['doi'] = doi

            pdf_url = metadata.pop('_pdf_url', None)
            keywords = metadata.pop('_keywords', [])

            # Same here: body, figures, tables, references and footnotes are
            # all present in the raw response (verified offline against the
            # captured page_raw.html), so the live DOM is never needed.
            try:
                fulltext_html = await self.get_page_html(page)
            except Exception:
                fulltext_html = ''

            if fulltext_html:
                metadata['references'] = self.extract_references_from_html(fulltext_html)
                metadata['_refs_raw'] = self._extract_raw_citation_references(fulltext_html)
                metadata['footnotes'] = self.extract_footnotes_from_html(fulltext_html)

            figure_urls = {}
            supp_urls = []
            supp_descriptions = {}
            if fulltext_html:
                figure_urls = self.extract_figures_from_html(fulltext_html)
                # ❌ extract_tables_from_html() is deliberately NOT called
                # here. Its result went to metadata['_tables'], which nothing
                # ever read -- the tables in the Markdown come from the body
                # walk (div.boxout in _walk_iop_body). On a tables-heavy
                # article that dead call was the single most expensive thing
                # in the run: measured 46 s on 10.1088/2515-7647/ac9e2f,
                # converting all 6,428 cells a second time and throwing the
                # result away.
                # Graphical abstract (figure inside the abstract container)
                # is downloaded as key_image via the shared workflow path,
                # not as fig_N, so it doesn't bump the body figure numbering.
                key_image_url = self.extract_graphical_abstract_url(fulltext_html)
                if key_image_url:
                    metadata['key_image_url'] = key_image_url

            # Supplementary: only a minority of IOP articles have any, and the
            # article page says so up front -- it renders
            #     <a id="supplDataLink" href="/article/{doi}/data" ...>
            # exactly when there is something behind that endpoint. Checking it
            # here is what keeps the majority from walking the whole fetch
            # ladder (plain request -> tab -> throwaway Chrome) to reach a page
            # with nothing on it, which on IOP also means courting a Radware
            # challenge for no reason.
            #
            # ⚠️ Skip only when the markup is in hand AND lacks the anchor. If
            # fulltext_html came back empty (the content() above failed), fall
            # through and try anyway: silently dropping supplements for every
            # article whose DOM read hiccuped is a far worse failure than one
            # wasted fetch. Verified against both captured samples -- the
            # anchor is present in page.html and page_raw.html of the article
            # that has data, and absent from the one that does not.
            _has_supp_link = (
                'supplDataLink' in fulltext_html
                or 'wd-btn-supp-data' in fulltext_html
            )
            if fulltext_html and not _has_supp_link:
                print("  ⏭  正文页无 supplDataLink，本文没有补充材料，跳过 /data 页面")
            elif page is not None:
                try:
                    supp_urls, supp_descriptions = (
                        await self._extract_supplementary_from_data_page(
                            page, doi, captured_data_dir=self.captured_data_dir,
                            # Pinned by process_with_handler before extract_all
                            # runs; a handler has no other way to know.
                            force_headed=self.is_headed_run(),
                        )
                    )
                except Exception as e:
                    print(f"  ⚠ 补充材料提取异常: {e}")


            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figure_urls,
                    'supplemental_urls': supp_urls,
                    'supplemental_descriptions': supp_descriptions,
                },
                'fulltext_data': fulltext_html,
                'journal_name': 'iop',
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

    @classmethod
    def extract_main_abstract_from_html(cls, html_content: str) -> str:
        """Extract the main abstract via shared fallback."""
        if not html_content:
            return ''
        soup = BeautifulSoup(html_content, 'html.parser')
        return extract_abstract_with_fallbacks(soup)

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        """Generate complete Markdown for an IOP article."""
        title = metadata.get('title') or 'IOP Article'
        md_parts = [
            f"# {title}",
            "",
        ]

        # Authors
        authors = metadata.get('authors', [])
        author_with_affiliations = metadata.get('author_with_affiliations', [])
        if author_with_affiliations:
            md_parts.append("**Authors:**")
            md_parts.append("")
            for entry in author_with_affiliations:
                name = entry.get('author', '')
                md_parts.append(name)
                for aff in entry.get('affiliations', []):
                    md_parts.append(aff)
                md_parts.append("")
        elif authors:
            md_parts.append("**Authors:**")
            md_parts.append("")
            for author in authors:
                md_parts.append(author)
                md_parts.append("")

        if metadata.get('doi'):
            md_parts.extend([f"**DOI:** {metadata['doi']}", ""])

        md_parts.extend([
            "## Publication",
            "",
            f"**Journal:** {metadata.get('journal') or 'IOP Publishing'}",
            "",
        ])

        if metadata.get('volume'):
            md_parts.append(f"**Volume:** {metadata['volume']}")
            md_parts.append("")
        if metadata.get('issue'):
            md_parts.append(f"**Issue:** {metadata['issue']}")
            md_parts.append("")
        if metadata.get('pages'):
            md_parts.append(f"**Pages:** {metadata['pages']}")
            md_parts.append("")
        if metadata.get('publication_date'):
            md_parts.append(f"**Published:** {metadata['publication_date']}")
            md_parts.append("")

        # Abstract
        abstract = metadata.get('abstract', '')
        if abstract:
            md_parts.extend([
                "---",
                "",
                "## Abstract",
                "",
                abstract,
                "",
            ])

        # Body text
        body_md = ''
        if isinstance(article_text, str) and article_text.strip():
            if article_text.lstrip().startswith('<'):
                _, body_md = self.extract_article_text_from_html(article_text)
            else:
                body_md = article_text.strip()

        # Insert downloaded figure images after captions
        if kwargs.get('add_figure_refs') and kwargs.get('figure_filenames'):
            figure_filenames = kwargs['figure_filenames']
            for fig_num, filename in sorted(figure_filenames.items(), key=lambda x: int(x[0])):
                body_md = re.sub(
                    rf'(\*\*(?:Fig\.?|Figure)\s*{re.escape(fig_num)}[.:]\*\*[^\n]*)',
                    rf'\1\n\n![Figure {fig_num}.]({filename})',
                    body_md,
                )

        md_parts.extend([
            "---",
            "",
            "## Article Text",
            "",
            body_md or "[Article text not found.]",
            "",
        ])

        # Supplemental materials
        supplemental_urls = kwargs.get('supplemental_urls', [])
        supplemental_descriptions = kwargs.get('supplemental_descriptions', {})
        supplemental_downloads = kwargs.get('supplemental_downloads', [])

        if supplemental_urls or supplemental_downloads:
            md_parts.extend([
                "---",
                "",
                "## Supplemental Material",
                "",
            ])
            if supplemental_downloads:
                for dl in supplemental_downloads:
                    md_parts.append(f"- {dl}")
            elif supplemental_urls:
                for url in supplemental_urls:
                    md_parts.append(f"- [{url}]({url})")
            md_parts.append("")

        # Footnotes (before references)
        footnotes = metadata.get('footnotes', [])
        if footnotes:
            md_parts.extend([
                "---",
                "",
                "## Footnotes",
                "",
            ])
            for fn in footnotes:
                md_parts.append(fn)
                md_parts.append("")

        # References: each ref = numbered text + its own BibTeX block
        references = metadata.get('references', [])
        refs_raw = metadata.get('_refs_raw', [])
        crossref_refs = metadata.get('_crossref_references', [])

        if crossref_refs:
            # Use Crossref references if available (unified BibTeX generation)
            md_parts.extend([
                "---",
                "",
                "## References",
                "",
            ])
            for idx, ref in enumerate(crossref_refs, 1):
                # Get original unstructured reference if available
                unstructured = ref.get('unstructured', '')
                if unstructured:
                    md_parts.append(f"[{idx}] {unstructured}")
                else:
                    # Generate readable text from Crossref data
                    ref_text = generate_reference_text_from_crossref(ref, index=idx)
                    md_parts.append(ref_text)
                md_parts.append("")

            md_parts.append("")
        elif references:
            md_parts.extend([
                "---",
                "",
                "## References",
                "",
            ])
            for idx, ref in enumerate(references):
                idx1 = idx + 1
                # Numbered text
                if refs_raw and idx < len(refs_raw):
                    try:
                        raw = refs_raw[idx]
                        parts = {}
                        for segment in raw.split(';'):
                            if '=' not in segment:
                                continue
                            k, v = segment.split('=', 1)
                            k = k.strip()
                            v = re.sub(r'\s+', ' ', v).strip()
                            if k and v:
                                parts[k] = v
                        md_parts.append(format_citation_as_text(parts, index=idx1))
                    except Exception:
                        md_parts.append(f"[{idx1}] {ref}")
                else:
                    md_parts.append(f"[{idx1}] {ref}")
                md_parts.append("")
            md_parts.append("")

        return "\n".join(md_parts)

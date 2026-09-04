"""
Science / AAAS publisher handler (www.science.org).

Extracts metadata, body, figures, equations, references, and supplemental
materials from Science Advances and other AAAS journal articles.

Science wraps the rendered article in a fixed Atypon Literatum skeleton:
    <div id="abstracts" data-extent="frontmatter">      ← abstract
    <section id="bodymatter" data-extent="bodymatter">  ← body
        <h2>/<h3>                                      ← headings
        <div role="paragraph">                         ← paragraphs
        <div class="figure-wrap">                      ← figures
        <div class="display-formula">                  ← display math
    <section id="backmatter">
        <section id="supplementary-materials">         ← supplementary
        <section id="bibliography">                    ← references

Display and inline math are emitted as MathML (`<math>`); we reuse the shared
pandoc-based MathML→LaTeX conversion.  The page requires a headed browser
because of the AAAS bot-detection layer.
"""

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString

from publisher.base import PublisherHandler
from publisher.wildcard import (
    convert_html_fragment_to_markdown,
    format_as_bibtex,
    generate_bibtex_key,
    generate_reference_text_from_crossref,
    init_extract_all_page,
    render_heading_md,
    set_actual_base_url,
)


class ScienceHandler(PublisherHandler):
    """Handler for Science / AAAS articles (www.science.org)."""

    SCIENCE_BASE = 'https://www.science.org'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_metadata_from_html_meta(html_content: str) -> dict:
        """Read Dublin-Core ``<meta name="dc.*">`` + AAAS ``citation_*`` tags."""
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        meta = {}
        authors = []

        for tag in soup.find_all('meta'):
            name = (tag.get('name') or '').lower()
            content = tag.get('content', '')
            if not name or not content:
                continue
            if name == 'dc.title':
                meta['title'] = content.strip()
            elif name == 'dc.creator':
                authors.append(content.strip())
            elif name == 'citation_journal_title':
                meta['journal'] = content.strip()
            elif name == 'dc.publisher':
                meta['publisher'] = content.strip()
            elif name == 'dc.description':
                meta['abstract'] = content.strip()
            elif name == 'dc.date':
                # e.g. "2018-05"
                date_str = content.strip()
                meta['publication_date'] = date_str
                m = re.search(r'(\d{4})', date_str)
                if m:
                    meta['year'] = m.group(1)
            elif name == 'dc.identifier' and (tag.get('scheme') or '').lower() == 'doi':
                meta['doi'] = content.strip()
            elif name == 'publication_doi':
                meta.setdefault('doi', content.strip())

        if authors:
            seen = set()
            unique = []
            for a in authors:
                if a not in seen:
                    seen.add(a)
                    unique.append(a)
            meta['authors'] = unique

        # Volume / Issue / Published date from the DOM (not exposed in meta).
        volume_el = soup.find(attrs={'property': 'volumeNumber'})
        if volume_el:
            meta['volume'] = volume_el.get_text(' ', strip=True)
        issue_el = soup.find(attrs={'property': 'issueNumber'})
        if issue_el:
            meta['issue'] = issue_el.get_text(' ', strip=True)
        date_el = soup.find(attrs={'property': 'datePublished'})
        if date_el:
            full_date = re.sub(r'\s+', ' ', date_el.get_text(' ', strip=True)).strip()
            if full_date:
                meta['publication_date'] = full_date
                m = re.search(r'(\d{4})', full_date)
                if m:
                    meta['year'] = m.group(1)

        return meta

    # ------------------------------------------------------------------
    # MathML / paragraph conversion
    # ------------------------------------------------------------------

    _INLINE_MATH_PLACEHOLDER = "XSCIMATHX{idx:04d}XEND"
    _DISPLAY_MATH_PLACEHOLDER = "XSCIDISPLAYX{idx:04d}XEND"

    @staticmethod
    def _extract_core_tex_latex(span) -> str:
        """Extract LaTeX body from a ``<span class="core-tex">`` element.

        Science wraps raw TeX inside delimiter pairs:
            \\[ … \\]    display
            \\( … \\)    inline

        The closing delimiter is often split into a nested ``<span>\\</span>]``
        (or ``)``) to defeat copy/paste — we strip both ``\\[``/``\\]`` and
        ``\\(``/``\\)`` from the flattened text. Returns the LaTeX body with
        delimiters removed, or '' if the span has no text content.
        """
        if span is None:
            return ''
        text = span.get_text('', strip=False)
        if not text:
            return ''
        text = text.strip()
        # Strip leading display/inline delimiter
        if text.startswith('\\[') or text.startswith('\\('):
            text = text[2:]
        # Strip trailing display/inline delimiter
        if text.endswith('\\]') or text.endswith('\\)'):
            text = text[:-2]
        # Some pages serialize ``\]`` as ``<span>\</span>]`` — after get_text we
        # may end up with ``\]`` recombined OR a bare trailing ``]``.  Strip a
        # bare leading/trailing backslash too if present.
        text = text.strip()
        if text.endswith('\\'):
            text = text[:-1].rstrip()
        return text.strip()

    @classmethod
    def _stash_math(cls, soup, restore: dict, counter: list) -> None:
        """Convert all ``<math>`` elements to placeholders restoring as ``$...$``.

        Handles both inline (``<span role="math"><math display="inline">``) and
        display variants (``<math display="block">`` inside a
        ``<div class="display-formula">``).  Display blocks are processed
        separately by ``_preprocess_sci_math`` because they have an outer
        wrapper that also carries the equation label.
        """
        from html_to_md_converter import mathml_to_latex_pandoc

        for math_tag in soup.find_all('math'):
            # Skip those already inside a display-formula wrapper — handled
            # separately so we can grab the equation label.
            if math_tag.find_parent('div', class_='display-formula') is not None:
                continue
            latex = mathml_to_latex_pandoc(str(math_tag))
            if not latex:
                math_tag.decompose()
                continue
            latex = latex.strip()
            if latex.startswith('$$') and latex.endswith('$$'):
                body = latex[2:-2].strip()
            elif latex.startswith('$') and latex.endswith('$'):
                body = latex[1:-1].strip()
            else:
                body = latex
            key = cls._INLINE_MATH_PLACEHOLDER.format(idx=counter[0])
            counter[0] += 1
            restore[key] = f"${body}$"
            # The outer ``<span role="math">`` (if any) wraps a single <math>;
            # replace the whole span to avoid the empty span lingering.
            parent_span = math_tag.find_parent(attrs={'role': 'math'})
            if parent_span is not None and not parent_span.find('math', recursive=False):
                # Only replace the outer span if it directly wraps this math tag.
                target = parent_span if math_tag.parent is parent_span else math_tag
            else:
                target = math_tag
            target.replace_with(soup.new_string(f" {key} "))

    @classmethod
    def _preprocess_sci_math(cls, html_fragment: str) -> tuple:
        """Stash all Science math markup with pandoc-safe placeholders.

        Returns ``(processed_html, restore_map)``.  Display equations live
        inside ``<div class="display-formula">`` with a sibling
        ``<div class="label">(N)</div>`` carrying the visible number; the
        label gets folded into the LaTeX as ``\\quad (N)``.
        """
        if not html_fragment:
            return html_fragment, {}

        soup = BeautifulSoup(html_fragment, 'html.parser')
        restore = {}
        counter = [0]

        # 1. Display formulae first so the label survives.
        #    Two source formats coexist on Science:
        #      a) MathML inside <math> (e.g. sciadv.aar3761, science.1059413)
        #      b) Raw LaTeX inside <span class="core-tex">\[…\]</span>
        #         (e.g. science.1132838)
        from html_to_md_converter import mathml_to_latex_pandoc
        for disp in soup.find_all('div', class_='display-formula'):
            label_text = ''
            label_div = disp.find('div', class_='label')
            if label_div:
                label_text = label_div.get_text(' ', strip=True)

            body = ''
            math_tag = disp.find('math')
            if math_tag is not None:
                latex = mathml_to_latex_pandoc(str(math_tag)) or ''
                latex = latex.strip()
                if latex.startswith('$$') and latex.endswith('$$'):
                    body = latex[2:-2].strip()
                elif latex.startswith('$') and latex.endswith('$'):
                    body = latex[1:-1].strip()
                else:
                    body = latex
            else:
                core_tex = disp.find('span', class_='core-tex')
                if core_tex is not None:
                    body = cls._extract_core_tex_latex(core_tex)

            if body:
                if label_text:
                    eq_md = f"\n$$\n{body} \\quad {label_text}\n$$\n"
                else:
                    eq_md = f"\n$$\n{body}\n$$\n"
                key = cls._DISPLAY_MATH_PLACEHOLDER.format(idx=counter[0])
                counter[0] += 1
                restore[key] = eq_md
                disp.replace_with(soup.new_string(f"\n{key}\n"))
            else:
                disp.decompose()

        # 1b. Inline LaTeX spans (<span class="core-tex">\(…\)</span>) that
        #     live outside a display-formula wrapper.
        for span in soup.find_all('span', class_='core-tex'):
            body = cls._extract_core_tex_latex(span)
            if body:
                key = cls._INLINE_MATH_PLACEHOLDER.format(idx=counter[0])
                counter[0] += 1
                restore[key] = f"${body}$"
                span.replace_with(soup.new_string(f" {key} "))
            else:
                span.decompose()

        # 2. Remaining (inline) math.
        cls._stash_math(soup, restore, counter)

        # 3. Strip biblio xref anchors — keep visible text only.
        for a in soup.find_all('a', attrs={'role': 'doc-biblioref'}):
            text = a.get_text(' ', strip=True)
            a.replace_with(soup.new_string(f"[{text}]"))

        # 4. Unwrap structural divs/sections/spans pandoc would otherwise
        #    turn into ``::: {#id} ... :::`` fenced div blocks.
        for tag in soup.find_all(['div', 'section', 'span']):
            classes = tag.get('class') or []
            if 'display-formula' in classes or 'label' in classes:
                continue
            tag.unwrap()

        return str(soup), restore

    @classmethod
    def _convert_paragraph_to_md(cls, html_fragment: str) -> str:
        if not html_fragment:
            return ''
        processed, restore = cls._preprocess_sci_math(html_fragment)
        md = convert_html_fragment_to_markdown(processed) if processed else ''
        for placeholder, replacement in restore.items():
            md = md.replace(placeholder, replacement)
        md = md.strip()
        md = re.sub(r'\n{3,}', '\n\n', md)
        return md

    # ------------------------------------------------------------------
    # Body / abstract extraction
    # ------------------------------------------------------------------

    @classmethod
    def _abstract_md_from_section(cls, abs_section) -> str:
        """Convert an abstract <section> into Markdown paragraphs."""
        parts = []
        for child in abs_section.children:
            if not hasattr(child, 'name') or not child.name:
                continue
            if child.name in ('h2', 'h3'):
                # Skip the "Abstract" heading itself; emit sub-headings as bold.
                heading_text = child.get_text(' ', strip=True)
                if heading_text and heading_text.lower() != 'abstract':
                    parts.append(f"**{heading_text}**")
                    parts.append("")
                continue
            if child.name == 'div' and 'paragraph' in (child.get('role') or ''):
                md = cls._convert_paragraph_to_md(str(child))
                if md:
                    parts.append(md)
                    parts.append("")
                continue
            # Recurse into nested wrappers.
            md = cls._convert_paragraph_to_md(str(child))
            if md:
                parts.append(md)
                parts.append("")
        return "\n".join(parts).strip()

    @classmethod
    def extract_abstract_from_html(cls, html_content: str) -> str:
        if not html_content:
            return ''
        soup = BeautifulSoup(html_content, 'html.parser')
        abs_div = soup.find('div', id='abstracts')
        if not abs_div:
            return ''
        # Walk every abstract section inside (some articles ship multiple).
        chunks = []
        for sec in abs_div.find_all('section'):
            md = cls._abstract_md_from_section(sec)
            if md:
                chunks.append(md)
        if not chunks:
            # Fallback: convert the whole abstracts container.
            return cls._convert_paragraph_to_md(str(abs_div))
        return "\n\n".join(chunks).strip()

    @classmethod
    def _figure_caption_md(cls, fig_wrap) -> tuple:
        """Return ``(label, caption_md, notes_md, image_url)`` for a figure-wrap div.

        Handles two figcaption layouts seen in the wild:

        * Older (sciadv.aar3761 style)::

              <figcaption>
                <div class="caption"><span class="heading">Fig. 1</span> body</div>
                <div class="notes">…</div>
              </figcaption>

        * Newer (science.1059413 style)::

              <figcaption>
                <span class="heading">Figure 1</span> body
              </figcaption>
        """
        label = ''
        caption_md = ''
        notes_md = ''
        image_url = ''

        figure = fig_wrap.find('figure') or fig_wrap
        figcaption = figure.find('figcaption') if figure is not None else None

        if figcaption is not None:
            caption_div = figcaption.find('div', class_='caption')
            if caption_div is not None:
                heading = caption_div.find('span', class_='heading')
                if heading is not None:
                    label = heading.get_text(' ', strip=True)
                    heading.decompose()
                caption_md = cls._convert_paragraph_to_md(str(caption_div))
                notes_div = figcaption.find('div', class_='notes')
                if notes_div is not None:
                    notes_md = cls._convert_paragraph_to_md(str(notes_div))
            else:
                # Direct layout: heading + caption text live as children of <figcaption>
                # without a wrapping <div class="caption">.
                from bs4 import BeautifulSoup as _BS
                figcaption_copy = _BS(str(figcaption), 'html.parser')
                heading = figcaption_copy.find('span', class_='heading')
                if heading is not None:
                    label = heading.get_text(' ', strip=True)
                    heading.decompose()
                caption_md = cls._convert_paragraph_to_md(str(figcaption_copy))

        img = figure.find('img') if figure is not None else None
        if img is not None:
            image_url = (img.get('src') or img.get('data-src') or '').strip()

        label = re.sub(r'\s+', ' ', label).strip()
        caption_md = re.sub(r'^[.\s]+', '', caption_md or '').strip()
        notes_md = re.sub(r'^[.\s]+', '', notes_md or '').strip()
        return label, caption_md, notes_md, image_url

    @classmethod
    def _walk_sci_body(cls, container, parts: list, depth_offset: int = 0) -> None:
        """Render bodymatter into Markdown lines.

        ``depth_offset`` increments the heading level when recursing into
        ``<section>`` children so that the top-level h2 ends up as Markdown
        ``###`` (with the top-level ``## Article Text`` already supplied by
        ``convert_to_markdown``).
        """
        for child in container.children:
            if not hasattr(child, 'name') or not child.name:
                continue

            if child.name == 'section':
                cls._walk_sci_body(child, parts, depth_offset + 1)
                continue

            if child.name in ('h2', 'h3', 'h4', 'h5'):
                level = '#' * min(int(child.name[1]) + 1, 6)
                heading_md = render_heading_md(
                    child, level, converter=cls._convert_paragraph_to_md
                )
                if not heading_md:
                    continue
                parts.append(heading_md)
                parts.append("")
                continue

            classes = child.get('class') or []
            role = (child.get('role') or '').strip()

            if child.name == 'div' and role == 'paragraph':
                md = cls._convert_paragraph_to_md(str(child))
                if md:
                    parts.append(md)
                    parts.append("")
                continue

            if child.name == 'div' and 'figure-wrap' in classes:
                # A figure-wrap can hold either a graphic figure or a table
                # figure (<figure id="T1" class="table">). For tables we also
                # render the actual <table> body as a Markdown table.
                inner_figure = child.find('figure')
                fig_classes = (inner_figure.get('class') or []) if inner_figure is not None else []

                label, caption_md, notes_md, _ = cls._figure_caption_md(child)
                if label and caption_md:
                    parts.append(f"**{label}.** {caption_md}")
                elif label:
                    parts.append(f"**{label}.**")
                elif caption_md:
                    parts.append(caption_md)
                if notes_md:
                    parts.append("")
                    parts.append(notes_md)
                parts.append("")

                if inner_figure is not None and 'table' in fig_classes:
                    table_el = inner_figure.find('table')
                    if table_el is not None:
                        table_md = cls._html_table_to_md(table_el)
                        if table_md:
                            parts.append(table_md)
                            parts.append("")
                continue

            if child.name == 'div' and 'display-formula' in classes:
                md = cls._convert_paragraph_to_md(str(child))
                if md:
                    parts.append(md)
                    parts.append("")
                continue

            # Structural wrappers (e.g. <div class="core-container">) — recurse.
            if child.name == 'div':
                cls._walk_sci_body(child, parts, depth_offset)
                continue

            # Generic fallback: convert the fragment.
            md = cls._convert_paragraph_to_md(str(child))
            if md:
                parts.append(md)
                parts.append("")

    @classmethod
    def _html_table_to_md(cls, table_el) -> str:
        """Convert a <table> element to a GitHub-flavored Markdown table.

        Each cell is fed through ``_convert_paragraph_to_md`` so inline
        formatting (math, italics, subscripts) and pipe-escapes are preserved.
        Multi-row headers (``<thead>`` with two ``<tr>``) are collapsed into a
        single header row of column captions, with any ``rowspan="2"`` cell
        repeated by its column header for clarity.
        """
        rows = []

        def cell_md(cell) -> str:
            md = cls._convert_paragraph_to_md(str(cell))
            md = re.sub(r'\s+', ' ', md).strip()
            return md.replace('|', '\\|')

        thead = table_el.find('thead')
        header_cells = []
        if thead is not None:
            # Collect every <th> from any <tr> in the head, in document order.
            # For a two-row head with rowspan/colspan we want the column-level
            # labels (the second row when present), prefixed by the rowspan
            # cell from row 1 only if it spans both header rows.
            head_trs = thead.find_all('tr')
            if len(head_trs) == 1:
                header_cells = [cell_md(c) for c in head_trs[0].find_all(['th', 'td'])]
            elif len(head_trs) >= 2:
                # Row 1: capture any th with rowspan>=2 as left-side group label
                row1 = head_trs[0]
                row2 = head_trs[1]
                row1_left = []
                for th in row1.find_all(['th', 'td']):
                    if int(th.get('rowspan', '1')) >= 2:
                        row1_left.append(cell_md(th))
                row2_cells = [cell_md(c) for c in row2.find_all(['th', 'td'])]
                header_cells = row1_left + row2_cells

        if header_cells:
            rows.append('| ' + ' | '.join(header_cells) + ' |')
            rows.append('|' + '|'.join(['---'] * len(header_cells)) + '|')

        tbody = table_el.find('tbody') or table_el
        for tr in tbody.find_all('tr'):
            if thead is not None and tr.find_parent('thead'):
                continue
            cells = [cell_md(c) for c in tr.find_all(['td', 'th'])]
            if not cells or all(c == '' for c in cells):
                continue
            if header_cells and len(cells) < len(header_cells):
                cells.extend([''] * (len(header_cells) - len(cells)))
            rows.append('| ' + ' | '.join(cells) + ' |')

        if len(rows) <= 1:
            return ''
        return "\n".join(rows)

    @classmethod
    def extract_body_md(cls, html_content: str) -> str:
        if not html_content:
            return ''
        soup = BeautifulSoup(html_content, 'html.parser')
        body = soup.find('section', id='bodymatter')
        if not body:
            return ''
        parts = []
        cls._walk_sci_body(body, parts)
        out = "\n".join(parts).strip()
        return re.sub(r'\n{3,}', '\n\n', out)

    # ------------------------------------------------------------------
    # Figure extraction (for download)
    # ------------------------------------------------------------------

    @classmethod
    def extract_figures_from_html(cls, html_content: str, base_url: str = None) -> dict:
        if not html_content:
            return {}
        base = base_url or cls.SCIENCE_BASE
        soup = BeautifulSoup(html_content, 'html.parser')
        figures = {}
        seen_ids = set()

        for fig_wrap in soup.find_all('div', class_='figure-wrap'):
            figure = fig_wrap.find('figure')
            if figure is None:
                continue
            fig_id = figure.get('id') or ''
            if fig_id and fig_id in seen_ids:
                continue
            if fig_id:
                seen_ids.add(fig_id)

            label, caption_md, _, image_url = cls._figure_caption_md(fig_wrap)
            if not image_url or image_url.startswith('data:'):
                continue
            if not image_url.startswith('http'):
                image_url = urljoin(base + '/', image_url.lstrip('/'))

            key = f"fig_{len(figures) + 1}"
            figures[key] = {
                'url': image_url,
                'caption': caption_md,
                'label': label,
            }
        return figures

    # ------------------------------------------------------------------
    # References
    # ------------------------------------------------------------------

    @classmethod
    def extract_references_from_html(cls, html_content: str) -> list:
        """Return ``[(text_md, crossref_url_or_empty), …]`` for each reference."""
        if not html_content:
            return []
        soup = BeautifulSoup(html_content, 'html.parser')
        bib = soup.find('section', id='bibliography')
        if not bib:
            return []
        out = []
        for item in bib.select('div[role="listitem"]'):
            citation = item.find('div', class_='citation-content')
            if citation is None:
                continue
            text_md = cls._convert_paragraph_to_md(str(citation))
            text_md = re.sub(r'\s+', ' ', text_md).strip()
            crossref_url = ''
            cr_div = item.find('div', class_='core-xlink-crossref')
            if cr_div is not None:
                a = cr_div.find('a', href=True)
                if a is not None:
                    crossref_url = a['href'].strip()
            if text_md:
                out.append((text_md, crossref_url))
        return out

    # ------------------------------------------------------------------
    # Supplementary materials
    # ------------------------------------------------------------------

    @classmethod
    def extract_supplementary_from_html(cls, html_content: str, base_url: str = None) -> tuple:
        """Return ``(urls, descriptions, summary_md)`` for the supplementary section."""
        if not html_content:
            return [], {}, ''

        base = base_url or cls.SCIENCE_BASE
        soup = BeautifulSoup(html_content, 'html.parser')
        supp = soup.find('section', id='supplementary-materials')
        if supp is None:
            return [], {}, ''

        # Summary paragraphs (table of contents of the supplement)
        summary_parts = []
        for sec in supp.find_all('section'):
            for child in sec.children:
                if not hasattr(child, 'name') or not child.name:
                    continue
                if child.name in ('h2', 'h3', 'h4'):
                    text = child.get_text(' ', strip=True)
                    if text and text.lower() != 'summary':
                        summary_parts.append(f"**{text}**")
                        summary_parts.append("")
                elif child.name == 'div' and (child.get('role') or '') == 'paragraph':
                    md = cls._convert_paragraph_to_md(str(child))
                    if md:
                        summary_parts.append(md)

        # Download links — extract by scanning EVERY anchor inside the supp
        # section and classifying it.  Science wraps each downloadable file
        # in a <div class="core-supplementary-material"> that ALSO contains
        # a <div class="core-description"> — and the description text often
        # includes biblio xrefs such as
        #    "References (<a href="#core-collateral-R31"><i>31</i></a>–
        #               <a href="#core-collateral-R35"><i>35</i></a>)"
        # so the previously-used ``item.find('a', href=True)`` returned the
        # first xref instead of the actual ``<a download=… href=/doi/suppl/…>``
        # link sitting in <div class="core-link">.
        #
        # The new logic gathers every <a href> under the supp section, keeps
        # only those that look like real file downloads, and discards the
        # same-page xref / navigation links entirely.
        urls = []
        descriptions = {}

        def _looks_like_file_url(a_tag, href: str) -> bool:
            """True if *a_tag* should be treated as a supplementary file link.

            We accept any anchor that:
              * carries an explicit ``download`` attribute, OR
              * is hosted under Science's supplementary-file path
                (/doi/suppl/…/suppl_file/… or /content/suppl_file/…), OR
              * has a file-style extension (.pdf .zip .docx .xlsx .pptx
                .csv .txt .tsv .mp4 .mov .avi .mkv .gz .tar .7z).
            We reject:
              * empty hrefs and same-page fragments (``#…``), which are the
                R31/R35 xref interference links inside core-description.
              * mailto:/javascript: pseudo-protocols.
            """
            if not href or href.startswith('#'):
                return False
            lo = href.lower()
            if lo.startswith(('mailto:', 'javascript:', 'tel:')):
                return False
            if a_tag.has_attr('download'):
                return True
            if '/suppl_file/' in lo or '/content/suppl_file/' in lo:
                return True
            file_exts = (
                '.pdf', '.zip', '.docx', '.doc', '.xlsx', '.xls', '.pptx',
                '.ppt', '.csv', '.tsv', '.txt', '.mp4', '.mov', '.avi',
                '.mkv', '.webm', '.gz', '.tar', '.7z', '.rar',
            )
            # Strip query string before checking the path's extension.
            path = lo.split('?', 1)[0].split('#', 1)[0]
            if path.endswith(file_exts):
                return True
            return False

        for a in supp.find_all('a', href=True):
            href = a['href'].strip()
            if not _looks_like_file_url(a, href):
                continue
            if not href.startswith('http'):
                href = urljoin(base + '/', href.lstrip('/'))
            if href in descriptions:
                continue

            # Description: prefer the sibling <div class="core-description">
            # inside the same <div class="core-supplementary-material">
            # ancestor, then the download attribute, then anchor text.
            description = ''
            csm_div = a.find_parent('div', class_='core-supplementary-material')
            if csm_div is not None:
                desc_div = csm_div.find('div', class_='core-description')
                if desc_div is not None:
                    description = desc_div.get_text(' ', strip=True)
            if not description:
                description = (a.get('download') or '').strip()
            if not description:
                description = a.get_text(' ', strip=True)
            if not description:
                description = 'Supplementary file'

            urls.append(href)
            descriptions[href] = description

        summary_md = "\n".join(summary_parts).strip()
        return urls, descriptions, summary_md

    # ------------------------------------------------------------------
    # Publisher contract
    # ------------------------------------------------------------------

    async def extract_metadata(self, page) -> dict:
        html_content = ''
        if page is not None:
            try:
                html_content = await page.content()
            except Exception:
                html_content = ''

        meta = self._extract_metadata_from_html_meta(html_content)
        abstract = self.extract_abstract_from_html(html_content)
        if not abstract:
            abstract = meta.get('abstract', '')

        doi_for_pdf = meta.get('doi') or self.doi
        pdf_url = ''
        if doi_for_pdf:
            pdf_url = f"{self.SCIENCE_BASE}/doi/pdf/{doi_for_pdf}?download=true"

        return {
            'title': meta.get('title') or 'Science Article',
            'authors': meta.get('authors', []),
            'author_with_affiliations': [],
            'corresponding_author_emails': [],
            'abstract': abstract,
            'journal': meta.get('journal') or 'Science',
            'publisher': meta.get('publisher') or 'American Association for the Advancement of Science',
            'publication_date': meta.get('publication_date'),
            'doi': meta.get('doi') or self.doi,
            'volume': meta.get('volume', ''),
            'issue': meta.get('issue', ''),
            'pages': meta.get('pages', ''),
            'year': meta.get('year'),
            'references': [],
            '_pdf_url': pdf_url,
        }

    async def get_fulltext_url(self, page) -> str:
        if page is not None:
            try:
                return page.url
            except Exception:
                pass
        return f"https://doi.org/{self.doi}" if self.doi else None

    async def get_pdf_url(self, doi: str) -> str:
        doi = doi or self.doi
        if not doi:
            return None
        return f"{self.SCIENCE_BASE}/doi/pdf/{doi}?download=true"

    async def get_supplemental_url(self, doi: str) -> str:
        return None

    async def extract_references(self, html: str) -> list:
        items = self.extract_references_from_html(html) if html else []
        return [text for text, _ in items]

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'ScienceHandler'
        )
        doi = self.doi

        set_actual_base_url(self, page)

        try:
            # Wait for the article body and references to render.
            try:
                await page.wait_for_selector('section#bodymatter', timeout=15000)
            except Exception:
                pass
            try:
                await page.wait_for_selector('section#bibliography', timeout=10000)
            except Exception:
                pass

            try:
                fulltext_html = await page.content()
            except Exception:
                fulltext_html = ''

            metadata = await self.extract_metadata(page)
            metadata['doi'] = doi or metadata.get('doi') or self.doi
            pdf_url = metadata.pop('_pdf_url', None)

            base_url = getattr(self, 'actual_base_url', None) or self.SCIENCE_BASE

            figures = {}
            supp_urls = []
            supp_descriptions = {}
            supp_summary_md = ''
            ref_tuples = []
            if fulltext_html:
                figures = self.extract_figures_from_html(fulltext_html, base_url=base_url)
                supp_urls, supp_descriptions, supp_summary_md = self.extract_supplementary_from_html(
                    fulltext_html, base_url=base_url
                )
                ref_tuples = self.extract_references_from_html(fulltext_html)

            metadata['references'] = [text for text, _ in ref_tuples]
            metadata['_ref_crossref_urls'] = [url for _, url in ref_tuples]
            metadata['_supp_summary_md'] = supp_summary_md

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figures,
                    'supplemental_urls': supp_urls,
                    'supplemental_descriptions': supp_descriptions,
                },
                'fulltext_data': fulltext_html,
                'journal_name': 'science',
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
    # Markdown generation
    # ------------------------------------------------------------------

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        title = metadata.get('title') or 'Science Article'
        md_parts = [f"# {title}", ""]

        authors = metadata.get('authors', [])
        if authors:
            md_parts.append("**Authors:** " + ", ".join(authors))
            md_parts.append("")

        if metadata.get('doi'):
            md_parts.extend([f"**DOI:** {metadata['doi']}", ""])

        md_parts.extend([
            "## Publication",
            "",
            f"**Journal:** {metadata.get('journal') or 'Science'}",
            "",
        ])
        for field, label in [
            ('volume', 'Volume'),
            ('issue', 'Issue'),
            ('pages', 'Pages'),
            ('publisher', 'Publisher'),
            ('publication_date', 'Published'),
        ]:
            value = metadata.get(field)
            if value:
                md_parts.append(f"**{label}:** {value}")
                md_parts.append("")

        # Abstract — use extracted HTML version if available; fall back to meta.
        abstract_md = ''
        body_md = ''
        if isinstance(article_text, str) and article_text.strip():
            if article_text.lstrip().startswith('<'):
                abstract_md = self.extract_abstract_from_html(article_text)
                body_md = self.extract_body_md(article_text)
            else:
                body_md = article_text.strip()

        if not abstract_md and metadata.get('abstract'):
            abstract_md = metadata['abstract']

        if abstract_md:
            md_parts.extend(["---", "", "## Abstract", "", abstract_md, ""])

        # Embed downloaded figures after their captions using the label.
        if kwargs.get('add_figure_refs') and kwargs.get('figure_filenames'):
            figure_filenames = kwargs['figure_filenames']
            figure_urls = kwargs.get('figure_urls', {}) or {}
            for fig_id, fig_info in figure_urls.items():
                if not isinstance(fig_info, dict):
                    continue
                m = re.search(r'(\d+)$', str(fig_id))
                if not m:
                    continue
                fig_num = m.group(1)
                filename = figure_filenames.get(fig_num)
                if not filename:
                    continue
                label = (fig_info.get('label') or '').strip().rstrip('.')
                if label:
                    label_re = re.escape(label).replace(r'\.', r'\.?')
                    pattern = rf'(\*\*{label_re}[.:]\*\*[^\n]*)'
                    alt_text = label
                else:
                    pattern = rf'(\*\*(?:Fig\.?|Figure)\s*{re.escape(fig_num)}[.:]\*\*[^\n]*)'
                    alt_text = f"Figure {fig_num}"
                body_md = re.sub(pattern, rf'\1\n\n![{alt_text}.]({filename})', body_md)

        md_parts.extend([
            "---",
            "",
            "## Article Text",
            "",
            body_md or "[Article text not found.]",
            "",
        ])

        # Supplemental Material section: summary (table of contents) + downloads.
        supp_summary_md = (metadata.get('_supp_summary_md') or '').strip()
        supplemental_urls = kwargs.get('supplemental_urls', [])
        supplemental_downloads = kwargs.get('supplemental_downloads', [])
        if supp_summary_md or supplemental_urls or supplemental_downloads:
            md_parts.extend(["---", "", "## Supplementary Material", ""])
            if supp_summary_md:
                md_parts.append(supp_summary_md)
                md_parts.append("")
            if supplemental_downloads:
                md_parts.append("**Files:**")
                md_parts.append("")
                for dl in supplemental_downloads:
                    md_parts.append(f"- {dl}")
                md_parts.append("")
            elif supplemental_urls:
                md_parts.append("**Files:**")
                md_parts.append("")
                for url in supplemental_urls:
                    md_parts.append(f"- [{url}]({url})")
                md_parts.append("")

        # References — text + Crossref link + Crossref-derived BibTeX.
        crossref_refs = metadata.get('_crossref_references', [])
        ref_text_list = metadata.get('references', []) or []
        ref_crossref_urls = metadata.get('_ref_crossref_urls', []) or []

        if ref_text_list or crossref_refs:
            md_parts.extend(["---", "", "## References", ""])

        # Build a map from Crossref ref key suffix to ref entry for BibTeX injection.
        crossref_by_index = {}
        for ref in crossref_refs:
            key = ref.get('key', '') or ''
            m = re.search(r'(\d+)$', key)
            if m:
                crossref_by_index[int(m.group(1))] = ref

        for idx, text in enumerate(ref_text_list, 1):
            md_parts.append(f"[{idx}] {text}")
            if idx - 1 < len(ref_crossref_urls):
                url = ref_crossref_urls[idx - 1]
                if url:
                    md_parts.append("")
                    md_parts.append(f"Crossref: [{url}]({url})")
            md_parts.append("")
            ref = crossref_by_index.get(idx)
            if ref is not None:
                ref_key = ref.get('key') or generate_bibtex_key(
                    [ref.get('author', '')] if ref.get('author') else [],
                    str(ref.get('year', '')),
                    ref.get('article-title', ''),
                )
                parts_dict = {
                    'author': ref.get('author', ''),
                    'title': ref.get('article-title', ''),
                    'journal': ref.get('journal-title', ''),
                    'volume': ref.get('volume', ''),
                    'firstpage': ref.get('first-page', ''),
                    'lastpage': ref.get('last-page', ''),
                    'year': str(ref.get('year', '')),
                    'doi': ref.get('DOI', ''),
                }
                parts_dict = {k: v for k, v in parts_dict.items() if v or k == 'doi'}
                if any(parts_dict.get(k) for k in ('author', 'title', 'journal')):
                    bibtex = format_as_bibtex(parts_dict, key=ref_key)
                    md_parts.extend(["```bibtex", bibtex, "```", ""])

        return "\n".join(md_parts)

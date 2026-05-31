"""
Oxford University Press (OUP) handler.

Extracts metadata, body, figures, tables, references, and supplemental materials
from Oxford Academic article pages (academic.oup.com).

OUP renders equations as MathJax CHTML with an ``<mjx-assistive-mml>`` MathML
fallback, so the body extractor walks the MathML to produce LaTeX. Figures are
shipped through OUP's ``/DownloadFile/DownloadImage.aspx`` redirector whose URL
embeds the real CDN link in its ``image=`` query parameter; we strip the
redirector and the per-session ``sec/ar/xsltPath/imagename/siteId`` params to
get a directly downloadable Silverchair CDN URL.
"""

import re
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, unquote

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


# Query parameters in OUP's DownloadImage.aspx redirector that must be stripped
# from the embedded CDN URL — they are session-scoped and break direct download.
_OUP_STRIP_PARAMS = {'sec', 'ar', 'xsltPath', 'imagename', 'siteId'}


class OupHandler(PublisherHandler):
    """Handler for Oxford University Press articles (academic.oup.com)."""

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = 'https://academic.oup.com'

    # ------------------------------------------------------------------
    # Metadata extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_metadata_from_html_meta(html_content: str) -> dict:
        """Extract OUP metadata from citation_* <meta> tags."""
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        meta = {}
        authors = []
        author_institutions = []  # ordered list of [institution, ...] per author

        for tag in soup.find_all('meta'):
            name = tag.get('name', '')
            content = tag.get('content', '')
            if not name or not content:
                continue

            if name == 'citation_author':
                authors.append(content.strip())
                author_institutions.append([])
            elif name == 'citation_author_institution':
                if author_institutions:
                    author_institutions[-1].append(content.strip())
            elif name == 'citation_title':
                meta['title'] = re.sub(r'\s+', ' ', content).strip()
            elif name == 'citation_doi':
                meta['doi'] = content.strip()
            elif name == 'citation_journal_title':
                meta['journal'] = content.strip()
            elif name == 'citation_volume':
                meta['volume'] = content.strip()
            elif name == 'citation_issue':
                meta['issue'] = content.strip()
            elif name == 'citation_firstpage':
                meta['firstpage'] = content.strip()
            elif name == 'citation_lastpage':
                meta['lastpage'] = content.strip()
            elif name == 'citation_publication_date':
                date_str = content.strip()
                meta['publication_date'] = date_str
                year_match = re.search(r'(\d{4})', date_str)
                if year_match:
                    meta['year'] = year_match.group(1)
            elif name == 'citation_pdf_url':
                meta['pdf_url'] = content.strip()

        if authors:
            meta['authors'] = authors
            meta['author_with_affiliations'] = [
                {'author': name, 'affiliations': affs}
                for name, affs in zip(authors, author_institutions)
            ]

        firstpage = meta.pop('firstpage', '')
        lastpage = meta.pop('lastpage', '')
        if firstpage and lastpage:
            meta['pages'] = f"{firstpage}-{lastpage}"
        elif firstpage:
            meta['pages'] = firstpage

        return meta

    # ------------------------------------------------------------------
    # Figure URL normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_download_slide_url(href: str) -> str:
        """Convert OUP's DownloadImage.aspx redirector to a direct CDN URL.

        ``href`` looks like::

            /DownloadFile/DownloadImage.aspx?image=https://oup.silverchair-cdn.com/.../stz656fig1.jpeg?Expires=...&Signature=...&Key-Pair-Id=...&sec=...&ar=...&xsltPath=...&imagename=...&siteId=...

        Strip the redirector prefix, pull out the embedded URL, then drop the
        session-scoped sec/ar/xsltPath/imagename/siteId parameters while
        preserving Expires/Signature/Key-Pair-Id which the CDN requires.
        """
        if not href:
            return ''

        href = unescape(href.strip())

        marker = 'image='
        if 'DownloadImage.aspx' in href and marker in href:
            embedded = href.split(marker, 1)[1]
        else:
            embedded = href

        # The embedded URL itself has a query string. parse_qs needs the part
        # after the *first* '?', but the embedded URL was inlined verbatim so
        # all its '&' params are mixed with the outer redirector's. Split on
        # the first '?' inside the embedded URL.
        if '?' not in embedded:
            return embedded

        base, query = embedded.split('?', 1)
        params = []
        for segment in query.split('&'):
            if '=' not in segment:
                continue
            key, _ = segment.split('=', 1)
            if key in _OUP_STRIP_PARAMS:
                continue
            params.append(segment)

        if not params:
            return base
        return f"{base}?{'&'.join(params)}"

    # ------------------------------------------------------------------
    # Math / paragraph conversion
    # ------------------------------------------------------------------

    @classmethod
    def _strip_inline_math_delimiters(cls, latex: str) -> str:
        latex = (latex or '').strip()
        if latex.startswith('$') and latex.endswith('$') and not latex.startswith('$$'):
            return latex[1:-1].strip()
        return latex

    @classmethod
    def _mathml_to_latex(cls, math_tag, display: bool = False) -> str:
        """Convert one MathML ``<math>`` element to LaTeX via pandoc.

        Strips both ``$...$`` and ``$$...$$`` delimiters pandoc emits so the
        caller can re-wrap consistently.
        """
        latex = mathml_to_latex_pandoc(str(math_tag))
        if not latex:
            return ''
        latex = latex.strip()
        # pandoc emits display math as $$...$$ on display blocks; strip first.
        if latex.startswith('$$') and latex.endswith('$$'):
            latex = latex[2:-2].strip()
        body = cls._strip_inline_math_delimiters(latex)
        return body

    @classmethod
    def _prepare_oup_fragment(cls, html_fragment: str) -> tuple:
        """Collapse OUP MathJax/xref markup to placeholders for HTML→MD pipeline.

        Replaces inline equations and cross-references with sentinel tokens that
        survive pandoc, then returns ``(prepared_html, formulas)``.
        """
        soup = BeautifulSoup(html_fragment, 'html.parser')
        formulas = []

        def stash(latex: str) -> str:
            formulas.append(latex)
            return f"OUPMATH{len(formulas) - 1:03d}MATHEND"

        for tag in soup(['script', 'style', 'noscript']):
            tag.decompose()

        # Drop aria-hidden decorative spans (used by OUP tables to insert
        # visual separators that pandoc otherwise emits as Markdown attrs).
        for span in soup.find_all('span', attrs={'aria-hidden': 'true'}):
            span.decompose()

        # Bibliography cross-refs: <a class="xref-bibr">2013</a> → "[2013]"
        for a_tag in soup.select('a.xref-bibr'):
            text = a_tag.get_text(' ', strip=True)
            if text:
                a_tag.replace_with(NavigableString(text))
            else:
                a_tag.decompose()

        # Figure / table cross-refs: keep the number text only.
        for a_tag in soup.select('a.xref-fig, a.xref-table, a.link.xref-fig, a.link.xref-table'):
            a_tag.replace_with(NavigableString(a_tag.get_text(' ', strip=True)))

        # Jumplink anchor spans next to xrefs (empty, used purely for navigation).
        for span in soup.find_all('span', class_='xrefLink'):
            span.decompose()

        # Inline formulas — MathJax CHTML wrappers with a MathML twin inside
        # <mjx-assistive-mml>. We stash the LaTeX as a placeholder so pandoc
        # doesn't escape the $ delimiters; the caller re-inserts $...$ after
        # the HTML→Markdown pass.
        for inline_formula in soup.find_all('span', class_='inline-formula'):
            math_tag = inline_formula.find('math')
            latex = cls._mathml_to_latex(math_tag) if math_tag else ''
            if latex:
                inline_formula.replace_with(NavigableString(f" {stash(latex)} "))
            else:
                inline_formula.replace_with(
                    NavigableString(inline_formula.get_text(' ', strip=True))
                )

        # Any leftover bare mjx-container that wasn't wrapped in inline-formula.
        for container in soup.find_all('mjx-container'):
            if container.get('display') == 'true':
                # Display equation — leave for the caller to extract whole.
                continue
            math_tag = container.find('math')
            latex = cls._mathml_to_latex(math_tag) if math_tag else ''
            if latex:
                container.replace_with(NavigableString(f" {stash(latex)} "))
            else:
                container.decompose()

        # Anchor wrappers around external links we want to keep as text.
        for a_tag in soup.find_all('a', class_='link-uri'):
            text = a_tag.get_text(' ', strip=True)
            href = a_tag.get('href', '')
            if href and text:
                a_tag.replace_with(NavigableString(f"[{text}]({href})"))
            elif text:
                a_tag.replace_with(NavigableString(text))

        return str(soup), formulas

    @classmethod
    def _convert_oup_fragment_to_md(cls, html_fragment: str) -> str:
        """Convert one HTML fragment (paragraph, list item, caption) to Markdown."""
        if not html_fragment:
            return ''
        prepared, formulas = cls._prepare_oup_fragment(html_fragment)
        md = convert_html_to_markdown(prepared)
        for idx, latex in enumerate(formulas):
            md = md.replace(f"OUPMATH{idx:03d}MATHEND", f"${latex}$")
        md = cleanup_markdown(md)
        md = remove_newlines_in_paragraph(md, "", "p")
        md = re.sub(r'\s+', ' ', md).strip()
        return md

    @classmethod
    def _display_equation_to_md(cls, formula_wrap) -> str:
        """Convert one ``<div class="formula-wrap">`` to a ``$$...$$`` block.

        The wrap may carry a trailing ``<span class="label title-label">(A1)</span>``
        sibling element — we append it as a ``\\tag{A1}`` inside the display math.
        """
        math_tag = formula_wrap.find('math')
        latex = ''
        if math_tag:
            latex = cls._mathml_to_latex(math_tag, display=True)

        if not latex:
            # Fallback: take container alt/text
            container = formula_wrap.find('mjx-container')
            if container:
                latex = container.get_text(' ', strip=True)

        latex = (latex or '').strip()
        if not latex:
            return ''

        label_span = formula_wrap.find('span', class_='label')
        if label_span:
            label_text = label_span.get_text(' ', strip=True)
            label_text = label_text.strip('() ').strip()
            if label_text:
                latex = f"{latex}\\tag{{{label_text}}}"

        return f"\n$$\n{latex}\n$$\n"

    # ------------------------------------------------------------------
    # Figure / table caption helpers
    # ------------------------------------------------------------------

    @classmethod
    def _figure_caption_md(cls, fig_div) -> str:
        """Extract Figure label + caption as one Markdown line."""
        label = ''
        label_div = fig_div.find('div', class_='fig-label')
        if label_div:
            label = re.sub(r'\s+', ' ', label_div.get_text(' ', strip=True)).strip()
            label = label.rstrip('.').strip()

        caption_md = ''
        caption_div = fig_div.find('div', class_='caption')
        if caption_div:
            cap_p = caption_div.find('p', class_='chapter-para') or caption_div.find('p')
            if cap_p:
                caption_md = cls._convert_oup_fragment_to_md(str(cap_p))
            if not caption_md:
                caption_md = re.sub(r'\s+', ' ', caption_div.get_text(' ', strip=True)).strip()

        if label and caption_md:
            return f"**{label}.** {caption_md}"
        if label:
            return f"**{label}**"
        return caption_md

    @classmethod
    def _table_to_md(cls, table_wrap) -> str:
        """Convert one ``<div class="table-full-width-wrap">`` to Markdown.

        Returns the label + caption as a bold heading, then a pipe-table
        rendering of the visible (non-modal) <table>, and finally any
        ``<div class="table-footer">`` notes.
        """
        label = ''
        title_label = table_wrap.find('span', class_='title-label')
        if title_label:
            label = re.sub(r'\s+', ' ', title_label.get_text(' ', strip=True)).strip()
            label = label.rstrip('.').strip()

        caption_md = ''
        # The visible caption lives in the .table-wrap-title block.
        caption_div = table_wrap.find('div', class_='caption')
        if caption_div:
            cap_p = caption_div.find('p', class_='chapter-para') or caption_div.find('p')
            if cap_p:
                caption_md = cls._convert_oup_fragment_to_md(str(cap_p))
            if not caption_md:
                caption_md = re.sub(r'\s+', ' ', caption_div.get_text(' ', strip=True)).strip()

        # Prefer the .table-overflow rendering (it's the on-page version).
        table = None
        overflow = table_wrap.find('div', class_='table-overflow')
        if overflow:
            table = overflow.find('table')
        if table is None:
            table = table_wrap.find('table')
        if table is None:
            return ''

        md_rows = []
        thead = table.find('thead')
        header_cells = []
        if thead:
            for th in thead.find_all('th'):
                header_cells.append(cls._convert_oup_fragment_to_md(str(th)) or '')
        if header_cells:
            md_rows.append('| ' + ' | '.join(header_cells) + ' |')
            md_rows.append('|' + '|'.join(['---'] * len(header_cells)) + '|')

        tbody = table.find('tbody') or table
        for tr in tbody.find_all('tr'):
            if thead and tr.find_parent('thead'):
                continue
            cells = [
                cls._convert_oup_fragment_to_md(str(cell)) or ''
                for cell in tr.find_all(['td', 'th'])
            ]
            if cells and not all(c == '' for c in cells):
                if header_cells and len(cells) < len(header_cells):
                    cells.extend([''] * (len(header_cells) - len(cells)))
                md_rows.append('| ' + ' | '.join(cells) + ' |')

        if not md_rows:
            return ''

        heading_parts = []
        if label:
            heading_parts.append(f"**{label}.**")
        if caption_md:
            heading_parts.append(caption_md)
        heading = ' '.join(heading_parts).strip()

        # Footer / notes (e.g. <div class="table-footer">)
        footer_md = ''
        footer = table_wrap.find('div', class_='table-footer')
        if footer:
            footer_md = cls._convert_oup_fragment_to_md(str(footer))

        lines = []
        if heading:
            lines.extend([heading, ''])
        lines.append('\n'.join(md_rows))
        if footer_md:
            lines.extend(['', footer_md])
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # Body extraction
    # ------------------------------------------------------------------

    @classmethod
    def _list_to_md(cls, list_el) -> str:
        """Convert a ``<ul>`` / ``<ol>`` to a Markdown list, recursing into <p>s."""
        if list_el is None:
            return ''
        ordered = (list_el.name == 'ol')
        out_lines = []
        for idx, li in enumerate(list_el.find_all('li', recursive=False), 1):
            inner_parts = []
            for child in li.children:
                if not getattr(child, 'name', None):
                    text = str(child).strip()
                    if text:
                        inner_parts.append(text)
                    continue
                if child.name == 'p':
                    md = cls._convert_oup_fragment_to_md(str(child))
                    if md:
                        inner_parts.append(md)
                elif child.name in ('ul', 'ol'):
                    sub = cls._list_to_md(child)
                    if sub:
                        # Indent nested list lines under the parent bullet
                        inner_parts.append('\n' + sub)
                else:
                    md = cls._convert_oup_fragment_to_md(str(child))
                    if md:
                        inner_parts.append(md)
            content = ' '.join(p for p in inner_parts if p).strip()
            if not content:
                continue
            marker = f"{idx}." if ordered else '-'
            out_lines.append(f"{marker} {content}")
        return '\n'.join(out_lines)

    @classmethod
    def _block_child_p_to_md(cls, block) -> str:
        """Convert a ``<div class="block-child-p">`` mixed inline + equation block.

        The block can interleave inline text/HTML with one or more
        ``<div class="formula-wrap">`` display equations. We swap each
        formula-wrap for a placeholder, convert the textual surround as a
        paragraph, then re-insert the LaTeX as standalone display blocks.
        """
        equations = []
        # Detach each formula-wrap so it doesn't get swallowed by the paragraph
        # conversion; we'll re-insert it after we know where the placeholder ended up.
        for fw in block.find_all('div', class_='formula-wrap'):
            eq_md = cls._display_equation_to_md(fw)
            placeholder_token = f"@@OUPEQ{len(equations)}@@"
            equations.append((placeholder_token, eq_md))
            fw.replace_with(NavigableString(f" {placeholder_token} "))

        text_md = cls._convert_oup_fragment_to_md(str(block))

        for token, eq_md in equations:
            replacement = f"\n\n{eq_md.strip()}\n\n"
            text_md = text_md.replace(token, replacement)

        text_md = re.sub(r'\n{3,}', '\n\n', text_md).strip()
        return text_md

    @classmethod
    def extract_article_text_from_html(cls, html_content: str):
        """Extract abstract + body, returning ``(abstract_md, body_md)``."""
        if not html_content:
            return '', ''

        soup = BeautifulSoup(html_content, 'html.parser')

        fulltext = soup.find('div', attrs={'data-widgetname': 'ArticleFulltext'})
        if fulltext is None:
            return '', ''

        abstract_md = ''
        body_parts = []

        # Sections we don't want to fold into the main body — they get their own
        # markdown sections later in convert_to_markdown.
        SKIP_HEADINGS = {'supporting information', 'supplementary data',
                         'acknowledgements', 'acknowledgments', 'references'}
        in_skipped_section = False

        # We'll process direct children of the fulltext widget container.
        # OUP wraps everything at the same depth so this is enough to capture
        # headings + sibling paragraphs / figures / equations / tables / lists.
        for child in fulltext.children:
            if not getattr(child, 'name', None):
                continue

            tag = child.name

            if tag in ('h2', 'h3', 'h4'):
                heading_text = child.get_text(' ', strip=True)
                heading_text = re.sub(r'\s+', ' ', heading_text or '').strip()
                if not heading_text:
                    continue
                # Stop accumulating body content once we hit a tail section.
                if heading_text.lower() in SKIP_HEADINGS:
                    in_skipped_section = True
                    continue
                in_skipped_section = False
                if 'abstract-title' in (child.get('class') or []):
                    continue
                level = '#' * (int(tag[1]) + 1)
                body_parts.extend([f"{level} {heading_text}", ''])
                continue

            if in_skipped_section:
                continue

            if tag == 'section' and 'abstract' in (child.get('class') or []):
                paragraphs = []
                for p in child.find_all('p', class_='chapter-para'):
                    md = cls._convert_oup_fragment_to_md(str(p))
                    if md:
                        paragraphs.append(md)
                abstract_md = '\n\n'.join(paragraphs)
                continue

            if tag == 'p' and 'chapter-para' in (child.get('class') or []):
                md = cls._convert_oup_fragment_to_md(str(child))
                if md:
                    body_parts.extend([md, ''])
                continue

            if tag in ('ul', 'ol'):
                list_md = cls._list_to_md(child)
                if list_md:
                    body_parts.extend([list_md, ''])
                continue

            if tag == 'div':
                classes = child.get('class') or []

                if 'formula-wrap' in classes:
                    eq_md = cls._display_equation_to_md(child)
                    if eq_md:
                        body_parts.extend([eq_md, ''])
                    continue

                if 'block-child-p' in classes:
                    block_md = cls._block_child_p_to_md(child)
                    if block_md:
                        body_parts.extend([block_md, ''])
                    continue

                if 'table-full-width-wrap' in classes or child.get('data-content-id', '').startswith('tbl'):
                    tbl_md = cls._table_to_md(child)
                    if tbl_md:
                        body_parts.extend([tbl_md, ''])
                    continue

                # Figure containers: match by the fig-section class so the
                # journal-specific data-content-id naming (fig1 vs pts067f1)
                # doesn't matter.
                if any(cls._FIG_CLASS_RE.search(c) for c in classes):
                    caption_md = cls._figure_caption_md(child)
                    if caption_md:
                        body_parts.extend([caption_md, ''])
                    continue

                # Article-metadata-panel, keyword groups, dataSuppLink etc — skip
                continue

            # Skip anchors, scripts, widgets, etc.

        # Collapse consecutive blank lines.
        body_md = '\n'.join(body_parts).strip()
        body_md = re.sub(r'\n{3,}', '\n\n', body_md)

        return abstract_md, body_md

    # ------------------------------------------------------------------
    # Figure extraction
    # ------------------------------------------------------------------

    # Figure container regex: matches both <div class="fig fig-section ..."> and
    # the legacy <div class="fig js-fig-section">. We intentionally use class
    # rather than data-content-id because OUP's figure IDs vary by journal:
    #   * mnras/stz656     → data-content-id="fig1"
    #   * ptep/pts067      → data-content-id="pts067f1"
    _FIG_CLASS_RE = re.compile(r'\bfig-section\b')

    @classmethod
    def _iter_figure_divs(cls, root):
        """Yield every figure container under *root* in document order."""
        for fig_div in root.find_all('div', class_=cls._FIG_CLASS_RE):
            yield fig_div

    @classmethod
    def extract_figures_from_html(cls, html_content: str) -> dict:
        """Extract figure URLs and captions from the article HTML.

        Each ``<div class="fig fig-section ...">`` is matched. The
        high-resolution download link lives in ``<a class="download-slide">``
        and goes through OUP's DownloadImage.aspx redirector — we unwrap it
        to a direct CDN URL.
        """
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        figures = {}
        seen_keys = set()
        fallback_idx = 0

        for fig_div in cls._iter_figure_divs(soup):
            # Figure id may be "fig1" (mnras) or "pts067f1" (ptep) — take the
            # trailing digit run as the figure number.
            fig_id = fig_div.get('data-content-id') or fig_div.get('data-id') or ''
            num_match = re.search(r'(\d+)$', fig_id)
            if num_match:
                fig_num = num_match.group(1)
            else:
                fallback_idx += 1
                fig_num = str(fallback_idx)

            key = f"fig_{fig_num}"
            if key in seen_keys:
                continue

            download_link = fig_div.find('a', class_='download-slide')
            img_url = ''
            if download_link:
                img_url = cls._clean_download_slide_url(download_link.get('href', ''))

            if not img_url:
                # Fallback to the thumbnail <img> src.
                img = fig_div.find('img', class_='content-image')
                if img:
                    img_url = (img.get('src') or '').strip()

            if not img_url:
                continue

            seen_keys.add(key)
            figures[key] = {
                'url': img_url,
                'caption': cls._figure_caption_md(fig_div),
            }

        return figures

    # ------------------------------------------------------------------
    # References
    # ------------------------------------------------------------------

    @classmethod
    def extract_references_from_html(cls, html_content: str) -> tuple:
        """Return ``(text_refs, raw_dois)``.

        ``text_refs`` is a list of strings rendered to match what the web page
        shows for each entry. ``raw_dois`` is a parallel list with each ref's
        DOI (or '') so we can look it up in Crossref data.

        OUP journals use different per-ref id schemes:
          * mnras: ``<div content-id="bib1" class="js-splitview-ref-item">``
            wrapping ``<div id="ref-auto-bib1" class="ref-content">``.
          * ptep:  ``<div content-id="PTS067C1" class="js-splitview-ref-item">``
            wrapping ``<div id="ref-auto-PTS067C1" class="ref-content">``
            with a leading ``<span class="label title-label">1</span>``.

        Selecting on the shared ``js-splitview-ref-item`` class covers both.
        """
        if not html_content:
            return [], []

        soup = BeautifulSoup(html_content, 'html.parser')
        text_refs = []
        raw_dois = []

        for ref_item in soup.find_all('div', class_='js-splitview-ref-item'):
            ref_content = ref_item.find('div', class_='ref-content')
            if ref_content is None:
                continue

            # Some journals (ptep) inline the citation; others (mnras) wrap it
            # in <div class="mixed-citation">. Either way, work off ref_content
            # so we always see the leading label span if present.
            decoration_copy = BeautifulSoup(str(ref_content), 'html.parser')
            # Hide the citation-links "Crossref"/"Search ADS"/"Find in my
            # library" decorations and pub-id DOI badges (we'll re-add the
            # bibtex block from Crossref separately).
            for trash in decoration_copy.find_all('div', class_='citation-links'):
                trash.decompose()

            text = decoration_copy.get_text(' ', strip=True)
            # Collapse spaces before punctuation and excess whitespace.
            text = re.sub(r'\s+([,.;:])', r'\1', text)
            text = re.sub(r'\s+', ' ', text).strip()
            # Strip the leading "N " label if the ref content carried one
            # (ptep). The markdown emitter prefixes each entry with [N] itself.
            text = re.sub(r'^\d+\s+', '', text)
            text = text.rstrip(' ,')

            doi = ''
            pub_id = ref_content.find('div', class_='pub-id')
            if pub_id:
                doi_link = pub_id.find('a')
                if doi_link:
                    doi = (doi_link.get_text(' ', strip=True)
                           or doi_link.get('href', '').rsplit('/', 1)[-1]).strip()

            text_refs.append(text)
            raw_dois.append(doi)

        return text_refs, raw_dois

    # ------------------------------------------------------------------
    # Footnotes
    # ------------------------------------------------------------------

    @classmethod
    def extract_footnotes_from_html(cls, html_content: str) -> list:
        """Extract endnote-style footnotes from the article HTML.

        OUP wraps each footnote in ``<div content-id="FN{N}" class="footnote">``
        with the number in ``<span class="end-note-link">`` and the body in
        ``<p class="footnote-compatibility">``. The body may contain multiple
        ``<p>`` paragraphs as well as inline math / xref-bibr links, so we
        run each paragraph through the OUP fragment pipeline.

        Returns a list of strings shaped ``"{N}. {text}"``.
        """
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        footnotes = []

        for fn_div in soup.find_all('div', class_='footnote', attrs={'content-id': re.compile(r'^FN\d+$', re.IGNORECASE)}):
            label_span = fn_div.find('span', class_='end-note-link')
            if label_span:
                label = label_span.get_text(' ', strip=True)
            else:
                # Fallback: take the digits from content-id (e.g. "FN3" → "3")
                cid = fn_div.get('content-id', '')
                m = re.search(r'(\d+)$', cid)
                label = m.group(1) if m else ''

            content_div = fn_div.find('div', class_='footnote-content')
            if content_div is None:
                continue

            paragraphs = []
            for p in content_div.find_all('p'):
                md = cls._convert_oup_fragment_to_md(str(p))
                if md:
                    paragraphs.append(md)

            if not paragraphs:
                # Fallback: take whatever text the content div has.
                text = re.sub(r'\s+', ' ', content_div.get_text(' ', strip=True)).strip()
                if text:
                    paragraphs.append(text)

            if not paragraphs:
                continue

            body_md = ' '.join(paragraphs).strip()
            prefix = f"{label}. " if label else ''
            footnotes.append(f"{prefix}{body_md}")

        return footnotes

    # ------------------------------------------------------------------
    # Supplemental materials
    # ------------------------------------------------------------------

    @classmethod
    def extract_supplemental_from_html(cls, html_content: str) -> tuple:
        """Return ``(urls, descriptions)`` for OUP supplemental files.

        Two sources:
        1. ``<div class="dataSuppLink">`` — the actual download links.
        2. SUPPORTING INFORMATION / Supplementary data ``<h2>`` sibling
           ``<p>`` elements, used as the file's description.
        """
        if not html_content:
            return [], {}

        soup = BeautifulSoup(html_content, 'html.parser')
        urls = []
        descriptions = {}

        # Walk the SUPPORTING INFORMATION section to harvest descriptions
        # (typically a "<p><strong>filename.ext</strong></p>" line).
        section_text = ''
        for h2 in soup.find_all('h2'):
            heading = (h2.get_text(' ', strip=True) or '').lower().strip()
            if heading in ('supporting information', 'supplementary data',
                           'supplemental data', 'supplementary material',
                           'supplemental material'):
                texts = []
                for sib in h2.next_siblings:
                    if getattr(sib, 'name', None) == 'h2':
                        break
                    if getattr(sib, 'name', None) == 'p':
                        line = sib.get_text(' ', strip=True)
                        if line:
                            texts.append(line)
                if texts:
                    section_text = ' '.join(texts).strip()
                break

        # The real download URLs live in <div class="dataSuppLink">.
        for link_div in soup.find_all('div', class_='dataSuppLink'):
            for a_tag in link_div.find_all('a', href=True):
                href = a_tag['href'].strip()
                if not href or href.startswith('#'):
                    continue
                urls.append(href)
                label = a_tag.get_text(' ', strip=True)
                # Prefer the SUPPORTING INFORMATION description over the
                # generic "Supplemental File" anchor label.
                descriptions[href] = section_text or label

        return urls, descriptions

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

        return {
            'title': meta.get('title') or 'Oxford Academic Article',
            'authors': meta.get('authors', []),
            'author_with_affiliations': meta.get('author_with_affiliations', []),
            'corresponding_author_emails': [],
            'abstract': '',  # filled in by extract_all from the body extractor
            'journal': meta.get('journal') or 'Oxford Academic',
            'publication_date': meta.get('publication_date'),
            'doi': meta.get('doi') or self.doi,
            'volume': meta.get('volume'),
            'issue': meta.get('issue'),
            'pages': meta.get('pages'),
            'year': meta.get('year'),
            'references': [],
            '_pdf_url': meta.get('pdf_url'),
        }

    async def get_fulltext_url(self, page) -> str:
        if page is not None:
            try:
                return page.url
            except Exception:
                pass
        return f"https://doi.org/{self.doi}" if self.doi else None

    async def get_pdf_url(self, doi: str) -> str:
        return None

    async def get_supplemental_url(self, doi: str) -> str:
        return None

    async def extract_references(self, html: str) -> list:
        if not html:
            return []
        refs, _ = self.extract_references_from_html(html)
        return refs

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        """Run the OUP handler through the unified publisher contract."""
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'OupHandler'
        )
        doi = self.doi

        set_actual_base_url(self, page)

        try:
            metadata = await self.extract_metadata(page)
            metadata['doi'] = doi

            pdf_url = metadata.pop('_pdf_url', None)

            try:
                fulltext_html = await page.content()
            except Exception:
                fulltext_html = ''

            if fulltext_html:
                abstract_md, _ = self.extract_article_text_from_html(fulltext_html)
                if abstract_md:
                    metadata['abstract'] = abstract_md

                text_refs, raw_dois = self.extract_references_from_html(fulltext_html)
                metadata['references'] = text_refs
                metadata['_ref_dois'] = raw_dois

                metadata['footnotes'] = self.extract_footnotes_from_html(fulltext_html)

            # Resolve PDF URL — citation_pdf_url meta first, otherwise look up
            # the <a class="al-link pdf article-pdfLink"> on the page.
            if not pdf_url and fulltext_html:
                soup = BeautifulSoup(fulltext_html, 'html.parser')
                pdf_anchor = soup.find('a', class_=re.compile(r'article-pdfLink'))
                if pdf_anchor and pdf_anchor.get('href'):
                    href = pdf_anchor['href'].strip()
                    if not href.startswith('http'):
                        href = urljoin(self.actual_base_url, href)
                    pdf_url = href

            figure_urls = {}
            supp_urls = []
            supp_descriptions = {}
            if fulltext_html:
                figure_urls = self.extract_figures_from_html(fulltext_html)
                supp_urls, supp_descriptions = self.extract_supplemental_from_html(fulltext_html)

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figure_urls,
                    'supplemental_urls': supp_urls,
                    'supplemental_descriptions': supp_descriptions,
                },
                'fulltext_data': fulltext_html,
                'journal_name': 'oup',
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

    def _build_supporting_info_section(self, html_content: str) -> str:
        """Extract the SUPPORTING INFORMATION / Supplementary data narrative."""
        if not html_content:
            return ''
        soup = BeautifulSoup(html_content, 'html.parser')
        for h2 in soup.find_all('h2'):
            heading = (h2.get_text(' ', strip=True) or '').lower().strip()
            if heading not in ('supporting information', 'supplementary data',
                               'supplemental data', 'supplementary material',
                               'supplemental material'):
                continue
            paragraphs = []
            for sib in h2.next_siblings:
                if getattr(sib, 'name', None) == 'h2':
                    break
                if getattr(sib, 'name', None) == 'p':
                    md = self._convert_oup_fragment_to_md(str(sib))
                    if md:
                        paragraphs.append(md)
            return '\n\n'.join(paragraphs).strip()
        return ''

    def _build_acknowledgements_section(self, html_content: str) -> str:
        if not html_content:
            return ''
        soup = BeautifulSoup(html_content, 'html.parser')
        for h2 in soup.find_all('h2'):
            heading = (h2.get_text(' ', strip=True) or '').lower().strip()
            if heading not in ('acknowledgements', 'acknowledgments'):
                continue
            paragraphs = []
            for sib in h2.next_siblings:
                if getattr(sib, 'name', None) == 'h2':
                    break
                if getattr(sib, 'name', None) == 'p':
                    md = self._convert_oup_fragment_to_md(str(sib))
                    if md:
                        paragraphs.append(md)
            return '\n\n'.join(paragraphs).strip()
        return ''

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        """Generate the full Markdown document for an OUP article."""
        title = metadata.get('title') or 'Oxford Academic Article'
        md_parts = [f"# {title}", '']

        # Authors + affiliations
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
            for author in metadata['authors']:
                md_parts.append(f"- {author}")
            md_parts.append('')

        # Publication block
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

        # Abstract
        if metadata.get('abstract'):
            md_parts.extend(['## Abstract', '', metadata['abstract'], '', '---', ''])

        # Body
        body_md = ''
        if isinstance(article_text, str) and article_text.strip():
            if article_text.lstrip().startswith('<'):
                _, body_md = self.extract_article_text_from_html(article_text)
            else:
                body_md = article_text.strip()

        # Replace figure-caption headings with the downloaded image afterward.
        # OUP journals vary the label text: mnras uses "Figure 1.", ptep uses
        # "Fig. 1.". Match both forms (case-insensitive) so the image lands
        # right after whichever caption the publisher emitted.
        figure_filenames = kwargs.get('figure_filenames') or {}
        if kwargs.get('add_figure_refs') and figure_filenames and body_md:
            for fig_num, filename in sorted(figure_filenames.items(),
                                            key=lambda x: int(x[0])):
                body_md = re.sub(
                    rf'(\*\*(?:Figure|Fig\.?)\s*{re.escape(fig_num)}\.\*\*[^\n]*)',
                    rf'\1\n\n![Figure {fig_num}]({filename})',
                    body_md,
                    flags=re.IGNORECASE,
                )

        if body_md:
            md_parts.extend(['## Article Text', '', body_md, ''])
        else:
            md_parts.extend(['## Article Text', '', '[Article text not found.]', ''])

        # Acknowledgements (own section, extracted from raw HTML)
        ack_md = self._build_acknowledgements_section(article_text) if isinstance(article_text, str) else ''
        if ack_md:
            md_parts.extend(['---', '', '## Acknowledgements', '', ack_md, ''])

        # Supporting information narrative + downloads
        info_md = self._build_supporting_info_section(article_text) if isinstance(article_text, str) else ''
        supplemental_urls = kwargs.get('supplemental_urls') or []
        supplemental_descriptions = kwargs.get('supplemental_descriptions') or {}
        supplemental_downloads = kwargs.get('supplemental_downloads') or []

        if info_md or supplemental_urls or supplemental_downloads:
            md_parts.extend(['---', '', '## Supporting Information', ''])
            if info_md:
                md_parts.extend([info_md, ''])
            if supplemental_downloads:
                for dl in supplemental_downloads:
                    md_parts.append(f"- {dl}")
                md_parts.append('')
            elif supplemental_urls:
                for url in supplemental_urls:
                    label = supplemental_descriptions.get(url, '') or url
                    md_parts.append(f"- [{label}]({url})")
                md_parts.append('')

        # Footnotes — render before References so cross-references in the
        # body (e.g. "[1]" superscripts) still resolve in linear reading order.
        footnotes = metadata.get('footnotes') or []
        if footnotes:
            md_parts.extend(['---', '', '## Footnotes', ''])
            for fn in footnotes:
                md_parts.extend([fn, ''])

        # References — text rendering + BibTeX block per entry.
        text_refs = metadata.get('references', []) or []
        raw_dois = metadata.get('_ref_dois', []) or []
        crossref_refs = metadata.get('_crossref_references', []) or []

        # Build a DOI → crossref entry index for matching.
        crossref_by_doi = {}
        for cr in crossref_refs:
            doi = (cr.get('DOI') or '').strip().lower()
            if doi:
                crossref_by_doi[doi] = cr

        if text_refs or crossref_refs:
            md_parts.extend(['---', '', '## References', ''])

            if text_refs:
                # Render each text reference, attaching a matching BibTeX block
                # built from Crossref data (year + DOI only, per spec).
                for idx, text in enumerate(text_refs, 1):
                    md_parts.extend([f"[{idx}] {text}", ''])

                    doi = (raw_dois[idx - 1] if idx - 1 < len(raw_dois) else '').strip()
                    cr = crossref_by_doi.get(doi.lower()) if doi else None

                    if cr or doi:
                        parts = {}
                        if cr:
                            parts['year'] = str(cr.get('year', '')).strip()
                            parts['doi'] = (cr.get('DOI') or doi).strip()
                        else:
                            # Try extracting a year from the text rendering.
                            year_match = re.search(r'(\b(?:18|19|20)\d{2}\b)', text)
                            if year_match:
                                parts['year'] = year_match.group(1)
                            parts['doi'] = doi

                        parts = {k: v for k, v in parts.items() if v}
                        if parts:
                            key = f"bib{idx}"
                            bibtex = format_as_bibtex(parts, key=key)
                            md_parts.extend(['```bibtex', bibtex, '```', ''])
            else:
                # No on-page reference text — fall back to Crossref-only rendering.
                for idx, cr in enumerate(crossref_refs, 1):
                    unstructured = cr.get('unstructured', '')
                    if unstructured:
                        md_parts.extend([f"[{idx}] {unstructured}", ''])
                    else:
                        md_parts.extend([generate_reference_text_from_crossref(cr, index=idx), ''])

                    parts = {
                        'year': str(cr.get('year', '')).strip(),
                        'doi': (cr.get('DOI') or '').strip(),
                    }
                    parts = {k: v for k, v in parts.items() if v}
                    if parts:
                        key = cr.get('key', f"bib{idx}")
                        bibtex = format_as_bibtex(parts, key=key)
                        md_parts.extend(['```bibtex', bibtex, '```', ''])

        return '\n'.join(md_parts)

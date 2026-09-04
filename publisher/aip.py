"""
AIP Publishing handler skeleton.

This module wires AIP into the shared PublisherHandler contract. Detailed
body/reference extraction for pubs.aip.org is intentionally left for a future
pass.
"""

import re

from bs4 import BeautifulSoup, NavigableString
from playwright.async_api import async_playwright

from html_to_md_converter import (
    cleanup_markdown,
    convert_html_to_markdown,
    mathml_to_latex_pandoc,
    remove_newlines_in_paragraph,
)
from core.utilities import (
    fetch_crossref,
    fetch_semanticscholar,
    _build_bibtex_from_s2,
    _build_bibtex_from_crossref,
)
from publisher.base import PublisherHandler
from publisher.wildcard import set_actual_base_url, init_extract_all_page


class AIPHandler(PublisherHandler):
    """Handler interface for AIP Publishing articles."""

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)

    @staticmethod
    def _extract_metadata_from_html_meta(html_content: str) -> dict:
        """Extract AIP metadata from citation_* <meta> tags in the HTML head."""
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        meta = {}
        authors = []
        author_institutions = []

        for tag in soup.find_all('meta'):
            name = tag.get('name', '')
            content = tag.get('content', '')
            if not name or not content:
                continue

            if name == 'citation_author':
                authors.append(content.strip())
            elif name == 'citation_author_institution':
                author_institutions.append(content.strip())
            elif name == 'citation_title':
                meta['title'] = content.strip()
            elif name == 'citation_doi':
                meta['doi'] = content.strip()
            elif name == 'citation_journal_title':
                meta['journal'] = content.strip()
            elif name == 'citation_volume':
                meta['volume'] = content.strip()
            elif name == 'citation_issue':
                meta['issue'] = content.strip()
            elif name == 'citation_publication_date':
                date_str = content.strip()
                meta['publication_date'] = date_str
                if date_str and '/' in date_str:
                    meta['year'] = date_str.split('/')[0]
            elif name == 'citation_pdf_url':
                meta['pdf_url'] = content.strip()

        if authors:
            meta['authors'] = authors

        author_with_affiliations = []
        for i, author in enumerate(authors):
            aff_list = []
            if i < len(author_institutions):
                aff_list = [author_institutions[i]]
            author_with_affiliations.append({
                'author': author,
                'affiliations': aff_list,
            })
        if author_with_affiliations:
            meta['author_with_affiliations'] = author_with_affiliations

        return meta

    async def extract_metadata(self, page) -> dict:
        """Return metadata from HTML citation meta tags, with abstract from page body."""
        html_content = ''
        if page is not None:
            try:
                html_content = await page.content()
            except Exception:
                html_content = ''

        meta = self._extract_metadata_from_html_meta(html_content)

        abstract = ''
        if html_content:
            try:
                abstract = self.extract_main_abstract_from_html(html_content)
            except Exception:
                abstract = ''

        return {
            'title': meta.get('title') or 'AIP Article',
            'authors': meta.get('authors', []),
            'author_with_affiliations': meta.get('author_with_affiliations', []),
            'corresponding_author_emails': [],
            'abstract': abstract,
            'journal': meta.get('journal') or 'AIP Publishing',
            'publication_date': meta.get('publication_date'),
            'doi': meta.get('doi') or self.doi,
            'volume': meta.get('volume'),
            'issue': meta.get('issue'),
            'pages': None,
            'year': meta.get('year'),
            'references': [],
            '_pdf_url': meta.get('pdf_url'),
        }

    @classmethod
    def extract_main_abstract_from_html(cls, html_content: str) -> str:
        """Extract AIP Main abstract as Markdown (convenience wrapper)."""
        abstract_md, _ = cls.extract_article_text_from_html(html_content)
        return abstract_md

    @staticmethod
    def _strip_inline_math_delimiters(latex: str) -> str:
        """Return the body of a single inline math expression."""
        latex = (latex or '').strip()
        if latex.startswith('$') and latex.endswith('$') and not latex.startswith('$$'):
            return latex[1:-1].strip()
        return latex

    @classmethod
    def _convert_aip_mathml(cls, math_tag, display: bool = False) -> str:
        latex = mathml_to_latex_pandoc(str(math_tag))
        if not latex:
            return ''

        if display:
            latex_body = cls._strip_inline_math_delimiters(latex)
            return f"$$\n{latex_body}\n$$"
        return latex

    @classmethod
    def _prepare_aip_html_fragment(cls, html_fragment: str) -> tuple[str, list[str]]:
        """Collapse AIP MathJax markup to placeholders before pandoc."""
        soup = BeautifulSoup(html_fragment, 'html.parser')
        formulas = []

        def stash_formula(latex: str) -> str:
            formulas.append(latex)
            return f"AIPMATH{len(formulas) - 1:03d}MATHEND"

        for tag in soup(['script', 'style', 'noscript']):
            tag.decompose()

        # Collapse xref-bibr links to plain [number] brackets.
        for a_tag in soup.select('a.xref-bibr'):
            sup_tag = a_tag.find('sup')
            ref_text = sup_tag.get_text(' ', strip=True) if sup_tag else a_tag.get_text(' ', strip=True)
            if ref_text:
                a_tag.replace_with(NavigableString(f"[{ref_text}]"))

        # Inline formulas are rendered as large MathJax CHTML trees with
        # assistive MathML. Keep only the MathML-derived LaTeX.
        for formula in soup.select('span.inline-formula'):
            math_tag = formula.find('math')
            latex = cls._convert_aip_mathml(math_tag) if math_tag else ''
            if latex:
                formula.replace_with(NavigableString(f" {stash_formula(latex)} "))

        for container in soup.find_all('mjx-container'):
            math_tag = container.find('math')
            latex = cls._convert_aip_mathml(math_tag) if math_tag else ''
            if latex:
                container.replace_with(NavigableString(f" {stash_formula(latex)} "))

        return str(soup), formulas

    @classmethod
    def _convert_aip_html_fragment_to_markdown(cls, html_fragment: str) -> str:
        prepared_html, formulas = cls._prepare_aip_html_fragment(html_fragment)
        md = convert_html_to_markdown(prepared_html)
        for index, latex in enumerate(formulas):
            md = md.replace(f"AIPMATH{index:03d}MATHEND", latex)
        md = cleanup_markdown(md)
        md = remove_newlines_in_paragraph(md, "", "p")
        md = re.sub(r'\s+', ' ', md).strip()
        return md

    @classmethod
    def _convert_aip_display_formula(cls, wrapper) -> str:
        math_tag = wrapper.find('math')
        if not math_tag:
            return ''

        md = cls._convert_aip_mathml(math_tag, display=True)
        label = wrapper.find(class_='label')
        if label:
            label_text = label.get_text(' ', strip=True)
            if label_text:
                md = f"{md}\n\n{label_text}"
        return md.strip()

    @classmethod
    def _convert_aip_figure(cls, wrapper) -> str:
        fig = wrapper.select_one('div.fig-section')
        if not fig:
            return ''

        label = fig.select_one('.fig-label')
        caption = fig.select_one('.caption')
        parts = []
        if label:
            label_text = label.get_text(' ', strip=True)
            if label_text:
                parts.append(f"**{label_text}**")
        if caption:
            caption_md = cls._convert_aip_html_fragment_to_markdown(str(caption))
            if caption_md:
                parts.append(caption_md)
        return " ".join(parts).strip()

    @classmethod
    def _convert_aip_table_to_md(cls, table_wrap) -> str:
        """Convert an AIP ``div.table-wrap`` element to markdown table.

        Pre-processes MathJax ``mjx-container`` markup in table cells before
        converting the ``<table>`` element to markdown via pandoc.
        """
        # Label and caption
        label_span = table_wrap.find('span', class_='label')
        label = label_span.get_text(' ', strip=True) if label_span else ''

        caption_div = table_wrap.find('div', class_='caption')
        caption = caption_div.get_text(' ', strip=True) if caption_div else ''

        table_overflow = table_wrap.find('div', class_='table-overflow')
        if not table_overflow:
            return ''
        table = table_overflow.find('table')
        if not table:
            return ''

        # Stash mjx-container formulas as LaTeX placeholders
        formulas = []
        for mjx in table.find_all('mjx-container'):
            math_tag = mjx.find('math')
            latex = cls._convert_aip_mathml(math_tag) if math_tag else ''
            if latex:
                placeholder = f"AIPMATH{len(formulas):03d}MATHEND"
                formulas.append(latex)
                mjx.replace_with(placeholder)

        # Remove scripts, styles, noscripts
        for tag in table(['script', 'style', 'noscript']):
            tag.decompose()

        # Convert xref-bibr links to plain [N] markers
        for a_tag in table.find_all('a', class_='xref-bibr'):
            sup = a_tag.find('sup')
            ref_text = sup.get_text(' ', strip=True) if sup else a_tag.get_text(' ', strip=True)
            if ref_text:
                a_tag.replace_with(f"[{ref_text}]")

        # Unwrap inline-formula spans (keep inner content)
        for span in table.find_all('span', class_='inline-formula'):
            span.unwrap()

        # Remove empty spans (e.g. <span class="mathFormula"></span>)
        for tag in table.find_all(lambda t: t.name == 'span' and not t.get_text(strip=True)):
            tag.decompose()

        # Convert <table> to markdown via pandoc
        import pypandoc
        table_html = str(table)
        md = pypandoc.convert_text(table_html, 'md', format='html', extra_args=['--wrap=none'])
        md = md.strip()

        # Restore LaTeX placeholders
        for idx, latex in enumerate(formulas):
            md = md.replace(f"AIPMATH{idx:03d}MATHEND", latex)

        # Extract table footnotes from div.table-wrap-foot
        foot_div = table_wrap.find('div', class_='table-wrap-foot')
        footnote_lines = []
        if foot_div:
            for fn in foot_div.find_all('div', class_='fn'):
                label_tag = fn.find('span', class_='fn-label')
                fn_label = label_tag.get_text(strip=True) if label_tag else ''
                p_tag = fn.find('p')
                if p_tag:
                    fn_md = cls._convert_aip_html_fragment_to_markdown(str(p_tag))
                    if fn_md:
                        prefix = f"^{fn_label}^ " if fn_label else ''
                        footnote_lines.append(f"{prefix}{fn_md}")

        # Combine header + table + footnotes
        header = f"**{label}** {caption}" if label else f"**{caption}**"
        result = f"\n{header}\n\n{md}\n"
        if footnote_lines:
            result += "\n" + "\n\n".join(footnote_lines) + "\n"
        return result

    @classmethod
    def _convert_aip_block_child_p(cls, block_div) -> str:
        """Convert AIP ``div.block-child-p`` to markdown.

        This element acts like a paragraph but can contain embedded
        ``div.formula-wrap`` display formulas and inline MathJax.
        Extract both and combine them in order.
        """
        # Extract display formulas and replace with placeholders
        display_formulas = []
        for fw in block_div.find_all('div', class_='formula-wrap'):
            formula_md = cls._convert_aip_display_formula(fw)
            if formula_md:
                placeholder = f"AIPDISPF{len(display_formulas):03d}MATHEND"
                display_formulas.append((placeholder, formula_md))
                fw.replace_with(placeholder)

        # Convert remaining content (text + inline MathJax) to markdown
        text_md = cls._convert_aip_html_fragment_to_markdown(str(block_div))

        # Restore display formulas (each on its own line)
        for placeholder, formula_md in display_formulas:
            text_md = text_md.replace(placeholder, f"\n\n{formula_md}")

        return text_md.strip()

    @classmethod
    def _walk_aip_section(cls, section, body_parts: list) -> None:
        """Walk direct children of an article-section-wrapper in document order.

        Older AIP renderings mix ``<p>``, a multi-``<div class="formula-wrap">``
        ``<div class="disp-formula">`` group, and loose ``NavigableString`` +
        inline-formula text (all as siblings of the section wrapper) — the
        previous select_one / early-return dispatch dropped everything after
        the first matched block type.
        """
        inline_buffer: list[str] = []

        def flush_inline():
            if not inline_buffer:
                return
            joined = ''.join(inline_buffer).strip()
            inline_buffer.clear()
            if not joined:
                return
            md = cls._convert_aip_html_fragment_to_markdown(f"<p>{joined}</p>")
            if md:
                body_parts.extend([md, ""])

        for child in section.children:
            if isinstance(child, NavigableString):
                text = str(child)
                if text.strip():
                    inline_buffer.append(text)
                continue
            if not getattr(child, 'name', None):
                continue

            classes = child.get('class') or []

            if child.name == 'p':
                flush_inline()
                md = cls._convert_aip_html_fragment_to_markdown(str(child))
                if md:
                    body_parts.extend([md, ""])
                continue

            if child.name in ('ul', 'ol'):
                flush_inline()
                md = cls._convert_aip_list(child)
                if md:
                    body_parts.extend([md, ""])
                continue

            if child.name == 'div':
                if 'block-child-p' in classes:
                    flush_inline()
                    md = cls._convert_aip_block_child_p(child)
                    if md:
                        body_parts.extend([md, ""])
                    continue

                if 'disp-formula' in classes:
                    flush_inline()
                    # A disp-formula may wrap several <div class="formula-wrap">
                    # under a shared (N) label. Emit each equation on its own,
                    # attaching the label to the first.
                    label_el = child.find('span', class_='label')
                    label_text = label_el.get_text(' ', strip=True) if label_el else ''
                    wraps = child.find_all('div', class_='formula-wrap', recursive=False)
                    if not wraps:
                        wraps = [child]
                    for i, wrap in enumerate(wraps):
                        fm = cls._convert_aip_display_formula(wrap)
                        if not fm:
                            continue
                        if i == 0 and label_text and label_text not in fm:
                            fm = f"{fm}\n\n{label_text}"
                        body_parts.extend([fm, ""])
                    continue

                if 'formula-wrap' in classes:
                    flush_inline()
                    md = cls._convert_aip_display_formula(child)
                    if md:
                        body_parts.extend([md, ""])
                    continue

                if 'table-wrap' in classes:
                    flush_inline()
                    md = cls._convert_aip_table_to_md(child)
                    if md:
                        body_parts.extend([md, ""])
                    continue

                if 'fig-section' in classes:
                    flush_inline()
                    fig_label = child.find('div', class_='fig-label')
                    fig_caption = child.find('div', class_='caption')
                    parts = []
                    if fig_label:
                        lt = fig_label.get_text(' ', strip=True)
                        if lt:
                            parts.append(f"**{lt}**")
                    if fig_caption:
                        cap_md = cls._convert_aip_html_fragment_to_markdown(str(fig_caption))
                        if cap_md:
                            parts.append(cap_md)
                    if parts:
                        body_parts.extend([" ".join(parts), ""])
                    continue

                if 'fig-modal' in classes or 'reveal-modal' in classes:
                    # Duplicate of the fig-section rendered as a lightbox
                    # modal — the real figure was already emitted above.
                    continue

            # Anything else (unknown div, <span>, <a>, <em>, ...) → buffer as
            # inline so trailing "Here <span>ε</span> is the …" text after a
            # display formula still ends up in a paragraph.
            inline_buffer.append(str(child))

        flush_inline()

    @classmethod
    def _convert_aip_list(cls, list_tag, level: int = 0) -> str:
        """Convert <ul>/<ol> to markdown, recursing into nested lists.

        Each ``<li>`` may contain a ``<p>`` plus more lists.  The shared
        fragment converter collapses whitespace, so we process every
        non-list child of the ``<li>`` as one fragment and recurse into
        nested ``<ul>``/``<ol>`` separately.
        """
        is_ordered = list_tag.name == 'ol'
        indent = '    ' * level
        lines = []
        counter = 0
        for li in list_tag.find_all('li', recursive=False):
            counter += 1
            marker = f"{counter}." if is_ordered else "-"

            nested_lists = [
                c for c in li.find_all(['ul', 'ol'], recursive=False)
            ]

            # Build an HTML fragment containing every direct child of the
            # ``<li>`` that is NOT a nested list, so the formula pipeline
            # still sees inline MathJax/MathML in the item's prose.
            inner_html_parts = []
            for child in li.children:
                if hasattr(child, 'name') and child.name in ('ul', 'ol'):
                    continue
                inner_html_parts.append(str(child))
            inner_html = ''.join(inner_html_parts).strip()

            text_md = ''
            if inner_html:
                # Wrap in a div so the converter sees a block context.
                text_md = cls._convert_aip_html_fragment_to_markdown(
                    f"<div>{inner_html}</div>"
                ).strip()

            if text_md:
                first_line, *rest = text_md.split('\n')
                lines.append(f"{indent}{marker} {first_line}")
                cont_indent = indent + ('   ' if is_ordered else '  ')
                for rl in rest:
                    lines.append(f"{cont_indent}{rl}" if rl.strip() else '')
            else:
                lines.append(f"{indent}{marker}")

            for nl in nested_lists:
                nested_md = cls._convert_aip_list(nl, level=level + 1)
                if nested_md:
                    lines.append(nested_md)

        return '\n'.join(lines).strip()

    @classmethod
    def extract_article_text_from_html(cls, html_content: str):
        """Extract AIP article text, returning (abstract_md, body_md).

        The abstract section is processed through the same pipeline as body
        paragraphs and returned separately so the caller can place it in the
        appropriate markdown section.
        """
        if not html_content:
            return '', ''

        soup = BeautifulSoup(html_content, 'html.parser')
        abstract_parts = []
        body_parts = []

        topics = [
            link.get_text(' ', strip=True)
            for link in soup.select('div.content-metadata.article-metadata a')
            if link.get_text(' ', strip=True)
        ]
        if topics:
            body_parts.extend([
                "### Topics",
                "",
                ", ".join(topics),
                "",
            ])

        # Heading levels we render:
        #   h2 → ###   h3 → ####   h4 → #####   h5 → ######   h6 → ######
        # (markdown caps at ######, so h5/h6 both map to it.)
        # data-section-title attr is only reliable on h2/h3 (jumplink headings);
        # h4-h6 subsection titles carry only the visible text, so accept them
        # unconditionally when they wear the section-title class.
        _HEADING_LEVELS = {
            'h2': '###', 'h3': '####', 'h4': '#####',
            'h5': '######', 'h6': '######',
        }

        seen_content_ids = set()
        article_nodes = soup.find_all(
            lambda tag: (
                tag.name in {'h2', 'h3'}
                and tag.get('data-section-title') is not None
            ) or (
                tag.name in {'h4', 'h5', 'h6'}
                and 'section-title' in (tag.get('class') or [])
            ) or (
                tag.name == 'div'
                and 'article-section-wrapper' in tag.get('class', [])
            )
        )

        for node in article_nodes:
            if node.name in _HEADING_LEVELS:
                # Route the heading INNER HTML through the same paragraph
                # pipeline as body text so <mjx-container> / <math> / <sup>
                # get preserved (previously get_text() stripped them, so a
                # heading like "B. Lawson's second insight: Dependence of
                # fuel energy gain on T and n τ" was truncated at the
                # first MathJax span).
                inner_html = node.decode_contents().strip()
                if inner_html:
                    heading = cls._convert_aip_html_fragment_to_markdown(
                        f"<p>{inner_html}</p>"
                    )
                else:
                    heading = ''
                # Fallback to data-section-title / plain text if the
                # conversion produced nothing.
                if not heading:
                    heading = (node.get('data-section-title')
                               or node.get_text(' ', strip=True) or '').strip()
                heading = re.sub(r'\s+', ' ', heading or '').strip()
                if not heading:
                    continue
                body_parts.extend([f"{_HEADING_LEVELS[node.name]} {heading}", ""])
                continue

            content_id = node.get('id')
            if not content_id or content_id in seen_content_ids:
                continue
            seen_content_ids.add(content_id)

            # Extract abstract through the same pipeline, but collect separately.
            if node.find('section', class_='abstract', attrs={'aria-label': 'Main abstract'}):
                abstract_section = node.find('section', class_='abstract', attrs={'aria-label': 'Main abstract'})
                for paragraph in abstract_section.find_all('p'):
                    paragraph_md = cls._convert_aip_html_fragment_to_markdown(str(paragraph))
                    if paragraph_md:
                        abstract_parts.append(paragraph_md)
                continue

            # Walk direct children in document order so a section that mixes
            # <p>, a multi-formula <div class="disp-formula">, and loose text
            # (e.g. 10.1063/1.2844352 section 66667746) doesn't drop anything.
            cls._walk_aip_section(node, body_parts)

        abstract_md = "\n\n".join(abstract_parts).strip()
        body_md = "\n".join(body_parts).strip()
        return abstract_md, body_md

    @classmethod
    def extract_references_from_html(cls, html_content: str) -> list:
        """Extract AIP references from HTML, preserving DOI links as markdown."""
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        references = []

        ref_divs = soup.find_all('div', attrs={'data-content-id': True})

        for ref_div in ref_divs:
            citation_div = ref_div.find('div', class_='mixed-citation')
            if not citation_div:
                continue

            label = ref_div.find('span', class_='label')
            ref_num = label.get_text(' ', strip=True) if label else ''

            # Remove citation-links (Google Scholar, Crossref, ADS, PubMed, OpenURL)
            citation_links = citation_div.find('div', class_='citation-links')
            if citation_links:
                citation_links.decompose()

            ref_html = str(citation_div)
            ref_md = cls._convert_aip_html_fragment_to_markdown(ref_html)
            if ref_md:
                ref_md = re.sub(r'\n+', ' ', ref_md).strip()
                ref_md = re.sub(r'[ \t]+', ' ', ref_md).strip()
                references.append(f"{ref_num} {ref_md}".strip())

        return references

    @classmethod
    def _extract_ref_dois_from_html(cls, html_content: str) -> list:
        """Extract DOIs from AIP reference citation-links."""
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        dois = []

        ref_divs = soup.find_all('div', attrs={'data-content-id': True})
        for ref_div in ref_divs:
            doi = ''
            citation_links = ref_div.find('div', class_='citation-links')
            if citation_links:
                doi_link = citation_links.find('a', href=re.compile(r'(doi\.org|dx\.doi\.org)'))
                if doi_link:
                    href = doi_link.get('href', '')
                    doi_match = re.search(r'10\.\d{4,}/[^\s"\'<>]+', href)
                    if doi_match:
                        doi = doi_match.group(0).rstrip('.')
            dois.append(doi)

        return dois

    @classmethod
    def extract_figures_from_html(cls, html_content: str) -> dict:
        """Extract AIP figure URLs and captions from HTML."""
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        figures = {}
        seen_ids = set()

        for fig_div in soup.find_all('div', class_='fig-section'):
            data_id = fig_div.get('data-id', '')
            if not data_id or data_id in seen_ids:
                continue
            seen_ids.add(data_id)

            fig_num = data_id.lstrip('f')
            key = f"fig_{fig_num}"

            img = fig_div.find('img', class_='content-image')
            if not img:
                continue
            thumb_url = (img.get('src') or img.get('data-src') or '').strip()
            if not thumb_url:
                continue

            # The <img> shows the "m_" thumbnail (e.g. m_054003_1_….jpeg). The
            # full-resolution JPEG is embedded as a signed CDN URL in the
            # "Download slide" link's `image=` query parameter, e.g.
            #   /DownloadFile/DownloadImage.aspx?image=<CDN URL>&sec=…&ar=…
            # A naive m_-strip won't work: CloudFront's Signature is bound to
            # the exact URL, so we must lift the pre-signed high-res URL that
            # AIP ships with the page. The CDN URL ends at the third
            # CloudFront param (Key-Pair-Id=<value>) — everything after that
            # belongs to the aspx handler, not to the JPEG's signed URL.
            hi_res_url = ''
            dl_slide = fig_div.find('a', class_='download-slide')
            if dl_slide:
                href = dl_slide.get('href', '') or ''
                m = re.search(
                    r'[?&]image=(https?://.+?[?&]Key-Pair-Id=[^&]+)',
                    href,
                )
                if m:
                    hi_res_url = m.group(1).strip()

            label = fig_div.find('div', class_='fig-label')
            caption = label.get_text(' ', strip=True) if label else ''

            entry = {
                'url': hi_res_url or thumb_url,
                'caption': caption,
            }
            # Keep the m_-thumbnail as fallback so the download retry logic in
            # complete_paper_extraction.py can drop back to it if the hi-res
            # URL 403s (e.g. signature expired between scrape and download).
            if hi_res_url and thumb_url and hi_res_url != thumb_url:
                entry['original_url'] = thumb_url
            figures[key] = entry

        return figures

    @staticmethod
    def _extract_direct_supplemental_links(html_content: str) -> tuple:
        """Extract supplemental material links directly from the article HTML.

        AIP marks supplemental links in several ways:
          * class="supplementary-material-link" (older inline links)
          * data-doctype="dataSupplementDoc" (data supplements widget)
          * any <a> inside div.widget-ArticleDataSupplements
          * any <a> following an h2 whose text matches "supplementary material"
            (some articles have two such h2s — the first is just the
            descriptive prose, the second wraps the actual download link)

        Returns (urls, descriptions) where descriptions maps URL -> text content.
        """
        if not html_content:
            return [], {}

        soup = BeautifulSoup(html_content, 'html.parser')
        seen_urls = set()
        links = []
        descriptions = {}

        def _add_link(link_tag):
            href = (link_tag.get('href') or '').strip()
            if not href or href.startswith(('javascript:', '#', 'mailto:')):
                return
            if href.startswith('/'):
                href = f"https://pubs.aip.org{href}"
            elif not href.startswith('http'):
                href = f"https://pubs.aip.org/{href}"
            if href in seen_urls:
                return
            seen_urls.add(href)
            links.append(href)
            link_text = link_tag.get_text(' ', strip=True)
            if link_text:
                descriptions[href] = link_text

        # 1) Inline supplementary-material-link anchors
        for link in soup.find_all('a', class_='supplementary-material-link'):
            _add_link(link)

        # 2) Data-supplement widget anchors (data-doctype attribute)
        for link in soup.find_all('a', attrs={'data-doctype': 'dataSupplementDoc'}):
            _add_link(link)

        # 3) Any <a href> inside the ArticleDataSupplements widget
        for widget in soup.find_all('div', class_='widget-ArticleDataSupplements'):
            for link in widget.find_all('a', href=True):
                _add_link(link)

        # 4) Walk every h2 whose text (or data-section-title) matches
        #    "supplementary material".  For each match, scan the enclosing
        #    section container AND the following siblings until the next h2
        #    for download anchors.  This catches articles where the actual
        #    link only appears under the second of two "Supplementary
        #    Material" headings.
        def _is_supp_heading(tag):
            if tag.name != 'h2':
                return False
            section_title = (tag.get('data-section-title') or '').strip().lower()
            text = tag.get_text(' ', strip=True).lower()
            return ('supplementary material' in section_title
                    or 'supplementary material' in text)

        def _looks_like_download(link_tag) -> bool:
            href = (link_tag.get('href') or '').strip().lower()
            if not href or href.startswith(('javascript:', '#', 'mailto:')):
                return False
            patterns = (
                'supplement', 'suppl_material', '/article-supplement/',
                'datasupplement', 'figshare', 'ndownloader',
            )
            if any(pat in href for pat in patterns):
                return True
            if link_tag.get('data-doctype') == 'dataSupplementDoc':
                return True
            classes = link_tag.get('class') or []
            if 'supplementary-material-link' in classes:
                return True
            return False

        for heading in soup.find_all(_is_supp_heading):
            # The heading itself may sit inside a wrapper widget; scan that
            # wrapper as well as the heading's following siblings.
            scan_roots = []
            parent = heading.parent
            if parent is not None:
                scan_roots.append(parent)
            for sib in heading.next_siblings:
                if getattr(sib, 'name', None) == 'h2':
                    break
                if hasattr(sib, 'find_all'):
                    scan_roots.append(sib)
            for root in scan_roots:
                for link in root.find_all('a', href=True):
                    if _looks_like_download(link):
                        _add_link(link)

        return links, descriptions

    @staticmethod
    def _extract_supplemental_links_from_html(html_content: str) -> tuple:
        """Extract supplemental material download links from the figshare widget or direct links.

        First tries to extract from class="supplementary-material-link" (direct AIP links).
        If none found, uses figshare API to discover all files in a collection.

        Returns (urls, descriptions) where descriptions maps URL -> filename.
        """
        if not html_content:
            return [], {}

        # Method 1: Try direct supplementary-material-link extraction
        direct_links, direct_descriptions = AIPHandler._extract_direct_supplemental_links(html_content)
        if direct_links:
            print(f"  ✓ 找到 {len(direct_links)} 个直接补充材料链接")
            return direct_links, direct_descriptions

        # Method 2: Fallback to figshare
        soup = BeautifulSoup(html_content, 'html.parser')
        links = []
        descriptions = {}

        figshare_wrapper = soup.find('div', id='articlefulltext_figshare')
        if figshare_wrapper:
            for a_tag in figshare_wrapper.find_all('a', href=True):
                href = a_tag['href'].strip()
                if 'figstatic.com' in href or 'ndownloader' in href:
                    links.append(href)

            # If no direct download links, discover via figshare API
            if not links:
                article_id = None
                for a_tag in figshare_wrapper.find_all('a', href=True):
                    href = a_tag['href'].strip()
                    match = re.search(r'/articles/(?:media/)?[^/]+/(\d+)', href)
                    if match:
                        article_id = match.group(1)
                        break

                if article_id:
                    links, descriptions = (
                        AIPHandler._fetch_figshare_collection(article_id, html_content)
                    )

        return links, descriptions

    @staticmethod
    def _fetch_figshare_collection(article_id: str, full_html: str) -> tuple:
        """Discover all files in a figshare collection via the figshare API."""
        import json
        import urllib.request

        collection_id = None
        match = re.search(r'10\.60893/figshare\.[^.]*\.c\.(\d+)', full_html)
        if match:
            collection_id = match.group(1)

        if not collection_id:
            return [], {}

        article_ids = []
        try:
            url = f"https://api.figshare.com/v2/collections/{collection_id}/articles"
            req = urllib.request.Request(url, headers={'User-Agent': 'DownloadPaper/1.0'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                articles = json.loads(resp.read().decode())
            article_ids = [(a.get('id'), a.get('title', '')) for a in articles if a.get('id')]
        except Exception as e:
            print(f"  ⚠ Figshare collection API error: {e}")
            return [], {}

        links = []
        descriptions = {}
        for aid, title in article_ids:
            try:
                url = f"https://api.figshare.com/v2/articles/{aid}"
                req = urllib.request.Request(url, headers={'User-Agent': 'DownloadPaper/1.0'})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode())

                files = data.get('files', [])
                for f in files:
                    download_url = f.get('download_url')
                    if download_url:
                        links.append(download_url)
                        descriptions[download_url] = f.get('name', title)
            except Exception as e:
                print(f"  ⚠ Figshare article API error for {aid}: {e}")

        return links, descriptions

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
        return []

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        """Run the AIP handler through the unified publisher contract."""
        # Initialize page and managed resources using shared function
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'AIPHandler'
        )
        doi = self.doi  # resolve doi from handler after init (may have been None)

        # Get the actual page URL for correct base_url resolution
        set_actual_base_url(self, page)

        try:
            metadata = await self.extract_metadata(page)
            metadata['doi'] = doi

            pdf_url = metadata.pop('_pdf_url', None)

            try:
                fulltext_html = await page.content()
            except Exception:
                fulltext_html = ''
            if fulltext_html and not metadata.get('abstract'):
                metadata['abstract'] = self.extract_main_abstract_from_html(fulltext_html)
            if fulltext_html:
                metadata['references'] = self.extract_references_from_html(fulltext_html)
                ref_dois = self._extract_ref_dois_from_html(fulltext_html)
                ref_bibtex = []
                for doi in ref_dois:
                    if doi:
                        # Try Crossref first, then fallback to Semantic Scholar
                        crossref_data = fetch_crossref(doi)
                        if crossref_data and crossref_data.get('title'):
                            bib = _build_bibtex_from_crossref(crossref_data, doi)
                        else:
                            s2_data = fetch_semanticscholar(doi)
                            if s2_data:
                                bib = _build_bibtex_from_s2(s2_data, doi)
                            else:
                                bib = None
                        ref_bibtex.append(bib if bib else '')
                    else:
                        ref_bibtex.append('')
                metadata['_refs_bibtex'] = ref_bibtex

            figure_urls = {}
            supp_urls = []
            supp_descriptions = {}
            if fulltext_html:
                figure_urls = self.extract_figures_from_html(fulltext_html)
                supp_urls, supp_descriptions = self._extract_supplemental_links_from_html(fulltext_html)

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figure_urls,
                    'supplemental_urls': supp_urls,
                    'supplemental_descriptions': supp_descriptions,
                },
                'fulltext_data': fulltext_html,
                'journal_name': 'aip',
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

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        """Return a minimal Markdown shell until AIP conversion is implemented."""
        title = metadata.get('title') or 'AIP Article'
        md_parts = [
            f"# {title}",
            "",
        ]

        authors = metadata.get('authors', [])
        author_with_affiliations = metadata.get('author_with_affiliations', [])
        if authors:
            md_parts.append("**Authors:**")
            md_parts.append("")
            for entry in author_with_affiliations:
                name = entry.get('author', '')
                md_parts.append(name)
                for aff in entry.get('affiliations', []):
                    md_parts.append(aff)
                md_parts.append("")
            if not author_with_affiliations:
                for author in authors:
                    md_parts.append(author)
                    md_parts.append("")

        if metadata.get('doi'):
            md_parts.extend([f"**DOI:** {metadata['doi']}", ""])

        md_parts.extend([
            "## Publication",
            "",
            f"**Journal:** {metadata.get('journal') or 'AIP Publishing'}",
            "",
        ])

        # Add DOI in Publication section if available
        if metadata.get('doi'):
            md_parts.extend([f"**DOI:** {metadata['doi']}", ""])

        if metadata.get('year'):
            md_parts.extend([f"**Year:** {metadata['year']}", ""])

        abstract_from_body = ''
        body_md = ''

        if isinstance(article_text, str) and article_text.strip():
            if article_text.lstrip().startswith('<'):
                abstract_from_body, body_md = self.extract_article_text_from_html(article_text)
            else:
                body_md = article_text.strip()

        abstract = metadata.get('abstract') or abstract_from_body
        if abstract:
            md_parts.extend([
                "---",
                "",
                "## Abstract",
                "",
                abstract,
                "",
            ])

        # Insert downloaded figure images after each caption.
        if kwargs.get('add_figure_refs') and kwargs.get('figure_filenames'):
            figure_filenames = kwargs['figure_filenames']
            for fig_num, filename in sorted(figure_filenames.items(), key=lambda x: int(x[0])):
                body_md = re.sub(
                    rf'(\*\*FIG\.\s*{re.escape(fig_num)}\.\*\*[^\n]*)',
                    rf'\1\n\n![FIG. {fig_num}.]({filename})',
                    body_md,
                )

        md_parts.extend([
            "---",
            "",
            "## Article Text",
            "",
            body_md or "[AIP article text not found.]",
            "",
        ])

        if metadata.get('references'):
            md_parts.extend([
                "---",
                "",
                "## References",
                "",
            ])
            refs_bibtex = metadata.get('_refs_bibtex', [])
            for idx, ref in enumerate(metadata['references']):
                md_parts.extend([ref, ""])
                if idx < len(refs_bibtex) and refs_bibtex[idx]:
                    md_parts.extend([f"```bibtex\n{refs_bibtex[idx]}\n```", ""])

        return "\n".join(md_parts)

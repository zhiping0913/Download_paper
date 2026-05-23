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

        # Remove decorative SVG icons (e.g. external-link arrows on physicstoday).
        # By this point all MathJax SVGs are already replaced with placeholders.
        for svg_tag in soup.find_all('svg'):
            svg_tag.decompose()

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
        # Collapse redundant ****+ sequences (from nested <b><b> or <i><i>) to **
        md = re.sub(r'\*{4,}', '**', md)
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

        # Combine header + table
        header = f"**{label}** {caption}" if label else f"**{caption}**"
        return f"\n{header}\n\n{md}\n"

    @classmethod
    def _convert_aip_disp_formula_block(cls, disp_formula_div) -> str:
        """Convert a div.disp-formula containing one or more formula-wraps.

        Replaces each formula-wrap with a placeholder, converts the container
        text via pandoc, then restores the LaTeX blocks.
        Does NOT consume siblings — callers handle trailing inline content.
        """
        import copy
        block_copy = copy.deepcopy(disp_formula_div)
        display_formulas = []
        for fw in block_copy.find_all('div', class_='formula-wrap'):
            formula_md = cls._convert_aip_display_formula(fw)
            if formula_md:
                placeholder = f"AIPDISPF{len(display_formulas):03d}MATHEND"
                display_formulas.append((placeholder, formula_md))
                fw.replace_with(placeholder)
        text_md = cls._convert_aip_html_fragment_to_markdown(str(block_copy))
        for placeholder, formula_md in display_formulas:
            text_md = text_md.replace(placeholder, f"\n\n{formula_md}\n")
        return text_md.strip()

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
    def extract_article_text_from_html(cls, html_content: str):
        """Extract AIP article text, returning (abstract_md, body_md).

        The abstract section is processed through the same pipeline as body
        paragraphs and returned separately so the caller can place it in the
        appropriate markdown section.
        """
        if not html_content:
            return '', ''

        soup = BeautifulSoup(html_content, 'html.parser')

        if cls._is_physicstoday_page(soup):
            return '', cls._extract_physicstoday_body(soup)

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

        seen_content_ids = set()
        article_nodes = soup.find_all(
            lambda tag: (
                tag.name in {'h2', 'h3'}
                and tag.get('data-section-title') is not None
            ) or (
                tag.name == 'div'
                and 'article-section-wrapper' in tag.get('class', [])
            )
        )

        for node in article_nodes:
            if node.name in {'h2', 'h3'}:
                heading = node.get('data-section-title') or node.get_text(' ', strip=True)
                heading = re.sub(r'\s+', ' ', heading or '').strip()
                if not heading:
                    continue
                level = "###" if node.name == 'h2' else "####"
                body_parts.extend([f"{level} {heading}", ""])
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

            figure_md = cls._convert_aip_figure(node)
            if figure_md:
                body_parts.extend([figure_md, ""])
                continue

            # Iterate direct children in document order, buffering inline
            # content (text nodes, spans, links) between structural blocks.
            # This correctly handles nodes where a <p> precedes a disp-formula,
            # or where trailing inline text follows a formula block.
            pending_inline: list = []

            def _flush_inline() -> None:
                if not pending_inline:
                    return
                after_md = cls._convert_aip_html_fragment_to_markdown(
                    "<p>" + "".join(pending_inline) + "</p>"
                )
                if after_md:
                    body_parts.extend([after_md, ""])
                pending_inline.clear()

            for child in node.children:
                if isinstance(child, NavigableString):
                    if str(child).strip():
                        pending_inline.append(str(child))
                    continue

                if child.name == 'p':
                    _flush_inline()
                    paragraph_md = cls._convert_aip_html_fragment_to_markdown(str(child))
                    if paragraph_md:
                        body_parts.extend([paragraph_md, ""])
                    continue

                if child.name == 'div':
                    if child.select_one('div.fig-section'):
                        _flush_inline()
                        fig_md = cls._convert_aip_figure(child)
                        if fig_md:
                            body_parts.extend([fig_md, ""])
                        continue
                    cls_list = child.get('class') or []
                    if 'block-child-p' in cls_list:
                        _flush_inline()
                        block_md = cls._convert_aip_block_child_p(child)
                        if block_md:
                            body_parts.extend([block_md, ""])
                        continue
                    if 'disp-formula' in cls_list:
                        _flush_inline()
                        formula_block_md = cls._convert_aip_disp_formula_block(child)
                        if formula_block_md:
                            body_parts.extend([formula_block_md, ""])
                        continue
                    if 'formula-wrap' in cls_list:
                        _flush_inline()
                        formula_md = cls._convert_aip_display_formula(child)
                        if formula_md:
                            body_parts.extend([formula_md, ""])
                        continue
                    if 'table-wrap' in cls_list:
                        _flush_inline()
                        table_md = cls._convert_aip_table_to_md(child)
                        if table_md:
                            body_parts.extend([table_md, ""])
                        continue
                    # Unrecognised div — treat as inline content
                    pending_inline.append(str(child))
                    continue

                # span, a, sup, sub, etc. — inline content
                pending_inline.append(str(child))

            _flush_inline()

        abstract_md = "\n\n".join(abstract_parts).strip()
        body_md = "\n".join(body_parts).strip()
        return abstract_md, body_md

    @classmethod
    def extract_references_from_html(cls, html_content: str) -> list:
        """Extract AIP references from HTML, preserving DOI links as markdown."""
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')

        if cls._is_physicstoday_page(soup):
            return cls._extract_physicstoday_references(soup)

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

        if cls._is_physicstoday_page(soup):
            return cls._extract_physicstoday_ref_dois(soup)

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

        if cls._is_physicstoday_page(soup):
            return cls._extract_physicstoday_figure_urls(soup)

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
            img_url = img.get('src') or img.get('data-src') or ''
            if not img_url:
                continue

            label = fig_div.find('div', class_='fig-label')
            caption = label.get_text(' ', strip=True) if label else ''

            figures[key] = {
                'url': img_url.strip(),
                'caption': caption,
            }

        return figures

    @staticmethod
    def _extract_direct_supplemental_links(html_content: str) -> tuple:
        """Extract supplemental material links directly from class="supplementary-material-link".

        AIP embeds supplemental links directly in the HTML with class="supplementary-material-link".
        Returns (urls, descriptions) where descriptions maps URL -> text content.
        """
        if not html_content:
            return [], {}

        soup = BeautifulSoup(html_content, 'html.parser')
        seen_urls = set()
        links = []
        descriptions = {}

        # Find all links with class="supplementary-material-link"
        supp_links = soup.find_all('a', class_='supplementary-material-link')

        for link in supp_links:
            href = link.get('href', '').strip()
            if not href:
                continue

            # Build full URL if relative
            if href.startswith('/'):
                href = f"https://pubs.aip.org{href}"
            elif not href.startswith('http'):
                href = f"https://pubs.aip.org/{href}"

            # Deduplicate
            if href in seen_urls:
                continue
            seen_urls.add(href)
            links.append(href)

            # Extract link text as description
            link_text = link.get_text(' ', strip=True)
            if link_text:
                descriptions[href] = link_text

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

    # -------------------------------------------------------------------------
    # Physics Today (physicstoday.aip.org) helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _is_physicstoday_page(soup) -> bool:
        """Detect a Physics Today page by its unique article body class."""
        return bool(soup.find('div', class_='RichTextArticleBody'))

    @classmethod
    def _extract_physicstoday_pdf_url(cls, soup) -> str:
        """Return the PDF href from the FloatingSocialMedia download button."""
        for li in soup.find_all('li', class_='FloatingSocialMedia-icon-item_download'):
            a = li.find('a', href=True)
            if a:
                href = a.get('href', '').strip()
                if href:
                    return href
        return ''

    @classmethod
    def _extract_physicstoday_metadata(cls, soup, doi: str = None) -> dict:
        """Extract title, year, abstract, and authors from a Physics Today page."""
        import json as json_mod
        meta = {}

        # JSON-LD: authoritative for headline, datePublished, description
        ld_script = soup.find('script', type='application/ld+json')
        if ld_script and ld_script.string:
            try:
                ld = json_mod.loads(ld_script.string)
                meta['title'] = ld.get('headline') or ld.get('name') or ''
                date_pub = ld.get('datePublished', '')
                if date_pub:
                    meta['year'] = date_pub[:4]
                meta['abstract'] = ld.get('description', '')
            except Exception:
                pass

        if not meta.get('title'):
            og = soup.find('meta', attrs={'property': 'og:title'})
            if og:
                meta['title'] = og.get('content', '').strip()
        if not meta.get('title'):
            h1 = soup.find('h1', class_='Page-headline')
            if h1:
                meta['title'] = h1.get_text(strip=True)

        if not meta.get('abstract'):
            og_desc = soup.find('meta', attrs={'property': 'og:description'})
            if og_desc:
                meta['abstract'] = og_desc.get('content', '').strip()
        if not meta.get('abstract'):
            sub = soup.find('div', class_='Page-subHeadline')
            if sub:
                meta['abstract'] = sub.get_text(strip=True)

        authors = []
        author_with_affiliations = []
        for name_div in soup.select('div.Page-authors .IndividualCard__name'):
            name = name_div.get_text(' ', strip=True)
            if name:
                authors.append(name)
                author_with_affiliations.append({'author': name, 'affiliations': []})
        meta['authors'] = authors
        meta['author_with_affiliations'] = author_with_affiliations

        doi_p = soup.find('p', class_='doi-wrapper')
        if doi_p:
            doi_a = doi_p.find('a')
            if doi_a:
                meta['doi'] = doi_a.get_text(strip=True)
        if not meta.get('doi') and doi:
            meta['doi'] = doi

        meta.setdefault('title', 'Physics Today Article')
        meta.setdefault('journal', 'Physics Today')
        meta.setdefault('year', None)
        meta.setdefault('abstract', '')
        meta['corresponding_author_emails'] = []
        meta['references'] = []
        return meta

    @classmethod
    def _convert_physicstoday_figure(cls, fig_element) -> str:
        """Convert a Physics Today <figure> to markdown, preferring the 2x webp URL."""
        img_url = ''
        webp_source = fig_element.find('source', attrs={'type': 'image/webp'})
        if webp_source:
            srcset = webp_source.get('srcset', '')
            if srcset:
                # srcset format: "URL1 1x,URL2 2x" — last entry is largest
                entries = [e.strip() for e in srcset.split(',') if e.strip()]
                if entries:
                    last = entries[-1].strip()
                    parts = last.split()
                    img_url = parts[0] if parts else ''
        if not img_url:
            img = fig_element.find('img')
            if img:
                img_url = img.get('src', '').strip()

        figcaption = fig_element.find('figcaption')
        caption = ''
        if figcaption:
            for modal in figcaption.find_all('bsp-modal-window'):
                modal.decompose()
            cap_parts = []
            for p in figcaption.find_all('p'):
                text = p.get_text(' ', strip=True)
                if text:
                    cap_parts.append(text)
            caption = ' '.join(cap_parts).strip()

        if img_url and caption:
            return f"![Figure]({img_url})\n\n{caption}"
        if img_url:
            return f"![Figure]({img_url})"
        return caption

    @classmethod
    def _extract_physicstoday_body(cls, soup) -> str:
        """Extract article body from div.RichTextArticleBody on Physics Today pages."""
        body_div = soup.find('div', class_='RichTextArticleBody')
        if not body_div:
            return ''

        parts = []
        for child in body_div.children:
            if isinstance(child, NavigableString):
                continue
            if child.name == 'p':
                text = child.get_text(strip=True)
                if not text:
                    continue
                md = cls._convert_aip_html_fragment_to_markdown(str(child))
                if md:
                    parts.extend([md, ''])
            elif child.name == 'h2':
                heading = child.get_text(' ', strip=True)
                if heading:
                    parts.extend([f"### {heading}", ''])
            elif child.name == 'div':
                for fig in child.find_all('figure'):
                    fig_md = cls._convert_physicstoday_figure(fig)
                    if fig_md:
                        parts.extend([fig_md, ''])
        return '\n'.join(parts).strip()

    @classmethod
    def _extract_physicstoday_figure_urls(cls, soup) -> dict:
        """Extract figure URLs from Physics Today pages, preferring webp 2x."""
        figures = {}
        for fig_idx, fig in enumerate(soup.find_all('figure'), 1):
            img_url = ''
            webp_source = fig.find('source', attrs={'type': 'image/webp'})
            if webp_source:
                srcset = webp_source.get('srcset', '')
                if srcset:
                    entries = [e.strip() for e in srcset.split(',') if e.strip()]
                    if entries:
                        last = entries[-1].strip()
                        parts = last.split()
                        img_url = parts[0] if parts else ''
            if not img_url:
                img = fig.find('img')
                if img:
                    img_url = img.get('src', '').strip()
            if img_url:
                figcaption = fig.find('figcaption')
                caption = ''
                if figcaption:
                    for modal in figcaption.find_all('bsp-modal-window'):
                        modal.decompose()
                    caption = figcaption.get_text(' ', strip=True)
                figures[str(fig_idx)] = {'url': img_url, 'caption': caption}
        return figures

    @classmethod
    def _extract_physicstoday_ref_dois(cls, soup) -> list:
        """Return a DOI (or '') for each reference in ol.BodyReference."""
        dois = []
        ref_ol = soup.find('ol', class_='BodyReference')
        if not ref_ol:
            return dois
        for li in ref_ol.find_all('li', recursive=False):
            doi = ''
            for a in li.find_all('a', href=True):
                href = a.get('href', '')
                m = re.search(r'10\.\d{4,}/[^\s"\'<>]+', href)
                if m:
                    doi = m.group(0).rstrip('.')
                    break
            dois.append(doi)
        return dois

    @classmethod
    def _extract_physicstoday_references(cls, soup) -> list:
        """Extract references from <ol class="BodyReference"> on Physics Today pages."""
        refs = []
        ref_ol = soup.find('ol', class_='BodyReference')
        if not ref_ol:
            return refs
        for idx, li in enumerate(ref_ol.find_all('li', recursive=False), 1):
            p = li.find('p')
            if not p:
                continue
            ref_md = cls._convert_aip_html_fragment_to_markdown(str(p))
            ref_md = re.sub(r'^[►▶]\s*', '', ref_md.strip()).strip()
            if ref_md:
                refs.append(f"[{idx}] {ref_md}")
        return refs

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

            # Override metadata for Physics Today pages (different HTML structure)
            if fulltext_html:
                _pt_soup = BeautifulSoup(fulltext_html, 'html.parser')
                if self._is_physicstoday_page(_pt_soup):
                    pt_meta = self._extract_physicstoday_metadata(_pt_soup, doi)
                    for k, v in pt_meta.items():
                        if v is not None and v != '' and v != []:
                            metadata[k] = v
                    if not pdf_url:
                        pdf_url = self._extract_physicstoday_pdf_url(_pt_soup)

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

        # For physicstoday: body already contains ![Figure](https://...) links embedded
        # inline. Replace each web URL sequentially with the corresponding local filename.
        if kwargs.get('figure_filenames') and re.search(r'!\[Figure\]\(https?://', body_md):
            for fig_num in sorted(kwargs['figure_filenames'].keys(), key=lambda x: int(x)):
                filename = kwargs['figure_filenames'][fig_num]
                body_md = re.sub(
                    r'!\[Figure\]\(https?://[^)]+\)',
                    f'![Figure {fig_num}]({filename})',
                    body_md,
                    count=1,
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

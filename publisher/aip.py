"""
AIP Publishing handler skeleton.

This module wires AIP into the shared PublisherHandler contract. Detailed
body/reference extraction for pubs.aip.org is intentionally left for a future
pass.
"""

import re

from bs4 import BeautifulSoup, NavigableString
from playwright.async_api import async_playwright

from json_to_md_converter import (
    cleanup_markdown,
    convert_html_to_markdown,
    mathml_to_latex_pandoc,
    remove_newlines_in_paragraph,
)
from publisher.base import PublisherHandler


class AIPHandler(PublisherHandler):
    """Handler interface for AIP Publishing articles."""

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.base_url = "https://pubs.aip.org"

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

            formula = node.select_one('div.formula-wrap')
            if formula:
                formula_md = cls._convert_aip_display_formula(formula)
                if formula_md:
                    body_parts.extend([formula_md, ""])
                continue

            paragraphs = node.find_all('p', recursive=False)
            for paragraph in paragraphs:
                paragraph_md = cls._convert_aip_html_fragment_to_markdown(str(paragraph))
                if paragraph_md:
                    body_parts.extend([paragraph_md, ""])

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
        doi = doi or self.doi
        if doi is None:
            raise ValueError("AIPHandler.extract_all() requires a DOI")

        page = page or self.page
        managed_playwright = None
        managed_browser = None
        managed_context = None

        if page is None:
            print("  ✓ AIPHandler未收到page，使用无头浏览器访问")
            managed_playwright = await async_playwright().start()
            managed_browser = await managed_playwright.chromium.launch(headless=True)
            managed_context = await managed_browser.new_context(accept_downloads=True)
            page = await managed_context.new_page()
            self.configure(page=page, doi=doi)
            await page.goto(f"https://doi.org/{doi}", wait_until='domcontentloaded', timeout=60000)
            try:
                await page.wait_for_load_state('networkidle', timeout=15000)
            except Exception:
                pass
        else:
            self.configure(page=page, doi=doi)

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

            figure_urls = {}
            if fulltext_html:
                figure_urls = self.extract_figures_from_html(fulltext_html)

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figure_urls,
                    'supplemental_urls': [],
                    'supplemental_descriptions': {},
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
            for ref in metadata['references']:
                md_parts.extend([ref, ""])

        return "\n".join(md_parts)

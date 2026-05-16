"""
IOP Publishing handler.

Extracts metadata, body, figures, references, and supplemental materials
from IOP Science article pages (iopscience.iop.org).

IOP shares structural patterns with Nature and other publishers, so
heavy lifting is delegated to the shared functions in ``wildcard.py``.
"""

import re

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from publisher.base import PublisherHandler
from publisher.wildcard import (
    convert_html_fragment_to_markdown,
    convert_mathml,
    extract_abstract_with_fallbacks,
    find_generic_article_body,
    format_as_bibtex,
    format_citation_as_text,
    generate_bibtex_key,
    parse_citation_reference_string,
    prepare_mathjax_html_fragment,
)


class IOPHandler(PublisherHandler):
    """Handler for IOP Publishing articles (iopscience.iop.org)."""

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.base_url = "https://iopscience.iop.org"

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
    # Body text extraction
    # ------------------------------------------------------------------

    @classmethod
    def extract_article_text_from_html(cls, html_content: str):
        """Extract article body, returning (abstract_md, body_md)."""
        if not html_content:
            return '', ''

        soup = BeautifulSoup(html_content, 'html.parser')

        # Abstract via shared fallback
        abstract_md = extract_abstract_with_fallbacks(
            soup,
            paragraph_converter=lambda html: convert_html_fragment_to_markdown(html, "IOPMATH"),
        )

        # Body via shared generic body finder
        body_div = find_generic_article_body(soup)
        if not body_div:
            return abstract_md, ''

        body_parts = []

        # Process headings and paragraphs
        for element in body_div.find_all(['h2', 'h3', 'h4', 'p', 'div']):
            if element.name in ('h2', 'h3', 'h4'):
                heading_text = element.get_text(' ', strip=True)
                heading_text = re.sub(r'\s+', ' ', heading_text or '').strip()
                if heading_text:
                    level = '#' * (int(element.name[1]) + 1)
                    body_parts.extend([f"{level} {heading_text}", ""])

            elif element.name == 'p':
                p_md = convert_html_fragment_to_markdown(str(element), "IOPMATH")
                if p_md:
                    body_parts.extend([p_md, ""])

            elif element.name == 'div' and 'c-article-equation' in element.get('class', []):
                math_tag = element.find('math')
                if math_tag:
                    latex = convert_mathml(math_tag, display=True)
                    if latex:
                        body_parts.extend([latex, ""])

        body_md = "\n".join(body_parts).strip()
        return abstract_md, body_md

    # ------------------------------------------------------------------
    # Figure extraction
    # ------------------------------------------------------------------

    @classmethod
    def extract_figures_from_html(cls, html_content: str) -> dict:
        """Extract figure URLs and captions from HTML.

        IOP figures are typically in <div class="wd-jnl-fig"> containers
        with <img> tags and captions in nearby <div class="wd-jnl-fig-caption">.
        """
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        figures = {}
        seen_srcs = set()

        for fig_div in soup.find_all('div', class_='wd-jnl-fig'):
            img = fig_div.find('img')
            if not img:
                continue
            img_url = img.get('src') or img.get('data-src') or ''
            if not img_url or img_url in seen_srcs:
                continue
            seen_srcs.add(img_url)

            fig_num = len(figures) + 1
            key = f"fig_{fig_num}"

            caption_div = fig_div.find('div', class_='wd-jnl-fig-caption')
            caption = caption_div.get_text(' ', strip=True) if caption_div else ''

            figures[key] = {
                'url': img_url.strip(),
                'caption': caption,
            }

        # Fallback: generic figure search
        if not figures:
            for img in soup.find_all('img'):
                src = img.get('src') or img.get('data-src') or ''
                if not src or 'figure' not in src.lower():
                    continue
                if src in seen_srcs:
                    continue
                seen_srcs.add(src)
                fig_num = len(figures) + 1
                figures[f"fig_{fig_num}"] = {'url': src.strip(), 'caption': ''}

        return figures

    # ------------------------------------------------------------------
    # Reference extraction
    # ------------------------------------------------------------------

    @classmethod
    def extract_references_from_html(cls, html_content: str) -> list:
        """Extract references from citation_reference meta tags and format as BibTeX."""
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        references = []

        for tag in soup.find_all('meta', {'name': 'citation_reference'}):
            ref_str = tag.get('content', '')
            if ref_str:
                bibtex = parse_citation_reference_string(ref_str)
                if bibtex:
                    references.append(bibtex)

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

    # ------------------------------------------------------------------
    # Supplemental material extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_supplemental_links_from_html(html_content: str) -> list:
        """Extract supplemental material download links."""
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        links = []

        for supp_class in ('supplementary-material', 'wd-jnl-supp-info', 'supplementary'):
            for div in soup.find_all('div', class_=re.compile(supp_class, re.I)):
                for a in div.find_all('a', href=True):
                    href = a['href'].strip()
                    if href and not href.startswith('#'):
                        links.append(href)

        return links

    # ------------------------------------------------------------------
    # Publisher contract methods
    # ------------------------------------------------------------------

    async def extract_metadata(self, page) -> dict:
        """Return metadata from HTML meta tags and DOM."""
        html_content = ''
        if page is not None:
            try:
                html_content = await page.content()
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
        return None

    async def get_supplemental_url(self, doi: str) -> str:
        return None

    async def extract_references(self, html: str) -> list:
        return []

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        """Run the IOP handler through the unified publisher contract."""
        doi = doi or self.doi
        if doi is None:
            raise ValueError("IOPHandler.extract_all() requires a DOI")

        page = page or self.page
        managed_playwright = None
        managed_browser = None
        managed_context = None

        if page is None:
            print("  ✓ IOPHandler未收到page，使用无头浏览器访问")
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
            keywords = metadata.pop('_keywords', [])

            try:
                fulltext_html = await page.content()
            except Exception:
                fulltext_html = ''

            if fulltext_html:
                metadata['references'] = self.extract_references_from_html(fulltext_html)
                metadata['_refs_raw'] = self._extract_raw_citation_references(fulltext_html)

            figure_urls = {}
            supp_urls = []
            if fulltext_html:
                figure_urls = self.extract_figures_from_html(fulltext_html)
                supp_urls = self._extract_supplemental_links_from_html(fulltext_html)

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figure_urls,
                    'supplemental_urls': supp_urls,
                    'supplemental_descriptions': {},
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
                    rf'(\*\*Figure\s*{re.escape(fig_num)}\.\*\*[^\n]*)',
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

        # References: each ref = numbered text + its own BibTeX block
        references = metadata.get('references', [])
        refs_raw = metadata.get('_refs_raw', [])
        if references:
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
                # BibTeX block for this reference
                md_parts.extend(["```bibtex", ref, "```", ""])
            md_parts.append("")

        return "\n".join(md_parts)

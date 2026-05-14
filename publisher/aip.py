"""
AIP Publishing handler skeleton.

This module wires AIP into the shared PublisherHandler contract. Detailed
body/reference extraction for pubs.aip.org is intentionally left for a future
pass.
"""

import re

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from json_to_md_converter import (
    cleanup_markdown,
    convert_html_to_markdown,
    remove_newlines_in_paragraph,
)
from publisher.base import PublisherHandler


class AIPHandler(PublisherHandler):
    """Handler interface for AIP Publishing articles."""

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.base_url = "https://pubs.aip.org"

    async def extract_metadata(self, page) -> dict:
        """Return a minimal metadata payload with AIP main abstract when available."""
        title = None
        abstract = ''
        if page is not None:
            try:
                title = await page.title()
            except Exception:
                title = None
            try:
                abstract = self.extract_main_abstract_from_html(await page.content())
            except Exception:
                abstract = ''

        return {
            'title': title or 'AIP Article',
            'authors': [],
            'author_with_affiliations': [],
            'corresponding_author_emails': [],
            'abstract': abstract,
            'journal': 'AIP Publishing',
            'publication_date': None,
            'doi': self.doi,
            'volume': None,
            'issue': None,
            'pages': None,
            'year': None,
            'references': [],
        }

    @staticmethod
    def extract_main_abstract_from_html(html_content: str) -> str:
        """Extract AIP Main abstract and convert each paragraph to Markdown."""
        if not html_content:
            return ''

        soup = BeautifulSoup(html_content, 'html.parser')
        abstract_section = soup.find('section', class_='abstract', attrs={'aria-label': 'Main abstract'})
        if not abstract_section:
            return ''

        paragraphs = abstract_section.find_all('p')
        converted_paragraphs = []
        for paragraph in paragraphs:
            paragraph_html = str(paragraph)
            try:
                md = convert_html_to_markdown(paragraph_html)
                md = cleanup_markdown(md)
                md = remove_newlines_in_paragraph(md, "", "p")
                md = re.sub(r'\s+', ' ', md).strip()
                if md:
                    converted_paragraphs.append(md)
            except Exception as e:
                print(f"  ⚠️  AIP abstract段落转换失败: {str(e)[:80]}")

        return "\n\n".join(converted_paragraphs)

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

            try:
                fulltext_html = await page.content()
            except Exception:
                fulltext_html = ''
            if fulltext_html and not metadata.get('abstract'):
                metadata['abstract'] = self.extract_main_abstract_from_html(fulltext_html)

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': None,
                    'figure_urls': {},
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
            "## Publication",
            "",
            f"**Journal:** {metadata.get('journal') or 'AIP Publishing'}",
            "",
        ]

        if metadata.get('doi'):
            md_parts.extend([f"**DOI:** {metadata['doi']}", ""])

        if metadata.get('abstract'):
            md_parts.extend([
                "---",
                "",
                "## Abstract",
                "",
                metadata['abstract'],
                "",
            ])

        md_parts.extend([
            "---",
            "",
            "## Article Text",
            "",
            "[AIP extraction interface is wired; detailed content parsing is not implemented yet.]",
            "",
        ])

        return "\n".join(md_parts)

"""
AIP Publishing handler skeleton.

This module wires AIP into the shared PublisherHandler contract. Detailed
metadata/body/reference extraction for pubs.aip.org is intentionally left for a
future pass.
"""

from playwright.async_api import async_playwright

from publisher.base import PublisherHandler


class AIPHandler(PublisherHandler):
    """Handler interface for AIP Publishing articles."""

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.base_url = "https://pubs.aip.org"

    async def extract_metadata(self, page) -> dict:
        """Return a minimal metadata payload until AIP parsing is implemented."""
        title = None
        if page is not None:
            try:
                title = await page.title()
            except Exception:
                title = None

        return {
            'title': title or 'AIP Article',
            'authors': [],
            'author_with_affiliations': [],
            'corresponding_author_emails': [],
            'abstract': '',
            'journal': 'AIP Publishing',
            'publication_date': None,
            'doi': self.doi,
            'volume': None,
            'issue': None,
            'pages': None,
            'year': None,
            'references': [],
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

        md_parts.extend([
            "---",
            "",
            "## Article Text",
            "",
            "[AIP extraction interface is wired; detailed content parsing is not implemented yet.]",
            "",
        ])

        return "\n".join(md_parts)

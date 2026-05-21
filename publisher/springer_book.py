"""
Springer Book Publisher Handler

Handles extraction from Springer book chapters and full books.
For chapter DOIs (e.g., 10.1007/978-981-15-2381-6_2), normalizes to book DOI.
"""

from typing import Optional, Dict, List
from publisher.base import PublisherHandler
from publisher.wildcard import init_extract_all_page, set_actual_base_url


class SpringerBookHandler(PublisherHandler):
    """Handler for Springer Books and book chapters (springer.com/book, springer.com/chapter)"""

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        """
        Initialize Springer Book handler

        For chapter DOIs like "10.1007/978-981-15-2381-6_2", normalizes to book DOI "10.1007/978-981-15-2381-6"

        Args:
            page: Playwright page object
            captured_data_dir: Directory for captured data
            doi: DOI of the book or chapter
        """
        # Normalize chapter DOI to book DOI if needed
        if doi and '_' in doi:
            # Extract DOI prefix before the last underscore (e.g., 10.1007/978-981-15-2381-6 from 10.1007/978-981-15-2381-6_2)
            book_doi = doi.rsplit('_', 1)[0]
            print(f"  ℹ️  Chapter DOI detected: {doi} → normalized to book DOI: {book_doi}")
            doi = book_doi

        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        """Execute complete extraction flow for Springer books

        Args:
            page: Playwright page object (should already be navigated to DOI)
            doi: DOI of the book
            captured: Optional dict with already-captured network data

        Returns:
            dict with keys: 'metadata', 'links', 'fulltext_data', 'journal_name'
        """
        # Initialize page and managed resources using shared function
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'SpringerBookHandler'
        )

        # Get the actual page URL for correct base_url resolution
        set_actual_base_url(self, page)

        try:
            # TODO: Implement full extraction flow
            # - Extract metadata (title, authors, publisher info, etc.)
            # - Extract TOC and chapter information
            # - Extract figures from the main chapter or representative section
            # - Extract references
            # - Generate markdown content

            return {
                'metadata': {},
                'links': {
                    'pdf_url': None,
                    'figure_urls': {},
                    'supplemental_urls': [],
                    'supplemental_descriptions': {},
                },
                'fulltext_data': None,
                'journal_name': 'springer_book',
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

    async def extract_metadata(self, page) -> dict:
        """Extract metadata from Springer book page"""
        # TODO: Implement metadata extraction
        return {}

    def convert_to_markdown(self, metadata: dict, fulltext_data=None,
                          add_figure_refs: bool = False,
                          figure_filenames: dict = None, **kwargs) -> str:
        """Convert extracted Springer book data to Markdown

        Args:
            metadata: Book metadata dict
            fulltext_data: HTML content or other fulltext data
            add_figure_refs: If True, add figure references
            figure_filenames: Mapping of figure number to downloaded local filename

        Returns:
            Markdown formatted text
        """
        # TODO: Implement markdown conversion
        md_content = ""

        # TODO: Add title, authors, DOI, publication info, etc.
        # TODO: Add book/chapter summary and TOC if available
        # TODO: Add main content with proper formatting

        return md_content


__all__ = ['SpringerBookHandler']

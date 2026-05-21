"""
Springer Book Publisher Handler

Handles extraction from Springer book chapters and full books.
For chapter DOIs (e.g., 10.1007/978-981-15-2381-6_2), normalizes to book DOI.
"""

from typing import Optional, Dict, List
from publisher.base import PublisherHandler
from publisher.wildcard import set_actual_base_url


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
            # Extract DOI prefix before underscore (e.g., 10.1007/978-981-15-2381-6 from 10.1007/978-981-15-2381-6_2)
            book_doi = doi.split('_')[0]
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
        doi = doi or self.doi
        if doi is None:
            raise ValueError("SpringerBookHandler.extract_all() requires a DOI")

        page = page or self.page

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

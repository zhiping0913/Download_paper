"""
Abstract base class for all publisher implementations
"""

from abc import ABC, abstractmethod
from pathlib import Path


class PublisherHandler(ABC):
    """Abstract base class for publisher-specific paper extraction"""

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        """Store shared workflow context for publisher-specific handlers.

        Args:
            page: Optional Playwright page. The main workflow may pass this before
                or after navigation.
            captured_data_dir: Directory where captured HTML/JSON responses are
                cached for this paper.
            doi: DOI for the current paper.
        """
        self.page = page
        self.captured_data_dir = Path(captured_data_dir) if captured_data_dir else None
        self.doi = doi

    def configure(self, page=None, captured_data_dir=None, doi: str = None):
        """Update shared workflow context without rebuilding the handler."""
        if page is not None:
            self.page = page
        if captured_data_dir is not None:
            self.captured_data_dir = Path(captured_data_dir)
        if doi is not None:
            self.doi = doi
        return self

    @abstractmethod
    async def extract_metadata(self, page) -> dict:
        """Extract paper metadata (author, title, abstract, etc.)"""
        pass

    @abstractmethod
    async def get_fulltext_url(self, page) -> str:
        """Get URL for full article text"""
        pass

    @abstractmethod
    async def get_pdf_url(self, doi: str) -> str:
        """Construct PDF download URL"""
        pass

    @abstractmethod
    async def get_supplemental_url(self, doi: str) -> str:
        """Construct supplemental materials URL"""
        pass

    @abstractmethod
    async def extract_references(self, html: str) -> list:
        """Parse references from HTML/JSON"""
        pass

    @abstractmethod
    async def get_figures(self, json_data: dict) -> dict:
        """Extract figure URLs and captions"""
        pass

    @abstractmethod
    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        """Run the complete publisher-specific extraction and return the shared workflow payload."""
        pass

    @abstractmethod
    def convert_to_markdown(self, metadata: dict, article_text: str) -> str:
        """Format extracted data as Markdown"""
        pass

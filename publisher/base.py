"""
Abstract base class for all publisher implementations
"""

from abc import ABC, abstractmethod


class PublisherHandler(ABC):
    """Abstract base class for publisher-specific paper extraction"""

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
    def convert_to_markdown(self, metadata: dict, article_text: str) -> str:
        """Format extracted data as Markdown"""
        pass

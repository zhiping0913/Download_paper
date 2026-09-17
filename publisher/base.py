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

    async def get_page_html(self, page=None) -> str:
        """Return the best available HTML for the current article page.

        Prefers ``self._raw_server_html`` (the original HTTP response body
        captured before JavaScript runs) over ``page.content()`` (the
        post-JS-rendered DOM).  Handlers that need the pre-JS HTML (e.g.
        Optica, where MathJax replaces LaTeX source with SVG) should call
        this instead of ``await page.content()``.

        ``_raw_server_html`` is set by the navigation helpers in
        ``wildcard.init_extract_all_page`` and by the headed-browser path in
        ``complete_paper_extraction.py`` before each main-page navigation.
        """
        raw = getattr(self, '_raw_server_html', None)
        if raw:
            return raw
        p = page or self.page
        if p is not None:
            try:
                return await p.content()
            except Exception:
                pass
        return ''

    def is_headed_run(self) -> bool:
        """Whether the page this handler was given belongs to a headed browser.

        A handler cannot ask Playwright this -- there is no ``headless`` flag
        on a Browser, and sniffing the user agent stopped working once Chrome's
        new headless mode began reporting an ordinary one. So the workflow pins
        the answer on the handler as ``_force_headed`` before ``extract_all``
        runs, the same way ``_landing_url`` and ``_raw_server_html`` are passed
        down.

        Anything that opens a browser of its own must route the answer through
        here rather than assume: a throwaway Chrome launched headless during a
        headed run is the most detectable browser we could present, and
        launching a window during a headless batch is the opposite nuisance.

        Defaults to False when nothing pinned it, which matches the standalone
        case -- a handler running without a workflow has no headed browser to
        belong to.
        """
        return bool(getattr(self, '_force_headed', False))

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
        """Run the complete publisher-specific extraction and return the shared workflow payload.

        Keys consumed by the workflow: ``metadata``, ``links``,
        ``fulltext_data``, ``journal_name``, and optionally ``access``.

        ``access`` is True unless the handler can see that the publisher has
        refused this article. False makes the workflow stop after saving the
        landing page and crossref.json -- no PDF, figures, supplements or
        markdown, all of which are gated the same way and would only burn the
        retry budget. Omit the key when the publisher gives no such signal;
        never guess, since a wrong False silently skips a paper that was
        actually available.
        """
        pass

    @abstractmethod
    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        """Format extracted data as Markdown"""
        pass

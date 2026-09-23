"""
Abstract base class for all publisher implementations
"""

from abc import ABC, abstractmethod
from pathlib import Path


class PublisherHandler(ABC):
    """Abstract base class for publisher-specific paper extraction"""

    #: Publisher token this handler serves, in the vocabulary
    #: ``orchestrator.detect_publisher_from_url`` returns ('iop', 'aps', ...).
    #:
    #: It exists for callers that hold a handler but cannot ask the
    #: orchestrator who it is -- ``wildcard`` is imported *by* the handlers,
    #: so importing the orchestrator back would close the tree's first import
    #: cycle. Empty by default, which keeps every handler that does not set it
    #: behaving exactly as before.
    PUBLISHER: str = ''

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

        ⚠️ A handler listed in ``RAW_HTML_PUBLISHERS`` never reaches
        ``page.content()``: it re-fetches the source with view-source, and if
        that fails too it gets ``''``. Reading the rendered DOM there would
        hand back a document whose formulas MathJax has already replaced --
        complete-looking output with every equation gone. Only handlers that
        were written against the rendered DOM keep that fallback.
        """
        raw = getattr(self, '_raw_server_html', None)
        if raw:
            return raw
        p = page or self.page
        if p is None:
            return ''

        # Imported here rather than at module scope: this is the rare path,
        # and base.py is imported by every handler in the tree.
        from core.utilities import RAW_HTML_PUBLISHERS, fetch_view_source_html  # noqa: F401

        if (self.PUBLISHER or '').lower() in RAW_HTML_PUBLISHERS:
            print("  ⚠️  未捕获到原始响应，改用 view-source 重取")
            try:
                source = await fetch_view_source_html(p)
            except Exception:
                source = ''
            if source:
                return source
            # ⛔ Stop here. A handler in RAW_HTML_PUBLISHERS parses the served
            # response; the rendered DOM is not a weaker version of that, it
            # is a different document -- MathJax has replaced the formulas,
            # so what comes out *looks* complete and has lost every equation.
            # An empty body is the honest answer, and the workflow already
            # renders "[... not found.]" for it.
            #
            # 📌 It is also very unlikely to contain anything: if neither the
            # capture nor a view-source re-fetch produced markup, the page
            # itself did not load. Cambridge and Wiley dropped their own
            # rendered-DOM last resorts for exactly this reason; this is the
            # same rule, one level down in the contract.
            print("  ⛔ view-source 也失败 —— 返回空正文，不读渲染后 DOM"
                  "（宁可空白，也不要一份公式已被替换、却看着完整的产出）")
            return ''

        # ⚠️ Through content_with_timeout, never a bare ``p.content()``.
        # Playwright's content() takes no timeout and is not covered by
        # set_default_timeout, so on a wedged renderer it neither succeeds nor
        # fails -- it simply never returns. Reported on IOP
        # 10.1088/2515-7647/ac9e2f: the run stopped at
        # "使用IOPHandler完整提取" with nothing after it. Every other
        # content() in the tree was wrapped when that hazard was catalogued;
        # this one, the contract's own last resort, was missed.
        from core.utilities import content_with_timeout
        return await content_with_timeout(p, what='get_page_html 回落')

    def captured_api(self, path_suffix: str) -> str:
        """A response body the *page itself* already fetched, or ''.

        The preload records what the article page requests while it loads, so
        an endpoint the page has already called does not need calling again.
        Measured on ScienceDirect 10.1016/j.cocom.2026.e01326: the page issues
        its own /sdfe/arp/pii/<PII>/body, and the handler was re-requesting the
        identical resource afterwards.

        ⚠️ Match on the path, not the URL -- these endpoints are signed per
        session, so the captured URL carries a different entitledToken than the
        one a handler would build. See core.utilities.captured_api_body.

        Returns '' whenever nothing was captured, so every caller must keep its
        own request as the fallback: capture is an optimisation and must never
        become a dependency.
        """
        return self.captured_api_entry(path_suffix)[0]

    def captured_api_entry(self, path_suffix: str) -> tuple:
        """``(body, url)`` for a captured response, or ``('', '')``.

        The URL answers questions the body cannot: SPIE's fulltext endpoint
        spells the content family in its path, so a handler can read which
        one the page used instead of inferring it from a meta tag.
        """
        captured = getattr(self, '_captured_api', None)
        if not captured:
            return '', ''
        from core.utilities import captured_api_entry
        return captured_api_entry(captured, path_suffix)

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

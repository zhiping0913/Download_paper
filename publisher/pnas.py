"""PNAS handler (pnas.org, DOI prefix 10.1073).

PNAS runs on **Atypon**, the same platform as ACM's digital library, and
serves the same markup: ``div.core-container`` holding ``section[id^=sec-]``,
floats wrapped in ``div.figure-wrap``, ``section#supplementary-materials``,
``section#bibliography``. So this handler is :class:`ACMHandler` with the
differences overridden rather than a second copy of the walker.

📌 What PNAS needed that ACM had not shown yet lives in **acm.py**, not
here: MathML formulas (PNAS ships no LaTeX source), a table rendered as a
picture, div-based lists (``role="list"``), and more than one
``section[role="doc-abstract"]``. None of that is PNAS-specific -- Atypon
serves it to whoever configures it that way, and ACM turned out to have the
multi-abstract case too ("Highlights"). Anything else discovered here
belongs there as well.

What is genuinely PNAS's own:

  * **The PDF needs a query string**: ``/doi/pdf/{doi}?download=true``.
    ``citation_pdf_url`` does not carry it, and without it the link opens the
    in-page reader instead of downloading.
  * **Back matter placement.** Data Availability and Acknowledgments sit in
    ``section#backmatter``, outside the body container, and the numbered
    footnotes live in ``section.core-article-notes`` -- inside the page's
    "Information & Authors" tab.
  * **Metadata comes from ``citation_*``**, which PNAS fills in completely,
    so there is no need to scrape the byline markup.
  * The section is called **Supporting Information**, not Supplemental
    Material.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from publisher.acm import ACMHandler


class PNASHandler(ACMHandler):
    """Full-text handler for Proceedings of the National Academy of Sciences."""

    PUBLISHER = 'pnas'

    SITE_BASE = 'https://www.pnas.org'

    #: PNAS's own name for the section (section#supplementary-materials's h2).
    SUPPLEMENTAL_HEADING = 'Supporting Information'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.SITE_BASE

    # ------------------------------------------------------------------
    # Links
    # ------------------------------------------------------------------

    async def get_pdf_url(self, doi: str) -> Optional[str]:
        """``/doi/pdf/{doi}?download=true``.

        ⚠️ The query string is load-bearing and ``citation_pdf_url`` does not
        carry it: without ``download=true`` the URL opens PNAS's in-page
        reader, the browser never fires a download event, and the whole retry
        budget burns on a viewer.
        """
        doi = (doi or self.doi or '').strip()
        if not doi:
            return None
        return f"{self.SITE_BASE}/doi/pdf/{doi}?download=true"

    # ------------------------------------------------------------------
    # Back matter
    # ------------------------------------------------------------------

    #: PNAS keeps these outside the body container, in <section id="backmatter">.
    BACK_MATTER_IDS = ('footnotes', 'appendix', 'data-availability',
                       'acknowledgments')

    @classmethod
    def _back_matter_nodes(cls, soup: BeautifulSoup) -> List:
        """The usual back matter, plus PNAS's article notes.

        📌 The numbered footnotes ("*", "†") are NOT in the body or in a
        section with an id -- they live in ``section.core-article-notes``,
        inside the page's "Information & Authors" tab. Without them the
        markers in the text point at nothing.
        """
        nodes = super()._back_matter_nodes(soup)
        notes = soup.find('section', class_='core-article-notes')
        if notes is not None:
            nodes.append(notes)
        return nodes

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _meta(soup: BeautifulSoup, name: str) -> str:
        tag = soup.find('meta', attrs={'name': name})
        return (tag.get('content') or '').strip() if tag else ''

    async def extract_metadata(self, page) -> dict:
        """Bibliographic metadata, all of it from ``citation_*``.

        PNAS declares every field we need in meta tags, so there is no reason
        to scrape the byline markup the way the ACM handler has to.
        """
        html = await self.get_page_html(page)
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')

        authors = [(m.get('content') or '').strip()
                   for m in soup.find_all('meta', attrs={'name': 'citation_author'})]
        date = self._meta(soup, 'citation_publication_date')
        year = ''
        match = re.search(r'\b(19|20|21)\d{2}\b', date)
        if match:
            year = match.group(0)

        pages = '-'.join(p for p in (self._meta(soup, 'citation_firstpage'),
                                     self._meta(soup, 'citation_lastpage')) if p)
        return {
            'title': self._meta(soup, 'citation_title') or self._extract_title(soup),
            'doi': self._meta(soup, 'citation_doi') or (self.doi or ''),
            'authors': [a for a in authors if a],
            'year': year,
            'journal': self._meta(soup, 'citation_journal_title'),
            'volume': self._meta(soup, 'citation_volume'),
            'issue': self._meta(soup, 'citation_issue'),
            'pages': pages,
            'abstract': self._extract_abstract(soup),
            'publisher': self._meta(soup, 'citation_publisher'),
        }

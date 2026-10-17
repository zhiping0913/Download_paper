"""University of Chicago Press handler (journals.uchicago.edu, DOI 10.1086).

UChicago Press runs on **Atypon**, the same platform as ACM, PNAS and the
other Literatum sites, and serves the same markup: ``div.core-container``
holding ``section[id^=sec-]``, floats in ``div.figure-wrap``,
``div[role=paragraph]`` for prose, ``div[role=doc-footnote]`` for notes,
``section#supplementary-materials``, ``div.biblioentry`` reference entries.
So this is :class:`ACMHandler` with the differences overridden, the way
:class:`PNASHandler` is -- not a fourth copy of the body walker.

📌 **Everything in the raw server response.** Both sample articles
(10.1086/429523, 10.1086/742128) carry body, tables, table notes, figures,
display formulas, the end notes and the supplement list in the HTML the
server sends, so this handler never needs the rendered DOM.

📌 What was not UChicago-specific went into **acm.py**, per the rule that
Atypon shapes belong with the Atypon walker: numbered back-matter ids
(``appendix-1`` … ``appendix-4``), picking the article's own
``bibliography-N`` over an appendix's nested one, numbering figures that
live in the back matter, stripping the per-float UI ("Download figure",
"View table as an image"), and not saving a table that also ships as a
picture. ACM and PNAS get those too.

What is genuinely UChicago's own:

  * **Metadata is Dublin Core, not ``citation_*``.** The page declares
    exactly two ``citation_`` tags (``citation_journal_title`` and nothing
    else useful) -- title, authors, date, DOI and pages all come from
    ``dc.*``. A handler written against ``citation_title`` here gets an
    article with no title.
  * **The PDF link is on the page**: ``a#downloadPdfUrl`` →
    ``/doi/pdf/{doi}?download=true``.
  * **Headed only.** Cloudflare sits in front of the site.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from publisher.acm import ACMHandler


class UChicagoHandler(ACMHandler):
    """Full-text handler for the University of Chicago Press."""

    PUBLISHER = 'uchicago'

    SITE_BASE = 'https://www.journals.uchicago.edu'

    #: The h2 of ``section#supplementary-materials``. ⚠️ The publisher is not
    #: consistent about it ("Supplementary Material" in 10.1086/429523,
    #: "Supplement" in 10.1086/742128), so the markdown prints one name
    #: rather than echoing whichever the page happened to use.
    SUPPLEMENTAL_HEADING = 'Supplementary Material'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.SITE_BASE

    # ------------------------------------------------------------------
    # Links
    # ------------------------------------------------------------------

    async def get_pdf_url(self, doi: str) -> Optional[str]:
        """``/doi/pdf/{doi}?download=true``, preferring the page's own link.

        The article page ships it as ``a#downloadPdfUrl``, already carrying
        ``?download=true`` -- which is the load-bearing part: without it the
        link opens Atypon's in-page reader, the browser never fires a
        download event, and the retry budget burns on a viewer (the same trap
        PNAS has).

        📌 There is also a ``/doi/pdfplus/{doi}`` rendition. The two are the
        same paper -- ``pdfplus`` is Atypon's "enhanced" view with live
        reference links -- and we take the plain ``pdf``: it is what the page
        itself offers through that button. This is a deliberate default, not
        a finding about which file is better.
        """
        doi = (doi or self.doi or '').strip()
        html = getattr(self, '_raw_server_html', '') or ''
        if html:
            soup = BeautifulSoup(html, 'html.parser')
            anchor = soup.find('a', id='downloadPdfUrl')
            href = (anchor.get('href') or '').strip() if anchor else ''
            if href:
                return self._absolute(href)
        if not doi:
            return None
        return f"{self.SITE_BASE}/doi/pdf/{doi}?download=true"

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _dc(soup: BeautifulSoup, name: str) -> List[str]:
        """Every ``<meta name="dc.X">`` value, in document order.

        Dublin Core repeats a name per value (one ``dc.Creator`` per author),
        so the caller decides whether it wants the first or all of them.
        """
        out = []
        for tag in soup.find_all('meta', attrs={'name': name}):
            value = (tag.get('content') or '').strip()
            if value:
                out.append(value)
        return out

    @classmethod
    def _dc_one(cls, soup: BeautifulSoup, name: str) -> str:
        values = cls._dc(soup, name)
        return values[0] if values else ''

    @staticmethod
    def _clean_author(name: str) -> str:
        """``'Diego \xa0San\xa0Mauro'`` → ``'Diego San Mauro'``.

        ⚠️ UChicago separates the parts of a name with **non-breaking
        spaces**, sometimes doubled with a normal one. Left alone they travel
        into ``metadata.json`` and the markdown byline, where a search for
        "Diego San Mauro" does not match.
        """
        return re.sub(r'\s+', ' ', (name or '').replace('\xa0', ' ')).strip()

    @classmethod
    def _dc_doi(cls, soup: BeautifulSoup) -> str:
        """The DOI out of ``dc.Identifier``.

        ⚠️ That name is used for **five different identifiers** on one page
        (an internal article number, the DOI, a stock number, the PMID and a
        price line), so "the first dc.Identifier" is not the DOI. There is
        also a ``publication_doi`` meta; this reads whichever looks like a
        DOI.
        """
        for value in cls._dc(soup, 'dc.Identifier') + cls._dc(soup, 'publication_doi'):
            if re.fullmatch(r'10\.\d{4,9}/\S+', value):
                return value
        return ''

    @classmethod
    def _enumeration(cls, soup: BeautifulSoup) -> tuple:
        """``('165', '5')`` from ``div.core-enumeration``.

        📌 Volume and issue are **not** in any meta tag here (``dc.Coverage``
        is "world"), so they come off the page's own enumeration line --
        "Volume 165 , Number 5". Without this, ``metadata.json`` would print
        them empty for every UChicago article, since the workflow takes those
        fields from the handler.
        """
        for el in soup.find_all(class_='core-enumeration'):
            text = el.get_text(' ', strip=True)
            match = re.search(r'Volume\s*(\S+?)\s*[,•]\s*Number\s*(\S+?)(?:\s|$|[,•])',
                              text)
            if match:
                return match.group(1), match.group(2)
        return '', ''

    @staticmethod
    def _pagination(soup: BeautifulSoup) -> str:
        """``'590-599'`` from ``div.core-pagination`` ("Pages : 590 - 599")."""
        el = soup.find(class_='core-pagination')
        if el is None:
            return ''
        match = re.search(r'(\d+)\s*[-–]\s*(\d+)', el.get_text(' ', strip=True))
        if match:
            return f"{match.group(1)}-{match.group(2)}"
        match = re.search(r'(\d+)', el.get_text(' ', strip=True))
        return match.group(1) if match else ''

    async def extract_metadata(self, page) -> dict:
        """Bibliographic metadata, out of the Dublin Core meta tags."""
        html = await self.get_page_html(page)
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')

        date = self._dc_one(soup, 'dc.Date')
        year_match = re.search(r'\b(19|20|21)\d{2}\b', date)

        keywords = self._dc_one(soup, 'dc.Subject')
        if not keywords:
            meta = soup.find('meta', attrs={'name': 'keywords'})
            keywords = (meta.get('content') or '') if meta else ''
        keyword_list = [k.strip() for k in re.split(r'[;,]', keywords) if k.strip()]

        # ⚠️ dc.Contributor is the *editors* ("Editor: Jonathan B. Losos"),
        # not the authors -- it must not be appended to the author list.
        authors = [self._clean_author(a) for a in self._dc(soup, 'dc.Creator')]

        volume, issue = self._enumeration(soup)

        return {
            'title': self._dc_one(soup, 'dc.Title') or self._extract_title(soup),
            'doi': self._dc_doi(soup) or (self.doi or ''),
            'authors': [a for a in authors if a],
            'year': year_match.group(0) if year_match else '',
            'journal': (self._dc_one(soup, 'citation_journal_title')
                        or self._extract_journal(soup)),
            'volume': volume,
            'issue': issue,
            'pages': self._pagination(soup),
            'keywords': keyword_list,
            'abstract': self._extract_abstract(soup),
            'publisher': self._dc_one(soup, 'dc.Publisher'),
        }

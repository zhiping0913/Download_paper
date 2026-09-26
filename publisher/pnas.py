"""PNAS handler (pnas.org, DOI prefix 10.1073).

PNAS runs on **Atypon**, the same platform as ACM's digital library, and
serves the same markup: ``div.core-container`` holding ``section[id^=sec-]``,
floats wrapped in ``div.figure-wrap``, ``section#supplementary-materials``,
``section#bibliography``. So this handler is :class:`ACMHandler` with the
differences overridden rather than a second copy of the walker.

What actually differs:

  * **Math is MathML**, not ``span.core-tex``. PNAS has no LaTeX source on
    the page at all -- 152 ``<math>`` elements in the sample article -- so
    the formulas go through the shared MathML→LaTeX converter.
  * **Two abstracts.** ``section#executive-summary-abstract`` (the
    "Significance" paragraph) sits beside ``section#abstract``; both are part
    of what a reader means by the abstract.
  * **A table can be a picture.** Table 1 of 10.1073/pnas.1522200113 is a
    JPEG with a note under it, while Tables S1–S3 are real ``<table>``
    markup, one of them carrying formulas.
  * **Lists are divs** (``role="list"`` / ``role="listitem"``), with the
    publisher's own "*i*)" labels.
  * **The PDF needs a query string**: ``/doi/pdf/{doi}?download=true``.
    Without it the link opens the reader.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString

from html_to_md_converter import mathml_to_latex_pandoc
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
    # Math (MathML, not LaTeX source)
    # ------------------------------------------------------------------

    @staticmethod
    def _latex_from_mathml(math_el) -> str:
        """LaTeX for one ``<math>``, or '' when it converts to nothing."""
        try:
            latex = mathml_to_latex_pandoc(str(math_el)) or ''
        except Exception as exc:
            print(f"  ⚠️  MathML 转换失败: {type(exc).__name__}: {exc}")
            return ''
        latex = latex.strip()
        # The shared converter hands back a display or inline wrapper
        # depending on the source; strip either, the caller re-wraps.
        for opener, closer in (('$$', '$$'), ('\\[', '\\]'),
                               ('\\(', '\\)'), ('$', '$')):
            if latex.startswith(opener) and latex.endswith(closer) and len(latex) > len(opener) + len(closer):
                latex = latex[len(opener):-len(closer)].strip()
                break
        return re.sub(r'\s+', ' ', latex).strip()

    @classmethod
    def _stash_inline_math(cls, fragment, formulas: List[str]) -> None:
        """Inline ``<math>`` → ``$…$`` token.

        ⚠️ Display formulas are left alone here: they are reached through
        ``div.display-formula`` by the body walk, and converting them inline
        too would print each one twice.
        """
        for math_el in fragment.find_all('math'):
            if math_el.find_parent('div', class_='display-formula') is not None:
                continue
            latex = cls._latex_from_mathml(math_el)
            if not latex:
                math_el.decompose()
                continue
            formulas.append(f"${latex}$")
            math_el.replace_with(f"DPMATH{len(formulas) - 1:04d}ZZ")

    @classmethod
    def _render_display_formula(cls, div) -> List[str]:
        """``div.display-formula`` → a ``$$`` block, with its label as a tag."""
        math_el = div.find('math')
        if math_el is None:
            return super()._render_display_formula(div)
        latex = cls._latex_from_mathml(math_el)
        if not latex:
            return []
        label_el = div.find('div', class_='label')
        label = label_el.get_text('', strip=True) if label_el else ''
        if label:
            latex += f"\\tag{{{cls._bare_label(label)}}}"
        return ['$$\n' + latex + '\n$$']

    # ------------------------------------------------------------------
    # Abstract: Significance + Abstract
    # ------------------------------------------------------------------

    @classmethod
    def _extract_abstract(cls, soup: BeautifulSoup) -> str:
        """Both abstract sections, in the order the page prints them.

        PNAS puts a plain-language "Significance" paragraph in
        ``section#executive-summary-abstract`` right above the abstract
        proper. Both carry ``role="doc-abstract"``; dropping the first one
        loses the part written for readers outside the field.
        """
        parts: List[str] = []
        for section_id in ('executive-summary-abstract', 'abstract'):
            section = soup.find('section', id=section_id)
            if section is None:
                continue
            heading = section.find(['h2', 'h3'])
            title = heading.get_text(' ', strip=True) if heading else ''
            paragraphs = [cls._inline_md(p.decode_contents())
                          for p in section.find_all('div', attrs={'role': 'paragraph'})]
            text = '\n\n'.join(p for p in paragraphs if p)
            if not text:
                continue
            # The workflow prints this under its own "## Abstract" heading, so
            # the section names go inline as bold leads rather than headings.
            parts.append(f"**{title}.** {text}" if title and section_id != 'abstract'
                         else text)
        if parts:
            return '\n\n'.join(parts)
        return super()._extract_abstract(soup)

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

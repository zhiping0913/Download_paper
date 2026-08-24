"""ACM Digital Library publisher handler (dl.acm.org, DOI prefix 10.1145).

**Abstract-only.** ACM Digital Library gates full text behind login for
almost all conference proceedings, so this handler is deliberately
minimal:

  * ``metadata`` — title, DOI, authors (+ affiliations + emails), year,
    journal/conference name, and abstract are extracted from the
    article landing page.
  * ``fulltext_data`` — the h2-section walk produces best-effort
    markdown for whatever ACM ships in the free landing page (Abstract
    is always present; References may or may not be; body / figures
    / supplemental are usually behind login and left unset).
  * ``links.pdf_url`` — always constructed as
    ``https://dl.acm.org/doi/pdf/{doi}``. Downloading may still 401 for
    paywalled papers; that's handled by the standard retry/skip logic
    in ``_download_all_resources``.
  * ``links.figure_urls`` / ``links.supplemental_urls`` — empty.
    Interfaces are kept so future full-text support can slot in
    without touching the extraction contract.

Headed-only: ACM fronts every request with Cloudflare bot protection
that hard-blocks headless Chromium. Do NOT add ``'acm'`` to
``HEADLESS_ACCESSIBLE_PUBLISHERS`` in ``complete_paper_extraction.py``.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString

from html_to_md_converter import (
    cleanup_markdown,
    convert_html_to_markdown,
    remove_newlines_in_paragraph,
)
from publisher.base import PublisherHandler
from publisher.wildcard import init_extract_all_page, set_actual_base_url


# H2 headings that live on the article landing page but describe UI /
# widgets rather than paper content. The h2 walker skips these.
_ACM_H2_SKIP = frozenset({
    'formats available',
    'recommendations',
    'comments',
    'information & contributors',
    'bibliometrics & citations',
    'view options',
    'figures',
    'tables',
    'media',
    'share',
    'export citations',
    'new citation alert added!',
    'add a citation alert',
    'footer',
    'acm is now open access',
})


class ACMHandler(PublisherHandler):
    """Abstract-only handler for ACM Digital Library (dl.acm.org).

    See module docstring for scope. Non-abstract getters return empty
    values on purpose — they exist to satisfy the ``PublisherHandler``
    contract and to leave a stub for future work.
    """

    ACM_BASE = 'https://dl.acm.org'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.ACM_BASE

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_title(soup: BeautifulSoup) -> str:
        # <h1> under <div class="core-container"> — only one on the page.
        h1 = soup.find('h1')
        if h1:
            return re.sub(r'\s+', ' ', h1.get_text(' ', strip=True)).strip()
        og = soup.find('meta', attrs={'property': 'og:title'})
        if og:
            return (og.get('content') or '').strip()
        return ''

    @staticmethod
    def _extract_doi_from_html(soup: BeautifulSoup) -> str:
        meta = soup.find('meta', attrs={'name': 'publication_doi'})
        if meta and meta.get('content'):
            return meta['content'].strip()
        return ''

    @classmethod
    def _extract_journal(cls, soup: BeautifulSoup) -> str:
        # <div class="core-self-citation"> reads
        #   "PASC '26: Platform for Advanced Scientific Computing Conference
        #    Article No.: 18, Pages 1 - 11 https://doi.org/10.1145/..."
        # The journal / proceedings name is everything before "Article No."
        core = soup.find('div', class_='core-self-citation')
        if not core:
            return ''
        text = core.get_text(' ', strip=True)
        text = re.sub(r'\s+', ' ', text)
        m = re.split(r'\s+Article\s+No\.?', text, maxsplit=1)
        return m[0].strip() if m else text

    @staticmethod
    def _extract_year(soup: BeautifulSoup) -> str:
        date_span = soup.find('span', class_='core-date-published')
        if date_span:
            m = re.search(r'\b(19|20|21)\d{2}\b', date_span.get_text())
            if m:
                return m.group(0)
        # Fallback: dc.date or citation_publication_date
        for name in ('citation_publication_date', 'dc.date', 'publication_date'):
            meta = soup.find('meta', attrs={'name': name})
            if meta and meta.get('content'):
                m = re.search(r'\b(19|20|21)\d{2}\b', meta['content'])
                if m:
                    return m.group(0)
        return ''

    @classmethod
    def _extract_authors(cls, soup: BeautifulSoup) -> Tuple[List[str], List[Dict[str, list]]]:
        """Extract author names + affiliations + emails from ACM markup.

        ACM wraps each author in ``<span property="author">`` containing
        ``<span property="givenName">`` and ``<span property="familyName">``
        for the visible link, and a ``<div class="dropBlock__holder">``
        popover with the full affiliation + email block. Iterate only
        the top-level author spans (skip the nested drop-holder ones,
        which would double-count).
        """
        names: List[str] = []
        detailed: List[Dict[str, list]] = []
        seen = set()

        for span in soup.find_all('span', attrs={'property': 'author'}):
            # Skip the inner spans that live inside a dropBlock__holder
            # (nested author markup for the popover) — the outer span
            # is the canonical one and its role is 'listitem'.
            if span.get('role') != 'listitem':
                continue

            given = span.find('span', attrs={'property': 'givenName'})
            family = span.find('span', attrs={'property': 'familyName'})
            name_parts = [p.get_text(strip=True) for p in (given, family) if p]
            name = ' '.join(name_parts).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)

            affiliations: List[str] = []
            emails: List[str] = []
            holder = span.find('div', class_='dropBlock__holder')
            if holder:
                for aff in holder.find_all('div', attrs={'property': 'affiliation'}):
                    aff_name = aff.find('span', attrs={'property': 'name'})
                    if aff_name:
                        # Text minus the trailing <a> email link
                        aff_copy = BeautifulSoup(str(aff_name), 'html.parser')
                        for a in aff_copy.find_all('a', attrs={'property': 'email'}):
                            a.decompose()
                        aff_text = re.sub(
                            r'\s+', ' ',
                            aff_copy.get_text(' ', strip=True),
                        ).strip()
                        if aff_text:
                            affiliations.append(aff_text)
                for a in holder.find_all('a', attrs={'property': 'email'}):
                    email = a.get_text(strip=True) or (a.get('href') or '').replace('mailto:', '')
                    email = email.strip()
                    if email and email not in emails:
                        emails.append(email)
            detailed.append({
                'author': name,
                'affiliations': affiliations,
                'emails': emails,
            })
        return names, detailed

    @classmethod
    def _extract_abstract(cls, soup: BeautifulSoup) -> str:
        """Return the abstract as markdown. Empty string if no abstract."""
        section = soup.find('section', attrs={'id': 'abstract'})
        if not section:
            # Fallback: any element with property="abstract"
            section = soup.find(attrs={'property': 'abstract'})
        if not section:
            return ''

        # Each ACM abstract paragraph is wrapped in <div role="paragraph">.
        # Feeding the div straight to pandoc emits a fenced-div wrapper
        # ("::: {role=\"paragraph\"}"). Wrap the div's INNER HTML in a
        # <p> tag so pandoc renders a plain paragraph instead.
        paragraphs: List[str] = []
        for p in section.find_all('div', attrs={'role': 'paragraph'}):
            md = cls._convert_paragraph_to_md('<p>' + p.decode_contents() + '</p>')
            if md:
                paragraphs.append(md)
        if paragraphs:
            return '\n\n'.join(paragraphs)
        # Fallback: strip <h2>Abstract</h2> then take rest as one block.
        section_copy = BeautifulSoup(str(section), 'html.parser')
        h2 = section_copy.find('h2')
        if h2:
            h2.decompose()
        return cls._convert_paragraph_to_md(str(section_copy)).strip()

    @classmethod
    def _convert_paragraph_to_md(cls, html_fragment: str) -> str:
        """Run an HTML fragment through the shared pandoc pipeline."""
        if not html_fragment:
            return ''
        md = convert_html_to_markdown(html_fragment)
        md = cleanup_markdown(md)
        md = remove_newlines_in_paragraph(md, '', 'p')
        return re.sub(r'\s+', ' ', md).strip()

    # ------------------------------------------------------------------
    # h2-section walker (abstract-only mode: best effort)
    # ------------------------------------------------------------------

    @classmethod
    def extract_article_text_from_html(cls, html_content: str) -> Tuple[str, str]:
        """Walk every non-skipped ``<h2>`` and render its content as markdown.

        Returns ``(abstract_md, body_md)``. In abstract-only mode the
        abstract is always populated when present; body_md contains any
        additional h2 sections we can salvage (Index Terms, References,
        etc.) but is not required to be complete — full-text sections
        are typically behind login on ACM.
        """
        if not html_content:
            return '', ''

        soup = BeautifulSoup(html_content, 'html.parser')

        # Abstract first — this is the only section we CARE about.
        abstract_md = cls._extract_abstract(soup)

        # Best-effort walk: iterate every top-level h2, skip the ones on the
        # blocklist, and render the enclosing <section> content as markdown.
        # Abstract is intentionally re-included so callers that use only
        # body_md still get the paper's text.
        seen_headings = set()
        body_parts: List[str] = []
        for h2 in soup.find_all('h2'):
            heading_text = re.sub(r'\s+', ' ', h2.get_text(' ', strip=True)).strip()
            if not heading_text:
                continue
            if heading_text.lower() in _ACM_H2_SKIP:
                continue
            if heading_text in seen_headings:
                continue
            seen_headings.add(heading_text)

            # Prefer the enclosing <section> as the content boundary;
            # fall back to sibling walking if the h2 isn't wrapped.
            section = h2.find_parent('section')
            content_html = ''
            if section is not None:
                # Rebuild without the <h2> so it doesn't render twice.
                section_copy = BeautifulSoup(str(section), 'html.parser')
                first_h2 = section_copy.find('h2')
                if first_h2:
                    first_h2.decompose()
                content_html = str(section_copy)
            else:
                buf = []
                for sib in h2.next_siblings:
                    if getattr(sib, 'name', None) in ('h2', 'h1'):
                        break
                    buf.append(str(sib))
                content_html = ''.join(buf)

            section_md = cls._convert_paragraph_to_md(content_html)
            if not section_md:
                continue
            body_parts.append(f"## {heading_text}\n\n{section_md}")

        body_md = '\n\n'.join(body_parts).strip()
        return abstract_md, body_md

    # ------------------------------------------------------------------
    # PublisherHandler contract
    # ------------------------------------------------------------------

    async def extract_metadata(self, page) -> dict:
        try:
            html = await page.content()
        except Exception:
            html = ''
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')

        title = self._extract_title(soup)
        doi = self._extract_doi_from_html(soup) or (self.doi or '')
        year = self._extract_year(soup)
        journal = self._extract_journal(soup)
        abstract_md = self._extract_abstract(soup)
        authors, detailed = self._extract_authors(soup)

        corr_emails: List[str] = []
        for entry in detailed:
            for email in entry.get('emails', []):
                if email not in corr_emails:
                    corr_emails.append(email)

        return {
            'title': title,
            'doi': doi,
            'authors': authors,
            'author_with_affiliations': detailed,
            'year': year,
            'journal': journal,
            'abstract': abstract_md,
            'corresponding_author_emails': corr_emails,
        }

    async def get_pdf_url(self, doi: str) -> Optional[str]:
        """Construct the canonical ACM PDF URL — always ``/doi/pdf/{doi}``."""
        doi = (doi or self.doi or '').strip()
        if not doi:
            return None
        return f"{self.ACM_BASE}/doi/pdf/{doi}"

    async def get_supplemental_url(self, doi: str) -> Optional[str]:
        # Stub — see module docstring. Abstract-only for now.
        return None

    async def extract_references(self, html: str) -> list:
        # Stub — abstract-only mode. Return an empty list so downstream
        # code that expects an iterable doesn't NPE.
        return []

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def get_fulltext_url(self, page) -> str:
        try:
            return page.url or ''
        except Exception:
            return f"{self.ACM_BASE}/doi/{self.doi}" if self.doi else ''

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        """Run the ACM handler through the unified publisher contract.

        Abstract-only: figure_urls / supplemental_urls are always empty.
        """
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'ACMHandler'
        )
        doi = self.doi
        set_actual_base_url(self, page)

        try:
            metadata = await self.extract_metadata(page)
            metadata['doi'] = doi or metadata.get('doi', '')

            try:
                fulltext_html = await page.content()
            except Exception:
                fulltext_html = ''

            # References are intentionally NOT extracted — full-text
            # (including the ref list DOM) is usually gated behind login
            # on ACM. Empty list satisfies downstream expectations.
            metadata.setdefault('references', [])

            pdf_url = await self.get_pdf_url(doi)

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': {},          # abstract-only
                    'supplemental_urls': [],    # abstract-only
                    'supplemental_descriptions': {},
                },
                'fulltext_data': fulltext_html,
                'journal_name': 'acm',
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

    # ------------------------------------------------------------------
    # Markdown rendering
    # ------------------------------------------------------------------

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        title = metadata.get('title') or 'ACM Article'
        md_parts: List[str] = [f"# {title}", '']

        # Authors with affiliations
        detailed = metadata.get('author_with_affiliations') or []
        if detailed:
            md_parts.extend(['## Authors', ''])
            for entry in detailed:
                name = entry.get('author', '')
                affs = entry.get('affiliations') or []
                emails = entry.get('emails') or []
                md_parts.append(f"- **{name}**")
                for aff in affs:
                    md_parts.append(f"  - {aff}")
                for em in emails:
                    md_parts.append(f"  - {em}")
            md_parts.append('')
        elif metadata.get('authors'):
            md_parts.extend(['## Authors', ''])
            md_parts.append(', '.join(metadata['authors']))
            md_parts.append('')

        # Publication metadata block
        md_parts.extend(['## Publication', ''])
        for key, label in (
            ('journal', '**Journal:**'),
            ('year', '**Year:**'),
            ('doi', '**DOI:**'),
        ):
            val = metadata.get(key)
            if val:
                md_parts.append(f"{label} {val}")
                md_parts.append('')

        # Abstract (the whole point of this handler)
        abstract = (metadata.get('abstract') or '').strip()
        body_md = ''

        # article_text is the raw fulltext_html; try to salvage extra
        # h2 sections from it.
        abstract_from_body = ''
        if isinstance(article_text, str) and article_text.strip():
            if article_text.lstrip().startswith('<'):
                abstract_from_body, body_md = self.extract_article_text_from_html(article_text)
            else:
                body_md = article_text.strip()
        if not abstract:
            abstract = abstract_from_body

        md_parts.extend(['---', '', '## Abstract', ''])
        md_parts.append(abstract or '[No abstract available.]')
        md_parts.append('')

        # Best-effort extra sections (Index Terms, References ...) —
        # explicitly labelled so the reader knows they're not full text.
        if body_md:
            md_parts.extend([
                '---',
                '',
                '## Additional sections',
                '',
                '*ACM support is abstract-only — the sections below are best-effort '
                'extracts from the article landing page.*',
                '',
                body_md,
                '',
            ])

        return '\n'.join(md_parts).rstrip() + '\n'

"""
Oxford University Press (OUP) book handler.

Walks the TOC of an OUP book landing page (academic.oup.com/book/<id>),
visits each chapter via its tocLink, hands the chapter HTML to OupHandler
for content extraction, and aggregates the results into one Markdown
document covering the whole book.

Chapter DOIs (e.g. 10.1093/acprof:oso/9780199299805.003.0011) are
normalized to the book DOI (.001.0001) at __init__ time so a single
chapter request still pulls the whole book per the OUP-book spec.
"""

import re
from typing import List, Dict, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from publisher.base import PublisherHandler
from publisher.oup import OupHandler
from publisher.wildcard import (
    init_extract_all_page,
    set_actual_base_url,
)


class OupBookHandler(PublisherHandler):
    """Handler for OUP / Oxford Academic books (academic.oup.com/book/…)."""

    # Matches the ISBN-suffix portion of an OUP book/chapter DOI:
    # ``10.1093/acprof:oso/9780199299805.003.0011`` → ``.003.0011``.
    # The book itself uses ``.001.0001`` as the suffix.
    _DOI_CHAPTER_SUFFIX_RE = re.compile(r'\.\d{3}\.\d{4}$')

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        self.original_doi = doi
        if doi:
            book_doi = self._DOI_CHAPTER_SUFFIX_RE.sub('.001.0001', doi)
            if book_doi != doi:
                print(f"  ℹ️  Chapter DOI detected: {doi} → normalized to book DOI: {book_doi}")
                doi = book_doi
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = 'https://academic.oup.com'

    # ------------------------------------------------------------------
    # Book-level metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_book_metadata(html_content: str) -> dict:
        """Pull title/authors/year/ISBN/publisher from <meta citation_*> tags."""
        if not html_content:
            return {}
        soup = BeautifulSoup(html_content, 'html.parser')
        meta = {}
        authors = []
        for tag in soup.find_all('meta'):
            name = tag.get('name', '')
            content = (tag.get('content') or '').strip()
            if not name or not content:
                continue
            if name == 'citation_title':
                meta['title'] = re.sub(r'\s+', ' ', content)
            elif name == 'citation_doi':
                meta['doi'] = content
            elif name == 'citation_author':
                authors.append(content)
            elif name == 'citation_publisher':
                meta['publisher'] = content
            elif name == 'citation_isbn':
                # citation_isbn appears multiple times — keep the first.
                meta.setdefault('isbn', content)
            elif name == 'citation_publication_date':
                meta['publication_date'] = content
                year_m = re.search(r'(\d{4})', content)
                if year_m:
                    meta['year'] = year_m.group(1)
        if authors:
            meta['authors'] = authors
        return meta

    # ------------------------------------------------------------------
    # TOC walk
    # ------------------------------------------------------------------

    @classmethod
    def _extract_chapter_links(cls, html_content: str) -> List[Dict]:
        """Return a list of ``{'href': ..., 'title': ...}`` for each chapter
        link found in the TOC (``<a class="tocLink">``)."""
        if not html_content:
            return []
        soup = BeautifulSoup(html_content, 'html.parser')
        links = []
        seen = set()
        for a in soup.find_all('a', class_='tocLink'):
            href = (a.get('href') or '').strip()
            if not href or href in seen:
                continue
            seen.add(href)
            title_span = a.find('span', class_='tocLink-title')
            if title_span:
                title = title_span.get_text(' ', strip=True)
            else:
                title = a.get_text(' ', strip=True)
            title = re.sub(r'\s+', ' ', title).strip()
            links.append({'href': href, 'title': title})
        return links

    # ------------------------------------------------------------------
    # Per-chapter extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_chapter_pdf_url(html_content: str, base_url: str) -> Optional[str]:
        """Find the ``<a class="article-pdfLink">`` on a chapter page."""
        if not html_content:
            return None
        soup = BeautifulSoup(html_content, 'html.parser')
        anchor = soup.find('a', class_=re.compile(r'article-pdfLink'))
        if not anchor:
            return None
        href = (anchor.get('href') or '').strip()
        if not href:
            return None
        if href.startswith('http'):
            return href
        return urljoin(base_url, href)

    async def _extract_one_chapter(self, page, chapter_link: Dict, ch_idx: int) -> Optional[Dict]:
        """Navigate to a chapter URL, run it through OupHandler, return the
        per-chapter payload that ``convert_to_markdown`` consumes."""
        full_url = urljoin(self.actual_base_url, chapter_link['href'])
        print(f"  📖 Chapter {ch_idx}: {chapter_link['title'][:60]}")
        print(f"     {full_url}")
        try:
            await page.goto(full_url, wait_until='domcontentloaded', timeout=60000)
            try:
                await page.wait_for_load_state('networkidle', timeout=15000)
            except Exception:
                pass
        except Exception as e:
            print(f"     ⚠️  navigate failed: {e}")
            return None

        try:
            chapter_html = await page.content()
        except Exception as e:
            print(f"     ⚠️  page.content() failed: {e}")
            return None

        # Pull chapter DOI from <meta citation_doi> on the chapter page.
        soup = BeautifulSoup(chapter_html, 'html.parser')
        doi_meta = soup.find('meta', {'name': 'citation_doi'})
        chapter_doi = doi_meta.get('content', '').strip() if doi_meta else ''

        # Reuse OupHandler's extractors against the chapter HTML — saves
        # duplicating any of the chapter-specific parsing logic.
        ch_handler = OupHandler(page=page, doi=chapter_doi or chapter_link['href'])
        ch_handler.actual_base_url = self.actual_base_url

        meta = OupHandler._extract_metadata_from_html_meta(chapter_html)
        abstract_md, _ = OupHandler.extract_article_text_from_html(chapter_html)
        text_refs, raw_dois = OupHandler.extract_references_from_html(chapter_html)
        footnotes = OupHandler.extract_footnotes_from_html(chapter_html)
        figure_urls = OupHandler.extract_figures_from_html(chapter_html)
        pdf_url = self._extract_chapter_pdf_url(chapter_html, self.actual_base_url)

        chapter_meta = {
            'title': meta.get('title') or chapter_link['title'],
            'authors': meta.get('authors', []),
            'author_with_affiliations': meta.get('author_with_affiliations', []),
            'corresponding_author_emails': [],
            'abstract': abstract_md,
            'journal': meta.get('journal', ''),
            'doi': chapter_doi,
            'year': meta.get('year'),
            'volume': meta.get('volume'),
            'issue': meta.get('issue'),
            'pages': meta.get('pages'),
            'publication_date': meta.get('publication_date'),
            'references': text_refs,
            '_ref_dois': raw_dois,
            'footnotes': footnotes,
        }

        return {
            'index': ch_idx,
            'title': chapter_meta['title'],
            'doi': chapter_doi,
            'url': full_url,
            'metadata': chapter_meta,
            'fulltext_html': chapter_html,
            'figure_urls': figure_urls,
            'pdf_url': pdf_url,
        }

    # ------------------------------------------------------------------
    # PublisherHandler contract
    # ------------------------------------------------------------------

    async def extract_metadata(self, page) -> dict:
        html_content = ''
        if page is not None:
            try:
                html_content = await page.content()
            except Exception:
                html_content = ''
        meta = self._extract_book_metadata(html_content)
        return {
            'title': meta.get('title') or 'Oxford Academic Book',
            'authors': meta.get('authors', []),
            'author_with_affiliations': [],
            'corresponding_author_emails': [],
            'abstract': '',
            'journal': meta.get('publisher') or 'Oxford University Press',
            'publisher': meta.get('publisher'),
            'doi': meta.get('doi') or self.doi,
            'year': meta.get('year'),
            'publication_date': meta.get('publication_date'),
            'isbn': meta.get('isbn'),
            'references': [],
        }

    async def get_fulltext_url(self, page) -> str:
        if page is not None:
            try:
                return page.url
            except Exception:
                pass
        return f"https://doi.org/{self.doi}" if self.doi else None

    async def get_pdf_url(self, doi: str) -> str:
        return None

    async def get_supplemental_url(self, doi: str) -> str:
        return None

    async def extract_references(self, html: str) -> list:
        return []

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        """Walk the book TOC, visit every chapter, and aggregate the
        per-chapter payloads into the shared workflow contract."""
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'OupBookHandler'
        )
        doi = self.doi
        set_actual_base_url(self, page)

        try:
            # If we landed on a chapter URL via the original DOI, jump to the
            # book landing page so the TOC is in scope.
            if self.original_doi and self.original_doi != self.doi:
                book_url = f"https://doi.org/{self.doi}"
                print(f"  → 重新导航到书籍页面: {book_url}")
                try:
                    await page.goto(book_url, wait_until='domcontentloaded', timeout=60000)
                    try:
                        await page.wait_for_load_state('networkidle', timeout=15000)
                    except Exception:
                        pass
                except Exception as e:
                    print(f"  ⚠️  导航到书籍页面失败: {e}")

            try:
                book_html = await page.content()
            except Exception:
                book_html = ''

            metadata = await self.extract_metadata(page)
            metadata['doi'] = doi

            chapter_links = self._extract_chapter_links(book_html)
            print(f"  📚 TOC: 找到 {len(chapter_links)} 个章节链接")

            chapters: List[Dict] = []
            all_figure_urls: Dict[str, Dict] = {}
            all_supp_urls: List[str] = []
            all_supp_descs: Dict[str, str] = {}

            for ch_idx, link in enumerate(chapter_links, 1):
                ch_payload = await self._extract_one_chapter(page, link, ch_idx)
                if ch_payload is None:
                    continue
                chapters.append(ch_payload)

                # Aggregate figure urls — OUP graphic numbers are globally
                # unique within a book, so a chapter prefix on the key is
                # belt-and-braces against the unlikely collision.
                for fig_id, fig_info in ch_payload['figure_urls'].items():
                    num_match = re.search(r'(\d+)$', fig_id)
                    fig_num = num_match.group(1) if num_match else fig_id
                    prefixed_key = f"ch{ch_idx:02d}_fig_{fig_num}"
                    all_figure_urls[prefixed_key] = fig_info

                if ch_payload.get('pdf_url'):
                    safe_title = re.sub(r'[<>:"/\\|?*]+', '_',
                                        ch_payload['title'] or f'chapter_{ch_idx}')
                    label = f"ch{ch_idx:02d}--{safe_title}.pdf"
                    all_supp_urls.append(ch_payload['pdf_url'])
                    all_supp_descs[ch_payload['pdf_url']] = label

            metadata['_chapters'] = chapters
            metadata['additional_doi'] = [c['doi'] for c in chapters if c.get('doi')]

            return {
                'metadata': metadata,
                'links': {
                    # Books have no full-volume PDF — chapter PDFs ride along
                    # as supplemental downloads instead.
                    'pdf_url': None,
                    'figure_urls': all_figure_urls,
                    'supplemental_urls': all_supp_urls,
                    'supplemental_descriptions': all_supp_descs,
                },
                'fulltext_data': book_html,
                'journal_name': 'oup_book',
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

    def convert_to_markdown(self, metadata: dict, fulltext_data=None, **kwargs) -> str:
        """Assemble the per-chapter markdowns into one book-level document."""
        title = metadata.get('title') or 'Oxford Academic Book'
        md_parts: List[str] = [f"# {title}", '']

        # Authors (book-level)
        if metadata.get('authors'):
            md_parts.extend(['## Authors', ''])
            for author in metadata['authors']:
                md_parts.append(f"- {author}")
            md_parts.append('')

        # Publication metadata block
        md_parts.extend(['## Publication', ''])
        if metadata.get('publisher'):
            md_parts.extend([f"**Publisher:** {metadata['publisher']}", ''])
        elif metadata.get('journal'):
            md_parts.extend([f"**Publisher:** {metadata['journal']}", ''])
        if metadata.get('year'):
            md_parts.extend([f"**Year:** {metadata['year']}", ''])
        if metadata.get('isbn'):
            md_parts.extend([f"**ISBN:** {metadata['isbn']}", ''])
        if metadata.get('doi'):
            md_parts.extend([f"**DOI:** {metadata['doi']}", ''])
        md_parts.extend(['---', ''])

        # Table of Contents — one line per chapter
        chapters = metadata.get('_chapters') or []
        if chapters:
            md_parts.extend(['## Table of Contents', ''])
            for ch in chapters:
                title_text = ch.get('title', '')
                doi_text = f" — DOI: {ch['doi']}" if ch.get('doi') else ''
                md_parts.append(f"{ch['index']}. {title_text}{doi_text}")
            md_parts.append('')

        # Each chapter rendered via OupHandler.convert_to_markdown — we keep
        # the per-chapter sectioning (Abstract / Article Text / References /
        # Footnotes / Supporting Info) for free and just demote each
        # chapter's leading "# Title" so it nests cleanly under a chapter
        # heading.
        figure_filenames = kwargs.get('figure_filenames') or {}
        supplemental_downloads = kwargs.get('supplemental_downloads') or []

        for ch in chapters:
            md_parts.extend(['---', '',
                             f"# Chapter {ch['index']}: {ch.get('title') or ''}",
                             ''])
            if ch.get('doi'):
                md_parts.extend([f"**DOI:** {ch['doi']}", ''])

            # Run the chapter through the existing chapter-level renderer.
            # The OupHandler instance is throwaway — it just needs to know
            # the actual base URL for any relative-link resolution.
            ch_handler = OupHandler(doi=ch.get('doi'))
            ch_handler.actual_base_url = self.actual_base_url

            chapter_md = ch_handler.convert_to_markdown(
                ch['metadata'],
                ch.get('fulltext_html'),
                add_figure_refs=bool(figure_filenames),
                figure_filenames=figure_filenames,
                figure_urls=ch.get('figure_urls', {}),
                # Per-chapter supplemental/footnote sections are already
                # handled inside OupHandler.convert_to_markdown — we don't
                # want the book-level supplemental list duplicated under
                # every chapter.
                supplemental_urls=[],
                supplemental_descriptions={},
                supplemental_downloads=[],
            )

            # Drop the leading "# Title\n\n" that OupHandler emits — we
            # already wrote a "# Chapter N: ..." heading above it.
            chapter_md = re.sub(r'^# [^\n]*\n+', '', chapter_md, count=1)

            md_parts.extend([chapter_md, ''])

        # Book-level supplemental block — one entry per chapter PDF that
        # actually downloaded.
        if supplemental_downloads:
            md_parts.extend(['---', '', '## Chapter PDFs', ''])
            for dl in supplemental_downloads:
                md_parts.append(f"- {dl}")
            md_parts.append('')

        return '\n'.join(md_parts)


__all__ = ['OupBookHandler']

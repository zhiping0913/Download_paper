"""
Springer Book Publisher Handler

Handles extraction from Springer book chapters and full books.
For chapter DOIs (e.g., 10.1007/978-981-15-2381-6_2), normalizes to book DOI.
"""

from typing import Optional, Dict, List
from bs4 import BeautifulSoup
from publisher.base import PublisherHandler
from publisher.wildcard import init_extract_all_page, set_actual_base_url, convert_html_fragment_to_markdown
from publisher.nature import NatureHandler


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
            # Extract DOI prefix before the last underscore (e.g., 10.1007/978-981-15-2381-6 from 10.1007/978-981-15-2381-6_2)
            book_doi = doi.rsplit('_', 1)[0]
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
        # Initialize page and managed resources using shared function
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'SpringerBookHandler'
        )

        # Get the actual page URL for correct base_url resolution
        set_actual_base_url(self, page)

        try:
            # Extract page HTML content
            try:
                fulltext_html = await page.content()
            except Exception:
                fulltext_html = ''

            # Extract key sections
            metadata = await self.extract_metadata(page)
            metadata['doi'] = doi

            # Extract Overview section
            overview_text = self._extract_section_paragraphs(fulltext_html, 'Overview')
            if overview_text:
                metadata['_overview'] = overview_text

            # Extract About this book section
            about_text = self._extract_section_paragraphs(fulltext_html, 'About this book')
            if about_text:
                metadata['_about'] = about_text

            # Extract PDF download link
            pdf_url = self._extract_pdf_url(fulltext_html)

            # Extract chapter PDFs from Table of Contents
            supplemental_urls, supplemental_descriptions = self._extract_toc_chapter_pdfs(fulltext_html)

            # Extract chapters information (links and DOIs)
            chapters_info = self._extract_chapters_info(fulltext_html)
            metadata['_chapters_info'] = chapters_info

            # Extract content from each chapter with a DOI
            chapters_content = []
            all_chapter_figures = {}
            global_fig_counter = 0

            if chapters_info:
                print(f"  📚 Found {len(chapters_info)} chapters, extracting content...")
                for idx, chapter_info in enumerate(chapters_info, 1):
                    if chapter_info.get('doi'):
                        print(f"    📖 Chapter {idx}: {chapter_info.get('title', 'Unknown')}")
                        chapter_content = await self._extract_chapter_content(page, chapter_info['doi'])
                        if chapter_content:
                            chapter_content['_chapter_info'] = chapter_info
                            chapters_content.append(chapter_content)

                            # Collect figures from each chapter with unique numbering
                            chapter_figures = chapter_content.get('links', {}).get('figure_urls', {})
                            if chapter_figures:
                                chapter_fig_mapping = {}
                                for fig_key, fig_url in chapter_figures.items():
                                    global_fig_counter += 1
                                    # Create unique key: ch_X_figY where X is chapter, Y is global counter
                                    prefixed_key = f"ch{idx}_fig{global_fig_counter}"
                                    all_chapter_figures[prefixed_key] = fig_url
                                    chapter_fig_mapping[fig_key] = prefixed_key

                                # Store mapping for convert_to_markdown
                                chapter_content['_figure_mapping'] = chapter_fig_mapping
                        else:
                            print(f"      ⚠️  Failed to extract content for chapter {idx}")

            metadata['_chapters_content'] = chapters_content

            # Return extraction results
            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': all_chapter_figures,
                    'supplemental_urls': supplemental_urls,
                    'supplemental_descriptions': supplemental_descriptions,
                },
                'fulltext_data': fulltext_html,
                'journal_name': 'springer_book',
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

    async def extract_metadata(self, page) -> dict:
        """Extract metadata from Springer book page"""
        # TODO: Implement metadata extraction
        return {}

    async def get_fulltext_url(self, page) -> str:
        """Get URL for full article text (not applicable for Springer books)"""
        return None

    async def get_pdf_url(self, doi: str) -> str:
        """Get PDF URL from DOI (extracted from page HTML)"""
        # PDF is extracted from HTML in extract_all
        return None

    async def get_supplemental_url(self, doi: str) -> str:
        """Get supplemental materials URL (handled in extract_all)"""
        return None

    async def extract_references(self, html: str) -> list:
        """Extract references from Springer book chapter"""
        if not html:
            return []

        soup = BeautifulSoup(html, 'html.parser')
        references = []

        # Look for references section
        ref_section = soup.find('section', {'data-title': 'References'})
        if not ref_section:
            return []

        # Extract individual references
        ref_items = ref_section.find_all('li')
        for item in ref_items:
            text = item.get_text(' ', strip=True)
            if text:
                references.append(text)

        return references

    async def get_figures(self, json_data: dict) -> dict:
        """Extract figure URLs and captions (handled in extract_all)"""
        return {}

    def _extract_section_paragraphs(self, html_content: str, section_title: str) -> str:
        """Extract paragraphs from a specific section by data-title attribute.

        Args:
            html_content: HTML page content
            section_title: Value of data-title attribute to search for (e.g., "Overview", "About this book")

        Returns:
            Concatenated text from all <p> tags in the section
        """
        soup = BeautifulSoup(html_content, 'html.parser')

        # Find section by data-title attribute
        section = soup.find('section', {'data-title': section_title})
        if not section:
            return ""

        # Extract all paragraphs within the section
        paragraphs = []
        for p in section.find_all('p'):
            text = p.get_text(' ', strip=True)
            if text:
                paragraphs.append(text)

        return '\n\n'.join(paragraphs)

    def _extract_chapters_info(self, html_content: str) -> List[Dict]:
        """Extract chapter information from Table of Contents.

        Returns list of dicts with:
        - title: Chapter title
        - url: Chapter page URL (if available)
        - doi: Chapter DOI (extracted from URL)
        - pdf_url: PDF download link
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        chapters_info = []

        # Find Table of contents section
        toc_section = soup.find('section', {'data-title': 'Table of contents'})
        if not toc_section:
            return []

        # Find all chapter list items
        chapters = toc_section.find_all('li', {'data-test': 'chapter'})

        for chapter in chapters:
            chapter_info = {}

            # Extract chapter title
            heading = chapter.find('h3', class_='app-card-open__heading')
            if heading:
                chapter_info['title'] = heading.get_text(' ', strip=True)

            # Look for chapter page link (either in heading or elsewhere)
            chapter_link = chapter.find('a', href=lambda x: x and '/chapter/' in x)
            if chapter_link:
                href = chapter_link.get('href', '')
                # Extract DOI from URL like "/chapter/10.1007/978-981-15-2381-6_1"
                if href:
                    chapter_info['url'] = href
                    # Extract DOI from href
                    parts = href.split('/')
                    if parts[-1]:
                        chapter_info['doi'] = parts[-1]  # e.g., "10.1007/978-981-15-2381-6_1"

            # Find PDF download link
            pdf_link = chapter.find('a', class_='c-pdf-chapter-download__link')
            if pdf_link:
                pdf_href = pdf_link.get('href', '')
                if pdf_href:
                    if pdf_href.startswith('//'):
                        chapter_info['pdf_url'] = 'https:' + pdf_href
                    elif not pdf_href.startswith('http'):
                        chapter_info['pdf_url'] = self.actual_base_url + pdf_href
                    else:
                        chapter_info['pdf_url'] = pdf_href

            if chapter_info.get('title'):  # Only add if we have at least a title
                chapters_info.append(chapter_info)

        return chapters_info

    async def _extract_chapter_content(self, page, chapter_doi: str) -> Optional[Dict]:
        """Extract content from a single chapter using NatureHandler.

        Args:
            page: Playwright page object (already in the browser context)
            chapter_doi: Full chapter DOI (e.g., "10.1007/978-981-15-2381-6_1")

        Returns:
            dict with chapter content including metadata, figures, and fulltext
        """
        try:
            # Create a NatureHandler for this chapter
            handler = NatureHandler(page=page, doi=chapter_doi)

            # Navigate to chapter page via DOI
            await page.goto(f"https://doi.org/{chapter_doi}", wait_until='domcontentloaded', timeout=60000)
            try:
                await page.wait_for_load_state('networkidle', timeout=15000)
            except Exception:
                pass

            # Extract chapter content using NatureHandler
            chapter_result = await handler.extract_all(page=page, doi=chapter_doi)
            return chapter_result

        except Exception as e:
            print(f"  ⚠️  Failed to extract chapter {chapter_doi}: {e}")
            return None

    def _extract_toc_chapter_pdfs(self, html_content: str) -> tuple:
        """Extract all chapter PDF links from Table of Contents section.

        Finds <section data-title="Table of contents">, then extracts each chapter's
        PDF link from <li data-test="chapter"> elements.

        Returns:
            tuple: (supplemental_urls, supplemental_descriptions)
                - supplemental_urls: List of chapter PDF URLs
                - supplemental_descriptions: Dict mapping filenames to chapter titles
        """
        soup = BeautifulSoup(html_content, 'html.parser')

        supplemental_urls = []
        supplemental_descriptions = {}

        # Find Table of contents section
        toc_section = soup.find('section', {'data-title': 'Table of contents'})
        if not toc_section:
            return [], {}

        # Find all chapter list items
        chapters = toc_section.find_all('li', {'data-test': 'chapter'})
        if not chapters:
            return [], {}

        for chapter in chapters:
            # Extract chapter title from heading
            heading = chapter.find('h3', class_='app-card-open__heading')
            if not heading:
                continue

            # Get chapter title (could be text or within an <a> tag)
            chapter_title = heading.get_text(' ', strip=True)
            if not chapter_title:
                continue

            # Find PDF download link in the chapter
            pdf_link = chapter.find('a', class_='c-pdf-chapter-download__link')
            if not pdf_link:
                continue

            href = pdf_link.get('href', '')
            if not href:
                continue

            # Convert relative URL to absolute
            if href.startswith('//'):
                url = 'https:' + href
            elif not href.startswith('http'):
                url = self.actual_base_url + href
            else:
                url = href

            # Extract filename from URL for description mapping
            from pathlib import Path
            filename = Path(href).name or f"chapter_{len(supplemental_urls) + 1}.pdf"

            supplemental_urls.append(url)
            supplemental_descriptions[filename] = chapter_title

        return supplemental_urls, supplemental_descriptions

    def _extract_pdf_url(self, html_content: str) -> Optional[str]:
        """Extract PDF download URL from page.

        Looks for <div class="c-pdf-download u-clear-both"> containing the PDF link.
        """
        soup = BeautifulSoup(html_content, 'html.parser')

        # Find PDF download section
        pdf_div = soup.find('div', class_='c-pdf-download')
        if not pdf_div:
            return None

        # Find the link within
        link = pdf_div.find('a')
        if not link:
            return None

        href = link.get('href', '')
        if not href:
            return None

        # Convert relative URLs to absolute
        if href.startswith('//'):
            href = 'https:' + href
        elif not href.startswith('http'):
            href = self.actual_base_url + href

        return href

    def convert_to_markdown(self, metadata: dict, fulltext_data=None,
                          add_figure_refs: bool = False,
                          figure_filenames: dict = None, **kwargs) -> str:
        """Convert extracted Springer book data to Markdown

        Organizes content by chapter, including book overview and each chapter's
        abstract, body, and figures.

        Args:
            metadata: Book metadata dict (contains _overview, _about, _chapters_content)
            fulltext_data: HTML content (not used for Springer books)
            add_figure_refs: If True, add figure references
            figure_filenames: Mapping of figure number to downloaded local filename

        Returns:
            Markdown formatted text
        """
        md_content = ""

        # ===== Book Title =====
        title = metadata.get('title') or "Springer Book"
        md_content += f"# {title}\n\n"

        # ===== Authors and Publication Info =====
        if metadata.get('author_with_affiliations'):
            md_content += "## Authors\n\n"
            for item in metadata['author_with_affiliations']:
                author = item['author']
                affiliations = item['affiliations']
                if affiliations:
                    md_content += f"- {author}\n  {', '.join(affiliations)}\n"
                else:
                    md_content += f"- {author}\n"
            md_content += "\n"

        # ===== DOI and Publication =====
        if metadata.get('doi'):
            md_content += f"**DOI**: {metadata['doi']}\n\n"

        if metadata.get('journal'):
            md_content += f"**Publisher**: {metadata['journal']}\n"
        if metadata.get('year'):
            md_content += f"**Year**: {metadata['year']}\n"
        if metadata.get('volume'):
            md_content += f"**Volume**: {metadata['volume']}\n"
        md_content += "\n"

        # ===== Book Overview =====
        if metadata.get('_overview'):
            md_content += "## Overview\n\n"
            md_content += metadata['_overview'] + "\n\n"

        # ===== About This Book =====
        if metadata.get('_about'):
            md_content += "## About This Book\n\n"
            md_content += metadata['_about'] + "\n\n"

        # ===== Table of Contents =====
        chapters_content = metadata.get('_chapters_content', [])
        if chapters_content:
            md_content += "## Table of Contents\n\n"
            for idx, chapter in enumerate(chapters_content, 1):
                chapter_info = chapter.get('_chapter_info', {})
                chapter_title = chapter_info.get('title', f'Chapter {idx}')
                chapter_doi = chapter_info.get('doi', '')
                md_content += f"{idx}. {chapter_title}"
                if chapter_doi:
                    md_content += f" (DOI: {chapter_doi})"
                md_content += "\n"
            md_content += "\n"

        # ===== Chapters =====
        if chapters_content:
            md_content += "## Chapters\n\n"

            for idx, chapter in enumerate(chapters_content, 1):
                chapter_info = chapter.get('_chapter_info', {})
                chapter_title = chapter_info.get('title', f'Chapter {idx}')
                chapter_doi = chapter_info.get('doi', '')

                # Chapter heading
                md_content += f"### Chapter {idx}: {chapter_title}\n"
                if chapter_doi:
                    md_content += f"**DOI**: {chapter_doi}\n"
                md_content += "\n"

                # Chapter metadata
                chapter_metadata = chapter.get('metadata', {})

                # Chapter abstract
                if chapter_metadata.get('abstract'):
                    md_content += "#### Abstract\n\n"
                    md_content += chapter_metadata['abstract'] + "\n\n"

                # Chapter body (from fulltext_data extraction)
                if chapter.get('fulltext_data'):
                    md_content += "#### Content\n\n"
                    chapter_html = chapter['fulltext_data']
                    chapter_md = convert_html_fragment_to_markdown(chapter_html)
                    if chapter_md:
                        md_content += chapter_md + "\n\n"

                # Chapter figures
                chapter_figures = chapter.get('links', {}).get('figure_urls', {})
                figure_mapping = chapter.get('_figure_mapping', {})
                if chapter_figures:
                    md_content += "#### Figures\n\n"
                    for fig_key, fig_url in chapter_figures.items():
                        mapped_key = figure_mapping.get(fig_key, fig_key)
                        if figure_filenames and mapped_key in figure_filenames:
                            md_content += f"- **{fig_key}**: ![{fig_key}]({figure_filenames[mapped_key]})\n"
                        else:
                            md_content += f"- **{fig_key}**: {fig_url}\n"
                    md_content += "\n"

                md_content += "---\n\n"

        return md_content


__all__ = ['SpringerBookHandler']

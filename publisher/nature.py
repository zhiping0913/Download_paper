"""
Nature Journal Publisher Implementation
Handles extraction from Nature and Nature-family journals (Nature Physics, Nature Materials, etc.)
"""

from publisher.base import PublisherHandler
from core.network_capture import setup_response_capture
import re
import json
from pathlib import Path
from typing import Optional, Dict, List
import asyncio
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from html_to_md_converter import cleanup_markdown, convert_html_to_markdown, mathml_to_latex_pandoc
from playwright.async_api import async_playwright

from publisher.wildcard import (
    convert_html_fragment_to_markdown,
    extract_abstract_with_fallbacks,
    find_generic_article_body,
    format_as_bibtex,
    format_citation_as_text,
    generate_bibtex_key,
    parse_citation_reference_string,
    prepare_mathjax_html_fragment,
    set_actual_base_url,
)


class NatureHandler(PublisherHandler):
    """Handler for Nature and Springer Nature journals"""

    def __init__(self, journal_name: str = 'nature', page=None, captured_data_dir=None, doi: str = None):
        """
        Initialize Nature handler

        Args:
            journal_name: Journal name (nature, nature_physics, nature_materials, etc.)
        """
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.journal_name = journal_name

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        """Execute complete extraction flow for Nature articles

        Args:
            page: Playwright page object (should already be navigated to DOI)
            doi: DOI of the paper
            captured: Optional dict with already-captured network data from setup_network_capture()

        Returns:
            dict with keys: 'metadata', 'links', 'fulltext_data', 'journal_name'
            where 'links' contains: 'pdf_url', 'figure_urls', 'supplemental_urls'
        """
        doi = doi or self.doi
        if doi is None:
            raise ValueError("NatureHandler.extract_all() requires a DOI")

        page = page or self.page
        managed_playwright = None
        managed_browser = None
        managed_context = None

        if page is None:
            print("  ✓ NatureHandler未收到page，使用无头浏览器访问")
            managed_playwright = await async_playwright().start()
            managed_browser = await managed_playwright.chromium.launch(headless=True)
            managed_context = await managed_browser.new_context(accept_downloads=True)
            page = await managed_context.new_page()
            self.configure(page=page, doi=doi)

            if captured is None:
                captured = self.setup_network_capture(page, doi)

            await page.goto(f"https://doi.org/{doi}", wait_until='domcontentloaded', timeout=60000)
            try:
                await page.wait_for_load_state('networkidle', timeout=15000)
            except:
                pass
        else:
            self.configure(page=page, doi=doi)

        # Get the actual page URL for correct base_url resolution
        set_actual_base_url(self, page)

        try:
            # 1. Extract metadata
            metadata = await self.extract_metadata(page)
            metadata['doi'] = doi

            # 2. Extract figures
            figure_urls = await self.get_figures(page, metadata)
            # Convert figure format to match APS format: {fig_id: {'url': '...', 'caption': '...'}}
            figure_urls_formatted = {}
            for fig_id, fig_data in figure_urls.items():
                figure_urls_formatted[fig_id] = {
                    'url': fig_data.get('url'),
                    'caption': fig_data.get('caption', '')
                }

            # 3. Extract references
            references, refs_raw = await self.extract_references(page)
            metadata['references'] = references
            metadata['_refs_raw'] = refs_raw

            # 4. Get supplemental materials links
            try:
                supplemental_urls, supplemental_descriptions = await self.get_supplemental_links(page)
            except:
                supplemental_urls = []
                supplemental_descriptions = {}

            # 5. Build links dict
            links = {
                'pdf_url': await self.get_pdf_url(page),  # May be None if PDF not accessible
                'figure_urls': figure_urls_formatted,
                'supplemental_urls': supplemental_urls,
                'supplemental_descriptions': supplemental_descriptions
            }

            # 6. Capture article HTML for fulltext (Nature doesn't have JSON API like APS)
            try:
                fulltext_html = await page.content()
            except:
                fulltext_html = None

            # 6.5 Fetch inline table pages and convert to markdown
            table_data = {}
            if fulltext_html:
                table_infos = self._extract_table_infos(fulltext_html)
                if table_infos:
                    print(f"  📊 发现 {len(table_infos)} 个行内表格，正在获取...")
                for table_id, caption, href in table_infos:
                    table_page_html = await self._fetch_table_page(page, href)
                    if table_page_html:
                        table_md = self._convert_table_html_to_md(table_page_html)
                        if table_md:
                            # Use caption text as key for matching in convert_to_markdown
                            table_key = caption
                            table_data[table_key] = {'caption': caption, 'markdown': table_md}
                            print(f"    ✓ {caption}")

            links['table_data'] = table_data

            # 7. Save HTML to captured_data if captured_data was set up
            if fulltext_html and captured is not None:
                try:
                    # captured dict should have been set up by setup_network_capture()
                    # Save HTML to the captured data directory
                    print("  ✓ HTML已在网络监听中保存")
                except:
                    pass

            return {
                'metadata': metadata,
                'links': links,
                'fulltext_data': fulltext_html,  # Store HTML instead of JSON for Nature
                'journal_name': self.journal_name
            }
        finally:
            if managed_context is not None:
                try:
                    await managed_context.close()
                except:
                    pass
            if managed_browser is not None:
                try:
                    await managed_browser.close()
                except:
                    pass
            if managed_playwright is not None:
                try:
                    await managed_playwright.stop()
                except:
                    pass
            if managed_context is not None:
                self.page = None

    def setup_network_capture(self, page=None, doi: str = None):
        """Set up network event listener to capture responses

        Call this before or after page navigation, so it captures network traffic.
        Returns a dict that will be populated with captured data.

        Args:
            page: Playwright page object
            doi: DOI for organizing output files

        Returns:
            dict that will be populated with captured data
        """
        page = page or self.page
        doi = doi or self.doi
        if page is None:
            raise ValueError("NatureHandler.setup_network_capture() requires a Playwright page")
        if doi is None:
            raise ValueError("NatureHandler.setup_network_capture() requires a DOI")

        self.configure(page=page, doi=doi)

        output_dir = self.captured_data_dir or Path("captured_data") / doi.replace('/', '_')
        captured = {
            'json_responses': [],
            'document': None,
            'documents': [],
            'timeline': [],
            'html': None,              # Save page HTML
        }

        def on_document(response, html, entry, captured):
            captured['html'] = html

        return setup_response_capture(
            page,
            output_dir,
            captured=captured,
            on_document=on_document,
        )

    async def extract_metadata(self, page) -> dict:
        """Extract metadata from Nature article page

        Extracts from:
        - Meta tags (citation_*, dc.*, prism.* prefixes)
        - JSON-LD structured data
        - HTML DOM elements

        Returns:
            Dictionary with extracted metadata
        """
        metadata = {
            'title': None,
            'authors': [],
            'author_emails': [],
            'abstract': None,
            'journal': None,
            'year': None,
            'volume': None,
            'issue': None,
            'pages': None,
            'doi': None,
            'author_with_affiliations': [],
            'corresponding_author_emails': [],
            'references': [],
            'image': [],
        }

        print("  🔍 Extracting metadata from Nature article...")

        # Extract all meta tags
        meta_data = await page.evaluate("""() => {
            const data = {};
            document.querySelectorAll('meta').forEach(meta => {
                const name = meta.getAttribute('name') || meta.getAttribute('property') || '';
                const content = meta.getAttribute('content') || '';
                if (name && content) {
                    data[name] = content;
                }
            });
            return data;
        }""")

        # Map meta tags to metadata fields
        metadata['title'] = meta_data.get('citation_title') or meta_data.get('dc.title')
        metadata['journal'] = meta_data.get('citation_journal_title', 'Nature')
        metadata['doi'] = (meta_data.get('citation_doi') or meta_data.get('prism.doi', '').replace('doi:', ''))

        # Parse publication date (format: 2026/04/22 or 2026-04-22)
        pub_date = meta_data.get('citation_online_date', '')
        if pub_date:
            # Handle both / and - separators
            date_parts = pub_date.replace('-', '/').split('/')
            if len(date_parts) >= 1:
                metadata['year'] = date_parts[0]

        # Get first author from meta tag
        if meta_data.get('citation_author'):
            metadata['authors'].append(meta_data['citation_author'])

        print(f"  ✅ Title: {metadata['title'][:60] if metadata['title'] else 'N/A'}...")
        print(f"  ✅ Journal: {metadata['journal']}")
        print(f"  ✅ DOI: {metadata['doi']}")

        # Extract JSON-LD for non-abstract metadata such as image URLs.
        json_ld_data = await page.evaluate("""() => {
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            for (let script of scripts) {
                try {
                    const data = JSON.parse(script.textContent);
                    const entity = data.mainEntity || data;
                    if (entity && (entity.description || entity.image)) {
                        return entity;
                    }
                } catch (e) {}
            }
            return null;
        }""")

        try:
            html_content = await page.content()
            metadata['abstract'] = self.extract_abstract_from_html_content(html_content)
        except Exception as e:
            print(f"  ⚠️  Abstract HTML extraction failed: {str(e)[:80]}")

        if not metadata['abstract'] and json_ld_data and 'description' in json_ld_data:
            metadata['abstract'] = json_ld_data['description']
            print(f"  ⚠️  Abstract fallback from JSON-LD: {metadata['abstract'][:60]}...")

        if json_ld_data and json_ld_data.get('image'):
            images = json_ld_data['image']
            if isinstance(images, str):
                images = [images]
            metadata['image'] = [img for img in images if isinstance(img, str) and img]
            print(f"  ✅ JSON-LD images: {len(metadata['image'])}")

        if json_ld_data:
            self.extract_authors_from_json_ld(json_ld_data, metadata)

        return metadata

    def extract_authors_from_json_ld(self, json_ld_data: dict, metadata: dict) -> None:
        """Extract author names, affiliations, and emails from JSON-LD author entries."""
        authors_data = json_ld_data.get('author') or []
        if isinstance(authors_data, dict):
            authors_data = [authors_data]
        if not isinstance(authors_data, list):
            return

        authors = []
        author_with_affiliations = []
        emails = []

        for author_data in authors_data:
            if isinstance(author_data, str):
                name = self.clean_text(author_data)
                affiliations = []
                email = None
            elif isinstance(author_data, dict):
                name = self.clean_text(author_data.get('name'))
                affiliations = self.extract_affiliations_from_json_ld(author_data.get('affiliation'))
                email = self.clean_text(author_data.get('email'))
            else:
                continue

            if not name:
                continue

            authors.append(name)
            author_entry = {
                'author': name,
                'affiliations': affiliations,
            }
            if email:
                author_entry['email'] = email
                emails.append(email)
            author_with_affiliations.append(author_entry)

        if authors:
            metadata['authors'] = authors
            metadata['author_with_affiliations'] = author_with_affiliations
            metadata['author_emails'] = emails
            metadata['corresponding_author_emails'] = emails
            print(f"  ✅ Authors found from JSON-LD: {len(authors)}")
            if emails:
                print(f"  ✅ Author emails found from JSON-LD: {len(emails)}")

    def extract_affiliations_from_json_ld(self, affiliation_data) -> List[str]:
        """Extract readable affiliation strings from JSON-LD affiliation entries."""
        if not affiliation_data:
            return []
        if isinstance(affiliation_data, (str, dict)):
            affiliation_data = [affiliation_data]
        if not isinstance(affiliation_data, list):
            return []

        affiliations = []
        seen = set()
        for affiliation in affiliation_data:
            if isinstance(affiliation, str):
                values = [affiliation]
            elif isinstance(affiliation, dict):
                values = []
                name = affiliation.get('name')
                address = affiliation.get('address')
                address_name = address.get('name') if isinstance(address, dict) else address

                if address_name:
                    values.append(address_name)
                elif name:
                    values.append(name)
            else:
                continue

            for value in values:
                value = self.clean_text(value)
                if value and value not in seen:
                    seen.add(value)
                    affiliations.append(value)

        return affiliations

    def clean_text(self, text) -> str:
        """Normalize text extracted from JSON-LD."""
        if text is None:
            return ""
        text = re.sub(r'\s+', ' ', unescape(str(text))).strip()
        return re.sub(r'\s+([),.;:])', r'\1', text)

    async def get_fulltext_url(self, page) -> Optional[str]:
        """Nature doesn't have a separate fulltext API, content is on the page itself"""
        return page.url

    async def get_pdf_url(self, page) -> Optional[str]:
        """Find PDF download URL

        Nature articles typically have a PDF button/link that needs to be located
        """
        print("  🔍 Looking for PDF download link...")

        actual_base_url = self.actual_base_url

        # Look for PDF download link - try multiple selectors
        selectors = [
            'a[href*=".pdf"]',
            'a[title*="PDF"]',
            '[class*="pdf-download"]',
            'a[href*="pdf"]',
            'button:has-text("PDF")',
            '[data-test*="pdf"]'
        ]

        for selector in selectors:
            try:
                pdf_link = await page.query_selector(selector)
                if pdf_link:
                    href = await pdf_link.get_attribute('href')
                    if href:
                        if not href.startswith('http'):
                            href = urljoin(actual_base_url, href)
                        print(f"  ✅ Found PDF: {href[:80]}...")
                        return href
            except:
                continue

        print("  ⚠️  PDF link not found (may require subscription)")
        return None

    async def get_supplemental_links(self, page) -> tuple:
        """Find Nature supplementary material file links.

        Searches for any section with title containing: supplementary, supporting,
        supplemental, extended (case-insensitive). Returns links from all matching sections.
        """
        print("  🔍 Looking for supplementary materials...")

        actual_base_url = self.actual_base_url

        candidates = await page.evaluate("""() => {
            const links = [];
            const keywords = ['supplementary', 'supporting', 'supplemental', 'extended'];

            // Strategy 1: Find sections with matching title keywords
            const sections = document.querySelectorAll('section[data-title]');
            sections.forEach(section => {
                const title = (section.getAttribute('data-title') || '').toLowerCase();
                const isSupplementary = keywords.some(keyword => title.includes(keyword));
                if (isSupplementary) {
                    section.querySelectorAll('a[href]').forEach(link => {
                        const href = link.getAttribute('href') || '';
                        const label = (link.textContent || '').replace(/\\s+/g, ' ').trim();
                        links.push({href, label, section_title: section.getAttribute('data-title')});
                    });
                }
            });

            // Strategy 2: Direct selectors (fallback)
            const selectors = [
                'a[data-test="supp-info-link"]',
                'a[href*="static-content.springer.com/esm"]',
                'a[href*="/esm/"]',
                'a[href*="MOESM"]'
            ];

            document.querySelectorAll(selectors.join(',')).forEach(link => {
                const href = link.getAttribute('href') || '';
                const label = (link.textContent || '').replace(/\\s+/g, ' ').trim();
                // Check if not already added via section search
                const isDuplicate = links.some(l => l.href === href && l.label === label);
                if (!isDuplicate) {
                    links.push({href, label, section_title: 'Direct link'});
                }
            });

            return links;
        }""")

        supplemental_urls = []
        supplemental_descriptions = {}
        seen = set()
        section_sources = {}  # Track which section each URL came from

        for item in candidates:
            href = (item.get('href') or '').strip()
            if not href:
                continue

            url = urljoin(actual_base_url, href)
            url_lower = url.lower()
            if 'support.nature.com' in url_lower:
                continue
            if not (
                'static-content.springer.com/esm' in url_lower
                or '/esm/' in url_lower
            ):
                continue

            if url in seen:
                continue

            seen.add(url)
            supplemental_urls.append(url)

            filename = Path(urlparse(url).path).name
            label = item.get('label') or ''
            if filename and label:
                supplemental_descriptions[filename] = label

            # Track which section this came from
            section_title = item.get('section_title', 'Unknown')
            if url not in section_sources:
                section_sources[url] = section_title

        if supplemental_urls:
            print(f"  ✅ Found supplementary materials: {len(supplemental_urls)}")
            # Group and display by section
            sections_found = {}
            for url in supplemental_urls:
                section = section_sources.get(url, 'Unknown')
                if section not in sections_found:
                    sections_found[section] = []
                sections_found[section].append(url)

            for section, urls in sections_found.items():
                print(f"     📋 From '{section}': {len(urls)} link(s)")
                for url in urls[:3]:
                    print(f"        - {url[:100]}...")
                if len(urls) > 3:
                    print(f"        ... and {len(urls) - 3} more")
        else:
            print("  ⚠️  Supplementary materials link not found")

        return supplemental_urls, supplemental_descriptions

    async def get_supplemental_url(self, page) -> Optional[str]:
        """Find the first supplementary materials link."""
        supplemental_urls, _ = await self.get_supplemental_links(page)
        return supplemental_urls[0] if supplemental_urls else None

    async def extract_references(self, page):
        """Parse references from citation_reference meta tags.

        Returns:
            (bibtex_list, raw_strings_list)
        """
        print("  🔍 Extracting references...")

        raw_refs = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('meta[name="citation_reference"]'))
                .map(meta => meta.getAttribute('content') || '')
                .filter(content => content.trim().length > 0);
        }""")

        bibtex_refs = [
            self.format_citation_reference(ref)
            for ref in raw_refs
            if ref and ref.strip()
        ]

        if bibtex_refs:
            print(f"  ✅ References found from citation_reference meta tags: {len(bibtex_refs)}")
        else:
            print("  ⚠️  No references found")

        return bibtex_refs, raw_refs

    def format_citation_reference(self, citation_reference: str) -> str:
        """Format Nature citation_reference content as a standard BibTeX entry.

        Delegates to wildcard.parse_citation_reference_string for the shared
        citation_reference → BibTeX pipeline.
        """
        return parse_citation_reference_string(citation_reference)

    def extract_paragraphs_from_html_content(self, html_content: str) -> List[str]:
        """Extract paragraph, equation, and heading HTML blocks from Nature main-content.

        Falls back to generic article body extraction for non-Nature publishers
        (IOP, Springer, Elsevier) routed through the Nature handler.
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        main_content_div = soup.find('div', {'class': 'main-content'})

        if not main_content_div:
            main_content_div = soup.find('div', {'class': 'main-content', 'data-nosnippet': ''})

        if not main_content_div:
            main_content_div = find_generic_article_body(soup)

        if not main_content_div:
            print("  ⚠️  main-content div not found")
            return []

        main_content_html = str(main_content_div)

        paragraph_matches = list(re.finditer(r'<p[^>]*>(.*?)</p>', main_content_html, re.DOTALL))
        equation_matches = list(
            re.finditer(
                r'<div[^>]*class="c-article-equation"[^>]*>(.*?)</div>\s*</div>',
                main_content_html,
                re.DOTALL,
            )
        )
        heading_matches = list(re.finditer(r'<h([2-6])[^>]*>(.*?)</h\1>', main_content_html, re.DOTALL))
        table_matches = list(re.finditer(
            r'<div[^>]*class="c-article-table"[^>]*>(.*?)</figure>\s*</div>',
            main_content_html,
            re.DOTALL,
        ))

        ordered_items = []
        for match in paragraph_matches:
            ordered_items.append((match.start(), match.group(0)))
        for match in equation_matches:
            ordered_items.append((match.start(), match.group(0)))
        for match in heading_matches:
            ordered_items.append((match.start(), match.group(0)))
        for match in table_matches:
            ordered_items.append((match.start(), match.group(0)))

        ordered_items.sort(key=lambda item: item[0])
        items = [content for _, content in ordered_items]

        print(f"  ✅ Main content blocks: {len(paragraph_matches)} paragraphs, {len(equation_matches)} equations, {len(heading_matches)} headings, {len(table_matches)} tables")
        return items

    def extract_paragraphs_from_html(self, html_file: str) -> List[str]:
        """Extract paragraph and equation HTML blocks from a Nature HTML file."""
        with open(html_file, 'r', encoding='utf-8') as f:
            html_content = f.read()
        return self.extract_paragraphs_from_html_content(html_content)

    # ------------------------------------------------------------------
    # Table page fetching and conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_table_infos(html_content: str) -> list:
        """Extract (table_id, caption, url) tuples from main article HTML."""
        soup = BeautifulSoup(html_content, 'html.parser')
        table_infos = []
        for table_div in soup.find_all('div', class_='c-article-table'):
            table_id = table_div.get('id', '')
            figcaption = table_div.find('figcaption')
            caption = figcaption.get_text(' ', strip=True) if figcaption else ''
            caption = re.sub(r'\s+', ' ', caption).strip()
            table_link = table_div.find('a', href=re.compile(r'/tables?/\d+'))
            href = table_link.get('href', '') if table_link else ''
            if href:
                table_infos.append((table_id, caption, href))
        return table_infos

    async def _fetch_table_page(self, page, table_url: str) -> str:
        """Navigate to a table page and return its HTML content."""
        full_url = table_url if table_url.startswith('http') else self.actual_base_url + table_url
        try:
            await page.goto(full_url, wait_until='domcontentloaded', timeout=30000)
            try:
                await page.wait_for_load_state('networkidle', timeout=10000)
            except Exception:
                pass
            return await page.content()
        except Exception as e:
            print(f"  ⚠️  Failed to fetch table page {full_url}: {e}")
            return ''

    @classmethod
    def _convert_table_html_to_md(cls, table_page_html: str) -> str:
        """Convert a Nature table page to a markdown table with footnotes.

        Pre-processes MathJax in table cells and footer (data-mathml → LaTeX)
        before converting to markdown via pandoc.
        """
        soup = BeautifulSoup(table_page_html, 'html.parser')
        table = soup.find('table')
        if not table:
            return ''

        # Replace MathJax spans with placeholder tokens to avoid pandoc
        # mangling the LaTeX, then restore after conversion.
        math_placeholders = {}

        for math_span in table.find_all('span', class_='MathJax_SVG'):
            mathml = math_span.get('data-mathml', '')
            latex = mathml_to_latex_pandoc(mathml) if mathml else ''
            if latex:
                placeholder = f"MATHPLACEHOLDER{len(math_placeholders):03d}MATHEND"
                math_placeholders[placeholder] = latex
                # Replace the outer mathjax-tex wrapper too, keeping only the LaTeX
                wrapper = math_span.find_parent('span', class_='mathjax-tex')
                if wrapper:
                    wrapper.replace_with(BeautifulSoup(placeholder, 'html.parser'))
                else:
                    math_span.replace_with(BeautifulSoup(placeholder, 'html.parser'))

        # Remove any remaining MathJax_Preview or mathjax-tex spans
        for tag in table.find_all('span', class_=['MathJax_Preview', 'mathjax-tex']):
            tag.decompose()

        # Convert <table> to markdown via pandoc
        table_html = str(table)
        import pypandoc
        md = pypandoc.convert_text(table_html, 'md', format='html', extra_args=['--wrap=none'])
        md = md.strip()

        # Restore LaTeX placeholders
        for placeholder, latex in math_placeholders.items():
            md = md.replace(placeholder, latex)

        # Collapse whitespace-only rows (pandoc often inserts empty table rows)
        md = re.sub(r'\|\s+\|\s+(\|\s+)*\n', '', md)

        # ---- Extract table footer / annotations ----
        footer_md = ''
        footer_div = soup.find('div', class_='c-article-table-footer')
        if footer_div:
            # Remove the class attribute so pandoc doesn't emit fenced div markers
            del footer_div['class']
            # Remove the wrapper <div> tags entirely — we only want the content
            footer_contents = ''.join(str(c) for c in footer_div.children)

            # Pre-process MathJax in footer the same way
            footer_placeholders = {}
            footer_soup = BeautifulSoup(footer_contents, 'html.parser')
            for math_span in footer_soup.find_all('span', class_='MathJax_SVG'):
                mathml = math_span.get('data-mathml', '')
                latex = mathml_to_latex_pandoc(mathml) if mathml else ''
                if latex:
                    placeholder = f"MATHPLACEHOLDER{len(footer_placeholders):03d}MATHEND"
                    footer_placeholders[placeholder] = latex
                    wrapper = math_span.find_parent('span', class_='mathjax-tex')
                    if wrapper:
                        wrapper.replace_with(BeautifulSoup(placeholder, 'html.parser'))
                    else:
                        math_span.replace_with(BeautifulSoup(placeholder, 'html.parser'))

            for tag in footer_soup.find_all('span', class_=['MathJax_Preview', 'mathjax-tex']):
                tag.decompose()

            footer_html = str(footer_soup)
            footer_md = pypandoc.convert_text(footer_html, 'md', format='html', extra_args=['--wrap=none'])
            footer_md = footer_md.strip()

            for placeholder, latex in footer_placeholders.items():
                footer_md = footer_md.replace(placeholder, latex)

            footer_md = re.sub(r'\s+', ' ', footer_md).strip()
            if footer_md:
                footer_md = '\n\n' + footer_md + '\n'

        return '\n' + md + '\n' + footer_md

    # ------------------------------------------------------------------
    # Paragraph conversion
    # ------------------------------------------------------------------

    def convert_paragraph(self, paragraph_html: str) -> str:
        """Convert one HTML paragraph/equation/heading block to cleaned Markdown."""
        try:
            stripped = paragraph_html.strip()

            # Handle headings: extract text and format as markdown heading
            h_match = re.match(r'<h([2-6])[^>]*>(.*?)</h\1>', stripped, re.DOTALL)
            if h_match:
                level = int(h_match.group(1))
                heading_text = BeautifulSoup(h_match.group(2), 'html.parser').get_text(' ', strip=True)
                heading_text = re.sub(r'\s+', ' ', heading_text).strip()
                if heading_text:
                    prefix = '#' * (level + 1)  # h2 → ###, h3 → ####
                    return f"{prefix} {heading_text}"
                return ""

            # Handle inline table placeholders — extract caption and link
            if 'c-article-table' in paragraph_html:
                table_soup = BeautifulSoup(paragraph_html, 'html.parser')
                figcaption = table_soup.find('figcaption')
                caption = figcaption.get_text(' ', strip=True) if figcaption else ''
                caption = re.sub(r'\s+', ' ', caption).strip()
                table_link = table_soup.find('a', href=re.compile(r'/tables?/\d+'))
                link_href = table_link.get('href', '') if table_link else ''
                if link_href and not link_href.startswith('http'):
                    link_href = self.actual_base_url + link_href
                lines = [f"\n**{caption}**"]
                if link_href:
                    lines.append(f"[View full table]({link_href})")
                return "\n".join(lines) + "\n"

            is_equation = 'c-article-equation' in paragraph_html
            md = convert_html_to_markdown(paragraph_html)
            md = cleanup_markdown(md)

            if is_equation:
                md = re.sub(r'^:::\s*\{#Equ\d+[^}]*\}\s*', '', md.strip())
                md = re.sub(r'\s*:::\s*$', '', md)
                md = re.sub(r'\$\\\\\((\d+)\\\\\)\$', r'(\1)', md)

            md = re.sub(r'\s+', ' ', md)
            return md.strip()
        except Exception as e:
            print(f"  ⚠️  Paragraph conversion error: {str(e)[:50]}")
            return ""

    def extract_abstract_from_html_content(self, html_content: str) -> str:
        """Extract and convert Nature abstract paragraphs from article HTML.

        Falls back to generic selectors for non-Nature publishers (IOP, Springer, etc.).
        """
        soup = BeautifulSoup(html_content, 'html.parser')

        abstract_section = soup.find('section', {'data-title': 'Abstract'})
        if not abstract_section:
            abstract_section = soup.find('div', {'class': 'c-article-section__content'})
        if not abstract_section:
            # Try shared fallback
            return extract_abstract_with_fallbacks(
                soup,
                paragraph_converter=lambda html: convert_html_fragment_to_markdown(html, "NATUREMATH"),
            ) or ""

        abstract_content = abstract_section.find('div', {'class': 'c-article-section__content'})
        if not abstract_content:
            print("  ⚠️  Abstract content not found")
            return ""

        abstract_paragraphs = abstract_content.find_all('p')
        if not abstract_paragraphs:
            print("  ⚠️  Abstract paragraphs not found")
            return ""

        converted_paragraphs = []
        for idx, abstract_p in enumerate(abstract_paragraphs, 1):
            md = self.convert_paragraph(str(abstract_p))
            if md:
                converted_paragraphs.append(md)
                print(f"  ✓ Abstract paragraph {idx}: {len(md)} chars")

        abstract_md = "\n\n".join(converted_paragraphs).strip()
        if abstract_md:
            print(f"  ✅ Abstract converted from HTML: {len(abstract_md)} chars")

        return abstract_md

    def extract_section_paragraphs_from_html_content(self, html_content: str, section_title: str) -> str:
        """Extract a Nature section's paragraphs and convert them to Markdown."""
        soup = BeautifulSoup(html_content, 'html.parser')

        section = soup.find('section', {'data-title': section_title})
        if not section:
            print(f"  ⚠️  {section_title} section not found")
            return ""

        content_div = section.find('div', {'class': 'c-article-section__content'})
        if not content_div:
            print(f"  ⚠️  {section_title} content not found")
            return ""

        paragraphs = content_div.find_all('p')
        if not paragraphs:
            print(f"  ⚠️  {section_title} paragraphs not found")
            return ""

        converted_paragraphs = []
        for paragraph in paragraphs:
            md = self.convert_paragraph(str(paragraph))
            if md:
                converted_paragraphs.append(md)

        result = "\n\n".join(converted_paragraphs).strip()
        if result:
            print(f"  ✅ {section_title} converted: {len(converted_paragraphs)} paragraphs, {len(result)} chars")

        return result

    def extract_acknowledgements_from_html_content(self, html_content: str) -> str:
        """Extract Nature acknowledgements and convert them to Markdown."""
        return self.extract_section_paragraphs_from_html_content(html_content, 'Acknowledgements')

    def extract_code_availability_from_html_content(self, html_content: str) -> str:
        """Extract Nature Code availability section and convert to Markdown."""
        return self.extract_section_paragraphs_from_html_content(html_content, 'Code availability')

    def extract_supplementary_items_from_html_content(self, html_content: str, section_title: str) -> str:
        """Extract Nature supplementary-style item lists from a named section (exact match)."""
        soup = BeautifulSoup(html_content, 'html.parser')

        section = soup.find('section', {'data-title': section_title})
        if not section:
            print(f"  ⚠️  {section_title} section not found")
            return ""

        return self._process_supplementary_items_from_section(section, section_title)

    def extract_supplementary_items_by_keywords(self, html_content: str, keywords: list) -> str:
        """Extract supplementary items from sections matching any of the keywords (case-insensitive).

        Args:
            html_content: HTML content to search
            keywords: List of keywords to match (e.g., ['supplementary', 'supporting', 'supplemental'])

        Returns:
            Concatenated markdown from all matching sections
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        keywords_lower = [kw.lower() for kw in keywords]

        matching_sections = []
        for section in soup.find_all('section'):
            data_title = (section.get('data-title') or '').lower()
            if any(kw in data_title for kw in keywords_lower):
                matching_sections.append((section.get('data-title', 'Unknown'), section))

        if not matching_sections:
            return ""

        all_results = []
        for section_title, section in matching_sections:
            result = self._process_supplementary_items_from_section(section, section_title)
            if result:
                all_results.append(result)

        return "\n\n".join(all_results) if all_results else ""

    def _process_supplementary_items_from_section(self, section, section_title: str) -> str:
        """Helper to process supplementary items from a section element."""
        items = section.find_all('div', {'class': 'c-article-supplementary__item'})
        if not items:
            print(f"  ⚠️  {section_title} items not found")
            return ""

        md_parts = []
        for idx, item in enumerate(items, 1):
            title_link = item.find('a', {'class': 'print-link'})
            if not title_link:
                continue

            title_text = self.clean_text(title_link.get_text(" ", strip=True))
            href = title_link.get('href', '')
            if not title_text:
                continue

            md_parts.append(f"{idx}. [{title_text}]({href})")

            desc_div = item.find('div', {'class': 'c-article-supplementary__description'})
            if desc_div:
                desc_paragraphs = []
                for paragraph in desc_div.find_all('p'):
                    md = self.convert_paragraph(str(paragraph))
                    if md:
                        desc_paragraphs.append(md)
                if desc_paragraphs:
                    desc_md = "\n\n".join(desc_paragraphs)
                    desc_md = desc_md.replace("\n\n", "\n\n   ")
                    md_parts.append(f"   {desc_md}")

        result = "\n\n".join(md_parts).strip()
        if result:
            print(f"  ✅ {section_title} converted: {len(md_parts)} blocks, {len(result)} chars")

        return result

    def extract_extended_data_from_html_content(self, html_content: str) -> str:
        """Extract Nature extended data figures and tables.

        Searches for sections matching 'extended' keyword (case-insensitive).
        Falls back to exact match if keyword search fails.
        """
        result = self.extract_supplementary_items_by_keywords(html_content, ['extended'])
        if result:
            return result
        # Fallback to exact match for compatibility
        return self.extract_supplementary_items_from_html_content(
            html_content,
            'Extended data figures and tables',
        )

    def extract_supplementary_information_from_html_content(self, html_content: str) -> str:
        """Extract Nature supplementary information item links.

        Searches for sections matching 'supplementary', 'supporting', 'supplemental',
        or 'electronic' keywords (case-insensitive). Falls back to exact match if
        keyword search fails.
        """
        result = self.extract_supplementary_items_by_keywords(
            html_content,
            ['supplementary', 'supporting', 'supplemental', 'electronic']
        )
        if result:
            return result
        # Fallback to exact match for compatibility
        return self.extract_supplementary_items_from_html_content(
            html_content,
            'Supplementary information',
        )

    def convert_main_content_by_paragraph(self, html_content: str) -> str:
        """Convert Nature main-content to Markdown one paragraph at a time."""
        paragraphs = self.extract_paragraphs_from_html_content(html_content)
        converted_paragraphs = []

        for idx, paragraph_html in enumerate(paragraphs, 1):
            md = self.convert_paragraph(paragraph_html)
            if md:
                converted_paragraphs.append(md)
                if idx <= 3 or idx % 10 == 0:
                    print(f"  ✓ Paragraph {idx}: {len(md)} chars")

        final_markdown = "\n\n".join(converted_paragraphs)
        final_markdown = re.sub(r'\n\n\n+', '\n\n', final_markdown)

        if final_markdown:
            final_markdown = "## Main\n\n" + final_markdown

        print(f"  ✅ Converted main paragraphs: {len(converted_paragraphs)}")
        return final_markdown

    def convert_by_paragraph(self, html_file: str, output_file: str) -> dict:
        """Convert a Nature HTML file's main content by paragraph and save Markdown."""
        with open(html_file, 'r', encoding='utf-8') as f:
            html_content = f.read()

        final_markdown = self.convert_main_content_by_paragraph(html_content)

        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(final_markdown)

        return {
            'output_file': output_file,
            'size': len(final_markdown),
            'lines': len(final_markdown.splitlines()),
            'paragraphs': max((len(final_markdown.split("\n\n")) - 1), 0),
        }

    async def get_figures(self, page, metadata: dict = None) -> Dict[str, dict]:
        """Extract figure URLs from JSON-LD mainEntity.image or HTML structure."""
        print("  🔍 Extracting figures...")

        figures = {}
        json_ld_images = (metadata or {}).get('image') or []

        # Try JSON-LD images first
        for idx, image_url in enumerate(json_ld_images, 1):
            figures[f'fig_{idx}'] = {
                'caption': '',
                'url': image_url,
            }

        if figures:
            print(f"  ✅ Figures found from JSON-LD images: {len(figures)}")
        else:
            print("  ⚠️  No JSON-LD mainEntity.image figures found, trying HTML structure...")
            # Fallback: extract from HTML structure
            try:
                fulltext_html = await page.content()
                figures = self._extract_figures_from_html(fulltext_html)
                if figures:
                    print(f"  ✅ Figures found from HTML structure: {len(figures)}")
            except Exception as e:
                print(f"  ⚠️  Failed to extract figures from HTML: {e}")

        return figures

    def _extract_figures_from_html(self, html_content: str) -> Dict[str, dict]:
        """Extract figures from HTML structure with class='c-article-section__figure-link'."""
        figures = {}
        soup = BeautifulSoup(html_content, 'html.parser')

        fig_idx = 1
        for figure_div in soup.find_all('div', class_='c-article-section__figure'):
            # Get caption
            caption_elem = figure_div.find('figcaption')
            caption = caption_elem.get_text(' ', strip=True) if caption_elem else f'Figure {fig_idx}'
            caption = re.sub(r'\s+', ' ', caption).strip()

            # Try to get image URL from picture/source or img tag
            image_url = None
            figure_link = figure_div.find('a', class_='c-article-section__figure-link')

            if figure_link:
                # Try to find picture > source with srcset
                picture = figure_link.find('picture')
                if picture:
                    source = picture.find('source')
                    if source:
                        srcset = source.get('srcset', '')
                        if srcset:
                            # Extract first URL from srcset (format: "url size, url size, ...")
                            urls = srcset.split(',')
                            if urls:
                                image_url = urls[0].strip().split()[0]

                # Fallback: try img src
                if not image_url:
                    img = figure_link.find('img')
                    if img:
                        image_url = img.get('src')

            # Ensure URL is absolute
            if image_url:
                if image_url.startswith('//'):
                    image_url = 'https:' + image_url
                elif not image_url.startswith('http'):
                    image_url = self.actual_base_url + image_url

                figures[f'fig_{fig_idx}'] = {
                    'caption': caption,
                    'url': image_url,
                }
                fig_idx += 1

        return figures

    def replace_remote_figure_placeholders(self, markdown: str, metadata: dict,
                                           figure_filenames: dict = None) -> str:
        """Replace Nature remote image placeholders with local downloaded files."""
        json_ld_images = metadata.get('image') or []
        if not markdown or not json_ld_images:
            return markdown

        figure_filenames = figure_filenames or {}
        known_fig_nums = {
            str(idx)
            for idx, image_url in enumerate(json_ld_images, 1)
            if image_url
        }

        def replace_match(match):
            alt_text = match.group(1)
            image_url = match.group(2)
            fig_match = re.search(r'Fig(\d+)[^/)]*', image_url)
            if not fig_match:
                return match.group(0)

            fig_num = fig_match.group(1)
            if fig_num not in known_fig_nums:
                return match.group(0)

            local_filename = figure_filenames.get(fig_num, f"figure_{fig_num}.png")
            return f"![{alt_text}]({local_filename})"

        return re.sub(
            r'!\[([^\]]*)\]\(([^)]*media\.springernature\.com[^)]*MediaObjects[^)]*)\)',
            replace_match,
            markdown,
        )

    def convert_to_markdown(self, metadata: dict, fulltext_data = None,
                          add_figure_refs: bool = False,
                          figure_filenames: dict = None, **kwargs) -> str:
        """Convert extracted data to Markdown

        Args:
            metadata: Paper metadata dict
            fulltext_data: HTML content of article (for Nature) or JSON (future)
            add_figure_refs: If True, add figure references in markdown
            figure_filenames: Mapping of figure number to downloaded local filename

        Returns:
            Markdown formatted text
        """
        md_content = ""

        # ===== Title =====
        title = metadata.get('title') or "Academic Paper"
        md_content += f"# {title}\n\n"

        # ===== Authors =====
        if metadata.get('author_with_affiliations'):
            md_content += "## Authors\n\n"
            for item in metadata['author_with_affiliations']:
                author = item['author']
                affiliations = item['affiliations']
                md_content += f"- **{author}**\n"
                if item.get('email'):
                    md_content += f"  Email: {item['email']}\n"
                for aff in affiliations:
                    md_content += f"  {aff}\n"
                md_content += "\n"
            md_content += "\n"
        elif metadata.get('authors'):
            md_content += "## Authors\n\n"
            for author in metadata['authors'][:30]:  # Limit display to first 30
                md_content += f"- {author}\n"
            if len(metadata['authors']) > 30:
                md_content += f"- ... and {len(metadata['authors']) - 30} more authors\n"
            md_content += "\n"

        # ===== Publication Info =====
        md_content += "## Publication\n\n"
        if metadata.get('journal'):
            md_content += f"**Journal:** {metadata['journal']}\n\n"
        if metadata.get('year'):
            md_content += f"**Year:** {metadata['year']}\n\n"
        if metadata.get('volume'):
            md_content += f"**Volume:** {metadata['volume']}"
            if metadata.get('issue'):
                md_content += f", Issue {metadata['issue']}"
            md_content += "\n\n"
        if metadata.get('pages'):
            md_content += f"**Pages:** {metadata['pages']}\n\n"
        if metadata.get('doi'):
            md_content += f"**DOI:** {metadata['doi']}\n\n"
        md_content += "---\n\n"

        # ===== Abstract =====
        if metadata.get('abstract'):
            md_content += "## Abstract\n\n"
            md_content += f"{metadata['abstract']}\n\n"
            md_content += "---\n\n"

        # ===== Main Content =====
        if fulltext_data:
            # For Nature, extract_all() keeps the unified contract by returning
            # the page HTML as fulltext_data. Convert only main-content here.
            if isinstance(fulltext_data, str) and fulltext_data.strip().startswith('<'):
                article_md = self.convert_main_content_by_paragraph(fulltext_data)
                if article_md:
                    # Replace table placeholders with actual table markdown.
                    # Process in reverse order so string positions remain valid.
                    table_data = kwargs.get('table_data', {})
                    if table_data:
                        # Sort by position in article (descending → replace from bottom up)
                        indexed = []
                        for caption, tinfo in table_data.items():
                            placeholder = f'\n**{caption}**\n[View full table]'
                            pos = article_md.find(placeholder)
                            if pos >= 0:
                                indexed.append((pos, caption, tinfo))
                        indexed.sort(key=lambda x: x[0], reverse=True)

                        for pos, caption, tinfo in indexed:
                            placeholder = f'\n**{caption}**\n[View full table]'
                            end = article_md.find('\n', pos + len(placeholder))
                            if end < 0:
                                end = len(article_md)
                            replacement = f'\n**{caption}**\n\n{tinfo["markdown"]}\n'
                            article_md = article_md[:pos] + replacement + article_md[end + 1:]
                    if add_figure_refs:
                        article_md = self.replace_remote_figure_placeholders(
                            article_md,
                            metadata,
                            figure_filenames,
                        )
                    md_content += f"{article_md}\n\n"
                else:
                    md_content += "## Main\n\n[Article main content not found]\n"

                acknowledgements_md = self.extract_acknowledgements_from_html_content(fulltext_data)
                if acknowledgements_md:
                    md_content += f"## Acknowledgements\n\n{acknowledgements_md}\n\n"

                code_availability_md = self.extract_code_availability_from_html_content(fulltext_data)
                if code_availability_md:
                    md_content += f"## Code availability\n\n{code_availability_md}\n\n"

                extended_data_md = self.extract_extended_data_from_html_content(fulltext_data)
                if extended_data_md:
                    md_content += f"## Extended data figures and tables\n\n{extended_data_md}\n\n"

                supplementary_info_md = self.extract_supplementary_information_from_html_content(fulltext_data)
                if supplementary_info_md:
                    md_content += f"## Supplementary information\n\n{supplementary_info_md}\n\n"
            else:
                md_content += "## Main\n\n[Article content available but could not be converted]\n"

        md_content += "\n---\n\n"

        # ===== References =====
        if metadata.get('references'):
            md_content += "## References\n\n"
            refs_raw = metadata.get('_refs_raw', [])
            references = metadata['references']
            for idx, ref in enumerate(references):
                idx1 = idx + 1
                # Numbered text
                raw_text = None
                if refs_raw and idx < len(refs_raw):
                    try:
                        raw = refs_raw[idx]
                        parts = {}
                        for segment in raw.split(';'):
                            if '=' not in segment:
                                continue
                            k, v = segment.split('=', 1)
                            k = k.strip()
                            v = re.sub(r'\s+', ' ', v).strip()
                            if k and v:
                                parts[k] = v
                        if parts:
                            md_content += format_citation_as_text(parts, index=idx1) + "\n\n"
                        else:
                            raw_text = re.sub(r'\s+', ' ', unescape(raw or '')).strip()
                            md_content += f"[{idx1}] {raw_text}\n\n"
                    except Exception:
                        if not raw_text:
                            raw_text = ref
                        md_content += f"[{idx1}] {raw_text}\n\n"
                else:
                    md_content += f"[{idx1}] {ref}\n\n"
                # BibTeX block — only for properly formatted entries
                if ref.strip().startswith('@'):
                    md_content += f"```bibtex\n{ref}\n```\n\n"

        return md_content


# ============================================================================
# Nature-Specific Extraction Functions
# ============================================================================

def extract_doi_from_nature_url(url: str) -> Optional[str]:
    """Extract DOI from Nature article URL

    Example: https://www.nature.com/articles/s41586-026-10400-2
    Returns: 10.1038/s41586-026-10400-2
    """
    match = re.search(r'articles/(s\d+\-[\d\-]+)', url)
    if match:
        article_id = match.group(1)
        return f"10.1038/{article_id}"
    return None


def parse_nature_meta_tags(meta_dict: dict) -> dict:
    """Parse Nature-specific meta tags into standard format"""
    return {
        'title': meta_dict.get('citation_title'),
        'doi': meta_dict.get('citation_doi', '').replace('doi:', ''),
        'journal': meta_dict.get('citation_journal_title'),
        'author': meta_dict.get('citation_author'),
        'author_institution': meta_dict.get('citation_author_institution'),
        'date': meta_dict.get('citation_online_date'),
        'abstract': None,
    }


def detect_nature_journal(url: str) -> Optional[str]:
    """Detect Nature journal type from URL

    Returns: 'nature', 'nature_physics', 'nature_materials', etc.
    """
    if 'nature.com/articles' in url:
        # Extract journal from article ID pattern or URL
        if 's41567' in url:
            return 'nature_physics'
        elif 's41563' in url:
            return 'nature_materials'
        elif 's41586' in url:
            return 'nature'
        else:
            return 'nature'  # default
    return None

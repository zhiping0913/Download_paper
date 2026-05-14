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
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from json_to_md_converter import cleanup_markdown, convert_html_to_markdown


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
        self.base_url = "https://www.nature.com"

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
        page = page or self.page
        doi = doi or self.doi
        if page is None:
            raise ValueError("NatureHandler.extract_all() requires a Playwright page")
        if doi is None:
            raise ValueError("NatureHandler.extract_all() requires a DOI")

        self.configure(page=page, doi=doi)

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
        references = await self.extract_references(page)
        metadata['references'] = references

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

        # Extract all authors from HTML DOM (more complete than meta tags)
        all_authors = await page.evaluate("""() => {
            const authors = [];
            const elements = document.querySelectorAll('[class*="author"]');
            let uniqueAuthors = new Set();

            elements.forEach(el => {
                const text = el.textContent.trim();
                if (text && text.length > 2 && text.length < 100) {
                    uniqueAuthors.add(text);
                }
            });

            return Array.from(uniqueAuthors).slice(0, 50);
        }""")

        if all_authors:
            metadata['authors'] = all_authors
            print(f"  ✅ Authors found: {len(metadata['authors'])}")

        return metadata

    async def get_fulltext_url(self, page) -> Optional[str]:
        """Nature doesn't have a separate fulltext API, content is on the page itself"""
        return page.url

    async def get_pdf_url(self, page) -> Optional[str]:
        """Find PDF download URL

        Nature articles typically have a PDF button/link that needs to be located
        """
        print("  🔍 Looking for PDF download link...")

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
                            href = f"https://www.nature.com{href}"
                        print(f"  ✅ Found PDF: {href[:80]}...")
                        return href
            except:
                continue

        print("  ⚠️  PDF link not found (may require subscription)")
        return None

    async def get_supplemental_links(self, page) -> tuple:
        """Find Nature supplementary material file links."""
        print("  🔍 Looking for supplementary materials...")

        candidates = await page.evaluate("""() => {
            const links = [];
            const selectors = [
                'a[data-test="supp-info-link"]',
                'section[data-title="Supplementary information"] a[href]',
                'a[href*="static-content.springer.com/esm"]',
                'a[href*="/esm/"]',
                'a[href*="MOESM"]'
            ];

            document.querySelectorAll(selectors.join(',')).forEach(link => {
                const href = link.getAttribute('href') || '';
                const label = (link.textContent || '').replace(/\\s+/g, ' ').trim();
                links.push({href, label});
            });
            return links;
        }""")

        supplemental_urls = []
        supplemental_descriptions = {}
        seen = set()

        for item in candidates:
            href = (item.get('href') or '').strip()
            if not href:
                continue

            url = urljoin(self.base_url, href)
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

        if supplemental_urls:
            print(f"  ✅ Found supplementary materials: {len(supplemental_urls)}")
            for url in supplemental_urls[:5]:
                print(f"     - {url[:100]}...")
        else:
            print("  ⚠️  Supplementary materials link not found")

        return supplemental_urls, supplemental_descriptions

    async def get_supplemental_url(self, page) -> Optional[str]:
        """Find the first supplementary materials link."""
        supplemental_urls, _ = await self.get_supplemental_links(page)
        return supplemental_urls[0] if supplemental_urls else None

    async def extract_references(self, page) -> List[str]:
        """Parse references from HTML reference list"""
        print("  🔍 Extracting references...")

        references = await page.evaluate("""() => {
            const refs = [];
            const refItems = document.querySelectorAll('[class*="reference"] li, [class*="ref-item"]');

            refItems.forEach(item => {
                const text = item.textContent.trim();
                if (text && text.length > 10) {
                    refs.push(text);
                }
            });

            return refs.slice(0, 200);  // Limit to first 200
        }""")

        if references:
            print(f"  ✅ References found: {len(references)}")
        else:
            print("  ⚠️  No references found")

        return references

    def extract_paragraphs_from_html_content(self, html_content: str) -> List[str]:
        """Extract paragraph and equation HTML blocks from Nature main-content."""
        soup = BeautifulSoup(html_content, 'html.parser')
        main_content_div = soup.find('div', {'class': 'main-content'})

        if not main_content_div:
            main_content_div = soup.find('div', {'class': 'main-content', 'data-nosnippet': ''})

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

        ordered_items = []
        for match in paragraph_matches:
            ordered_items.append((match.start(), match.group(0)))
        for match in equation_matches:
            ordered_items.append((match.start(), match.group(0)))

        ordered_items.sort(key=lambda item: item[0])
        items = [content for _, content in ordered_items]

        print(f"  ✅ Main content blocks: {len(paragraph_matches)} paragraphs, {len(equation_matches)} equations")
        return items

    def extract_paragraphs_from_html(self, html_file: str) -> List[str]:
        """Extract paragraph and equation HTML blocks from a Nature HTML file."""
        with open(html_file, 'r', encoding='utf-8') as f:
            html_content = f.read()
        return self.extract_paragraphs_from_html_content(html_content)

    def convert_paragraph(self, paragraph_html: str) -> str:
        """Convert one HTML paragraph/equation block to cleaned Markdown."""
        try:
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
        """Extract and convert Nature abstract paragraphs from article HTML."""
        soup = BeautifulSoup(html_content, 'html.parser')

        abstract_section = soup.find('section', {'data-title': 'Abstract'})
        if not abstract_section:
            print("  ⚠️  Abstract section not found")
            return ""

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
        """Extract figure URLs from JSON-LD mainEntity.image."""
        print("  🔍 Extracting figures...")

        figures = {}
        json_ld_images = (metadata or {}).get('image') or []

        for idx, image_url in enumerate(json_ld_images, 1):
            figures[f'fig_{idx}'] = {
                'caption': '',
                'url': image_url,
            }

        if figures:
            print(f"  ✅ Figures found from JSON-LD images: {len(figures)}")
        else:
            print("  ⚠️  No JSON-LD mainEntity.image figures found")

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
                          figure_filenames: dict = None) -> str:
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
                    if add_figure_refs:
                        article_md = self.replace_remote_figure_placeholders(
                            article_md,
                            metadata,
                            figure_filenames,
                        )
                    md_content += f"{article_md}\n\n"
                else:
                    md_content += "## Main\n\n[Article main content not found]\n"
            else:
                md_content += "## Main\n\n[Article content available but could not be converted]\n"

        md_content += "\n---\n\n"

        # ===== References =====
        if metadata.get('references'):
            md_content += "## References\n\n"
            for i, ref in enumerate(metadata['references'][:100], 1):  # First 100
                md_content += f"[{i}] {ref}\n\n"
            if len(metadata['references']) > 100:
                md_content += f"\n[... and {len(metadata['references']) - 100} more references]\n"

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

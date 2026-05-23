"""
Optica Publishing handler.

Extracts metadata, body, figures, tables, references, and supplemental materials
from Optica Publishing articles (opg.optica.org).

Optica stores LaTeX equations directly in HTML with $ and $$ delimiters,
so no preprocessing is needed. References and supplemental materials are
extracted from the article page.
"""

import re
import urllib.request
import json

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from publisher.base import PublisherHandler
from publisher.wildcard import (
    convert_html_fragment_to_markdown,
    extract_abstract_with_fallbacks,
    find_generic_article_body,
    format_as_bibtex,
    format_citation_as_text,
    generate_bibtex_key,
    init_extract_all_page,
    parse_citation_reference_string,
    prepare_mathjax_html_fragment,
    set_actual_base_url,
    generate_reference_text_from_crossref,
)
from core.utilities import fetch_crossref


class OpticaHandler(PublisherHandler):
    """Handler for Optica Publishing articles (opg.optica.org)."""

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)

    # ------------------------------------------------------------------
    # Metadata extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_metadata_from_html_meta(html_content: str) -> dict:
        """Extract Optica metadata from citation_* <meta> tags."""
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        meta = {}
        authors = []

        for tag in soup.find_all('meta'):
            name = tag.get('name', '')
            content = tag.get('content', '')
            if not name or not content:
                continue

            if name == 'citation_author':
                authors.append(content.strip())
            elif name == 'citation_title':
                meta['title'] = content.strip()
            elif name == 'citation_doi':
                meta['doi'] = content.strip()
            elif name == 'citation_journal_title':
                meta['journal'] = content.strip()
            elif name == 'citation_volume':
                meta['volume'] = content.strip()
            elif name in ('citation_issue', 'citation_number'):
                meta['issue'] = content.strip()
            elif name == 'citation_firstpage':
                meta['first_page'] = content.strip()
            elif name == 'citation_lastpage':
                meta['last_page'] = content.strip()
            elif name == 'citation_publication_date':
                date_str = content.strip()
                meta['publication_date'] = date_str
                if date_str and '/' in date_str:
                    meta['year'] = date_str.split('/')[0]
                elif date_str:
                    year_match = re.search(r'(\d{4})', date_str)
                    if year_match:
                        meta['year'] = year_match.group(1)
            elif name == 'citation_online_date':
                if not meta.get('publication_date'):
                    meta['publication_date'] = content.strip()
            elif name == 'citation_pdf_url':
                meta['pdf_url'] = content.strip()
            elif name == 'citation_abstract':
                meta['abstract'] = content.strip()
            elif name == 'citation_keywords':
                meta['keywords'] = [k.strip() for k in content.split(';') if k.strip()]

        if authors:
            meta['authors'] = authors

        return meta

    # ------------------------------------------------------------------
    # Body text extraction
    # ------------------------------------------------------------------

    @classmethod
    def extract_article_text_from_html(cls, html_content: str):
        """Extract article body, returning (abstract_md, body_md).

        Optica stores LaTeX directly in HTML with $ and $$ delimiters.
        Traverse each h2 section (Abstract, numbered sections, Supplemental document)
        and extract paragraphs with formula handling.
        """
        if not html_content:
            return '', ''

        soup = BeautifulSoup(html_content, 'html.parser')

        # Find article body marker
        article_body_marker = soup.find(string=re.compile(r'Article Body', re.IGNORECASE))
        if not article_body_marker:
            return '', ''

        # Find the main content div after the marker
        main_content = article_body_marker.find_next('div', class_='main-content')
        if not main_content:
            main_content = article_body_marker.find_next('div')

        if not main_content:
            return '', ''

        abstract_md = ''
        body_parts = []

        # Traverse h2 sections
        for h2 in main_content.find_all('h2', class_='article-heading'):
            h2_id = h2.get('id', '')
            h2_text = h2.get_text(' ', strip=True)

            # Handle Abstract separately
            if 'abstract' in h2_id.lower():
                # Extract abstract content following this h2
                abstract_div = h2.find_next('div')
                if abstract_div:
                    abstract_md = cls._extract_section_to_md(abstract_div)
                continue

            # Handle Supplemental document (don't include in body)
            if 'supplemental' in h2_id.lower() or 'supplement' in h2_id.lower():
                continue

            # Handle References separately
            if 'references' in h2_id.lower():
                continue

            # Regular section: add heading and process paragraphs
            if h2_text:
                body_parts.append(f"## {h2_text}")
                body_parts.append("")

            # Process all content between this h2 and the next h2
            current = h2.find_next_sibling()
            while current and current.name != 'h2':
                if current.name == 'p':
                    p_md = cls._convert_optica_paragraph_to_md(str(current))
                    if p_md:
                        body_parts.append(p_md)
                        body_parts.append("")

                elif current.name == 'div':
                    classes = (current.get('class') or [])
                    # Display equations
                    if 'article-math-block' in classes:
                        # Extract LaTeX from $$...$$ or equation notation
                        math_content = current.get_text(strip=True)
                        if math_content:
                            body_parts.append(f"\n$$\n{math_content}\n$$\n")
                            body_parts.append("")
                    # Figure content
                    elif 'figure-image' in classes:
                        # Figures are extracted separately, skip here
                        pass

                elif current.name == 'h3':
                    h3_text = current.get_text(' ', strip=True)
                    if h3_text:
                        body_parts.append(f"### {h3_text}")
                        body_parts.append("")

                current = current.find_next_sibling()

        body_md = "\n".join(body_parts).strip()
        if body_md:
            body_md = re.sub(r'\n{3,}', '\n\n', body_md)

        return abstract_md, body_md

    @classmethod
    def _extract_section_to_md(cls, section_div) -> str:
        """Extract text from a section div, processing paragraphs."""
        parts = []
        for p in section_div.find_all('p', recursive=False):
            p_md = cls._convert_optica_paragraph_to_md(str(p))
            if p_md:
                parts.append(p_md)
        return "\n\n".join(parts) if parts else section_div.get_text(' ', strip=True)

    @staticmethod
    def _convert_optica_paragraph_to_md(html_fragment: str) -> str:
        """Convert Optica HTML paragraph to Markdown.

        Optica uses $...$ for inline math and $$...$$ for display math,
        which are already in LaTeX format. Convert HTML tags while preserving
        math delimiters.
        """
        if not html_fragment:
            return ''

        # Protect LaTeX math from HTML conversion
        math_placeholders = {}

        # Extract display math $$...$$
        def replace_display_math(m):
            key = f"<<<OPTICA_DISPLAY_MATH_{len(math_placeholders)}>>>"
            math_placeholders[key] = m.group(0)
            return key

        html_fragment = re.sub(
            r'\$\$[^$]*\$\$',
            replace_display_math,
            html_fragment,
            flags=re.DOTALL
        )

        # Extract inline math $...$
        def replace_inline_math(m):
            key = f"<<<OPTICA_INLINE_MATH_{len(math_placeholders)}>>>"
            math_placeholders[key] = m.group(0)
            return key

        html_fragment = re.sub(
            r'\$[^$]+?\$',
            replace_inline_math,
            html_fragment,
        )

        # Convert HTML to markdown
        md = convert_html_fragment_to_markdown(html_fragment) if html_fragment else ''

        # Restore math placeholders
        for placeholder, math in math_placeholders.items():
            md = md.replace(placeholder, math)

        md = md.strip()
        md = re.sub(r'\n{3,}', '\n\n', md)
        return md

    # ------------------------------------------------------------------
    # Figure extraction
    # ------------------------------------------------------------------

    @classmethod
    def extract_figures_from_html(cls, html_content: str) -> dict:
        """Extract figure URLs and captions from HTML.

        Optica figures use <div class="figure-image"> with:
        - Title in <span class="figure-title">
        - Caption in <span class="figure-caption">
        - Download link in <a href="/viewmedia.cfm?...&imagetype=full">
        """
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        figures = {}
        seen_ids = set()

        for fig_div in soup.find_all('div', class_='figure-image'):
            fig_id = fig_div.get('id', '')
            if fig_id in seen_ids:
                continue
            seen_ids.add(fig_id)

            fig_num = len(figures) + 1
            key = f"fig_{fig_num}"

            # Extract download link
            img_url = ''
            download_link = fig_div.find('a', string=re.compile(r'Download Full Size', re.IGNORECASE))
            if download_link:
                img_url = download_link.get('href', '')
                # Convert relative URLs to absolute
                if img_url and not img_url.startswith('http'):
                    img_url = 'https://opg.optica.org' + img_url if not img_url.startswith('/') else 'https://opg.optica.org' + img_url

            # Fallback: find img tag with data-src
            if not img_url:
                img = fig_div.find('img')
                if img:
                    img_url = img.get('data-src') or img.get('src') or ''
                    if img_url and not img_url.startswith('http'):
                        img_url = 'https://opg.optica.org' + img_url

            if not img_url or 'data:image' in img_url:
                continue

            # Extract caption
            caption = ''
            caption_span = fig_div.find('span', class_='figure-caption')
            if caption_span:
                caption = cls._convert_optica_paragraph_to_md(str(caption_span))
            else:
                # Fallback: use figure-title + figure-caption
                title_span = fig_div.find('span', class_='figure-title')
                if title_span:
                    caption = title_span.get_text(' ', strip=True)

            caption = re.sub(r'\s+', ' ', caption).strip()

            figures[key] = {
                'url': img_url.strip(),
                'caption': caption,
            }

        return figures

    # ------------------------------------------------------------------
    # Reference extraction
    # ------------------------------------------------------------------

    @classmethod
    def extract_references_from_html(cls, html_content: str) -> list:
        """Extract references from HTML.

        Optica stores references in <p id="refN" class="reference-body">
        with HTML formatting (bold numbers, journal names, DOI links).
        """
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        references = []

        # Find References section
        refs_h2 = soup.find('h2', class_='article-heading', id='References')
        if not refs_h2:
            return []

        # Find all reference paragraphs
        for ref_p in soup.find_all('p', class_='reference-body'):
            ref_html = str(ref_p)
            ref_text = ref_p.get_text(' ', strip=True)

            if not ref_text:
                continue

            # Try to fetch DOI from reference and get metadata from Crossref
            crossref_data = None
            doi_link = ref_p.find('a', href=re.compile(r'doi.org'))
            if doi_link:
                doi_url = doi_link.get('href', '')
                # Extract DOI from URL
                doi_match = re.search(r'10\.\d+/[\w./\-()]+', doi_url)
                if doi_match:
                    doi = doi_match.group(0)
                    try:
                        crossref_data = fetch_crossref(doi)
                    except Exception:
                        pass

            # Extract BibTeX from Crossref if available
            if crossref_data:
                bibtex = format_as_bibtex(crossref_data)
                if bibtex:
                    references.append(bibtex)
                else:
                    # Fallback to raw text with DOI
                    references.append({
                        'raw': ref_text,
                        'type': 'misc',
                    })
            else:
                # No DOI found, use raw reference text
                references.append({
                    'raw': ref_text,
                    'type': 'misc',
                })

        return references

    # ------------------------------------------------------------------
    # Supplemental material extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_supplemental_links_from_html(html_content: str) -> list:
        """Extract supplemental material links from article page.

        Optica stores supplemental links in:
        - Supplemental document section with <a href="https://doi.org/10.6084/...">
        - Supplementary Material section with links to viewmedia.cfm
        """
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        links = []

        # Find Supplemental document section
        supp_h2 = soup.find('h2', class_='article-heading', string=re.compile(r'Supplemental', re.IGNORECASE))
        if supp_h2:
            # Look for figshare links
            for a in supp_h2.find_next_siblings('p'):
                figshare_link = a.find('a', href=re.compile(r'figshare|doi.org/10.6084'))
                if figshare_link:
                    href = figshare_link.get('href', '')
                    if href and 'http' in href:
                        links.append(href)

        # Find Supplementary Material section (if exists)
        supp_table = soup.find('table')
        if supp_table:
            for a in supp_table.find_all('a', class_='view_media'):
                href = a.get('href', '')
                if href:
                    if not href.startswith('http'):
                        href = 'https://opg.optica.org/' + href
                    links.append(href)

        return links

    @staticmethod
    async def _extract_supplementary_from_figshare(page, figshare_url: str) -> tuple:
        """Navigate to a figshare URL and extract the download link.

        Figshare pages have a Download button that points to the actual file.
        """
        if not figshare_url or 'figshare' not in figshare_url:
            return [], {}

        print(f"  🔗 访问figshare页面: {figshare_url}")

        current_url = page.url
        try:
            await page.goto(figshare_url, wait_until='networkidle', timeout=30000)
        except Exception:
            try:
                await page.goto(figshare_url, wait_until='domcontentloaded', timeout=30000)
            except Exception as e:
                print(f"  ⚠ Figshare页面访问失败: {e}")
                return [], {}

        urls = []
        descriptions = {}

        try:
            # Look for download button on figshare
            download_js = """() => {
                const results = [];
                const downloadBtn = document.querySelector('a[href*="/download"]');
                if (downloadBtn) {
                    const href = downloadBtn.getAttribute('href');
                    if (href) {
                        const url = new URL(href, window.location.href).href;
                        results.push({text: 'Supplementary Material', url: url});
                    }
                }
                return results;
            }"""
            dl_data = await page.evaluate(download_js)
            for item in dl_data:
                url = item.get('url', '')
                text = item.get('text', '')
                if url and 'http' in url:
                    urls.append(url)
                    if text:
                        descriptions[url] = text
        except Exception as e:
            print(f"  ⚠ 提取Figshare下载链接失败: {e}")

        # Navigate back to article page
        try:
            await page.goto(current_url, wait_until='domcontentloaded', timeout=15000)
        except Exception:
            pass

        if urls:
            print(f"  ✓ 补充材料: {len(urls)} 个文件")

        return urls, descriptions

    # ------------------------------------------------------------------
    # Publisher contract methods
    # ------------------------------------------------------------------

    async def extract_metadata(self, page) -> dict:
        """Return metadata from HTML meta tags."""
        html_content = ''
        if page is not None:
            try:
                html_content = await page.content()
            except Exception:
                html_content = ''

        meta = self._extract_metadata_from_html_meta(html_content)

        abstract = meta.get('abstract', '')
        if not abstract and html_content:
            try:
                abstract, _ = self.extract_article_text_from_html(html_content)
            except Exception:
                pass

        return {
            'title': meta.get('title') or 'Optica Article',
            'authors': meta.get('authors', []),
            'author_with_affiliations': [],
            'corresponding_author_emails': [],
            'abstract': abstract,
            'journal': meta.get('journal') or 'Optics Express',
            'publication_date': meta.get('publication_date'),
            'doi': meta.get('doi') or self.doi,
            'volume': meta.get('volume'),
            'issue': meta.get('issue'),
            'pages': f"{(meta.get('first_page') or '')}-{(meta.get('last_page') or '')}".strip('-'),
            'year': meta.get('year'),
            'references': [],
            '_pdf_url': meta.get('pdf_url'),
        }

    async def get_fulltext_url(self, page) -> str:
        """Get URL for full article text (use current page URL)."""
        if page:
            return page.url
        return ''

    async def get_pdf_url(self, doi: str) -> str:
        """Construct PDF download URL for Optica article."""
        # Optica PDFs are typically at viewmedia.cfm with seq=0
        if not doi:
            return ''
        # Extract the article ID from DOI (e.g., OE.444043 from 10.1364/OE.444043)
        match = re.search(r'10\.1364/(.+)', doi)
        if match:
            article_id = match.group(1).replace('.', '_')
            return f"https://opg.optica.org/viewmedia.cfm?uri={article_id}&seq=0"
        return ''

    async def get_supplemental_url(self, doi: str) -> str:
        """Return supplemental materials endpoint (same as article page in Optica)."""
        return ''

    async def extract_references(self, html: str) -> list:
        """Parse references from HTML."""
        return self.extract_references_from_html(html)

    async def get_figures(self, json_data: dict) -> dict:
        """Extract figures from JSON (not used for Optica; use extract_figures_from_html instead)."""
        return {}

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        """Run complete extraction and return shared workflow payload."""
        html_content = ''
        if page:
            html_content = await page.content()
        elif captured and 'html' in captured:
            html_content = captured['html']

        if not html_content:
            return {}

        # Extract metadata
        metadata = await self.extract_metadata(page)

        # Extract article text
        abstract_md, body_md = self.extract_article_text_from_html(html_content)
        metadata['abstract'] = abstract_md or metadata.get('abstract', '')

        # Extract figures
        figures = self.extract_figures_from_html(html_content)

        # Extract references
        references = self.extract_references_from_html(html_content)
        metadata['references'] = references

        # Extract supplemental links
        supp_links = self._extract_supplemental_links_from_html(html_content)

        return {
            'metadata': metadata,
            'body': body_md,
            'figures': figures,
            'references': references,
            'supplemental_links': supp_links,
        }

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        """Format extracted data as Markdown."""
        lines = []

        # Title
        if metadata.get('title'):
            lines.append(f"# {metadata['title']}")
            lines.append("")

        # Metadata header
        if metadata.get('authors'):
            authors_str = ', '.join(metadata['authors'])
            lines.append(f"**Authors:** {authors_str}")

        if metadata.get('journal'):
            journal_str = metadata['journal']
            if metadata.get('volume'):
                journal_str += f", Vol. {metadata['volume']}"
            if metadata.get('issue'):
                journal_str += f", Issue {metadata['issue']}"
            if metadata.get('pages'):
                journal_str += f", pp. {metadata['pages']}"
            lines.append(f"**Journal:** {journal_str}")

        if metadata.get('year'):
            lines.append(f"**Year:** {metadata['year']}")

        if metadata.get('doi'):
            lines.append(f"**DOI:** {metadata['doi']}")

        lines.append("")

        # Abstract
        if metadata.get('abstract'):
            lines.append("## Abstract")
            lines.append(metadata['abstract'])
            lines.append("")

        # Body
        if article_text:
            lines.append(article_text)
            lines.append("")

        # References
        if metadata.get('references'):
            lines.append("## References")
            for ref in metadata['references']:
                if isinstance(ref, dict) and 'raw' in ref:
                    lines.append(f"- {ref['raw']}")
                elif isinstance(ref, str):
                    lines.append(f"- {ref}")
            lines.append("")

        return "\n".join(lines)

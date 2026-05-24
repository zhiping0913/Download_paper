"""
Optica Publishing handler.

Extracts metadata, body, figures, tables, references, and supplemental materials
from Optica Publishing Group articles (opg.optica.org).

Optica stores LaTeX equations directly in HTML with $ and $$ delimiters,
so no MathML preprocessing is needed. The article body is structured under
<h2 class="article-heading"> sections. Figures, references, and supplemental
links are extracted from the article page.

Requires headed browser (Chrome CDP) due to JavaScript-heavy rendering.
"""

import re

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from publisher.base import PublisherHandler
from publisher.wildcard import (
    convert_html_fragment_to_markdown,
    format_as_bibtex,
    format_citation_as_text,
    generate_bibtex_key,
    generate_reference_text_from_crossref,
    init_extract_all_page,
    set_actual_base_url,
)


class OpticaHandler(PublisherHandler):
    """Handler for Optica Publishing articles (opg.optica.org)."""

    OPTICA_BASE = 'https://opg.optica.org'

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
                    m = re.search(r'(\d{4})', date_str)
                    if m:
                        meta['year'] = m.group(1)
            elif name == 'citation_online_date':
                if not meta.get('publication_date'):
                    meta['publication_date'] = content.strip()
            elif name == 'citation_pdf_url':
                meta['pdf_url'] = content.strip()
            elif name == 'citation_abstract':
                if not meta.get('abstract'):
                    meta['abstract'] = content.strip()
            elif name == 'citation_keywords':
                meta['keywords'] = [k.strip() for k in content.split(';') if k.strip()]

        if authors:
            # Optica sometimes duplicates authors (multi-affiliation)
            seen = set()
            unique_authors = []
            for a in authors:
                if a not in seen:
                    seen.add(a)
                    unique_authors.append(a)
            meta['authors'] = unique_authors

        if meta.get('first_page') or meta.get('last_page'):
            fp = meta.pop('first_page', '')
            lp = meta.pop('last_page', '')
            meta['pages'] = f"{fp}-{lp}".strip('-')

        # Fallback: extract PDF URL from <li class="pdf-download"><a href="...">
        if not meta.get('pdf_url'):
            pdf_li = soup.find('li', class_='pdf-download')
            if pdf_li:
                a = pdf_li.find('a', href=True)
                if a:
                    href = a['href'].strip()
                    if not href.startswith('http'):
                        href = cls.OPTICA_BASE + ('/' if not href.startswith('/') else '') + href
                    meta['pdf_url'] = href

        return meta

    # ------------------------------------------------------------------
    # Body text extraction
    # ------------------------------------------------------------------

    @classmethod
    def extract_article_text_from_html(cls, html_content: str, crossref_refs=None):
        """Extract article body, returning (abstract_md, body_md).

        All <h2 class="article-heading"> sections are rendered into body_md in
        document order. Abstract is extracted separately into abstract_md.
        References section is rendered with HTML text (full author names) plus
        BibTeX blocks from crossref_refs. Supplemental, Funding, Acknowledgment
        and all other sections are rendered as normal body sections.
        """
        if not html_content:
            return '', ''

        soup = BeautifulSoup(html_content, 'html.parser')

        # Find the article body container
        article_body = soup.find('div', id='articleBody')
        if not article_body:
            marker = soup.find(string=re.compile(r'Article Body', re.IGNORECASE))
            if marker:
                article_body = marker.find_next('div', class_=re.compile(r'main-content'))

        if not article_body:
            return '', ''

        abstract_md = ''
        body_parts = []

        def is_section_h2(el):
            return (hasattr(el, 'name') and el.name == 'h2'
                    and 'article-heading' in (el.get('class') or []))

        # Traverse all h2 headings in document order
        for h2 in article_body.find_all('h2', class_='article-heading'):
            h2_id = h2.get('id', '').lower()
            h2_text = h2.get_text(' ', strip=True)

            # Abstract: extract to metadata, skip from body
            if h2_id == 'abstract':
                abs_parts = []
                cur = h2.next_sibling
                while cur is not None and not is_section_h2(cur):
                    if hasattr(cur, 'name') and cur.name == 'p':
                        p_md = cls._convert_paragraph_to_md(str(cur))
                        if p_md:
                            abs_parts.append(p_md)
                    elif hasattr(cur, 'name') and cur.name == 'div':
                        for p in cur.find_all('p', recursive=False):
                            p_md = cls._convert_paragraph_to_md(str(p))
                            if p_md:
                                abs_parts.append(p_md)
                    cur = cur.next_sibling
                abstract_md = '\n\n'.join(abs_parts).strip()
                continue

            # References: render HTML text + BibTeX; do not walk siblings generically
            if re.search(r'^references?$', h2_id):
                body_parts.extend(cls._render_references_section(h2, crossref_refs))
                body_parts.append('')
                continue

            # All other sections (Supplemental, Funding, Acknowledgment, etc.)
            if h2_text:
                body_parts.append(f"## {h2_text}")
                body_parts.append("")

            # Walk ALL siblings (including NavigableString text nodes) so that
            # orphaned inline content after display equations is captured.
            cur = h2.next_sibling
            inline_buffer = []

            def flush_inline_buffer():
                if not inline_buffer:
                    return
                combined_html = ''.join(inline_buffer)
                p_md = cls._convert_paragraph_to_md(combined_html)
                if p_md:
                    body_parts.append(p_md)
                    body_parts.append("")
                inline_buffer.clear()

            from bs4 import NavigableString as _NS
            while cur is not None and not is_section_h2(cur):
                if isinstance(cur, _NS):
                    text = str(cur)
                    if text.strip():
                        inline_buffer.append(text)
                elif hasattr(cur, 'name'):
                    if cur.name in ('span', 'a', 'em', 'strong', 'sub', 'sup', 'b', 'i'):
                        inline_buffer.append(str(cur))
                    else:
                        flush_inline_buffer()
                        cls._walk_optica_element(cur, body_parts, soup_root=soup)
                cur = cur.next_sibling
            flush_inline_buffer()

        body_md = "\n".join(body_parts).strip()
        body_md = re.sub(r'\n{3,}', '\n\n', body_md)

        # Fallback for articles with no body <h2> sections (e.g. short letters):
        # body text lives in a bare <article> element inside articleBody.
        if not body_md:
            article_el = article_body.find('article')
            if article_el:
                fallback_parts = []
                for child in article_el.children:
                    if hasattr(child, 'name') and child.name:
                        cls._walk_optica_element(child, fallback_parts, soup_root=soup)
                body_md = "\n".join(fallback_parts).strip()
                body_md = re.sub(r'\n{3,}', '\n\n', body_md)

        return abstract_md, body_md

    @classmethod
    def _render_references_section(cls, h2_el, crossref_refs=None):
        """Render the References section with HTML text and optional BibTeX blocks.

        Format per reference:
            [N] <full HTML text with author names and Crossref link>
            ```bibtex
            @article{key, ...}
            ```
        """
        parts = ['## References', '']

        # Build index map: 1-based ref number → crossref data
        bibtex_map = {}
        if crossref_refs:
            for ref in crossref_refs:
                key = ref.get('key', '')
                m = re.search(r'-R(\d+)$', key)
                if m:
                    bibtex_map[int(m.group(1))] = ref

        idx = 0
        cur = h2_el.next_sibling
        while cur is not None:
            if hasattr(cur, 'name'):
                if cur.name == 'h2' and 'article-heading' in (cur.get('class') or []):
                    break
                if cur.name == 'p' and 'reference-body' in (cur.get('class') or []):
                    idx += 1
                    # Remove the bold "N. " number tag before conversion
                    ref_html = re.sub(
                        r'<strong\s+class=["\']number["\'][^>]*>.*?</strong>',
                        '',
                        str(cur),
                        flags=re.DOTALL,
                    )
                    ref_md = cls._convert_paragraph_to_md(ref_html).strip()
                    parts.append(f"[{idx}] {ref_md}")
                    parts.append('')
                    if idx in bibtex_map:
                        ref_data = bibtex_map[idx]
                        bib_parts = {
                            'author': ref_data.get('author', ''),
                            'title': ref_data.get('article-title', ''),
                            'journal': ref_data.get('journal-title', ''),
                            'volume': ref_data.get('volume', ''),
                            'firstpage': ref_data.get('first-page', ''),
                            'year': str(ref_data.get('year', '')),
                            'doi': ref_data.get('DOI', ''),
                        }
                        bib_parts = {k: v for k, v in bib_parts.items() if v}
                        if any(bib_parts.get(k) for k in ['author', 'title', 'journal']):
                            bibtex = format_as_bibtex(
                                bib_parts, key=ref_data.get('key', f'ref{idx}')
                            )
                            parts.extend(['```bibtex', bibtex, '```', ''])
            cur = cur.next_sibling
        return parts

    @classmethod
    def _render_table_element(cls, element, parts, soup_root=None):
        """Render an inline table reference (div.text-plus-thumb).

        Extracts the caption from the description paragraph and fetches the
        actual table data from the linked modal element (#tableN).
        """
        # Caption from <p class="table-caption-label"> or first <p>
        caption_p = (element.find('p', class_='table-caption-label')
                     or element.find('p'))
        if caption_p:
            caption_md = cls._convert_paragraph_to_md(str(caption_p)).strip()
            if caption_md:
                parts.append(caption_md)

        # Fetch table data from modal via data-bs-target="#tableN"
        modal_link = element.find('a', attrs={'data-bs-target': True})
        if modal_link and soup_root:
            modal_id = modal_link.get('data-bs-target', '').lstrip('#')
            if modal_id:
                modal_el = soup_root.find(id=modal_id)
                if modal_el:
                    table_target = (modal_el.find('div', class_='table-responsive')
                                    or modal_el.find('table'))
                    if table_target:
                        table_md = cls._convert_paragraph_to_md(str(table_target))
                        if table_md:
                            parts.append(table_md)
        parts.append('')

    @classmethod
    def _walk_optica_element(cls, element, parts: list, soup_root=None):
        """Process a single element and append markdown lines to parts."""
        if element.name == 'p':
            p_md = cls._convert_paragraph_to_md(str(element))
            if p_md:
                parts.append(p_md)
                parts.append("")

        elif element.name == 'h3':
            h3_text = element.get_text(' ', strip=True)
            h3_text = re.sub(r'\s+', ' ', h3_text).strip()
            if h3_text:
                parts.append(f"### {h3_text}")
                parts.append("")

        elif element.name == 'h4':
            h4_text = element.get_text(' ', strip=True)
            if h4_text:
                parts.append(f"#### {h4_text}")
                parts.append("")

        elif element.name == 'table':
            table_md = cls._convert_paragraph_to_md(str(element))
            if table_md:
                parts.append(table_md)
                parts.append("")

        elif element.name in ('ul', 'ol'):
            list_md = cls._convert_paragraph_to_md(str(element))
            if list_md:
                parts.append(list_md)
                parts.append("")

        elif element.name == 'div':
            classes = element.get('class') or []
            if 'article-math-block' in classes:
                eq_md = cls._convert_paragraph_to_md(str(element)).strip()
                eq_md = re.sub(r'^\[\]\{#\S+\}\s*\(\d+\)\s*', '', eq_md)
                eq_md = re.sub(r'^\(\d+\)\s*', '', eq_md)
                if eq_md:
                    parts.append(f"\n{eq_md}\n")
                    parts.append("")
            elif 'figure-image' in classes:
                title_span = element.find('span', class_='figure-title')
                caption_span = element.find('span', class_='figure-caption')
                if title_span:
                    title_text = title_span.get_text(' ', strip=True)
                    caption_text = ''
                    if caption_span:
                        caption_text = cls._convert_paragraph_to_md(str(caption_span))
                    label = re.sub(r'\s+', ' ', title_text).strip().rstrip('.')
                    if caption_text:
                        parts.append(f"**{label}.** {caption_text}")
                    else:
                        parts.append(f"**{label}.**")
                    parts.append("")
            elif 'text-plus-thumb' in classes:
                # Inline table reference: caption + table data from modal
                cls._render_table_element(element, parts, soup_root)
            elif 'table-responsive' in classes:
                table_md = cls._convert_paragraph_to_md(str(element))
                if table_md:
                    parts.append(table_md)
                    parts.append("")
            elif element.find('h2', class_='article-heading'):
                # Skip section-container divs (e.g. <div class="back">):
                # their h2 children are handled by the outer section loop.
                return
            elif any(c in (classes or []) for c in
                     ('modal', 'modal-dialog', 'modal-content', 'modal-body')):
                # Skip modal containers; table data accessed via _render_table_element
                return
            else:
                for child in element.find_all(['p', 'h3', 'h4', 'div', 'table', 'ul', 'ol'],
                                              recursive=False):
                    cls._walk_optica_element(child, parts, soup_root)

    @staticmethod
    def _convert_paragraph_to_md(html_fragment: str) -> str:
        """Convert Optica HTML paragraph to Markdown.

        Optica uses $...$ for inline math and $$...$$ for display math.
        Protect math from HTML conversion, then restore.
        """
        if not html_fragment:
            return ''

        # Pre-clean: unwrap article-math-block divs so that $$...$$ equations
        # become inline text inside the <p>, preventing pandoc from splitting the
        # paragraph at the nested <div> boundary.
        # 1. Remove equation label anchors (produces "[]{#e1} (1)" noise).
        fragment = re.sub(
            r'<a\s[^>]*class=["\'][^"\']*math-controls[^"\']*["\'][^>]*>.*?</a>',
            '',
            html_fragment,
            flags=re.IGNORECASE | re.DOTALL,
        )
        # 2. Remove named anchors like <a name="e1">...</a>
        fragment = re.sub(
            r'<a\s+name=["\'][^"\']*["\'][^>]*>.*?</a>',
            '',
            fragment,
            flags=re.IGNORECASE | re.DOTALL,
        )
        # 3. Replace <div class="article-math-block">...</div> with its inner text,
        #    so $$...$$ sits inline in the paragraph rather than inside a block div.
        fragment = re.sub(
            r'<div[^>]*class=["\'][^"\']*article-math-block[^"\']*["\'][^>]*>([\s\S]*?)</div>',
            r'\1',
            fragment,
            flags=re.IGNORECASE,
        )

        math_store = {}
        counter = [0]

        def stash(s):
            key = f"\x00MATH{counter[0]}\x00"
            counter[0] += 1
            math_store[key] = s
            return key

        # Stash display math first (longer match)
        fragment = re.sub(
            r'\$\$[\s\S]*?\$\$',
            lambda m: stash(m.group(0)),
            fragment,
        )
        # Stash inline math
        fragment = re.sub(
            r'\$[^$\n]+?\$',
            lambda m: stash(m.group(0)),
            fragment,
        )

        md = convert_html_fragment_to_markdown(fragment) if fragment else ''

        # Restore math
        for key, val in math_store.items():
            md = md.replace(key, val)

        md = md.strip()
        md = re.sub(r'\n{3,}', '\n\n', md)
        return md

    # ------------------------------------------------------------------
    # Figure extraction
    # ------------------------------------------------------------------

    @classmethod
    def extract_figures_from_html(cls, html_content: str, base_url: str = None) -> dict:
        """Extract figure URLs and captions.

        Optica figures: <div class="figure-image" id="g001">
        - Title: <span class="figure-title"><strong>Fig. 1.</strong></span>
        - Caption: <span class="figure-caption">...</span>
        - Download: <a href="/viewmedia.cfm?...&imagetype=full">Download Full Size</a>
        - Also: data-src on <img class="figure lazyimg">
        """
        if not html_content:
            return {}

        base = base_url or cls.OPTICA_BASE
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

            # Prefer "Download Full Size" link
            img_url = ''
            for a in fig_div.find_all('a', href=True):
                text = a.get_text(' ', strip=True).lower()
                if 'download full' in text or 'full size' in text:
                    img_url = a['href']
                    break

            # Fallback: data-src on lazy img
            if not img_url:
                img_tag = fig_div.find('img')
                if img_tag:
                    img_url = img_tag.get('data-src') or img_tag.get('src') or ''

            if not img_url or 'data:image' in img_url or 'ajax-loader' in img_url:
                continue

            if img_url and not img_url.startswith('http'):
                img_url = base + img_url if img_url.startswith('/') else base + '/' + img_url

            # Caption
            caption_span = fig_div.find('span', class_='figure-caption')
            if caption_span:
                caption = cls._convert_paragraph_to_md(str(caption_span))
            else:
                caption = ''
            caption = re.sub(r'\s+', ' ', caption).strip()

            figures[key] = {
                'url': img_url.strip(),
                'caption': caption,
            }

        return figures

    # ------------------------------------------------------------------
    # Reference extraction (from inline HTML, enriched via Crossref)
    # ------------------------------------------------------------------

    @classmethod
    def extract_references_from_html(cls, html_content: str) -> list:
        """Extract reference list from HTML as raw text strings.

        Returns list of plain-text reference strings (one per entry).
        BibTeX enrichment is handled by convert_to_markdown using _crossref_references.
        """
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        references = []

        for ref_p in soup.find_all('p', class_='reference-body'):
            # Remove footnote-like leading number markers
            number_span = ref_p.find('strong', class_='number')
            if number_span:
                number_span.decompose()

            # Remove "Crossref" link text but keep DOI info
            for a in ref_p.find_all('a', href=True):
                if 'doi.org' in a.get('href', ''):
                    doi_url = a['href']
                    doi_m = re.search(r'10\.\d{4,}/\S+', doi_url)
                    if doi_m:
                        # Keep DOI as text
                        a.replace_with(f"DOI:{doi_m.group(0)}")
                    else:
                        a.decompose()
                else:
                    a.decompose()

            text = ref_p.get_text(' ', strip=True)
            text = re.sub(r'\s+', ' ', text).strip()
            if text:
                references.append(text)

        return references

    # ------------------------------------------------------------------
    # Supplemental material extraction
    # ------------------------------------------------------------------

    @classmethod
    def _find_supplemental_links_from_html(cls, html_content: str) -> list:
        """Find supplemental material links from the article HTML.

        Looks in:
        1. <h2> containing "Supplemental" → <a href="...figshare...">
        2. Supplementary Material table → <a class="view_media">
        """
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        links = []

        # 1. Supplemental document section with figshare / DOI links
        for h2 in soup.find_all('h2'):
            h2_text = h2.get_text().strip().lower()
            if 'supplemental' not in h2_text and 'supplement' not in h2_text:
                continue
            # Walk siblings until next h2
            cur = h2.find_next_sibling()
            while cur and cur.name != 'h2':
                for a in (cur.find_all('a', href=True) if hasattr(cur, 'find_all') else []):
                    href = a.get('href', '')
                    if 'figshare' in href or '10.6084' in href or '/m9.figshare' in href:
                        if href not in links:
                            links.append(href)
                cur = cur.find_next_sibling() if hasattr(cur, 'find_next_sibling') else None

        # 2. Supplementary Material table (local viewmedia links)
        for a in soup.find_all('a', class_='view_media'):
            href = a.get('href', '')
            if not href:
                continue
            if not href.startswith('http'):
                href = cls.OPTICA_BASE + '/' + href.lstrip('/')
            if href not in links:
                links.append(href)

        return links

    @staticmethod
    async def _resolve_figshare_download(page, figshare_url: str) -> str:
        """Open figshare URL in a new tab and locate the direct download link.

        Figshare pages expose a download anchor such as:
            <a href="https://opticapublishing.figshare.com/ndownloader/files/...">
        """
        print(f"  🔗 解析Figshare链接: {figshare_url}")
        new_tab = await page.context.new_page()
        try:
            await new_tab.goto(figshare_url, wait_until='networkidle', timeout=30000)
        except Exception:
            try:
                await new_tab.goto(figshare_url, wait_until='domcontentloaded', timeout=30000)
            except Exception as e:
                print(f"  ⚠ Figshare页面加载失败: {e}")
                await new_tab.close()
                return figshare_url  # return original as fallback

        try:
            # Look for download links matching ndownloader pattern
            download_url = await new_tab.evaluate("""() => {
                // ndownloader direct link
                const dl = document.querySelector('a[href*="ndownloader"]');
                if (dl) return dl.href;
                // generic Download button
                const btn = document.querySelector('a[href*="/download"]');
                if (btn) return btn.href;
                return null;
            }""")
            if download_url:
                print(f"  ✓ 找到下载链接: {download_url[:80]}")
                await new_tab.close()
                return download_url
        except Exception as e:
            print(f"  ⚠ 提取Figshare下载链接失败: {e}")

        await new_tab.close()
        return figshare_url  # fallback to original DOI link

    # ------------------------------------------------------------------
    # Publisher contract: extract_metadata
    # ------------------------------------------------------------------

    async def extract_metadata(self, page) -> dict:
        """Return metadata dict from HTML meta tags."""
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
            'journal': meta.get('journal') or 'Optica Publishing',
            'publication_date': meta.get('publication_date'),
            'doi': meta.get('doi') or self.doi,
            'volume': meta.get('volume'),
            'issue': meta.get('issue'),
            'pages': meta.get('pages', ''),
            'year': meta.get('year'),
            'references': [],
            '_pdf_url': meta.get('pdf_url'),
            '_keywords': meta.get('keywords', []),
        }

    async def get_fulltext_url(self, page) -> str:
        return page.url if page else ''

    async def get_pdf_url(self, doi: str) -> str:
        return ''

    async def get_supplemental_url(self, doi: str) -> str:
        return ''

    async def extract_references(self, html: str) -> list:
        return self.extract_references_from_html(html)

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    # ------------------------------------------------------------------
    # Main extraction entry point
    # ------------------------------------------------------------------

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        """Complete Optica extraction following the unified publisher contract.

        Returns:
            {
                'metadata': dict,
                'links': {
                    'pdf_url': str,
                    'figure_urls': dict,          # {'fig_N': {'url': ..., 'caption': ...}}
                    'supplemental_urls': list,
                    'supplemental_descriptions': dict,
                },
                'fulltext_data': str,             # raw HTML
                'journal_name': 'optica',
            }
        """
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'OpticaHandler'
        )

        set_actual_base_url(self, page)

        try:
            # Get full HTML
            try:
                fulltext_html = await page.content()
            except Exception:
                fulltext_html = ''

            # Metadata
            metadata = await self.extract_metadata(page)
            doi = doi or self.doi
            metadata['doi'] = doi

            pdf_url = metadata.pop('_pdf_url', None)
            metadata.pop('_keywords', None)

            # Figures
            base_url = getattr(self, '_base_url', None) or self.OPTICA_BASE
            figure_urls = {}
            if fulltext_html:
                figure_urls = self.extract_figures_from_html(fulltext_html, base_url=base_url)

            # References (raw text list from HTML)
            if fulltext_html:
                metadata['references'] = self.extract_references_from_html(fulltext_html)

            # Supplemental materials
            supp_urls = []
            supp_descriptions = {}
            if fulltext_html:
                raw_supp = self._find_supplemental_links_from_html(fulltext_html)
                for link in raw_supp:
                    if 'figshare' in link or '10.6084' in link:
                        resolved = await self._resolve_figshare_download(page, link)
                        supp_urls.append(resolved)
                        supp_descriptions[resolved] = 'Supplemental document'
                    else:
                        supp_urls.append(link)
                        supp_descriptions[link] = 'Supplemental document'

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figure_urls,
                    'supplemental_urls': supp_urls,
                    'supplemental_descriptions': supp_descriptions,
                },
                'fulltext_data': fulltext_html,
                'journal_name': 'optica',
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
    # Markdown generation
    # ------------------------------------------------------------------

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        """Generate complete Markdown for an Optica article.

        Args:
            metadata: dict returned by extract_all
            article_text: full HTML string (fulltext_data from extract_all)
            **kwargs: add_figure_refs, figure_filenames, supplemental_urls,
                      supplemental_descriptions, supplemental_downloads
        """
        title = metadata.get('title') or 'Optica Article'
        md_parts = [f"# {title}", ""]

        # Authors
        authors = metadata.get('authors', [])
        if authors:
            md_parts.append("**Authors:** " + ', '.join(authors))
            md_parts.append("")

        if metadata.get('doi'):
            md_parts.append(f"**DOI:** {metadata['doi']}")
            md_parts.append("")

        md_parts.append("## Publication")
        md_parts.append("")
        md_parts.append(f"**Journal:** {metadata.get('journal') or 'Optica Publishing'}")
        md_parts.append("")

        for field, label in [('volume', 'Volume'), ('issue', 'Issue'), ('pages', 'Pages'),
                              ('publication_date', 'Published')]:
            if metadata.get(field):
                md_parts.append(f"**{label}:** {metadata[field]}")
                md_parts.append("")

        # Abstract
        abstract = metadata.get('abstract', '')
        if abstract:
            md_parts.extend(["---", "", "## Abstract", "", abstract, ""])

        # Body text (extract from HTML, passing crossref_refs for BibTeX injection)
        body_md = ''
        crossref_refs = metadata.get('_crossref_references', [])
        if isinstance(article_text, str) and article_text.strip():
            if article_text.lstrip().startswith('<'):
                _, body_md = self.extract_article_text_from_html(
                    article_text, crossref_refs=crossref_refs
                )
            else:
                body_md = article_text.strip()

        # Embed downloaded figure images after captions
        if kwargs.get('add_figure_refs') and kwargs.get('figure_filenames'):
            for fig_num, filename in sorted(
                kwargs['figure_filenames'].items(),
                key=lambda x: (int(x[0]) if str(x[0]).isdigit() else 0)
            ):
                body_md = re.sub(
                    rf'(\*\*Fig\.?\s*{re.escape(str(fig_num))}[.:]\*\*[^\n]*)',
                    rf'\1\n\n![Figure {fig_num}.]({filename})',
                    body_md,
                )

        md_parts.extend(["---", "", "## Article Text", "", body_md or "[Article text not found.]", ""])

        # Supplemental material download filenames (extra metadata, not in HTML body)
        supp_downloads = kwargs.get('supplemental_downloads', [])
        if supp_downloads:
            md_parts.extend(["---", "", "## Supplemental Material Downloads", ""])
            for dl in supp_downloads:
                md_parts.append(f"- {dl}")
            md_parts.append("")

        return "\n".join(md_parts)

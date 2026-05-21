"""
Cambridge University Press handler.

Extracts metadata, body, figures, references, and supplemental materials
from Cambridge Core article pages (cambridge.org).
"""

import re

from bs4 import BeautifulSoup, NavigableString
from playwright.async_api import async_playwright

from html_to_md_converter import (
    cleanup_markdown,
    convert_html_to_markdown,
    mathml_to_latex_pandoc,
    remove_newlines_in_paragraph,
)
from publisher.base import PublisherHandler
from publisher.wildcard import set_actual_base_url, init_extract_all_page, format_as_bibtex, generate_reference_text_from_crossref


class CambridgeHandler(PublisherHandler):
    """Handler for Cambridge University Press articles (cambridge.org)."""

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)

    # ------------------------------------------------------------------
    # Metadata extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_metadata_from_html_meta(html_content: str) -> dict:
        """Extract Cambridge metadata from citation_* <meta> tags."""
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        meta = {}
        authors = []
        author_orcids = {}

        for tag in soup.find_all('meta'):
            name = tag.get('name', '')
            content = tag.get('content', '')
            if not name or not content:
                continue

            if name == 'citation_author':
                authors.append(content.strip())
            elif name == 'citation_author_orcid':
                orcid = content.strip()
                orcid_id = orcid.rsplit('/', 1)[-1] if '/' in orcid else orcid
                author_orcids[len(authors) - 1 if authors else 0] = orcid_id
            elif name == 'citation_title':
                meta['title'] = CambridgeHandler._clean_html_entities(content)
            elif name == 'citation_doi':
                meta['doi'] = content.strip()
            elif name == 'citation_journal_title':
                meta['journal'] = content.strip()
            elif name == 'citation_volume':
                meta['volume'] = content.strip()
            elif name == 'citation_firstpage':
                meta['pages'] = content.strip()
            elif name == 'citation_publication_date':
                date_str = content.strip()
                meta['publication_date'] = date_str
                if date_str and '/' in date_str:
                    meta['year'] = date_str.split('/')[0]
            elif name == 'citation_pdf_url':
                meta['pdf_url'] = content.strip()
            elif name == 'citation_abstract':
                meta['abstract'] = content.strip()
            elif name == 'citation_keywords':
                meta['keywords'] = [k.strip() for k in content.split(';') if k.strip()]

        if authors:
            meta['authors'] = authors

        return meta

    @staticmethod
    def _clean_html_entities(text: str) -> str:
        """Replace HTML entities like &nbsp; with their plain equivalents."""
        if not text:
            return text
        return text.replace('\xa0', ' ').replace('&nbsp;', ' ').strip()

    @staticmethod
    def _extract_authors_from_html(html_content: str) -> list:
        """Extract author names and affiliations from DOM."""
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        author_entries = []

        for author_div in soup.find_all('div', class_='row', attrs={'data-test-author': True}):
            name_el = author_div.find('dt', class_='title')
            if not name_el:
                continue
            name = name_el.get_text(' ', strip=True)
            # Remove trailing * (corresponding author marker)
            name = name.rstrip('*').strip()

            affiliations = []
            for dd in author_div.find_all('dd'):
                # Skip any non-affiliation content
                content_title = dd.find('span', class_='content__title')
                if content_title and 'affiliation' in content_title.get_text('', '').lower():
                    for span in dd.find_all('span', recursive=True):
                        text = span.get_text(' ', strip=True)
                        if text and text != content_title.get_text(' ', strip=True):
                            affiliations.append(text)
                            break

            # Deduplicate while preserving order
            seen = set()
            unique_affs = []
            for aff in affiliations:
                if aff not in seen:
                    seen.add(aff)
                    unique_affs.append(aff)

            author_entries.append({
                'author': name,
                'affiliations': unique_affs,
            })

        return author_entries

    @staticmethod
    def _extract_corresponding_emails(html_content: str) -> list:
        """Extract corresponding author emails from DOM."""
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        emails = []

        corresp_div = soup.find('div', class_='corresp')
        if corresp_div:
            for a in corresp_div.find_all('a', href=True):
                href = a['href']
                if href.startswith('mailto:'):
                    emails.append(href.replace('mailto:', '').strip())

        return emails

    # ------------------------------------------------------------------
    # Math / HTML → Markdown helpers (reuse AIP pipeline)
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_inline_math_delimiters(latex: str) -> str:
        latex = (latex or '').strip()
        if latex.startswith('$') and latex.endswith('$') and not latex.startswith('$$'):
            return latex[1:-1].strip()
        return latex

    @classmethod
    def _convert_mathml(cls, math_tag, display: bool = False) -> str:
        latex = mathml_to_latex_pandoc(str(math_tag))
        if not latex:
            return ''

        if display:
            latex_body = cls._strip_inline_math_delimiters(latex)
            return f"$$\n{latex_body}\n$$"
        return latex

    @classmethod
    def _prepare_html_fragment(cls, html_fragment: str) -> tuple[str, list[str]]:
        """Collapse MathJax markup to placeholders before pandoc conversion."""
        soup = BeautifulSoup(html_fragment, 'html.parser')
        formulas = []

        def stash_formula(latex: str) -> str:
            formulas.append(latex)
            return f"CAMBMATH{len(formulas) - 1:03d}MATHEND"

        for tag in soup(['script', 'style', 'noscript']):
            tag.decompose()

        # Collapse xref-bibr links to plain [number] brackets
        for a_tag in soup.select('a.xref-bibr'):
            sup_tag = a_tag.find('sup')
            ref_text = sup_tag.get_text(' ', strip=True) if sup_tag else a_tag.get_text(' ', strip=True)
            if ref_text:
                a_tag.replace_with(NavigableString(f"[{ref_text}]"))

        # Collapse xref-fig links to plain text
        for a_tag in soup.select('a.xref-fig, a.xref-table'):
            a_tag.replace_with(NavigableString(a_tag.get_text(' ', strip=True)))

        # Inline formulas rendered as MathJax CHTML with assistive MathML
        for formula in soup.select('span.inline-formula'):
            math_tag = formula.find('math')
            latex = cls._convert_mathml(math_tag) if math_tag else ''
            if latex:
                formula.replace_with(NavigableString(f" {stash_formula(latex)} "))

        for container in soup.find_all('mjx-container'):
            math_tag = container.find('math')
            latex = cls._convert_mathml(math_tag) if math_tag else ''
            if latex:
                container.replace_with(NavigableString(f" {stash_formula(latex)} "))

        return str(soup), formulas

    @classmethod
    def _convert_html_fragment_to_markdown(cls, html_fragment: str) -> str:
        prepared_html, formulas = cls._prepare_html_fragment(html_fragment)
        md = convert_html_to_markdown(prepared_html)
        for index, latex in enumerate(formulas):
            md = md.replace(f"CAMBMATH{index:03d}MATHEND", latex)
        md = cleanup_markdown(md)
        md = remove_newlines_in_paragraph(md, "", "p")
        md = re.sub(r'\s+', ' ', md).strip()
        return md

    # ------------------------------------------------------------------
    # Body text extraction
    # ------------------------------------------------------------------

    @classmethod
    def extract_article_text_from_html(cls, html_content: str):
        """Extract article body, returning (abstract_md, body_md)."""
        if not html_content:
            return '', ''

        soup = BeautifulSoup(html_content, 'html.parser')
        body_parts = []

        # Find the main body container
        body_div = soup.find('div', class_='body')
        if not body_div:
            return '', ''

        # Process sections (sec1, sec2, etc.) in order
        for sec in body_div.find_all('div', class_=re.compile(r'^sec\b')):
            sec_id = sec.get('id', '')

            # Skip abstract section (sec0)
            if sec_id == 'sec0' or 'abstract' in sec.get('class', []):
                continue

            # Section heading
            heading_tag = sec.find(['h2', 'h3'])
            if heading_tag:
                label_span = heading_tag.find('span', class_='label')
                heading_text = heading_tag.get_text(' ', strip=True) if not label_span else heading_tag.get_text(' ', strip=True)
                heading_text = re.sub(r'\s+', ' ', heading_text or '').strip()
                if heading_text:
                    level = "###" if heading_tag.name == 'h2' else "####"
                    body_parts.extend([f"{level} {heading_text}", ""])

            # Process child elements: paragraphs, figure sections
            for child in sec.children:
                if not hasattr(child, 'name') or child.name is None:
                    continue

                if child.name == 'p' and 'p' in child.get('class', []):
                    para_md = cls._convert_html_fragment_to_markdown(str(child))
                    if para_md:
                        body_parts.extend([para_md, ""])

                elif child.name == 'section':
                    # Figure block: <section> containing fig-ada + figure-thumb
                    fig_ada = child.find('div', class_='fig-ada')
                    fig_thumb = child.find('div', class_='figure-thumb')

                    if fig_ada:
                        caption_div = fig_ada.find('div', class_='caption')
                        if caption_div:
                            label = caption_div.find('span', class_='label')
                            caption_p = caption_div.find('p', class_='p')

                            parts = []
                            if label:
                                label_text = label.get_text(' ', strip=True)
                                if label_text:
                                    parts.append(f"**{label_text}**")
                            if caption_p:
                                caption_md = cls._convert_html_fragment_to_markdown(str(caption_p))
                                if caption_md:
                                    parts.append(caption_md)
                            if parts:
                                body_parts.extend([" ".join(parts), ""])

                    if fig_thumb:
                        img = fig_thumb.find('img', class_='aop-lazy-load-image')
                        if img:
                            img_name = img.get('data-img-name', '')
                            img_src = img.get('data-src') or img.get('data-original-image') or ''
                            if img_src and img_name:
                                body_parts.extend([f"![{img_name}]({img_src})", ""])

                elif child.name == 'div' and 'displayed-formula' in child.get('class', []):
                    math_tag = child.find('math')
                    if math_tag:
                        formula_md = cls._convert_mathml(math_tag, display=True)
                        if formula_md:
                            body_parts.extend([formula_md, ""])

        body_md = "\n".join(body_parts).strip()
        return '', body_md

    # ------------------------------------------------------------------
    # Figure extraction
    # ------------------------------------------------------------------

    @classmethod
    def extract_figures_from_html(cls, html_content: str) -> dict:
        """Extract figure URLs and captions from HTML."""
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        figures = {}
        seen_ids = set()

        for fig_ada in soup.find_all('div', class_='fig-ada'):
            fig_id = fig_ada.get('id', '')
            if not fig_id or fig_id in seen_ids:
                continue
            seen_ids.add(fig_id)

            # Extract figure number from id like "fig1"
            fig_num = fig_id.replace('fig', '')
            key = f"fig_{fig_num}"

            # Find the image in the sibling or parent section
            section = fig_ada.find_parent('section')
            img = None
            if section:
                fig_thumb = section.find('div', class_='figure-thumb')
                if fig_thumb:
                    img = fig_thumb.find('img', class_='aop-lazy-load-image')
            if not img:
                continue

            img_url = img.get('data-src') or img.get('data-original-image') or img.get('src') or ''
            if not img_url:
                continue

            # Build caption
            caption_div = fig_ada.find('div', class_='caption')
            caption = ''
            if caption_div:
                label = caption_div.find('span', class_='label')
                cap_p = caption_div.find('p', class_='p')
                parts = []
                if label:
                    parts.append(label.get_text(' ', strip=True))
                if cap_p:
                    parts.append(cap_p.get_text(' ', strip=True))
                caption = ' '.join(parts).strip()

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
        """Extract references from the references-list section."""
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        references = []

        refs_div = soup.find('div', id='references-list')
        if not refs_div:
            return []

        for ref_content in refs_div.find_all('div', id=re.compile(r'^reference-\d+-content')):
            ref_num_match = re.search(r'reference-(\d+)-content', ref_content.get('id', ''))
            ref_num = ref_num_match.group(1) if ref_num_match else ''

            # Get all text, preserving DOI links
            for a in ref_content.find_all('a', href=True):
                href = a['href']
                if 'doi.org' in href:
                    a.replace_with(NavigableString(f" [{href}] "))

            text = ref_content.get_text(' ', strip=True)
            text = re.sub(r'\s+', ' ', text).strip()
            if text:
                references.append(f"[{ref_num}] {text}")

        return references

    # ------------------------------------------------------------------
    # Supplemental material extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_supplemental_links_from_html(html_content: str) -> list:
        """Extract supplemental material download links.

        Primary source: #supplementary-materials-tab (Vue-rendered
        materials table with direct file download URLs).  Falls back to
        .notes.supplementary-material in the article body.
        """
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')

        links = []

        # Primary: the tabbed supplementary-materials pane with real download URLs
        materials_tab = soup.find('div', id='supplementary-materials-tab')
        if materials_tab:
            for a_tag in materials_tab.find_all('a', href=True):
                href = a_tag['href'].strip()
                if href and 'static.cambridge.org' in href:
                    links.append(href)

        # Fallback: inline .notes.supplementary-material (usually just DOI links,
        # only used when the materials table is absent)
        if not links:
            supp_div = soup.find('div', class_='supplementary-material')
            if supp_div:
                for a_tag in supp_div.find_all('a', href=True):
                    href = a_tag['href'].strip()
                    if href and not href.startswith('#'):
                        links.append(href)

        return links

    @staticmethod
    def _extract_supplemental_descriptions(html_content: str) -> dict:
        """Extract supplemental material file names, descriptions and sizes."""
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        descriptions = {}

        materials_tab = soup.find('div', id='supplementary-materials-tab')
        if materials_tab:
            for item in materials_tab.find_all('div', class_='materials-table__item'):
                # Get title, description and download link from the first row
                row = item.find('div', class_='materials-table__row')
                if not row:
                    continue

                main_elem = row.find('div', class_='materials-table__row__main-elem')
                title_el = main_elem.find('h3', class_='materials-table__row__title') if main_elem else None
                desc_el = main_elem.find('div', class_='materials-table__row__description') if main_elem else None

                download_a = row.find('a', href=True, attrs={'download': True})
                if not download_a:
                    continue

                url = download_a['href'].strip()
                title = title_el.get_text(' ', strip=True) if title_el else ''
                desc = desc_el.get_text(' ', strip=True) if desc_el else ''

                # File size is in a separate row within the same item
                size_info = ''
                for size_row in item.find_all('div', class_='materials-table__row'):
                    size_el = size_row.find('div', class_='materials-table__row__size-type')
                    if size_el:
                        size_info = size_el.get_text(' ', strip=True)
                        size_info = re.sub(r'\s+', ' ', size_info).strip()
                        break

                descriptions[url] = {
                    'title': title,
                    'description': desc,
                    'file_info': size_info,
                }

        if not descriptions:
            supp_div = soup.find('div', class_='supplementary-material')
            if supp_div:
                for p in supp_div.find_all('p', class_='p'):
                    text = p.get_text(' ', strip=True)
                    text = re.sub(r'\s+', ' ', text).strip()
                    if text:
                        key = text[:80]
                        descriptions[key] = {'title': '', 'description': text, 'file_info': ''}

        return descriptions

    # ------------------------------------------------------------------
    # Publisher contract methods
    # ------------------------------------------------------------------

    async def extract_metadata(self, page) -> dict:
        """Return metadata from HTML meta tags and DOM."""
        html_content = ''
        if page is not None:
            try:
                html_content = await page.content()
            except Exception:
                html_content = ''

        meta = self._extract_metadata_from_html_meta(html_content)
        author_entries = self._extract_authors_from_html(html_content)
        emails = self._extract_corresponding_emails(html_content)

        abstract = meta.get('abstract', '')
        if not abstract and html_content:
            try:
                abstract_md, _ = self.extract_article_text_from_html(html_content)
                abstract = abstract_md
            except Exception:
                pass

        return {
            'title': meta.get('title') or 'Cambridge Article',
            'authors': meta.get('authors', []),
            'author_with_affiliations': author_entries,
            'corresponding_author_emails': emails,
            'abstract': abstract,
            'journal': meta.get('journal') or 'Cambridge University Press',
            'publication_date': meta.get('publication_date'),
            'doi': meta.get('doi') or self.doi,
            'volume': meta.get('volume'),
            'issue': meta.get('issue'),
            'pages': meta.get('pages'),
            'year': meta.get('year'),
            'references': [],
            '_pdf_url': meta.get('pdf_url'),
            '_keywords': meta.get('keywords', []),
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
        """Run the Cambridge handler through the unified publisher contract."""
        # Initialize page and managed resources using shared function
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'CambridgeHandler'
        )

        # Get the actual page URL for correct base_url resolution
        set_actual_base_url(self, page)

        try:
            metadata = await self.extract_metadata(page)
            metadata['doi'] = doi

            pdf_url = metadata.pop('_pdf_url', None)
            keywords = metadata.pop('_keywords', [])

            try:
                fulltext_html = await page.content()
            except Exception:
                fulltext_html = ''

            if fulltext_html and not metadata.get('abstract'):
                metadata['abstract'] = self.extract_main_abstract_from_html(fulltext_html)
            if fulltext_html:
                metadata['references'] = self.extract_references_from_html(fulltext_html)

            figure_urls = {}
            supp_urls = []
            supp_descriptions = {}
            if fulltext_html:
                figure_urls = self.extract_figures_from_html(fulltext_html)
                supp_urls = self._extract_supplemental_links_from_html(fulltext_html)
                supp_descriptions = self._extract_supplemental_descriptions(fulltext_html)

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figure_urls,
                    'supplemental_urls': supp_urls,
                    'supplemental_descriptions': supp_descriptions,
                },
                'fulltext_data': fulltext_html,
                'journal_name': 'cambridge',
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

    @classmethod
    def extract_main_abstract_from_html(cls, html_content: str) -> str:
        """Extract the main abstract as Markdown."""
        if not html_content:
            return ''

        soup = BeautifulSoup(html_content, 'html.parser')
        abstract_div = soup.find('div', class_='article-abstract')
        if not abstract_div:
            return ''

        abstract_content = abstract_div.find('div', class_='abstract-content')
        if not abstract_content:
            return ''

        paragraphs = []
        for p in abstract_content.find_all('p'):
            para_md = cls._convert_html_fragment_to_markdown(str(p))
            if para_md:
                paragraphs.append(para_md)

        return "\n\n".join(paragraphs).strip()

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        """Generate complete Markdown for a Cambridge article."""
        title = metadata.get('title') or 'Cambridge Article'
        md_parts = [
            f"# {title}",
            "",
        ]

        # Authors and affiliations
        author_with_affiliations = metadata.get('author_with_affiliations', [])
        authors = metadata.get('authors', [])
        if author_with_affiliations:
            md_parts.append("**Authors:**")
            md_parts.append("")
            for entry in author_with_affiliations:
                name = entry.get('author', '')
                md_parts.append(name)
                for aff in entry.get('affiliations', []):
                    md_parts.append(aff)
                md_parts.append("")
        elif authors:
            md_parts.append("**Authors:**")
            md_parts.append("")
            for author in authors:
                md_parts.append(author)
                md_parts.append("")

        # Corresponding author email
        emails = metadata.get('corresponding_author_emails', [])
        if emails:
            md_parts.append(f"**Email:** {', '.join(emails)}")
            md_parts.append("")

        if metadata.get('doi'):
            md_parts.extend([f"**DOI:** {metadata['doi']}", ""])

        md_parts.extend([
            "## Publication",
            "",
            f"**Journal:** {metadata.get('journal') or 'Cambridge University Press'}",
            "",
        ])

        if metadata.get('volume'):
            md_parts.append(f"**Volume:** {metadata['volume']}")
            md_parts.append("")
        if metadata.get('pages'):
            md_parts.append(f"**Pages:** {metadata['pages']}")
            md_parts.append("")
        if metadata.get('publication_date'):
            md_parts.append(f"**Published:** {metadata['publication_date']}")
            md_parts.append("")

        # Abstract
        abstract = metadata.get('abstract', '')
        if abstract:
            md_parts.extend([
                "---",
                "",
                "## Abstract",
                "",
                abstract,
                "",
            ])

        # Body text
        body_md = ''
        if isinstance(article_text, str) and article_text.strip():
            if article_text.lstrip().startswith('<'):
                _, body_md = self.extract_article_text_from_html(article_text)
            else:
                body_md = article_text.strip()

        # Insert downloaded figure images after captions
        if kwargs.get('add_figure_refs') and kwargs.get('figure_filenames'):
            figure_filenames = kwargs['figure_filenames']
            for fig_num, filename in sorted(figure_filenames.items(), key=lambda x: int(x[0])):
                body_md = re.sub(
                    rf'(\*\*Figure\s*{re.escape(fig_num)}\.\*\*[^\n]*)',
                    rf'\1\n\n![Figure {fig_num}.]({filename})',
                    body_md,
                )

        md_parts.extend([
            "---",
            "",
            "## Article Text",
            "",
            body_md or "[Article text not found.]",
            "",
        ])

        # Supplemental materials
        supplemental_urls = kwargs.get('supplemental_urls', [])
        supplemental_descriptions = kwargs.get('supplemental_descriptions', {})
        supplemental_downloads = kwargs.get('supplemental_downloads', [])

        if supplemental_urls or supplemental_downloads:
            md_parts.extend([
                "---",
                "",
                "## Supplemental Material",
                "",
            ])
            if supplemental_downloads:
                for dl in supplemental_downloads:
                    md_parts.append(f"- {dl}")
            elif supplemental_urls:
                for url in supplemental_urls:
                    info = supplemental_descriptions.get(url, {})
                    if isinstance(info, dict):
                        title = info.get('title', '')
                        desc = info.get('description', '')
                        file_info = info.get('file_info', '')
                        if title:
                            md_parts.append(f"**{title}**")
                            md_parts.append("")
                        parts = []
                        if desc:
                            parts.append(desc)
                        if file_info:
                            parts.append(f"({file_info})")
                        if parts:
                            md_parts.append(" ".join(parts))
                            md_parts.append("")
                        md_parts.append(f"[{url}]({url})")
                    else:
                        md_parts.append(f"- [{url}]({url}){f' — {info}' if info else ''}")
                    md_parts.append("")
            md_parts.append("")

        # References
        references = metadata.get('references', [])
        crossref_refs = metadata.get('_crossref_references', [])

        if crossref_refs:
            # Use Crossref references if available (unified BibTeX generation)
            md_parts.extend([
                "---",
                "",
                "## References",
                "",
            ])
            for idx, ref in enumerate(crossref_refs, 1):
                # Get original unstructured reference if available
                unstructured = ref.get('unstructured', '')
                if unstructured:
                    md_parts.extend([f"[{idx}] {unstructured}", ""])
                else:
                    # Generate readable text from Crossref data
                    ref_text = generate_reference_text_from_crossref(ref, index=idx)
                    md_parts.extend([ref_text, ""])

                # Generate BibTeX from Crossref data
                ref_key = ref.get('key', f'ref{idx}')
                parts = {
                    'author': ref.get('author', ''),
                    'title': ref.get('article-title', ''),
                    'journal': ref.get('journal-title', ''),
                    'volume': ref.get('volume', ''),
                    'firstpage': ref.get('first-page', ''),
                    'lastpage': ref.get('last-page', ''),
                    'year': str(ref.get('year', '')),
                    'doi': ref.get('DOI', ''),
                }
                # Filter empty values but preserve DOI even if empty
                parts = {k: v for k, v in parts.items() if v or k == 'doi'}
                if any(parts.get(k) for k in ['author', 'title', 'journal']):
                    bibtex = format_as_bibtex(parts, key=ref_key)
                    md_parts.extend(["```bibtex", bibtex, "```", ""])
        elif references:
            md_parts.extend([
                "---",
                "",
                "## References",
                "",
            ])
            for ref in references:
                md_parts.extend([ref, ""])

        return "\n".join(md_parts)

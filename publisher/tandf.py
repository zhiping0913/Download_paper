"""Taylor & Francis handler (tandfonline.com, DOI prefix 10.1080).

**Abstract only, on purpose.** ⚠️ Most T&F articles render their formulas as
**images**, not MathJax or LaTeX -- a full-text extraction would produce a
body whose equations are unrecoverable pictures, and the articles that do ship
real markup are only the last few years'. A body that silently loses its
mathematics is worse than no body, so this handler does not pretend to have
one; it takes what the page states cleanly and leaves the rest to the PDF.

What it does take:

  * **metadata and abstract from ``<script type="application/ld+json">``**.
    T&F publishes a ``ScholarlyArticle`` node there with the title, the
    abstract, the authors, the keywords, the date and the page range -- plain
    text, already assembled, no scraping.
  * **the PDF**, from the page's own "Download PDF" link
    (``/doi/pdf/{doi}``).
  * **supplemental material**, which the page lists in
    ``div.supplemental-material-container``: one ``div.supplement-box`` per
    file, each with the file's name in an ``<h3>`` and an
    ``/action/downloadSupplement?doi=…&file=…`` link.

⚠️ ``isAccessibleForFree`` is NOT turned into ``access: False``. It says
whether the article is free, not whether *we* can read it -- institutional
access is exactly the case it cannot see, and a wrong ``False`` silently skips
an article that would have downloaded (see the access-decision rules in
CLAUDE.md).
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from publisher.base import PublisherHandler
from publisher.wildcard import init_extract_all_page, set_actual_base_url


class TandFHandler(PublisherHandler):
    """Abstract-only handler for Taylor & Francis Online."""

    PUBLISHER = 'tandf'

    SITE_BASE = 'https://www.tandfonline.com'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.SITE_BASE

    # ------------------------------------------------------------------
    # ld+json
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r'[ \t]+', ' ', (text or '').replace('\r', '')).strip()

    @classmethod
    def ld_nodes(cls, html: str) -> List[dict]:
        """Every JSON-LD node on the page, flattened.

        ⚠️ Flattened because T&F nests them: one ``<script>`` holds a list
        whose second element is a ``@graph`` wrapper, and the article node is
        inside that. Walking only the top level finds a ``BreadcrumbList``
        and nothing else.
        """
        soup = BeautifulSoup(html or '', 'html.parser')
        nodes: List[dict] = []
        for script in soup.find_all('script', attrs={'type': 'application/ld+json'}):
            raw = script.string or script.get_text() or ''
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            for item in (data if isinstance(data, list) else [data]):
                if not isinstance(item, dict):
                    continue
                graph = item.get('@graph')
                if isinstance(graph, list):
                    nodes.extend(n for n in graph if isinstance(n, dict))
                else:
                    nodes.append(item)
        return nodes

    @classmethod
    def _node(cls, nodes: List[dict], want_type: str) -> dict:
        for node in nodes:
            node_type = node.get('@type')
            types = node_type if isinstance(node_type, list) else [node_type]
            if want_type in types:
                return node
        return {}

    # ------------------------------------------------------------------
    # Links
    # ------------------------------------------------------------------

    def _site(self) -> str:
        landed = getattr(self, '_landing_url', '') or ''
        match = re.match(r'(https?://[^/]+)', landed)
        return match.group(1) if match else self.SITE_BASE

    @classmethod
    def pdf_url_from_html(cls, html: str, site: str, doi: str) -> str:
        """The "Download PDF" link, or the canonical shape as a fallback.

        ⚠️ Not the ``/doi/epdf/`` one the page shows first: that is the online
        reader, and navigating to it never fires a download.
        """
        soup = BeautifulSoup(html or '', 'html.parser')
        for anchor in soup.find_all('a', href=True):
            href = anchor['href']
            if re.search(r'/doi/pdf/', href):
                return urljoin(site + '/', href)
        return f"{site}/doi/pdf/{doi}" if doi else ''

    async def get_pdf_url(self, doi: str) -> Optional[str]:
        return self.pdf_url_from_html(getattr(self, '_landing_html', '') or '',
                                      self._site(), doi or self.doi or '')

    async def get_supplemental_url(self, doi: str) -> Optional[str]:
        # T&F lists its supplements on the article page; extract_all collects
        # them, so there is no separate page to open.
        return None

    async def get_fulltext_url(self, page) -> str:
        try:
            return page.url or ''
        except Exception:
            return getattr(self, '_landing_url', '') or ''

    @classmethod
    def supplements_from_html(cls, html: str, site: str) -> Tuple[List[dict], Dict[str, str]]:
        """``(links, {url: description})`` from the Supplemental material section.

        Each ``div.supplement-box`` names its file in an ``<h3>`` and links to
        ``/action/downloadSupplement?doi=…&file=…``. 📌 The name is worth
        keeping as the filename hint: the URL's basename is ``downloadSupplement``,
        so without it every file of every article lands under that one name.
        """
        soup = BeautifulSoup(html or '', 'html.parser')
        container = soup.find('div', class_='supplemental-material-container')
        if container is None:
            return [], {}
        links: List[dict] = []
        descriptions: Dict[str, str] = {}
        seen = set()
        for box in container.find_all('div', class_='supplement-box'):
            anchor = box.find('a', href=True)
            if anchor is None:
                continue
            url = urljoin(site + '/', anchor['href'])
            if url in seen:
                continue
            seen.add(url)
            heading = box.find(['h3', 'h4'])
            name = cls._clean(heading.get_text(' ', strip=True)) if heading else ''
            if not name:
                match = re.search(r'[?&]file=([^&]+)', url)
                name = match.group(1) if match else ''
            links.append({'url': url, 'filename': name} if name else {'url': url})
            if name:
                descriptions[url] = name
        return links, descriptions

    # ------------------------------------------------------------------
    # Contract
    # ------------------------------------------------------------------

    async def extract_metadata(self, page) -> dict:
        html = await self.get_page_html(page)
        self._landing_html = html
        if not html:
            return {}
        nodes = self.ld_nodes(html)
        article = self._node(nodes, 'ScholarlyArticle')
        issue = self._node(nodes, 'PublicationIssue')
        soup = BeautifulSoup(html, 'html.parser')

        authors = []
        for author in (article.get('author') or []):
            if isinstance(author, dict):
                name = self._clean(author.get('name') or '')
            else:
                name = self._clean(str(author))
            if name and name not in authors:
                authors.append(name)

        keywords = [k.strip() for k in
                    re.split(r'[;,]', article.get('keywords') or '') if k.strip()]

        abstract = self._clean(article.get('abstract')
                               or article.get('description') or '')
        if not abstract:
            # Fallback to the rendered abstract. Kept because the JSON-LD
            # block is a template detail, and a page without it should still
            # produce the one section this handler exists for.
            section = soup.find('div', class_='abstractSection')
            if section is not None:
                abstract = self._clean(section.get_text(' ', strip=True))
                abstract = re.sub(r'^Abstract\s*', '', abstract)

        date = article.get('datePublished') or issue.get('datePublished') or ''
        year_match = re.search(r'\b(19|20|21)\d{2}\b', str(date))
        journal_meta = soup.find('meta', attrs={'name': 'citation_journal_title'})
        pages = '-'.join(str(p) for p in (article.get('pageStart'),
                                          article.get('pageEnd')) if p)

        return {
            'title': self._clean(article.get('headline') or article.get('name') or ''),
            'authors': authors,
            'abstract': abstract,
            '_keywords': keywords,
            'doi': (article.get('identifier') or self.doi or '').strip(),
            'journal': ((journal_meta.get('content') or '').strip()
                        if journal_meta else ''),
            'issue': str(issue.get('issueNumber') or '').strip(),
            'year': year_match.group(0) if year_match else '',
            'pages': pages,
            'publisher': ((article.get('publisher') or {}).get('name')
                          if isinstance(article.get('publisher'), dict) else ''),
        }

    async def extract_references(self, html: str) -> list:
        # Abstract-only: Crossref's list (crossref.json) is the reference
        # record for these articles.
        return []

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'TandFHandler')
        doi = self.doi
        set_actual_base_url(self, page)

        try:
            live = page.url or ''
        except Exception:
            live = ''
        if live and not live.startswith(('about:', 'chrome://')):
            self._landing_url = live

        metadata = await self.extract_metadata(page)
        metadata['doi'] = doi or metadata.get('doi', '')

        html = getattr(self, '_landing_html', '') or ''
        supplemental_urls, supplemental_descriptions = self.supplements_from_html(
            html, self._site())
        pdf_url = await self.get_pdf_url(doi)

        print(f"  ✓ 标题: {metadata.get('title', '')[:56]}")
        print(f"  ✓ 作者: {len(metadata.get('authors') or [])} 位")
        print(f"  ✓ 摘要: {len(metadata.get('abstract') or '')} 字符")
        print(f"  ✓ 补充材料: {len(supplemental_urls)} 个")
        print(f"  ✓ PDF: {pdf_url or '(未取到)'}")
        print("  ℹ️  T&F 按 abstract-only 处理：网页公式多为图片，不提取正文")

        return {
            'metadata': metadata,
            'links': {
                'pdf_url': pdf_url,
                'figure_urls': {},        # abstract-only
                'supplemental_urls': supplemental_urls,
                'supplemental_descriptions': supplemental_descriptions,
            },
            # No body, so nothing to hand back; page_raw.html is the archive.
            'fulltext_data': '',
            'journal_name': 'tandf',
        }

    # ------------------------------------------------------------------
    # Markdown
    # ------------------------------------------------------------------

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        supplemental_urls = kwargs.get('supplemental_urls') or []
        supplemental_descriptions = kwargs.get('supplemental_descriptions') or {}
        supplemental_downloads = kwargs.get('supplemental_downloads') or []

        md: List[str] = [f"# {metadata.get('title') or 'Taylor & Francis Article'}", '']
        if metadata.get('authors'):
            md.extend(['**Authors:** ' + ', '.join(metadata['authors']), ''])

        md.extend(['## Publication', ''])
        for key, label in (('journal', 'Journal'), ('volume', 'Volume'),
                           ('issue', 'Issue'), ('pages', 'Pages'),
                           ('year', 'Year'), ('doi', 'DOI'),
                           ('publisher', 'Publisher')):
            value = (metadata.get(key) or '').strip()
            if value:
                md.extend([f"**{label}:** {value}", ''])

        md.extend(['---', '', '## Abstract', '',
                   (metadata.get('abstract') or '').strip() or '[No abstract available.]', ''])
        if metadata.get('_keywords'):
            md.extend(['**Keywords:** ' + ', '.join(metadata['_keywords']), ''])

        # Say why there is no body, rather than let its absence read as a
        # failed extraction.
        md.extend(['---', '',
                   '*Taylor & Francis 的网页公式多为图片，本工具对该出版商只提取摘要；'
                   '正文见 `paper.pdf`。*', ''])

        if supplemental_urls or supplemental_downloads:
            md.extend(['---', '', '## Supplemental Material', ''])
            for index, entry in enumerate(supplemental_urls):
                url = entry.get('url', '') if isinstance(entry, dict) else entry
                label = (supplemental_descriptions.get(url)
                         or (entry.get('filename') if isinstance(entry, dict) else '')
                         or url)
                local = (supplemental_downloads[index]
                         if index < len(supplemental_downloads) else '')
                md.append(f"- [{label}](<{local or url}>)")
            md.append('')

        return '\n'.join(md).rstrip() + '\n'

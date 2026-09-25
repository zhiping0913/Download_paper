"""RCSI handler (journals.rcsi.science) — metadata + PDF only.

The Russian Center for Scientific Information runs an OJS-style platform for
Russian Academy of Sciences journals. The article page carries no body text,
only metadata and a PDF, so this handler extracts metadata and hands the PDF
URL to the download ladder. No body, figure or supplemental extraction.

Where the metadata comes from
-----------------------------
Not the landing page: the platform publishes the article's **JATS XML** at the
same path with ``view`` replaced by ``xml``::

    https://journals.rcsi.science/0044-4510/article/view/247372
    https://journals.rcsi.science/0044-4510/article/xml/247372

That XML carries both languages in one document -- ``<article-title
xml:lang="en">`` beside the Russian ``<trans-title>``, an English
``<abstract>`` beside a Russian ``<trans-abstract>``, and every contributor
with a ``<name xml:lang="en">`` and a ``<name xml:lang="ru">``. So unlike
J-STAGE, one request is enough for both spellings.

⚠️ The XML is served as ``application/xml``, which the response listener does
not record (it keeps ``text/html`` documents), so it is fetched through the
shared fetch ladder rather than read out of the capture.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup

from core.utilities import fetch_html_via_ladder
from publisher.base import PublisherHandler
from publisher.wildcard import init_extract_all_page, set_actual_base_url


class RCSIHandler(PublisherHandler):
    """Metadata and PDF for an article on journals.rcsi.science."""

    PUBLISHER = 'rcsi'

    # ------------------------------------------------------------------
    # URLs
    # ------------------------------------------------------------------

    @staticmethod
    def article_id_from_url(url: str) -> str:
        match = re.search(r'/article/(?:view|xml|download)/(\d+)', url or '')
        return match.group(1) if match else ''

    @classmethod
    def xml_url_from_article_url(cls, url: str) -> str:
        """``/article/view/247372`` → ``/article/xml/247372``."""
        if not url or '/article/view/' not in url:
            return ''
        return url.split('?')[0].rstrip('/').replace('/article/view/',
                                                     '/article/xml/')

    @classmethod
    def pdf_url_from_landing(cls, landing_html: str, url: str) -> str:
        """The PDF link, preferring what the page itself declares.

        ⚠️ ``citation_pdf_url`` is the only complete source. The download path
        carries a **second** id -- the file's, not the article's
        (``/article/download/247372/225467``) -- and nothing in the view URL
        or the XML says what it is. Replacing ``view`` with ``download`` gets
        the article-level path only, which is why that is the fallback and not
        the rule.
        """
        if landing_html:
            soup = BeautifulSoup(landing_html, 'html.parser')
            tag = soup.find('meta', attrs={'name': 'citation_pdf_url'})
            declared = (tag.get('content') or '').strip() if tag else ''
            if declared:
                return declared
        if url and '/article/view/' in url:
            print("  ⚠️  页面没有 citation_pdf_url，按 view→download 构造"
                  "（缺文件 id，可能需要平台自行跳转）")
            return url.split('?')[0].rstrip('/').replace('/article/view/',
                                                         '/article/download/')
        return ''

    async def get_pdf_url(self, doi: str) -> str:
        return self.pdf_url_from_landing(
            getattr(self, '_landing_html', '') or '',
            getattr(self, '_landing_url', '') or '')

    async def get_fulltext_url(self, page) -> str:
        return ''

    async def get_supplemental_url(self, doi: str) -> str:
        return None

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    # ------------------------------------------------------------------
    # JATS XML
    # ------------------------------------------------------------------

    @staticmethod
    def _soup(xml_text: str):
        # 'xml' keeps xml:lang attributes addressable; html.parser lowercases
        # and mangles the namespaced ones.
        return BeautifulSoup(xml_text, 'xml') if xml_text else None

    @classmethod
    def titles_from_xml(cls, xml_text: str) -> Tuple[str, str]:
        """``(english, russian)`` article titles.

        ⚠️ Scoped to ``article-meta``. The journal's own name is also a
        ``<trans-title>`` (Журнал экспериментальной и теоретической физики)
        and sits earlier in the document, so a document-wide search for the
        first ``trans-title`` returns the *journal* title.
        """
        soup = cls._soup(xml_text)
        if soup is None:
            return '', ''
        meta = soup.find('article-meta')
        group = meta.find('title-group') if meta else None
        if group is None:
            return '', ''
        en_el = group.find('article-title')
        ru_el = group.find('trans-title')
        en = en_el.get_text(' ', strip=True) if en_el is not None else ''
        ru = ru_el.get_text(' ', strip=True) if ru_el is not None else ''
        return en, ru

    @classmethod
    def authors_from_xml(cls, xml_text: str) -> Tuple[List[str], List[str]]:
        """``(english_names, russian_names)`` in document order.

        Each ``<contrib>`` carries both spellings as ``<name xml:lang="en">``
        and ``<name xml:lang="ru">``; the order of the two inside a contrib is
        not fixed, so they are picked by attribute rather than by position.
        """
        soup = cls._soup(xml_text)
        if soup is None:
            return [], []
        en_names, ru_names = [], []
        meta = soup.find('article-meta')
        if meta is None:
            return [], []
        for contrib in meta.find_all('contrib'):
            for name in contrib.find_all(['name', 'string-name']):
                lang = (name.get('xml:lang') or name.get('lang') or '').lower()
                text = cls._person_name(name)
                if not text:
                    continue
                if lang == 'ru' and text not in ru_names:
                    ru_names.append(text)
                elif lang != 'ru' and text not in en_names:
                    en_names.append(text)
        return en_names, ru_names

    @staticmethod
    def _person_name(name_el) -> str:
        """"Surname Initials" for a JATS ``<name>``, or its text."""
        surname = name_el.find('surname')
        given = name_el.find('given-names')
        if surname is not None or given is not None:
            parts = [el.get_text(' ', strip=True)
                     for el in (surname, given) if el is not None]
            return ' '.join(p for p in parts if p).strip()
        return re.sub(r'\s+', ' ', name_el.get_text(' ', strip=True)).strip()

    @classmethod
    def abstracts_from_xml(cls, xml_text: str) -> Tuple[str, str]:
        """``(english, russian)`` abstracts, heading stripped."""
        soup = cls._soup(xml_text)
        if soup is None:
            return '', ''
        meta = soup.find('article-meta')
        if meta is None:
            return '', ''

        def text_of(el) -> str:
            if el is None:
                return ''
            for title in el.find_all('title'):
                title.decompose()
            return re.sub(r'\s+', ' ', el.get_text(' ', strip=True)).strip()

        return text_of(meta.find('abstract')), text_of(meta.find('trans-abstract'))

    @classmethod
    def references_from_xml(cls, xml_text: str) -> List[str]:
        """Reference strings, as the publisher printed them."""
        soup = cls._soup(xml_text)
        if soup is None:
            return []
        refs = []
        for ref in soup.find_all('ref'):
            citation = ref.find(['mixed-citation', 'element-citation',
                                 'nlm-citation'])
            text = (citation or ref).get_text(' ', strip=True)
            text = re.sub(r'\s+', ' ', text).strip()
            if text:
                refs.append(text)
        return refs

    @classmethod
    def journal_meta_from_xml(cls, xml_text: str) -> dict:
        soup = cls._soup(xml_text)
        if soup is None:
            return {}

        def first(tag_name, parent=None):
            el = (parent or soup).find(tag_name)
            return el.get_text(' ', strip=True) if el is not None else ''

        meta = soup.find('article-meta')
        jmeta = soup.find('journal-meta')
        year = ''
        pub_date = meta.find('pub-date') if meta else None
        if pub_date is not None:
            year = first('year', pub_date)
        return {
            'journal': first('journal-title', jmeta) if jmeta else '',
            'publisher': first('publisher-name', jmeta) if jmeta else '',
            'volume': first('volume', meta) if meta else '',
            'issue': first('issue', meta) if meta else '',
            'pages': '-'.join(p for p in (first('fpage', meta) if meta else '',
                                          first('lpage', meta) if meta else '')
                              if p),
            'year': year,
            'keywords': [k.get_text(' ', strip=True)
                         for k in (meta.find_all('kwd') if meta else [])
                         if k.get_text(strip=True)],
        }

    # ------------------------------------------------------------------
    # Contract
    # ------------------------------------------------------------------

    async def extract_metadata(self, page) -> dict:
        html = await self.get_page_html(page)
        self._landing_html = html
        return {'title': '', 'authors': [], 'abstract': ''}

    async def extract_references(self, html: str) -> list:
        return self.references_from_xml(getattr(self, '_article_xml', '') or '')

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'RCSIHandler')
        doi = self.doi
        set_actual_base_url(self, page)

        landed = getattr(self, '_landing_url', '') or ''
        try:
            live = page.url or ''
        except Exception:
            live = ''
        if live and not live.startswith(('about:', 'chrome://')):
            landed = live
        self._landing_url = landed

        landing_html = await self.get_page_html(page)
        self._landing_html = landing_html

        xml_url = self.xml_url_from_article_url(landed)
        xml_text = ''
        if xml_url:
            print(f"  📄 取 JATS XML: {xml_url}")
            # ⚠️ Through the ladder, not the capture: the XML is served as
            # application/xml and the response listener only records
            # text/html documents.
            xml_text = await fetch_html_via_ladder(
                xml_url, kind='api', page=page,
                context=getattr(page, 'context', None),
                referer=landed,
                expect=lambda body: '<article' in (body or '').lower(),
                headless=not self.is_headed_run(),
                restore_url=landed,
                # ⚠️ The plain request first, not the usual 'tab': the XML is
                # served as application/xml, so a browser tab hands back
                # Chrome's *XML viewer* DOM (an <html> wrapper with the
                # source embedded) rather than the publisher's bytes. It
                # happens to still parse, but what lands in article.xml would
                # not be the document the publisher served.
                default=('request', 'tab', 'fresh'))
            if xml_text:
                self._save_text('article.xml', xml_text)
            else:
                print("  ⚠️  未取到 XML —— 元数据将只剩落地页能给的部分")
        else:
            print(f"  ⚠️  落地 URL 不是 /article/view/…（{landed or '(空)'}），"
                  f"无法构造 XML 地址")
        self._article_xml = xml_text

        en_title, ru_title = self.titles_from_xml(xml_text)
        en_authors, ru_authors = self.authors_from_xml(xml_text)
        en_abstract, ru_abstract = self.abstracts_from_xml(xml_text)
        jmeta = self.journal_meta_from_xml(xml_text)

        if not (en_title or ru_title) and landing_html:
            soup = BeautifulSoup(landing_html, 'html.parser')
            tag = soup.find('meta', attrs={'name': 'citation_title'})
            en_title = (tag.get('content') or '').strip() if tag else ''

        # Both languages in the searchable fields, the same convention J-STAGE
        # uses: one metadata.json query hits either spelling.
        titles = [t for t in (ru_title, en_title) if t]
        authors = ru_authors + [n for n in en_authors if n not in ru_authors]

        metadata = {
            'title': ' '.join(titles),
            # The folder keeps the English title: it is what the DOI resolves
            # to in Crossref, and a Cyrillic directory name is harder to type
            # and search for on the machines this corpus is read on.
            '_dir_title': en_title or ru_title,
            '_title_en': en_title,
            '_title_ru': ru_title,
            'authors': authors,
            '_authors_en': en_authors,
            '_authors_ru': ru_authors,
            'abstract': ru_abstract or en_abstract,
            '_abstract_en': en_abstract if en_abstract != ru_abstract else '',
            'doi': doi,
            'journal': jmeta.get('journal', ''),
            'publisher': jmeta.get('publisher', ''),
            'volume': jmeta.get('volume', ''),
            'issue': jmeta.get('issue', ''),
            'pages': jmeta.get('pages', ''),
            'year': jmeta.get('year', ''),
            'references': self.references_from_xml(xml_text),
            '_keywords': jmeta.get('keywords', []),
        }

        print(f"  ✓ 标题(en): {en_title[:56]}")
        print(f"  ✓ 标题(ru): {ru_title[:56]}")
        print(f"  ✓ 作者: {len(authors)} 位（ru {len(ru_authors)} / en {len(en_authors)}）")
        print(f"  ✓ 摘要: ru {len(ru_abstract)} / en {len(en_abstract)} 字符")
        print(f"  ✓ 参考文献: {len(metadata['references'])} 条")

        pdf_url = self.pdf_url_from_landing(landing_html, landed)
        print(f"  ✓ PDF: {pdf_url or '(未取到)'}")

        return {
            'metadata': metadata,
            'links': {
                'pdf_url': pdf_url,
                'figure_urls': {},
                'supplemental_urls': [],
                'supplemental_descriptions': {},
            },
            # No publisher-hosted body; '' keeps the workflow from writing a
            # page.html that would only duplicate page_raw.html.
            'fulltext_data': '',
            'journal_name': 'rcsi',
        }

    def _save_text(self, name: str, text: str) -> None:
        if not (self.captured_data_dir and text):
            return
        try:
            from pathlib import Path
            out = Path(self.captured_data_dir) / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding='utf-8')
            print(f"  ✓ {name} 已保存 ({len(text):,} 字符)")
        except OSError as exc:
            print(f"  ⚠️  {name} 保存失败: {exc}")

    # ------------------------------------------------------------------
    # Markdown
    # ------------------------------------------------------------------

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        md: List[str] = []
        en = metadata.get('_title_en') or ''
        ru = metadata.get('_title_ru') or ''
        md.extend([f"# {en or ru or metadata.get('title') or 'RCSI Article'}", ''])
        if en and ru:
            md.extend([f"**Русское название:** {ru}", ''])

        if metadata.get('_authors_en'):
            md.extend(['**Authors:** ' + ', '.join(metadata['_authors_en']), ''])
        if metadata.get('_authors_ru'):
            md.extend(['**Авторы:** ' + ', '.join(metadata['_authors_ru']), ''])

        md.extend(['## Publication', ''])
        for key, label in (('journal', 'Journal'), ('volume', 'Volume'),
                           ('issue', 'Issue'), ('pages', 'Pages'),
                           ('year', 'Year'), ('doi', 'DOI'),
                           ('publisher', 'Publisher')):
            value = (metadata.get(key) or '').strip()
            if value:
                md.extend([f"**{label}:** {value}", ''])

        en_abstract = (metadata.get('_abstract_en') or '').strip()
        ru_abstract = (metadata.get('abstract') or '').strip()
        md.extend(['---', '', '## Abstract', '',
                   en_abstract or ru_abstract or '[No abstract available.]', ''])
        if en_abstract and ru_abstract and ru_abstract != en_abstract:
            md.extend(['## Аннотация', '', ru_abstract, ''])

        if metadata.get('_keywords'):
            md.extend(['**Keywords:** ' + ', '.join(metadata['_keywords']), ''])

        # No "## Article Text": the platform publishes none, and an empty
        # heading reads as a failed extraction.
        references = metadata.get('references') or []
        if references:
            md.extend(['---', '', '## References', ''])
            for index, ref in enumerate(references, 1):
                md.extend([f"[{index}] {ref}", ''])

        return '\n'.join(md)

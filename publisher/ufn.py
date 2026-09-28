"""UFN handler — Успехи физических наук / Physics-Uspekhi (ufn.ru).

**Abstract only, plus the PDF.** The site publishes no full text as HTML.

⚠️ **Every article exists twice**, as a Russian and an English edition, each
with its own DOI and its own page, and **the two DOIs have no derivable
relationship**::

    10.3367/UFNr.2023.03.039335      10.3367/UFNe.2023.03.039335
    10.3367/ufnr.0072.196010a.0161   10.1070/PU1961v003n05ABEH003322

📌 The two **pages**, on the other hand, differ by one path segment:
``https://ufn.ru/ru/articles/2023/5/b/`` ↔ ``https://ufn.ru/en/articles/2023/5/b/``.
That is the hinge this handler turns on: whichever edition the DOI lands on,
the counterpart is one string substitution away, so **the output is the same
either way** -- the Russian edition is the record (directory name, title,
DOI), and the English one is attached as ``additional_doi`` /
``additional_title`` / ``additional_author``.

⚠️ An English DOI can also be an **IOP** one (``10.1070/PU…``) because IOP
publishes the English edition -- and it still resolves to ufn.ru. So routing
is by the landed domain, not by DOI prefix.

Where the fields come from (both pages carry the same shapes):

  * ``<span class="gray">DOI:</span> <noindex><a href="…">…</a></noindex>`` --
    that page's own DOI. ⚠️ Not ``citation_doi``: the pages do not declare one.
  * ``citation_title`` / ``citation_author`` (repeated per author) /
    ``citation_journal_title`` / volume / issue / first-last page / date.
  * ``<p itemprop="articleBody" class="mathjax">`` -- the abstract.
  * ``citation_pdf_url`` -- **only the Russian page has it**, which is why the
    PDF is always taken from that side.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from publisher.base import PublisherHandler
from publisher.wildcard import (
    convert_html_fragment_to_markdown,
    goto_and_capture_document,
    init_extract_all_page,
    set_actual_base_url,
)


class UFNHandler(PublisherHandler):
    """Abstract + PDF for Physics-Uspekhi / УФН, both language editions."""

    PUBLISHER = 'ufn'

    SITE_BASE = 'https://ufn.ru'

    #: The language segment in ``https://ufn.ru/<lang>/articles/2023/5/b/``.
    _LANG_RE = re.compile(r'^(https?://[^/]+)/(ru|en)(/.*)$', re.I)

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.SITE_BASE

    # ------------------------------------------------------------------
    # URLs
    # ------------------------------------------------------------------

    @classmethod
    def language_of(cls, url: str) -> str:
        """``'ru'``, ``'en'``, or ``''`` when the URL says nothing."""
        match = cls._LANG_RE.match((url or '').strip())
        return match.group(2).lower() if match else ''

    #: An article page's own path: /ru/articles/2023/5/b/
    _ARTICLE_PATH_RE = re.compile(r'^/(ru|en)/articles/\d{4}/\d+/[a-z]+/?$', re.I)

    @classmethod
    def url_for_language(cls, url: str, language: str) -> str:
        """The counterpart's URL guessed by swapping the language segment.

        ❌ **Only a fallback.** The two editions do not always share a path:
        ``10.3367/ufnr.0072.196010a.0161`` is ``/ru/articles/1960/10/a/`` while
        its English edition is ``/en/articles/1961/5/d/`` -- different year,
        issue AND letter, because the translation appeared a year later. A
        swap would have fetched some *other* article, or a 404, and called it
        the counterpart. Use :meth:`counterpart_from_html` first.
        """
        match = cls._LANG_RE.match((url or '').strip())
        if not match:
            return ''
        return f"{match.group(1)}/{language}{match.group(3)}"

    @classmethod
    def counterpart_from_html(cls, html: str, landed_url: str) -> str:
        """The other edition's URL, as the page itself links to it.

        📌 Every page carries a language switch -- an anchor reading
        "English" on a Russian page and "Русский" on an English one -- and
        that link is the publisher's own statement of which article is the
        translation. Measured on all four samples, including the 1960/1961
        pair whose paths do not correspond.

        The match is on the href shape plus a language different from the
        page's, not on the anchor text: the text is the thing most likely to
        be reworded, and sibling-article links are in the page's own language
        so they cannot be confused with it.
        """
        if not html:
            return ''
        landed_lang = cls.language_of(landed_url)
        soup = BeautifulSoup(html, 'html.parser')
        base = (cls._LANG_RE.match(landed_url or '') or [None, cls.SITE_BASE])[1] \
            if cls._LANG_RE.match(landed_url or '') else cls.SITE_BASE
        for anchor in soup.find_all('a', href=True):
            href = anchor['href'].strip()
            match = cls._ARTICLE_PATH_RE.match(href)
            if not match:
                continue
            if landed_lang and match.group(1).lower() == landed_lang:
                continue
            return base + href
        return ''

    async def get_fulltext_url(self, page) -> str:
        try:
            return page.url or ''
        except Exception:
            return getattr(self, '_landing_url', '') or ''

    async def get_supplemental_url(self, doi: str) -> Optional[str]:
        # Nothing on these pages beyond the PDF.
        return None

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def extract_references(self, html: str) -> list:
        # Abstract-only; crossref.json carries the reference record.
        return []

    async def get_pdf_url(self, doi: str) -> Optional[str]:
        """``citation_pdf_url`` from the **Russian** page.

        ⚠️ The English page does not declare one (measured on both samples),
        so asking the landed page would leave every English-DOI run without a
        PDF.
        """
        return self._pdf_url

    # ------------------------------------------------------------------
    # Page parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r'\s+', ' ', text or '').strip()

    @staticmethod
    def _meta_all(soup: BeautifulSoup, name: str) -> List[str]:
        return [(m.get('content') or '').strip()
                for m in soup.find_all('meta', attrs={'name': name})
                if (m.get('content') or '').strip()]

    @classmethod
    def _meta(cls, soup: BeautifulSoup, name: str) -> str:
        values = cls._meta_all(soup, name)
        return values[0] if values else ''

    @classmethod
    def doi_from_html(cls, soup: BeautifulSoup) -> str:
        """The DOI of **this** edition: the anchor after its "DOI:" label.

        ⚠️ It has to be anchored to that label. A page carries several
        ``doi.org`` links -- the Russian page advertises the English edition
        twice ("English fulltext is available at DOI: …" and the suggested
        citation of the translation) **before** printing its own -- so taking
        the first one gives the wrong edition: measured, the Russian page for
        10.3367/UFNr.2023.03.039335 reported ``UFNe…`` instead.

        There is no ``citation_doi`` meta on these pages, so this markup is
        the only statement of it::

            <span class="gray">DOI:</span> <noindex><a
              href="https://doi.org/10.3367/UFNr.2023.03.039335">…</a></noindex>
        """
        for label in soup.find_all('span', class_='gray'):
            if 'DOI' not in label.get_text():
                continue
            # The anchor is a following sibling (sometimes wrapped in
            # <noindex>), not a child of the label.
            for sibling in label.next_elements:
                name = getattr(sibling, 'name', None)
                if name == 'a' and sibling.get('href'):
                    match = re.search(r'(10\.\d{4,9}/\S+?)(?:["\s<]|$)',
                                      sibling['href'].replace('%2F', '/'))
                    if match:
                        return match.group(1).rstrip('/')
                if name == 'span' and sibling is not label:
                    break   # next field started; this label had no anchor
        return ''

    @classmethod
    def abstract_from_html(cls, soup: BeautifulSoup) -> str:
        """The abstract, through the shared formula pipeline.

        📌 The paragraph is marked ``class="mathjax"``, i.e. the publisher
        expects math in it, so it goes through the fragment converter rather
        than ``get_text()``.
        """
        parts: List[str] = []
        for para in soup.find_all(attrs={'itemprop': 'articleBody'}):
            md = convert_html_fragment_to_markdown(para.decode_contents())
            md = cls._clean(md)
            if md:
                parts.append(md)
        return '\n\n'.join(parts)

    @classmethod
    def parse_page(cls, html: str) -> dict:
        """Everything one language's page states."""
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')
        headline = soup.find(attrs={'itemprop': 'headline'})
        pages = '-'.join(p for p in (cls._meta(soup, 'citation_firstpage'),
                                     cls._meta(soup, 'citation_lastpage')) if p)
        date = cls._meta(soup, 'citation_date')
        year_match = re.search(r'\b(19|20|21)\d{2}\b', date)
        return {
            'doi': cls.doi_from_html(soup),
            'title': (cls._meta(soup, 'citation_title')
                      or (cls._clean(headline.get_text(' ', strip=True))
                          if headline else '')),
            'authors': cls._meta_all(soup, 'citation_author'),
            'abstract': cls.abstract_from_html(soup),
            'journal': cls._meta(soup, 'citation_journal_title'),
            'issn': cls._meta(soup, 'citation_issn'),
            'volume': cls._meta(soup, 'citation_volume'),
            'issue': cls._meta(soup, 'citation_issue'),
            'pages': pages,
            'year': year_match.group(0) if year_match else '',
            'pdf_url': cls._meta(soup, 'citation_pdf_url'),
        }

    # ------------------------------------------------------------------
    # Contract
    # ------------------------------------------------------------------

    async def extract_metadata(self, page) -> dict:
        html = await self.get_page_html(page)
        self._landing_html = html
        parsed = self.parse_page(html)
        return {
            'title': parsed.get('title', ''),
            'authors': parsed.get('authors', []),
            'abstract': parsed.get('abstract', ''),
            'doi': parsed.get('doi') or (self.doi or ''),
        }

    def _save_text(self, name: str, text: str) -> None:
        if not (self.captured_data_dir and text):
            return
        try:
            out = Path(self.captured_data_dir) / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding='utf-8')
            print(f"  ✓ {name} 已保存 ({len(text):,} 字符)")
        except OSError as exc:
            print(f"  ⚠️  {name} 保存失败: {exc}")

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'UFNHandler')
        doi = self.doi
        set_actual_base_url(self, page)

        try:
            live = page.url or ''
        except Exception:
            live = ''
        if live and not live.startswith(('about:', 'chrome://')):
            self._landing_url = live
        landed = getattr(self, '_landing_url', '') or ''

        landed_html = await self.get_page_html(page)
        self._landing_html = landed_html
        landed_lang = self.language_of(landed) or 'ru'
        other_lang = 'en' if landed_lang == 'ru' else 'ru'

        pages_html = {landed_lang: landed_html}
        # The page's own language switch first; the path swap only if it is
        # missing (see url_for_language for why the swap cannot be trusted).
        other_url = self.counterpart_from_html(landed_html, landed)
        if other_url:
            print(f"  ↪ 另一语种取自页面自己的语言切换链接")
        else:
            other_url = self.url_for_language(landed, other_lang)
            if other_url:
                print(f"  ⚠️  页面没有语言切换链接，改按路径替换猜 "
                      f"{other_lang} 版（两版路径未必对应，结果要核对）")
        if other_url:
            print(f"  🌐 另取 {other_lang} 版页面: {other_url}")
            # ⚠️ Through goto_and_capture_document: it lands the ORIGINAL
            # response rather than the rendered DOM, which is what every other
            # handler in this repo reads, and it retries a miss instead of
            # silently returning a shell.
            other_html = await goto_and_capture_document(page, other_url)
            if other_html:
                pages_html[other_lang] = other_html
            else:
                print(f"  ⚠️  {other_lang} 版页面没取到 —— "
                      f"只有 {landed_lang} 版的信息")
        else:
            print(f"  ⚠️  落地 URL 不是 /ru/ 或 /en/ 形式（{landed or '(空)'}），"
                  f"无法推出另一语种")

        for lang, html in pages_html.items():
            self._save_text(f'page_{lang}.html', html)

        russian = self.parse_page(pages_html.get('ru', ''))
        english = self.parse_page(pages_html.get('en', ''))
        # The Russian edition is the record; fall back to the English one when
        # its page could not be fetched, so a run never ends up with nothing.
        record = russian or english
        other = english if russian else {}

        self._pdf_url = (russian.get('pdf_url') or english.get('pdf_url') or '')

        # 📌 Pin the record edition's URL so metadata.json's `link` does not
        # depend on which DOI was typed. The other edition is still recorded,
        # in additional_doi/title/author.
        record_url = (self.counterpart_from_html(landed_html, landed)
                      if landed_lang != 'ru' else landed)
        metadata = {
            '_landing_url': record_url or landed,
            'title': record.get('title', ''),
            '_dir_title': record.get('title', ''),
            'authors': record.get('authors', []),
            'abstract': record.get('abstract', ''),
            'doi': record.get('doi') or (doi or ''),
            # ⚠️ The record's own DOI, not the one that was typed: an English
            # DOI has to produce the same metadata.json as the Russian one.
            '_canonical_doi': record.get('doi') or (doi or ''),
            'journal': record.get('journal', ''),
            'volume': record.get('volume', ''),
            'issue': record.get('issue', ''),
            'pages': record.get('pages', ''),
            'year': record.get('year', ''),
            'ISSN': record.get('issn', ''),
            '_abstract_ru': russian.get('abstract', ''),
            '_abstract_en': english.get('abstract', ''),
            '_title_ru': russian.get('title', ''),
            '_title_en': english.get('title', ''),
            '_authors_ru': russian.get('authors', []),
            '_authors_en': english.get('authors', []),
            '_publication_en': {k: english.get(k, '') for k in
                                ('journal', 'volume', 'issue', 'pages', 'year')},
        }
        for key, value in (('additional_doi', other.get('doi', '')),
                           ('additional_title', other.get('title', '')),
                           ('additional_author', other.get('authors', []))):
            if value:
                metadata[key] = value if isinstance(value, list) else [value]

        print(f"  ✓ 俄文标题: {metadata.get('_title_ru', '')[:44]}")
        print(f"  ✓ 英文标题: {metadata.get('_title_en', '')[:44]}")
        print(f"  ✓ DOI: 俄 {russian.get('doi') or '(无)'} / "
              f"英 {english.get('doi') or '(无)'}")
        print(f"  ✓ 作者: 俄 {len(metadata['_authors_ru'])} / "
              f"英 {len(metadata['_authors_en'])}")
        print(f"  ✓ 摘要: 俄 {len(metadata['_abstract_ru'])} / "
              f"英 {len(metadata['_abstract_en'])} 字符")
        print(f"  ✓ PDF: {self._pdf_url or '(未取到)'}")
        print("  ℹ️  UFN 按 abstract-only 处理：站点不提供 HTML 正文")

        return {
            'metadata': metadata,
            'links': {
                'pdf_url': self._pdf_url,
                'figure_urls': {},
                'supplemental_urls': [],
                'supplemental_descriptions': {},
            },
            # page_ru.html / page_en.html are the archive; there is no body.
            'fulltext_data': '',
            'journal_name': 'ufn',
        }

    # ------------------------------------------------------------------
    # Markdown
    # ------------------------------------------------------------------

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        title_ru = metadata.get('_title_ru') or ''
        title_en = metadata.get('_title_en') or ''
        md: List[str] = [f"# {title_ru or title_en or 'UFN Article'}", '']
        if title_en and title_ru:
            md.extend([f"**English title:** {title_en}", ''])

        if metadata.get('_authors_ru'):
            md.extend(['**Авторы:** ' + ', '.join(metadata['_authors_ru']), ''])
        if metadata.get('_authors_en'):
            md.extend(['**Authors:** ' + ', '.join(metadata['_authors_en']), ''])

        md.extend(['## Publication', ''])
        for key, label in (('journal', 'Journal'), ('volume', 'Volume'),
                           ('issue', 'Issue'), ('pages', 'Pages'),
                           ('year', 'Year'), ('doi', 'DOI')):
            value = (metadata.get(key) or '').strip()
            if value:
                md.extend([f"**{label}:** {value}", ''])
        english = metadata.get('_publication_en') or {}
        if metadata.get('additional_doi') or english.get('journal'):
            parts = [english.get('journal', '')]
            if english.get('volume'):
                parts.append(f"vol. {english['volume']}")
            if english.get('issue'):
                parts.append(f"no. {english['issue']}")
            if english.get('pages'):
                parts.append(f"pp. {english['pages']}")
            md.extend(['**English edition:** ' + ', '.join(p for p in parts if p)
                       + (f" — DOI {metadata['additional_doi'][0]}"
                          if metadata.get('additional_doi') else ''), ''])

        abstract_ru = (metadata.get('_abstract_ru') or '').strip()
        abstract_en = (metadata.get('_abstract_en') or '').strip()
        md.extend(['---', '', '## Аннотация', '', abstract_ru or '[нет аннотации]', ''])
        if abstract_en:
            md.extend(['## Abstract', '', abstract_en, ''])

        # Say why there is no body rather than let its absence read as a
        # failed extraction.
        md.extend(['---', '',
                   '*ufn.ru 不提供 HTML 正文，本工具对该出版商只提取摘要；'
                   '正文见 `paper.pdf`。*', ''])
        return '\n'.join(md).rstrip() + '\n'

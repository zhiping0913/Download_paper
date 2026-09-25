"""J-STAGE handler (jstage.jst.go.jp) — abstract + PDF only.

**Abstract-only, by observation.** Every J-STAGE article seen so far offers
an abstract (抄録) and a PDF; no publisher-hosted full text exists to scrape.
So this handler collects metadata, the abstract and the reference list, and
hands the PDF URL to the normal download ladder. There is no body, no figure
and no supplemental extraction, and the interfaces stay empty rather than
guessing.

Two languages, one article
--------------------------
``doi.org/{doi}`` lands on ``…/_article/-char/ja`` or ``…/-char/en``
depending on what the browser asks for, and the two pages are **not**
translations of one record -- each carries its own title and author spelling:

    -char/ja   meta[title]   高強度レーザーパルスによる非線形Compton 散乱の モンテカルロ法
               meta[authors] 瀬戸 慧大
    -char/en   meta[title]   Monte Carlo Method for Nonlinear Compton Scattering …
               meta[authors] Keita SETO

⚠️ The ``citation_*`` tags are **identical on both** and always Japanese, so
they cannot supply the English title. That is why this handler fetches the
*other* language too, and why the language-specific values come from
``meta[name=title]`` / ``meta[name=authors]`` rather than from ``citation_*``.

What lands in the output
------------------------
* ``metadata['title']`` -- ``"<日本語> <English>"``, both spellings in one
  string so a search over metadata.json hits either.
* ``metadata['_dir_title']`` -- the Japanese title alone, for the folder name.
* ``metadata['authors']`` -- ``["瀬戸 慧大", "Keita SETO"]``, same reasoning.
* ``html/page_ja.html`` and ``html/page_en.html`` -- both responses, because
  each holds something the other does not.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup

from publisher.base import PublisherHandler
from publisher.wildcard import (
    goto_and_capture_document,
    init_extract_all_page,
    set_actual_base_url,
)


class JStageHandler(PublisherHandler):
    """Metadata, abstract and references for a J-STAGE article."""

    PUBLISHER = 'jstage'

    #: Neither language is "the" page; whichever one the redirect gave us is
    #: fetched from the capture, the other with one extra navigation.
    LANGS = ('ja', 'en')

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self._html_by_lang = {}

    # ------------------------------------------------------------------
    # URLs
    # ------------------------------------------------------------------

    @staticmethod
    def lang_of_url(url: str) -> str:
        """``'ja'`` / ``'en'`` / ``''`` for a J-STAGE article URL."""
        match = re.search(r'/-char/([a-z]{2})', url or '', re.I)
        return match.group(1).lower() if match else ''

    @classmethod
    def article_url_for(cls, url: str, lang: str) -> str:
        """The same article in *lang*."""
        base = re.sub(r'/-char/[a-z]{2}.*$', '', url or '', flags=re.I)
        return f"{base.rstrip('/')}/-char/{lang}"

    @classmethod
    def pdf_url_from_article_url(cls, url: str) -> str:
        """``…/_article/-char/ja`` → ``…/_pdf``.

        ⚠️ Built from the URL the redirect actually landed on, not from the
        DOI: J-STAGE's path carries the journal's own volume/page identifiers
        (``/article/lsj/51/5/51_337/``) which cannot be derived from
        ``10.2184/lsj.51.5_337`` reliably. Everything after ``/-char`` is
        dropped -- the bare ``_pdf`` path serves the file.
        """
        base = re.sub(r'/-char/[a-z]{2}.*$', '', url or '', flags=re.I)
        if '/_article' not in base:
            return ''
        return base.replace('/_article', '/_pdf').rstrip('/')

    async def get_pdf_url(self, doi: str) -> str:
        return self.pdf_url_from_article_url(getattr(self, '_landing_url', '') or '')

    async def get_fulltext_url(self, page) -> str:
        return ''

    async def get_supplemental_url(self, doi: str) -> str:
        return None

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _meta(soup, name: str) -> str:
        tag = soup.find('meta', attrs={'name': name})
        return (tag.get('content') or '').strip() if tag is not None else ''

    @classmethod
    def title_from_html(cls, html: str) -> str:
        """The article title in this page's language."""
        if not html:
            return ''
        soup = BeautifulSoup(html, 'html.parser')
        # meta[title] is language-specific; citation_title is not (it is always
        # Japanese on both pages), so it is deliberately not consulted here.
        title = cls._meta(soup, 'title')
        if title:
            return title
        el = soup.select_one('div.global-article-title')
        return el.get_text(' ', strip=True) if el is not None else ''

    #: The English page prints this instead of a name when the author has no
    #: Latin spelling. It is a placeholder, not a person.
    _NAME_PLACEHOLDERS = ('[in japanese]', '[in english]')

    @staticmethod
    def _norm_name(name: str) -> str:
        """Collapse the double spaces J-STAGE's DOM puts inside names.

        ⚠️ The DOM writes ``余語  覚文`` (two spaces) where citation_author
        writes ``余語 覚文`` (one). Without this the same person appears twice
        in the author list.
        """
        return re.sub(r'\s+', ' ', name or '').strip()

    @classmethod
    def citation_authors_from_html(cls, html: str) -> List[str]:
        """Every author, from the ``citation_author`` tags.

        📌 This is the only complete list. Measured on
        10.2184/lsj.49.6_349 (three authors): both language pages carry the
        same three ``citation_author`` tags, so this is what the author list
        is built from; the per-language DOM only contributes alternative
        spellings.
        """
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        names = []
        for tag in soup.find_all('meta', attrs={'name': 'citation_author'}):
            name = cls._norm_name(tag.get('content') or '')
            if name and name not in names:
                names.append(name)
        return names

    @classmethod
    def authors_from_html(cls, html: str) -> List[str]:
        """Author names as *this page* spells them, in order.

        ❌ ``meta[name=authors]`` is deliberately not used, not even as a
        fallback: it holds **one arbitrary author**, not the list. Measured on
        10.2184/lsj.49.6_349, whose three authors are 余語 覚文 /
        GOLOVIN Daniil O. / Yanjun GU -- the ja page's meta says
        "余語 覚文" and the en page's says "GOLOVIN Daniil O.". Trusting it
        would silently drop two authors.
        """
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        names = []
        for anchor in soup.select('div.global-authors-name-tags a.customTooltip'):
            name = cls._norm_name(anchor.get_text(' ', strip=True))
            if not name or name.lower() in cls._NAME_PLACEHOLDERS:
                # ⚠️ The English page shows "[in Japanese]" for an author with
                # no Latin spelling. Keeping it would put a placeholder in the
                # author list.
                continue
            if name not in names:
                names.append(name)
        return names

    @classmethod
    def abstract_from_html(cls, html: str) -> str:
        """The 抄録 / Abstract text, without its heading."""
        if not html:
            return ''
        soup = BeautifulSoup(html, 'html.parser')
        wrap = soup.select_one('div#article-overiew-abstract-wrap')
        if wrap is None:
            return ''
        body = wrap.select_one('div.global-para-14')
        if body is None:
            # Drop the section heading ("抄録" / "Abstract") and keep the rest.
            heading = wrap.select_one('div.section-title-18')
            if heading is not None:
                heading.decompose()
            body = wrap
        text = body.get_text(' ', strip=True)
        return re.sub(r'\s+', ' ', text).strip()

    @classmethod
    def references_from_html(cls, html: str) -> List[str]:
        """The ``citation_reference`` meta contents, copied verbatim.

        ⚠️ No reformatting. J-STAGE writes them the way the journal printed
        them -- Japanese and English in one entry, the original numbering
        ("1）"), page ranges in the society's own style -- and every
        transformation we could apply would lose part of that.
        """
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        refs = []
        for tag in soup.find_all('meta', attrs={'name': 'citation_reference'}):
            text = (tag.get('content') or '').strip()
            if text:
                refs.append(text)
        return refs

    @classmethod
    def keywords_from_html(cls, html: str) -> List[str]:
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        words = []
        for tag in soup.find_all('meta', attrs={'name': 'citation_keywords'}):
            word = (tag.get('content') or '').strip()
            if word and word not in words:
                words.append(word)
        return words

    # ------------------------------------------------------------------
    # Contract
    # ------------------------------------------------------------------

    @classmethod
    def _combined_authors(cls, ja_html: str, en_html: str) -> List[str]:
        """Every author, plus the other language's spelling of each.

        Order: the complete ``citation_author`` list first, then any spelling
        the per-language DOM adds that is not already there. For a single
        Japanese author that yields ``["瀬戸 慧大", "Keita SETO"]`` -- both
        spellings, so a search over metadata.json hits either language; for
        10.2184/lsj.49.6_349 it yields the three authors once each, because
        the DOM spellings there are the same strings.
        """
        names = cls.citation_authors_from_html(ja_html or en_html)
        for html in (ja_html, en_html):
            for name in cls.authors_from_html(html):
                if name not in names:
                    names.append(name)
        return names

    async def extract_metadata(self, page) -> dict:
        html = await self.get_page_html(page)
        soup = BeautifulSoup(html, 'html.parser') if html else BeautifulSoup('', 'html.parser')
        return {
            'title': self.title_from_html(html),
            'authors': self._combined_authors(html, ''),
            'abstract': self.abstract_from_html(html),
            'doi': self._meta(soup, 'citation_doi') or self.doi,
            'journal': self._meta(soup, 'citation_journal_title'),
            'publisher': self._meta(soup, 'citation_publisher'),
            'year': self._meta(soup, 'citation_publication_date')[:4],
            'volume': self._meta(soup, 'citation_volume'),
            'issue': self._meta(soup, 'citation_issue'),
            'pages': self._meta(soup, 'citation_firstpage'),
            'references': self.references_from_html(html),
            '_keywords': self.keywords_from_html(html),
        }

    async def extract_references(self, html: str) -> list:
        return self.references_from_html(html)

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'JStageHandler')
        doi = self.doi
        set_actual_base_url(self, page)

        # ⚠️ Only take page.url when it is a real URL. "about:blank" is
        # truthy, so `page.url or landed` let it overwrite the landing URL the
        # workflow had pinned -- and then every URL built from it was wrong
        # ("about:blank/-char/en", no PDF link). That happens whenever the
        # flow runs off the capture rather than a live tab, which is a normal
        # outcome, not an error (see PageCapture.article_url).
        landed = getattr(self, '_landing_url', '') or ''
        try:
            live = page.url or ''
        except Exception:
            live = ''
        if live and not live.startswith(('about:', 'chrome://')):
            landed = live
        self._landing_url = landed
        if not landed:
            print("  ⚠️  没有落地 URL，无法确定语言版本与 PDF 链接")

        here = self.lang_of_url(landed) or 'ja'
        self._html_by_lang[here] = await self.get_page_html(page)

        # The other language, one navigation. Both pages are kept because each
        # carries a title and an author spelling the other does not.
        other = 'en' if here == 'ja' else 'ja'
        other_url = self.article_url_for(landed, other) if landed else ''
        if not other_url or 'jstage' not in other_url:
            # Nothing sane to navigate to; say so instead of fetching a
            # made-up URL and reporting "page not retrieved".
            print(f"  ⚠️  落地 URL 不可用（{landed or '(空)'}），跳过另一语言版本")
            other_url = ''
        else:
            print(f"  🌐 另取 {other} 版页面: {other_url}")
        other_html = await goto_and_capture_document(
            page, other_url, timeout_ms=60000,
            label=f"J-STAGE {other}") if other_url else ''
        if other_html:
            self._html_by_lang[other] = other_html
        else:
            print(f"  ⚠️  {other} 版页面未取到，元数据只会有 {here} 版的写法")

        for lang, html in self._html_by_lang.items():
            self._save_html(f'page_{lang}.html', html)

        ja_html = self._html_by_lang.get('ja', '')
        en_html = self._html_by_lang.get('en', '')
        # ⚠️ Japanese first, everywhere: it is the version of record for these
        # journals, and the folder name is built from it alone.
        primary = ja_html or en_html

        metadata = await self.extract_metadata(page) if not primary else {}
        if primary:
            soup = BeautifulSoup(primary, 'html.parser')
            ja_title = self.title_from_html(ja_html)
            en_title = self.title_from_html(en_html)
            titles = [t for t in (ja_title, en_title) if t]
            authors = self._combined_authors(ja_html, en_html)
            ja_abstract = self.abstract_from_html(ja_html)
            en_abstract = self.abstract_from_html(en_html)
            metadata = {
                # Both spellings in one string so a search over metadata.json
                # hits either language.
                'title': ' '.join(titles),
                '_dir_title': ja_title or en_title,
                '_title_ja': ja_title,
                '_title_en': en_title,
                'authors': authors,
                'abstract': ja_abstract or en_abstract,
                '_abstract_en': en_abstract if en_abstract != ja_abstract else '',
                'doi': self._meta(soup, 'citation_doi') or doi,
                'journal': self._meta(soup, 'citation_journal_title'),
                'publisher': self._meta(soup, 'citation_publisher'),
                'year': self._meta(soup, 'citation_publication_date')[:4],
                'volume': self._meta(soup, 'citation_volume'),
                'issue': self._meta(soup, 'citation_issue'),
                'pages': self._meta(soup, 'citation_firstpage'),
                'references': self.references_from_html(primary),
                '_keywords': self.keywords_from_html(primary),
            }

        print(f"  ✓ 标题(ja): {metadata.get('_title_ja', '')[:50]}")
        print(f"  ✓ 标题(en): {metadata.get('_title_en', '')[:50]}")
        print(f"  ✓ 作者: {metadata.get('authors')}")
        print(f"  ✓ 抄録: {len(metadata.get('abstract') or '')} 字符")
        print(f"  ✓ 引用文献: {len(metadata.get('references') or [])} 条")

        pdf_url = self.pdf_url_from_article_url(landed)
        if pdf_url:
            print(f"  ✓ PDF: {pdf_url}")
        else:
            print("  ⚠️  无法从落地 URL 构造 PDF 链接")

        return {
            'metadata': metadata,
            'links': {
                'pdf_url': pdf_url,
                'figure_urls': {},
                'supplemental_urls': [],
                'supplemental_descriptions': {},
            },
            # No publisher-hosted full text exists; the empty string is the
            # honest answer and keeps the workflow from writing a page.html.
            'fulltext_data': '',
            'journal_name': 'jstage',
        }

    def _save_html(self, name: str, html: str) -> None:
        if not (self.captured_data_dir and html):
            return
        try:
            from pathlib import Path
            out = Path(self.captured_data_dir) / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html, encoding='utf-8')
            print(f"  ✓ {name} 已保存 ({len(html):,} 字符)")
        except OSError as exc:
            print(f"  ⚠️  {name} 保存失败: {exc}")

    # ------------------------------------------------------------------
    # Markdown
    # ------------------------------------------------------------------

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        md: List[str] = []
        ja = metadata.get('_title_ja') or ''
        en = metadata.get('_title_en') or ''
        md.append(f"# {ja or en or metadata.get('title') or 'J-STAGE Article'}")
        md.append("")
        if ja and en:
            # The English title as its own line rather than glued to the
            # Japanese one: the combined form exists for searching
            # metadata.json, not for reading.
            md.extend([f"**English title:** {en}", ""])

        authors = metadata.get('authors') or []
        if authors:
            md.extend(['**Authors:** ' + ', '.join(authors), ''])

        md.extend(['## Publication', ''])
        for key, label in (('journal', 'Journal'), ('volume', 'Volume'),
                           ('issue', 'Issue'), ('pages', 'Pages'),
                           ('year', 'Year'), ('doi', 'DOI'),
                           ('publisher', 'Publisher')):
            value = (metadata.get(key) or '').strip()
            if value:
                md.extend([f"**{label}:** {value}", ''])

        abstract = (metadata.get('abstract') or '').strip()
        md.extend(['---', '', '## Abstract', '',
                   abstract or '[No abstract available.]', ''])
        en_abstract = (metadata.get('_abstract_en') or '').strip()
        if en_abstract:
            md.extend(['## Abstract (English page)', '', en_abstract, ''])

        if metadata.get('_keywords'):
            md.extend(['**Keywords:** ' + ', '.join(metadata['_keywords']), ''])

        # ⚠️ No "## Article Text" section at all. J-STAGE publishes no full
        # text, so an empty heading would read as a failed extraction rather
        # than as the publisher having nothing to give.
        references = metadata.get('references') or []
        if references:
            md.extend(['---', '', '## References', ''])
            for ref in references:
                md.extend([ref, ''])

        return '\n'.join(md)

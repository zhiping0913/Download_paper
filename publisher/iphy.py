"""iphy handler — Institute of Physics, Chinese Academy of Sciences.

Covers the journals at ``*.iphy.ac.cn`` (物理学报 / Acta Physica Sinica is the
sample, DOI prefix ``10.7498``). ``doi.org`` lands on
``https://wulixb.iphy.ac.cn/article/doi/{doi}``.

Where each thing comes from
---------------------------
The landing page's **own response** carries the bibliography twice, once per
language: ``div.info-cn`` and ``div.info-en`` each hold an
``h2.article-tit`` and an ``.article-author``, and the two abstracts are
``div.article-abstract.abstract-cn`` / ``.abstract-en``. Both languages are
kept -- the directory is named with the Chinese title, while
``metadata.json`` holds both so a search in either language finds the paper
(the same convention as J-STAGE and RCSI).

⚠️ **The body is NOT in the page.** It arrives as a separate XHR the page
posts during load::

    POST https://wulixb.iphy.ac.cn/data/article/articleFulltextData
    id=<articleId>&language=cn          (application/x-www-form-urlencoded)

So it is taken from the capture (``/article/articleFulltextData`` is in
``DEFAULT_API_HARVEST``) and only posted again when the capture came up
empty. ``articleId`` is a UUID that the page prints in its PDF button:
``previewPdf(this.href, '058edf31-…')``.

📌 The response is a gift compared with scraping HTML: ``data.secList`` is
the section tree, each section's ``paraContents`` holds paragraphs, display
formulas, figures and tables in ``sortNum`` order, and **every formula is
LaTeX** (``<tex-math>$…$</tex-math>``) -- no MathML, no images, nothing for
MathJax to eat. Not every article has one; an empty answer is an answer.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup, NavigableString

from core.utilities import (
    captured_api_entry,
    evaluate_with_timeout,
    http_asset_headers,
)
from html_to_md_converter import cleanup_markdown, convert_html_to_markdown
from publisher.base import PublisherHandler
from publisher.wildcard import (
    html_table_to_markdown,
    init_extract_all_page,
    set_actual_base_url,
)


class IPhyHandler(PublisherHandler):
    """Full-text handler for the IOP-CAS journal platform (iphy.ac.cn)."""

    PUBLISHER = 'iphy'

    SITE_BASE = 'https://wulixb.iphy.ac.cn'

    #: The body endpoint, relative to the site the article landed on.
    FULLTEXT_PATH = '/data/article/articleFulltextData'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.SITE_BASE
        self._fulltext: dict = {}

    # ------------------------------------------------------------------
    # URLs
    # ------------------------------------------------------------------

    def _site(self) -> str:
        """The host the article actually landed on, not a hardcoded one.

        iphy runs one platform for several journals (wulixb, cpb, …), so every
        derived URL is built from the landing URL rather than from
        :attr:`SITE_BASE`, which is only the fallback.
        """
        landed = getattr(self, '_landing_url', '') or ''
        match = re.match(r'(https?://[^/]+)', landed)
        return match.group(1) if match else self.SITE_BASE

    def supplemental_url(self) -> str:
        """``/article/doi/{doi}`` → ``/supplement/download/{doi}``."""
        landed = getattr(self, '_landing_url', '') or ''
        if '/article/doi/' in landed:
            return landed.split('?')[0].replace('/article/doi/',
                                                '/supplement/download/')
        doi = (self.doi or '').strip()
        return f"{self._site()}/supplement/download/{doi}" if doi else ''

    async def get_supplemental_url(self, doi: str) -> Optional[str]:
        return None

    async def get_fulltext_url(self, page) -> str:
        try:
            return page.url or ''
        except Exception:
            return getattr(self, '_landing_url', '') or ''

    @staticmethod
    def pdf_url_from_html(html: str) -> str:
        """The PDF link the page declares in ``citation_pdf_url``."""
        if not html:
            return ''
        soup = BeautifulSoup(html, 'html.parser')
        tag = soup.find('meta', attrs={'name': 'citation_pdf_url'})
        return (tag.get('content') or '').strip() if tag else ''

    async def get_pdf_url(self, doi: str) -> Optional[str]:
        return self.pdf_url_from_html(getattr(self, '_landing_html', '') or '')

    # ------------------------------------------------------------------
    # Metadata (both languages)
    # ------------------------------------------------------------------

    @staticmethod
    def _meta(soup: BeautifulSoup, name: str) -> str:
        tag = soup.find('meta', attrs={'name': name})
        return (tag.get('content') or '').strip() if tag else ''

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r'\s+', ' ', text or '').strip()

    @classmethod
    def _info_block(cls, soup: BeautifulSoup, language: str) -> Tuple[str, List[str]]:
        """``(title, authors)`` from ``div.info-cn`` / ``div.info-en``."""
        block = soup.find('div', class_=f'info-{language}')
        if block is None:
            return '', []
        heading = block.find(['h2', 'h1'], class_='article-tit')
        title = cls._clean(heading.get_text(' ', strip=True)) if heading else ''
        authors: List[str] = []
        author_el = block.find(class_='article-author')
        if author_el is not None:
            raw = cls._clean(author_el.get_text(' ', strip=True))
            # "张津瑞 , 栾其斌 , …" -- the separator is a comma, ASCII or full
            # width; a name never contains one.
            authors = [a for a in (part.strip() for part in re.split(r'[,，、]', raw)) if a]
        return title, authors

    @classmethod
    def _abstract(cls, soup: BeautifulSoup, language: str) -> str:
        """The abstract for one language, label stripped."""
        block = soup.select_one(f'div.article-abstract.abstract-{language}')
        if block is None:
            return ''
        text = cls._clean(block.get_text(' ', strip=True))
        # The block starts with its own label ("摘要:", "Abstract:").
        return re.sub(r'^(摘\s*要|Abstract)\s*[:：]?\s*', '', text, flags=re.I)

    @classmethod
    def _keywords(cls, soup: BeautifulSoup) -> Tuple[List[str], List[str]]:
        """``(chinese, english)`` keyword lists.

        ⚠️ The items are separated by "/" inside one element, so they cannot
        be read off child tags; and the element opens with its own label.
        """
        out: List[List[str]] = [[], []]
        for index, block in enumerate(soup.select('.article-keyword')[:2]):
            text = cls._clean(block.get_text(' ', strip=True))
            text = re.sub(r'^(关键词|Keywords)\s*[:：]?\s*', '', text, flags=re.I)
            out[index] = [k.strip() for k in text.split('/') if k.strip()]
        return out[0], out[1]

    #: The page hides the whole article record in a base64 JS variable.
    _META_BLOB_RE = re.compile(
        r"""article_meta_data\s*=\s*['"]([A-Za-z0-9+/=\s]+)['"]""")

    @classmethod
    def meta_blob(cls, html: str) -> dict:
        """The article record the page embeds as ``article_meta_data``.

        📌 This is the platform's own source of truth and it is **in the raw
        response**: ``allData.article = JSON.parse(Base64.decode(article_meta_data))``.
        It carries the article UUID, both titles, both abstracts, the keyword
        pairs, the authors in both languages, volume/issue/year and -- the
        reason it was hunted down -- the **supplements** with their ids.

        ⚠️ It is **base64**, which is why searching the HTML for a supplement
        id finds nothing. Measured while chasing CPL's
        ``exportSupplementary?id=302d912f-…``: the id is in no response body,
        no script, no XHR -- 106 harvested responses, zero hits -- because on
        the wire it only exists inside this blob.
        """
        match = cls._META_BLOB_RE.search(html or '')
        if not match:
            return {}
        try:
            raw = base64.b64decode(re.sub(r'\s+', '', match.group(1)))
            data = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
            print(f"  ⚠️  article_meta_data 解不开（{type(exc).__name__}: {exc}）")
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def article_id_from_html(html: str) -> str:
        """The platform's own UUID for the article.

        📌 It is not in a data attribute -- the only place the page prints it
        is the PDF button's onclick: ``previewPdf(this.href, '<uuid>')``.
        """
        match = re.search(r"previewPdf\(this\.href,\s*'([0-9a-fA-F-]{36})'", html or '')
        return match.group(1) if match else ''

    @classmethod
    def _blob_metadata(cls, blob: dict) -> dict:
        """Bibliography from the decoded ``article_meta_data``.

        Preferred over the DOM because it is structured: the two titles, the
        two abstracts (as HTML, formulas included), the keyword pairs and the
        author names in both languages are separate fields rather than text
        that has to be split on punctuation.
        """
        authors_cn, authors_en = [], []
        for author in blob.get('authors') or []:
            # ⚠️ Filter on authorRoleType, NOT authorType: CPL records every
            # author with authorType "org" while wulixb uses "author", so the
            # obvious-looking field drops **every author** on one of the two
            # journals. Measured: 0 authors for 10.1088/0256-307X/41/11/111201.
            role = (author.get('authorRoleType') or 'author').strip().lower()
            if role and role != 'author':
                continue
            name_cn = cls._clean(author.get('authorNameCn') or '')
            name_en = cls._clean(author.get('authorNameEn') or '')
            if name_cn and name_cn not in authors_cn:
                authors_cn.append(name_cn)
            if name_en and name_en not in authors_en:
                authors_en.append(name_en)

        keywords_cn, keywords_en = [], []
        for keyword in blob.get('keywords') or []:
            word_cn = cls._clean(keyword.get('keywordCn') or '')
            word_en = cls._clean(keyword.get('keywordEn') or '')
            if word_cn:
                keywords_cn.append(word_cn)
            if word_en:
                keywords_en.append(word_en)

        pages = '-'.join(str(p) for p in (blob.get('fpage'), blob.get('lpage')) if p)
        return {
            '_title_cn': cls._clean(blob.get('titleCn') or ''),
            '_title_en': cls._clean(blob.get('titleEn') or ''),
            '_authors_cn': authors_cn,
            '_authors_en': authors_en,
            # ⚠️ The abstracts are HTML with <inline-formula> inside, so they
            # go through the same pipeline as the body, not get_text().
            '_abstract_cn': cls._fragment_md(blob.get('abstractinfoCn') or ''),
            '_abstract_en': cls._fragment_md(blob.get('abstractinfoEn') or ''),
            '_keywords_cn': keywords_cn,
            '_keywords_en': keywords_en,
            'doi': (blob.get('doi') or '').strip(),
            'volume': str(blob.get('volume') or '').strip(),
            'issue': str(blob.get('issue') or '').strip(),
            'year': str(blob.get('year') or '').strip(),
            'pages': pages,
        }

    async def extract_metadata(self, page) -> dict:
        html = await self.get_page_html(page)
        self._landing_html = html
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')
        blob = self.meta_blob(html)
        self._meta_blob_cache = blob

        if blob:
            fields = self._blob_metadata(blob)
        else:
            # Fallback: scrape the rendered bibliography. Kept because the
            # blob is an implementation detail of the platform's templates,
            # and a page that stops shipping it should still produce metadata.
            print("  ⚠️  页面没有 article_meta_data，改从 DOM 读书目信息")
            title_cn, authors_cn = self._info_block(soup, 'cn')
            title_en, authors_en = self._info_block(soup, 'en')
            keywords_cn, keywords_en = self._keywords(soup)
            fields = {
                '_title_cn': title_cn, '_title_en': title_en,
                '_authors_cn': authors_cn, '_authors_en': authors_en,
                '_abstract_cn': self._abstract(soup, 'cn'),
                '_abstract_en': self._abstract(soup, 'en'),
                '_keywords_cn': keywords_cn, '_keywords_en': keywords_en,
                'doi': self._meta(soup, 'citation_doi') or (self.doi or ''),
                'volume': self._meta(soup, 'citation_volume'),
                'issue': self._meta(soup, 'citation_issue'),
                'year': '',
                'pages': '',
            }

        if not fields.get('year'):
            date = (self._meta(soup, 'citation_date')
                    or self._meta(soup, 'citation_publication_date'))
            match = re.search(r'\b(19|20|21)\d{2}\b', date)
            fields['year'] = match.group(0) if match else ''

        title_cn, title_en = fields['_title_cn'], fields['_title_en']
        authors_cn, authors_en = fields['_authors_cn'], fields['_authors_en']
        # Both languages in the searchable fields; the folder keeps the
        # Chinese title via _dir_title (organize_paper_output prefers it).
        # ⚠️ English-only journals on this platform (CPL) have an empty
        # titleCn, so the folder must fall back to the English one.
        metadata = dict(fields)
        metadata.update({
            'title': ' '.join(t for t in (title_cn, title_en) if t),
            '_dir_title': title_cn or title_en,
            'authors': authors_cn + [a for a in authors_en if a not in authors_cn],
            'abstract': fields['_abstract_cn'] or fields['_abstract_en'],
            'doi': fields.get('doi') or (self.doi or ''),
            'journal': self._meta(soup, 'citation_journal_title'),
            'publisher': self._meta(soup, 'citation_publisher'),
        })
        return metadata

    async def extract_references(self, html: str) -> list:
        # The reference list is loaded by yet another XHR
        # (loadRelativeArticles) and Crossref already has this article's
        # references, so nothing is scraped here.
        return []

    # ------------------------------------------------------------------
    # Body: the fulltext XHR
    # ------------------------------------------------------------------

    async def fetch_fulltext(self, page, captured: dict = None) -> dict:
        """The parsed fulltext payload, from the capture when possible.

        Order matters: the page has already posted this request during load,
        so asking again is a second request for bytes we hold. Only a miss
        falls through to posting it ourselves, and it says so.
        """
        # ⚠️ Through the base helper, which reads the capture the main flow
        # pinned on the handler (_captured_api). The `captured` argument only
        # carries what a particular call site chose to pass.
        body, url = self.captured_api_entry('/article/articleFulltextData')
        if not body and captured:
            body, url = captured_api_entry(captured, '/article/articleFulltextData')
        if body:
            print(f"  ✓ 复用捕获的正文响应（{len(body):,} 字符）")
        else:
            print("  ↪ 捕获里没有正文响应，改为主动 POST")
            body = await self._post_fulltext(page)
        if not body:
            return {}
        self._save_text('articleFulltextData.json', body)
        try:
            return json.loads(body)
        except ValueError as exc:
            print(f"  ⚠️  正文响应不是 JSON: {exc}")
            return {}

    async def _post_fulltext(self, page) -> str:
        """POST the fulltext endpoint from inside the page.

        ⚠️ In the page, not out of it: the endpoint answers to the article's
        own session and Referer, and the form encoding has to match what the
        site's own script sends (``id=<uuid>&language=cn``).
        """
        # The blob's own id first; the onclick regex is the fallback.
        article_id = ((getattr(self, '_meta_blob_cache', None) or {}).get('id') or '').strip() \
            or self.article_id_from_html(getattr(self, '_landing_html', '') or '')
        if not article_id:
            print("  ⚠️  页面里找不到 articleId，无法请求正文")
            return ''
        url = self._site() + self.FULLTEXT_PATH
        snippet = """async ([u, body]) => {
            const r = await fetch(u, {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
                body: body,
                credentials: 'include',
            });
            if (!r.ok) return '';
            return await r.text();
        }"""
        try:
            return await evaluate_with_timeout(
                page, snippet,
                [url, f"id={article_id}&language={self.page_type()}"]) or ''
        except Exception as exc:
            print(f"  ⚠️  正文 POST 失败: {type(exc).__name__}: {exc}")
            return ''

    # ------------------------------------------------------------------
    # Rendering the payload
    # ------------------------------------------------------------------

    @staticmethod
    def _sections(payload: dict) -> List[dict]:
        """``data.secList`` in the publisher's own order."""
        sections = ((payload or {}).get('data') or {}).get('secList') or []
        return sorted(sections, key=lambda s: (s.get('sortNum') or 0))

    @classmethod
    def _tex(cls, tex_el) -> str:
        """The LaTeX inside a ``<tex-math>``, delimiters normalised away."""
        text = (tex_el.get_text() or '').strip()
        text = re.sub(r'^\$+', '', text)
        text = re.sub(r'\$+$', '', text)
        return re.sub(r'\s+', ' ', text).strip()

    @classmethod
    def _fragment_md(cls, html_fragment: str) -> str:
        """Markdown for one paragraph/cell, keeping the author's LaTeX.

        Each ``<tex-math>`` becomes an opaque token before pandoc runs and is
        swapped back after -- pandoc would otherwise escape the backslashes.
        """
        if not html_fragment:
            return ''
        fragment = BeautifulSoup(f"<div>{html_fragment}</div>", 'html.parser')
        # pandoc keeps attributes it does not understand and prints them as a
        # brace suffix: the platform's cross-references carry ref-type, which
        # otherwise lands in the markdown as `{ref-type="table"}`.
        for el in fragment.find_all(attrs={'ref-type': True}):
            del el['ref-type']
        formulas: List[str] = []
        for tex_el in fragment.find_all('tex-math'):
            latex = cls._tex(tex_el)
            target = tex_el.parent if (tex_el.parent is not None
                                       and tex_el.parent.name == 'inline-formula') else tex_el
            if not latex:
                target.decompose()
                continue
            formulas.append(f"${latex}$")
            target.replace_with(f"DPTEX{len(formulas) - 1:04d}ZZ")
        # ⚠️ smart=False: the authors use an en dash as a minus sign, and
        # pandoc's smart punctuation writes it as "--" -- a number that looks
        # like it grew a second sign ("--5674.984").
        md = convert_html_to_markdown(fragment.div.decode_contents(), smart=False)
        md = cleanup_markdown(md)
        for index, latex in enumerate(formulas):
            md = md.replace(f"DPTEX{index:04d}ZZ", latex)
        return re.sub(r'[ \t]+', ' ', md).strip()

    @classmethod
    def _caption(cls, para: dict, label_prefix_cn: str, label_prefix_en: str) -> List[str]:
        """The float's two captions, Chinese then English.

        Both are kept: the English one routinely carries detail the Chinese
        one compresses away, and vice versa.
        """
        label = str(para.get('labelText') or '').strip()
        lines: List[str] = []
        # ⚠️ Through the formula pipeline, not as text: these captions carry
        # <inline-formula> and <i> markup of their own ("图 11 … 图中
        # $x_{input}$ 与 …"), and get_text would print the tex-math raw.
        title_cn = cls._fragment_md(para.get('contentTitleCn') or '')
        title_en = cls._fragment_md(para.get('contentTitleEn') or '')
        if title_cn:
            lines.append(f"**{label_prefix_cn} {label}**　{title_cn}".strip())
        if title_en:
            lines.append(f"**{label_prefix_en} {label}.**　{title_en}".strip())
        return lines

    @classmethod
    def _render_formula(cls, para: dict) -> List[str]:
        """A display formula, with the publisher's number as a ``\\tag``."""
        fragment = BeautifulSoup(para.get('paraContent') or '', 'html.parser')
        tex_el = fragment.find('tex-math')
        if tex_el is None:
            return []
        latex = cls._tex(tex_el)
        if not latex:
            return []
        label = str(para.get('labelText') or '').strip()
        if label:
            latex += f"\\tag{{{label}}}"
        return ['$$\n' + latex + '\n$$']

    @classmethod
    def _render_table(cls, para: dict) -> List[str]:
        """Caption, the table, then its note.

        ⚠️ The note lives in the table's own ``<tfoot>`` ("注: SE采用…"). Left
        in place it renders as one more data row spanning every column, which
        reads as data rather than as the footnote it is -- so it is pulled out
        and printed under the table.
        """
        fragment = BeautifulSoup(para.get('paraContent') or '', 'html.parser')
        table = fragment.find('table')
        if table is None:
            return []
        notes: List[str] = []
        for foot in table.find_all('tfoot'):
            text = cls._fragment_md(foot.decode_contents())
            text = re.sub(r'\s+', ' ', text).strip()
            if text:
                notes.append(text)
            foot.decompose()
        blocks = cls._caption(para, '表', 'Table')
        md = html_table_to_markdown(table, cls._fragment_md)
        if md:
            blocks.append(md)
        blocks.extend(notes)
        return blocks

    @classmethod
    def _render_figure(cls, para: dict, figures: Dict[str, dict], site: str) -> List[str]:
        """Caption plus a placeholder; registers the download URL.

        📌 The image is fetched by the platform's own export endpoint keyed on
        the paragraph's UUID -- ``paraImgSrc`` ("15-20260331-1.jpg") is a bare
        file name with no path, so it cannot be turned into a URL.
        """
        para_id = (para.get('id') or '').strip()
        label = str(para.get('labelText') or '').strip()
        blocks = cls._caption(para, '图', 'Fig.')
        if not para_id:
            return blocks
        key = f"fig_{label or len(figures) + 1}"
        figures[key] = {
            'url': f"{site}/article/exportImg?id={para_id}&type=para",
            'original_url': None,
        }
        blocks.append(f"[FIGURE_{label or len(figures)}]")
        return blocks

    @classmethod
    def render_body(cls, payload: dict, site: str) -> Tuple[str, Dict[str, dict]]:
        """``(body_md, figure_urls)`` for a fulltext payload."""
        figures: Dict[str, dict] = {}
        blocks: List[str] = []
        for section in cls._sections(payload):
            title = cls._clean((section.get('title') or '').replace('　', ' '))
            level = int(section.get('level') or 1)
            if title:
                blocks.append('#' * min(6, level + 1) + ' ' + title)
            paras = sorted(section.get('paraContents') or [],
                           key=lambda p: (p.get('sortNum') or 0))
            for para in paras:
                kind = (para.get('paraType') or '').strip()
                if kind == 'sec':
                    # The section heading, repeated inside the section's own
                    # paragraph list; it was printed from section['title'].
                    continue
                if kind == 'formula':
                    blocks.extend(cls._render_formula(para))
                elif kind == 'table':
                    blocks.extend(cls._render_table(para))
                elif kind == 'fig':
                    blocks.extend(cls._render_figure(para, figures, site))
                else:
                    md = cls._fragment_md(para.get('paraContent') or '')
                    if md:
                        blocks.append(md)
        return '\n\n'.join(b for b in blocks if b).strip(), figures

    # ------------------------------------------------------------------
    # Contract
    # ------------------------------------------------------------------

    async def get_figures(self, json_data: dict) -> dict:
        return {}

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

    def page_type(self) -> str:
        """``en`` or ``cn`` -- which language surface the article landed on.

        The platform serves both (``/en/article/id/…`` vs ``/article/doi/…``)
        and every derived request has to agree with the page, including the
        fulltext POST's ``language`` and the supplement download's
        ``pageType``.
        """
        landed = (getattr(self, '_landing_url', '') or '').lower()
        return 'en' if re.search(r'://[^/]+/en(/|$)', landed) else 'cn'

    def supplements_from_blob(self, blob: dict) -> Tuple[List[dict], Dict[str, str]]:
        """``(urls, {url: description})`` from ``article_meta_data.supplements``.

        📌 This is the method the page itself uses: every attachment is a
        record with its own UUID, and the download endpoint is
        ``/article/exportSupplementary?id=<that uuid>&pageType=<en|cn>``.
        ⚠️ The id is **not** the article's -- CPL's
        ``10.1088/0256-307X/41/11/111201`` is article ``6df20af1-…`` while its
        attachment is ``302d912f-…``.

        ⚠️ The description matters here more than usual: ``fileType`` says
        what the file actually is, and not every attachment is supplementary
        material in the scholarly sense -- ``firstFig`` is the cover
        thumbnail the site shows under "Other Related Supplements", while
        ``file`` is the real supplement (a 102 KB PDF on the wulixb sample).
        Nothing is dropped, but the markdown says which is which.
        """
        urls: List[dict] = []
        descriptions: Dict[str, str] = {}
        seen = set()
        site, page_type = self._site(), self.page_type()
        for item in blob.get('supplements') or []:
            item_id = (item.get('id') or '').strip()
            if not item_id:
                continue
            url = f"{site}/article/exportSupplementary?id={item_id}&pageType={page_type}"
            if url in seen:
                continue
            seen.add(url)
            name = (self._clean(item.get('nameEn') or '')
                    or self._clean(item.get('nameCn') or '')
                    or self._clean(item.get('fileName') or '')
                    or item_id)
            extension = (item.get('fileLastName') or '').strip().lstrip('.')
            # ⚠️ Name the file ourselves: the URL's basename is the endpoint
            # ("exportSupplementary"), so every attachment of every article
            # would land under the same name with no extension.
            filename = f"{name}.{extension}" if extension else name
            urls.append({'url': url, 'filename': filename})
            parts = [p for p in (name, (item.get('fileType') or '').strip(),
                                 (item.get('fileSize') or '').strip()) if p]
            descriptions[url] = ' — '.join(parts) if parts else item_id
        return urls, descriptions

    def _supplemental_by_doi(self, url: str) -> List[str]:
        """The DOI-shaped endpoint, used only when the blob is missing.

        ⚠️ iphy answers it with **200 and an empty body** for an article that
        has none (measured on four other 10.7498 DOIs: ``Content-Length: 0``
        and no ``Content-Type``), so the URL alone proves nothing, and a
        wrong guess costs the whole download ladder -- five attempts with a
        90 s throttle between them. Hence the HEAD.

        A HEAD, not a GET: the point is to learn whether a file is there
        without pulling it twice. This is a file endpoint, which is the one
        place the project fetches over plain HTTP by design.
        """
        if not url:
            return []
        try:
            resp = requests.head(
                url, timeout=(15, 20), allow_redirects=True,
                headers=http_asset_headers(getattr(self, '_landing_url', '') or None))
            declared = int(resp.headers.get('Content-Length') or 0)
        except (requests.RequestException, ValueError, OSError) as exc:
            # Unknown is not "absent": let the downloader try.
            print(f"  ⚠️  补充材料探测失败（{type(exc).__name__}），仍按有处理")
            return [url]
        if declared > 0:
            print(f"  ✓ 补充材料: 1 个（{declared:,} 字节，按 DOI 端点）")
            return [url]
        print("  ✓ 补充材料: 无（DOI 端点返回空）")
        return []

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'IPhyHandler')
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

        payload = await self.fetch_fulltext(page, captured)
        body_md, figure_urls = self.render_body(payload, self._site())
        metadata['_body_md'] = body_md

        print(f"  ✓ 标题(中): {metadata.get('_title_cn', '')[:44]}")
        print(f"  ✓ 标题(英): {metadata.get('_title_en', '')[:44]}")
        print(f"  ✓ 作者: {len(metadata.get('authors') or [])} 位"
              f"（中 {len(metadata.get('_authors_cn') or [])} /"
              f" 英 {len(metadata.get('_authors_en') or [])}）")
        print(f"  ✓ 摘要: 中 {len(metadata.get('_abstract_cn') or '')} /"
              f" 英 {len(metadata.get('_abstract_en') or '')} 字符")
        print(f"  ✓ 正文: {len(body_md):,} 字符")
        print(f"  ✓ 图片: {len(figure_urls)} 个")
        if not body_md:
            print("  ⚠️  这篇没有正文数据 —— md 只会有摘要")

        blob = getattr(self, '_meta_blob_cache', None) or {}
        if blob:
            supplemental_urls, supplemental_descriptions = self.supplements_from_blob(blob)
            print(f"  ✓ 补充材料: {len(supplemental_urls)} 个"
                  + (f"（{'; '.join(supplemental_descriptions.values())[:90]}）"
                     if supplemental_urls else "（article_meta_data 里没有）"))
        else:
            supplemental_urls = self._supplemental_by_doi(self.supplemental_url())
            supplemental_descriptions = {}

        pdf_url = await self.get_pdf_url(doi)
        print(f"  ✓ PDF: {pdf_url or '(未取到)'}")

        return {
            'metadata': metadata,
            'links': {
                'pdf_url': pdf_url,
                'figure_urls': figure_urls,
                'supplemental_urls': supplemental_urls,
                'supplemental_descriptions': supplemental_descriptions,
            },
            # The body is JSON, not the page; landing it as page.html would
            # only duplicate page_raw.html.
            'fulltext_data': '',
            'journal_name': 'iphy',
        }

    # ------------------------------------------------------------------
    # Markdown
    # ------------------------------------------------------------------

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        figure_filenames = kwargs.get('figure_filenames') or {}
        figure_urls = kwargs.get('figure_urls') or {}
        supplemental_urls = kwargs.get('supplemental_urls') or []
        supplemental_downloads = kwargs.get('supplemental_downloads') or []

        title_cn = metadata.get('_title_cn') or ''
        title_en = metadata.get('_title_en') or ''
        md: List[str] = [f"# {title_cn or title_en or 'iphy Article'}", '']
        if title_cn and title_en:
            md.extend([f"**English title:** {title_en}", ''])

        if metadata.get('_authors_cn'):
            md.extend(['**作者:** ' + ', '.join(metadata['_authors_cn']), ''])
        if metadata.get('_authors_en'):
            md.extend(['**Authors:** ' + ', '.join(metadata['_authors_en']), ''])

        md.extend(['## Publication', ''])
        for key, label in (('journal', 'Journal'), ('volume', 'Volume'),
                           ('issue', 'Issue'), ('year', 'Year'), ('doi', 'DOI')):
            value = (metadata.get(key) or '').strip()
            if value:
                md.extend([f"**{label}:** {value}", ''])

        abstract_cn = (metadata.get('_abstract_cn') or '').strip()
        abstract_en = (metadata.get('_abstract_en') or '').strip()
        md.extend(['---', '', '## 摘要', '', abstract_cn or '[无摘要]', ''])
        if abstract_en:
            md.extend(['## Abstract', '', abstract_en, ''])
        if metadata.get('_keywords_cn'):
            md.extend(['**关键词:** ' + ' / '.join(metadata['_keywords_cn']), ''])
        if metadata.get('_keywords_en'):
            md.extend(['**Keywords:** ' + ' / '.join(metadata['_keywords_en']), ''])

        body_md = (metadata.get('_body_md') or '').strip()
        if not body_md and isinstance(article_text, str) and not article_text.lstrip().startswith('<'):
            body_md = article_text.strip()
        if body_md:
            md.extend(['---', '',
                       self._resolve_figures(body_md, figure_filenames, figure_urls), ''])
        else:
            md.extend(['---', '', '*该文章没有提供正文数据，以上只有摘要。*', ''])

        if supplemental_urls or supplemental_downloads:
            md.extend(['---', '', '## 补充材料', ''])
            descriptions = kwargs.get('supplemental_descriptions') or {}
            for index, entry in enumerate(supplemental_urls):
                url = entry.get('url', '') if isinstance(entry, dict) else entry
                label = descriptions.get(url) or (
                    entry.get('filename') if isinstance(entry, dict) else '') or url
                local = (supplemental_downloads[index]
                         if index < len(supplemental_downloads) else '')
                md.append(f"- [{label}](<{local or url}>)")
            md.append('')

        return '\n'.join(md).rstrip() + '\n'

    @staticmethod
    def _resolve_figures(body_md: str, figure_filenames: dict,
                         figure_urls: dict) -> str:
        """Turn ``[FIGURE_N]`` into an image reference.

        A placeholder whose file did not download falls back to the remote
        URL rather than vanishing: a missing figure should be visible in the
        markdown, not silently absent.
        """
        def replace(match):
            number = match.group(1)
            filename = (figure_filenames.get(number)
                        or figure_filenames.get(int(number))
                        if figure_filenames else None)
            if filename:
                return f"![图 {number}]({filename})"
            info = figure_urls.get(f'fig_{number}')
            url = info.get('url') if isinstance(info, dict) else info
            return f"![图 {number}]({url})" if url else f"*[图 {number} 未下载]*"

        return re.sub(r'\[FIGURE_(\d+)\]', replace, body_md)

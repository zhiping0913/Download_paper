"""EDP Sciences handler (10.1051 -- aanda.org, epjconf, and the rest).

EDP runs **one in-house platform under many journal domains** (A&A on
aanda.org, EPJ on epj-conferences.org, …), so this handler is routed by the
``10.1051`` prefix *and* by the domains, and everything it builds is derived
from the page's own ``citation_fulltext_html_url`` rather than from a
hard-coded host.

📌 **Body, display formulas and the authors' LaTeX are all in the raw server
response** -- and the LaTeX is the good kind: every formula carries

    <span class="img-equation … ressouce-equation-block"
          data-latex="$$ \\begin{aligned} \\nabla \\cdot \\mathbf u &= 0, \\end{aligned} $$">

so the source is read off an attribute instead of being reconstructed from
the MathML (which is there too, next to a GIF rendering of the same thing).

⚠️ **The body is flat: a run of ``h2.sec`` / ``h3.sec2`` / ``h4.sec3`` plus
``<p>`` and floats as siblings inside ``div#contenu``**, with no section
nesting at all -- so it is walked by heading, like World Scientific's old
Atypon skin. Section depth comes from the tag name, never from the numbering
in the text.

❌ **The article page does not contain its own tables or its full-size
figures.** This is the shape that makes EDP different from every other
publisher in the tree:

    div.inset#T1    the table's *caption only*, plus a link to T1.html
    div.inset#F1    a `*_small.jpg` thumbnail, plus a link to F1.html

The real table markup lives on ``T<N>.html`` and the original image is
advertised on ``F<N>.html`` (``a.btn2.original`` → a ``.tif``). Both pages
are fetched, **landed in ``html/``, and parsed from there**; "Movies" are the
same story on a third page (``…/olm/…``). A handler that only read the
article page would publish a paper whose tables are an empty caption and
whose figures are thumbnails.

⚠️ **Must be headed, and the real Chrome profile has to have visited the
site once.** EDP sits behind **DataDome**, not Cloudflare: a blank profile is
answered with a 403 carrying a ``geo.captcha-delivery.com`` interstitial, and
this repo's Turnstile clicking has nothing to click on it (measured: a fully
empty-profile Chrome sat on ``body=0`` for 60s across 5 separate URLs). With
the seeded profile's ``datadome`` cookie the very same code gets the article
in **2 seconds**. So the cookie is the whole mechanism -- which is also why
nothing here tries to solve the interstitial.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString

from core.utilities import html_looks_like_bot_challenge
from html_to_md_converter import (
    cleanup_markdown,
    convert_html_to_markdown,
    mathml_to_latex_pandoc,
)
from publisher.base import PublisherHandler
from publisher.wildcard import (
    generate_reference_text_from_crossref,
    goto_and_capture_document,
    parse_article_html,
    html_table_to_markdown,
    init_extract_all_page,
    remove_newlines_in_paragraph,
    set_actual_base_url,
)


class EDPHandler(PublisherHandler):
    """Full-text handler for EDP Sciences journals."""

    PUBLISHER = 'edp'

    #: Only used when a page gives us nothing absolute to join against.
    FALLBACK_BASE = 'https://www.aanda.org'

    #: ``h2.sec`` ids that end the body. ``references`` is rendered on its own;
    #: ⚠️ "All Tables" / "All Figures" are *listings of what the article
    #: already showed* -- walking into them prints every caption a second
    #: time (4 tables + 24 figures in 10.1051/0004-6361/202243956).
    BODY_STOP_IDS = ('references', 'tables', 'figures')

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.FALLBACK_BASE
        #: ``{'F1': html}`` -- the side pages, as landed in ``html/``.
        self._side_pages: Dict[str, str] = {}
        self._fulltext_url = ''

    # ==================================================================
    # URLs
    # ==================================================================

    @staticmethod
    def _fulltext_url_from_html(html: str) -> str:
        soup = BeautifulSoup(html or '', 'html.parser')
        meta = soup.find('meta', attrs={'name': 'citation_fulltext_html_url'})
        return (meta.get('content') or '').strip() if meta else ''

    def _side_page_url(self, name: str) -> str:
        """``F1`` / ``T1`` → the article directory's ``F1.html``.

        Anchored on ``citation_fulltext_html_url`` because the article's
        directory encodes volume, issue and the publisher's own article id
        (``/articles/aa/full_html/2025/12/aa46508-23/``) -- none of which can
        be derived from the DOI.
        """
        base = self._fulltext_url or self.actual_base_url
        return urljoin(base, f"{name}.html")

    def _olm_url(self, html: str) -> str:
        """The "Movies" page, preferring the link the article itself prints.

        The article's own "Article contents" block carries it
        (``a[href*="/olm/"]``); the constructed form is the same path with
        ``full_html`` swapped for ``olm``, and is only the fallback.
        """
        soup = BeautifulSoup(html or '', 'html.parser')
        for anchor in soup.find_all('a', href=True):
            if '/olm/' in anchor['href']:
                return urljoin(self._fulltext_url or self.actual_base_url,
                               anchor['href'])
        if self._fulltext_url and '/full_html/' in self._fulltext_url:
            return self._fulltext_url.replace('/full_html/', '/olm/')
        return ''

    async def get_pdf_url(self, doi: str = None) -> Optional[str]:
        """``citation_pdf_url``, which EDP declares on every article page."""
        html = await self.get_page_html()
        soup = BeautifulSoup(html or '', 'html.parser')
        meta = soup.find('meta', attrs={'name': 'citation_pdf_url'})
        url = (meta.get('content') or '').strip() if meta else ''
        return urljoin(self._fulltext_url or self.actual_base_url, url) if url else None

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def get_fulltext_url(self, page) -> str:
        try:
            return (page.url or '') if page is not None else self._fulltext_url
        except Exception:
            return self._fulltext_url

    async def get_supplemental_url(self, doi: str = None) -> Optional[str]:
        """The "Movies" page, if the article has one.

        📌 EDP's only supplemental shape seen so far is the ``/olm/`` page;
        it is reached through ``extract_all`` (which needs its markup, not
        just its URL), so this is the contract's answer and nothing more.
        """
        return self._olm_url(await self.get_page_html()) or None

    def _warn_if_challenged(self, html: str) -> None:
        """Say which domain needs a visit, when DataDome answered instead.

        ⚠️ **EDP's DataDome cookie is per domain, and EDP is one journal per
        domain.** Seeding the real Chrome profile with a visit to
        epj-conferences.org does **not** get e3s-conferences.org in -- user
        measured. The seeded cookie is the whole mechanism here (nothing in
        this repo can solve the interstitial: it has no clickable widget), so
        when it is missing the only useful output is *which* host to open
        once by hand. Otherwise the run looks like an extraction bug.
        """
        if not html or not html_looks_like_bot_challenge(html):
            return
        host = urlsplit(self.actual_base_url or self.FALLBACK_BASE).netloc
        print(f"  ⚠️  这一页是 DataDome 的拦截页，不是文章。"
              f"EDP 的 cookie **按域名隔离**，为别的 EDP 刊播种不算 —— "
              f"请在真实 Chrome 里打开一次 https://{host}/ 再重跑")

    # ------------------------------------------------------------------
    # The abstract page → the full-text page
    # ------------------------------------------------------------------

    @staticmethod
    def looks_like_fulltext(html: str) -> bool:
        """Is this the full-text rendition, or the abstract landing page?

        ⚠️ ``doi.org/{doi}`` lands on **``/abs/``**, not on ``/full_html/``
        (measured on 10.1051/epjap/2023220290). The two are easy to tell
        apart: the abstract page has **no ``div#contenu``** at all -- no
        sections, no floats, zero ``h2.sec`` -- so the predicate is the body
        container itself rather than a URL pattern (EDP has one domain per
        journal, and the paths differ between them).
        """
        return parse_article_html(html or '').find(id='contenu') is not None

    @staticmethod
    def fulltext_page_url(html: str) -> str:
        """Where the full text is, as the abstract page itself declares it.

        Two sources, both on the abstract page: ``citation_fulltext_html_url``
        and the "Full HTML" entry of the "Article contents" block
        (``ul.article_doc``). The meta is preferred because it is absolute.

        📌 **Not every EDP article has a full-text rendition** --
        10.1051/epjconf/202634803001 (an EPJ Web of Conferences paper) is PDF
        only. Then there is nothing to find here, and '' means "stay on the
        page we have": the abstract, the metadata and the PDF are still the
        answer.
        """
        soup = parse_article_html(html or '')
        meta = soup.find('meta', attrs={'name': 'citation_fulltext_html_url'})
        declared = (meta.get('content') or '').strip() if meta else ''
        if declared:
            return declared
        for anchor_el in soup.select('ul.article_doc a[href], div.article_doc a[href]'):
            if '/full_html/' in anchor_el['href']:
                return anchor_el['href'].strip()
        return ''

    async def _promote_to_fulltext(self, page, html: str) -> str:
        """Follow the abstract page to the full text, or keep what we have.

        The fetched document **replaces** ``_raw_server_html`` so the whole
        contract (metadata, PDF link, figures, body) reads the same bytes --
        the way Cambridge's re-fetch does. It is landed as
        ``full_html.html``; the main flow's ``page_raw.html`` keeps the
        abstract page it actually navigated to, so the directory shows both
        hops.
        """
        if self.looks_like_fulltext(html):
            return html
        target = self.fulltext_page_url(html)
        if not target:
            print("  ℹ️  这篇没有 full html 版本（EDP 不是每篇都有），"
                  "正文留空，摘要与 PDF 照常")
            return html
        target = urljoin(self._fulltext_url or self.actual_base_url, target)
        print(f"  ↪ 落地页是摘要页，跳到 full html: {target}")
        if page is None:
            return self._read_landed('full_html') or html
        fetched = await goto_and_capture_document(
            page, target, label='EDP full html 页')
        if not fetched:
            print("  ⚠️  full html 页未捕获到原始响应 —— 用摘要页继续"
                  "（宁可只有摘要，也不要一份公式已被替换的 DOM）")
            return html
        self._land_side_page('full_html', fetched)
        self._raw_server_html = fetched
        return fetched

    # ==================================================================
    # Side pages (figures, tables, movies)
    # ==================================================================

    async def _load_side_page(self, page, name: str, url: str = '') -> str:
        """Fetch ``<name>.html``, land it in ``html/``, and return it.

        ⚠️ The raw document, never ``page.content()``: these pages carry
        formulas in table cells and captions, and MathJax rewrites them in the
        rendered DOM exactly as it does in the article.

        Without a page (offline rendering, tests) it reads the copy already in
        ``html/`` -- which is the same file the online path just wrote, so
        both routes parse identical bytes.
        """
        if name in self._side_pages:
            return self._side_pages[name]

        html = ''
        if page is not None:
            html = await goto_and_capture_document(
                page, url or self._side_page_url(name), label=f"EDP {name} 页")
        if not html:
            html = self._read_landed(name)
            if html:
                print(f"    ↪ {name}.html 取自已落盘的副本")
        if not html:
            print(f"  ⚠️  {name} 页始终未捕获到原始响应，跳过"
                  f"（宁可缺这一项，也不要一份公式已被替换的 DOM）")
            self._side_pages[name] = ''
            return ''
        self._side_pages[name] = html
        self._land_side_page(name, html)
        return html

    def _read_landed(self, name: str) -> str:
        if not self.captured_data_dir:
            return ''
        path = Path(self.captured_data_dir) / f"{name}.html"
        try:
            return path.read_text(encoding='utf-8') if path.is_file() else ''
        except Exception:
            return ''

    def _land_side_page(self, name: str, html: str) -> None:
        """Every page this run opened belongs in the output directory.

        The markdown is built from these files, so leaving them out would
        make the paper directory unable to explain its own tables.
        """
        if not self.captured_data_dir:
            return
        safe = re.sub(r'[^A-Za-z0-9_.-]', '_', name)[:40]
        try:
            out = Path(self.captured_data_dir) / f"{safe}.html"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html, encoding='utf-8')
            print(f"    ✓ {out.name} 已保存 ({out.stat().st_size:,} bytes)")
        except Exception as exc:
            print(f"    ⚠️  {safe}.html 保存失败: {str(exc)[:60]}")

    def seed_side_pages(self, pages: Dict[str, str]) -> None:
        """Offline entry point: hand in ``{'F1': html}`` instead of fetching."""
        self._side_pages.update(pages)

    # ==================================================================
    # Metadata
    # ==================================================================

    @classmethod
    def extract_metadata_from_html(cls, html: str) -> dict:
        """Everything from ``citation_*`` -- EDP declares them in full."""
        soup = BeautifulSoup(html or '', 'html.parser')

        def one(name: str) -> str:
            el = soup.find('meta', attrs={'name': name})
            return (el.get('content') or '').strip() if el else ''

        def many(name: str) -> List[str]:
            return [(el.get('content') or '').strip()
                    for el in soup.find_all('meta', attrs={'name': name})
                    if (el.get('content') or '').strip()]

        date = one('citation_publication_date') or one('citation_online_date')
        metadata = {
            'title': one('citation_title'),
            'authors': many('citation_author'),
            'journal': one('citation_journal_title'),
            'publisher': one('citation_publisher') or 'EDP Sciences',
            'volume': one('citation_volume'),
            'issue': one('citation_issue'),
            'pages': cls._pages(one('citation_firstpage'), one('citation_lastpage')),
            'year': (date.split('/')[0] if date else ''),
            'doi': one('citation_doi'),
            'abstract': cls.extract_abstract_from_html(html) or one('citation_abstract'),
            '_keywords': many('citation_keyword'),
            '_article_type': one('citation_article_type'),
        }
        return metadata

    @staticmethod
    def _pages(first: str, last: str) -> str:
        if first and last and first != last:
            return f"{first}-{last}"
        return first or last or ''

    @classmethod
    def extract_abstract_from_html(cls, html: str) -> str:
        """The rendered abstract, not ``citation_abstract``.

        ⚠️ The meta is **plain text**, and these abstracts are not: A&A's
        structured form marks its parts with ``<i>Methods.</i>`` and the prose
        is full of ``<sub>``/``<sup>`` ("10<sup>6</sup>"). Reading the meta
        would flatten "10⁶" to "106". So the section is rendered through the
        same pipeline as the body, and the meta is only the fallback -- the
        order MDPI got wrong.
        """
        soup = parse_article_html(html or '')
        head = soup.find(id='head')
        if head is None:
            return ''
        marker = head.find(id='abs')
        start = marker.find_parent('p') if marker else None
        if start is None:
            return ''
        parts: List[str] = []
        for sib in start.find_next_siblings():
            # The keyword box closes the abstract.
            if sib.name == 'div' and 'kword' in (sib.get('class') or []):
                break
            if sib.name != 'p':
                continue
            md = cls._inline_md(sib)
            if md:
                parts.append(md)
        return '\n\n'.join(parts).strip()

    async def extract_metadata(self, page=None) -> dict:
        return self.extract_metadata_from_html(await self.get_page_html(page))

    # ==================================================================
    # Inline conversion
    # ==================================================================

    @classmethod
    def _inline_md(cls, element) -> str:
        """Markdown for one inline run, with the authors' LaTeX carried through.

        Each inline formula becomes an opaque ``DPMATH####ZZ`` token before
        pandoc runs and is swapped back afterwards -- pandoc would otherwise
        escape the backslashes in ``data-latex``.
        """
        fragment = BeautifulSoup(f'<div>{element}</div>', 'html.parser')
        formulas: List[str] = []

        for span in fragment.select('span.ressouce-equation-inline'):
            latex = cls._latex_from_span(span)
            if not latex:
                span.decompose()
                continue
            formulas.append(f"${latex}$")
            span.replace_with(NavigableString(f"DPMATH{len(formulas) - 1:04d}ZZ"))

        cls._clean_inline_markup(fragment)

        md = cls._paragraph_md('<p>' + fragment.div.decode_contents() + '</p>')
        for index, tex in enumerate(formulas):
            md = md.replace(f"DPMATH{index:04d}ZZ", tex)
        return md.strip()

    @classmethod
    def _clean_inline_markup(cls, fragment) -> None:
        """Remove what pandoc would turn into literal noise."""
        # The page's spambot guard: two <script>s writing a mailto, with this
        # sentence between them as the no-JS fallback. get_text() sees both --
        # innerText never did -- so they would land in the markdown.
        for junk in fragment.find_all(['script', 'style']):
            junk.decompose()
        # The COinS citation span is metadata for reference managers.
        for span in fragment.select('span.Z3988'):
            span.decompose()

        for anchor in list(fragment.find_all('a')):
            href = (anchor.get('href') or '').strip()
            text = anchor.get_text(' ', strip=True)
            # ⚠️ An empty named anchor ("<a id="InR116"></a>", one before every
            # citation) renders as a literal "[]{#InR116}".
            if not text and not href:
                anchor.decompose()
                continue
            if cls._is_internal_link(href):
                # A cross-reference inside the article: the href is the *full*
                # article URL plus a fragment, so it cannot be collapsed by
                # cleanup_markdown's "[N](#…) → [N]" rule and would publish a
                # 90-character link on every citation. The text is what the
                # sentence reads.
                if anchor.find_parent('sup') is not None or '#FN' in href:
                    # A footnote marker. Square brackets because in plain
                    # text a superscript digit sticks to the previous word
                    # -- and the <sup> goes with it, or pandoc wraps the
                    # result again as "^[1]^".
                    sup = anchor.parent
                    target = (sup if (sup is not None and sup.name == 'sup'
                                      and len(sup.find_all(recursive=False)) == 1
                                      and not sup.get_text(strip=True).replace(text, '').strip())
                              else anchor)
                    target.replace_with(NavigableString(f"[{text}]"))
                else:
                    anchor.replace_with(NavigableString(text))
                continue
            for attr in ('target', 'id', 'name', 'class'):
                if attr in anchor.attrs:
                    del anchor[attr]

        # Presentational / markup-trace attributes pandoc would echo as
        # "{data-url=…}" suffixes.
        for el in fragment.find_all(True):
            for attr in ('data-url', 'data-latex', 'valign', 'frame', 'rules'):
                if attr in el.attrs:
                    del el[attr]
            if el.name == 'span' and el.get('id'):
                del el['id']

    @staticmethod
    def _is_internal_link(href: str) -> bool:
        """True for a link into this same article page."""
        if not href:
            return False
        if href.startswith('#'):
            return True
        return '#' in href and '/full_html/' in href

    @classmethod
    def _paragraph_md(cls, html_fragment: str) -> str:
        if not html_fragment:
            return ''
        md = convert_html_to_markdown(html_fragment)
        md = cleanup_markdown(md)
        md = remove_newlines_in_paragraph(md, '', 'p')
        return re.sub(r'[ \t]+', ' ', md).strip()

    # ==================================================================
    # Formulas
    # ==================================================================

    @classmethod
    def _latex_from_span(cls, span) -> str:
        """One formula's LaTeX, minus its delimiters: attribute, else MathML.

        📌 EDP ships up to three renderings of a formula -- a ``data-latex``
        attribute, a MathML tree, and a GIF/PNG -- and the attribute is the
        only one that is the **author's own source**, so it comes first.

        ❌ **But it is not always there.** 10.1051/epjap/2023220290 carries
        **zero** ``data-latex`` attributes: all 30 of its formulas (5 display,
        25 inline) are MathML only. Reported by the user as "没有 display
        formula" -- and that is exactly what happened, the renderer returned
        '' for every one of them and the body simply had no equations, with
        nothing in the log. Those get converted through the repo's shared
        ``mathml_to_latex_pandoc``, the same route PNAS and Optica take.

        Both sources come back **with** delimiters, which are stripped here
        because the caller decides whether this is inline or display.
        """
        latex = (span.get('data-latex') or '').strip()
        if not latex:
            math_el = span.find('math')
            if math_el is not None:
                latex = (mathml_to_latex_pandoc(str(math_el)) or '').strip()
        if not latex:
            return ''
        for opener, closer in (('$$', '$$'), ('\\[', '\\]'), ('$', '$')):
            if latex.startswith(opener) and latex.endswith(closer) \
                    and len(latex) > len(opener) + len(closer):
                latex = latex[len(opener):-len(closer)]
                break
        return latex.strip()

    def _render_display_formula(self, span) -> str:
        """``$$ … \\tag{N} $$`` for one ``span.ressouce-equation-block``.

        An instance method, not a classmethod: the image last resort below
        has to resolve a relative src against *this* article's directory.
        """
        latex = self._latex_from_span(span)
        if not latex:
            # Last resort: EDP's own picture of the formula, referenced where
            # it lives. A visible equation image beats a silently missing
            # equation, and the symbol-sized images are not worth downloading
            # (the same call SPIE and PNAS make).
            img = span.select_one('span.img img[src], img[src]')
            if img is not None:
                return f"![]({self._absolute(img['src'])})"
            return ''
        label = ''
        holder = span.find('span', class_='label-eq')
        if holder is None and span.parent is not None:
            holder = span.parent.find('span', class_='label-eq')
        if holder is not None:
            label = holder.get_text(strip=True).strip('()').strip()
        if label:
            latex = self._with_tag(latex, label)
        return f"$$\n{latex}\n$$"

    #: Sub-environments that do **not** number their own rows. ``\tag`` has
    #: to go *outside* these (but still inside the ``$$``); inside one it is
    #: not valid amsmath and MathJax may drop the number.
    INNER_MATH_ENVS = ('aligned', 'split', 'gathered', 'array', 'cases',
                       'matrix', 'bmatrix', 'pmatrix', 'vmatrix')

    @classmethod
    def _with_tag(cls, latex: str, label: str) -> str:
        r"""Attach ``\tag{label}`` where the environment allows it.

        ⚠️ Two different rules, and using one for both loses the number:

        * ``aligned`` / ``split`` (EDP's own wrapper -- 33 of 33 display
          formulas in 10.1051/0004-6361/202243956) is a *sub*-environment, so
          the tag belongs after its ``\end``, still inside the ``$$``.
        * ``align`` / ``gather`` / ``multline`` number their own rows, so the
          tag has to go **before** the ``\end`` (the rule ACM already needed).
        """
        tag = f"\\tag{{{label}}}"
        match = re.search(r'\\end\{([A-Za-z]+)\*?\}\s*$', latex)
        if not match:
            return f"{latex} {tag}"
        if match.group(1) in cls.INNER_MATH_ENVS:
            return f"{latex.rstrip()} {tag}"
        return latex[:match.start()] + tag + '\n' + latex[match.start():]

    # ==================================================================
    # Floats
    # ==================================================================

    @classmethod
    def _float_label_and_caption(cls, inset) -> Tuple[str, str]:
        """``('Fig. 1.', caption md)`` for a ``div.inset`` on the article page.

        The two shapes differ only in packaging: a figure is a one-row
        ``<table>`` (thumbnail | caption), a table is a ``div.ligne``.
        """
        label_el = inset.find('span', class_='bold')
        label = label_el.get_text(' ', strip=True) if label_el else ''
        holder = inset.find('td', class_='img-txt') or inset.find('div', class_='ligne') or inset
        caption = ' '.join(cls._inline_md(p) for p in holder.find_all('p'))
        return label, caption.strip()

    def _render_figure(self, inset) -> List[str]:
        """``[FIGURE_n]`` plus the label and caption."""
        number = self._float_number(inset.get('id'))
        label, caption = self._float_label_and_caption(inset)
        head = f"**{label}**" if label else ''
        text = f"{head} {caption}".strip()
        return [f"[FIGURE_{number}]", text] if text else [f"[FIGURE_{number}]"]

    def _render_table(self, inset) -> List[str]:
        """The caption from the article page, the table itself from ``T<N>.html``.

        ⚠️ ``div.inset#T1`` on the article page holds **only** a caption and a
        link; the markup is on the side page. With no side page the caption is
        still printed -- a table that is announced and missing is a fact about
        the run, and silently dropping it would read as "this paper has no
        tables".
        """
        label, caption = self._float_label_and_caption(inset)
        blocks: List[str] = []
        text = f"**{label}** {caption}".strip() if label else caption
        if text:
            blocks.append(text)
        table_md = self._table_md_from_side_page(inset.get('id') or '')
        if table_md:
            blocks.append(table_md)
        else:
            blocks.append(f"*[{label or inset.get('id')} 的表格内容来自 "
                          f"{inset.get('id')}.html，本次未取到]*")
        return blocks

    def _table_md_from_side_page(self, name: str) -> str:
        html = self._side_pages.get(name) or ''
        if not html:
            return ''
        soup = parse_article_html(html)
        annex = soup.find(id='annex') or soup
        table = annex.find('table')
        if table is None:
            return ''
        return html_table_to_markdown(table, self._inline_md)

    @staticmethod
    def _float_number(float_id: str) -> str:
        match = re.search(r'(\d+)', float_id or '')
        return match.group(1) if match else '0'

    # ==================================================================
    # Figures
    # ==================================================================

    def extract_figures_from_html(self, html: str) -> Dict[str, dict]:
        """``{'fig_N': {'url': original, 'original_url': thumbnail}}``.

        ⚠️ **The article page only has thumbnails** (``…-fig1_small.jpg``).
        The full-size rendering is advertised on the figure's own page as
        ``a.btn2.original`` (a ``.tif``), with ``div.img_annex img`` -- the
        1200px JPEG -- next to it. Preference order is original → annex JPEG →
        thumbnail, so a figure page that failed to load costs resolution and
        not the image.
        """
        soup = parse_article_html(html or '')
        figures: Dict[str, dict] = {}
        for inset in self._body_floats(soup, 'F'):
            name = inset.get('id')
            number = self._float_number(name)
            thumb = ''
            img = inset.find('img')
            if img is not None:
                thumb = (img.get('src') or '').strip()
            best = self._original_image_url(name) or thumb
            if not best:
                continue
            figures[f'fig_{number}'] = {
                'url': self._absolute(best),
                'original_url': (self._absolute(thumb)
                                 if thumb and thumb != best else None),
            }
        return figures

    def _original_image_url(self, name: str) -> str:
        """The best image URL the figure's own page offers, or ''."""
        html = self._side_pages.get(name) or ''
        if not html:
            return ''
        soup = BeautifulSoup(html, 'html.parser')
        annex = soup.find(id='annex') or soup
        original = annex.find('a', class_='original', href=True)
        if original is not None:
            return original['href'].strip()
        img = annex.select_one('div.img_annex img[src]')
        return img['src'].strip() if img is not None else ''

    def _absolute(self, url: str) -> str:
        if not url:
            return ''
        return urljoin(self._fulltext_url or self.actual_base_url, url)

    @staticmethod
    def _body_floats(soup, kind: str) -> List:
        """The article's own ``div.inset#F*`` / ``#T*``, in document order.

        ⚠️ Scoped to ``div#contenu`` and to insets that **have an id**: the
        "All Tables" / "All Figures" listings at the end of the page repeat
        every float as an id-less ``div.inset``.
        """
        container = soup.find(id='contenu') or soup
        pattern = re.compile(rf'^{kind}\d+$')
        return [el for el in container.find_all('div', class_='inset',
                                                id=pattern)]

    # ==================================================================
    # Movies / supplemental material
    # ==================================================================

    def extract_olm_items(self, olm_html: str) -> Tuple[List[str], Dict[str, str]]:
        """``(urls, {url: description})`` from the "online material" page.

        📌 **The same page serves two different things**, and only the markup
        says which: A&A's 10.1051/0004-6361/202346508 lists *movies* in
        ``div.movie`` players, while EPJAP's 10.1051/epjap/2023220290 lists a
        *Supplementary Material* PDF as a bare "(Access here)" link inside a
        ``<p>``. Both shapes are read, and the page's own ``h2.sec`` supplies
        the markdown heading so it does not have to guess which it got.

        Each ``div.movie`` holds a ``<video>`` with several ``<source>``s and
        then a "Download Video: Formats: …" paragraph listing them again as
        plain links. The **"Original …"** entry is what we take -- the mp4 /
        webm / ogg beside it are the publisher's web renditions, and the
        original is the file the authors submitted. It is the last link in the
        row; the name is the predicate, not the position.

        The description is the paragraph *before* the player ("Movie 1
        associated with Fig. 1"), which is the only place the movie's number
        and the figure it belongs to are stated.
        """
        soup = BeautifulSoup(olm_html or '', 'html.parser')
        urls: List[str] = []
        descriptions: Dict[str, str] = {}
        for index, movie in enumerate(soup.find_all('div', class_='movie'), 1):
            anchors = [a for a in movie.find_all('a', href=True)
                       if a.get_text(strip=True)]
            if not anchors:
                continue
            chosen = next((a for a in anchors
                           if a.get_text(strip=True).lower().startswith('original')),
                          anchors[-1])
            url = self._absolute(chosen['href'].strip())
            if not url or url in descriptions:
                continue
            caption = self._movie_caption(movie)
            fmt = chosen.get_text(' ', strip=True)
            urls.append(url)
            # ⚠️ No brackets in the format note: this string becomes a
            # markdown link's text, and a nested "[…]" closes it early.
            descriptions[url] = ((caption or f"Movie {index}")
                                 + (f" — {fmt}" if fmt else ''))

        if not urls:
            urls, descriptions = self._olm_file_links(soup)
        return urls, descriptions

    def _olm_file_links(self, soup) -> Tuple[List[str], Dict[str, str]]:
        """The no-player shape: a sentence with a link to the file.

        ``<p>Supplementary Material : results obtained with the LDA
        approximation. <a href="…-1-olm.pdf">(Access here)</a></p>``

        ⚠️ The description is the paragraph **minus the anchor's own text**:
        "(Access here)" says nothing, and the sentence beside it is the only
        statement of what the file is. Anchors are selected by **not**
        pointing at another HTML page -- the link text is a UI label, and the
        extension varies with what the authors submitted.
        """
        container = soup.find(id='contenu-min') or soup
        urls: List[str] = []
        descriptions: Dict[str, str] = {}
        for anchor in container.find_all('a', href=True):
            href = anchor['href'].strip()
            if not href or href.startswith('#'):
                continue
            path = href.split('?', 1)[0].split('#', 1)[0]
            if '.' not in path.rsplit('/', 1)[-1]:
                continue
            if re.search(r'\.(html?|xhtml|php)$', path, re.I):
                continue
            url = self._absolute(href)
            if url in descriptions:
                continue
            holder = anchor.find_parent('p')
            caption = ''
            if holder is not None:
                label = anchor.get_text(' ', strip=True)
                caption = re.sub(r'\s+', ' ',
                                 holder.get_text(' ', strip=True)
                                 .replace(label, ' ')).strip(' .:,;')
            urls.append(url)
            descriptions[url] = caption or path.rsplit('/', 1)[-1]
        return urls, descriptions

    @staticmethod
    def olm_heading(olm_html: str, default: str = 'Supplemental Material') -> str:
        """The page's own section title ("Movies" / "Supplementary Material")."""
        soup = parse_article_html(olm_html or '')
        heading = soup.select_one('#contenu-min h2.sec, #article h2.sec')
        text = heading.get_text(' ', strip=True) if heading else ''
        return text or default

    @staticmethod
    def _movie_caption(movie) -> str:
        """The "Movie 1 associated with Fig. 1" line above a player.

        ⚠️ **The markup is invalid**: the caption's ``<p>`` is never closed,
        so the player ends up *inside* it. html.parser keeps that nesting
        (``movie.parent`` is the ``<p>``) while lxml would close the paragraph
        and make them siblings -- so both shapes are handled, and the ``<p>``
        is read without the player's own text.
        """
        holder = movie.parent if (movie.parent is not None
                                  and movie.parent.name == 'p') else None
        if holder is None:
            holder = movie.find_previous_sibling('p')
        if holder is None:
            return ''
        text = ' '.join(child for child in holder.strings
                        if child.parent is holder)
        return re.sub(r'\s+', ' ', text).strip()

    # ==================================================================
    # Body
    # ==================================================================

    def extract_article_text_from_html(self, html: str) -> str:
        """The article body as markdown, walked by heading.

        ⚠️ The walk stops at ``h2#references`` -- the reference list is
        rendered from ``extract_references`` so it gets numbered the same way
        every other publisher's does -- and the footnote definitions after the
        final ``<hr>`` are collected and printed last, under their own
        heading, instead of being left where they would read as stray prose.
        """
        # ⚠️ parse_article_html (lxml), not html.parser: EDP leaves a
        # paragraph open before every float, so `html.parser` nests
        # `div.inset#T1` *inside* the empty `<p>` that precedes it and the
        # walk never sees it as a float -- measured: every table and figure
        # came out as a literal `::: {#T1}` pandoc fence. A browser closes
        # the `<p>`, and so does lxml.
        soup = parse_article_html(html or '')
        container = soup.find(id='contenu')
        if container is None:
            return ''

        blocks: List[str] = []
        footnotes: List[str] = []
        in_footnotes = False

        for child in container.children:
            if getattr(child, 'name', None) is None:
                continue
            name = child.name
            classes = child.get('class') or []
            node_id = child.get('id') or ''

            if name == 'hr':
                # Everything after the rule is footnote definitions.
                in_footnotes = True
                continue
            if in_footnotes:
                footnotes.extend(self._render_footnotes(child))
                continue

            if name in ('h2', 'h3', 'h4'):
                if name == 'h2' and node_id in self.BODY_STOP_IDS:
                    break
                blocks.extend(self._render_heading(child, name))
                continue
            if node_id == 'head' or 'license' in classes:
                # Title block and the licence notice: page furniture, and the
                # metadata block already carries what is in them.
                continue
            if name == 'div' and 'inset' in classes:
                if node_id.startswith('T'):
                    blocks.extend(self._render_table(child))
                else:
                    blocks.extend(self._render_figure(child))
                continue
            if name in ('ul', 'ol'):
                blocks.extend(self._render_list(child))
                continue
            if name == 'p':
                text = child.get_text(' ', strip=True)
                # The copyright line; the licence block beside it is skipped
                # above, and neither is part of the paper.
                if text.startswith('©'):
                    continue
                blocks.extend(self._render_paragraph(child))
                continue
            if name == 'table':
                blocks.append(html_table_to_markdown(child, self._inline_md))
                continue
            if name == 'div':
                nested = self._render_footnotes(child)
                if nested:
                    footnotes.extend(nested)
                    continue
                md = self._inline_md(child)
                if md:
                    blocks.append(md)
                continue

        if footnotes:
            blocks.extend(['## Notes', *footnotes])

        return '\n\n'.join(b for b in blocks if b).strip()

    def _render_heading(self, node, tag: str) -> List[str]:
        """``h2``→``##``, ``h3``→``###``, ``h4``→``####``.

        ⚠️ Depth comes from the tag, never from the numbering in the text:
        A&A's appendices are ``h2.sec`` reading "Appendix A", and three of
        them in 10.1051/0004-6361/202243956 **share** ``id="app"`` -- so ids
        cannot be the key either. Some ``h4.sec3`` are run-in headings with no
        number at all ("Nightside cloud coverage"); they are still headings.
        """
        # ⚠️ The node's *contents*, not the node: handing pandoc a whole
        # <h2 class="sec" id="S1"> makes it emit its own "## 1. Introduction
        # {#S1}" inside ours ("## ## 1. Introduction {#S1}").
        text = self._inline_md(node.decode_contents())
        if not text:
            return []
        # pandoc escapes a leading "5." so it cannot be read as an ordered
        # list -- but this run is going behind "## ", where it cannot be.
        text = re.sub(r'^((?:[A-Z]\s)?[\d.]+)\\\.', r'\1.', text)
        level = {'h2': '##', 'h3': '###', 'h4': '####'}[tag]
        return [f"{level} {text}"]

    def _render_paragraph(self, node) -> List[str]:
        """A paragraph, or the display formula that is standing in one.

        EDP puts each numbered equation in its own ``<p>`` -- but not always
        alone: a paragraph can end with an equation, so both are rendered in
        document order rather than one being chosen over the other.
        """
        blocks: List[str] = []
        displays = node.find_all('span', class_='ressouce-equation-block')
        if not displays:
            md = self._inline_md(node)
            return [md] if md else []

        working = BeautifulSoup(str(node), 'html.parser')
        rendered: List[str] = []
        for span in working.find_all('span', class_='ressouce-equation-block'):
            formula = self._render_display_formula(span)
            if formula:
                rendered.append(formula)
            span.decompose()
        prose = self._inline_md(working)
        if prose:
            blocks.append(prose)
        blocks.extend(rendered)
        return blocks

    def _render_list(self, node, depth: int = 0) -> List[str]:
        """A ``<ul>``/``<ol>``, nesting included.

        📌 **The publisher's own markers are kept.** EDP writes them as plain
        text inside the ``<li>`` ("(1) ", "(a) ") with
        ``list-style-type: none`` on the list, because the prose refers to
        them ("as described in (2) above"). ``ul.dash`` has no marker text and
        gets a plain bullet. ⚠️ A nested list is a *child of the ``<li>``*
        here, so the item's own text must be rendered without it -- otherwise
        every sub-item is printed twice, once flattened into its parent.
        """
        indent = '  ' * depth
        blocks: List[str] = []
        for item in node.find_all('li', recursive=False):
            working = BeautifulSoup(str(item), 'html.parser').li
            sublists = []
            for sub in working.find_all(['ul', 'ol'], recursive=False):
                sublists.append(BeautifulSoup(str(sub), 'html.parser'))
                sub.decompose()
            paragraphs = [self._inline_md(p) for p in working.find_all('p')]
            own = [p for p in paragraphs if p]
            if not own:
                own = [self._inline_md(working)]
            marker = self._list_marker(working)
            first = f"{indent}- {(marker + ' ') if marker else ''}{own[0]}".rstrip()
            lines = [first]
            # A continuation paragraph belongs to the same item, indented so
            # markdown keeps it inside the bullet.
            lines.extend(f"{indent}  {extra}" for extra in own[1:] if extra)
            blocks.append('\n'.join(lines))
            for sub in sublists:
                target = sub.find(['ul', 'ol'])
                if target is not None:
                    blocks.extend(self._render_list(target, depth + 1))
        return blocks

    @staticmethod
    def _list_marker(item) -> str:
        """The publisher's marker, from the bare text before the item's ``<p>``.

        Only a parenthesised/short run counts -- anything longer is prose that
        happens not to be wrapped in a ``<p>``, and must not be mistaken for a
        label.
        """
        for child in item.children:
            if isinstance(child, NavigableString):
                text = child.strip()
                if text and len(text) <= 6 and re.fullmatch(r'[(\[]?[\w.]{1,4}[)\].]?', text):
                    return text
            elif getattr(child, 'name', None) in ('p', 'ul', 'ol'):
                break
        return ''

    def _render_footnotes(self, node) -> List[str]:
        """``div#FN<N>`` → ``[n] text``.

        ⚠️ The marker is the ``<sup>``'s own text, not the number in the id:
        footnote **1** lives in ``div#FN2`` (the ids are offset by one), and
        the body's reference to it reads ``href="…#FN2"`` with the text "1".
        Numbering off the id would renumber every footnote in the paper.
        """
        blocks: List[str] = []
        for note in node.find_all(id=re.compile(r'^FN\d+$')) if node.name == 'div' else []:
            sup = note.find('sup')
            marker = sup.get_text(strip=True) if sup else ''
            if sup is not None:
                sup.decompose()
            # The contents, not the element: the div carries id="FN2" and
            # pandoc would print it as a literal "::: {#FN2}" fence.
            text = self._inline_md(note.decode_contents())
            if text:
                blocks.append(f"[{marker}] {text}" if marker else text)
        return blocks

    # ==================================================================
    # References
    # ==================================================================

    @classmethod
    def extract_references_from_html(cls, html: str) -> List[str]:
        """``ol.references > li`` as plain text.

        ⚠️ Every entry ends with a row of link-out buttons -- ``[NASA ADS]``,
        ``[CrossRef]``, ``[Google Scholar]``, ``[EDP Sciences]`` -- and the
        Google Scholar one is a ~300-character search URL. They are dropped by
        the shape of the **anchor text** (bracketed), which costs nothing and
        does not need a list of hosts that will grow.
        """
        soup = BeautifulSoup(html or '', 'html.parser')
        container = soup.find('ol', class_='references')
        if container is None:
            return []
        references: List[str] = []
        for item in container.find_all('li', recursive=False):
            working = BeautifulSoup(str(item), 'html.parser')
            for anchor in working.find_all('a'):
                if re.fullmatch(r'\[.*\]', anchor.get_text(' ', strip=True)):
                    anchor.decompose()
            for span in working.select('span.Z3988'):
                span.decompose()
            text = re.sub(r'\s+', ' ', working.get_text(' ', strip=True)).strip()
            text = re.sub(r'[\s,;]+$', '', text)
            if text:
                references.append(text)
        return references

    @staticmethod
    def reference_page_url(html: str) -> str:
        """The "References" entry of the "Article contents" block, or ''.

        EDP keeps the reference list on its own page
        (``…/ref/2026/04/…/….html``) and links it from the same block that
        offers the PDF and the supplementary material.
        """
        soup = parse_article_html(html or '')
        for anchor_el in soup.select('ul.article_doc a[href], div.article_doc a[href]'):
            if '/ref/' in anchor_el['href']:
                return anchor_el['href'].strip()
        return ''

    async def _references(self, page, html: str) -> List[str]:
        """The reference list, from the publisher's own References page.

        📌 **Why not just the article page**: an abstract-only article has no
        ``ol.references`` at all -- measured on
        10.1051/epjconf/202634803001, whose 49 references exist *only* on the
        ``/ref/`` page. And the two are the same markup, so one extractor
        serves both.

        The page is landed as ``ref.html`` like every other page this run
        opens. The article's own list stays the fallback: if the fetch comes
        back empty (or there is no link), nothing is lost on a full-text
        article, which carries the list inline.
        """
        inline = self.extract_references_from_html(html)
        target = self.reference_page_url(html)
        if not target:
            return inline
        target = urljoin(self._fulltext_url or self.actual_base_url, target)
        ref_html = await self._load_side_page(page, 'ref', target)
        from_page = self.extract_references_from_html(ref_html)
        if from_page:
            if len(from_page) != len(inline):
                print(f"  ↪ 参考文献取自 References 页: {len(from_page)} 条"
                      f"（正文页上是 {len(inline)} 条）")
            return from_page
        if ref_html:
            print("  ⚠️  References 页上没有解析出条目，用正文页那份")
        return inline

    async def extract_references(self, html: str = '') -> List[str]:
        return self.extract_references_from_html(html or await self.get_page_html())

    # ==================================================================
    # Contract
    # ==================================================================

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'EDPHandler'
        )
        doi = self.doi
        set_actual_base_url(self, page)

        try:
            html = await self.get_page_html(page)
            self._warn_if_challenged(html)
            self._fulltext_url = self._fulltext_url_from_html(html) or self.actual_base_url
            # ⚠️ Before anything reads the markup: doi.org lands on the
            # abstract page for some journals, and every selector below
            # would find nothing on it -- without a word in the log.
            html = await self._promote_to_fulltext(page, html)
            self._fulltext_url = self._fulltext_url_from_html(html) or self._fulltext_url

            metadata = self.extract_metadata_from_html(html)
            metadata['doi'] = doi or metadata.get('doi', '')

            pdf_url = await self.get_pdf_url(doi)

            # ⚠️ The side pages must be fetched BEFORE the body is rendered:
            # the tables and the full-size images only exist there. The
            # article itself stays available throughout -- _raw_server_html
            # was pinned before any of these navigations.
            soup = parse_article_html(html)
            names = ([el.get('id') for el in self._body_floats(soup, 'F')]
                     + [el.get('id') for el in self._body_floats(soup, 'T')])
            if names:
                print(f"  ↪ 取 {len(names)} 个图/表页（高清图与表格内容只在那里）")
            for name in names:
                await self._load_side_page(page, name)

            supplemental_urls: List[str] = []
            supplemental_descriptions: Dict[str, str] = {}
            olm_url = self._olm_url(html)
            if olm_url:
                olm_html = await self._load_side_page(page, 'olm', olm_url)
                supplemental_urls, supplemental_descriptions = (
                    self.extract_olm_items(olm_html))
                if supplemental_urls:
                    metadata['_olm_heading'] = self.olm_heading(olm_html)

            metadata['references'] = await self._references(page, html)

            figure_urls = self.extract_figures_from_html(html)
            body_md = self.extract_article_text_from_html(html)
            if body_md:
                metadata['_body_md'] = body_md

            print(f"  ✓ 正文: {len(body_md):,} 字符")
            print(f"  ✓ 参考文献: {len(metadata['references'])} 条")
            print(f"  ✓ 图片: {len(figure_urls)} 个")
            print(f"  ✓ 补充材料: {len(supplemental_urls)} 个")

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figure_urls,
                    'supplemental_urls': supplemental_urls,
                    'supplemental_descriptions': supplemental_descriptions,
                },
                'fulltext_data': html,
                'journal_name': 'edp',
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

    # ==================================================================
    # Markdown
    # ==================================================================

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        figure_filenames = kwargs.get('figure_filenames') or {}
        figure_urls = kwargs.get('figure_urls') or {}
        supplemental_urls = kwargs.get('supplemental_urls') or []
        supplemental_descriptions = kwargs.get('supplemental_descriptions') or {}
        supplemental_downloads = kwargs.get('supplemental_downloads') or []

        md: List[str] = [f"# {metadata.get('title') or 'EDP Sciences Article'}", '']

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
        if metadata.get('_keywords'):
            md.extend(['**Key words:** ' + ' / '.join(metadata['_keywords']), ''])

        body_md = (metadata.get('_body_md') or '').strip()
        if not body_md and isinstance(article_text, str) and article_text.strip():
            body_md = self.extract_article_text_from_html(article_text)
        if body_md:
            md.extend(['---', '',
                       self._resolve_figures(body_md, figure_filenames, figure_urls),
                       ''])
        else:
            # Say it, rather than let an empty document read as a failed
            # extraction: EDP publishes plenty of articles (EPJ Web of
            # Conferences, for one) with no full-text rendition at all.
            md.extend(['---', '',
                       '*EDP 没有为这篇提供 full html 版本（只有摘要和 PDF），'
                       '正文见 paper.pdf。*', ''])

        if supplemental_urls or supplemental_downloads:
            heading = metadata.get('_olm_heading') or 'Supplemental Material'
            md.extend(['---', '', f"## {heading}", ''])
            for url in supplemental_urls:
                description = supplemental_descriptions.get(url, '')
                name = url.rsplit('/', 1)[-1]
                local = next((str(f) for f in supplemental_downloads
                              if str(f).endswith(name)), '')
                md.append(f"- [{description or name}](<{local or url}>)")
            md.append('')

        references = metadata.get('references') or []
        if references:
            md.extend(['---', '', '## References', ''])
            for index, ref in enumerate(references, 1):
                md.extend([f"[{index}] {ref}", ''])
        elif metadata.get('_crossref_references'):
            # ⚠️ An abstract-only article has no ``ol.references`` on the page
            # (EDP keeps the list on a separate ``/ref/`` page), and the main
            # flow's Crossref copy lands under ``_crossref_references`` --
            # a different key, kept for BibTeX. Without this branch
            # 10.1051/epjconf/202634803001 published **no references at all**
            # while 49 of them were sitting in metadata.
            md.extend(['---', '', '## References', ''])
            for index, ref in enumerate(metadata['_crossref_references'], 1):
                text = (ref.get('unstructured') or '').strip()
                md.extend([f"[{index}] {text}" if text
                           else generate_reference_text_from_crossref(ref, index=index), ''])

        return '\n'.join(md).rstrip() + '\n'

    @staticmethod
    def _resolve_figures(body_md: str, figure_filenames: dict,
                         figure_urls: dict) -> str:
        """``[FIGURE_n]`` → the local file, else the remote URL.

        A placeholder whose file did not download falls back to the URL rather
        than vanishing: a missing image should be visible in the markdown.
        """
        def replace(match):
            number = match.group(1)
            filename = (figure_filenames.get(number)
                        or figure_filenames.get(int(number))) if figure_filenames else None
            if filename:
                return f"![Figure {number}]({filename})"
            info = figure_urls.get(f'fig_{number}')
            url = info.get('url') if isinstance(info, dict) else info
            if url:
                return f"![Figure {number}]({url})"
            return f"*[Figure {number} 未下载]*"

        return re.sub(r'\[FIGURE_(\d+)\]', replace, body_md)

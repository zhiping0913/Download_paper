"""Taylor & Francis handler (tandfonline.com, DOI prefix 10.1080).

Full text, read from the **server's own response** (``page_raw.html``) --
T&F renders the whole article server-side under ``div.hlFld-Fulltext``.

What the page looks like, and where each piece comes from:

  * **metadata** -- ``<script type="application/ld+json">`` (a
    ``ScholarlyArticle`` node: title, authors, keywords, date, pages).
  * **abstract** -- the rendered ``div.hlFld-Abstract#abstractId1``, through
    the formula pipeline (the ld+json copy is plain text, so a formula in the
    abstract would be flattened); ld+json is the fallback.
  * **graphical abstract** -- ``div.hlFld-Abstract`` with an
    ``h2#graphical-abstract``; its image becomes ``key_image``.
  * **body** -- ``div.NLM_sec`` sections (``NLM_sec_level_N``), headings
    ``h2/h3/h4.section-heading-N``, plain ``<p>``.
  * **formulas** -- ⚠️ **every formula is there twice**: a
    ``span.NLM_disp-formula-image`` (a GIF in ``data-formula-source``) and a
    ``span.NLM_disp-formula`` holding MathML. The MathML is used; the GIF is
    only the fallback for (older) articles that ship no MathML for it, and is
    then referenced remotely, not downloaded (same rule as SPIE/PNAS).
    Display formulas sit **inside** a ``<p>``; the label ``(1)`` is in
    ``span.mathjaxLabel``.
  * **figures** -- ``div.figureView``. ⚠️ The ``<img>`` on the page is a
    500 px preview with a *different asset id* from the full-size image; the
    full-size one is only in the ``tandf.tfviewerdata`` JSON (the "Display
    full size" viewer's data, in the raw response).
  * **tables** -- ⚠️ **not on the page**: ``div.tableView`` carries only the
    caption and two buttons. The page's own "Download CSV"
    (``/action/downloadTable?id=T0001&doi=…&downloadType=CSV``) is fetched,
    landed in ``html/`` as ``table_T0001.csv`` and rendered into the
    markdown. ``tfviewerdata`` also holds each table's HTML -- that is the
    fallback when the CSV cannot be had (or the table has none,
    ``settings.hasCsvFormat``).
  * **references** -- ``ul.references > li``.
  * **supplements** -- figshare (by ``resource_doi``, verified) first: it
    serves the files openly and under the publisher's own names; then the
    page's ``/action/downloadSupplement`` links, deduplicated by filename.

⚠️ ``isAccessibleForFree`` is NOT turned into ``access: False``. It says
whether the article is free, not whether *we* can read it -- institutional
access is exactly the case it cannot see, and a wrong ``False`` silently skips
an article that would have downloaded (see the access-decision rules in
CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

from html_to_md_converter import mathml_to_latex_pandoc
from publisher.base import PublisherHandler
from publisher.wildcard import (
    convert_html_fragment_to_markdown,
    init_extract_all_page,
    parse_article_html,
    set_actual_base_url,
)


# Elements that are page furniture, never content.
_DROP_SELECTORS = (
    'span.hidden',               # a full copy of each cross-referenced formula
    'span.off-screen',           # "Citation" / "Equation" screen-reader words
    'div.tableDownloadOption',   # "Download CSV" / "Display Table" buttons
    'button.show-full-size',
    'button.show-more',
    'div.extra-links',           # reference linkouts (WoS, Google Scholar…)
    'script', 'style', 'noscript',
)

# Top-level blocks of div.hlFld-Fulltext that are not part of the body.
_SKIP_CLASSES = {'hlFld-Abstract', 'abstractKeywords', 'with-border',
                 'pb-dropzone', 'supplemental-material-container'}


class TandFHandler(PublisherHandler):
    """Taylor & Francis Online: full text, tables via the page's CSV export."""

    PUBLISHER = 'tandf'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = ''
        self._tables_md: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # small helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r'[ \t]+', ' ', (text or '').replace('\r', '')).strip()

    def _site(self) -> str:
        """scheme://host of the landed page -- taken from the URL, not assumed."""
        for cand in (getattr(self, 'actual_base_url', '') or '',
                     getattr(self, '_landing_url', '') or ''):
            match = re.match(r'(https?://[^/]+)', cand.strip())
            if match and 'doi.org' not in match.group(1):
                return match.group(1)
        return 'https://www.tandfonline.com'

    def _abs(self, url: str) -> str:
        if not url:
            return ''
        if url.startswith('//'):
            return 'https:' + url
        return urljoin(self._site() + '/', url)

    # ------------------------------------------------------------------
    # ld+json
    # ------------------------------------------------------------------

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
    # tandf.tfviewerdata -- the full-size viewer's data
    # ------------------------------------------------------------------

    @staticmethod
    def viewer_data(html: str) -> dict:
        """The ``tandf.tfviewerdata = {...};`` object, or ``{}``.

        📌 This is the only place the **full-size** figure URLs exist (the
        page's ``<img>`` is a 500 px preview under a different asset id) and
        the only place the table *markup* exists (the page shows a caption
        and a "Download CSV" button). It is in the raw response, so it costs
        nothing.
        """
        match = re.search(r'tandf\.tfviewerdata\s*=\s*(\{.*?\})\s*;\s*</script>',
                          html or '', re.S)
        if not match:
            return {}
        try:
            return json.loads(match.group(1))
        except ValueError:
            return {}

    @staticmethod
    def _viewer_items(data: dict, kind: str) -> Dict[str, dict]:
        # Keyed case-insensitively: older articles write the viewer's ids in
        # lower case ('f0004') while the page's buttons say 'F0004' (实测
        # 10.1080/14685248.2018.1462496) -- an exact match found no
        # full-size image at all and fell back to the preview GIF.
        return {str(item.get('id')).lower(): item for item in (data.get(kind) or [])
                if isinstance(item, dict) and item.get('id')}

    def _viewer_figure_src(self, data: dict, fig_id: str) -> str:
        item = self._viewer_items(data, 'figures').get((fig_id or '').lower()) or {}
        soup = BeautifulSoup(item.get('content') or '', 'html.parser')
        # ⚠️ Not the first <img>: a caption's formulas are <img src="//:0">
        # placeholders, and they come before the figure (measured
        # 10.1080/00268976.2020.1802525). The figure's own <img> has
        # id="fig-…" and a real src.
        imgs = [i for i in soup.find_all('img')
                if (i.get('src') or '') and not i['src'].startswith('//:')
                and not i.get('data-formula-source')]
        best = next((i for i in imgs if (i.get('id') or '').startswith('fig-')),
                    imgs[-1] if imgs else None)
        return self._abs(best['src']) if best is not None else ''

    # ------------------------------------------------------------------
    # Links
    # ------------------------------------------------------------------

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
        ``/action/downloadSupplement?doi=…&file=…``. The filename hint is the
        ``file=`` parameter: the URL's basename is ``downloadSupplement``.
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
            label = cls._clean(heading.get_text(' ', strip=True)) if heading else ''
            match = re.search(r'[?&]file=([^&]+)', url)
            from urllib.parse import unquote
            filename = unquote(match.group(1)).split('/')[-1] if match else ''
            entry = {'url': url}
            if filename:
                entry['filename'] = filename
            links.append(entry)
            descriptions[url] = ' — '.join(x for x in (
                label, cls._clean(anchor.get_text(' ', strip=True))) if x)
        return links, descriptions

    @staticmethod
    def figshare_supplements(doi: str) -> Tuple[List[dict], Dict[str, str]]:
        """Files figshare records against this DOI (``resource_doi``, verified).

        📌 T&F deposits every supplement on figshare (the page's
        ``div.figshare_widget`` is filled from it by JS, so the raw response
        holds none of it). figshare serves the bytes openly and under the
        publisher's own filenames -- measured 10.1080/00268976.2020.1802525:
        ``S1_Virial Formula.pdf`` + ``S2_Virial Data.docx``, while the page
        itself offers one ``.zip``. The lookup is the one AIP already uses.
        """
        from publisher.aip import AIPHandler
        links: List[dict] = []
        descriptions: Dict[str, str] = {}
        for aid in AIPHandler._figshare_articles_by_resource_doi(doi):
            entries, desc = AIPHandler._figshare_article_files(aid)
            for entry in entries:
                links.append(entry)
                descriptions[entry['url']] = (entry.get('filename')
                                              or desc.get(entry['url'], ''))
        return links, descriptions

    # ------------------------------------------------------------------
    # Formulas
    # ------------------------------------------------------------------

    @staticmethod
    def _unwrap_mtable(math: Tag) -> str:
        """``'aligned'`` / ``''`` -- and collapse a 1×1 ``mtable`` in place.

        T&F wraps **every** display formula in an ``mtable`` (columnalign
        "right left right left…"), which pandoc renders as
        ``\\begin{matrix}{…}\\end{matrix}`` -- legal, but a matrix is not what
        the author wrote. One cell → unwrap it; several rows of the
        right/left pattern → the caller rewrites matrix as ``aligned``.
        """
        tables = math.find_all('mtable', recursive=False)
        if len(tables) != 1 or len(math.find_all(recursive=False)) != 1:
            return ''
        table = tables[0]
        rows = table.find_all(['mtr', 'mlabeledtr'], recursive=False)
        cells = [c for r in rows for c in r.find_all('mtd', recursive=False)]
        if len(rows) == 1 and len(cells) == 1:
            table.replace_with(*list(cells[0].contents)) if cells[0].contents \
                else table.decompose()
            return ''
        if (table.get('columnalign') or '').startswith('right left'):
            return 'aligned'
        return ''

    def _math_to_latex(self, math: Tag) -> str:
        mode = self._unwrap_mtable(math)
        latex = (mathml_to_latex_pandoc(str(math)) or '').strip()
        latex = latex[1:-1].strip() if latex.startswith('$') and latex.endswith('$') else latex
        latex = latex.strip('$').strip()
        if mode == 'aligned':
            latex = latex.replace('\\begin{matrix}', '\\begin{aligned}') \
                         .replace('\\end{matrix}', '\\end{aligned}')
        return latex

    @staticmethod
    def _formula_image_src(span: Tag) -> str:
        img = span.find('img', attrs={'data-formula-source': True})
        if img is None:
            return ''
        try:
            info = json.loads(img['data-formula-source'])
        except (ValueError, TypeError):
            return ''
        return info.get('src') or '' if info.get('type') == 'image' else ''

    def _stash_formulas(self, root: Tag, stash: Dict[str, str]) -> None:
        """Replace every formula under *root* with an opaque token.

        ⚠️ Tokens are ``DPTFMATHnZZ``-shaped: pandoc escapes brackets and
        underscores, and an escaped token can no longer be found.
        """
        for span in list(root.select('span.NLM_disp-formula, span.NLM_disp-formula-image')):
            if span.parent is None:          # already removed with its twin
                continue
            classes = span.get('class') or []
            display = 'disp-formula' in classes
            if 'NLM_disp-formula-image' in classes:
                twin = span.find_next_sibling()
                if twin is not None and 'NLM_disp-formula' in (twin.get('class') or []) \
                        and twin.find('math') is not None:
                    span.decompose()          # the MathML twin will speak
                    continue
                src = self._formula_image_src(span)
                token = f"DPTFMATH{len(stash)}ZZ"
                url = self._abs(src) if src else ''
                stash[token] = (f"\n\n![]({url})\n\n" if display else f"![]({url})") if url else ''
                span.replace_with(NavigableString(token))
                continue
            math = span.find('math')
            label_el = span.select_one('span.mathjaxLabel, span.disp_formula_label_div')
            label = ''
            if label_el is not None:
                label = re.sub(r'^\(|\)$', '', label_el.get_text('', strip=True))
            if math is None:
                span.decompose()
                continue
            latex = self._math_to_latex(math)
            token = f"DPTFMATH{len(stash)}ZZ"
            if display:
                tag = f" \\tag{{{label}}}" if label else ''
                stash[token] = f"\n\n$$\n{latex}{tag}\n$$\n\n"
            else:
                stash[token] = f"${latex}$"
            span.replace_with(NavigableString(token))

    @staticmethod
    def _restore(md: str, stash: Dict[str, str]) -> str:
        for token in sorted(stash, key=len, reverse=True):
            md = md.replace(token, stash[token])
        md = re.sub(r'[ \t]*\n\n\$\$', '\n\n$$', md)
        md = re.sub(r'\$\$\n\n[ \t]*', '$$\n\n', md)
        return re.sub(r'\n{3,}', '\n\n', md).strip()

    # ------------------------------------------------------------------
    # Inline conversion
    # ------------------------------------------------------------------

    def _prepare(self, root: Tag) -> None:
        """Strip furniture and flatten cross-references, in place."""
        for selector in _DROP_SELECTORS:
            for el in root.select(selector):
                el.decompose()
        for button in root.select('button.show-table-fig-ref'):
            button.replace_with(NavigableString(button.get_text('', strip=True)))
        for ref in root.select('span.ref-lnk'):
            ref.replace_with(NavigableString(ref.get_text('', strip=True)))
        for anchor in root.find_all('a'):
            href = anchor.get('href') or ''
            if not href or href == '#' or href.startswith('#'):
                anchor.unwrap()
                continue
            # The publisher's own typo, seen on 10.1080/00268976.2026.2756513:
            # href="https://doi.org/https://doi.org/10.1080/…".
            anchor['href'] = re.sub(r'^https?://(?:dx\.)?doi\.org/(?=https?://)', '', href)
        # pandoc prints attributes it does not know as a {…} suffix --
        # measured here: '[]{aria-level="3" role="heading"}' in every
        # caption, '{target="_blank"}' after every reference DOI link.
        for el in [root] + root.find_all(True):
            for attr in list(el.attrs):
                if attr == 'data-formula-source':
                    continue    # the GIF of an image-only formula -- read later
                if attr in ('role', 'target', 'id') or attr.startswith(('aria-', 'data-')):
                    del el.attrs[attr]
        for span in root.find_all('span'):
            if not span.attrs:
                span.unwrap()

    def _inline_md(self, el: Tag) -> str:
        stash: Dict[str, str] = {}
        import copy
        el = copy.copy(el)     # the stash rewrites the tree; leave the caller's alone
        self._stash_formulas(el, stash)
        # Contents, never the element: pandoc renders an <h2> as its own
        # "## … {#id}", which would sit inside ours.
        html = el.decode_contents()
        md = convert_html_fragment_to_markdown(html) if html.strip() else ''
        return self._restore(md, stash)

    # ------------------------------------------------------------------
    # Body walk
    # ------------------------------------------------------------------

    def _render_figure(self, fv: Tag, out: List[str], counter: List[int]) -> None:
        counter[0] += 1
        number = counter[0]
        out.append(f"[FIGURE_{number}]")
        caption = fv.select_one('p.captionText')
        if caption is not None:
            label = caption.select_one('span.captionLabel')
            label_text = self._clean(label.get_text(' ', strip=True)) if label else ''
            if label is not None:
                label.decompose()
            text = self._inline_md(caption)
            out.append(f"**{label_text}** {text}".strip() if label_text else text)
        desc = fv.select_one('div.long-desc-content')
        if desc is not None:
            text = self._clean(desc.get_text(' ', strip=True))
            if text:
                out.append(f"> *Figure description:* {text}")

    def _render_table(self, tv: Tag, out: List[str]) -> None:
        table_id = tv.get('dptable', '')
        caption = tv.select_one('p.captionText')
        if caption is not None:
            label = caption.select_one('span.captionLabel')
            label_text = self._clean(label.get_text(' ', strip=True)) if label else ''
            if label is not None:
                label.decompose()
            text = self._inline_md(caption)
            out.append(f"**{label_text}** {text}".strip() if label_text else text)
        body = self._tables_md.get(table_id, '')
        out.append(body or f"*[Table {table_id} 未取到]*")

    def _walk(self, node: Tag, out: List[str], counter: List[int]) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                text = str(child).strip()
                if text:
                    out.append(text)
                continue
            if not isinstance(child, Tag):
                continue
            classes = set(child.get('class') or [])
            name = child.name
            if classes & _SKIP_CLASSES:
                continue
            if re.fullmatch(r'h[1-6]', name):
                level = int(name[1])
                text = self._inline_md(child)
                # "3. Comparisons" at the start of a line reads as a list to
                # pandoc, which escapes it to "3\." -- not needed after '##'.
                text = re.sub(r'^(\d+)\\\.', r'\1.', text)
                if text:
                    out.append('#' * max(2, min(level, 6)) + ' ' + text)
                continue
            if 'figureView' in classes:
                self._render_figure(child, out, counter)
                continue
            if 'tableView' in classes:
                self._render_table(child, out)
                continue
            if name == 'p':
                text = self._inline_md(child)
                if text:
                    out.append(text)
                continue
            if name in ('ul', 'ol'):
                for index, li in enumerate(child.find_all('li', recursive=False), 1):
                    marker = f"{index}." if name == 'ol' else '-'
                    text = self._inline_md(li)
                    if text:
                        out.append(f"{marker} {text}")
                continue
            if name in ('div', 'section'):
                self._walk(child, out, counter)
                continue
            text = self._inline_md(child)
            if text:
                out.append(text)

    def _fulltext(self, soup: BeautifulSoup) -> Optional[Tag]:
        return soup.select_one('div.hlFld-Fulltext')

    def body_markdown(self, html: str) -> str:
        soup = parse_article_html(html)
        root = self._fulltext(soup)
        if root is None:
            return ''
        # Tag each table with its id before the buttons that carry it go.
        for tv in root.select('div.tableView'):
            wrapper = tv.select_one('[id$="-table-wrapper"]')
            tid = wrapper['id'].rsplit('-table-wrapper', 1)[0] if wrapper else ''
            if not tid:
                btn = tv.select_one('[data-id]')
                tid = btn.get('data-id', '') if btn else ''
            tv['dptable'] = tid     # not data-*: _prepare strips those
        # "Additional information" (Funding, …) is a sibling of the
        # full-text container, not inside it -- walking only the container
        # silently drops it.  Grab it before _prepare strips ids.
        extra = soup.select_one('div#infos-holder')
        if extra is not None and root in extra.parents:
            extra = None
        self._prepare(root)
        out: List[str] = []
        self._walk(root, out, [0])
        if extra is not None:
            self._prepare(extra)
            self._walk(extra, out, [0])
        return '\n\n'.join(x for x in out if x.strip())

    # ------------------------------------------------------------------
    # Figures
    # ------------------------------------------------------------------

    def figures_from_html(self, html: str) -> Tuple[Dict[str, dict], str]:
        """``({'fig_N': {'url', 'original_url'}}, graphical-abstract URL)``.

        Numbered in document order -- the same order ``_walk`` emits the
        ``[FIGURE_N]`` placeholders in. The graphical abstract is not a
        numbered figure; it becomes ``key_image``.
        """
        soup = parse_article_html(html)
        root = self._fulltext(soup)
        data = self.viewer_data(html)
        figures: Dict[str, dict] = {}
        ga_url = ''
        if root is None:
            return figures, ga_url
        for fv in root.select('div.figureView'):
            img = fv.select_one('img.figure-image')
            if img is None:
                continue
            preview = self._abs(img.get('src') or img.get('data-src') or '')
            # The figure's own controls, not the first [data-id]: a caption
            # that cites a table carries that table's button too (Figure 4
            # of 10.1080/14685248.2018.1462496 offered 't0001' first).
            id_el = fv.select_one('a.thumbnail[data-id], button.show-full-size[data-id]')
            fig_id = id_el.get('data-id', '') if id_el else ''
            full = self._viewer_figure_src(data, fig_id) if fig_id else ''
            in_ga = fv.find_parent('div', class_='hlFld-Abstract') is not None
            if in_ga:
                if not fig_id:
                    figs = data.get('figures') or []
                    ga = next((f for f in figs if str(f.get('id', '')).upper().startswith('UF')), None)
                    if ga:
                        full = self._viewer_figure_src(data, ga['id'])
                ga_url = ga_url or full or preview
                continue
            number = len(figures) + 1
            figures[f'fig_{number}'] = {
                'url': full or preview,
                'original_url': preview if full and full != preview else None,
            }
        return figures, ga_url

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    def table_sources(self, html: str) -> List[dict]:
        """``[{id, csv_url, html}]`` for every table, in page order."""
        soup = parse_article_html(html)
        root = self._fulltext(soup)
        data = self.viewer_data(html)
        items = self._viewer_items(data, 'tables')
        tables = []
        if root is None:
            return tables
        for tv in root.select('div.tableView'):
            wrapper = tv.select_one('[id$="-table-wrapper"]')
            tid = wrapper['id'].rsplit('-table-wrapper', 1)[0] if wrapper else ''
            csv_url = ''
            for anchor in tv.select('a[href*="downloadTable"]'):
                if 'csv' in anchor['href'].lower():
                    csv_url = self._abs(anchor['href'])
                    m = re.search(r'[?&]id=([^&]+)', anchor['href'])
                    tid = tid or (m.group(1) if m else '')
                    break
            item = items.get((tid or '').lower()) or {}
            tables.append({'id': tid, 'csv_url': csv_url,
                           'html': item.get('content') or ''})
        return tables

    @staticmethod
    def _looks_like_csv(text: str) -> bool:
        head = (text or '').lstrip()[:200].lower()
        return bool(text and text.strip()) and not head.startswith(('<!doctype', '<html', '<'))

    async def fetch_csv(self, page, url: str, table_id: str = '') -> str:
        """The CSV behind "Download CSV". '' when neither route delivers one.

        Two routes, both a *navigation* the browser treats as a download:

        ① **click the page's own "Download CSV" button** in the article tab
          -- exactly what a reader does;
        ② open the CSV URL in a throwaway tab of the same context (Referer =
          the article), catching the download that aborts the navigation.

        ❌ **Not ``fetch()`` and not a bare request** -- measured on
        10.1080/00268976.2026.2756513, both answer **403** for every table,
        cookies or not, while the click and the tab both get the file
        (542 bytes for T0002). The endpoint serves navigations, not
        programmatic requests (``Sec-Fetch-Mode: cors`` / ``Dest: empty``).
        ⚠️ Judged by the bytes: a 200 that is a web page is not a CSV.
        """
        import os
        import tempfile
        from core.utilities import download_save_as_with_timeout
        if page is None:
            return ''

        async def _read(download, how: str) -> str:
            fd, tmp = tempfile.mkstemp(suffix='.csv')
            os.close(fd)
            try:
                if not await download_save_as_with_timeout(
                        download, tmp, timeout_s=60, what=f'表格 {table_id} CSV'):
                    return ''
                with open(tmp, 'rb') as fh:
                    data = fh.read()
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            text = data.decode('utf-8-sig', errors='replace')
            if self._looks_like_csv(text):
                print(f"    ✓ CSV [{how}] {len(data):,} bytes")
                return text
            print(f"    ↪ {how} 拿回来的是网页不是 CSV")
            return ''

        # ① the button
        selector = (f'a[href*="downloadTable"][href*="id={table_id}&"][href*="CSV"]'
                    if table_id else f'a[href*="downloadTable"][href*="CSV"]')
        try:
            button = page.locator(selector).first
            if await button.count():
                async with page.expect_download(timeout=30000) as info:
                    await button.click(timeout=10000)
                text = await _read(await info.value, '点击 Download CSV')
                if text:
                    return text
            else:
                print(f"    ↪ 页面上找不到表格 {table_id} 的 Download CSV 按钮")
        except Exception as exc:
            print(f"    ↪ 点击 Download CSV 未触发下载（{type(exc).__name__}），改开新标签页")

        # ② a throwaway tab
        tab = None
        try:
            tab = await page.context.new_page()
            async with tab.expect_download(timeout=30000) as info:
                try:
                    await tab.goto(url, referer=getattr(self, '_landing_url', '') or None,
                                   timeout=30000)
                except Exception:
                    pass        # "Download is starting" -- the expected abort
            return await _read(await info.value, '新标签页')
        except Exception as exc:
            print(f"    ⚠️  没拿到 CSV（{type(exc).__name__}: {str(exc)[:80]}）")
            return ''
        finally:
            if tab is not None:
                try:
                    await tab.close()
                except Exception:
                    pass

    @staticmethod
    def _md_cell(text: str) -> str:
        return re.sub(r'\s+', ' ', (text or '')).replace('|', '\\|').strip()

    def csv_to_markdown(self, text: str, caption_hint: str = '') -> str:
        """A CSV as a pipe table; single-cell rows around it as plain lines.

        T&F's CSV can open with the table's title and close with its notes,
        each a row with one non-empty cell -- those are not data rows and
        would make a ragged table, so they are lifted out: the leading ones
        are dropped when they repeat the caption, the trailing ones become
        the table's notes.
        """
        rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text))]
        rows = [r for r in rows if any(c for c in r)]
        if not rows:
            return ''
        width = max(len(r) for r in rows)

        def single(r):
            # Text in the first column only. ⚠️ Not "any one cell": a
            # header like ``"","Energy",,,`` (a colspan over empty cells)
            # also has one non-empty cell, and it is part of the table.
            return width > 1 and bool(r[0]) and not any(r[1:])

        head_notes, tail_notes = [], []
        while rows and single(rows[0]):
            head_notes.append(next(c for c in rows.pop(0) if c))
        while rows and single(rows[-1]):
            tail_notes.insert(0, next(c for c in rows.pop() if c))
        if not rows:
            return '\n\n'.join(head_notes + tail_notes)
        width = max(len(r) for r in rows)
        rows = [r + [''] * (width - len(r)) for r in rows]
        lines = ['| ' + ' | '.join(self._md_cell(c) for c in rows[0]) + ' |',
                 '|' + '---|' * width]
        for r in rows[1:]:
            lines.append('| ' + ' | '.join(self._md_cell(c) for c in r) + ' |')
        hint = re.sub(r'\W+', '', caption_hint.lower())
        out = [n for n in head_notes
               if not hint or re.sub(r'\W+', '', n.lower())[:40] not in hint]
        out.append('\n'.join(lines))
        out.extend(tail_notes)
        return '\n\n'.join(out)

    def table_html_to_markdown(self, table_html: str) -> str:
        """Fallback: the table markup from ``tfviewerdata``, through pandoc."""
        if not table_html:
            return ''
        import pypandoc
        soup = BeautifulSoup(table_html, 'lxml')
        table = soup.find('table')
        if table is None:
            return ''
        for cap in table.find_all('caption'):
            cap.decompose()
        stash: Dict[str, str] = {}
        self._prepare(table)
        self._stash_formulas(table, stash)
        notes = []
        for foot in table.find_all('tfoot'):
            text = convert_html_fragment_to_markdown(foot.decode_contents())
            if text:
                notes.append(text)
            foot.decompose()
        try:
            md = pypandoc.convert_text(str(table), 'gfm', format='html',
                                       extra_args=['--wrap=none'])
        except Exception:
            return ''
        md = self._restore(md, stash)
        return '\n\n'.join([md.strip()] + notes)

    def _land_csv(self, table_id: str, text: str) -> None:
        if not self.captured_data_dir or not text:
            return
        safe = re.sub(r'[^A-Za-z0-9_.-]', '_', table_id or 'table')
        try:
            out = Path(self.captured_data_dir) / f"table_{safe}.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding='utf-8')
            print(f"    ✓ {out.name} 已保存 ({out.stat().st_size:,} bytes)")
        except Exception as exc:
            print(f"    ⚠️  table_{safe}.csv 保存失败: {str(exc)[:60]}")

    async def collect_tables(self, page, html: str) -> None:
        soup = parse_article_html(html)
        captions = {}
        root = self._fulltext(soup)
        if root is not None:
            for tv in root.select('div.tableView'):
                wrapper = tv.select_one('[id$="-table-wrapper"]')
                if wrapper is not None:
                    cap = tv.select_one('p.captionText')
                    captions[wrapper['id'].rsplit('-table-wrapper', 1)[0]] = \
                        cap.get_text(' ', strip=True) if cap else ''
        for source in self.table_sources(html):
            tid = source['id']
            md = ''
            if source['csv_url']:
                print(f"  📊 表格 {tid}: Download CSV")
                text = await self.fetch_csv(page, source['csv_url'], tid)
                if text:
                    self._land_csv(tid, text)
                    md = self.csv_to_markdown(text, captions.get(tid, ''))
            if not md and source['html']:
                print(f"    ↪ 表格 {tid} 改用页面内嵌的表格 HTML（tfviewerdata）")
                md = self.table_html_to_markdown(source['html'])
            self._tables_md[tid] = md

    # ------------------------------------------------------------------
    # References
    # ------------------------------------------------------------------

    def references_from_html(self, html: str) -> List[str]:
        soup = parse_article_html(html)
        ul = soup.select_one('ul.references')
        if ul is None:
            return []
        refs = []
        for li in ul.find_all('li', recursive=False):
            doi_el = li.select_one('ul.getFTR[data-target]')
            doi = doi_el.get('data-target', '') if doi_el else ''
            import copy
            li = copy.copy(li)
            self._prepare(li)
            text = self._inline_md(li)
            text = re.sub(r'\s+', ' ', text).strip()
            if not text:
                continue
            if doi and doi.lower() not in text.lower():
                text += f" [doi:{doi}](https://doi.org/{doi})"
            refs.append(text)
        return refs

    @staticmethod
    def _numeric_citations(html: str) -> bool:
        """True when the in-text citations are numbers (``[6]``), not author-year."""
        labels = re.findall(r'data-ref-type="bibr"[^>]*>(?:<span class="off-screen">'
                            r'Citation</span>)?([^<]{1,40})<', html or '')
        # Ranges and lists count as numeric too: "1–20", "36-47", "3, 5".
        return bool(labels) and all(re.fullmatch(r'[\d\s,–—-]+', l) for l in labels)

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
        volume = self._node(nodes, 'PublicationVolume')
        soup = parse_article_html(html)

        authors = []
        for author in (article.get('author') or []):
            name = self._clean(author.get('name') if isinstance(author, dict) else str(author))
            if name and name not in authors:
                authors.append(name)

        keywords = [k.strip() for k in
                    re.split(r'[;,]', article.get('keywords') or '') if k.strip()]

        # The rendered abstract first: it keeps formulas, sub/superscripts
        # and italics that the ld+json plain-text copy flattens.
        abstract = ''
        section = soup.select_one('div.hlFld-Abstract#abstractId1') or next(
            (d for d in soup.select('div.hlFld-Abstract')
             if not d.select_one('#graphical-abstract')), None)
        if section is not None:
            import copy
            section = copy.copy(section)
            for heading in section.find_all(re.compile(r'^h[1-6]$')):
                heading.decompose()
            self._prepare(section)
            parts = [self._inline_md(p) for p in section.find_all('p')] or \
                    [self._inline_md(section)]
            abstract = '\n\n'.join(p for p in parts if p)
        if not abstract:
            abstract = self._clean(article.get('abstract') or article.get('description') or '')

        date = article.get('datePublished') or issue.get('datePublished') or ''
        year_match = re.search(r'\b(19|20|21)\d{2}\b', str(date))
        journal_meta = soup.find('meta', attrs={'name': 'citation_journal_title'})
        pages = '-'.join(str(p) for p in (article.get('pageStart'),
                                          article.get('pageEnd')) if p)
        # The volume hangs off the issue: PublicationIssue.isPartOf is a
        # ["PublicationVolume", "Periodical"] node, not a top-level one.
        part = issue.get('isPartOf') if isinstance(issue.get('isPartOf'), dict) else {}
        vol = str(volume.get('volumeNumber') or part.get('volumeNumber') or '').strip()
        if not vol:
            m = soup.find('meta', attrs={'name': 'citation_volume'})
            vol = (m.get('content') or '').strip() if m else ''

        return {
            'title': self._clean(article.get('headline') or article.get('name') or ''),
            'authors': authors,
            'abstract': abstract,
            '_keywords': keywords,
            'doi': (article.get('identifier') or self.doi or '').strip(),
            'journal': ((journal_meta.get('content') or '').strip()
                        if journal_meta else ''),
            'volume': vol,
            'issue': str(issue.get('issueNumber') or '').strip(),
            'year': year_match.group(0) if year_match else '',
            'pages': pages,
            'publisher': ((article.get('publisher') or {}).get('name')
                          if isinstance(article.get('publisher'), dict) else ''),
        }

    async def extract_references(self, html: str) -> list:
        return self.references_from_html(html)

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
        if live and not live.startswith(('about:', 'chrome://')) and not getattr(self, '_landing_url', ''):
            self._landing_url = live

        metadata = await self.extract_metadata(page)
        metadata['doi'] = doi or metadata.get('doi', '')
        html = getattr(self, '_landing_html', '') or ''

        figure_urls, ga_url = self.figures_from_html(html)
        if ga_url:
            metadata['key_image_url'] = ga_url
        await self.collect_tables(page, html)
        metadata['references'] = self.references_from_html(html)
        metadata['_numeric_refs'] = self._numeric_citations(html)

        page_links, page_desc = self.supplements_from_html(html, self._site())
        fs_links, fs_desc = self.figshare_supplements(doi)
        supplemental_urls: List[dict] = []
        supplemental_descriptions: Dict[str, str] = {}
        seen_names = set()
        for links, desc, source in ((fs_links, fs_desc, 'figshare'),
                                    (page_links, page_desc, 'page')):
            for entry in links:
                name = (entry.get('filename') or '').lower()
                if name and name in seen_names:
                    print(f"    ↪ 跳过页面上的 {entry.get('filename')}（figshare 已有同名文件）")
                    continue
                # ⚠️ The page's .zip is a bundle of the figshare files, not
                # something extra: 10.1080/00268976.2020.1802525's
                # tmph_a_1802525_sm0135.zip holds exactly S1_Virial Formula.pdf
                # + S2_Virial Data.docx, byte for byte the sizes figshare
                # serves. Skipped only when figshare did answer.
                if source == 'page' and fs_links and re.search(
                        r'\.(zip|rar|7z|tar|tgz|gz)$', name):
                    print(f"    ↪ 跳过页面上的打包文件 {entry.get('filename')}"
                          f"（figshare 已逐个给出文件）")
                    continue
                if name:
                    seen_names.add(name)
                supplemental_urls.append(entry)
                if desc.get(entry['url']):
                    supplemental_descriptions[entry['url']] = desc[entry['url']]

        pdf_url = await self.get_pdf_url(doi)
        body_probe = self._fulltext(parse_article_html(html))

        print(f"  ✓ 标题: {metadata.get('title', '')[:56]}")
        print(f"  ✓ 作者: {len(metadata.get('authors') or [])} 位")
        print(f"  ✓ 摘要: {len(metadata.get('abstract') or '')} 字符")
        print(f"  ✓ 正文: {'有' if body_probe is not None and body_probe.select_one('div.NLM_sec') else '无'}")
        print(f"  ✓ 图片: {len(figure_urls)} 个" + ('（另有 graphical abstract）' if ga_url else ''))
        print(f"  ✓ 表格: {len(self._tables_md)} 个"
              f"（取到 {sum(1 for v in self._tables_md.values() if v)} 个）")
        print(f"  ✓ 参考文献: {len(metadata['references'])} 条")
        print(f"  ✓ 补充材料: {len(supplemental_urls)} 个"
              f"（figshare {len(fs_links)}、页面 {len(page_links)}）")
        print(f"  ✓ PDF: {pdf_url or '(未取到)'}")

        return {
            'metadata': metadata,
            'links': {
                'pdf_url': pdf_url,
                'figure_urls': figure_urls,
                'supplemental_urls': supplemental_urls,
                'supplemental_descriptions': supplemental_descriptions,
            },
            'fulltext_data': html,
            'journal_name': 'tandf',
        }

    # ------------------------------------------------------------------
    # Markdown
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_figures(body_md: str, figure_filenames: dict, figure_urls: dict) -> str:
        def replace(match):
            number = match.group(1)
            filename = (figure_filenames.get(number) or figure_filenames.get(int(number))) \
                if figure_filenames else None
            if filename:
                return f"![Figure {number}]({filename})"
            info = figure_urls.get(f'fig_{number}')
            url = info.get('url') if isinstance(info, dict) else info
            return f"![Figure {number}]({url})" if url else f"*[Figure {number} 未下载]*"
        return re.sub(r'\[FIGURE_(\d+)\]', replace, body_md)

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        figure_filenames = kwargs.get('figure_filenames') or {}
        figure_urls = kwargs.get('figure_urls') or {}
        supplemental_urls = kwargs.get('supplemental_urls') or []
        supplemental_descriptions = kwargs.get('supplemental_descriptions') or {}
        supplemental_downloads = kwargs.get('supplemental_downloads') or []
        key_image = kwargs.get('key_image_filename')

        md: List[str] = [f"# {metadata.get('title') or 'Taylor & Francis Article'}", '']
        if metadata.get('authors'):
            md.extend(['**Authors:** ' + ', '.join(metadata['authors']), ''])

        md.extend(['## Publication', ''])
        for key, label in (('journal', 'Journal'), ('volume', 'Volume'),
                           ('issue', 'Issue'), ('pages', 'Pages'),
                           ('year', 'Year'), ('doi', 'DOI'),
                           ('publisher', 'Publisher')):
            value = str(metadata.get(key) or '').strip()
            if value:
                md.extend([f"**{label}:** {value}", ''])

        md.extend(['---', '', '## Abstract', '',
                   (metadata.get('abstract') or '').strip() or '[No abstract available.]', ''])
        if metadata.get('_keywords'):
            md.extend(['**Keywords:** ' + ', '.join(metadata['_keywords']), ''])

        ga = key_image or metadata.get('key_image_url')
        if ga:
            md.extend(['### Graphical Abstract', '', f"![Graphical Abstract]({ga})", ''])

        html = article_text if isinstance(article_text, str) else ''
        body = self.body_markdown(html) if html else ''
        md.extend(['---', '', '## Article Text', ''])
        if body:
            md.extend([self._resolve_figures(body, figure_filenames, figure_urls), ''])
        else:
            md.extend(['*页面上没有 HTML 正文；正文见 `paper.pdf`。*', ''])

        if supplemental_urls or supplemental_downloads:
            # The body usually carries its own "Supplemental Material"
            # section (a sentence pointing at the DOI); don't print a
            # second heading with the same name.
            heading = ('Supplemental Files'
                       if re.search(r'^#+ Supplemental Material\s*$', body, re.M | re.I)
                       else 'Supplemental Material')
            md.extend(['---', '', f'## {heading}', ''])
            for index, entry in enumerate(supplemental_urls):
                url = entry.get('url', '') if isinstance(entry, dict) else entry
                name = entry.get('filename') if isinstance(entry, dict) else ''
                label = name or supplemental_descriptions.get(url) or url
                local = (supplemental_downloads[index]
                         if index < len(supplemental_downloads) else '')
                md.append(f"- [{label}](<{local or url}>)")
            md.append('')

        refs = metadata.get('references') or []
        if refs:
            md.extend(['---', '', '## References', ''])
            numeric = metadata.get('_numeric_refs', True)
            for index, ref in enumerate(refs, 1):
                md.extend([f"[{index}] {ref}" if numeric else f"- {ref}", ''])

        return '\n'.join(md).rstrip() + '\n'

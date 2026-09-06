"""IEEE Xplore publisher handler (ieeexplore.ieee.org, DOI prefix 10.1109).

IEEE renders its article page client-side from a set of REST endpoints, all
keyed by the numeric *article id* (not the DOI). The rendered DOM is a 2 MB
Angular tree; the REST payloads behind it are small, clean, and -- crucially --
carry math as raw LaTeX inside ``<tex-math notation="LaTeX">`` instead of
MathJax output. So this handler ignores the rendered page for content and
talks to the endpoints directly:

    https://ieeexplore.ieee.org/document/{articleId}/            landing page
    https://ieeexplore.ieee.org/rest/document/{articleId}/?logAccess=true
                                                                body (XHTML)
    https://ieeexplore.ieee.org/rest/document/{articleId}/references
    https://ieeexplore.ieee.org/rest/document/{articleId}/multimedia
    https://ieeexplore.ieee.org/rest/document/{articleId}/footnotes

The article id and all bibliographic metadata come from the
``xplGlobal.document.metadata = {...}`` JSON blob embedded in the landing
page, which also holds ``authors`` (with ``affiliation``), ``abstract``,
``keywords``, ``pdfUrl`` / ``pdfPath`` and -- on articles that have them --
``supplementGroup``.

Every endpoint response is cached next to ``page.html`` (``rest.html``,
``references.json``, ``multimedia.json``, ``footnotes.json``) so a capture can
be re-rendered offline without touching the network.

All requests are issued from *inside* the page via ``fetch()``. IEEE binds
entitlement to the session that rendered the article; an out-of-page
``context.request`` shares cookies but not the JS/TLS fingerprint and comes
back as a denial stub.
"""

from __future__ import annotations

import json
import re
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString, Tag

try:                                        # bs4 warns when html.parser sees XML
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:                         # older bs4 -- nothing to silence
    pass

from publisher.base import PublisherHandler
from publisher.wildcard import init_extract_all_page, set_actual_base_url


# Elements that carry no article content -- IEEE's in-page UI affordances:
# "View Source" links, zoom buttons, "Show All" ref-link buttons, and the
# tooltip icon that sits inside every <disp-formula>.
_IEEE_DROP_SELECTORS = (
    'span.formula',          # "View Source" widget inside display formulas
    'div.zoom-container',
    'p.links',               # "Show All" figure / reference buttons
    'button',
)

# Inline wrappers rendered as markdown emphasis.
_IEEE_EMPHASIS = {
    'b': '**', 'strong': '**',
    'i': '*', 'em': '*',
}


# Reverse cp1252 map used by :meth:`IEEEHandler._fix_mojibake`.
#
# Python's cp1252 codec refuses the five slots the standard leaves undefined
# (0x81, 0x8d, 0x8f, 0x90, 0x9d), but the encoder that produced IEEE's mojibake
# passed those bytes through as the matching C1 control characters. A right
# double quote (U+201D -> e2 80 9d) therefore round-trips to a string ending in
# U+009D, and str.encode('cp1252') raises on it. Mapping each character back to
# its byte by hand covers those slots, so the repair does not give up on
# exactly the curly quotes it exists to fix.
_CP1252_BYTE = {chr(b): bytes([b]) for b in range(0x100)}
for _b in range(0x80, 0xA0):
    try:
        _CP1252_BYTE[bytes([_b]).decode('cp1252')] = bytes([_b])
    except UnicodeDecodeError:
        pass                                 # undefined slot: keep the C1 char


class IEEEHandler(PublisherHandler):
    """Full-text handler for IEEE Xplore."""

    IEEE_BASE = 'https://ieeexplore.ieee.org'

    # REST endpoints, formatted with the numeric article id.
    REST_BODY = '/rest/document/{aid}/?logAccess=true'
    REST_REFERENCES = '/rest/document/{aid}/references'
    REST_MULTIMEDIA = '/rest/document/{aid}/multimedia'
    REST_FOOTNOTES = '/rest/document/{aid}/footnotes'

    # Cache filenames, matching the names the reference samples were
    # captured under.
    CACHE_NAMES = {
        'body': 'rest.html',
        'references': 'references.json',
        'multimedia': 'multimedia.json',
        'footnotes': 'footnotes.json',
    }

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.IEEE_BASE
        self.article_id: str = ''
        self._xpl_cache: dict = {}

    # ==================================================================
    # Article id
    # ==================================================================

    @staticmethod
    def article_id_from_url(url: str) -> str:
        """Pull the numeric article id out of an ieeexplore URL.

        The stable full-text URL is ``/document/10398424/``; ``/abstract/...``
        and ``/stamp/stamp.jsp?...arnumber=10398424`` are also seen.
        """
        if not url:
            return ''
        m = re.search(r'/(?:document|abstract)/(\d{5,})', url)
        if m:
            return m.group(1)
        m = re.search(r'[?&]arnumber=(\d{5,})', url)
        if m:
            return m.group(1)
        return ''

    @classmethod
    def article_id_from_html(cls, html: str) -> str:
        """Pull ``"articleId":"10398424"`` out of the landing-page HTML."""
        if not html:
            return ''
        m = re.search(r'"articleId"\s*:\s*"(\d{5,})"', html)
        if m:
            return m.group(1)
        m = re.search(r'"articleNumber"\s*:\s*"(\d{5,})"', html)
        return m.group(1) if m else ''

    def resolve_article_id(self, page_url: str = '', html: str = '') -> str:
        """Resolve and remember the article id, URL first then page HTML."""
        aid = self.article_id_from_url(page_url) or self.article_id_from_html(html)
        if aid:
            self.article_id = aid
        return self.article_id

    # ==================================================================
    # Landing-page metadata blob
    # ==================================================================

    @staticmethod
    def extract_xpl_metadata(html: str) -> dict:
        """Parse the ``xplGlobal.document.metadata = {...};`` JSON blob.

        A brace-matching scan is used rather than a lazy regex: abstracts and
        author bios routinely contain ``}`` and ``;``, and a non-greedy
        ``\\{.*?\\};`` would truncate the object at the first such character.
        """
        if not html:
            return {}
        idx = html.find('xplGlobal.document.metadata')
        if idx < 0:
            return {}
        start = html.find('{', idx)
        if start < 0:
            return {}

        depth, in_str, escaped = 0, False, False
        for pos in range(start, len(html)):
            ch = html[pos]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == '\\':
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start:pos + 1])
                    except json.JSONDecodeError:
                        return {}
        return {}

    @staticmethod
    def _fix_mojibake(text: str) -> str:
        """Repair UTF-8 text that IEEE re-encoded as cp1252.

        Reference strings come back with a-circumflex sequences where curly
        quotes belong -- the classic round-trip of UTF-8 bytes read as cp1252.
        Only rewrite when the round-trip succeeds, so clean text is untouched.
        """
        if not text or 'â' not in text:
            return text
        try:
            raw = b''.join(_CP1252_BYTE[ch] for ch in text)
        except KeyError:
            return text                      # not a clean cp1252 round-trip
        try:
            return raw.decode('utf-8', errors='strict')
        except UnicodeDecodeError:
            return text

    # ==================================================================
    # REST fetching
    # ==================================================================

    def _rest_url(self, template: str) -> str:
        return self.IEEE_BASE + template.format(aid=self.article_id)

    def _cache_write(self, name: str, content: str) -> None:
        """Save a REST response next to page.html for offline re-rendering."""
        if not self.captured_data_dir or not content:
            return
        try:
            out = Path(self.captured_data_dir) / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(content, encoding='utf-8')
            print(f"    ✓ {name} 已保存 ({out.stat().st_size:,} bytes)")
        except OSError as exc:
            print(f"    ⚠️  {name} 保存失败: {exc}")

    async def _fetch_rest(self, page, url: str, cache_name: str = '') -> str:
        """GET *url* from inside the page; return the body text (or '').

        In-page ``fetch()`` rather than ``page.context.request``: IEEE ties
        entitlement to the rendering session, and the out-of-page request
        comes back as an unauthenticated stub even though cookies are shared.
        """
        print(f"  ↪ 请求 {url.replace(self.IEEE_BASE, '')}")
        try:
            result = await page.evaluate(
                """async (url) => {
                    try {
                        const r = await fetch(url, {
                            method: 'GET',
                            credentials: 'include',
                            headers: {'Accept': 'application/json, text/html, */*'},
                        });
                        if (!r.ok) return {__err: 'status ' + r.status};
                        return {text: await r.text()};
                    } catch (e) {
                        return {__err: String(e)};
                    }
                }""",
                url,
            )
        except Exception as exc:
            print(f"    ⚠️  in-page fetch 异常: {type(exc).__name__}: {str(exc)[:120]}")
            return ''

        if not isinstance(result, dict) or result.get('__err'):
            print(f"    ⚠️  请求失败: {(result or {}).get('__err', 'no response')}")
            return ''

        text = result.get('text') or ''
        if not text.strip():
            return ''
        if cache_name:
            self._cache_write(cache_name, text)
        return text

    async def _fetch_rest_json(self, page, url: str, cache_name: str = '') -> dict:
        text = await self._fetch_rest(page, url, cache_name)
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            print(f"    ⚠️  响应不是合法 JSON ({len(text):,} 字符)")
            return {}
        return payload if isinstance(payload, dict) else {}

    # ==================================================================
    # Math -- the single conversion pipeline every element goes through
    # ==================================================================

    @staticmethod
    def _tex_of(formula_el: Tag) -> str:
        """Return the LaTeX carried by ``<inline-formula>`` / ``<disp-formula>``.

        IEEE stores the source directly in ``<tex-math notation="LaTeX">``.
        Inline formulas already include their ``$`` delimiters -- sometimes
        backslash-escaped (``\\$q\\$`` in the footnotes endpoint) -- so the
        delimiters are stripped here and re-added by the caller, which knows
        whether the formula is inline or display.
        """
        tex_el = formula_el.find('tex-math')
        tex = ((tex_el.get_text() if tex_el else formula_el.get_text()) or '').strip()

        # Strip ONE symmetric delimiter pair. Only the outermost pair may be
        # backslash-escaped; an inner \$ is a literal dollar sign that belongs
        # to the formula -- IEEE writes the "sampled uniformly at random" arrow
        # as $e\stackrel{{\$} }{\leftarrow }S$, and unescaping every \$ in
        # the string would turn that decoration into a stray delimiter.
        for delim in ('$$', '\\$', '$'):
            if tex.startswith(delim) and tex.endswith(delim) and len(tex) > 2 * len(delim):
                tex = tex[len(delim):-len(delim)].strip()
                break
        return tex

    @classmethod
    def inline_md(cls, node) -> str:
        """Render an inline subtree (text + math + emphasis) to markdown.

        This is the one formula-aware text pipeline for the whole handler:
        paragraphs, list items, figure captions, algorithm steps, headings,
        footnotes and reference text all go through it, so math can never be
        dropped by a bare ``get_text()`` on any of them.
        """
        if node is None:
            return ''
        if isinstance(node, NavigableString):
            return re.sub(r'\s+', ' ', str(node))
        if not isinstance(node, Tag):
            return ''

        name = node.name.lower()
        classes = node.get('class') or []

        if name == 'inline-formula':
            tex = cls._tex_of(node)
            return f"${tex}$" if tex else ''
        if name == 'disp-formula':
            tex = cls._tex_of(node)
            return f"$${tex}$$" if tex else ''
        if name in ('script', 'style', 'button'):
            return ''
        if name == 'span' and 'formula' in classes:
            return ''                                   # "View Source" widget
        if name == 'img':
            return ''                                   # tooltip icons only

        inner = ''.join(cls.inline_md(c) for c in node.children)

        # Emphasis markers must hug the text, but the surrounding whitespace
        # has to survive outside them -- IEEE writes captions as
        # '<b class="title">Fig. 1. </b><fig>...', and swallowing that trailing
        # space glues the caption onto its label ("**Fig. 1.**Basic FHE-...").
        if name in _IEEE_EMPHASIS or name in ('sup', 'sub'):
            stripped = inner.strip()
            if not stripped:
                return inner
            lead = inner[:len(inner) - len(inner.lstrip())]
            trail = inner[len(inner.rstrip()):]
            mark = _IEEE_EMPHASIS.get(name) or ('^' if name == 'sup' else '~')
            return f"{lead}{mark}{stripped}{mark}{trail}"
        if name == 'a':
            href = (node.get('href') or '').strip()
            # Cross-references use href="javascript:void()" and carry their
            # label as text ("[34]"); only real links become markdown links.
            if href and not href.startswith('javascript:'):
                if href.startswith('/'):
                    href = cls.IEEE_BASE + href
                text = inner.strip()
                return f"[{text}]({href})" if text else ''
            return inner

        return inner

    @classmethod
    def _text_md(cls, node) -> str:
        """:meth:`inline_md` with surrounding whitespace collapsed."""
        return re.sub(r'\s+', ' ', cls.inline_md(node)).strip()

    # ==================================================================
    # Body rendering
    # ==================================================================

    @classmethod
    def _figure_urls_of(cls, fig: Tag) -> Tuple[str, str]:
        """Return ``(large_url, small_url)`` for a figure / table block.

        ``div.img-wrap`` wraps the thumbnail in an anchor pointing at the
        ``-large.gif`` rendition; the ``<img src>`` inside is the ``-small``
        one. Prefer large, keep small as the download fallback.
        """
        def _abs(u: str) -> str:
            if not u:
                return ''
            if u.startswith('//'):
                return 'https:' + u
            if u.startswith('/'):
                return cls.IEEE_BASE + u
            return u

        wrap = fig.find('div', class_='img-wrap')
        if wrap is None:
            return '', ''
        large, small = '', ''
        a = wrap.find('a', href=True)
        if a:
            large = a['href'].strip()
        img = wrap.find('img')
        if img and img.get('src'):
            small = img['src'].strip()
        if not large:
            btn = fig.find('button', attrs={'data-src': True})
            if btn:
                large = btn['data-src'].strip()
        return _abs(large), _abs(small)

    @classmethod
    def _caption_md(cls, fig: Tag) -> str:
        """Caption of a figure / table block: ``b.title`` + ``<fig>`` text."""
        cap = fig.find('div', class_='figcaption')
        return cls._text_md(cap) if cap is not None else ''

    @classmethod
    def _render_list(cls, node: Tag, depth: int = 0) -> List[str]:
        """Render ``<ul>`` / ``<ol>``, recursing into nested lists.

        IEEE nests enumerations inside list items (e.g. the "Key generation.
        The algorithm takes security parameter ..." block), so child lists are
        indented two spaces per level rather than flattened into the parent.
        """
        lines: List[str] = []
        ordered = node.name.lower() == 'ol'
        indent = '  ' * depth
        counter = 0
        for li in node.find_all('li', recursive=False):
            counter += 1
            bullet = f"{counter}." if ordered else '-'
            own_parts: List[str] = []
            sub_lines: List[str] = []
            for child in li.children:
                child_name = child.name.lower() if isinstance(child, Tag) else ''
                if child_name in ('ul', 'ol'):
                    sub_lines.extend(cls._render_list(child, depth + 1))
                elif child_name == 'disp-formula':
                    tex = cls._tex_of(child)
                    if tex:
                        sub_lines.append(f"{indent}  $${tex}$$")
                else:
                    own_parts.append(cls.inline_md(child))
            text = re.sub(r'\s+', ' ', ''.join(own_parts)).strip()
            if text:
                lines.append(f"{indent}{bullet} {text}")
            elif sub_lines:
                lines.append(f"{indent}{bullet}")
            lines.extend(sub_lines)
        return lines

    @classmethod
    def _render_algorithm(cls, node: Tag) -> List[str]:
        """Render ``div.algorithm`` as a titled list of numbered steps.

        Each ``div.alg-item`` is one step, prefixed by a ``span.label`` line
        number. The label is kept verbatim in backticks so the pseudo-code
        line numbering survives into the markdown.
        """
        out: List[str] = []
        head = node.find(['h3', 'h4'])
        if head is not None:
            title = cls._text_md(head)
            if title:
                out.extend([f"**{title}**", ''])
        for item in node.find_all('div', class_='alg-item'):
            label_el = item.find('span', class_='label')
            label = cls._text_md(label_el) if label_el is not None else ''
            if label_el is not None:
                label_el.extract()
            body = cls._text_md(item)
            if not body and not label:
                continue
            out.append(f"- `{label}` {body}".rstrip() if label else f"- {body}")
        if out:
            out.append('')
        return out

    @classmethod
    def _render_figure(cls, node: Tag, ctx: dict) -> List[str]:
        """Emit a figure / table block and register its download URLs.

        Figures and tables share one numbering sequence because they share
        one download namespace downstream: ``figure_urls`` keys must end in
        the number the downloader uses to name the local file. The image link
        is written as a ``__IEEE_FIG_n__`` placeholder that
        :meth:`convert_to_markdown` swaps for the saved filename.
        """
        large, small = cls._figure_urls_of(node)
        caption = cls._caption_md(node)
        if not large and not small:
            return [caption, ''] if caption else []

        ctx['fig_seq'] += 1
        index = ctx['fig_seq']
        ctx['figure_urls'][f'fig_{index}'] = {
            'url': large or small,
            'original_url': small or large,
            'caption': caption,
        }

        # The caption already opens with its own bold label
        # ("**Fig. 1.** Basic FHE-based ..."), so it is emitted verbatim --
        # wrapping it again would nest ** inside **. The alt text drops the
        # markup, since markdown emphasis is not rendered inside alt.
        alt = re.sub(r'[*_`]', '', caption).strip() or f"Figure {index}"
        out: List[str] = []
        if caption:
            out.extend([caption, ''])
        out.extend([f"![{alt}](__IEEE_FIG_{index}__)", ''])
        return out

    @classmethod
    def _is_block(cls, node) -> bool:
        """True if *node* must become its own markdown block, not inline text."""
        if not isinstance(node, Tag):
            return False
        name = node.name.lower()
        if name in ('ul', 'ol', 'disp-formula', 'table'):
            return True
        classes = node.get('class') or []
        return name == 'div' and ('figure' in classes or 'algorithm' in classes)

    @classmethod
    def _render_paragraph(cls, node: Tag, ctx: dict, level: int) -> List[str]:
        """Render a ``<p>``, splitting out any block-level children.

        IEEE nests whole lists inside paragraphs -- the HE-scheme definition
        ("Key generation. The algorithm takes security parameter ...") is a
        four-item ``<ul>`` sitting inside ``<p class="has-inline-formula">``.
        Running such a paragraph through the inline pipeline alone would
        concatenate every bullet into one run-on sentence, so the paragraph is
        cut at each block child: leading prose, then the block, then whatever
        prose follows.
        """
        out: List[str] = []
        buffer: List[str] = []

        def _flush() -> None:
            text = re.sub(r'\s+', ' ', ''.join(buffer)).strip()
            buffer.clear()
            if text:
                out.extend([text, ''])

        for child in node.children:
            if cls._is_block(child):
                _flush()
                out.extend(cls._render_node(child, ctx, level))
            else:
                buffer.append(cls.inline_md(child))
        _flush()
        return out

    @classmethod
    def _render_node(cls, node, ctx: dict, level: int) -> List[str]:
        """Render one body-level node to a list of markdown blocks."""
        if isinstance(node, NavigableString):
            text = re.sub(r'\s+', ' ', str(node)).strip()
            return [text, ''] if text else []
        if not isinstance(node, Tag):
            return []

        name = node.name.lower()
        classes = node.get('class') or []
        out: List[str] = []

        if name in ('script', 'style', 'button'):
            return []

        # Section wrappers -- recurse into children at the same heading level
        # (the nesting depth is carried by the heading tag itself, h2/h3/h4).
        if name == 'div' and ('section' in classes or 'section_2' in classes
                              or node.get('id') == 'article'):
            for child in node.children:
                out.extend(cls._render_node(child, ctx, level))
            return out

        # Section header block: <div class="kicker">SECTION I.</div><h2>...</h2>
        # The kicker is dropped -- it duplicates the heading's own numbering.
        if name == 'div' and 'header' in classes:
            head = node.find(['h2', 'h3', 'h4', 'h5'])
            return cls._render_node(head, ctx, level) if head is not None else []

        if name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            text = cls._text_md(node)
            if not text:
                return []
            # A "statement" heading (Definition / Theorem / Proof) is run-in
            # prose, not a section -- bold it instead of making it a heading.
            if 'statement' in classes:
                return [f"**{text}**", '']
            depth = {'h1': 0, 'h2': 0, 'h3': 1, 'h4': 2, 'h5': 3, 'h6': 4}[name]
            return ['#' * (level + depth) + f" {text}", '']

        if name == 'p':
            return cls._render_paragraph(node, ctx, level)

        if name in ('ul', 'ol'):
            lines = cls._render_list(node)
            return lines + [''] if lines else []

        if name == 'disp-formula':
            tex = cls._tex_of(node)
            return [f"$${tex}$$", ''] if tex else []

        if name == 'div' and 'algorithm' in classes:
            return cls._render_algorithm(node)

        if name == 'div' and 'figure' in classes:
            return cls._render_figure(node, ctx)

        if name in ('div', 'span', 'section', 'fig'):
            for child in node.children:
                out.extend(cls._render_node(child, ctx, level))
            return out

        text = cls._text_md(node)
        return [text, ''] if text else []

    @classmethod
    def render_body_html(cls, body_html: str, base_level: int = 2) -> dict:
        """Render the body REST payload (XHTML) to markdown.

        Returns ``{'body_md': str, 'figure_urls': dict}``.
        """
        if not body_html:
            return {'body_md': '', 'figure_urls': {}}

        # The payload opens with an XML declaration; strip it so html.parser
        # is happy, which avoids a hard lxml dependency.
        cleaned = re.sub(r'^\s*<\?xml[^>]*\?>\s*', '', body_html)
        soup = BeautifulSoup(cleaned, 'html.parser')

        for selector in _IEEE_DROP_SELECTORS:
            for el in soup.select(selector):
                el.decompose()

        root = (soup.find('div', id='article')
                or soup.find('div', id='BodyWrapper')
                or soup)
        ctx: dict = {'fig_seq': 0, 'figure_urls': {}}

        blocks: List[str] = []
        for child in root.children:
            blocks.extend(cls._render_node(child, ctx, base_level))

        md = re.sub(r'\n{3,}', '\n\n', '\n'.join(blocks)).strip()
        return {'body_md': md, 'figure_urls': ctx['figure_urls']}

    # ==================================================================
    # References
    # ==================================================================

    @classmethod
    def format_references(cls, payload: dict) -> List[str]:
        """Format the references endpoint into ``[N] text`` markdown entries.

        Each entry carries ``order``, ``text`` (HTML with ``<em>`` markup),
        and optionally ``title`` and ``crossRefLink``. The per-reference
        ``context`` array -- every spot in the body where the work is cited --
        is dropped: it triples the size of the section without telling a
        reader of the markdown anything the inline ``[N]`` markers don't.
        """
        refs = (payload or {}).get('references') or []
        out: List[str] = []
        for idx, ref in enumerate(refs, 1):
            raw = (ref.get('text') or '').strip() or (ref.get('title') or '').strip()
            if not raw:
                continue
            text = cls._text_md(BeautifulSoup(cls._fix_mojibake(raw), 'html.parser'))
            order = (ref.get('order') or str(idx)).strip()
            line = f"[{order}] {text}"
            link = (ref.get('crossRefLink') or '').strip()
            if link:
                line += f"\n\nCrossref: [{link}]({link})"
            out.append(line)
        return out

    # ==================================================================
    # Multimedia (supplemental)
    # ==================================================================

    @classmethod
    def parse_multimedia(cls, payload: dict) -> Tuple[List[str], Dict[str, str]]:
        """Return ``(urls, descriptions)`` from the multimedia endpoint.

        ``filePath`` is site-relative (``/ielx7/.../supp1-3353536.pdf``). The
        description keeps the supplement's own ``doi`` so the markdown records
        which DOI the file is registered under.
        """
        items = (payload or {}).get('multimedia') or []
        urls: List[str] = []
        descriptions: Dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            path = (item.get('filePath') or '').strip()
            if not path:
                continue
            url = path if path.startswith('http') else cls.IEEE_BASE + path
            parts: List[str] = []
            desc = (item.get('description') or '').strip()
            if desc:
                parts.append(cls._fix_mojibake(desc))
            doi = (item.get('doi') or '').strip()
            if doi:
                parts.append(f"DOI: {doi}")
            urls.append(url)
            descriptions[url] = ' — '.join(parts) if parts else (
                item.get('fileName') or url
            )
        return urls, descriptions

    @classmethod
    def supplement_group_urls(cls, xpl: dict) -> Tuple[List[str], Dict[str, str]]:
        """Supplemental files listed in the landing-page metadata blob.

        Some articles carry a ``supplementGroup`` in ``xplGlobal`` instead of
        (or as well as) an entry in the multimedia endpoint.
        """
        group = (xpl or {}).get('supplementGroup') or {}
        entries = group.get('supplement') if isinstance(group, dict) else group
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return [], {}

        urls: List[str] = []
        descriptions: Dict[str, str] = {}
        for item in entries:
            if not isinstance(item, dict):
                continue
            path = (item.get('filePath') or item.get('url') or '').strip()
            if not path:
                continue
            url = path if path.startswith('http') else cls.IEEE_BASE + path
            desc = (item.get('description') or item.get('title')
                    or item.get('fileName') or '').strip()
            urls.append(url)
            descriptions[url] = cls._fix_mojibake(desc) if desc else url
        return urls, descriptions

    # ==================================================================
    # Footnotes
    # ==================================================================

    @classmethod
    def render_footnotes(cls, payload: dict) -> str:
        """Render the footnotes endpoint as pandoc footnote definitions.

        Footnote bodies carry the same ``<inline-formula>`` /
        ``<disp-formula>`` markup as the body, so they go through the same
        pipeline. Display math inside a footnote becomes its own block, and
        every continuation line is indented four spaces so it stays part of
        the definition rather than terminating it.
        """
        notes = (payload or {}).get('footnote') or []
        if isinstance(notes, dict):
            notes = [notes]
        blocks: List[str] = []
        for idx, note in enumerate(notes, 1):
            if not isinstance(note, dict):
                continue
            raw = (note.get('text') or '').strip()
            if not raw:
                continue
            soup = BeautifulSoup(raw, 'html.parser')
            label = (note.get('label') or str(idx)).strip()

            parts: List[str] = []
            for child in soup.children:
                if isinstance(child, Tag) and child.name.lower() == 'disp-formula':
                    tex = cls._tex_of(child)
                    if tex:
                        parts.append(f"\n\n$${tex}$$\n\n")
                else:
                    parts.append(cls.inline_md(child))
            text = ''.join(parts)
            text = re.sub(r'[ \t]+', ' ', text)
            text = re.sub(r'\n{3,}', '\n\n', text).strip()
            text = text.replace('\n\n', '\n\n    ')
            blocks.append(f"[^{label}]: {text}")
        return '\n\n'.join(blocks)

    # ==================================================================
    # Metadata
    # ==================================================================

    async def extract_metadata(self, page) -> dict:
        try:
            html = await page.content()
        except Exception:
            html = ''

        xpl = self.extract_xpl_metadata(html)
        self.resolve_article_id(getattr(page, 'url', '') or '', html)

        authors: List[str] = []
        detailed: List[dict] = []
        for entry in xpl.get('authors') or []:
            name = (entry.get('name') or '').strip()
            if not name:
                continue
            affs = [a.strip() for a in (entry.get('affiliation') or []) if a and a.strip()]
            authors.append(name)
            detailed.append({'author': name, 'affiliations': affs, 'emails': []})

        # Keywords come in typed groups ("IEEE Keywords", "Index Terms", ...).
        keyword_groups: List[Tuple[str, List[str]]] = []
        for group in xpl.get('keywords') or []:
            kwds = [k.strip() for k in (group.get('kwd') or []) if k and k.strip()]
            if kwds:
                keyword_groups.append(((group.get('type') or 'Keywords').strip(), kwds))

        issn = ''
        for entry in xpl.get('issn') or []:
            if 'electronic' in (entry.get('format') or '').lower():
                issn = (entry.get('value') or '').strip()
                break
        if not issn and xpl.get('issn'):
            issn = (xpl['issn'][0].get('value') or '').strip()

        start = (xpl.get('startPage') or '').strip()
        end = (xpl.get('endPage') or '').strip()
        pages = f"{start}-{end}" if start and end else start

        return {
            'title': self._fix_mojibake((xpl.get('title') or '').strip()),
            'doi': (xpl.get('doi') or self.doi or '').strip(),
            'authors': authors,
            'author_with_affiliations': detailed,
            'year': (xpl.get('publicationYear') or '').strip(),
            'journal': (xpl.get('publicationTitle') or '').strip(),
            'volume': (xpl.get('volume') or '').strip(),
            'issue': (xpl.get('issue') or '').strip(),
            'pages': pages,
            'issn': issn,
            'publisher': (xpl.get('publisher') or 'IEEE').strip(),
            'abstract': self._fix_mojibake((xpl.get('abstract') or '').strip()),
            'corresponding_author_emails': [],
            '_keyword_groups': keyword_groups,
            '_xpl': xpl,
        }

    # ==================================================================
    # Links
    # ==================================================================

    async def get_pdf_url(self, doi: str = None) -> Optional[str]:
        """PDF link, preferring the metadata blob over the constructed one.

        ``pdfPath`` (``/iel7/.../10398424.pdf``) is the file itself and
        ``pdfUrl`` (``/stamp/stamp.jsp?tp=&arnumber=...``) the viewer around
        it; both are site-relative. ``pdfPath`` is returned first -- see
        :meth:`download_pdf_via_page` for why neither URL can be downloaded by
        navigating to it.

        With neither field present the stamp URL is constructed from the
        article id, since that is the only form derivable without the
        metadata blob.
        """
        xpl = self._xpl_cache or {}
        for key in ('pdfPath', 'pdfUrl'):
            path = (xpl.get(key) or '').strip()
            if path:
                return path if path.startswith('http') else self.IEEE_BASE + path
        if self.article_id:
            return f"{self.IEEE_BASE}/stamp/stamp.jsp?tp=&arnumber={self.article_id}"
        return None

    async def download_pdf_via_page(self, page, output_dir, filename: str = 'paper.pdf'):
        """Fetch the article PDF with an in-page ``fetch()`` and write it out.

        Navigating to an IEEE PDF URL never produces a file: the ``iel7`` path
        redirects to ``stamp.jsp``, and the stamp page renders the PDF in an
        embedded viewer that waits for a human to click "open". Either way the
        browser's download event never fires and the generic downloader
        exhausts its retry budget.

        Fetching the bytes from inside the page sidesteps the viewer entirely
        -- same session, same cookies, same fingerprint as the rendered
        article, so entitlement is honoured -- and writes the file directly.
        The payload comes back base64-encoded because ``page.evaluate`` can
        only return JSON-serialisable values.

        Returns the filename on success, or ``None`` so the caller can fall
        back to the normal navigation-based download.
        """
        from pathlib import Path as _Path

        print("  📥 IEEE: 页面内 fetch PDF...")

        # /stampPDF/getPDF.jsp is the endpoint the stamp viewer itself calls
        # for the bytes, so it answers with the file directly -- unlike
        # /stamp/stamp.jsp (the viewer chrome, which waits for a human to
        # click "open") and unlike pdfPath's /iel7/... (which redirects to
        # that viewer). Try it first, then fall back to the metadata blob's
        # URLs, following any viewer page's embedded <iframe src>.
        candidates: List[str] = []
        if self.article_id:
            candidates.append(
                f"{self.IEEE_BASE}/stampPDF/getPDF.jsp?tp=&arnumber={self.article_id}"
            )
        primary = await self.get_pdf_url(self.doi)
        if primary and primary not in candidates:
            candidates.append(primary)
        if self.article_id:
            stamp = f"{self.IEEE_BASE}/stamp/stamp.jsp?tp=&arnumber={self.article_id}"
            if stamp not in candidates:
                candidates.append(stamp)

        seen = set()
        for _ in range(4):                      # getPDF -> pdfPath -> viewer -> iframe
            if not candidates:
                break
            url = candidates.pop(0)
            if url in seen:
                continue
            seen.add(url)

            print(f"     链接: {url}")
            payload = await self._fetch_bytes(page, url)
            if payload is None:
                continue
            data, content_type = payload

            if data.startswith(b'%PDF'):
                out = _Path(output_dir) / filename
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
                print(f"    ✓ 已保存: {filename} ({len(data) / 1024 / 1024:.2f} MB)")
                return filename

            # Not the file -- most likely the stamp viewer, which embeds the
            # real PDF in an <iframe>/<embed> whose src carries the session
            # token (an /ielx7/... path, note the "x", unlike pdfPath's
            # /iel7/...). Follow that and try again.
            embedded = self._embedded_pdf_src(data)
            if embedded:
                candidates.append(embedded)
                continue

            head = data[:16].decode('latin-1', 'replace')
            print(f"    ⚠️  响应不是 PDF (content-type={content_type or '?'}, "
                  f"起始={head!r})")

        return None

    async def _fetch_bytes(self, page, url: str):
        """In-page ``fetch()`` returning ``(bytes, content_type)`` or ``None``.

        ``page.evaluate`` can only hand back JSON, so the body is base64'd on
        the JS side and decoded here.
        """
        import base64

        try:
            result = await page.evaluate(
                """async (url) => {
                    try {
                        const r = await fetch(url, {
                            credentials: 'include',
                            headers: {'Accept': 'application/pdf,*/*'},
                        });
                        if (!r.ok) return {__err: 'status ' + r.status};
                        const buf = new Uint8Array(await r.arrayBuffer());
                        let s = '';
                        const CHUNK = 0x8000;
                        for (let i = 0; i < buf.length; i += CHUNK) {
                            s += String.fromCharCode.apply(
                                null, buf.subarray(i, i + CHUNK));
                        }
                        return {b64: btoa(s), type: r.headers.get('content-type') || ''};
                    } catch (e) {
                        return {__err: String(e)};
                    }
                }""",
                url,
            )
        except Exception as exc:
            print(f"    ⚠️  in-page fetch 异常: {type(exc).__name__}: {str(exc)[:120]}")
            return None

        if not isinstance(result, dict) or result.get('__err'):
            print(f"    ⚠️  请求失败: {(result or {}).get('__err', 'no response')}")
            return None
        try:
            return base64.b64decode(result.get('b64') or ''), (result.get('type') or '')
        except Exception:
            return None

    @classmethod
    def _embedded_pdf_src(cls, data: bytes) -> str:
        """Find the PDF that a viewer page embeds, if this is a viewer page."""
        try:
            html = data.decode('utf-8', errors='replace')
        except Exception:
            return ''
        m = re.search(
            r'<(?:iframe|embed)[^>]+src=["\']([^"\']*\.pdf[^"\']*)["\']',
            html, re.IGNORECASE)
        if not m:
            m = re.search(r'["\'](/ielx?\d*/[^"\']+\.pdf[^"\']*)["\']', html)
        if not m:
            return ''
        src = m.group(1).replace('&amp;', '&').strip()
        if src.startswith('//'):
            return 'https:' + src
        if src.startswith('/'):
            return cls.IEEE_BASE + src
        return src

    async def get_supplemental_url(self, doi: str) -> Optional[str]:
        # Supplemental files come from the multimedia endpoint in extract_all.
        return None

    async def extract_references(self, html: str) -> list:
        # References come from the REST endpoint in extract_all, not the DOM.
        return []

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def get_fulltext_url(self, page) -> str:
        if self.article_id:
            return f"{self.IEEE_BASE}/document/{self.article_id}/"
        try:
            return page.url or ''
        except Exception:
            return ''

    # ==================================================================
    # Orchestration
    # ==================================================================

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'IEEEHandler'
        )
        doi = self.doi
        set_actual_base_url(self, page)

        try:
            metadata = await self.extract_metadata(page)
            metadata['doi'] = doi or metadata.get('doi', '')
            self._xpl_cache = metadata.pop('_xpl', {})

            if not self.article_id:
                print("  ⚠️  未能解析 articleId — REST 接口无法访问")
                return {
                    'metadata': metadata,
                    'links': {
                        'pdf_url': await self.get_pdf_url(doi),
                        'figure_urls': {},
                        'supplemental_urls': [],
                        'supplemental_descriptions': {},
                    },
                    'fulltext_data': '',
                    'journal_name': 'ieee',
                }

            print(f"  ✓ articleId = {self.article_id}")

            # -- Body -------------------------------------------------------
            body_html = await self._fetch_rest(
                page, self._rest_url(self.REST_BODY), self.CACHE_NAMES['body']
            )
            rendered = self.render_body_html(body_html)
            figure_urls = rendered['figure_urls']
            if rendered['body_md']:
                metadata['_body_md'] = rendered['body_md']
                print(f"  ✓ 正文: {len(rendered['body_md']):,} 字符, "
                      f"{len(figure_urls)} 图/表")

            # -- References -------------------------------------------------
            ref_payload = await self._fetch_rest_json(
                page, self._rest_url(self.REST_REFERENCES),
                self.CACHE_NAMES['references'],
            )
            references = self.format_references(ref_payload)
            metadata['references'] = references
            if references:
                print(f"  ✓ 参考文献: {len(references)} 条")

            # -- Supplemental -----------------------------------------------
            mm_payload = await self._fetch_rest_json(
                page, self._rest_url(self.REST_MULTIMEDIA),
                self.CACHE_NAMES['multimedia'],
            )
            supp_urls, supp_descriptions = self.parse_multimedia(mm_payload)
            group_urls, group_desc = self.supplement_group_urls(self._xpl_cache)
            for url in group_urls:
                if url not in supp_urls:
                    supp_urls.append(url)
                    supp_descriptions[url] = group_desc[url]
            if supp_urls:
                print(f"  ✓ 补充材料: {len(supp_urls)} 个")

            # -- Footnotes --------------------------------------------------
            fn_payload = await self._fetch_rest_json(
                page, self._rest_url(self.REST_FOOTNOTES),
                self.CACHE_NAMES['footnotes'],
            )
            footnotes_md = self.render_footnotes(fn_payload)
            if footnotes_md:
                metadata['_footnotes_md'] = footnotes_md
                print(f"  ✓ 脚注: {footnotes_md.count('[^')} 条")

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': await self.get_pdf_url(doi),
                    'figure_urls': figure_urls,
                    'supplemental_urls': supp_urls,
                    'supplemental_descriptions': supp_descriptions,
                },
                'fulltext_data': body_html,
                'journal_name': 'ieee',
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
        md: List[str] = []
        title = (metadata.get('title') or '').strip() or 'IEEE Article'
        md.extend([f"# {title}", ''])

        detailed = metadata.get('author_with_affiliations') or []
        if detailed:
            md.extend(['## Authors', ''])
            for entry in detailed:
                md.append(f"- **{entry.get('author', '')}**")
                for aff in entry.get('affiliations') or []:
                    md.append(f"  - {aff}")
            md.append('')
        elif metadata.get('authors'):
            md.extend(['## Authors', '', ', '.join(metadata['authors']), ''])

        md.extend(['## Publication', ''])
        for key, label in (('journal', '**Journal:**'), ('volume', '**Volume:**'),
                           ('issue', '**Issue:**'), ('pages', '**Pages:**'),
                           ('year', '**Year:**'), ('doi', '**DOI:**')):
            val = (metadata.get(key) or '').strip()
            if val:
                md.extend([f"{label} {val}", ''])

        # Abstract + keywords. The keywords live inside the abstract section
        # so a chunker that keeps the abstract also keeps the article's
        # topical vocabulary.
        abstract = (metadata.get('abstract') or '').strip()
        md.extend(['---', '', '## Abstract', '',
                   abstract or '[No abstract available.]', ''])
        for group_type, kwds in metadata.get('_keyword_groups') or []:
            md.extend([f"**{group_type}:** " + ', '.join(kwds), ''])

        # Body -- figure placeholders resolved to the downloaded filenames.
        body_md = (metadata.get('_body_md') or '').strip()
        if not body_md and isinstance(article_text, str) and article_text.strip():
            body_md = self.render_body_html(article_text)['body_md']
        if body_md:
            md.extend(['---', '', self._resolve_figure_placeholders(body_md, kwargs), ''])

        footnotes_md = (metadata.get('_footnotes_md') or '').strip()
        if footnotes_md:
            md.extend(['---', '', '## Footnotes', '', footnotes_md, ''])

        supp_downloads = kwargs.get('supplemental_downloads') or []
        supp_urls = kwargs.get('supplemental_urls') or []
        supp_desc = kwargs.get('supplemental_descriptions') or {}
        if supp_downloads or supp_urls:
            md.extend(['---', '', '## Supplementary Material', ''])
            if supp_downloads:
                for local, url in zip(supp_downloads, supp_urls):
                    desc = supp_desc.get(url, '')
                    md.append(f"- `{local}`" + (f" — {desc}" if desc else ''))
            else:
                for url in supp_urls:
                    md.append(f"- [{supp_desc.get(url, url)}]({url})")
            md.append('')

        references = metadata.get('references') or []
        if references:
            md.extend(['---', '', '## References', ''])
            for entry in references:
                md.extend([entry, ''])

        return '\n'.join(md).rstrip() + '\n'

    @staticmethod
    def _resolve_figure_placeholders(body_md: str, kwargs: dict) -> str:
        """Replace ``__IEEE_FIG_n__`` with the local filename of figure *n*.

        Falls back to the remote URL when a figure failed to download, so a
        broken download degrades to a working remote link rather than leaving
        a raw placeholder in the markdown.
        """
        filenames = kwargs.get('figure_filenames') or {}
        figure_urls = kwargs.get('figure_urls') or {}

        def _sub(match: "re.Match") -> str:
            index = match.group(1)
            local = filenames.get(index) or filenames.get(int(index))
            if local:
                return str(local)
            info = figure_urls.get(f'fig_{index}') or {}
            return info.get('url', '') if isinstance(info, dict) else str(info)

        return re.sub(r'__IEEE_FIG_(\d+)__', _sub, body_md)

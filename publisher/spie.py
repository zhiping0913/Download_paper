"""SPIE Digital Library handler (spiedigitallibrary.org, DOI prefix 10.1117 / 10.3788).

The landing page carries only metadata; the article body comes from a POST
API:

    POST https://www.spiedigitallibrary.org/api/journals/article/fulltexthtml
    {"urlId": "<doi>"}
    Referer: <citation_fulltext_html_url>

    -> {"hasAccess": ..., "data": {"urlId": ..., "fullTextHtml": "<html>"}}

which returns the whole article as HTML with MathML intact -- no MathJax
rendering to work around. The response is cached as ``fulltexthtml.json``
beside ``page.html``.

Landing-page markup used:

    <meta name="citation_pdf_url">              PDF
    <meta name="citation_fulltext_html_url">    the Referer the API requires
    <script type="application/ld+json">         authors, abstract, journal

Body markup, all inside ``fullTextHtml``:

    div.section                 sections; the number and the title are two
                                separate headings (``<h2 class="label">2.1.</h2>``
                                followed by ``<h3>Optical Force…</h3>``)
    div.disp-formula.panel      display equation + its ``Eq. (N)`` label
    span.inline-formula         inline math
    div.fig.panel               figure: hi-res link + low-res <img>
    div.article-table           table, with div.table-footnotes beneath
    div.ref-content             references

No supplemental: no SPIE article seen so far ships any, so nothing is
extracted for it rather than guessing at markup that may not exist.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from html_to_md_converter import mathml_to_latex_pandoc
from publisher.base import PublisherHandler
from publisher.wildcard import init_extract_all_page, set_actual_base_url


_SPIE_DROP_SELECTORS = (
    'span.lookupLink',          # "Google Scholar" / "Crossref" per reference
    'script',
    'style',
    'noscript',
)

_SPIE_NOISE_LINES = frozenset({'google scholar', 'crossref', 'pubmed'})


class SPIEHandler(PublisherHandler):
    """Full-text handler for the SPIE Digital Library."""

    SPIE_BASE = 'https://www.spiedigitallibrary.org'
    FULLTEXT_API = f'{SPIE_BASE}/api/journals/article/fulltexthtml'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.SPIE_BASE

    # ==================================================================
    # Math -- one pipeline for every element
    # ==================================================================

    @staticmethod
    def _strip_delims(latex: str) -> str:
        latex = (latex or '').strip()
        latex = re.sub(r'^\\[\(\[]|\\[\)\]]$', '', latex).strip()
        if latex.startswith('$$') and latex.endswith('$$') and len(latex) > 4:
            latex = latex[2:-2].strip()
        elif latex.startswith('$') and latex.endswith('$') and len(latex) > 2:
            latex = latex[1:-1].strip()
        return latex

    @classmethod
    def _math_latex(cls, node: Tag) -> str:
        """LaTeX for one ``<math>`` element (or the first one inside *node*)."""
        math = node if node.name == 'math' else node.find('math')
        if math is None:
            return ''
        annotation = math.find('annotation')
        if annotation is not None:
            latex = cls._strip_delims(annotation.get_text())
            if latex:
                return latex
        try:
            return cls._strip_delims(mathml_to_latex_pandoc(str(math)) or '')
        except Exception:
            return ''

    @classmethod
    def _inline_md(cls, node) -> str:
        """Render an inline subtree to markdown, formulas included.

        Paragraphs, captions, list items, headings, table cells and reference
        text all go through here, so math cannot be lost to a bare
        ``get_text()`` on any of them.
        """
        if node is None:
            return ''
        if isinstance(node, Comment):
            # SPIE marks anchors with <a id="g001"><!-- named anchor --></a>;
            # a Comment is a NavigableString subclass, so without this the
            # words "named anchor" land in the prose.
            return ''
        if isinstance(node, NavigableString):
            return re.sub(r'\s+', ' ', str(node))
        if not isinstance(node, Tag) or not node.name:
            return ''

        name = node.name.lower()
        classes = node.get('class') or []

        if name in ('script', 'style', 'noscript'):
            return ''
        if 'lookupLink' in classes:
            return ''
        if name == 'math':
            latex = cls._math_latex(node)
            return f"${latex}$" if latex else ''
        if 'inline-formula' in classes:
            latex = cls._math_latex(node)
            return f"${latex}$" if latex else ''

        inner = ''.join(cls._inline_md(c) for c in node.children)

        if name in ('b', 'strong'):
            return cls._wrap(inner, '**')
        if name in ('i', 'em'):
            return cls._wrap(inner, '*')
        if name == 'sup':
            return cls._wrap(inner, '^')
        if name == 'sub':
            return cls._wrap(inner, '~')
        if name == 'a':
            href = (node.get('href') or '').strip()
            text = inner.strip()
            # Internal cross-references point at anchors in a page we are not
            # shipping.
            if (not href or href.startswith('#') or href.startswith('javascript:')
                    or 'internal-link' in classes):
                return inner
            resolved = urljoin(cls.SPIE_BASE, href)
            if not urlparse(resolved).netloc:
                return inner
            return f"[{text}]({cls._md_url(resolved)})" if text else ''

        return inner

    @staticmethod
    def _md_url(url: str) -> str:
        """Percent-encode the parentheses that would end a markdown target."""
        return (url or '').strip().replace('(', '%28').replace(')', '%29')

    @staticmethod
    def _wrap(inner: str, mark: str) -> str:
        stripped = inner.strip()
        if not stripped:
            return inner
        # SPIE nests identical emphasis (<b><b>Mechanism</b></b>); wrapping
        # again would yield ****Mechanism****, which renders as literal
        # asterisks rather than bold.
        if stripped.startswith(mark) and stripped.endswith(mark):
            return inner
        lead = inner[:len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()):]
        return f"{lead}{mark}{stripped}{mark}{trail}"

    @classmethod
    def _text_md(cls, node) -> str:
        text = re.sub(r'\s+', ' ', cls._inline_md(node)).strip()
        # SPIE nests emphasis in shapes the per-element guard cannot catch,
        # e.g. <b><b>Intuitive expression</b><a><i>a</i></a></b> ends up with
        # four leading asterisks. A run of four or more is never valid
        # markdown -- it renders as literal asterisks -- so collapse it.
        # Three is left alone: that is bold-italic.
        return re.sub(r'\*{4,}', '**', text)

    @staticmethod
    def _is_noise(text: str) -> bool:
        return text.strip().lower().rstrip('.') in _SPIE_NOISE_LINES

    # ==================================================================
    # Landing-page metadata
    # ==================================================================

    @staticmethod
    def _meta(soup: BeautifulSoup, name: str) -> str:
        tag = soup.find('meta', attrs={'name': name})
        return (tag.get('content') or '').strip() if tag else ''

    @classmethod
    def _ld_json(cls, soup: BeautifulSoup) -> dict:
        """The ScholarlyArticle block, which carries authors and abstract."""
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string or '{}')
            except (ValueError, TypeError):
                continue
            if isinstance(data, list):
                data = next((d for d in data
                             if isinstance(d, dict)
                             and 'Article' in str(d.get('@type', ''))), {})
            if isinstance(data, dict) and 'Article' in str(data.get('@type', '')):
                return data
        return {}

    @classmethod
    def extract_metadata_from_html(cls, html: str) -> dict:
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')
        ld = cls._ld_json(soup)

        authors = []
        for entry in ld.get('author') or []:
            if isinstance(entry, dict):
                name = (entry.get('name') or '').strip()
            else:
                name = str(entry).strip()
            if name:
                authors.append(name)

        part = ld.get('isPartOf') or {}
        journal = cls._meta(soup, 'citation_journal_title')
        if not journal and isinstance(part, dict):
            journal = (part.get('name') or '').strip()

        date = (ld.get('datePublished') or ''
                or cls._meta(soup, 'citation_publication_date'))
        m = re.search(r'(19|20|21)\d{2}', date)

        first = cls._meta(soup, 'citation_firstpage') or str(ld.get('pageStart') or '')
        last = cls._meta(soup, 'citation_lastpage')
        pages = f"{first}-{last}" if first and last else first

        abstract = (ld.get('abstract') or ld.get('description') or '').strip()

        return {
            'title': (cls._meta(soup, 'citation_title')
                      or (ld.get('name') or '')).strip(),
            'doi': cls._meta(soup, 'citation_doi'),
            'authors': authors,
            'journal': journal,
            'volume': cls._meta(soup, 'citation_volume'),
            'issue': cls._meta(soup, 'citation_issue'),
            'pages': pages,
            'year': m.group(0) if m else '',
            'publication_date': date,
            'issn': cls._meta(soup, 'citation_issn'),
            'publisher': 'SPIE',
            'abstract': abstract,
            'corresponding_author_emails': [],
            '_pdf_url': cls._meta(soup, 'citation_pdf_url'),
            '_fulltext_referer': cls._meta(soup, 'citation_fulltext_html_url'),
        }

    # ==================================================================
    # Full-text API
    # ==================================================================

    async def fetch_fulltext_html(self, page, referer: str = '') -> str:
        """POST the fulltext API from inside the page and return the HTML.

        The call is made with the page's own ``fetch()`` rather than an
        out-of-page request: it inherits the session and the exact origin that
        just rendered the article, which is what the endpoint's Referer check
        and any entitlement cookie expect.

        The whole payload is cached as ``fulltexthtml.json`` so a capture can
        be re-rendered offline.
        """
        doi = (self.doi or '').strip()
        if not doi:
            return ''
        referer = referer or f"{self.SPIE_BASE}/journals"

        print(f"  ↪ 请求正文 API: /api/journals/article/fulltexthtml ({doi})")
        try:
            payload = await page.evaluate(
                """async ([api, urlId, referer]) => {
                    try {
                        const r = await fetch(api, {
                            method: 'POST',
                            credentials: 'include',
                            headers: {
                                'Content-Type': 'application/json',
                                'Accept': 'application/json',
                                'referer': referer,
                            },
                            body: JSON.stringify({urlId: urlId}),
                        });
                        if (!r.ok) return {__err: 'status ' + r.status};
                        return await r.json();
                    } catch (e) {
                        return {__err: String(e)};
                    }
                }""",
                [self.FULLTEXT_API, doi, referer],
            )
        except Exception as exc:
            print(f"  ⚠️  正文 API 异常: {type(exc).__name__}: {str(exc)[:120]}")
            return ''

        if not isinstance(payload, dict) or payload.get('__err'):
            print(f"  ⚠️  正文 API 失败: {(payload or {}).get('__err', 'no response')}")
            return ''

        html = ((payload.get('data') or {}).get('fullTextHtml') or '')
        if not html:
            print(f"  ⚠️  正文 API 无 fullTextHtml (hasAccess={payload.get('hasAccess')})")
            return ''

        self._cache_json('fulltexthtml.json', payload)
        return html

    def _cache_json(self, name: str, payload: dict) -> None:
        if not self.captured_data_dir:
            return
        try:
            from pathlib import Path
            out = Path(self.captured_data_dir) / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding='utf-8')
            print(f"  ✓ {name} 已保存 ({out.stat().st_size:,} bytes)")
        except OSError as exc:
            print(f"  ⚠️  {name} 保存失败: {exc}")

    @staticmethod
    def fulltext_html_from_payload(payload: dict) -> str:
        """Pull the body HTML out of a cached API response."""
        if not isinstance(payload, dict):
            return ''
        return ((payload.get('data') or {}).get('fullTextHtml') or '')

    # ==================================================================
    # Figures
    # ==================================================================

    @classmethod
    def extract_figures_from_fulltext(cls, html: str) -> dict:
        """``{'fig_N': {'url', 'original_url', 'caption', 'label'}}``.

        The anchor points at ``FigureImages/`` (full resolution) and the inline
        ``<img>`` at ``WebImages/`` (the screen preview), so the anchor wins
        and the preview is kept as the download fallback.
        """
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')
        figures = {}
        for index, fig in enumerate(soup.select('div.fig'), 1):
            hires, preview = cls._figure_urls(fig)
            if not (hires or preview):
                continue
            label_el = fig.find(class_='label')
            figures[f'fig_{index}'] = {
                'url': hires or preview,
                'original_url': preview or hires,
                'caption': cls._figure_caption(fig),
                'label': (label_el.get_text(' ', strip=True)
                          if label_el is not None else f'Fig. {index}'),
            }
        return figures

    @classmethod
    def _figure_urls(cls, fig: Tag) -> Tuple[str, str]:
        hires = ''
        for a in fig.find_all('a', href=True):
            if 'FigureImages' in a['href']:
                hires = urljoin(cls.SPIE_BASE + '/', a['href'].strip())
                break
        preview = ''
        img = fig.find('img')
        if img is not None:
            src = (img.get('src') or img.get('data-src') or '').strip()
            if src:
                preview = urljoin(cls.SPIE_BASE + '/', src)
        return hires, preview

    @classmethod
    def _figure_caption(cls, fig: Tag) -> str:
        cap = fig.find('div', class_='caption')
        return cls._text_md(cap) if cap is not None else ''

    # ==================================================================
    # References
    # ==================================================================

    @classmethod
    def extract_references_from_fulltext(cls, html: str) -> list:
        """One entry per ``div.ref-content``, keeping any real link."""
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        refs = []
        for entry in soup.select('div.ref-content'):
            clone = BeautifulSoup(str(entry), 'html.parser')
            # The leading number is re-emitted by the caller; the
            # "Google Scholar" widget is UI, not citation text.
            for el in clone.select('p.ref-label, span.label, span.lookupLink'):
                el.decompose()
            text = cls._text_md(clone)
            text = re.sub(r'\s+([,.;:])', r'\1', text)
            text = re.sub(r'\s{2,}', ' ', text).strip(' ,;')
            if text:
                refs.append(text)
        return refs

    # ==================================================================
    # Body
    # ==================================================================

    _BLOCK_TAGS = frozenset({'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                             'ul', 'ol', 'table', 'div'})

    @classmethod
    def _is_block(cls, node) -> bool:
        if not isinstance(node, Tag) or not node.name:
            return False
        classes = node.get('class') or []
        return (node.name.lower() in ('p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                                      'ul', 'ol', 'table')
                or bool({'section', 'disp-formula', 'fig', 'article-table',
                         'table-footnotes'} & set(classes)))

    @classmethod
    def _has_block_descendant(cls, node: Tag) -> bool:
        return any(cls._is_block(d) for d in node.descendants if isinstance(d, Tag))

    @classmethod
    def _render_block(cls, node, level: int, ctx: dict) -> List[str]:
        if isinstance(node, Comment):
            return []
        if isinstance(node, NavigableString):
            text = re.sub(r'\s+', ' ', str(node)).strip()
            return [text, ''] if text and not cls._is_noise(text) else []
        if not isinstance(node, Tag) or not node.name:
            return []

        name = node.name.lower()
        classes = node.get('class') or []

        if name in ('script', 'style', 'noscript'):
            return []
        if 'lookupLink' in classes:
            return []
        if 'ref-content' in classes or 'ref-label' in classes:
            return []                      # emitted from metadata['references']
        if 'disp-formula' in classes:
            return cls._render_equation(node)
        if 'fig' in classes:
            return cls._render_figure(node, ctx)
        if 'article-table' in classes:
            md = cls._render_table(node.find('table') or node)
            return [md, ''] if md else []
        if 'caption' in classes and ctx.get('pending_label'):
            # A float caption, with its label sitting in the preceding
            # <h2 class="label">.
            label = ctx.pop('pending_label', '')
            text = cls._text_md(node)
            return [f"**{label}.** {text}".strip(), ''] if text else []
        if 'table-footnotes' in classes:
            text = cls._text_md(node)
            return [text, ''] if text else []

        if name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            return cls._render_heading(node, level, ctx)
        if name == 'p':
            # SPIE nests floats inside paragraphs: every div.fig,
            # div.disp-formula and div.article-table has a <p> as its parent.
            # Rendering the paragraph inline-only would flatten each of them
            # into running text -- the equations, figure placeholders and
            # table grids all silently disappear.
            if cls._has_block_descendant(node):
                return cls._render_mixed(node, level, ctx)
            text = cls._text_md(node)
            if not text or cls._is_noise(text):
                return []
            # A table's label ("Table 1") is a heading *outside* the table,
            # followed by the caption as a plain paragraph; figures and
            # equations carry theirs inside. Attach it here so the caption is
            # not left anonymous and the label does not leak onto the next
            # section heading.
            label = ctx.pop('pending_label', '')
            if label:
                return [f"**{label}.** {text}", '']
            return [text, '']
        if name in ('ul', 'ol'):
            lines = cls._render_list(node)
            return lines + [''] if lines else []
        if name == 'table':
            md = cls._render_table(node)
            return [md, ''] if md else []

        if cls._has_block_descendant(node):
            return cls._render_mixed(node, level, ctx)

        text = cls._text_md(node)
        return [text, ''] if text and not cls._is_noise(text) else []

    @classmethod
    def _render_mixed(cls, node: Tag, level: int, ctx: dict) -> List[str]:
        """Render a container of mixed inline text and block children.

        Runs of inline children are buffered into one paragraph and flushed at
        each block child, so prose reflows normally while figures, equations
        and tables stand on their own.
        """
        out: List[str] = []
        buffer: List[str] = []

        def _flush() -> None:
            text = re.sub(r'\s+', ' ', ''.join(buffer)).strip()
            buffer.clear()
            if text and not cls._is_noise(text):
                out.extend([text, ''])

        for child in node.children:
            if isinstance(child, Tag) and (cls._is_block(child)
                                           or cls._has_block_descendant(child)):
                _flush()
                out.extend(cls._render_block(child, level, ctx))
            else:
                buffer.append(cls._inline_md(child))
        _flush()
        return out

    @classmethod
    def _render_heading(cls, node: Tag, level: int, ctx: dict) -> List[str]:
        """Render a heading, joining SPIE's split number and title.

        SPIE emits the section number and its title as two separate headings:
        ``<h2 class="label">2.1.</h2>`` then ``<h3>Optical Force…</h3>``.
        Rendering them independently gives a document of empty numbered
        headings interleaved with unnumbered titles, so the label is stashed
        and prefixed onto the next heading. Labels that belong to a float
        (``Fig. 1``, ``Eq. (1)``, ``Table 1``) are handled by those renderers
        and never reach here.
        """
        text = cls._text_md(node)
        if not text:
            return []
        if 'label' in (node.get('class') or []):
            ctx['pending_label'] = text
            return []
        label = ctx.pop('pending_label', '')
        if label:
            text = f"{label} {text}".strip()
        depth = {'h1': 0, 'h2': 0, 'h3': 1, 'h4': 2, 'h5': 3, 'h6': 4}[node.name.lower()]
        return ['#' * (level + depth) + f" {text}", '']

    @classmethod
    def _render_list(cls, node: Tag, depth: int = 0) -> List[str]:
        lines: List[str] = []
        ordered = node.name.lower() == 'ol'
        indent = '  ' * depth
        for index, li in enumerate(node.find_all('li', recursive=False), 1):
            bullet = f"{index}." if ordered else '-'
            own, nested = [], []
            for child in li.children:
                if isinstance(child, Tag) and child.name.lower() in ('ul', 'ol'):
                    nested.extend(cls._render_list(child, depth + 1))
                else:
                    own.append(cls._inline_md(child))
            text = re.sub(r'\s+', ' ', ''.join(own)).strip()
            if text:
                lines.append(f"{indent}{bullet} {text}")
            lines.extend(nested)
        return lines

    @classmethod
    def _render_table(cls, table: Tag) -> str:
        """Render a table, honouring rowspan and colspan."""
        grid: List[List[Optional[str]]] = []

        def _span(cell: Tag, attr: str) -> int:
            try:
                return max(1, min(int((cell.get(attr) or '1').strip()), 50))
            except (TypeError, ValueError):
                return 1

        def _place(row_index: int, text: str, rowspan: int, colspan: int) -> None:
            while len(grid) <= row_index + rowspan - 1:
                grid.append([])
            row = grid[row_index]
            col = 0
            while col < len(row) and row[col] is not None:
                col += 1
            for r in range(row_index, row_index + rowspan):
                target = grid[r]
                while len(target) < col + colspan:
                    target.append(None)
                for c in range(col, col + colspan):
                    target[c] = text

        for index, tr in enumerate(table.find_all('tr')):
            cells = tr.find_all(['td', 'th'])
            if not cells:
                continue
            while len(grid) <= index:
                grid.append([])
            for cell in cells:
                _place(index, cls._text_md(cell),
                       _span(cell, 'rowspan'), _span(cell, 'colspan'))

        grid = [r for r in grid if any(c for c in r)]
        if not grid:
            return ''
        width = max(len(r) for r in grid)
        rows = [[(c or '') for c in r] + [''] * (width - len(r)) for r in grid]
        out = ['| ' + ' | '.join(rows[0]) + ' |', '|' + '---|' * width]
        for row in rows[1:]:
            out.append('| ' + ' | '.join(row) + ' |')
        return '\n'.join(out)

    @classmethod
    def _render_equation(cls, node: Tag) -> List[str]:
        """A display equation, keeping its ``Eq. (N)`` number."""
        label_el = node.find(class_='label')
        label = label_el.get_text(' ', strip=True) if label_el is not None else ''
        latex = cls._math_latex(node)
        if not latex:
            return []
        line = f"$${latex}$$"
        if label:
            number = re.search(r'\(([^)]+)\)', label)
            line += f" ({number.group(1)})" if number else f" {label}"
        return [line, '']

    @classmethod
    def _render_figure(cls, node: Tag, ctx: dict) -> List[str]:
        ctx['fig_seq'] += 1
        index = ctx['fig_seq']
        label_el = node.find(class_='label')
        label = (label_el.get_text(' ', strip=True)
                 if label_el is not None else f'Fig. {index}')
        caption = cls._figure_caption(node)
        out: List[str] = []
        out.extend([f"**{label}.** {caption}".strip() if caption else f"**{label}.**", ''])
        out.extend([f"![{label}](__SPIE_FIG_{index}__)", ''])
        return out

    @classmethod
    def extract_body_from_fulltext(cls, html: str, base_level: int = 2) -> str:
        if not html:
            return ''
        soup = BeautifulSoup(html, 'html.parser')
        for selector in _SPIE_DROP_SELECTORS:
            for el in soup.select(selector):
                el.decompose()
        # References are emitted from metadata['references'].
        for el in soup.select('div.ref-content, div.ref-label'):
            el.decompose()

        ctx = {'fig_seq': 0, 'pending_label': ''}
        blocks: List[str] = []
        for child in soup.children:
            blocks.extend(cls._render_block(child, base_level, ctx))
        return re.sub(r'\n{3,}', '\n\n', '\n'.join(blocks)).strip()

    # ==================================================================
    # Contract
    # ==================================================================

    async def get_pdf_url(self, doi: str = None) -> Optional[str]:
        html = ''
        if self.page is not None:
            try:
                html = await self.page.content()
            except Exception:
                html = ''
        if html:
            url = self.extract_metadata_from_html(html).get('_pdf_url')
            if url:
                return url
        doi = (doi or self.doi or '').strip()
        return f"{self.SPIE_BASE}/journals/{doi}.pdf" if doi else None

    async def get_supplemental_url(self, doi: str) -> Optional[str]:
        # No SPIE article seen so far ships supplemental material.
        return None

    async def extract_references(self, html: str) -> list:
        return self.extract_references_from_fulltext(html) if html else []

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def get_fulltext_url(self, page) -> str:
        try:
            return page.url or ''
        except Exception:
            return f"{self.SPIE_BASE}/journals" if self.doi else ''

    async def extract_metadata(self, page) -> dict:
        try:
            html = await page.content()
        except Exception:
            html = ''
        return self.extract_metadata_from_html(html)

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'SPIEHandler'
        )
        doi = self.doi
        set_actual_base_url(self, page)

        try:
            try:
                landing = await page.content()
            except Exception:
                landing = ''

            metadata = self.extract_metadata_from_html(landing)
            metadata['doi'] = doi or metadata.get('doi', '')
            pdf_url = metadata.pop('_pdf_url', None)
            referer = metadata.pop('_fulltext_referer', '')

            fulltext = await self.fetch_fulltext_html(page, referer=referer)

            figure_urls = {}
            if fulltext:
                metadata['references'] = self.extract_references_from_fulltext(fulltext)
                if metadata['references']:
                    print(f"  ✓ 参考文献: {len(metadata['references'])} 条")

                figure_urls = self.extract_figures_from_fulltext(fulltext)
                if figure_urls:
                    print(f"  ✓ 图片: {len(figure_urls)} 个")

                body_md = self.extract_body_from_fulltext(fulltext)
                if body_md:
                    metadata['_body_md'] = body_md
                    print(f"  ✓ 正文: {len(body_md):,} 字符")
            else:
                metadata.setdefault('references', [])

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figure_urls,
                    'supplemental_urls': [],
                    'supplemental_descriptions': {},
                },
                'fulltext_data': fulltext or landing,
                'journal_name': 'spie',
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
        md: List[str] = [f"# {metadata.get('title') or 'SPIE Article'}", '']

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

        body_md = (metadata.get('_body_md') or '').strip()
        if not body_md and isinstance(article_text, str) and article_text.strip():
            body_md = self.extract_body_from_fulltext(article_text)
        if body_md:
            md.extend(['---', '', self._resolve_figures(body_md, kwargs), ''])

        references = metadata.get('references') or []
        if references:
            md.extend(['---', '', '## References', ''])
            for index, ref in enumerate(references, 1):
                md.extend([f"[{index}] {ref}", ''])

        return '\n'.join(md).rstrip() + '\n'

    @staticmethod
    def _resolve_figures(body_md: str, kwargs: dict) -> str:
        """Swap ``__SPIE_FIG_n__`` for the local file, else the remote URL."""
        filenames = kwargs.get('figure_filenames') or {}
        figure_urls = kwargs.get('figure_urls') or {}

        def _sub(match: 're.Match') -> str:
            index = match.group(1)
            local = filenames.get(index) or filenames.get(int(index))
            if local:
                return str(local)
            info = figure_urls.get(f'fig_{index}') or {}
            return info.get('url', '') if isinstance(info, dict) else str(info)

        return re.sub(r'__SPIE_FIG_(\d+)__', _sub, body_md)

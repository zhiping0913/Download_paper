"""Researching.cn handler (Chinese Laser Press, DOI prefix 10.3788).

CLP publishes *Photonics Insights*, *Chinese Optics Letters*, *Advanced
Photonics* and others on researching.cn, and ``https://doi.org/10.3788/...``
resolves here. Some titles are co-published with SPIE, in which case the same
article also exists on spiedigitallibrary.org -- that copy is handled by
:mod:`publisher.spie` and reached by passing its URL as ``link``.

The article is server-rendered, so everything comes out of the page:

    <meta name="citation_*">     bibliographic metadata and the PDF
    div.text_area                the article (div#mainView also wraps the
                                 site header, nav and footer)
    p.abstract / p.keywords      abstract and keywords
    p.text_index                 section headings, numbered in the text
                                 ("2.1.1 Physics origin") rather than by
                                 heading level -- there are no <h*> tags
    article.text_details         body paragraphs
    p.text_pic + p.figure        figure image and its caption
    <disp-formula>               display equations (MathML)
    p.references_index           references

Images are lazy-loaded: ``src`` is a spinner GIF and the real asset is in
``lay-src``.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from html_to_md_converter import mathml_to_latex_pandoc
from publisher.base import PublisherHandler
from publisher.wildcard import init_extract_all_page, set_actual_base_url


_RESEARCHING_DROP_SELECTORS = (
    'div.download',             # "Download full size / View all figures"
    'script',
    'style',
    'noscript',
)

_RESEARCHING_NOISE_LINES = frozenset({
    'download full size',
    'view all figures',
    'download full size view all figures',
    'abstract',
    'keywords',
})


class ResearchingHandler(PublisherHandler):
    """Full-text handler for researching.cn (Chinese Laser Press)."""

    RESEARCHING_BASE = 'https://www.researching.cn'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.RESEARCHING_BASE

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

        Paragraphs, captions, headings and reference text all go through
        here, so math is never lost to a bare ``get_text()``.
        """
        if isinstance(node, Comment):
            return ''
        if isinstance(node, NavigableString):
            return re.sub(r'\s+', ' ', str(node))
        if not isinstance(node, Tag) or not node.name:
            return ''

        name = node.name.lower()
        classes = node.get('class') or []

        if name in ('script', 'style', 'noscript'):
            return ''
        if name == 'math':
            latex = cls._math_latex(node)
            return f"${latex}$" if latex else ''
        if name == 'inline-formula':
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
            # Cross-references (class="xref") point at anchors in a page we
            # are not shipping.
            if (not href or href.startswith('#') or href.startswith('javascript:')
                    or 'xref' in classes):
                return inner
            resolved = urljoin(cls.RESEARCHING_BASE, href)
            if not urlparse(resolved).netloc:
                return inner
            return f"[{text}]({cls._md_url(resolved)})" if text else ''

        return inner

    @staticmethod
    def _md_url(url: str) -> str:
        return (url or '').strip().replace('(', '%28').replace(')', '%29')

    @staticmethod
    def _wrap(inner: str, mark: str) -> str:
        stripped = inner.strip()
        if not stripped:
            return inner
        if stripped.startswith(mark) and stripped.endswith(mark):
            return inner
        lead = inner[:len(inner) - len(inner.lstrip())]
        trail = inner[len(inner.rstrip()):]
        return f"{lead}{mark}{stripped}{mark}{trail}"

    @classmethod
    def _text_md(cls, node) -> str:
        text = re.sub(r'\s+', ' ', cls._inline_md(node)).strip()
        return re.sub(r'\*{4,}', '**', text)

    @staticmethod
    def _is_noise(text: str) -> bool:
        return text.strip().lower().rstrip('.') in _RESEARCHING_NOISE_LINES

    # ==================================================================
    # Metadata
    # ==================================================================

    @staticmethod
    def _meta(soup: BeautifulSoup, name: str) -> str:
        tag = soup.find('meta', attrs={'name': name})
        return (tag.get('content') or '').strip() if tag else ''

    @staticmethod
    def _meta_all(soup: BeautifulSoup, name: str) -> List[str]:
        return [(t.get('content') or '').strip()
                for t in soup.find_all('meta', attrs={'name': name})
                if (t.get('content') or '').strip()]

    @classmethod
    def extract_metadata_from_html(cls, html: str) -> dict:
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')

        authors = []
        for raw in cls._meta_all(soup, 'citation_author'):
            if ',' in raw:
                surname, given = raw.split(',', 1)
                raw = f"{given.strip()} {surname.strip()}".strip()
            authors.append(raw)

        first = cls._meta(soup, 'citation_firstpage')
        last = cls._meta(soup, 'citation_lastpage')
        pages = f"{first}-{last}" if first and last else first

        date = cls._meta(soup, 'citation_publication_date')
        m = re.search(r'(19|20|21)\d{2}', date)

        return {
            'title': re.sub(r'\s+', ' ', cls._meta(soup, 'citation_title')).strip(),
            'doi': cls._meta(soup, 'citation_doi'),
            'authors': authors,
            'journal': cls._meta(soup, 'citation_journal_title'),
            'volume': cls._meta(soup, 'citation_volume'),
            'issue': cls._meta(soup, 'citation_issue'),
            'pages': pages,
            'year': m.group(0) if m else '',
            'publication_date': date,
            'issn': cls._meta(soup, 'citation_issn'),
            'publisher': 'Chinese Laser Press',
            'abstract': cls.extract_abstract_from_html(html),
            'corresponding_author_emails': [],
            '_keywords': cls._extract_keywords(soup),
            '_pdf_url': cls._meta(soup, 'citation_pdf_url'),
        }

    @classmethod
    def extract_abstract_from_html(cls, html: str) -> str:
        if not html:
            return ''
        soup = BeautifulSoup(html, 'html.parser')
        for el in soup.select('p.abstract, div.abstract'):
            text = cls._text_md(el)
            text = re.sub(r'^Abstract[:：]?\s*', '', text, flags=re.IGNORECASE)
            if text and not cls._is_noise(text):
                return text
        return cls._meta(soup, 'citation_abstract') or cls._meta(soup, 'description')

    @classmethod
    def _extract_keywords(cls, soup: BeautifulSoup) -> List[str]:
        for el in soup.select('p.keywords, div.keywords'):
            # Each keyword is its own <a>, with no delimiter between them --
            # splitting the flattened text would give one run-on string.
            parts = [a.get_text(' ', strip=True)
                     for a in el.select('p.labels a, a')
                     if a.get_text(strip=True)]
            if parts:
                return parts
            text = cls._text_md(el)
            text = re.sub(r'^Keywords?[:：]?\s*', '', text, flags=re.IGNORECASE)
            parts = [k.strip() for k in re.split(r'[;；,，]', text) if k.strip()]
            if parts:
                return parts
        raw = cls._meta(soup, 'citation_keywords')
        return [k.strip() for k in re.split(r'[;；,，]', raw) if k.strip()]

    # ==================================================================
    # Figures
    # ==================================================================

    @classmethod
    def extract_figures_from_html(cls, html: str) -> dict:
        """``{'fig_N': {...}}`` from ``p.text_pic``.

        The images are lazy-loaded: ``src`` is a spinner placeholder and the
        real asset lives in ``lay-src``, so reading ``src`` would download 24
        copies of loading.gif.
        """
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')
        figures = {}
        for index, holder in enumerate(soup.select('p.text_pic'), 1):
            img = holder.find('img')
            if img is None:
                continue
            src = (img.get('lay-src') or img.get('data-src') or '').strip()
            if not src or 'loading.gif' in src:
                src = ''
            if not src:
                continue
            caption, label = cls._figure_caption(holder)
            figures[f'fig_{index}'] = {
                'url': urljoin(cls.RESEARCHING_BASE, src),
                'original_url': urljoin(cls.RESEARCHING_BASE, src),
                'caption': caption,
                'label': label or f'Figure {index}',
            }
        return figures

    @classmethod
    def _figure_caption(cls, holder: Tag) -> Tuple[str, str]:
        """``(caption, label)`` from the ``p.figure`` following the image."""
        for sibling in holder.next_siblings:
            if not isinstance(sibling, Tag) or not sibling.name:
                continue
            if 'figure' in (sibling.get('class') or []):
                text = cls._text_md(sibling)
                # "Figure 1.Overview of ..." -> label plus caption.
                m = re.match(r'^((?:Figure|Fig\.?|Table)\s*\d+)\s*[.:]?\s*(.*)$',
                             text, re.IGNORECASE | re.DOTALL)
                if m:
                    return m.group(2).strip(), m.group(1).strip()
                return text, ''
            if sibling.name.lower() == 'p' and 'text_pic' in (sibling.get('class') or []):
                break
        return '', ''

    # ==================================================================
    # References
    # ==================================================================

    @classmethod
    def extract_references_from_html(cls, html: str) -> list:
        """One entry per ``p.references_index``, numbering stripped."""
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        refs = []
        for entry in soup.select('p.references_index'):
            text = cls._text_md(entry)
            text = re.sub(r'^\[\d+\]\s*', '', text)
            text = re.sub(r'\s+([,.;:])', r'\1', text)
            text = re.sub(r'\s{2,}', ' ', text).strip(' ,;')
            if text:
                refs.append(text)
        return refs

    # ==================================================================
    # Body
    # ==================================================================

    @classmethod
    def _heading_level(cls, numbering: str) -> int:
        """Depth from the section number: "2.1.1" -> 3."""
        return len([p for p in numbering.split('.') if p.strip()])

    @classmethod
    def _render_heading(cls, node: Tag, level: int) -> List[str]:
        """Render ``p.text_index``, whose depth is encoded in its number.

        researching.cn ships no ``<h*>`` tags at all -- every section title is
        a paragraph reading "2.1.1 Physics origin" -- so the heading level has
        to come from the numbering or the document would be one flat list.
        """
        text = cls._text_md(node)
        if not text:
            return []
        m = re.match(r'^(\d+(?:\.\d+)*)\s+(.*)$', text)
        depth = cls._heading_level(m.group(1)) - 1 if m else 0
        return ['#' * (level + depth) + f" {text}", '']

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
        if 'download' in classes:
            return []
        if 'references_index' in classes:
            return []                       # emitted from metadata
        if 'text_index' in classes:
            return cls._render_heading(node, level)
        if 'text_pic' in classes:
            return cls._render_figure(node, ctx)
        if 'figure' in classes:
            return []                       # emitted with its image
        if name == 'disp-formula':
            return cls._render_equation(node)
        if name in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            text = cls._text_md(node)
            depth = {'h1': 0, 'h2': 0, 'h3': 1, 'h4': 2, 'h5': 3, 'h6': 4}[name]
            return ['#' * (level + depth) + f" {text}", ''] if text else []
        if name == 'p':
            if cls._has_block_descendant(node):
                return cls._render_mixed(node, level, ctx)
            text = cls._text_md(node)
            return [text, ''] if text and not cls._is_noise(text) else []
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
    def _is_block(cls, node) -> bool:
        if not isinstance(node, Tag) or not node.name:
            return False
        classes = node.get('class') or []
        return (node.name.lower() in ('p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                                      'ul', 'ol', 'table', 'article',
                                      'disp-formula')
                or bool({'text_index', 'text_pic', 'figure'} & set(classes)))

    @classmethod
    def _has_block_descendant(cls, node: Tag) -> bool:
        return any(cls._is_block(d) for d in node.descendants if isinstance(d, Tag))

    @classmethod
    def _render_mixed(cls, node: Tag, level: int, ctx: dict) -> List[str]:
        """Container of mixed inline text and block children."""
        out: List[str] = []
        buffer: List[str] = []

        def _flush() -> None:
            text = re.sub(r'\s+', ' ', ''.join(buffer)).strip()
            buffer.clear()
            if text and not cls._is_noise(text):
                out.extend([re.sub(r'\*{4,}', '**', text), ''])

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
        latex = cls._math_latex(node)
        if not latex:
            return []
        label = ''
        label_el = node.find(class_=re.compile('label|formula-label'))
        if label_el is not None:
            label = label_el.get_text(' ', strip=True)
        line = f"$${latex}$$"
        if label:
            number = re.search(r'\(?([\d.]+)\)?', label)
            line += f" ({number.group(1)})" if number else f" {label}"
        return [line, '']

    @classmethod
    def _render_figure(cls, node: Tag, ctx: dict) -> List[str]:
        ctx['fig_seq'] += 1
        index = ctx['fig_seq']
        caption, label = cls._figure_caption(node)
        label = label or f'Figure {index}'
        out: List[str] = []
        out.extend([f"**{label}.** {caption}".strip() if caption else f"**{label}.**", ''])
        out.extend([f"![{label}](__RSCH_FIG_{index}__)", ''])
        return out

    @classmethod
    def extract_body_from_html(cls, html: str, base_level: int = 2) -> str:
        if not html:
            return ''
        soup = BeautifulSoup(html, 'html.parser')
        # div#mainView wraps the whole page including the site header, nav and
        # footer; div.text_area is the article itself.
        body = (soup.select_one('div.text_area')
                or soup.find('div', id='mainView')
                or soup)
        body = BeautifulSoup(str(body), 'html.parser')

        for selector in _RESEARCHING_DROP_SELECTORS:
            for el in body.select(selector):
                el.decompose()
        # Emitted as their own markdown sections.
        for el in body.select('p.abstract, div.abstract, p.keywords, '
                              'div.keywords, p.references_index'):
            el.decompose()

        ctx = {'fig_seq': 0}
        blocks: List[str] = []
        for child in body.children:
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
                return urljoin(self.RESEARCHING_BASE, url)
        return None

    async def get_supplemental_url(self, doi: str) -> Optional[str]:
        return None

    async def extract_references(self, html: str) -> list:
        return self.extract_references_from_html(html) if html else []

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def get_fulltext_url(self, page) -> str:
        try:
            return page.url or ''
        except Exception:
            return self.RESEARCHING_BASE

    async def extract_metadata(self, page) -> dict:
        try:
            html = await page.content()
        except Exception:
            html = ''
        return self.extract_metadata_from_html(html)

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'ResearchingHandler'
        )
        doi = self.doi
        set_actual_base_url(self, page)

        try:
            try:
                html = await page.content()
            except Exception:
                html = ''

            metadata = self.extract_metadata_from_html(html)
            metadata['doi'] = doi or metadata.get('doi', '')
            pdf_url = metadata.pop('_pdf_url', None)
            if pdf_url:
                pdf_url = urljoin(self.RESEARCHING_BASE, pdf_url)

            metadata['references'] = self.extract_references_from_html(html)
            if metadata['references']:
                print(f"  ✓ 参考文献: {len(metadata['references'])} 条")

            figure_urls = self.extract_figures_from_html(html)
            if figure_urls:
                print(f"  ✓ 图片: {len(figure_urls)} 个")

            body_md = self.extract_body_from_html(html)
            if body_md:
                metadata['_body_md'] = body_md
                print(f"  ✓ 正文: {len(body_md):,} 字符")

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figure_urls,
                    'supplemental_urls': [],
                    'supplemental_descriptions': {},
                },
                'fulltext_data': html,
                'journal_name': 'researching',
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
        md: List[str] = [f"# {metadata.get('title') or 'Article'}", '']

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
            md.extend(['**Keywords:** ' + ', '.join(metadata['_keywords']), ''])

        body_md = (metadata.get('_body_md') or '').strip()
        if not body_md and isinstance(article_text, str) and article_text.strip():
            body_md = self.extract_body_from_html(article_text)
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
        """Swap ``__RSCH_FIG_n__`` for the local file, else the remote URL."""
        filenames = kwargs.get('figure_filenames') or {}
        figure_urls = kwargs.get('figure_urls') or {}

        def _sub(match: 're.Match') -> str:
            index = match.group(1)
            local = filenames.get(index) or filenames.get(int(index))
            if local:
                return str(local)
            info = figure_urls.get(f'fig_{index}') or {}
            return info.get('url', '') if isinstance(info, dict) else str(info)

        return re.sub(r'__RSCH_FIG_(\d+)__', _sub, body_md)

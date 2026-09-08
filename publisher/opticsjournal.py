"""Optics Journal handler (opticsjournal.net, 中国光学期刊网).

Chinese Laser Press runs two sites. ``researching.cn`` is where
``https://doi.org/10.3788/...`` resolves and is handled by
:mod:`publisher.researching`; ``opticsjournal.net`` carries the Chinese-language
journals (中国激光, 光学学报, 中国光学快报 …) and is reached by URL rather than by
DOI, so the host test must come *before* the 10.3788 one in the router.

Everything is server-rendered, so the whole article comes out of the page:

    <meta name="citation_*">     bibliographic metadata and the PDF link
    div.abstract-cn              abstract -- there are **two** of these, 摘要
                                 (Chinese) and Abstract (English); both are kept
    div.fullText-con             the article body
    h2                           section headings ("1　引言", ideographic space)
    div.ArticleFigure-list       figure: image + a Chinese and an English caption
    div.tableDirectory           table: two captions, then the grid
    <disp-formula> / <math>      equations, as MathML

Two traps worth naming:

*Lazy images.* ``src`` is a 310x200 placeholder from ``/NV_LEGCY/images/``;
the real asset is in ``data-src``.

*Nested tables.* ``div.tableDiv`` holds ``<table id="topTable">`` whose single
cell contains the actual table. Rendering the outer one yields a 1x1 grid with
the whole table flattened into it.

No article with supplementary material has been seen on this site, so there is
no supplement path -- :meth:`get_supplemental_url` returns None.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from html_to_md_converter import mathml_to_latex_pandoc
from publisher.base import PublisherHandler
from publisher.wildcard import init_extract_all_page, set_actual_base_url


_OJ_DROP_SELECTORS = (
    'script',
    'style',
    'noscript',
)

# Body text that is site furniture rather than article prose. Matched on a
# whole rendered line, so a sentence merely containing the words survives.
_OJ_NOISE_LINES = frozenset({
    'abstract',
    'keywords',
    '摘要',
    '关键词',
    'download citation',
    '下载引文',
    # The abstract block ships a text-to-speech player whose fallback text
    # and trigger link would otherwise read as the first lines of the
    # abstract.
    'ai语音播报',
    '您的浏览器不支持 audio 元素。',
    '您的浏览器不支持 audio 元素',
})

# Media the page embeds for its own UI, never article content.
_OJ_SKIP_TAGS = frozenset({'audio', 'video', 'source', 'track', 'iframe'})

# The placeholder every lazy <img> starts out with.
_OJ_IMG_PLACEHOLDER = '/NV_LEGCY/images/'


class OpticsJournalHandler(PublisherHandler):
    """Full-text handler for opticsjournal.net."""

    OJ_BASE = 'https://www.opticsjournal.net'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.OJ_BASE

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
        """LaTeX for one ``<math>`` element (or the first one inside *node*).

        The served page carries MathML with no ``<annotation>``: MathJax is
        what would normally turn it into markup, and the extractor blocks
        MathJax precisely so the source survives. pandoc does the conversion.
        """
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

        Paragraphs, captions, headings, table cells and reference text all go
        through here, so math is never lost to a bare ``get_text()``.
        """
        if isinstance(node, Comment):
            return ''
        if isinstance(node, NavigableString):
            return re.sub(r'\s+', ' ', str(node))
        if not isinstance(node, Tag) or not node.name:
            return ''

        name = node.name.lower()
        classes = node.get('class') or []

        if name in ('script', 'style', 'noscript') or name in _OJ_SKIP_TAGS:
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
            # Citation markers and figure cross-references point at anchors in
            # a page we are not shipping.
            if (not href or href.startswith('#') or href.startswith('javascript:')
                    or 'aTag' in classes):
                return inner
            resolved = urljoin(cls.OJ_BASE, href)
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
        return text.strip().lower().rstrip('.：:') in _OJ_NOISE_LINES

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

        first = cls._meta(soup, 'citation_firstpage')
        last = cls._meta(soup, 'citation_lastpage')
        pages = f"{first}-{last}" if first and last and first != last else first

        date = cls._meta(soup, 'citation_publication_date')
        year = re.search(r'(19|20|21)\d{2}', date)

        # The site writes the DOI as "doi:10.3788/CJL231490".
        doi = re.sub(r'^doi:\s*', '', cls._meta(soup, 'citation_doi'),
                     flags=re.IGNORECASE)

        abstracts = cls.extract_abstracts_from_html(html)

        return {
            'title': re.sub(r'\s+', ' ', cls._meta(soup, 'citation_title')).strip(),
            'doi': doi,
            'authors': cls._meta_all(soup, 'citation_author'),
            'journal': cls._meta(soup, 'citation_journal_title'),
            'journal_cn': cls._meta(soup, 'citation_journal_abbrev'),
            'volume': cls._meta(soup, 'citation_volume'),
            'issue': cls._meta(soup, 'citation_issue'),
            'pages': pages,
            'year': year.group(0) if year else '',
            'publication_date': date,
            'issn': cls._meta(soup, 'citation_issn'),
            'publisher': 'Chinese Laser Press',
            # 'abstract' is the one the rest of the pipeline (BibTeX, the
            # metadata JSON) consumes; the Chinese one matches dc.description.
            'abstract': abstracts.get('cn') or abstracts.get('en', ''),
            '_abstract_cn': abstracts.get('cn', ''),
            '_abstract_en': abstracts.get('en', ''),
            'corresponding_author_emails': [],
            '_keywords': cls._extract_keywords(soup),
            '_pdf_url': cls._meta(soup, 'citation_pdf_url'),
        }

    @classmethod
    def extract_abstracts_from_html(cls, html: str) -> Dict[str, str]:
        """Both abstracts: ``{'cn': …, 'en': …}``.

        The page ships two ``div.abstract-cn`` blocks -- titled 摘要 and
        Abstract -- and ``dc.description`` holds only the Chinese one. The
        English block is structured, using ``<title>`` elements as the
        sub-headings (Significance / Progress / Conclusions and Prospects),
        so it is rendered rather than flattened.
        """
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')

        found: Dict[str, str] = {}
        for block in soup.find_all('div', class_='abstract-cn'):
            title_el = block.find('div', class_='abstract-cn-tit')
            body_el = block.find('div', class_='abstract-cn-con')
            if body_el is None:
                continue
            heading = title_el.get_text(strip=True) if title_el else ''
            lang = 'en' if re.match(r'abstract', heading, re.IGNORECASE) else 'cn'
            text = cls._render_abstract_body(body_el)
            if text:
                found.setdefault(lang, text)

        if not found:
            fallback = cls._meta(soup, 'dc.description') or cls._meta(soup, 'description')
            if fallback:
                found['cn'] = fallback
        return found

    @classmethod
    def _render_abstract_body(cls, body: Tag) -> str:
        """Abstract text, keeping the English block's <title> sub-headings."""
        parts: List[str] = []
        for child in body.children:
            if not isinstance(child, Tag):
                text = re.sub(r'\s+', ' ', str(child)).strip()
                if text:
                    parts.append(text)
                continue
            if child.name in _OJ_SKIP_TAGS:
                continue
            if child.name == 'title':
                label = child.get_text(' ', strip=True)
                if label:
                    parts.append(f"**{label}**")
                continue
            text = cls._text_md(child)
            if text and not cls._is_noise(text):
                parts.append(text)
        if not parts:
            text = cls._text_md(body)
            return '' if cls._is_noise(text) else text
        return '\n\n'.join(parts)

    @classmethod
    def _extract_keywords(cls, soup: BeautifulSoup) -> List[str]:
        raw = cls._meta(soup, 'citation_keywords')
        return [k.strip() for k in re.split(r'[;；]', raw) if k.strip()]

    # ==================================================================
    # Figures
    # ==================================================================

    @classmethod
    def extract_figures_from_html(cls, html: str) -> dict:
        """``{'fig_N': {'url', 'original_url', 'caption', 'label'}}``.

        Keyed by document order, matching the ``__OJ_FIG_n__`` markers the
        body walk emits.
        """
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')

        figures = {}
        for index, holder in enumerate(soup.find_all('div', class_='ArticleFigure-list'), 1):
            url = cls._figure_url(holder)
            if not url:
                continue
            label, caption = cls._float_captions(holder)
            figures[f'fig_{index}'] = {
                'url': url,
                'original_url': url,
                'caption': caption,
                'label': label or f'图 {index}',
            }
        return figures

    @classmethod
    def _figure_url(cls, holder: Tag) -> str:
        """The real image URL, past the lazy-loading placeholder."""
        for img in holder.find_all('img'):
            src = (img.get('data-src') or '').strip()
            if not src:
                candidate = (img.get('src') or '').strip()
                if candidate and _OJ_IMG_PLACEHOLDER not in candidate:
                    src = candidate
            if src:
                return urljoin(cls.OJ_BASE, src)
        return ''

    @classmethod
    def _float_captions(cls, holder: Tag) -> Tuple[str, str]:
        """``(label, caption)`` from a float's two <h4> headings.

        Figures and tables both carry a Chinese caption ("图 1. …" / "表 1. …")
        and an English one ("Fig. 1. …" / "Table 1. …"). The label is the
        leading span; both caption texts are kept, since the two are not
        translations of the same length and the English one often carries
        detail the Chinese one compresses.
        """
        texts: List[str] = []
        label = ''
        for h4 in holder.find_all('h4', recursive=False):
            span = h4.find('span')
            if span is not None and not label:
                label = span.get_text(' ', strip=True).rstrip('. ')
            marker = span.get_text(' ', strip=True) if span is not None else ''
            text = cls._text_md(h4)
            if marker:
                text = text[len(marker):].strip() if text.startswith(marker) else text
                text = re.sub(r'^(图|表)\s*\d+\s*[.．]\s*', '', text)
                text = re.sub(r'^(Fig|Table)\.?\s*\d+\s*[.．]\s*', '', text,
                              flags=re.IGNORECASE)
            if text and not cls._is_noise(text):
                texts.append(text)
        return label, ' '.join(texts).strip()

    # ==================================================================
    # References
    # ==================================================================

    @classmethod
    def extract_references_from_html(cls, html: str) -> list:
        """Reference strings, with the ``[n]`` marker stripped."""
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')

        refs = []
        for p in soup.find_all('p', id=re.compile(r'^Ref_\d+$')):
            number = p.find('span', class_='ref-number')
            if number is not None:
                number.decompose()
            text = cls._text_md(p)
            text = re.sub(r'^\[\d+\]\s*', '', text).strip()
            if text:
                refs.append(text)
        return refs

    # ==================================================================
    # Body
    # ==================================================================

    @classmethod
    def _is_block(cls, node) -> bool:
        if not isinstance(node, Tag):
            return False
        if node.name in ('p', 'div', 'h2', 'h3', 'h4', 'h5', 'table',
                         'ul', 'ol', 'disp-formula'):
            return True
        return False

    @classmethod
    def _has_block_descendant(cls, node: Tag) -> bool:
        return any(cls._is_block(c) for c in node.children)

    @classmethod
    def _render_block(cls, node, level: int, ctx: dict) -> List[str]:
        if isinstance(node, Comment):
            return []
        if isinstance(node, NavigableString):
            text = re.sub(r'\s+', ' ', str(node)).strip()
            return [text, ''] if text else []
        if not isinstance(node, Tag) or not node.name:
            return []

        name = node.name.lower()
        classes = node.get('class') or []

        if name in ('script', 'style', 'noscript'):
            return []
        if 'ArticleFigure-list' in classes:
            return cls._render_figure(node, ctx)
        if 'tableDirectory' in classes:
            return cls._render_table_float(node)
        if name == 'disp-formula':
            return cls._render_equation(node)
        if name in ('h2', 'h3', 'h4', 'h5'):
            return cls._render_heading(node, level)
        if name == 'table':
            rendered = cls._render_table(node)
            return [rendered, ''] if rendered else []
        if name in ('ul', 'ol'):
            return cls._render_list(node)
        if name == 'p':
            text = cls._text_md(node)
            if not text or cls._is_noise(text):
                return []
            return [text, '']
        if name == 'div':
            if cls._has_block_descendant(node):
                out: List[str] = []
                for child in node.children:
                    out.extend(cls._render_block(child, level, ctx))
                return out
            text = cls._text_md(node)
            return [text, ''] if text and not cls._is_noise(text) else []
        text = cls._text_md(node)
        return [text, ''] if text and not cls._is_noise(text) else []

    @classmethod
    def _render_heading(cls, node: Tag, level: int) -> List[str]:
        """Section heading, its level taken from the number it carries.

        Headings are numbered in the text ("2.1.1 …") and separated from the
        title by an ideographic space, so the depth comes from the numbering
        rather than from the tag -- every section is an <h2>.
        """
        text = cls._text_md(node)
        if not text:
            return []
        depth = 0
        m = re.match(r'^(\d+(?:[.．]\d+)*)[\s　]', text)
        if m:
            depth = len(re.split(r'[.．]', m.group(1))) - 1
        return ['#' * min(level + depth, 6) + f" {text}", '']

    @classmethod
    def _render_equation(cls, node: Tag) -> List[str]:
        """A display equation, keeping its "（N）" number as a tag."""
        latex = cls._math_latex(node)
        if not latex:
            text = cls._text_md(node)
            return [text, ''] if text else []
        number = ''
        m = re.search(r'[（(](\d+[a-z]?)[）)]\s*$', cls._text_md(node))
        if m:
            number = m.group(1)
        if number:
            return [f"$$ {latex} \\tag{{{number}}} $$", '']
        return [f"$$ {latex} $$", '']

    @classmethod
    def _render_figure(cls, node: Tag, ctx: dict) -> List[str]:
        ctx['fig_seq'] += 1
        index = ctx['fig_seq']
        label, caption = cls._float_captions(node)
        label = label or f'图 {index}'
        out: List[str] = [f"**{label}.** {caption}".strip(), '']
        out.extend([f"![{label}](__OJ_FIG_{index}__)", ''])
        return out

    @classmethod
    def _render_table_float(cls, node: Tag) -> List[str]:
        """A table with its two captions.

        ``div.tableDiv`` wraps ``table#topTable``, whose single cell holds the
        real table; rendering the outer one gives a 1x1 grid with everything
        flattened inside it.
        """
        label, caption = cls._float_captions(node)
        out: List[str] = [f"**{label}.** {caption}".strip(), '']

        table = cls._innermost_table(node)
        rendered = cls._render_table(table) if table is not None else ''
        if rendered:
            out.extend([rendered, ''])
        return out

    @staticmethod
    def _innermost_table(holder: Tag) -> Optional[Tag]:
        """The real table inside the ``topTable`` wrapper, if there is one."""
        table = holder.find('table')
        while table is not None:
            nested = table.find('table')
            if nested is None:
                return table
            table = nested
        return None

    @classmethod
    def _render_table(cls, table: Tag) -> str:
        """A markdown table, placing cells on a grid so spans stay aligned."""
        if table is None:
            return ''

        def _span(cell: Tag, attr: str) -> int:
            try:
                return max(1, int(cell.get(attr, 1)))
            except (TypeError, ValueError):
                return 1

        grid: List[List[Optional[str]]] = []

        def _place(row_index: int, text: str, rowspan: int, colspan: int) -> None:
            while len(grid) <= row_index + rowspan - 1:
                grid.append([])
            row = grid[row_index]
            col = 0
            while col < len(row) and row[col] is not None:
                col += 1
            for r in range(rowspan):
                target = grid[row_index + r]
                while len(target) < col + colspan:
                    target.append(None)
                for c in range(colspan):
                    target[col + c] = text if (r == 0 and c == 0) else ''

        for row_index, tr in enumerate(table.find_all('tr')):
            for cell in tr.find_all(['td', 'th'], recursive=False):
                _place(row_index, cls._text_md(cell),
                       _span(cell, 'rowspan'), _span(cell, 'colspan'))

        rows = [[(c or '') for c in row] for row in grid if any(row)]
        if not rows:
            return ''
        width = max(len(r) for r in rows)
        rows = [r + [''] * (width - len(r)) for r in rows]

        lines = ['| ' + ' | '.join(rows[0]) + ' |',
                 '| ' + ' | '.join(['---'] * width) + ' |']
        for row in rows[1:]:
            lines.append('| ' + ' | '.join(row) + ' |')
        return '\n'.join(lines)

    @classmethod
    def _render_list(cls, node: Tag, depth: int = 0) -> List[str]:
        out: List[str] = []
        ordered = node.name.lower() == 'ol'
        for index, li in enumerate(node.find_all('li', recursive=False), 1):
            nested = [c for c in li.find_all(['ul', 'ol'], recursive=False)]
            for sub in nested:
                sub.extract()
            text = cls._text_md(li)
            marker = f"{index}." if ordered else '-'
            if text:
                out.append('  ' * depth + f"{marker} {text}")
            for sub in nested:
                out.extend(cls._render_list(sub, depth + 1))
        if out:
            out.append('')
        return out

    @classmethod
    def extract_body_from_html(cls, html: str, base_level: int = 2) -> str:
        if not html:
            return ''
        soup = BeautifulSoup(html, 'html.parser')
        for selector in _OJ_DROP_SELECTORS:
            for el in soup.select(selector):
                el.decompose()

        root = soup.find('div', class_='fullText-con')
        if root is None:
            return ''

        ctx = {'fig_seq': 0}
        out: List[str] = []
        for child in root.children:
            out.extend(cls._render_block(child, base_level, ctx))

        lines: List[str] = []
        for line in out:
            if line == '' and lines and lines[-1] == '':
                continue
            lines.append(line)
        return '\n'.join(lines).strip()

    # ==================================================================
    # PublisherHandler contract
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
                return urljoin(self.OJ_BASE, url)
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
            return self.OJ_BASE

    async def extract_metadata(self, page) -> dict:
        try:
            html = await page.content()
        except Exception:
            html = ''
        return self.extract_metadata_from_html(html)

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'OpticsJournalHandler'
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
                pdf_url = urljoin(self.OJ_BASE, pdf_url)

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
                'journal_name': 'opticsjournal',
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
        for key, label in (('journal', 'Journal'), ('journal_cn', '期刊'),
                           ('volume', 'Volume'), ('issue', 'Issue'),
                           ('pages', 'Pages'), ('year', 'Year'),
                           ('doi', 'DOI'), ('publisher', 'Publisher')):
            value = (metadata.get(key) or '').strip()
            if value:
                md.extend([f"**{label}:** {value}", ''])

        # Both abstracts, in the order the page presents them.
        cn = (metadata.get('_abstract_cn') or '').strip()
        en = (metadata.get('_abstract_en') or '').strip()
        if not (cn or en):
            cn = (metadata.get('abstract') or '').strip()
        md.extend(['---', ''])
        md.extend(['## 摘要', '', cn or '[No Chinese abstract available.]', ''])
        md.extend(['## Abstract', '', en or '[No English abstract available.]', ''])

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
        """Swap ``__OJ_FIG_n__`` for the local file, else the remote URL."""
        filenames = kwargs.get('figure_filenames') or {}
        figure_urls = kwargs.get('figure_urls') or {}

        def _sub(match: 're.Match') -> str:
            index = match.group(1)
            local = filenames.get(index) or filenames.get(int(index))
            if local:
                return str(local)
            info = figure_urls.get(f'fig_{index}') or {}
            return info.get('url', '') if isinstance(info, dict) else str(info)

        return re.sub(r'__OJ_FIG_(\d+)__', _sub, body_md)

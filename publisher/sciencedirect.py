"""
ScienceDirect / Elsevier publisher handler.

Extracts metadata, body, figures, tables, references, and supplemental materials
from ScienceDirect article pages (www.sciencedirect.com).

ScienceDirect ships article data both as:
  * ``window.__PRELOADED_STATE__`` JSON (canonical metadata, PDF link payload).
  * Rendered HTML under ``<div class="body-area">`` and
    ``<div class="body u-font-serif" id="body">`` (figures, tables, formulas).

The page is heavily JavaScript-rendered, so a headed browser is required to
load the rendered DOM.  MathJax stores assistive MathML alongside the SVG
rendering, so equation conversion reuses the shared pandoc MathML path.
"""

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from publisher.base import PublisherHandler
from publisher.wildcard import (
    convert_html_fragment_to_markdown,
    format_as_bibtex,
    generate_bibtex_key,
    generate_reference_text_from_crossref,
    init_extract_all_page,
    set_actual_base_url,
)


class ScienceDirectHandler(PublisherHandler):
    """Handler for ScienceDirect / Elsevier articles."""

    SD_BASE = 'https://www.sciencedirect.com'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)

    # ------------------------------------------------------------------
    # __PRELOADED_STATE__ extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_preloaded_state(html_content: str) -> dict:
        """Return the parsed ``window.__PRELOADED_STATE__`` JSON, or {}."""
        if not html_content:
            return {}

        m = re.search(
            r'window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});\s*</script>',
            html_content,
            re.DOTALL,
        )
        if not m:
            return {}
        raw = m.group(1)
        try:
            return json.loads(raw)
        except Exception:
            # Trailing comma / malformed — try to recover greedily
            try:
                return json.loads(raw.rstrip(';').strip())
            except Exception:
                return {}

    @staticmethod
    def _collect_text_from_xocs(node) -> str:
        """Flatten Elsevier's xocs JSON tree (``_`` / ``$$``) into plain text."""
        if node is None:
            return ''
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            return ' '.join(ScienceDirectHandler._collect_text_from_xocs(x) for x in node)
        if isinstance(node, dict):
            parts = []
            if '_' in node:
                parts.append(str(node.get('_', '')))
            if '$$' in node:
                parts.append(ScienceDirectHandler._collect_text_from_xocs(node['$$']))
            return ' '.join(p for p in parts if p)
        return ''

    @classmethod
    def _authors_from_state(cls, state: dict) -> list:
        """Extract author names from the ``article.authors`` xocs tree."""
        authors_state = state.get('authors') or {}
        content = authors_state.get('content') or []
        results = []
        for group in content:
            if not isinstance(group, dict):
                continue
            if group.get('#name') != 'author-group':
                continue
            for child in group.get('$$', []) or []:
                if not isinstance(child, dict):
                    continue
                if child.get('#name') != 'author':
                    continue
                given = ''
                surname = ''
                for sub in child.get('$$', []) or []:
                    if not isinstance(sub, dict):
                        continue
                    if sub.get('#name') == 'given-name':
                        given = sub.get('_', '') or given
                    elif sub.get('#name') == 'surname':
                        surname = sub.get('_', '') or surname
                full = ' '.join(p for p in (given, surname) if p).strip()
                if full:
                    results.append(full)
        return results

    @classmethod
    def _pdf_url_from_state(cls, state: dict) -> str:
        """Build the PDF URL from ``article.pdfDownload`` payload."""
        article = state.get('article') or {}
        pdf_info = article.get('pdfDownload') or {}
        url_meta = pdf_info.get('urlMetadata') or {}
        path = (url_meta.get('path') or '').strip('/')
        pii = url_meta.get('pii') or article.get('pii') or ''
        pdf_ext = url_meta.get('pdfExtension') or '/pdfft'
        query_params = url_meta.get('queryParams') or {}
        if not (path and pii):
            return ''
        query = '&'.join(f"{k}={v}" for k, v in query_params.items() if v)
        url = f"{cls.SD_BASE}/{path}/{pii}{pdf_ext}"
        if query:
            url = f"{url}?{query}"
        return url

    @classmethod
    def _pdf_url_from_html(cls, soup: BeautifulSoup) -> str:
        """Locate the PDF URL from the headed ``View PDF`` button."""
        anchor = soup.find('a', attrs={'aria-label': re.compile(r'View PDF', re.IGNORECASE)})
        if anchor and anchor.get('href'):
            href = anchor['href'].strip()
            if href.startswith('/'):
                href = cls.SD_BASE + href
            return href
        return ''

    @classmethod
    def _abstract_from_state(cls, state: dict) -> str:
        """Build the abstract string from ``abstracts.content`` (author class)."""
        abstracts_state = state.get('abstracts') or {}
        for entry in abstracts_state.get('content', []) or []:
            if not isinstance(entry, dict):
                continue
            attrs = entry.get('$') or {}
            cls_attr = (attrs.get('class') or '').strip()
            if cls_attr == 'author':  # the "Abstract" entry (not author-highlights)
                text = cls._collect_text_from_xocs(entry.get('$$') or [])
                text = re.sub(r'^\s*Abstract\s*', '', text or '').strip()
                return re.sub(r'\s+', ' ', text).strip()
        return ''

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_metadata_from_html_meta(html_content: str) -> dict:
        """Read ``<meta name="citation_*">`` tags into a flat dict."""
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        meta = {}
        for tag in soup.find_all('meta'):
            name = tag.get('name', '')
            content = tag.get('content', '')
            if not name or not content:
                continue
            if name == 'citation_title':
                meta['title'] = content.strip()
            elif name == 'citation_doi':
                meta['doi'] = content.strip()
            elif name == 'citation_journal_title':
                meta['journal'] = content.strip()
            elif name == 'citation_volume':
                meta['volume'] = content.strip()
            elif name in ('citation_issue', 'citation_number'):
                meta['issue'] = content.strip()
            elif name == 'citation_firstpage':
                meta['pages'] = content.strip()
            elif name == 'citation_publisher':
                meta['publisher'] = content.strip()
            elif name == 'citation_publication_date':
                date_str = content.strip()
                meta['publication_date'] = date_str
                if '/' in date_str:
                    meta['year'] = date_str.split('/')[0]
                elif date_str:
                    m = re.search(r'(\d{4})', date_str)
                    if m:
                        meta['year'] = m.group(1)
            elif name == 'citation_online_date' and not meta.get('publication_date'):
                meta['publication_date'] = content.strip()
            elif name == 'citation_pii':
                meta['pii'] = content.strip()
        return meta

    @classmethod
    def _extract_keywords_from_html(cls, soup: BeautifulSoup) -> list:
        """Pull rendered keywords from the body-area keywords section."""
        keywords = []
        for kw in soup.select('div.keywords-section div.keyword'):
            text = kw.get_text(' ', strip=True)
            if text:
                keywords.append(text)
        return keywords

    @classmethod
    def _extract_highlights_md(cls, soup: BeautifulSoup) -> str:
        """Return Highlights as a Markdown bulleted list, if present."""
        section = soup.find('div', class_='abstract author-highlights')
        if not section:
            return ''
        items = []
        for li in section.select('li.react-xocs-list-item'):
            content_div = li.find('div', class_='u-margin-s-bottom')
            if content_div:
                text = cls._convert_paragraph_to_md(str(content_div))
            else:
                text = li.get_text(' ', strip=True)
            text = re.sub(r'\s+', ' ', text).strip()
            if text:
                items.append(f"- {text}")
        if not items:
            return ''
        return "**Highlights**\n\n" + "\n".join(items)

    @classmethod
    def _extract_abstract_md(cls, soup: BeautifulSoup) -> str:
        """Return the Abstract (rendered HTML) as Markdown."""
        # The Abstract container has class "abstract author" (highlights is "abstract author-highlights")
        for div in soup.find_all('div', class_='abstract'):
            classes = div.get('class') or []
            if 'author' in classes and 'author-highlights' not in classes:
                # Skip the inner "abstract author" duplicate; look at the structural one
                # The outer one wraps the inner h2 + inner content
                if div.find('h2'):
                    content_div = div.find('div', class_='abstract')
                    if content_div is None:
                        # If no nested 'abstract', use the section content excluding h2
                        text_parts = []
                        for elem in div.children:
                            if hasattr(elem, 'name') and elem.name and elem.name != 'h2':
                                text_parts.append(cls._convert_paragraph_to_md(str(elem)))
                        return "\n\n".join(p for p in text_parts if p).strip()
                    return cls._convert_paragraph_to_md(str(content_div)).strip()
        return ''

    # ------------------------------------------------------------------
    # Math / paragraph conversion
    # ------------------------------------------------------------------

    # Placeholder format chosen so pandoc passes it through unchanged.
    # Both inline and display formula placeholders are alphanumeric tokens.
    _INLINE_MATH_PLACEHOLDER = "XSDMATHX{idx:04d}XEND"
    _DISPLAY_MATH_PLACEHOLDER = "XSDDISPLAYX{idx:04d}XEND"

    @classmethod
    def _preprocess_sd_math(cls, html_fragment: str) -> tuple:
        """Replace SD MathJax markup with placeholders that survive pandoc.

        ScienceDirect renders each formula as::

            <span class="math">
              <span class="MathJax_Preview" ...></span>
              <span class="MathJax_SVG" data-mathml="<math ...>...</math>" ...>
                ...svg...
                <span class="MJX_Assistive_MathML"><math>...</math></span>
              </span>
              <script type="math/mml"><math>...</math></script>
            </span>

        We feed the inner ``<math>`` to pandoc (via ``mathml_to_latex_pandoc``)
        and substitute alphanumeric placeholders so the HTML→MD pass cannot
        mangle the LaTeX (pandoc otherwise escapes ``$`` and ``<``).

        Display equations live inside ``<span class="display"><span class="formula"><span class="label">(N)</span>...``
        — those get a trailing ``\\quad (N)`` tag.

        Returns ``(processed_html, restore_map)`` where ``restore_map`` maps
        placeholder strings back to the LaTeX (already wrapped with the
        correct ``$``/``$$`` delimiters).
        """
        from html_to_md_converter import mathml_to_latex_pandoc

        if not html_fragment:
            return html_fragment, {}

        soup = BeautifulSoup(html_fragment, 'html.parser')

        restore = {}

        # Strip MathJax_Preview wrappers (they only contain duplicated visual junk).
        for prev in soup.select('span.MathJax_Preview'):
            prev.decompose()
        # Strip the assistive MathML (we'll use the math/mml script or data-mathml directly).
        for assist in soup.select('span.MJX_Assistive_MathML'):
            assist.decompose()
        # Strip the inline <svg> renderings — we keep only the source MathML.
        for svg in soup.find_all('svg'):
            svg.decompose()

        # First pass: convert each <span class="math"> to a placeholder mapped to $...$.
        inline_counter = [0]
        for math_span in soup.find_all('span', class_='math'):
            latex = ''
            script = math_span.find('script', attrs={'type': re.compile(r'math/m?ml')})
            if script and script.string:
                latex = mathml_to_latex_pandoc(script.string)
            else:
                svg_span = math_span.find('span', class_='MathJax_SVG')
                if svg_span and svg_span.get('data-mathml'):
                    latex = mathml_to_latex_pandoc(svg_span['data-mathml'])
                else:
                    math_tag = math_span.find('math')
                    if math_tag:
                        latex = mathml_to_latex_pandoc(str(math_tag))

            if not latex:
                math_span.decompose()
                continue

            latex = latex.strip()
            if latex.startswith('$$') and latex.endswith('$$'):
                latex_body = latex[2:-2].strip()
            elif latex.startswith('$') and latex.endswith('$'):
                latex_body = latex[1:-1].strip()
            else:
                latex_body = latex

            key = cls._INLINE_MATH_PLACEHOLDER.format(idx=inline_counter[0])
            inline_counter[0] += 1
            restore[key] = f"${latex_body}$"
            math_span.replace_with(soup.new_string(f" {key} "))

        # Second pass: rewrap <span class="display"><span class="formula"> as a
        # display-math placeholder.  Inline math inside the formula was already
        # stashed in the first pass, so we expand any inline placeholders to
        # LaTeX before composing the display body.
        display_counter = [0]
        for disp in soup.find_all('span', class_='display'):
            for formula in disp.find_all('span', class_='formula'):
                label = ''
                label_span = formula.find('span', class_='label')
                if label_span:
                    label = label_span.get_text(' ', strip=True)
                    label_span.decompose()

                inner = formula.get_text(' ', strip=True)
                # Expand any inline-math placeholders inside the formula body.
                def _expand(match, _r=restore):
                    val = _r.get(match.group(0), '')
                    if val.startswith('$') and val.endswith('$'):
                        val = val[1:-1]
                    return val

                inner = re.sub(r'XSDMATHX\d{4}XEND', _expand, inner)
                latex_body = re.sub(r'\s+', ' ', inner).strip()

                if not latex_body:
                    formula.decompose()
                    continue

                if label:
                    eq_md = f"\n$$\n{latex_body} \\quad {label}\n$$\n"
                else:
                    eq_md = f"\n$$\n{latex_body}\n$$\n"

                key = cls._DISPLAY_MATH_PLACEHOLDER.format(idx=display_counter[0])
                display_counter[0] += 1
                restore[key] = eq_md
                formula.replace_with(soup.new_string(f"\n{key}\n"))

            if not disp.get_text(strip=True):
                disp.decompose()

        # Footnote refs (<a href="#fnN" ...>†</a>) → pandoc ``[^N]`` markers,
        # stashed via the restore_map so pandoc doesn't escape the ``[``/``^``.
        # Must run BEFORE the generic anchor-stripping below, which would
        # otherwise reduce the anchor to the cycling †/‡ symbol and lose the
        # link to the actual footnote definition.
        fn_counter = [0]
        for a in soup.find_all('a', href=re.compile(r'^#fn\d+$')):
            m_fn = re.match(r'^#fn(\d+)$', a.get('href', ''))
            if not m_fn:
                continue
            key = f"XSDFNX{fn_counter[0]:04d}XEND"
            fn_counter[0] += 1
            restore[key] = f"[^{m_fn.group(1)}]"
            a.replace_with(soup.new_string(key))

        # Drop noisy cross-reference anchors so they don't pollute the MD output.
        for a in soup.find_all('a', class_=re.compile(r'anchor')):
            text = a.get_text(' ', strip=True)
            # Keep the visible text but strip the anchor wrapper
            a.replace_with(soup.new_string(text))

        # Drop topic-link anchors but keep the visible word — these are
        # SciencDirect's automatic "concept" links, not real references.
        for a in soup.find_all('a', class_='topic-link'):
            a.replace_with(soup.new_string(a.get_text(' ', strip=True)))

        # Unwrap structural <div>/<section>/<span> wrappers that pandoc would
        # otherwise convert to fenced divs (``::: {#id} ... :::``).  We only
        # want the inline text and inline markup to survive.
        for tag in soup.find_all(['div', 'section', 'span']):
            classes = tag.get('class') or []
            # Preserve our internal placeholders and structural cues we still need.
            if 'display' in classes or 'formula' in classes:
                continue
            tag.unwrap()

        return str(soup), restore

    @classmethod
    def _convert_paragraph_to_md(cls, html_fragment: str) -> str:
        """Convert a ScienceDirect HTML fragment to Markdown with formulas restored."""
        if not html_fragment:
            return ''

        processed, restore_map = cls._preprocess_sd_math(html_fragment)
        md = convert_html_fragment_to_markdown(processed) if processed else ''
        for placeholder, replacement in restore_map.items():
            md = md.replace(placeholder, replacement)
        md = md.strip()
        md = re.sub(r'\n{3,}', '\n\n', md)
        return md

    # ------------------------------------------------------------------
    # Body text extraction
    # ------------------------------------------------------------------

    @classmethod
    def extract_article_text_from_html(cls, html_content: str):
        """Return ``(abstract_md, body_md)`` from the rendered HTML."""
        if not html_content:
            return '', ''

        soup = BeautifulSoup(html_content, 'html.parser')

        abstract_parts = []
        highlights_md = cls._extract_highlights_md(soup)
        if highlights_md:
            abstract_parts.append(highlights_md)

        abstract_md = cls._extract_abstract_md(soup)
        if abstract_md:
            abstract_parts.append("**Abstract**\n\n" + abstract_md)

        keywords = cls._extract_keywords_from_html(soup)
        if keywords:
            abstract_parts.append("**Keywords:** " + "; ".join(keywords))

        combined_abstract = "\n\n".join(abstract_parts).strip()

        # The body wrapper may use lowercase or capitalized class
        # (``body u-font-serif`` vs ``Body u-font-serif``). Fall back to ``id``.
        body_div = soup.find('div', id='body')

        if not body_div:
            return combined_abstract, ''

        parts = []
        cls._walk_sd_body(body_div, parts)

        body_md = "\n".join(parts).strip()
        body_md = re.sub(r'\n{3,}', '\n\n', body_md)

        # Append footnote definitions so pandoc-style ``[^N]`` refs resolve.
        footnotes = cls._extract_footnotes_from_html(soup)
        if footnotes:
            footnote_lines = []
            for n, text in footnotes:
                # Indent continuation lines so the definition stays attached.
                indented = text.replace('\n', '\n    ')
                footnote_lines.append(f"[^{n}]: {indented}")
            body_md = body_md.rstrip() + "\n\n" + "\n\n".join(footnote_lines) + "\n"

        return combined_abstract, body_md

    @classmethod
    def _extract_footnotes_from_html(cls, soup) -> list:
        """Return ``[(n, markdown), ...]`` for footnotes in ``div.footnotes``.

        Each ``<dl class="footnote">`` pairs a ``<dt class="footnote-label">``
        (back-anchor like ``<a href="#bfnN">``) with a ``<dd class="footnote-detail">``
        containing ``<div id="cenotepN">`` — the footnote text.  The number N
        matches the ``#fnN`` href used by body references.

        Notes on where they live:
          * Modern ScienceDirect renders footnotes in
            ``<div class="footnotes text-xs">`` (lowercase, extra utility
            class), NOT ``<div class="Footnotes">``. A single container is
            usually a sibling of ``<div id="body">`` inside
            ``<div class="body-area">``; some templates split them across
            multiple containers.
          * Each footnote's contents may include LaTeX / MathJax, so the
            detail HTML must go through the same paragraph converter as
            body text — we already do this via
            ``_convert_paragraph_to_md``, which stashes formulas.
        """
        results = []
        seen_numbers = set()
        # Match any div whose class list contains 'footnotes' (or the older
        # capitalised 'Footnotes'). Iterate all matches so multi-container
        # layouts (some CE-styled papers) are covered.
        containers = soup.find_all(
            'div',
            class_=lambda v: bool(v) and any(c in ('footnotes', 'Footnotes')
                                             for c in (v if isinstance(v, list) else [v])),
        )
        if not containers:
            return results

        for footnotes_div in containers:
            for dl in footnotes_div.find_all('dl', class_='footnote'):
                n = None
                dt = dl.find('dt', class_='footnote-label')
                if dt:
                    back = dt.find('a', href=re.compile(r'^#bfn\d+$'))
                    if back:
                        m = re.match(r'^#bfn(\d+)$', back.get('href', ''))
                        if m:
                            n = m.group(1)
                if n is None:
                    # Fallback: derive from the cenotepN id on the detail div.
                    detail_div = dl.find('div', id=re.compile(r'^cenotep\d+$'))
                    if detail_div:
                        m = re.match(r'^cenotep(\d+)$', detail_div.get('id', ''))
                        if m:
                            n = m.group(1)
                if n is None or n in seen_numbers:
                    continue

                dd = dl.find('dd', class_='footnote-detail')
                if not dd:
                    continue

                # A single <dd> can hold MULTIPLE <div id="cenotepN"> paragraph
                # divs — Elsevier assigns cenotepN ids per paragraph, not per
                # footnote, so a footnote with several equations/paragraphs
                # renders as e.g. cenotep34 + cenotep35 + … all under one
                # <dl>/<dd> keyed by the same #bfn33 back-anchor. Iterate every
                # such div and join their converted markdown with a blank line.
                inner_divs = dd.find_all(
                    'div',
                    id=re.compile(r'^cenotep\d+$'),
                    recursive=False,
                )
                if inner_divs:
                    parts = []
                    for div in inner_divs:
                        md_part = cls._convert_paragraph_to_md(str(div))
                        if md_part:
                            parts.append(md_part)
                    text_md = "\n\n".join(parts).strip()
                else:
                    # Legacy shape: no cenotepN wrappers, just prose in the <dd>.
                    text_md = cls._convert_paragraph_to_md(str(dd))

                if not text_md:
                    continue
                seen_numbers.add(n)
                results.append((n, text_md))

        # Sort by numeric footnote number so [^1] appears before [^2].
        try:
            results.sort(key=lambda kv: int(kv[0]))
        except Exception:
            pass
        return results

    @classmethod
    def _walk_sd_body(cls, container, parts: list):
        """Walk ScienceDirect body content in document order.

        Uses inline buffering so paragraph wrappers like ``<div id="pr0080">``
        that mix prose with embedded block elements (figures, tables, or a
        ``<span class="display">`` wrapping a figure/table) emit each part
        correctly: text → paragraph, figure → figure block, more text →
        next paragraph.  Without buffering, the recursive walker dropped
        every NavigableString and the prose between the inline blocks
        silently vanished from the body.

        Block-level children (sections, headings, paragraph-wrappers without
        nested blocks, figures, tables, span-wrapped figures/tables) flush
        the inline buffer before being emitted.  Inline-level children
        (NavigableStrings, ``<span>``, ``<em>``, ``<strong>``, ``<sub>``,
        ``<sup>``, ``<a>``, ``<b>``, ``<i>``, ``<br>``) and any other
        unrecognised tag are appended to the buffer and flushed at the next
        block boundary or at the end of the walk.
        """
        from bs4 import NavigableString

        inline_buffer = []

        def flush():
            if not inline_buffer:
                return
            combined = ''.join(inline_buffer)
            p_md = cls._convert_paragraph_to_md(combined)
            if p_md:
                parts.append(p_md)
                parts.append("")
            inline_buffer.clear()

        def _direct_block_child(span_el):
            """Return ('figure'|'table', element) if *span_el* has a direct
            block child (<figure class="figure"> or <div class="tables">)."""
            for c in span_el.children:
                if not hasattr(c, 'name') or not c.name:
                    continue
                c_cls = c.get('class') or []
                if c.name == 'figure' and 'figure' in c_cls:
                    return ('figure', c)
                if c.name == 'div' and 'tables' in c_cls:
                    return ('table', c)
            return (None, None)

        for child in container.children:
            if isinstance(child, NavigableString):
                text = str(child)
                if text.strip():
                    inline_buffer.append(text)
                continue
            if not hasattr(child, 'name') or not child.name:
                continue

            classes = child.get('class') or []

            if child.name == 'section':
                flush()
                cls._walk_sd_body(child, parts)
                continue

            if child.name in ('h2', 'h3', 'h4', 'h5'):
                flush()
                heading = child.get_text(' ', strip=True)
                heading = re.sub(r'\s+', ' ', heading or '').strip()
                if heading and heading.lower() not in ('references', 'reference'):
                    level = '#' * (int(child.name[1]) + 1)
                    parts.append(f"{level} {heading}")
                    parts.append("")
                continue

            if child.name == 'figure' and 'figure' in classes:
                flush()
                cls._render_figure(child, parts)
                continue

            # <span class="display"> often wraps a block figure or table
            # inline within a paragraph.  Promote it to a block emission.
            if child.name == 'span' and 'display' in classes:
                kind, inner = _direct_block_child(child)
                if kind == 'figure':
                    flush()
                    cls._render_figure(inner, parts)
                    continue
                if kind == 'table':
                    flush()
                    cls._render_table_block(inner, parts)
                    continue
                # No nested block — treat as inline (e.g. display-math span).
                inline_buffer.append(str(child))
                continue

            if child.name == 'div':
                if 'tables' in classes:
                    flush()
                    cls._render_table_block(child, parts)
                    continue

                # A div with NO nested block descendants is a leaf
                # paragraph (covers both pr* ids and the d1eNNNN ids seen
                # in newer ScienceDirect renderings).  Inline figures
                # (<figure class="inline-figure">) do NOT count.
                has_nested_block = bool(
                    child.find('div', class_='tables')
                    or child.find('figure', class_='figure')
                    or child.find('section')
                )
                if has_nested_block:
                    flush()
                    cls._walk_sd_body(child, parts)
                    continue

                flush()
                p_md = cls._convert_paragraph_to_md(str(child))
                if p_md:
                    parts.append(p_md)
                    parts.append("")
                continue

            if child.name == 'p':
                flush()
                p_md = cls._convert_paragraph_to_md(str(child))
                if p_md:
                    parts.append(p_md)
                    parts.append("")
                continue

            # Inline-level tags → buffer (text + em + span + …).
            if child.name in ('span', 'em', 'strong', 'sub', 'sup', 'a', 'b', 'i', 'br'):
                inline_buffer.append(str(child))
                continue

            # Anything else → buffer as inline (closest reasonable default).
            inline_buffer.append(str(child))

        flush()

    @staticmethod
    def _direct_sub_figures(fig_elem) -> list:
        """Return ``<figure class="figure">`` elements that are direct
        children of *fig_elem* (composite figures like Fig. 4(a)–(d))."""
        return [
            c for c in fig_elem.children
            if hasattr(c, 'name') and c.name == 'figure'
            and 'figure' in (c.get('class') or [])
        ]

    @staticmethod
    def _direct_caption_span(fig_elem):
        """Return the ``<span class="captions">`` direct child of *fig_elem*.

        Used to locate the shared caption of a composite figure without
        accidentally grabbing a sub-figure's caption (which ``find`` would
        otherwise return because BeautifulSoup walks descendants by default).
        """
        for child in fig_elem.children:
            if (hasattr(child, 'name') and child.name == 'span'
                    and 'captions' in (child.get('class') or [])):
                return child
        return None

    @classmethod
    def _extract_figure_caption(cls, caption_span) -> tuple:
        """Return ``(label, caption_md)`` parsed from a captions span.

        Captions are structured as ``<span class="captions"><span><p><span
        class="label">Fig. N</span>. body</p></span></span>``.
        """
        if caption_span is None:
            return '', ''

        label = ''
        caption_md = ''
        inner_p = caption_span.find('p')
        if inner_p:
            label_span = inner_p.find('span', class_='label')
            if label_span:
                label = label_span.get_text(' ', strip=True)
                label_span.decompose()
            caption_md = cls._convert_paragraph_to_md(str(inner_p))
        else:
            label_span = caption_span.find('span', class_='label')
            if label_span:
                label = label_span.get_text(' ', strip=True)
                label_span.decompose()
            caption_md = cls._convert_paragraph_to_md(str(caption_span))

        # Normalize whitespace in the label so e.g. "Fig.\n   4(a)" → "Fig. 4(a)".
        label = re.sub(r'\s+', ' ', label).strip()
        caption_md = re.sub(r'^[.\s]+', '', caption_md or '').strip()
        return label, caption_md

    @classmethod
    def _figure_image_url(cls, fig_elem) -> str:
        """Return the best image URL for a single ``<figure>`` element.

        Prefers the high-res download anchor, falls back to full-size,
        then to the inline ``<img>`` src. Looks only inside the figure's
        own ``<span>`` (the wrapper that contains image + download links),
        not inside any nested ``<span class="captions">``.
        """
        direct_span = None
        for child in fig_elem.children:
            if (hasattr(child, 'name') and child.name == 'span'
                    and not (child.get('class') and 'captions' in child.get('class'))):
                direct_span = child
                break
        scope = direct_span or fig_elem

        for anchor in scope.find_all('a', class_='download-link'):
            title = (anchor.get('title') or '').lower()
            if 'high-res' in title:
                url = anchor.get('href', '').strip()
                if url:
                    return url
        for anchor in scope.find_all('a', class_='download-link'):
            title = (anchor.get('title') or '').lower()
            if 'full-size' in title or 'full size' in title:
                url = anchor.get('href', '').strip()
                if url:
                    return url
        img = scope.find('img')
        if img:
            return (img.get('src') or img.get('data-src') or '').strip()
        return ''

    @classmethod
    def _render_figure(cls, fig_elem, parts: list):
        """Append a Markdown figure block.

        - For figures with a ``<span class="captions">`` (e.g. publisher's
          numbered "Fig. N. caption"), emit ``**Fig. N.** caption``.  The
          actual image is inserted later in ``convert_to_markdown`` via the
          ``**Fig. N.**``-pattern → ``![…](filename)`` rewrite step.

        - For figures WITHOUT a caption (e.g. inline algorithm-cycle
          illustrations like ``fg0160`` in 10.1016/j.cpc.2022.108457),
          embed the image inline using the source URL.  ``convert_to_markdown``
          then rewrites that URL to the local downloaded filename.  This
          way the figure appears where it belongs in the body even though
          there's no caption text to attach a label to.

        - Composite figures (``<figure id="figN">`` containing sub-figures
          ``<figure id="figNa">…``) recurse: one Markdown block per
          sub-figure, followed by the shared outer caption.
        """
        sub_figures = cls._direct_sub_figures(fig_elem)

        if sub_figures:
            for sub in sub_figures:
                cls._render_figure(sub, parts)
            outer_caption = cls._direct_caption_span(fig_elem)
            label, caption_md = cls._extract_figure_caption(outer_caption)
            if label and caption_md:
                parts.append(f"**{label}.** {caption_md}")
                parts.append("")
            elif label:
                parts.append(f"**{label}.**")
                parts.append("")
            elif caption_md:
                parts.append(caption_md)
                parts.append("")
            return

        caption_span = fig_elem.find('span', class_='captions')
        label, caption_md = cls._extract_figure_caption(caption_span)
        if label and caption_md:
            parts.append(f"**{label}.** {caption_md}")
        elif label:
            parts.append(f"**{label}.**")
        elif caption_md:
            parts.append(caption_md)
        else:
            # No publisher caption — embed the image inline so it appears in
            # the body markdown anyway.  convert_to_markdown will rewrite
            # this URL to the local filename after the asset is downloaded.
            img_url = cls._figure_image_url(fig_elem)
            if img_url and 'data:image' not in img_url:
                parts.append(f"![]({img_url})")
                parts.append("")
            return
        parts.append("")

    @classmethod
    def _render_table_block(cls, tbl_div, parts: list):
        """Append a Markdown table block (caption + table)."""
        caption_span = tbl_div.find('span', class_='captions')
        label = ''
        caption_md = ''
        if caption_span:
            inner_p = caption_span.find('p')
            if inner_p:
                label_span = inner_p.find('span', class_='label')
                if label_span:
                    label = label_span.get_text(' ', strip=True)
                    label_span.decompose()
                caption_md = cls._convert_paragraph_to_md(str(inner_p))
            else:
                caption_md = cls._convert_paragraph_to_md(str(caption_span))

        caption_md = re.sub(r'^[.\s]+', '', caption_md or '').strip()
        if label and caption_md:
            parts.append(f"**{label}.** {caption_md}")
        elif label:
            parts.append(f"**{label}.**")
        elif caption_md:
            parts.append(f"**{caption_md}**")

        # Build the actual MD table
        table = tbl_div.find('table')
        if table is not None:
            md_table = cls._convert_table_to_md(table)
            if md_table:
                parts.append("")
                parts.append(md_table)
        parts.append("")

    @classmethod
    def _convert_table_to_md(cls, table) -> str:
        """Convert <table> element to Markdown, processing cells through the math pipeline."""
        rows = []
        widths = 0

        thead = table.find('thead')
        if thead:
            header_cells = []
            for tr in thead.find_all('tr'):
                cells = []
                for cell in tr.find_all(['th', 'td']):
                    cells.append(cls._process_table_cell(cell))
                if cells:
                    header_cells = cells
                    break
            if header_cells:
                widths = len(header_cells)
                rows.append('| ' + ' | '.join(header_cells) + ' |')
                rows.append('|' + '|'.join(['---'] * widths) + '|')

        tbody = table.find('tbody') or table
        for tr in tbody.find_all('tr'):
            if thead and tr.find_parent('thead'):
                continue
            cells = []
            for cell in tr.find_all(['th', 'td']):
                cells.append(cls._process_table_cell(cell))
            if not cells or all(c == '' for c in cells):
                continue
            if widths and len(cells) < widths:
                cells.extend([''] * (widths - len(cells)))
            rows.append('| ' + ' | '.join(cells) + ' |')

        if len(rows) <= 1:
            return ''
        return "\n".join(rows)

    @classmethod
    def _process_table_cell(cls, cell) -> str:
        """Convert a <td>/<th> cell, preserving inline formulas."""
        md = cls._convert_paragraph_to_md(str(cell))
        md = re.sub(r'\s+', ' ', md).strip()
        # Escape pipe characters that would break the markdown table.
        md = md.replace('|', '\\|')
        return md

    # ------------------------------------------------------------------
    # Figure extraction
    # ------------------------------------------------------------------

    @classmethod
    def _figure_is_graphical_abstract(cls, fig_elem) -> bool:
        """Return True when *fig_elem* lives inside an ``abstract`` container.

        ScienceDirect places the graphical abstract as
        ``<figure class="figure" id="dfig1">`` under
        ``<div class="abstract graphical">``.  Those should not be numbered
        alongside the real article figures.
        """
        for parent in fig_elem.parents:
            classes = parent.get('class') or []
            if 'abstract' in classes or 'graphical' in classes:
                return True
        return False

    @classmethod
    def extract_graphical_abstract_url(cls, html_content: str) -> str:
        """Return the high-res URL of the article's graphical abstract, if any."""
        if not html_content:
            return ''
        soup = BeautifulSoup(html_content, 'html.parser')
        for fig_elem in soup.find_all('figure', class_='figure'):
            if not cls._figure_is_graphical_abstract(fig_elem):
                continue
            # Prefer the high-res download anchor; fall back to standard.
            for anchor in fig_elem.find_all('a', class_='download-link'):
                href = (anchor.get('href') or '').strip()
                if href:
                    return href
            img = fig_elem.find('img')
            if img:
                return (img.get('src') or img.get('data-src') or '').strip()
        return ''

    @classmethod
    def extract_figures_from_html(cls, html_content: str) -> dict:
        """Return ``{'fig_N': {'url': ..., 'caption': ...}}`` for download.

        Figures inside the abstract container (the graphical abstract) are
        excluded — they are returned by ``extract_graphical_abstract_url`` and
        downloaded separately as ``key_image.*``.
        """
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        figures = {}
        seen = set()

        for fig_elem in soup.find_all('figure', class_='figure'):
            if cls._figure_is_graphical_abstract(fig_elem):
                continue
            # Composite outer figures (e.g. fig4 wrapping fig4a–d) have no image
            # of their own; the actual images live in the inner sub-figures,
            # which find_all() will visit on subsequent iterations.
            if cls._direct_sub_figures(fig_elem):
                continue
            fig_id = fig_elem.get('id', '') or ''
            if fig_id and fig_id in seen:
                continue
            if fig_id:
                seen.add(fig_id)

            # Prefer high-res download anchor; fall back to full-size, then <img>.
            # Shared helper so that _render_figure's caption-less fallback uses
            # the same URL the workflow downloads.
            img_url = cls._figure_image_url(fig_elem)
            if not img_url or 'data:image' in img_url:
                continue

            # Caption span is a direct sibling of the image wrapper inside fig_elem.
            caption_span = cls._direct_caption_span(fig_elem)
            if caption_span is None:
                # Fall back to the first descendant captions span (single figures).
                caption_span = fig_elem.find('span', class_='captions')
            label_text, caption_md = cls._extract_figure_caption(caption_span)
            caption_md = re.sub(r'\s+', ' ', caption_md).strip()

            key = f"fig_{len(figures) + 1}"
            figures[key] = {
                'url': img_url,
                'caption': caption_md,
                'label': label_text,  # e.g. "Fig. 4(a)" — used to embed in MD
            }

        return figures

    # ------------------------------------------------------------------
    # Reference extraction
    # ------------------------------------------------------------------

    @classmethod
    def extract_references_from_html(cls, html_content: str) -> list:
        """Return raw reference text strings (one per <li>)."""
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        ref_section = soup.find('div', attrs={'data-testid': 'references'})
        if not ref_section:
            return []

        references = []
        for li in ref_section.select('ol.references > li'):
            ref_span = li.find('span', class_='reference')
            if not ref_span:
                continue

            parts = []
            authors_div = ref_span.find('div', class_='authors')
            if authors_div:
                parts.append(authors_div.get_text(' ', strip=True).rstrip('.'))

            title_div = ref_span.find('div', class_='title')
            if title_div:
                parts.append(title_div.get_text(' ', strip=True).rstrip('.'))

            host_div = ref_span.find('div', class_='host')
            if host_div:
                parts.append(host_div.get_text(' ', strip=True))

            text = '. '.join(p for p in parts if p)
            text = re.sub(r'\s+', ' ', text).strip()
            if text:
                references.append(text)
        return references

    # ------------------------------------------------------------------
    # Supplemental material extraction
    # ------------------------------------------------------------------

    @classmethod
    def _extract_supplemental_from_html(cls, html_content: str) -> tuple:
        """Find supplementary download links inside ``Appendix … Supplementary …`` sections.

        Strategy (per ``support_sciencedirect.md``): walk every heading whose
        text contains both ``Appendix`` and ``Supp`` (case-insensitive); inside
        the enclosing section, harvest any download anchor with an absolute
        URL that points to a content asset (``ars.els-cdn.com``) or carries a
        ``download`` attribute / ``download-link`` class.

        Returns ``(urls, descriptions)`` where ``descriptions`` is keyed by URL
        with the caption text (label + body) when available.
        """
        if not html_content:
            return [], {}

        soup = BeautifulSoup(html_content, 'html.parser')
        urls = []
        descriptions = {}

        for heading in soup.find_all(['h2', 'h3', 'h4']):
            text = heading.get_text(' ', strip=True)
            if not text:
                continue
            if not (re.search(r'Appendix', text, re.IGNORECASE)
                    and re.search(r'Supp', text, re.IGNORECASE)):
                continue

            container = heading.find_parent('section')
            if container is None:
                # Fall back to siblings until the next sibling heading.
                container = heading.parent

            # Each <span class="e-component"> wraps one attachment (link + caption).
            components = container.find_all('span', class_=re.compile(r'\be-component\b'))
            if not components:
                # Last-resort: any download anchor under the container.
                components = [container]

            for comp in components:
                anchor = None
                for a in comp.find_all('a', href=True):
                    href = a['href'].strip()
                    classes = a.get('class') or []
                    if not href:
                        continue
                    if not href.startswith('http'):
                        if href.startswith('/'):
                            href = cls.SD_BASE + href
                        else:
                            continue
                    if (
                        'download-link' in classes
                        or a.has_attr('download')
                        or 'ars.els-cdn.com' in href
                        or 'els-cdn.com' in href
                    ):
                        anchor = (a, href)
                        break

                if anchor is None:
                    continue
                a_tag, url = anchor
                if url in descriptions:
                    continue

                # Caption: <span class="captions"><span><p><span class="label">MMC S1</span>. …</p></span></span>
                caption_text = ''
                cap_span = comp.find('span', class_='captions')
                if cap_span:
                    inner_p = cap_span.find('p')
                    caption_text = (inner_p or cap_span).get_text(' ', strip=True)
                if not caption_text:
                    caption_text = (a_tag.get('title') or '').strip()
                if not caption_text:
                    caption_text = 'Supplementary material'

                urls.append(url)
                descriptions[url] = re.sub(r'\s+', ' ', caption_text).strip()

        return urls, descriptions

    # ------------------------------------------------------------------
    # Publisher contract methods
    # ------------------------------------------------------------------

    async def extract_metadata(self, page) -> dict:
        html_content = ''
        if page is not None:
            try:
                html_content = await page.content()
            except Exception:
                html_content = ''

        meta = self._extract_metadata_from_html_meta(html_content)
        state = self._extract_preloaded_state(html_content)

        # Authors: prefer __PRELOADED_STATE__ (HTML often has no citation_author meta).
        authors = self._authors_from_state(state)

        # Abstract: __PRELOADED_STATE__ (per support_sciencedirect.md spec #2).
        abstract = self._abstract_from_state(state)

        # PDF URL from state, with HTML fallback.
        pdf_url = self._pdf_url_from_state(state)
        if not pdf_url and html_content:
            soup = BeautifulSoup(html_content, 'html.parser')
            pdf_url = self._pdf_url_from_html(soup)

        # Publication date — also try state.article.dates
        article_state = state.get('article') or {}
        dates = article_state.get('dates') or {}
        publication_date = (
            meta.get('publication_date')
            or dates.get('Publication date')
            or dates.get('Available online')
            or ''
        )

        year = meta.get('year') or ''
        if not year and publication_date:
            m = re.search(r'(\d{4})', publication_date)
            if m:
                year = m.group(1)

        keywords = []
        if html_content:
            soup = BeautifulSoup(html_content, 'html.parser')
            keywords = self._extract_keywords_from_html(soup)

        return {
            'title': meta.get('title') or article_state.get('titleString') or 'ScienceDirect Article',
            'authors': authors,
            'author_with_affiliations': [],
            'corresponding_author_emails': [],
            'abstract': abstract,
            'journal': meta.get('journal') or article_state.get('srctitle') or 'ScienceDirect',
            'publisher': meta.get('publisher') or 'Elsevier',
            'publication_date': publication_date,
            'doi': meta.get('doi') or article_state.get('doi') or self.doi,
            'volume': meta.get('volume') or article_state.get('vol-first') or '',
            'issue': meta.get('issue') or '',
            'pages': meta.get('pages') or '',
            'year': year,
            'references': [],
            '_pdf_url': pdf_url,
            '_keywords': keywords,
        }

    async def get_fulltext_url(self, page) -> str:
        if page is not None:
            try:
                return page.url
            except Exception:
                pass
        return f"https://doi.org/{self.doi}" if self.doi else None

    async def get_pdf_url(self, doi: str) -> str:
        return None  # resolved through extract_metadata

    async def get_supplemental_url(self, doi: str) -> str:
        return None

    async def extract_references(self, html: str) -> list:
        return self.extract_references_from_html(html) if html else []

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'ScienceDirectHandler'
        )
        doi = self.doi  # init may have updated handler.doi

        set_actual_base_url(self, page)

        try:
            # Give the body / references some time to render — ScienceDirect lazy-loads them.
            try:
                await page.wait_for_selector('div.body-area', timeout=15000)
            except Exception:
                pass
            try:
                await page.wait_for_selector('div#body', timeout=10000)
            except Exception:
                pass
            try:
                await page.wait_for_selector('div[data-testid="references"]', timeout=15000)
            except Exception:
                pass

            try:
                fulltext_html = await page.content()
            except Exception:
                fulltext_html = ''

            metadata = await self.extract_metadata(page)
            metadata['doi'] = doi or metadata.get('doi') or self.doi

            pdf_url = metadata.pop('_pdf_url', None)
            keywords = metadata.pop('_keywords', [])

            if fulltext_html:
                metadata['references'] = self.extract_references_from_html(fulltext_html)

            figure_urls = {}
            supp_urls = []
            supp_descriptions = {}
            if fulltext_html:
                figure_urls = self.extract_figures_from_html(fulltext_html)
                supp_urls, supp_descriptions = self._extract_supplemental_from_html(
                    fulltext_html
                )
                # Graphical abstract (e.g. ga1_lrg.jpg) is downloaded as key_image
                # via the shared download pipeline (metadata['key_image_url']).
                key_image_url = self.extract_graphical_abstract_url(fulltext_html)
                if key_image_url:
                    metadata['key_image_url'] = key_image_url

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figure_urls,
                    'supplemental_urls': supp_urls,
                    'supplemental_descriptions': supp_descriptions,
                },
                'fulltext_data': fulltext_html,
                'journal_name': 'sciencedirect',
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

    # ------------------------------------------------------------------
    # Markdown generation
    # ------------------------------------------------------------------

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        title = metadata.get('title') or 'ScienceDirect Article'
        md_parts = [f"# {title}", ""]

        authors = metadata.get('authors', [])
        if authors:
            md_parts.append("**Authors:** " + ", ".join(authors))
            md_parts.append("")

        if metadata.get('doi'):
            md_parts.extend([f"**DOI:** {metadata['doi']}", ""])

        md_parts.extend([
            "## Publication",
            "",
            f"**Journal:** {metadata.get('journal') or 'ScienceDirect'}",
            "",
        ])
        for field, label in [
            ('volume', 'Volume'),
            ('issue', 'Issue'),
            ('pages', 'Pages'),
            ('publisher', 'Publisher'),
            ('publication_date', 'Published'),
        ]:
            value = metadata.get(field)
            if value:
                md_parts.append(f"**{label}:** {value}")
                md_parts.append("")

        # Abstract block: combine highlights + abstract + keywords (HTML-derived).
        abstract_md = ''
        body_md = ''
        if isinstance(article_text, str) and article_text.strip():
            if article_text.lstrip().startswith('<'):
                abstract_md, body_md = self.extract_article_text_from_html(article_text)
            else:
                body_md = article_text.strip()

        # If extract_article_text_from_html missed the abstract, fall back to metadata.
        if not abstract_md and metadata.get('abstract'):
            abstract_md = "**Abstract**\n\n" + metadata['abstract']

        if abstract_md:
            md_parts.extend([
                "---",
                "",
                "## Abstract",
                "",
                abstract_md,
                "",
            ])

        # Insert downloaded figure images after captions.
        # Each figure URL entry carries a ``label`` (e.g. "Fig. 4(a)") so we
        # can match composite-figure captions like ``**Fig. 4(a).**`` directly
        # rather than just by trailing digit (which would miss the sub-letter).
        if kwargs.get('add_figure_refs') and kwargs.get('figure_filenames'):
            figure_filenames = kwargs['figure_filenames']
            figure_urls = kwargs.get('figure_urls', {}) or {}
            for fig_id, fig_info in figure_urls.items():
                if not isinstance(fig_info, dict):
                    continue
                m = re.search(r'(\d+)$', str(fig_id))
                if not m:
                    continue
                fig_num = m.group(1)
                filename = figure_filenames.get(fig_num)
                if not filename:
                    continue
                label = (fig_info.get('label') or '').strip().rstrip('.')
                if label:
                    # Build a regex from the visible label, e.g. "Fig. 4(a)".
                    label_re = re.escape(label).replace(r'\.', r'\.?')
                    pattern = rf'(\*\*{label_re}[.:]\*\*[^\n]*)'
                    alt_text = label
                    body_md = re.sub(
                        pattern,
                        rf'\1\n\n![{alt_text}.]({filename})',
                        body_md,
                    )
                # Always do a URL→filename rewrite as a safety net.  This
                # handles uncaptioned inline figures (e.g. algorithm-cycle
                # illustrations like fg0160 in 10.1016/j.cpc.2022.108457)
                # that ``_render_figure`` emitted as ``![](URL)`` rather than
                # via the labelled caption pattern above.
                src_url = (fig_info.get('url') or '').strip()
                if src_url:
                    body_md = body_md.replace(src_url, filename)

        md_parts.extend([
            "---",
            "",
            "## Article Text",
            "",
            body_md or "[Article text not found.]",
            "",
        ])

        # Supplemental block (currently always empty for ScienceDirect)
        supplemental_urls = kwargs.get('supplemental_urls', [])
        supplemental_downloads = kwargs.get('supplemental_downloads', [])
        if supplemental_urls or supplemental_downloads:
            md_parts.extend(["---", "", "## Supplemental Material", ""])
            if supplemental_downloads:
                for dl in supplemental_downloads:
                    md_parts.append(f"- {dl}")
            else:
                for url in supplemental_urls:
                    md_parts.append(f"- [{url}]({url})")
            md_parts.append("")

        # References: prefer Crossref-enriched entries, fall back to raw HTML text.
        crossref_refs = metadata.get('_crossref_references', [])
        references = metadata.get('references', [])

        if crossref_refs:
            md_parts.extend(["---", "", "## References", ""])
            for idx, ref in enumerate(crossref_refs, 1):
                unstructured = ref.get('unstructured', '')
                if unstructured:
                    md_parts.append(f"[{idx}] {unstructured}")
                else:
                    md_parts.append(generate_reference_text_from_crossref(ref, index=idx))
                md_parts.append("")

                ref_key = ref.get('key') or generate_bibtex_key(
                    [ref.get('author', '')] if ref.get('author') else [],
                    str(ref.get('year', '')),
                    ref.get('article-title', ''),
                )
                parts_dict = {
                    'author': ref.get('author', ''),
                    'title': ref.get('article-title', ''),
                    'journal': ref.get('journal-title', ''),
                    'volume': ref.get('volume', ''),
                    'firstpage': ref.get('first-page', ''),
                    'lastpage': ref.get('last-page', ''),
                    'year': str(ref.get('year', '')),
                    'doi': ref.get('DOI', ''),
                }
                parts_dict = {k: v for k, v in parts_dict.items() if v or k == 'doi'}
                if any(parts_dict.get(k) for k in ('author', 'title', 'journal')):
                    bibtex = format_as_bibtex(parts_dict, key=ref_key)
                    md_parts.extend(["```bibtex", bibtex, "```", ""])
            md_parts.append("")
        elif references:
            md_parts.extend(["---", "", "## References", ""])
            for idx, ref in enumerate(references, 1):
                md_parts.append(f"[{idx}] {ref}")
                md_parts.append("")

        return "\n".join(md_parts)

"""
ScienceDirect / Elsevier publisher handler.

Extracts metadata, body, figures, tables, references, and supplemental materials
from ScienceDirect article pages (www.sciencedirect.com).

ScienceDirect ships article data as:
  * ``window.__PRELOADED_STATE__`` JSON — canonical metadata, PDF link payload.
  * ``/sdfe/arp/pii/{PII}/body?entitledToken={TOKEN}`` JSON — **the body**.
  * Rendered HTML under ``<div class="body-area">`` — the visual DOM.

Body extraction uses the JSON API (``render_body_json``). The payload is
Elsevier's "xocs" serialisation of the JATS XML and still carries source
**MathML**, so equations convert to real LaTeX. The rendered DOM cannot be
relied on for this: since ~2023 Elsevier ships MathJax 3 CHTML with every
LaTeX/MathML annotation stripped (no data-latex, no <mjx-assistive-mml>,
no <math>), leaving only visual glyphs plus a speech string.

The per-session ``entitledToken`` is scraped from the landing-page HTML.
Figures and supplemental files resolve through the payload's ``attachments``
list: ``https://ars.els-cdn.com/content/image/{attachment-eid}``.

The DOM walk is retained as a fallback for when the token/PII can't be
resolved — see the LEGACY banner further down. Metadata, abstract,
highlights, keywords and references still come from the HTML.

The page is heavily JavaScript-rendered, so a headed browser is required.
"""

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from html_to_md_converter import mathml_to_latex_pandoc
from publisher.base import PublisherHandler
from publisher.wildcard import (
    convert_html_fragment_to_markdown,
    format_as_bibtex,
    generate_bibtex_key,
    generate_reference_text_from_crossref,
    init_extract_all_page,
    render_heading_md,
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
        # Try source-recovery in priority order:
        #   1) <script type="math/mml">          MathJax 2.x embedded MathML
        #   2) <span class="MathJax_SVG" data-mathml="…">  MathJax 2.x SVG rendering
        #   3) bare <math>                       raw MathML (rare on SD)
        #   4) <mjx-container data-latex="…">    MathJax 3 with LaTeX annotation
        #   5) <mjx-assistive-mml> inside <mjx-container>  MathJax 3 accessibility MathML
        #   6) placeholder built from data-semantic-speech-none + <mjx-speech>
        #      — some newer SD papers ship MathJax 3 CHTML with NO source
        #        annotation of any kind (data-latex missing, no assistive-mml,
        #        no <math>, no <script> — the DOM only carries visual glyphs
        #        and a semantic speech string). Emitting a "[Equation: …]"
        #        text placeholder is worse than clean LaTeX but far better
        #        than silently deleting the formula (previous behaviour),
        #        which broke paragraph flow around every equation.
        inline_counter = [0]
        for math_span in soup.find_all('span', class_='math'):
            latex = ''
            speech_fallback = ''
            script = math_span.find('script', attrs={'type': re.compile(r'math/m?ml')})
            if script and script.string:
                latex = mathml_to_latex_pandoc(script.string)
            if not latex:
                svg_span = math_span.find('span', class_='MathJax_SVG')
                if svg_span and svg_span.get('data-mathml'):
                    latex = mathml_to_latex_pandoc(svg_span['data-mathml'])
            if not latex:
                math_tag = math_span.find('math')
                if math_tag:
                    latex = mathml_to_latex_pandoc(str(math_tag))
            if not latex:
                mjx = math_span.find('mjx-container')
                if mjx is not None:
                    # 4) direct LaTeX annotation
                    data_latex = (mjx.get('data-latex') or '').strip()
                    if data_latex:
                        latex = data_latex
                    # 5) assistive MathML
                    if not latex:
                        assistive = mjx.find('mjx-assistive-mml')
                        if assistive is not None:
                            inner_math = assistive.find('math')
                            src_ml = str(inner_math) if inner_math else str(assistive)
                            latex = mathml_to_latex_pandoc(src_ml)
                    # 6) speech-none placeholder — preserve paragraph flow when
                    #    Elsevier's CHTML strips all source annotations
                    if not latex:
                        speech = (mjx.get('data-semantic-speech-none')
                                  or mjx.get('data-semantic-speech') or '').strip()
                        if not speech:
                            speech_el = mjx.find('mjx-speech')
                            if speech_el is not None:
                                speech = speech_el.get_text(' ', strip=True)
                        if speech:
                            # Trim runaway aria-label repetition (some papers
                            # include the speech twice via mark tags).
                            speech = re.sub(r'\s+', ' ', speech)[:400]
                            speech_fallback = f"\\text{{[math: {speech}]}}"

            if not latex and not speech_fallback:
                math_span.decompose()
                continue

            if speech_fallback and not latex:
                latex = speech_fallback

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

    # ==================================================================
    # LEGACY — rendered-DOM body extraction (fallback path)
    # ==================================================================
    # Everything from here to ``extract_references_from_html`` walks the
    # rendered <div id="body"> DOM. It was the primary path until the
    # sdfe/arp body-JSON API was wired in (see ``render_body_json`` above).
    #
    # Kept because it is a genuine fallback: ``extract_all`` uses it when
    # the entitledToken / PII can't be resolved or the API request fails.
    # Note its known limitation — on papers rendered with MathJax 3 CHTML
    # (Elsevier, ~2023 onward) the DOM carries no LaTeX/MathML source, so
    # equations degrade to "[math: <speech text>]" placeholders. The API
    # path has the real MathML and should be preferred whenever available.
    #
    # ``_preprocess_sd_math`` / ``_convert_paragraph_to_md`` above are NOT
    # legacy — the abstract + highlights extraction still runs through them.

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

            if child.name in ('h2', 'h3', 'h4', 'h5', 'h6'):
                flush()
                # h2 → ###, h3 → ####, h4 → #####, h5/h6 → ######
                level = '#' * min(int(child.name[1]) + 1, 6)
                heading_md = render_heading_md(
                    child, level, converter=cls._convert_paragraph_to_md
                )
                # Skip standalone "References" heading — refs are emitted
                # separately with their own section.
                stripped = re.sub(r'^#+\s+', '', heading_md).strip().lower()
                if heading_md and stripped not in ('references', 'reference'):
                    parts.append(heading_md)
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

        # Table footnote / legend text lives in a sibling <div class="legend">
        # inside the same <div class="tables"> wrapper — Elsevier uses this
        # for "explanation of symbols" style notes (e.g. "A boundary field
        # (BF) 0 means that it is vanishing, while A refers to 'point A' of
        # Ref. [18]"). Emit each contained paragraph as a "**Note:**" line
        # below the table so the context isn't lost.
        legend = tbl_div.find('div', class_='legend')
        if legend is not None:
            note_paras = legend.find_all(
                'div', class_='u-margin-s-bottom', recursive=False,
            ) or [legend]
            for note in note_paras:
                note_md = cls._convert_paragraph_to_md(str(note))
                note_md = re.sub(r'\s+', ' ', note_md or '').strip()
                if not note_md:
                    continue
                parts.append("")
                parts.append(f"**Note:** {note_md}")
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

        # Also grab <figure class="inline-figure">: Elsevier renders in-text
        # program listings, snippets, algorithm cycles etc. as GIFs
        # (e.g. …/fx001.gif) wrapped in this element. They aren't numbered so
        # they don't have a caption / label, but the URL-swap safety net in
        # convert_to_markdown() rewrites the body markdown's inline
        # ![](https://…) reference to the local filename once the download
        # finishes. Skip anything without a proper CDN URL to avoid picking
        # up icon / spinner assets.
        for fig_elem in soup.find_all('figure', class_='inline-figure'):
            img = fig_elem.find('img')
            if not img:
                continue
            img_url = (img.get('src') or img.get('data-src') or '').strip()
            if not img_url or img_url.startswith('data:'):
                continue
            # Only pick up hosted content assets; skip UI icons that some
            # templates render as inline-figures.
            if not ('ars.els-cdn.com' in img_url or 'els-cdn.com' in img_url):
                continue
            # Dedupe against normal figures whose URL might already be listed
            # (defensive — unlikely but cheap).
            if any(entry.get('url') == img_url for entry in figures.values()):
                continue

            key = f"fig_{len(figures) + 1}"
            figures[key] = {
                'url': img_url,
                'caption': '',
                'label': '',       # No label → won't match ``**Fig. N.**``
                                    # regex; URL-swap safety net covers it.
                'inline': True,     # marker for anyone inspecting the dict
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

        for heading in soup.find_all(['h2', 'h3', 'h4', 'h5', 'h6']):
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

    # ==================================================================
    # Body API (sdfe/arp/pii/{PII}/body) — PRIMARY body extraction path
    # ==================================================================
    # ScienceDirect renders the article body client-side from a JSON API:
    #     https://www.sciencedirect.com/sdfe/arp/pii/{PII}/body?entitledToken={TOKEN}
    # The per-session token is embedded in the landing-page HTML as
    # ``"entitledToken":"…"``.
    #
    # Why this replaces the DOM walk: since ~2023 Elsevier serves the body
    # as MathJax 3 CHTML with EVERY LaTeX/MathML source annotation stripped
    # (no data-latex, no <mjx-assistive-mml>, no <math>, nothing in
    # __PRELOADED_STATE__) — only visual glyphs plus a speech string. The
    # JSON API still carries the original MathML, so formulas convert to
    # real LaTeX instead of "[math: script L sub HE equals …]" placeholders.
    #
    # JSON node model (Elsevier "xocs" serialisation of JATS XML):
    #     {"#name": tag, "$": {attrs}, "$$": [children], "_": "leaf text"}
    # Top level: {content, floats, footnotes, attachments, …}

    SD_ATTACHMENT_BASE = 'https://ars.els-cdn.com/content/image/'
    SD_BODY_API = 'https://www.sciencedirect.com/sdfe/arp/pii/{pii}/body?entitledToken={token}'

    # Inline wrappers: xocs tag -> (prefix, suffix)
    _XOCS_INLINE_WRAP = {
        'italic': ('*', '*'),
        'bold': ('**', '**'),
        'sup': ('^', '^'),
        'inf': ('~', '~'),
        'sub': ('~', '~'),
        'monospace': ('`', '`'),
    }

    # Nodes whose children we render but that add no markup of their own.
    _XOCS_TRANSPARENT = {
        'display', 'sections', 'simple-para', 'caption', 'textbox-body',
        'para-block', 'outline', 'entry-para', 'floats',
        'nomenclature', 'glossary', 'textbox',
    }

    # Nodes to drop entirely (metadata / bookkeeping that isn't body prose).
    _XOCS_SKIP = {
        'alt-text', 'grant-sponsor', 'grant-number', 'author-group',
        'correspondence', 'affiliation', 'author', 'date',
    }

    @staticmethod
    def _extract_entitled_token(html_content: str) -> str:
        """Pull the per-session body-API token out of the landing page HTML."""
        if not html_content:
            return ''
        m = re.search(r'"entitledToken"\s*:\s*"([^"]+)"', html_content)
        return m.group(1) if m else ''

    @staticmethod
    def _extract_pii(html_content: str = '', url: str = '') -> str:
        """Resolve the article PII from the URL or the page HTML."""
        for src in (url or '', html_content or ''):
            if not src:
                continue
            m = re.search(r'/pii/([A-Z0-9]{15,20})', src)
            if m:
                return m.group(1)
        if html_content:
            m = re.search(r'"pii"\s*:\s*"([A-Z0-9]{15,20})"', html_content)
            if m:
                return m.group(1)
        return ''

    @classmethod
    def _body_api_url(cls, pii: str, token: str) -> str:
        if not pii or not token:
            return ''
        return cls.SD_BODY_API.format(pii=pii, token=token)

    # ---- xocs node helpers -------------------------------------------

    @staticmethod
    def _xocs_xml(node) -> str:
        """Serialise an xocs JSON node back to an XML string (for MathML)."""
        def esc(s):
            return (str(s).replace('&', '&amp;')
                          .replace('<', '&lt;').replace('>', '&gt;'))

        def rec(n):
            if isinstance(n, str):
                return esc(n)
            if not isinstance(n, dict):
                return ''
            name = n.get('#name')
            if not name:
                return ''
            if name == '__text__':
                return esc(n.get('_', '') or '')
            attrs = ''.join(
                f' {k}="{esc(v)}"'
                for k, v in (n.get('$') or {}).items()
                # xmlns:* come through as boolean True — they'd serialise as
                # xmlns:mml="True" and confuse pandoc's MathML reader.
                if v is not True and not str(k).startswith('xmlns')
            )
            kids = n.get('$$')
            if kids:
                inner = ''.join(rec(c) for c in kids)
            else:
                inner = esc(n.get('_', '') or '')
            return f'<{name}{attrs}>{inner}</{name}>'

        return rec(node)

    @classmethod
    def _xocs_math_latex(cls, math_node, display: bool = False) -> str:
        """Convert an xocs <math> node to a LaTeX body (no $ delimiters)."""
        xml = cls._xocs_xml(math_node)
        if not xml:
            return ''
        latex = mathml_to_latex_pandoc(xml) or ''
        latex = latex.strip()
        if not latex:
            return ''
        # pandoc returns $…$ / $$…$$ — strip so the caller controls wrapping.
        if latex.startswith('$$') and latex.endswith('$$'):
            latex = latex[2:-2].strip()
        elif latex.startswith('$') and latex.endswith('$'):
            latex = latex[1:-1].strip()
        return latex

    @classmethod
    def _sd_attachment_index(cls, attachments: list) -> dict:
        """Map a file basename → {'url', 'type', 'filename', 'filesize'}.

        Elsevier ships several renditions of the same asset::

            1-s2.0-<PII>-gr002.jpg       IMAGE-DOWNSAMPLED
            1-s2.0-<PII>-gr002_lrg.jpg   IMAGE-HIGH-RES     ← preferred
            1-s2.0-<PII>-gr002.sml       IMAGE-THUMBNAIL    ← skipped
            1-s2.0-<PII>-mmc1.pdf        APPLICATION        ← supplemental

        High-res variants carry a ``_lrg`` suffix; index them under the plain
        basename so a body ``link locator="gr002"`` resolves to the best copy.
        """
        rank = {
            'IMAGE-HIGH-RES': 3,
            'IMAGE-DOWNSAMPLED': 2,
            'APPLICATION': 2,
            'IMAGE-THUMBNAIL': 0,   # never worth downloading
        }
        idx = {}
        for att in attachments or []:
            eid = (att.get('attachment-eid') or '').strip()
            base = (att.get('file-basename') or '').strip()
            atype = (att.get('attachment-type') or '').strip()
            if not eid or not base:
                continue
            score = rank.get(atype, 1)
            if score <= 0:
                continue
            key = base[:-4] if base.endswith('_lrg') else base
            prev = idx.get(key)
            if prev is not None and prev['_score'] >= score:
                continue
            idx[key] = {
                '_score': score,
                'url': cls.SD_ATTACHMENT_BASE + eid,
                'type': atype,
                'filename': att.get('filename') or base,
                'filesize': att.get('filesize') or '',
                'eid': eid,
            }
        return idx

    @classmethod
    def _sd_float_index(cls, floats: list) -> dict:
        """Map float id (fig001 / tbl001) → the float node."""
        idx = {}
        for f in floats or []:
            fid = ((f.get('$') or {}).get('id') or '').strip()
            if fid:
                idx[fid] = f
        return idx

    # ---- xocs → markdown ---------------------------------------------

    @classmethod
    def _xocs_render(cls, node, ctx: dict, depth: int = 0) -> str:
        """Recursively render an xocs node to Markdown.

        ``ctx`` carries the attachment index, float index, heading base level
        and the set of float ids already emitted (so a float rendered at its
        anchor isn't repeated in the trailing sweep).
        """
        if node is None:
            return ''
        if isinstance(node, list):
            return ''.join(cls._xocs_render(n, ctx, depth) for n in node)
        if isinstance(node, str):
            return node
        if not isinstance(node, dict):
            return ''

        name = node.get('#name') or ''
        attrs = node.get('$') or {}
        kids = node.get('$$')
        text = node.get('_')

        def inner(d=depth):
            if kids:
                return ''.join(cls._xocs_render(k, ctx, d) for k in kids)
            return text or ''

        if name in cls._XOCS_SKIP:
            return ''

        if name == '__text__':
            return text or ''

        # ---- math ----
        if name == 'math':
            latex = cls._xocs_math_latex(node)
            return f' ${latex}$ ' if latex else ''

        if name == 'formula':
            label, math_node = '', None
            for k in (kids or []):
                kn = k.get('#name')
                if kn == 'label':
                    label = (k.get('_') or '').strip()
                elif kn == 'math':
                    math_node = k
            if math_node is None:
                return inner()
            latex = cls._xocs_math_latex(math_node, display=True)
            if not latex:
                return ''
            tail = f' \\quad {label}' if label else ''
            return f'\n\n$$\n{latex}{tail}\n$$\n\n'

        # ---- inline formatting ----
        if name in cls._XOCS_INLINE_WRAP:
            pre, suf = cls._XOCS_INLINE_WRAP[name]
            body = inner().strip()
            return f'{pre}{body}{suf}' if body else ''

        if name == 'hsp':
            return ' '

        if name in ('cross-ref', 'cross-refs'):
            # A cross-ref pointing at a footnote id becomes a markdown
            # footnote marker; everything else (equation / figure / citation
            # refs) keeps rendering as its plain visible text.
            refid = (attrs.get('refid') or '').strip()
            num = (ctx.get('footnote_numbers') or {}).get(refid)
            if num:
                ctx['footnotes_used'].add(refid)
                return f'[^{num}]'
            return (text or inner() or '').strip()

        if name == 'inter-ref':
            href = (attrs.get('href') or '').strip()
            label = (text or inner() or href).strip()
            if href.startswith('mailto:'):
                return label
            return f'[{label}]({href})' if href else label

        # ---- headings / sections ----
        if name == 'section':
            label, title = '', ''
            rest = []
            for k in (kids or []):
                kn = k.get('#name')
                if kn == 'label' and not label:
                    label = (k.get('_') or '').strip()
                elif kn == 'section-title' and not title:
                    title = cls._xocs_render(k, ctx, depth).strip()
                else:
                    rest.append(k)
            out = ''
            if title:
                hashes = '#' * min(ctx.get('base_level', 3) + depth, 6)
                heading = f'{label} {title}'.strip() if label else title
                out += f'\n\n{hashes} {heading}\n\n'
            out += ''.join(cls._xocs_render(k, ctx, depth + 1) for k in rest)
            return out

        if name == 'section-title':
            return (text or inner() or '').strip()

        if name in ('appendices', 'acknowledgment', 'conflict-of-interest',
                    'ack', 'appendix'):
            titles = {
                'acknowledgment': 'Acknowledgements',
                'ack': 'Acknowledgements',
                'conflict-of-interest': 'Declaration of competing interest',
                'appendices': 'Appendix',
                'appendix': 'Appendix',
            }
            hashes = '#' * min(ctx.get('base_level', 3), 6)
            has_own_title = any(
                (k.get('#name') == 'section-title') for k in (kids or [])
            )
            head = '' if has_own_title else f'\n\n{hashes} {titles.get(name, name)}\n\n'
            return head + inner(depth)

        if name in ('para', 'note-para'):
            body = inner().strip()
            return f'\n\n{body}\n\n' if body else ''

        # ---- lists ----
        if name == 'list':
            items = [k for k in (kids or []) if k.get('#name') == 'list-item']
            pieces = []
            for i, item in enumerate(items, 1):
                pieces.append(cls._xocs_render_list_item(item, ctx, depth, i))
            return '\n' + '\n'.join(p for p in pieces if p) + '\n'

        if name == 'list-item':
            return cls._xocs_render_list_item(node, ctx, depth, 1)

        # ---- figures / floats ----
        if name == 'inline-figure':
            return cls._xocs_render_inline_figure(node, ctx)

        if name == 'float-anchor':
            refid = (attrs.get('refid') or '').strip()
            return cls._xocs_render_float(refid, ctx, depth)

        if name in ('figure', 'table'):
            fid = (attrs.get('id') or '').strip()
            return cls._xocs_render_float_node(node, ctx, depth, fid)

        if name == 'e-component':
            return cls._xocs_render_e_component(node, ctx)

        if name == 'link':
            # A bare link outside inline-figure — resolve to an image if we can.
            loc = (attrs.get('locator') or '').strip()
            hit = ctx['attachments'].get(loc)
            if hit and hit['type'] != 'APPLICATION':
                ctx['inline_images'].append(hit['url'])
                return f'\n\n![]({hit["url"]})\n\n'
            return ''

        if name in cls._XOCS_TRANSPARENT or name in ('body', 'content'):
            return inner(depth)

        if name == 'label':
            return (text or inner() or '').strip()

        # Unknown node — render children so nothing silently disappears.
        return inner(depth)

    @classmethod
    def _xocs_render_list_item(cls, item, ctx: dict, depth: int, ordinal: int) -> str:
        """Render one <list-item>, preserving nesting via indentation."""
        label, body_parts = '', []
        for k in (item.get('$$') or []):
            if k.get('#name') == 'label' and not label:
                label = (k.get('_') or '').strip()
            else:
                body_parts.append(cls._xocs_render(k, ctx, depth + 1))
        body = ''.join(body_parts).strip()
        if not body and not label:
            return ''
        indent = '    ' * ctx.get('list_depth', 0)
        bullet = label if label else f'{ordinal}.'
        # Collapse the leading blank lines a nested <para> introduces, then
        # re-indent continuation lines so the markdown list stays intact.
        lines = [ln for ln in body.split('\n')]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return ''
        first, rest = lines[0], lines[1:]
        out = f'{indent}- {bullet} {first}'.rstrip()
        for ln in rest:
            out += f'\n{indent}  {ln}' if ln.strip() else '\n'
        return out

    @classmethod
    def _xocs_render_inline_figure(cls, node, ctx: dict) -> str:
        """<inline-figure><link locator="fx003"/></inline-figure> → ![](url).

        These are the un-numbered in-text illustrations (program listings,
        algorithm cycles) that carry no Fig. N label.
        """
        loc = ''
        for k in (node.get('$$') or []):
            if k.get('#name') == 'link':
                loc = ((k.get('$') or {}).get('locator') or '').strip()
                break
        if not loc:
            return ''
        hit = ctx['attachments'].get(loc)
        if not hit:
            return ''
        ctx['inline_images'].append(hit['url'])
        return f'\n\n![]({hit["url"]})\n\n'

    @classmethod
    def _xocs_render_float(cls, refid: str, ctx: dict, depth: int) -> str:
        """Render the float referenced by a <float-anchor refid="…"/>."""
        if not refid:
            return ''
        node = ctx['floats'].get(refid)
        if node is None or refid in ctx['emitted_floats']:
            return ''
        return cls._xocs_render_float_node(node, ctx, depth, refid)

    @classmethod
    def _xocs_render_float_node(cls, node, ctx: dict, depth: int, fid: str) -> str:
        """Render a <figure> or <table> float: label + caption + content."""
        if fid:
            if fid in ctx['emitted_floats']:
                return ''
            ctx['emitted_floats'].add(fid)

        kind = node.get('#name')
        label, caption_md, locator, table_node = '', '', '', None
        footnotes = []
        sub_floats = []
        for k in (node.get('$$') or []):
            kn = k.get('#name')
            if kn == 'label' and not label:
                label = (k.get('_') or '').strip()
            elif kn == 'caption':
                caption_md = cls._xocs_render(k, ctx, depth).strip()
            elif kn == 'link' and not locator:
                locator = ((k.get('$') or {}).get('locator') or '').strip()
            elif kn == 'tgroup':
                table_node = k
            elif kn in ('figure', 'table'):
                # Composite float: a parent <figure> holding its own overall
                # label/caption plus one nested <figure> per panel, each with
                # its own label ("Fig. 4(a)"), caption and image link. The
                # parent has NO direct <link>, so without this the whole
                # group rendered as a bare caption and every panel image was
                # dropped (e.g. gr4a–gr4d in 10.1016/j.cpc.2018.03.018).
                sub_floats.append(k)
            elif kn in ('table-footnote', 'legend'):
                fn = cls._xocs_render(k, ctx, depth).strip()
                if fn:
                    footnotes.append(fn)

        caption_md = re.sub(r'\s+', ' ', caption_md).strip()
        out = '\n\n'
        if label and caption_md:
            out += f'**{label}.** {caption_md}\n\n'
        elif label:
            out += f'**{label}.**\n\n'
        elif caption_md:
            out += f'**{caption_md}**\n\n'

        if kind == 'figure' and locator:
            hit = ctx['attachments'].get(locator)
            if hit:
                alt = label or 'Figure'
                out += f'![{alt}]({hit["url"]})\n\n'
                ctx['figures'].append({
                    'id': fid, 'label': label,
                    'caption': caption_md, 'url': hit['url'],
                })
        elif kind == 'table' and table_node is not None:
            tbl = cls._xocs_table_to_md(table_node, ctx, depth)
            if tbl:
                out += tbl + '\n\n'

        # Render nested panels after the group's own label/caption so each
        # sub-figure contributes its image and its own downloadable entry.
        for sub in sub_floats:
            sub_id = ((sub.get('$') or {}).get('id') or '').strip()
            out += cls._xocs_render_float_node(sub, ctx, depth, sub_id)

        for fn in footnotes:
            out += f'**Note:** {fn}\n\n'
        return out

    @classmethod
    def _xocs_table_to_md(cls, tgroup, ctx: dict, depth: int) -> str:
        """Convert a CALS <tgroup> to a Markdown table."""
        def cells(row):
            out = []
            for e in (row.get('$$') or []):
                if e.get('#name') != 'entry':
                    continue
                txt = cls._xocs_render(e, ctx, depth).strip()
                txt = re.sub(r'\s+', ' ', txt).replace('|', r'\|')
                out.append(txt)
            return out

        header, body = [], []
        for part in (tgroup.get('$$') or []):
            pn = part.get('#name')
            if pn == 'thead':
                for r in (part.get('$$') or []):
                    if r.get('#name') == 'row':
                        header.append(cells(r))
            elif pn == 'tbody':
                for r in (part.get('$$') or []):
                    if r.get('#name') == 'row':
                        body.append(cells(r))

        rows = header + body
        if not rows:
            return ''
        width = max(len(r) for r in rows)
        rows = [r + [''] * (width - len(r)) for r in rows]

        lines = []
        if header:
            lines.append('| ' + ' | '.join(rows[0]) + ' |')
            lines.append('|' + '|'.join(['---'] * width) + '|')
            data = rows[1:]
        else:
            lines.append('| ' + ' | '.join([''] * width) + ' |')
            lines.append('|' + '|'.join(['---'] * width) + '|')
            data = rows
        for r in data:
            lines.append('| ' + ' | '.join(r) + ' |')
        return '\n'.join(lines)

    @classmethod
    def _xocs_render_e_component(cls, node, ctx: dict) -> str:
        """<e-component> — a supplemental-material reference in the body."""
        attrs = node.get('$') or {}
        label, caption, locator = '', '', (attrs.get('id') or '').strip()
        for k in (node.get('$$') or []):
            kn = k.get('#name')
            if kn == 'label' and not label:
                label = (k.get('_') or '').strip()
            elif kn == 'caption':
                caption = cls._xocs_render(k, ctx, 0).strip()
            elif kn == 'link':
                loc = ((k.get('$') or {}).get('locator') or '').strip()
                if loc:
                    locator = loc
        hit = ctx['attachments'].get(locator) if locator else None
        if hit:
            ctx['supplemental'].append({
                'url': hit['url'],
                'label': label or locator,
                'caption': caption,
                'filename': hit['filename'],
            })
        bits = [b for b in (f'**{label}**' if label else '', caption) if b]
        return ('\n\n' + ' '.join(bits) + '\n\n') if bits else ''

    @staticmethod
    def _xocs_footnote_index(footnotes: list) -> dict:
        """Map footnote id (fn1, fn2, …) → sequential 1-based marker number.

        The source labels can't be used as markers: Elsevier cycles †/‡ so
        the same symbol repeats many times per article (fn1=†, fn2=‡,
        fn3=† …). Numbering by array position gives each footnote a unique
        markdown marker while preserving document order.
        """
        idx = {}
        for node in footnotes or []:
            fid = ((node.get('$') or {}).get('id') or '').strip()
            if fid and fid not in idx:
                idx[fid] = len(idx) + 1
        return idx

    @classmethod
    def _xocs_render_footnotes(cls, footnotes: list, ctx: dict) -> str:
        """Render the footnote array as pandoc-style ``[^N]: …`` definitions.

        A footnote can hold several ``<note-para>`` blocks, each with its own
        display equations (e.g. fn33 in 10.1016/B978-0-08-030275-1.50008-4
        carries three). Continuation lines are indented four spaces so the
        whole block stays bound to its marker instead of breaking out into
        body text after the first line.
        """
        numbers = ctx.get('footnote_numbers') or {}
        out = []
        for node in footnotes or []:
            fid = ((node.get('$') or {}).get('id') or '').strip()
            num = numbers.get(fid)
            if not num:
                continue
            # Skip <label> (†/‡) — the markdown marker replaces that glyph.
            body = ''.join(
                cls._xocs_render(k, ctx, 0)
                for k in (node.get('$$') or [])
                if k.get('#name') != 'label'
            )
            body = re.sub(r'[ \t]+\n', '\n', body)
            body = re.sub(r'\n{3,}', '\n\n', body).strip()
            if not body:
                continue
            lines = body.split('\n')
            first = lines[0].strip()
            rest = [('    ' + ln.rstrip()) if ln.strip() else '' for ln in lines[1:]]
            block = f'[^{num}]: {first}'
            if rest:
                block += '\n' + '\n'.join(rest)
            out.append(block)
        return '\n\n'.join(out)

    # ---- top-level body-JSON driver -----------------------------------

    @classmethod
    def render_body_json(cls, body_json: dict, base_level: int = 3) -> dict:
        """Render a body-API JSON payload into markdown + asset links.

        Returns ``{'body_md', 'footnotes_md', 'figure_urls',
        'supplemental_urls', 'supplemental_descriptions', 'inline_images'}``.
        """
        if not isinstance(body_json, dict):
            return {
                'body_md': '', 'footnotes_md': '', 'figure_urls': {},
                'supplemental_urls': [], 'supplemental_descriptions': {},
                'inline_images': [],
            }

        attachments = cls._sd_attachment_index(body_json.get('attachments') or [])
        ctx = {
            'attachments': attachments,
            'floats': cls._sd_float_index(body_json.get('floats') or []),
            'base_level': base_level,
            'list_depth': 0,
            'emitted_floats': set(),
            'figures': [],
            'supplemental': [],
            'inline_images': [],
            # Footnote ids are resolved before the walk so a cross-ref
            # encountered mid-body already knows its marker number.
            'footnote_numbers': cls._xocs_footnote_index(
                body_json.get('footnotes') or []
            ),
            'footnotes_used': set(),
        }

        body_md = cls._xocs_render(body_json.get('content') or [], ctx, 0)

        # Floats never anchored in the body still belong in the output.
        trailing = []
        for fid, fnode in ctx['floats'].items():
            if fid not in ctx['emitted_floats']:
                trailing.append(cls._xocs_render_float_node(fnode, ctx, 0, fid))
        if any(t.strip() for t in trailing):
            body_md += '\n\n' + ''.join(trailing)

        # Normalise whitespace: collapse 3+ blank lines, trim trailing spaces.
        body_md = re.sub(r'[ \t]+\n', '\n', body_md)
        body_md = re.sub(r'\n{3,}', '\n\n', body_md).strip()

        # Figures → the {'fig_N': {...}} shape the download pipeline expects.
        figure_urls = {}
        for i, fig in enumerate(ctx['figures'], 1):
            figure_urls[f'fig_{i}'] = {
                'url': fig['url'],
                'caption': fig['caption'],
                'label': fig['label'],
            }
        # Un-numbered inline illustrations (fx001.gif …) download too.
        seen_urls = {v['url'] for v in figure_urls.values()}
        for url in ctx['inline_images']:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            figure_urls[f'fig_{len(figure_urls) + 1}'] = {
                'url': url, 'caption': '', 'label': '', 'inline': True,
            }

        # Supplemental: body-referenced <e-component>s first, then any
        # APPLICATION attachment that wasn't referenced inline.
        supp_urls, supp_desc = [], {}
        for s in ctx['supplemental']:
            if s['url'] in supp_desc:
                continue
            supp_urls.append(s['url'])
            supp_desc[s['url']] = (s['caption'] or s['label']
                                   or s['filename'] or 'Supplementary material')
        for key, hit in attachments.items():
            if hit['type'] != 'APPLICATION' or hit['url'] in supp_desc:
                continue
            supp_urls.append(hit['url'])
            supp_desc[hit['url']] = hit['filename'] or key

        footnotes_md = cls._xocs_render_footnotes(
            body_json.get('footnotes') or [], ctx
        )

        return {
            'body_md': body_md,
            'footnotes_md': footnotes_md,
            'figure_urls': figure_urls,
            'supplemental_urls': supp_urls,
            'supplemental_descriptions': supp_desc,
            'inline_images': ctx['inline_images'],
        }

    async def _fetch_body_json(self, page, html_content: str) -> dict:
        """Fetch + cache the sdfe/arp body JSON for the current article.

        Returns the parsed payload, or ``{}`` when the token / PII can't be
        resolved or the request fails (caller then falls back to the legacy
        DOM walk).
        """
        try:
            page_url = page.url or ''
        except Exception:
            page_url = ''

        pii = self._extract_pii(html_content, page_url) or self._extract_pii(html_content)
        token = self._extract_entitled_token(html_content)
        if not pii or not token:
            print(f"  ⚠️  body API 跳过：pii={'✓' if pii else '✗'} token={'✓' if token else '✗'}")
            return {}

        api_url = self._body_api_url(pii, token)
        print(f"  ↪ 请求正文 API: /sdfe/arp/pii/{pii}/body")

        # Issue the request from INSIDE the page via fetch(). Playwright's
        # APIRequestContext shares cookies but not the page's JS/TLS
        # fingerprint, and Elsevier answers it with 403. A same-origin
        # in-page fetch inherits the exact session that just rendered the
        # article, so it is accepted.
        payload = None
        try:
            payload = await page.evaluate(
                """async (url) => {
                    try {
                        const r = await fetch(url, {
                            method: 'GET',
                            credentials: 'include',
                            headers: {'Accept': 'application/json'},
                        });
                        if (!r.ok) return {__err: 'status ' + r.status};
                        return await r.json();
                    } catch (e) {
                        return {__err: String(e)};
                    }
                }""",
                api_url,
            )
        except Exception as exc:
            print(f"  ⚠️  body API in-page fetch 异常: {type(exc).__name__}: {str(exc)[:120]}")
            payload = None

        if isinstance(payload, dict) and payload.get('__err'):
            print(f"  ⚠️  body API in-page fetch 失败: {payload['__err']}")
            payload = None

        # Fall back to the out-of-page request context (works on some
        # mirrors / when the page navigated away mid-flight).
        if payload is None:
            try:
                resp = await page.context.request.get(
                    api_url,
                    headers={
                        'Accept': 'application/json',
                        'Referer': page_url or f'https://www.sciencedirect.com/science/article/pii/{pii}',
                    },
                    timeout=60000,
                )
                if not resp.ok:
                    print(f"  ⚠️  body API 返回 {resp.status}")
                    return {}
                payload = await resp.json()
            except Exception as exc:
                print(f"  ⚠️  body API 请求失败: {type(exc).__name__}: {str(exc)[:120]}")
                return {}

        if not isinstance(payload, dict) or not payload.get('content'):
            print("  ⚠️  body API 响应缺少 content")
            return {}

        # Cache alongside page.html so the JSON can be re-inspected offline.
        try:
            if self.captured_data_dir:
                out = Path(self.captured_data_dir) / 'body.json'
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding='utf-8',
                )
                print(f"  ✓ body.json 已保存 ({out.stat().st_size:,} bytes)")
        except Exception:
            pass

        return payload

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

            # ---- PRIMARY: body JSON API --------------------------------
            # Preferred over the DOM walk because the API payload still
            # carries source MathML; the rendered page (MathJax 3 CHTML
            # since ~2023) has every LaTeX/MathML annotation stripped.
            body_json = await self._fetch_body_json(page, fulltext_html)
            if body_json:
                rendered = self.render_body_json(body_json)
                if rendered.get('body_md'):
                    metadata['_body_md'] = rendered['body_md']
                    if rendered.get('footnotes_md'):
                        metadata['_footnotes_md'] = rendered['footnotes_md']
                    figure_urls = rendered['figure_urls']
                    supp_urls = rendered['supplemental_urls']
                    supp_descriptions = rendered['supplemental_descriptions']
                    n_fn = rendered.get('footnotes_md', '').count('\n[^') + (
                        1 if rendered.get('footnotes_md') else 0
                    )
                    print(
                        f"  ✓ 正文来自 body API: {len(rendered['body_md']):,} 字符, "
                        f"{len(figure_urls)} 图, {len(supp_urls)} 补充材料, "
                        f"{n_fn} 脚注"
                    )

            # ---- FALLBACK: legacy DOM walk -----------------------------
            if not metadata.get('_body_md') and fulltext_html:
                print("  ↪ 回退到 DOM 提取路径")
                figure_urls = self.extract_figures_from_html(fulltext_html)
                supp_urls, supp_descriptions = self._extract_supplemental_from_html(
                    fulltext_html
                )

            if fulltext_html:
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
        # Body from the sdfe/arp JSON API (rendered in extract_all) wins —
        # it carries source MathML, so equations are real LaTeX. The HTML
        # walk below only runs when the API path was unavailable.
        api_body = (metadata.get('_body_md') or '').strip()
        if isinstance(article_text, str) and article_text.strip():
            if article_text.lstrip().startswith('<'):
                abstract_md, html_body_md = self.extract_article_text_from_html(article_text)
                body_md = api_body or html_body_md
            else:
                body_md = api_body or article_text.strip()
        else:
            body_md = api_body

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

        # Footnotes: pandoc-style ``[^N]: …`` definitions matching the
        # ``[^N]`` markers already inlined in the body. Placed after the
        # article text so the definitions sit next to their references.
        footnotes_md = (metadata.get('_footnotes_md') or '').strip()
        if footnotes_md:
            md_parts.extend(["---", "", "## Footnotes", "", footnotes_md, ""])

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

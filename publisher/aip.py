"""
AIP Publishing handler skeleton.

This module wires AIP into the shared PublisherHandler contract. Detailed
body/reference extraction for pubs.aip.org is intentionally left for a future
pass.
"""

import re
from urllib.parse import unquote

from bs4 import BeautifulSoup, NavigableString
from playwright.async_api import async_playwright

from html_to_md_converter import (
    cleanup_markdown,
    convert_html_to_markdown,
    mathml_to_latex_pandoc,
    remove_newlines_in_paragraph,
)
from publisher.base import PublisherHandler
from publisher.wildcard import set_actual_base_url, init_extract_all_page


class AIPHandler(PublisherHandler):
    """Handler interface for AIP Publishing articles."""

    PUBLISHER = 'aip'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)

    @staticmethod
    def _extract_metadata_from_html_meta(html_content: str) -> dict:
        """Extract AIP metadata from citation_* <meta> tags in the HTML head."""
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')
        meta = {}
        authors = []
        author_institutions = []

        for tag in soup.find_all('meta'):
            name = tag.get('name', '')
            content = tag.get('content', '')
            if not name or not content:
                continue

            if name == 'citation_author':
                authors.append(content.strip())
            elif name == 'citation_author_institution':
                author_institutions.append(content.strip())
            elif name == 'citation_title':
                meta['title'] = content.strip()
            elif name == 'citation_doi':
                meta['doi'] = content.strip()
            elif name == 'citation_journal_title':
                meta['journal'] = content.strip()
            elif name == 'citation_volume':
                meta['volume'] = content.strip()
            elif name == 'citation_issue':
                meta['issue'] = content.strip()
            elif name == 'citation_publication_date':
                date_str = content.strip()
                meta['publication_date'] = date_str
                if date_str and '/' in date_str:
                    meta['year'] = date_str.split('/')[0]
            elif name == 'citation_pdf_url':
                meta['pdf_url'] = content.strip()

        if authors:
            meta['authors'] = authors

        author_with_affiliations = []
        for i, author in enumerate(authors):
            aff_list = []
            if i < len(author_institutions):
                aff_list = [author_institutions[i]]
            author_with_affiliations.append({
                'author': author,
                'affiliations': aff_list,
            })
        if author_with_affiliations:
            meta['author_with_affiliations'] = author_with_affiliations

        return meta

    async def extract_metadata(self, page) -> dict:
        """Return metadata from HTML citation meta tags, with abstract from page body."""
        html_content = await self.get_page_html(page) if page is not None else ''

        meta = self._extract_metadata_from_html_meta(html_content)
        # Physics Today is the same publisher on a different CMS: no
        # citation_* meta tags at all, so _extract_metadata_from_html_meta
        # returns {} and every field below would fall back to its placeholder.
        if html_content:
            meta.update(self._pt_metadata_from_html(html_content))

        abstract = ''
        if html_content:
            try:
                abstract = self.extract_main_abstract_from_html(html_content)
            except Exception:
                abstract = ''

        if not abstract:
            abstract = meta.get('abstract') or ''

        return {
            'title': meta.get('title') or 'AIP Article',
            'authors': meta.get('authors', []),
            'author_with_affiliations': meta.get('author_with_affiliations', []),
            'corresponding_author_emails': [],
            'abstract': abstract,
            'journal': meta.get('journal') or 'AIP Publishing',
            'publication_date': meta.get('publication_date'),
            'doi': meta.get('doi') or self.doi,
            'volume': meta.get('volume'),
            'issue': meta.get('issue'),
            'pages': meta.get('pages'),
            'year': meta.get('year'),
            'references': [],
            '_pdf_url': meta.get('pdf_url'),
        }

    @classmethod
    def extract_main_abstract_from_html(cls, html_content: str) -> str:
        """Extract AIP Main abstract as Markdown (convenience wrapper)."""
        abstract_md, _ = cls.extract_article_text_from_html(html_content)
        return abstract_md

    @staticmethod
    def _strip_inline_math_delimiters(latex: str) -> str:
        """Return the body of a single inline math expression."""
        latex = (latex or '').strip()
        if latex.startswith('$') and latex.endswith('$') and not latex.startswith('$$'):
            return latex[1:-1].strip()
        return latex

    @classmethod
    def _convert_aip_mathml(cls, math_tag, display: bool = False) -> str:
        latex = mathml_to_latex_pandoc(str(math_tag))
        if not latex:
            return ''

        if display:
            # ⚠️ A <math display="block"> already comes back from pandoc as
            # $$…$$ (Physics Today's JATS declares the attribute; Silverchair
            # does not). Wrapping it again produced an empty $$ pair above and
            # below the equation.
            if latex.startswith('$$') and latex.endswith('$$'):
                return latex
            latex_body = cls._strip_inline_math_delimiters(latex)
            return f"$$\n{latex_body}\n$$"
        return latex

    @classmethod
    def _prepare_aip_html_fragment(cls, html_fragment: str) -> tuple[str, list[str]]:
        """Collapse AIP MathJax markup to placeholders before pandoc."""
        soup = BeautifulSoup(html_fragment, 'html.parser')
        formulas = []

        def stash_formula(latex: str) -> str:
            formulas.append(latex)
            return f"AIPMATH{len(formulas) - 1:03d}MATHEND"

        for tag in soup(['script', 'style', 'noscript']):
            tag.decompose()

        # Collapse xref-bibr links to plain [number] brackets.
        for a_tag in soup.select('a.xref-bibr'):
            sup_tag = a_tag.find('sup')
            ref_text = sup_tag.get_text(' ', strip=True) if sup_tag else a_tag.get_text(' ', strip=True)
            if ref_text:
                a_tag.replace_with(NavigableString(f"[{ref_text}]"))

        # Physics Today (physicstoday.aip.org) ships JATS leftovers inside a
        # Brightspot page: citation markers are <span class="References"><sup>
        # <a href="#ref8">8</a>, or a bare <xref ref-type="bibr"><sup>13</sup>
        # (the latter lives in figure captions). Both become [N] -- the same
        # shape a.xref-bibr gets above.
        for marker in soup.select('span.References'):
            ref_text = marker.get_text(' ', strip=True)
            marker.replace_with(NavigableString(f"[{ref_text}]" if ref_text else ''))
        for marker in soup.find_all('xref'):
            ref_text = marker.get_text(' ', strip=True)
            # ⚠️ Only a bibliographic xref becomes [N]. A table/figure xref is
            # a word inside the sentence -- "(see table)" must not turn into
            # "(see [table])", which pandoc then escapes to "\[table\]".
            bracket = (marker.get('ref-type') or '') == 'bibr'
            marker.replace_with(
                NavigableString((f"[{ref_text}]" if bracket else ref_text) if ref_text else '')
            )

        # Icons and in-page anchors: an <svg><use> renders as nothing useful,
        # and a cross-reference to "#image-040004" is dead in a .md file, so
        # keep the anchor's text (the figure number the sentence reads).
        for svg in soup.find_all('svg'):
            svg.decompose()
        for a_tag in soup.find_all('a', href=True):
            if a_tag['href'].startswith('#'):
                a_tag.replace_with(NavigableString(a_tag.get_text(' ', strip=True)))
                continue
            # pandoc keeps attributes it does not understand and writes them
            # as a brace suffix: [March 2005, page 39](…){rel="noopener"
            # target="_blank"}.
            for attr in ('rel', 'target'):
                if attr in a_tag.attrs:
                    del a_tag[attr]

        # Inline formulas are rendered as large MathJax CHTML trees with
        # assistive MathML. Keep only the MathML-derived LaTeX.
        for formula in soup.select('span.inline-formula'):
            math_tag = formula.find('math')
            latex = cls._convert_aip_mathml(math_tag) if math_tag else ''
            if latex:
                formula.replace_with(NavigableString(f" {stash_formula(latex)} "))

        for container in soup.find_all('mjx-container'):
            math_tag = container.find('math')
            latex = cls._convert_aip_mathml(math_tag) if math_tag else ''
            if latex:
                container.replace_with(NavigableString(f" {stash_formula(latex)} "))

        # A bare <math> with no MathJax wrapper around it -- Physics Today's
        # inline math. Without this it reaches pandoc as MathML and comes out
        # as the accessibility read-aloud string.
        for math_tag in soup.find_all('math'):
            latex = cls._convert_aip_mathml(math_tag)
            if latex:
                math_tag.replace_with(NavigableString(f" {stash_formula(latex)} "))

        return str(soup), formulas

    @classmethod
    def _convert_aip_html_fragment_to_markdown(cls, html_fragment: str) -> str:
        prepared_html, formulas = cls._prepare_aip_html_fragment(html_fragment)
        md = convert_html_to_markdown(prepared_html)
        for index, latex in enumerate(formulas):
            md = md.replace(f"AIPMATH{index:03d}MATHEND", latex)
        md = cleanup_markdown(md)
        md = remove_newlines_in_paragraph(md, "", "p")
        md = re.sub(r'\s+', ' ', md).strip()
        return md

    @classmethod
    def _convert_aip_display_formula(cls, wrapper) -> str:
        math_tag = wrapper.find('math')
        if not math_tag:
            return ''

        md = cls._convert_aip_mathml(math_tag, display=True)
        # Silverchair marks the equation number with class="label"; the JATS
        # markup Physics Today embeds uses a <label> tag instead.
        label = wrapper.find(class_='label') or wrapper.find('label')
        if label:
            label_text = label.get_text(' ', strip=True)
            if label_text:
                md = f"{md}\n\n{label_text}"
        return md.strip()

    @classmethod
    def _convert_aip_figure(cls, wrapper) -> str:
        """Render one AIP figure (label + caption) to markdown.

        ``wrapper`` may be the ``div.fig-section`` itself or an element that
        contains one.

        ⚠️ Every lookup here is **tag-agnostic on purpose**. AIP's caption is a
        ``<figcaption class="caption fig-caption">``, not a ``<div>`` -- the
        walker used to ask for ``find('div', class_='caption')`` and got None
        for every figure in the paper, so the Markdown carried a bare
        ``**FIG. 1.**`` with the caption text silently dropped. Match on the
        class, let the tag be whatever Silverchair ships.
        """
        fig = (wrapper if 'fig-section' in (wrapper.get('class') or [])
               else wrapper.select_one('div.fig-section'))
        if not fig:
            return ''

        label = fig.select_one('.fig-label')
        caption = fig.select_one('.caption')
        parts = []
        if label:
            label_text = label.get_text(' ', strip=True)
            if label_text:
                parts.append(f"**{label_text}**")
        if caption:
            caption_md = cls._convert_aip_html_fragment_to_markdown(str(caption))
            if caption_md:
                parts.append(caption_md)
        return " ".join(parts).strip()

    @classmethod
    def _convert_aip_table_to_md(cls, table_wrap, writer: str = 'md') -> str:
        """Convert an AIP ``div.table-wrap`` element to markdown table.

        Pre-processes MathJax ``mjx-container`` markup in table cells before
        converting the ``<table>`` element to markdown via pandoc.
        """
        # Label and caption. ⚠️ Both the class and the tag have to be
        # accepted: Silverchair wraps the table in div.table-wrap with
        # span.label / .caption, while the JATS markup Physics Today embeds
        # in its Brightspot page uses <table-wrap> with <label> / <caption>.
        label_span = table_wrap.find('span', class_='label') or table_wrap.find('label')
        label = label_span.get_text(' ', strip=True) if label_span else ''

        caption_div = table_wrap.select_one('.caption') or table_wrap.find('caption')
        caption = caption_div.get_text(' ', strip=True) if caption_div else ''

        # Silverchair puts the table inside div.table-overflow; JATS has it as
        # a direct child. Ask for the <table> itself -- that is the one thing
        # both shapes agree on.
        table = table_wrap.find('table')
        if not table:
            return ''

        # Stash mjx-container formulas as LaTeX placeholders
        formulas = []
        for mjx in table.find_all('mjx-container'):
            math_tag = mjx.find('math')
            latex = cls._convert_aip_mathml(math_tag) if math_tag else ''
            if latex:
                placeholder = f"AIPMATH{len(formulas):03d}MATHEND"
                formulas.append(latex)
                mjx.replace_with(placeholder)

        # Remove scripts, styles, noscripts
        for tag in table(['script', 'style', 'noscript']):
            tag.decompose()

        # Convert xref-bibr links to plain [N] markers
        for a_tag in table.find_all('a', class_='xref-bibr'):
            sup = a_tag.find('sup')
            ref_text = sup.get_text(' ', strip=True) if sup else a_tag.get_text(' ', strip=True)
            if ref_text:
                a_tag.replace_with(f"[{ref_text}]")

        # Unwrap inline-formula spans (keep inner content)
        for span in table.find_all('span', class_='inline-formula'):
            span.unwrap()

        # Remove empty spans (e.g. <span class="mathFormula"></span>)
        for tag in table.find_all(lambda t: t.name == 'span' and not t.get_text(strip=True)):
            tag.decompose()

        # Convert <table> to markdown via pandoc
        import pypandoc
        table_html = str(table)
        md = pypandoc.convert_text(table_html, writer, format='html', extra_args=['--wrap=none'])
        md = md.strip()

        # Restore LaTeX placeholders
        for idx, latex in enumerate(formulas):
            md = md.replace(f"AIPMATH{idx:03d}MATHEND", latex)

        # Extract table footnotes from div.table-wrap-foot (JATS:
        # <table-wrap-foot><fn><label>)
        foot_div = (table_wrap.find('div', class_='table-wrap-foot')
                    or table_wrap.find('table-wrap-foot'))
        footnote_lines = []
        if foot_div:
            for fn in (foot_div.find_all('div', class_='fn')
                       or foot_div.find_all('fn')):
                label_tag = fn.find('span', class_='fn-label') or fn.find('label')
                fn_label = label_tag.get_text(strip=True) if label_tag else ''
                p_tag = fn.find('p')
                if p_tag:
                    fn_md = cls._convert_aip_html_fragment_to_markdown(str(p_tag))
                    if fn_md:
                        prefix = f"^{fn_label}^ " if fn_label else ''
                        footnote_lines.append(f"{prefix}{fn_md}")

        # Combine header + table + footnotes
        header = f"**{label}** {caption}" if label else f"**{caption}**"
        result = f"\n{header}\n\n{md}\n"
        if footnote_lines:
            result += "\n" + "\n\n".join(footnote_lines) + "\n"
        return result

    @classmethod
    def _convert_aip_block_child_p(cls, block_div) -> str:
        """Convert AIP ``div.block-child-p`` to markdown.

        This element acts like a paragraph but can contain embedded
        ``div.formula-wrap`` display formulas and inline MathJax.
        Extract both and combine them in order.
        """
        # Extract display formulas and replace with placeholders
        display_formulas = []
        for fw in block_div.find_all('div', class_='formula-wrap'):
            formula_md = cls._convert_aip_display_formula(fw)
            if formula_md:
                placeholder = f"AIPDISPF{len(display_formulas):03d}MATHEND"
                display_formulas.append((placeholder, formula_md))
                fw.replace_with(placeholder)

        # Convert remaining content (text + inline MathJax) to markdown
        text_md = cls._convert_aip_html_fragment_to_markdown(str(block_div))

        # Restore display formulas (each on its own line)
        for placeholder, formula_md in display_formulas:
            text_md = text_md.replace(placeholder, f"\n\n{formula_md}")

        return text_md.strip()

    @classmethod
    def _walk_aip_section(cls, section, body_parts: list) -> None:
        """Walk direct children of an article-section-wrapper in document order.

        Older AIP renderings mix ``<p>``, a multi-``<div class="formula-wrap">``
        ``<div class="disp-formula">`` group, and loose ``NavigableString`` +
        inline-formula text (all as siblings of the section wrapper) — the
        previous select_one / early-return dispatch dropped everything after
        the first matched block type.
        """
        inline_buffer: list[str] = []

        def flush_inline():
            if not inline_buffer:
                return
            joined = ''.join(inline_buffer).strip()
            inline_buffer.clear()
            if not joined:
                return
            md = cls._convert_aip_html_fragment_to_markdown(f"<p>{joined}</p>")
            if md:
                body_parts.extend([md, ""])

        for child in section.children:
            if isinstance(child, NavigableString):
                text = str(child)
                if text.strip():
                    inline_buffer.append(text)
                continue
            if not getattr(child, 'name', None):
                continue

            classes = child.get('class') or []

            if child.name == 'p':
                flush_inline()
                md = cls._convert_aip_html_fragment_to_markdown(str(child))
                if md:
                    body_parts.extend([md, ""])
                continue

            if child.name in ('ul', 'ol'):
                flush_inline()
                md = cls._convert_aip_list(child)
                if md:
                    body_parts.extend([md, ""])
                continue

            if child.name == 'div':
                if 'block-child-p' in classes:
                    flush_inline()
                    md = cls._convert_aip_block_child_p(child)
                    if md:
                        body_parts.extend([md, ""])
                    continue

                if 'disp-formula' in classes:
                    flush_inline()
                    # A disp-formula may wrap several <div class="formula-wrap">
                    # under a shared (N) label. Emit each equation on its own,
                    # attaching the label to the first.
                    label_el = child.find('span', class_='label')
                    label_text = label_el.get_text(' ', strip=True) if label_el else ''
                    wraps = child.find_all('div', class_='formula-wrap', recursive=False)
                    if not wraps:
                        wraps = [child]
                    for i, wrap in enumerate(wraps):
                        fm = cls._convert_aip_display_formula(wrap)
                        if not fm:
                            continue
                        if i == 0 and label_text and label_text not in fm:
                            fm = f"{fm}\n\n{label_text}"
                        body_parts.extend([fm, ""])
                    continue

                if 'formula-wrap' in classes:
                    flush_inline()
                    md = cls._convert_aip_display_formula(child)
                    if md:
                        body_parts.extend([md, ""])
                    continue

                if 'table-wrap' in classes:
                    flush_inline()
                    md = cls._convert_aip_table_to_md(child)
                    if md:
                        body_parts.extend([md, ""])
                    continue

                if 'fig-section' in classes:
                    flush_inline()
                    # 📌 One definition of "how an AIP figure renders".
                    # This branch used to carry its own copy, and that copy
                    # was the one that rotted (it asked for a <div> caption);
                    # _convert_aip_figure had the right lookups and no caller
                    # at all, so fixing it would have changed nothing.
                    md = cls._convert_aip_figure(child)
                    if md:
                        body_parts.extend([md, ""])
                    continue

                if 'fig-modal' in classes or 'reveal-modal' in classes:
                    # Duplicate of the fig-section rendered as a lightbox
                    # modal — the real figure was already emitted above.
                    continue

            # Anything else (unknown div, <span>, <a>, <em>, ...) → buffer as
            # inline so trailing "Here <span>ε</span> is the …" text after a
            # display formula still ends up in a paragraph.
            inline_buffer.append(str(child))

        flush_inline()

    @classmethod
    def _convert_aip_list(cls, list_tag, level: int = 0) -> str:
        """Convert <ul>/<ol> to markdown, recursing into nested lists.

        Each ``<li>`` may contain a ``<p>`` plus more lists.  The shared
        fragment converter collapses whitespace, so we process every
        non-list child of the ``<li>`` as one fragment and recurse into
        nested ``<ul>``/``<ol>`` separately.
        """
        is_ordered = list_tag.name == 'ol'
        indent = '    ' * level
        lines = []
        counter = 0
        for li in list_tag.find_all('li', recursive=False):
            counter += 1
            marker = f"{counter}." if is_ordered else "-"

            nested_lists = [
                c for c in li.find_all(['ul', 'ol'], recursive=False)
            ]

            # Build an HTML fragment containing every direct child of the
            # ``<li>`` that is NOT a nested list, so the formula pipeline
            # still sees inline MathJax/MathML in the item's prose.
            inner_html_parts = []
            for child in li.children:
                if hasattr(child, 'name') and child.name in ('ul', 'ol'):
                    continue
                inner_html_parts.append(str(child))
            inner_html = ''.join(inner_html_parts).strip()

            text_md = ''
            if inner_html:
                # Wrap in a div so the converter sees a block context.
                text_md = cls._convert_aip_html_fragment_to_markdown(
                    f"<div>{inner_html}</div>"
                ).strip()

            if text_md:
                first_line, *rest = text_md.split('\n')
                lines.append(f"{indent}{marker} {first_line}")
                cont_indent = indent + ('   ' if is_ordered else '  ')
                for rl in rest:
                    lines.append(f"{cont_indent}{rl}" if rl.strip() else '')
            else:
                lines.append(f"{indent}{marker}")

            for nl in nested_lists:
                nested_md = cls._convert_aip_list(nl, level=level + 1)
                if nested_md:
                    lines.append(nested_md)

        return '\n'.join(lines).strip()

    # ------------------------------------------------------------------
    # Physics Today (physicstoday.aip.org)
    #
    # 📌 Same publisher, a different CMS: Physics Today is a Brightspot site,
    # not Silverchair, so none of the pubs.aip.org selectors match -- the
    # handler used to produce a "AIP Article / 0 authors / 0 figures" shell
    # and print no error at all. What it *does* ship is JATS leftovers inside
    # Brightspot markup (<disp-formula>, <table-wrap>, <xref>, <label>), so
    # the formula / table / list / reference machinery above is reused and
    # only the container selectors are new.
    # ------------------------------------------------------------------
    PT_BODY_SELECTOR = 'div.RichTextArticleBody'

    @classmethod
    def _pt_body(cls, soup):
        """The Physics Today article body, or None if this is not one."""
        return soup.select_one(cls.PT_BODY_SELECTOR)

    @staticmethod
    def _pt_original_cdn_url(url: str) -> str:
        """Brightspot's derivative URL → the unresized original on the CDN.

        Every ``<img>``/``<source>`` on the page points at a dims4 derivative
        whose ``?url=`` parameter carries the S3 original, e.g.
        ``…/resize/820x496!/quality/90/?url=https%3A%2F%2Fk1-prod-aip.s3…
        %2Fbrightspot%2FPTO.v58.i3.39_1.f3.jpg``. The bare CDN path for that
        key is the full-resolution file (measured: 657,109 bytes / 2096 px
        wide, versus 88,362 bytes for the largest webp derivative).

        ⚠️ The key is the path **after** ``/brightspot/``, not the basename --
        author headshots live in ``bc/c7/<hash>/feder.jpg``.
        """
        m = re.search(r'[?&]url=([^&\s]+)', url or '')
        if not m:
            return ''
        decoded = unquote(m.group(1))
        marker = '/brightspot/'
        if marker not in decoded:
            return ''
        return 'https://aip.brightspotcdn.com/' + decoded.split(marker, 1)[1]

    @classmethod
    def _pt_picture_urls(cls, pic) -> tuple:
        """``(hi_res, fallback)`` for one Brightspot ``<picture>``.

        Order asked for by the user: the unresized original on the CDN first,
        then the last (2×, 1640 px) entry of the ``image/webp`` srcset, then
        whatever the ``<img>`` itself shows.
        """
        img = pic.find('img')
        webp = ''
        for source in pic.find_all('source'):
            if (source.get('type') or '') != 'image/webp':
                continue
            entries = [e.strip().split(' ')[0]
                       for e in (source.get('srcset') or '').split(',') if e.strip()]
            if entries:
                webp = entries[-1]
        shown = ((img.get('src') if img else '') or '').strip()
        original = ((img.get('data-original-src') if img else '') or '').strip()
        if not original:
            original = cls._pt_original_cdn_url(webp or shown)
        candidates = [u for u in (original, webp, shown) if u]
        if not candidates:
            return '', ''
        hi_res = candidates[0]
        fallback = next((u for u in candidates[1:] if u != hi_res), '')
        return hi_res, fallback

    @classmethod
    def _pt_images(cls, soup, rewrite: bool = False) -> list:
        """``[(index, hi_res, fallback)]`` for every image in the body.

        The figure scan and the body walk parse the page separately, so -- as
        with ``_inline_images`` -- the index has to come from document order,
        the one thing both passes agree on. With *rewrite* the ``<picture>``
        is collapsed to a single ``<img src="__AIP_IMG_n__">`` so pandoc emits
        ``![](token)`` and ``convert_to_markdown`` later swaps in the
        downloaded file (or the publisher URL, if it never came down).
        """
        body = cls._pt_body(soup)
        if body is None:
            return []
        found = []
        index = 0
        for pic in body.find_all('picture'):
            # The lightbox ("View larger") is a copy of the figure, caption
            # included -- rendering it would print every caption twice.
            if pic.find_parent(class_=re.compile(r'ModalWindow')):
                continue
            hi_res, fallback = cls._pt_picture_urls(pic)
            if not hi_res:
                continue
            index += 1
            found.append((index, hi_res, fallback))
            if rewrite:
                # No alt text: Brightspot's alt is the file name
                # ("PTO.v58.i3.39_1.f1.jpg"), and the caption follows anyway.
                placeholder = soup.new_tag('img', src=f'__AIP_IMG_{index}__')
                pic.replace_with(placeholder)
        return found

    @classmethod
    def _pt_prepare_body(cls, soup):
        """Rewrite images, drop lightboxes, lift figures out of prose.

        A figure can sit inside a ``<li>`` (10.1063/1.1595054 figure 2) or a
        ``<p>``; the fragment converter collapses whitespace, so leaving it
        there would splice a whole caption into the middle of a sentence.
        Moving it after the top-level element it belongs to keeps document
        order and lets the walk render it as a block.
        """
        body = cls._pt_body(soup)
        if body is None:
            return None
        cls._pt_images(soup, rewrite=True)
        for modal in body.find_all(['bsp-modal-window']) + body.select('.ModalWindow'):
            modal.decompose()
        for enhancement in body.select('.Enhancement'):
            top = enhancement
            while top.parent is not None and top.parent is not body:
                top = top.parent
            if top is not enhancement:
                top.insert_after(enhancement.extract())
        return body

    @classmethod
    def _pt_figure_md(cls, enhancement) -> str:
        """Image + caption + credit for one Physics Today figure."""
        parts = []
        img = enhancement.find('img')
        if img is not None and (img.get('src') or '').startswith('__AIP_IMG_'):
            parts.append(f"![]({img['src']})")
        credit = enhancement.select_one('.Figure-credit')
        credit_md = ''
        if credit is not None:
            credit_md = cls._convert_aip_html_fragment_to_markdown(str(credit))
            credit.decompose()
        caption = enhancement.find('figcaption')
        if caption is not None:
            caption_md = cls._convert_aip_html_fragment_to_markdown(str(caption))
            if caption_md:
                parts.append(caption_md)
        if credit_md:
            parts.append(f"*{credit_md}*")
        return "\n\n".join(p for p in parts if p).strip()

    @classmethod
    def _walk_pt_body(cls, body, body_parts: list) -> None:
        """Render the Brightspot body in document order."""
        for child in body.children:
            if not getattr(child, 'name', None):
                continue
            classes = child.get('class') or []

            if child.name in ('h2', 'h3', 'h4'):
                heading = child.get_text(' ', strip=True)
                if heading:
                    level = {'h2': '###', 'h3': '####', 'h4': '#####'}[child.name]
                    body_parts.extend([f"{level} {heading}", ""])
                continue

            if 'Enhancement' in classes:
                md = cls._pt_figure_md(child)
                if md:
                    body_parts.extend([md, ""])
                continue

            if child.name in ('ul', 'ol'):
                md = cls._convert_aip_list(child)
                if md:
                    body_parts.extend([md, ""])
                continue

            if 'BlockTextModule' in classes:
                # A boxed sidebar ("Microscopic physics of high-order harmonic
                # generation"). Rendered as a blockquote so it stays visibly
                # separate from the running text instead of reading as a
                # section of the article.
                headline = child.find(class_='BlockTextModule_headline')
                lines = []
                if headline is not None:
                    text = headline.get_text(' ', strip=True)
                    headline.decompose()
                    if text:
                        lines.append(f"**{text}**")
                inner: list = []
                cls._walk_pt_body(child, inner)
                lines.extend(inner)
                # Prefix every physical line: a single entry can itself be
                # multi-line (a $$…$$ block), and a line without "> " ends the
                # blockquote.
                quoted = "\n".join(
                    (f"> {line}" if line.strip() else ">")
                    for entry in lines for line in entry.split("\n")
                ).strip()
                if quoted:
                    body_parts.extend([quoted, ""])
                continue

            if 'AckTextModule' in classes:
                # ⚠️ Not always an acknowledgment: 10.1063/1.3455243 has
                # <ack><title>Basic research and applications</title></ack> --
                # a section heading in the wrong tag. Emit a title as a
                # heading and only real prose as acknowledgments.
                title = child.find('title')
                if title is not None:
                    text = title.get_text(' ', strip=True)
                    title.decompose()
                    if text:
                        body_parts.extend([f"### {text}", ""])
                md = cls._convert_aip_html_fragment_to_markdown(str(child))
                if md:
                    body_parts.extend(["#### Acknowledgments", "", md, ""])
                continue

            if child.find('table') is not None:
                table_wrap = (child.find('table-wrap') or child.find(class_='table-wrap')
                              or child)
                # gfm, not the default markdown writer: pandoc's simple/
                # multiline tables align by column width, and Physics Today's
                # cells are long enough to break the layout (measured on
                # 10.1063/1.3455243 -- the header row came out detached from
                # the rule and the last row lost its empty cells). Pipe tables
                # survive any cell content.
                md = cls._convert_aip_table_to_md(table_wrap, writer='gfm')
                if md:
                    body_parts.extend([md.strip(), ""])
                continue

            # A <p> can hold a display formula (with its JATS <label>), an
            # inline image, or plain prose.
            formulas = child.find_all('disp-formula')
            if formulas:
                for wrap in formulas:
                    fm = cls._convert_aip_display_formula(wrap)
                    if fm:
                        body_parts.extend([fm, ""])
                    wrap.decompose()

            md = cls._convert_aip_html_fragment_to_markdown(str(child))
            if md:
                body_parts.extend([md, ""])

    @classmethod
    def _pt_metadata_from_html(cls, html_content: str) -> dict:
        """Physics Today metadata: Brightspot has no citation_* meta tags."""
        soup = BeautifulSoup(html_content, 'html.parser')
        body = cls._pt_body(soup)
        if body is None:
            return {}

        def text_of(selector):
            el = soup.select_one(selector)
            return el.get_text(' ', strip=True) if el is not None else ''

        meta = {'journal': 'Physics Today'}
        title = text_of('h1.Page-headline')
        if title:
            meta['title'] = title
        authors = [el.get_text(' ', strip=True)
                   for el in soup.select('.Page-authors .IndividualCard__name')]
        authors = [a for a in authors if a]
        if authors:
            meta['authors'] = authors
            meta['author_with_affiliations'] = [
                {'author': a, 'affiliations': []} for a in authors
            ]

        doi_el = soup.select_one('[data-doi]')
        doi = (doi_el.get('data-doi') if doi_el is not None else '') or ''
        if not doi:
            dc = soup.find('meta', attrs={'name': 'DC.Identifier'})
            doi = (dc.get('content') if dc is not None else '') or ''
        if doi:
            meta['doi'] = doi.strip()

        published = soup.find('meta', attrs={'property': 'article:published_time'})
        date_str = (published.get('content') if published is not None else '') or ''
        if date_str:
            meta['publication_date'] = date_str.split('T')[0]
            meta['year'] = date_str[:4]

        # The dek under the headline is the only summary the page carries --
        # Physics Today features have no abstract.
        dek = text_of('.Page-subHeadline')
        if dek:
            meta['abstract'] = dek

        # 📌 The PDF link is an attribute on the body element itself
        # (data-pdf="https://aip.brightspotcdn.com/PTO.v58.i3.39_1.online.pdf")
        # and needs no entitlement, unlike the pubs.aip.org article-pdf URL
        # Crossref hands out.
        pdf_url = (body.get('data-pdf') or '').strip()
        if pdf_url:
            meta['pdf_url'] = pdf_url
            # 📌 Volume and issue appear nowhere else on the page (Brightspot
            # has no citation_* meta), but the publisher's own file name states
            # them: PTO.v58.i3.39_1.online.pdf → volume 58, issue 3. Measured
            # against Crossref on all three samples (58/3, 56/6, 63/6 — exact).
            # A name that does not match leaves the fields empty rather than
            # guessed.
            # ⚠️ The number after the issue is the **first page** only (39,
            # while the article runs 39-46), so it does not go into 'pages':
            # a field that contradicts its own name is worse than an empty
            # one, and crossref.json carries the real range.
            m = re.search(r'\.v(\d+)\.i(\d+)\.', pdf_url)
            if m:
                meta['volume'], meta['issue'] = m.groups()
        return meta

    @classmethod
    def _pt_references_from_html(cls, html_content: str) -> list:
        """``ol.BodyReference > li`` -- each item already prints its number."""
        soup = BeautifulSoup(html_content, 'html.parser')
        references = []
        for li in soup.select('ol.BodyReference > li'):
            ref_md = cls._convert_aip_html_fragment_to_markdown(str(li))
            if ref_md:
                references.append(re.sub(r'\s+', ' ', ref_md).strip())
        return references

    @classmethod
    def extract_article_text_from_html(cls, html_content: str):
        """Extract AIP article text, returning (abstract_md, body_md).

        The abstract section is processed through the same pipeline as body
        paragraphs and returned separately so the caller can place it in the
        appropriate markdown section.
        """
        if not html_content:
            return '', ''

        soup = BeautifulSoup(html_content, 'html.parser')

        pt_body = cls._pt_prepare_body(soup)
        if pt_body is not None:
            pt_parts: list = []
            cls._walk_pt_body(pt_body, pt_parts)
            dek = soup.select_one('.Page-subHeadline')
            abstract_md = dek.get_text(' ', strip=True) if dek is not None else ''
            return abstract_md, "\n".join(pt_parts).strip()

        cls._inline_images(soup, rewrite=True)
        abstract_parts = []
        body_parts = []

        topics = [
            link.get_text(' ', strip=True)
            for link in soup.select('div.content-metadata.article-metadata a')
            if link.get_text(' ', strip=True)
        ]
        if topics:
            body_parts.extend([
                "### Topics",
                "",
                ", ".join(topics),
                "",
            ])

        # Heading levels we render:
        #   h2 → ###   h3 → ####   h4 → #####   h5 → ######   h6 → ######
        # (markdown caps at ######, so h5/h6 both map to it.)
        # data-section-title attr is only reliable on h2/h3 (jumplink headings);
        # h4-h6 subsection titles carry only the visible text, so accept them
        # unconditionally when they wear the section-title class.
        _HEADING_LEVELS = {
            'h2': '###', 'h3': '####', 'h4': '#####',
            'h5': '######', 'h6': '######',
        }

        # Sections emitted elsewhere in the markdown, so the body walk must
        # not render them a second time. References are the whole list here:
        # they come out of metadata['references'] as their own "## References"
        # section.
        _SKIP_HEADINGS = {'references'}

        seen_content_ids = set()
        article_nodes = soup.find_all(
            lambda tag: (
                tag.name in {'h2', 'h3'}
                and tag.get('data-section-title') is not None
            ) or (
                tag.name in {'h4', 'h5', 'h6'}
                and 'section-title' in (tag.get('class') or [])
            ) or (
                tag.name == 'div'
                and 'article-section-wrapper' in tag.get('class', [])
            )
        )

        for node in article_nodes:
            if node.name in _HEADING_LEVELS:
                # Route the heading INNER HTML through the same paragraph
                # pipeline as body text so <mjx-container> / <math> / <sup>
                # get preserved (previously get_text() stripped them, so a
                # heading like "B. Lawson's second insight: Dependence of
                # fuel energy gain on T and n τ" was truncated at the
                # first MathJax span).
                inner_html = node.decode_contents().strip()
                if inner_html:
                    heading = cls._convert_aip_html_fragment_to_markdown(
                        f"<p>{inner_html}</p>"
                    )
                else:
                    heading = ''
                # Fallback to data-section-title / plain text if the
                # conversion produced nothing.
                if not heading:
                    heading = (node.get('data-section-title')
                               or node.get_text(' ', strip=True) or '').strip()
                heading = re.sub(r'\s+', ' ', heading or '').strip()
                if not heading:
                    continue
                label = (node.get('data-section-title')
                         or node.get_text(' ', strip=True) or '')
                if label.strip().lower().rstrip('.') in _SKIP_HEADINGS:
                    continue
                body_parts.extend([f"{_HEADING_LEVELS[node.name]} {heading}", ""])
                continue

            content_id = node.get('id')
            if not content_id or content_id in seen_content_ids:
                continue
            seen_content_ids.add(content_id)

            # Extract abstract through the same pipeline, but collect separately.
            if node.find('section', class_='abstract', attrs={'aria-label': 'Main abstract'}):
                abstract_section = node.find('section', class_='abstract', attrs={'aria-label': 'Main abstract'})
                for paragraph in abstract_section.find_all('p'):
                    paragraph_md = cls._convert_aip_html_fragment_to_markdown(str(paragraph))
                    if paragraph_md:
                        abstract_parts.append(paragraph_md)
                continue

            # The reference list already has its own "## References" section,
            # built from metadata['references']. Rendering the same wrapper
            # here produced a second, malformed REFERENCES block inside
            # Article Text. Match on mixed-citation — the marker
            # extract_references_from_html() keys on — rather than on the
            # heading text, so the wording and case of the heading cannot
            # break it.
            if node.find('div', class_='mixed-citation'):
                continue

            # Walk direct children in document order so a section that mixes
            # <p>, a multi-formula <div class="disp-formula">, and loose text
            # (e.g. 10.1063/1.2844352 section 66667746) doesn't drop anything.
            cls._walk_aip_section(node, body_parts)

        abstract_md = "\n\n".join(abstract_parts).strip()
        body_md = "\n".join(body_parts).strip()
        return abstract_md, body_md

    @classmethod
    def extract_references_from_html(cls, html_content: str) -> list:
        """Extract AIP references from HTML, preserving DOI links as markdown."""
        if not html_content:
            return []

        soup = BeautifulSoup(html_content, 'html.parser')
        if cls._pt_body(soup) is not None:
            return cls._pt_references_from_html(html_content)

        references = []

        ref_divs = soup.find_all('div', attrs={'data-content-id': True})

        for ref_div in ref_divs:
            citation_div = ref_div.find('div', class_='mixed-citation')
            if not citation_div:
                continue

            label = ref_div.find('span', class_='label')
            ref_num = label.get_text(' ', strip=True) if label else ''

            # Remove citation-links (Google Scholar, Crossref, ADS, PubMed, OpenURL)
            citation_links = citation_div.find('div', class_='citation-links')
            if citation_links:
                citation_links.decompose()

            ref_html = str(citation_div)
            ref_md = cls._convert_aip_html_fragment_to_markdown(ref_html)
            if ref_md:
                ref_md = re.sub(r'\n+', ' ', ref_md).strip()
                ref_md = re.sub(r'[ \t]+', ' ', ref_md).strip()
                references.append(f"{ref_num} {ref_md}".strip())

        return references

    # Images that are not figures: AIP's appendices ship code listings and
    # screenshots as a bare <p><img class="content-image">, with no
    # div.fig-section, no label and no caption (10.1063/5.0294656 has three,
    # ".figures.online.dN.jpeg", in "APPENDIX: EXAMPLES ON THE MAIN STEPS
    # USED IN PYTHON CODING"). They were never collected, so pandoc left the
    # publisher's **signed** CloudFront URL in the Markdown -- a link that
    # stops working when the signature expires, in a document whose whole
    # point is to still be readable later.
    #
    # Numbered from a base well above any figure number so the two key spaces
    # cannot collide: the downloader derives its number with
    # re.search(r'(\d+)$', fig_id), so "fig_d1" would come back as 1 and
    # overwrite Figure 1.
    _INLINE_IMG_BASE = 900

    @classmethod
    def _inline_images(cls, soup, rewrite: bool = False) -> list:
        """``[(index, url)]`` for every content image outside a figure.

        The figure scan and the body walk parse the page separately, so the
        index has to come from document order -- the one thing both passes
        agree on. With *rewrite* the ``src`` is replaced by the placeholder
        ``convert_to_markdown`` later resolves, and the duplicate
        ``data-src`` / ``path-from-xml`` attributes are dropped so pandoc
        emits a plain ``![](token)`` instead of trailing a second copy of the
        URL in an attribute block.
        """
        found = []
        index = cls._INLINE_IMG_BASE
        for img in soup.select('img.content-image'):
            if img.find_parent('div', class_='fig-section') is not None:
                continue
            if img.find_parent(class_=re.compile('fig-modal|reveal-modal')):
                continue
            url = (img.get('src') or img.get('data-src') or '').strip()
            if not url:
                continue
            index += 1
            found.append((index, url))
            if rewrite:
                img['src'] = f'__AIP_IMG_{index}__'
                for attr in ('data-src', 'path-from-xml', 'srcset'):
                    if attr in img.attrs:
                        del img[attr]
        return found

    @classmethod
    def extract_figures_from_html(cls, html_content: str) -> dict:
        """Extract AIP figure URLs and captions from HTML."""
        if not html_content:
            return {}

        soup = BeautifulSoup(html_content, 'html.parser')

        if cls._pt_body(soup) is not None:
            pt_figures = {}
            for index, hi_res, fallback in cls._pt_images(soup):
                entry = {'url': hi_res, 'caption': ''}
                if fallback and fallback != hi_res:
                    entry['original_url'] = fallback
                pt_figures[f'fig_{index}'] = entry
            return pt_figures

        figures = {}
        seen_ids = set()

        for fig_div in soup.find_all('div', class_='fig-section'):
            data_id = fig_div.get('data-id', '')
            if not data_id or data_id in seen_ids:
                continue
            seen_ids.add(data_id)

            fig_num = data_id.lstrip('f')
            key = f"fig_{fig_num}"

            img = fig_div.find('img', class_='content-image')
            if not img:
                continue
            thumb_url = (img.get('src') or img.get('data-src') or '').strip()
            if not thumb_url:
                continue

            # The <img> shows the "m_" thumbnail (e.g. m_054003_1_….jpeg). The
            # full-resolution JPEG is embedded as a signed CDN URL in the
            # "Download slide" link's `image=` query parameter, e.g.
            #   /DownloadFile/DownloadImage.aspx?image=<CDN URL>&sec=…&ar=…
            # A naive m_-strip won't work: CloudFront's Signature is bound to
            # the exact URL, so we must lift the pre-signed high-res URL that
            # AIP ships with the page. The CDN URL ends at the third
            # CloudFront param (Key-Pair-Id=<value>) — everything after that
            # belongs to the aspx handler, not to the JPEG's signed URL.
            hi_res_url = ''
            dl_slide = fig_div.find('a', class_='download-slide')
            if dl_slide:
                href = dl_slide.get('href', '') or ''
                m = re.search(
                    r'[?&]image=(https?://.+?[?&]Key-Pair-Id=[^&]+)',
                    href,
                )
                if m:
                    hi_res_url = m.group(1).strip()

            # The dict key is called 'caption', so put the caption in it --
            # this used to hold only the label ("FIG. 1."). Nothing in the
            # workflow reads this field today (the Markdown gets its captions
            # from the body walker), but a field that contradicts its own name
            # is a trap for whoever reads it next.
            label = fig_div.find(class_='fig-label')
            caption_el = fig_div.select_one('.caption')
            caption = ' '.join(
                t for t in (
                    label.get_text(' ', strip=True) if label else '',
                    caption_el.get_text(' ', strip=True) if caption_el else '',
                ) if t
            )

            entry = {
                'url': hi_res_url or thumb_url,
                'caption': caption,
            }
            # Keep the m_-thumbnail as fallback so the download retry logic in
            # complete_paper_extraction.py can drop back to it if the hi-res
            # URL 403s (e.g. signature expired between scrape and download).
            if hi_res_url and thumb_url and hi_res_url != thumb_url:
                entry['original_url'] = thumb_url
            figures[key] = entry

        for index, url in cls._inline_images(soup):
            figures[f'fig_{index}'] = {'url': url, 'caption': ''}

        return figures

    @staticmethod
    def _extract_direct_supplemental_links(html_content: str) -> tuple:
        """Extract supplemental material links directly from the article HTML.

        AIP marks supplemental links in several ways:
          * class="supplementary-material-link" (older inline links)
          * data-doctype="dataSupplementDoc" (data supplements widget)
          * any <a> inside div.widget-ArticleDataSupplements
          * any <a> following an h2 whose text matches "supplementary material"
            (some articles have two such h2s — the first is just the
            descriptive prose, the second wraps the actual download link)

        Returns (urls, descriptions) where descriptions maps URL -> text content.
        """
        if not html_content:
            return [], {}

        soup = BeautifulSoup(html_content, 'html.parser')
        seen_urls = set()
        links = []
        descriptions = {}

        def _add_link(link_tag):
            href = (link_tag.get('href') or '').strip()
            if not href or href.startswith(('javascript:', '#', 'mailto:')):
                return
            if href.startswith('/'):
                href = f"https://pubs.aip.org{href}"
            elif not href.startswith('http'):
                href = f"https://pubs.aip.org/{href}"
            if href in seen_urls:
                return
            seen_urls.add(href)
            links.append(href)
            link_text = link_tag.get_text(' ', strip=True)
            if link_text:
                descriptions[href] = link_text

        # 1) Inline supplementary-material-link anchors
        for link in soup.find_all('a', class_='supplementary-material-link'):
            _add_link(link)

        # 2) Data-supplement widget anchors (data-doctype attribute)
        for link in soup.find_all('a', attrs={'data-doctype': 'dataSupplementDoc'}):
            _add_link(link)

        # 3) Any <a href> inside the ArticleDataSupplements widget
        for widget in soup.find_all('div', class_='widget-ArticleDataSupplements'):
            for link in widget.find_all('a', href=True):
                _add_link(link)

        # 4) Walk every h2 whose text (or data-section-title) matches
        #    "supplementary material".  For each match, scan the enclosing
        #    section container AND the following siblings until the next h2
        #    for download anchors.  This catches articles where the actual
        #    link only appears under the second of two "Supplementary
        #    Material" headings.
        def _is_supp_heading(tag):
            if tag.name != 'h2':
                return False
            section_title = (tag.get('data-section-title') or '').strip().lower()
            text = tag.get_text(' ', strip=True).lower()
            return ('supplementary material' in section_title
                    or 'supplementary material' in text)

        def _looks_like_download(link_tag) -> bool:
            href = (link_tag.get('href') or '').strip().lower()
            if not href or href.startswith(('javascript:', '#', 'mailto:')):
                return False
            patterns = (
                'supplement', 'suppl_material', '/article-supplement/',
                'datasupplement', 'figshare', 'ndownloader',
            )
            if any(pat in href for pat in patterns):
                return True
            if link_tag.get('data-doctype') == 'dataSupplementDoc':
                return True
            classes = link_tag.get('class') or []
            if 'supplementary-material-link' in classes:
                return True
            return False

        for heading in soup.find_all(_is_supp_heading):
            # The heading itself may sit inside a wrapper widget; scan that
            # wrapper as well as the heading's following siblings.
            scan_roots = []
            parent = heading.parent
            if parent is not None:
                scan_roots.append(parent)
            for sib in heading.next_siblings:
                if getattr(sib, 'name', None) == 'h2':
                    break
                if hasattr(sib, 'find_all'):
                    scan_roots.append(sib)
            for root in scan_roots:
                for link in root.find_all('a', href=True):
                    if _looks_like_download(link):
                        _add_link(link)

        return links, descriptions

    @staticmethod
    def _extract_supplemental_links_from_html(html_content: str) -> tuple:
        """Extract supplemental material download links from the figshare widget or direct links.

        First tries to extract from class="supplementary-material-link" (direct AIP links).
        If none found, uses figshare API to discover all files in a collection.

        Returns (urls, descriptions) where descriptions maps URL -> filename.
        """
        if not html_content:
            return [], {}

        # Method 1: Try direct supplementary-material-link extraction
        direct_links, direct_descriptions = AIPHandler._extract_direct_supplemental_links(html_content)
        if direct_links:
            print(f"  ✓ 找到 {len(direct_links)} 个直接补充材料链接")
            return direct_links, direct_descriptions

        # Method 2: Fallback to figshare
        soup = BeautifulSoup(html_content, 'html.parser')
        links = []
        descriptions = {}

        figshare_wrapper = soup.find('div', id='articlefulltext_figshare')
        if figshare_wrapper:
            for a_tag in figshare_wrapper.find_all('a', href=True):
                href = a_tag['href'].strip()
                if 'figstatic.com' in href or 'ndownloader' in href:
                    links.append(href)

            # If no direct download links, discover via the figshare API.
            #
            # ⚠️ This used to be gated on finding an article id in one of the
            # widget's anchors -- an id ``_fetch_figshare_collection`` does
            # not even use (it re-derives the *collection* id from the page
            # text). The gate was therefore testing the wrong thing, and it
            # only ever passed because the widget's JavaScript had injected
            # those anchors into the rendered DOM. Reading the server
            # response instead left the wrapper empty, the gate shut, and
            # 10.1063/5.0321661 reported "补充材料: 0 个" while the paper
            # actually has 6 videos plus a DOCX on figshare.
            if not links:
                links, descriptions = (
                    AIPHandler._fetch_figshare_collection('', html_content)
                )

        return links, descriptions

    @staticmethod
    def _fetch_figshare_collection(article_id: str, full_html: str) -> tuple:
        """Discover all files in a figshare collection via the figshare API.

        *article_id* is unused and kept only so existing callers do not have
        to change: what identifies the collection is the ``10.60893/figshare
        .<journal>.c.<id>`` DOI printed in the article text, which is read
        from *full_html* below.

        📌 The article page also fetches
        ``widgets.figshare.com/public/files?articleResourceDOI=<doi>``, which
        answers with the same list plus sizes. It is not used: that request
        belongs to the figshare widget's cross-origin iframe, which is a
        separate CDP target from the page we attach to, so it is not reliably
        in our capture. The links it hands out for video are also the signed,
        one-hour S3 *previews* -- this API gives the originals.
        """
        import json
        import urllib.request

        collection_id = None
        match = re.search(r'10\.60893/figshare\.[^.]*\.c\.(\d+)', full_html)
        if match:
            collection_id = match.group(1)

        if not collection_id:
            return [], {}

        article_ids = []
        try:
            url = f"https://api.figshare.com/v2/collections/{collection_id}/articles"
            req = urllib.request.Request(url, headers={'User-Agent': 'DownloadPaper/1.0'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                articles = json.loads(resp.read().decode())
            article_ids = [(a.get('id'), a.get('title', '')) for a in articles if a.get('id')]
        except Exception as e:
            print(f"  ⚠ Figshare collection API error: {e}")
            return [], {}

        links = []
        descriptions = {}
        for aid, title in article_ids:
            try:
                url = f"https://api.figshare.com/v2/articles/{aid}"
                req = urllib.request.Request(url, headers={'User-Agent': 'DownloadPaper/1.0'})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode())

                files = data.get('files', [])
                for f in files:
                    download_url = f.get('download_url')
                    if download_url:
                        links.append(download_url)
                        descriptions[download_url] = f.get('name', title)
            except Exception as e:
                print(f"  ⚠ Figshare article API error for {aid}: {e}")

        return links, descriptions

    async def get_fulltext_url(self, page) -> str:
        if page is not None:
            try:
                return page.url
            except Exception:
                pass
        return f"https://doi.org/{self.doi}" if self.doi else None

    async def get_pdf_url(self, doi: str) -> str:
        return None

    async def get_supplemental_url(self, doi: str) -> str:
        return None

    async def extract_references(self, html: str) -> list:
        return []

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        """Run the AIP handler through the unified publisher contract."""
        # Initialize page and managed resources using shared function
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'AIPHandler'
        )
        doi = self.doi  # resolve doi from handler after init (may have been None)

        # Get the actual page URL for correct base_url resolution
        set_actual_base_url(self, page)

        try:
            metadata = await self.extract_metadata(page)
            metadata['doi'] = doi

            pdf_url = metadata.pop('_pdf_url', None)

            # The raw server response, captured while the page loaded. AIP is
            # the simplest handler here -- no in-page fetch, no navigation of
            # its own -- so reading the capture leaves the article page with
            # nothing but navigation and passive listening.
            fulltext_html = await self.get_page_html(page)
            if fulltext_html and not metadata.get('abstract'):
                metadata['abstract'] = self.extract_main_abstract_from_html(fulltext_html)
            if fulltext_html:
                # The page's own reference list is the output; no per-entry
                # Crossref/S2 lookup (one HTTP round-trip per reference).
                metadata['references'] = self.extract_references_from_html(fulltext_html)

            figure_urls = {}
            supp_urls = []
            supp_descriptions = {}
            if fulltext_html:
                figure_urls = self.extract_figures_from_html(fulltext_html)
                supp_urls, supp_descriptions = self._extract_supplemental_links_from_html(fulltext_html)

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figure_urls,
                    'supplemental_urls': supp_urls,
                    'supplemental_descriptions': supp_descriptions,
                },
                'fulltext_data': fulltext_html,
                'journal_name': 'aip',
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

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        """Return a minimal Markdown shell until AIP conversion is implemented."""
        title = metadata.get('title') or 'AIP Article'
        md_parts = [
            f"# {title}",
            "",
        ]

        authors = metadata.get('authors', [])
        author_with_affiliations = metadata.get('author_with_affiliations', [])
        if authors:
            md_parts.append("**Authors:**")
            md_parts.append("")
            for entry in author_with_affiliations:
                name = entry.get('author', '')
                md_parts.append(name)
                for aff in entry.get('affiliations', []):
                    md_parts.append(aff)
                md_parts.append("")
            if not author_with_affiliations:
                for author in authors:
                    md_parts.append(author)
                    md_parts.append("")

        if metadata.get('doi'):
            md_parts.extend([f"**DOI:** {metadata['doi']}", ""])

        md_parts.extend([
            "## Publication",
            "",
            f"**Journal:** {metadata.get('journal') or 'AIP Publishing'}",
            "",
        ])

        # Add DOI in Publication section if available
        if metadata.get('doi'):
            md_parts.extend([f"**DOI:** {metadata['doi']}", ""])

        if metadata.get('year'):
            md_parts.extend([f"**Year:** {metadata['year']}", ""])

        abstract_from_body = ''
        body_md = ''

        if isinstance(article_text, str) and article_text.strip():
            if article_text.lstrip().startswith('<'):
                abstract_from_body, body_md = self.extract_article_text_from_html(article_text)
            else:
                body_md = article_text.strip()

        abstract = metadata.get('abstract') or abstract_from_body
        if abstract:
            md_parts.extend([
                "---",
                "",
                "## Abstract",
                "",
                abstract,
                "",
            ])

        # Resolve the appendix/inline images: the local file if it came
        # down, otherwise the publisher's URL, which at least still points
        # somewhere until its signature expires.
        _filenames = kwargs.get('figure_filenames') or {}
        _urls = kwargs.get('figure_urls') or {}

        def _resolve_inline(match: 're.Match') -> str:
            index = match.group(1)
            local = _filenames.get(index) or _filenames.get(int(index))
            if local:
                return str(local)
            info = _urls.get(f'fig_{index}') or {}
            return info.get('url', '') if isinstance(info, dict) else str(info)

        body_md = re.sub(r'__AIP_IMG_(\d+)__', _resolve_inline, body_md)

        # Insert downloaded figure images after each caption.
        if kwargs.get('add_figure_refs') and kwargs.get('figure_filenames'):
            figure_filenames = kwargs['figure_filenames']
            for fig_num, filename in sorted(figure_filenames.items(), key=lambda x: int(x[0])):
                body_md = re.sub(
                    rf'(\*\*FIG\.\s*{re.escape(fig_num)}\.\*\*[^\n]*)',
                    rf'\1\n\n![FIG. {fig_num}.]({filename})',
                    body_md,
                )

        md_parts.extend([
            "---",
            "",
            "## Article Text",
            "",
            body_md or "[AIP article text not found.]",
            "",
        ])

        # Supplemental material. ⚠️ AIP had no such section: the files landed
        # on disk and the Markdown never mentioned them -- "downloaded, and
        # nobody knows". Same gap SPIE had. Emitted only when there is
        # something to list, so papers without supplements gain no empty
        # heading.
        supp_downloads = kwargs.get('supplemental_downloads') or []
        supp_urls = kwargs.get('supplemental_urls') or []
        supp_desc = kwargs.get('supplemental_descriptions') or {}
        if supp_downloads or supp_urls:
            md_parts.extend(["---", "", "## Supplementary Material", ""])
            if supp_downloads:
                # The saved name comes from the URL (figshare serves
                # /files/<id>), so pair it with the publisher's own title --
                # "63831630.mp4" alone says nothing about which video it is.
                for item in supp_downloads:
                    label = supp_desc.get(item) or ''
                    md_parts.append(f"- `{item}`" + (f" — {label}" if label else ""))
            else:
                for url in supp_urls:
                    md_parts.append(f"- [{supp_desc.get(url, url)}]({url})")
            md_parts.append("")

        if metadata.get('references'):
            md_parts.extend([
                "---",
                "",
                "## References",
                "",
            ])
            for ref in metadata['references']:
                md_parts.extend([ref, ""])

        return "\n".join(md_parts)

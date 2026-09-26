"""ACM Digital Library publisher handler (dl.acm.org, DOI prefix 10.1145).

**Full text when ACM prints it, abstract when it does not.** Open-access
articles ship the whole paper -- sections, LaTeX, algorithms, footnotes,
appendices, references and supplemental files -- in the server's own
response, so everything here parses the raw HTML and never reads the live
DOM. Gated conference papers ship only the abstract; the handler says so
instead of leaving a blank that reads like a rendering bug.

  * ``metadata`` -- title, DOI, authors (+ affiliations + emails), year,
    journal/conference name, abstract, references.
  * ``fulltext_data`` -- the article page's raw HTML; the markdown is built
    from it by walking ``section[id^=sec-]`` in document order.
  * ``links.pdf_url`` -- always ``https://dl.acm.org/doi/pdf/{doi}``.
    Downloading may still 401 for paywalled papers; that is the standard
    retry/skip path in ``_download_all_resources``.
  * ``links.figure_urls`` -- every ``<img>`` in the body and appendices,
    numbered by document order. ⚠️ On ACM these are often **algorithms**
    rendered as JPEGs rather than figures.
  * ``links.supplemental_urls`` -- the ``/doi/suppl/...`` files listed in
    ``section#supplementary-materials``.

Headed-only: ACM fronts every request with Cloudflare bot protection
that hard-blocks headless Chromium. Do NOT add ``'acm'`` to
``HEADLESS_ACCESSIBLE_PUBLISHERS`` in ``complete_paper_extraction.py``.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString

from html_to_md_converter import (
    cleanup_markdown,
    convert_html_to_markdown,
    mathml_to_latex_pandoc,
    remove_newlines_in_paragraph,
)
from publisher.base import PublisherHandler
from publisher.wildcard import init_extract_all_page, set_actual_base_url


class ACMHandler(PublisherHandler):
    """Abstract-only handler for ACM Digital Library (dl.acm.org).

    See module docstring for scope. Non-abstract getters return empty
    values on purpose — they exist to satisfy the ``PublisherHandler``
    contract and to leave a stub for future work.
    """

    PUBLISHER = 'acm'

    ACM_BASE = 'https://dl.acm.org'

    #: Host that relative URLs on the page resolve against. Split out from
    #: ACM_BASE because the Atypon markup this handler understands is also
    #: what PNAS serves -- see publisher/pnas.py.
    SITE_BASE = ACM_BASE

    #: What the publisher calls the supplemental section in its own page.
    SUPPLEMENTAL_HEADING = 'Supplemental Material'

    def __init__(self, page=None, captured_data_dir=None, doi: str = None):
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.actual_base_url = self.ACM_BASE

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_title(soup: BeautifulSoup) -> str:
        # <h1> under <div class="core-container"> — only one on the page.
        h1 = soup.find('h1')
        if h1:
            return re.sub(r'\s+', ' ', h1.get_text(' ', strip=True)).strip()
        og = soup.find('meta', attrs={'property': 'og:title'})
        if og:
            return (og.get('content') or '').strip()
        return ''

    @staticmethod
    def _extract_doi_from_html(soup: BeautifulSoup) -> str:
        meta = soup.find('meta', attrs={'name': 'publication_doi'})
        if meta and meta.get('content'):
            return meta['content'].strip()
        return ''

    @classmethod
    def _extract_journal(cls, soup: BeautifulSoup) -> str:
        # <div class="core-self-citation"> reads
        #   "PASC '26: Platform for Advanced Scientific Computing Conference
        #    Article No.: 18, Pages 1 - 11 https://doi.org/10.1145/..."
        # The journal / proceedings name is everything before "Article No."
        core = soup.find('div', class_='core-self-citation')
        if not core:
            return ''
        text = core.get_text(' ', strip=True)
        text = re.sub(r'\s+', ' ', text)
        m = re.split(r'\s+Article\s+No\.?', text, maxsplit=1)
        return m[0].strip() if m else text

    @staticmethod
    def _extract_year(soup: BeautifulSoup) -> str:
        date_span = soup.find('span', class_='core-date-published')
        if date_span:
            m = re.search(r'\b(19|20|21)\d{2}\b', date_span.get_text())
            if m:
                return m.group(0)
        # Fallback: dc.date or citation_publication_date
        for name in ('citation_publication_date', 'dc.date', 'publication_date'):
            meta = soup.find('meta', attrs={'name': name})
            if meta and meta.get('content'):
                m = re.search(r'\b(19|20|21)\d{2}\b', meta['content'])
                if m:
                    return m.group(0)
        return ''

    @classmethod
    def _extract_authors(cls, soup: BeautifulSoup) -> Tuple[List[str], List[Dict[str, list]]]:
        """Extract author names + affiliations + emails from ACM markup.

        ACM wraps each author in ``<span property="author">`` containing
        ``<span property="givenName">`` and ``<span property="familyName">``
        for the visible link, and a ``<div class="dropBlock__holder">``
        popover with the full affiliation + email block. Iterate only
        the top-level author spans (skip the nested drop-holder ones,
        which would double-count).
        """
        names: List[str] = []
        detailed: List[Dict[str, list]] = []
        seen = set()

        # The served page's second author block, keyed by name: the one that
        # actually holds the affiliations. Empty on a rendered DOM, where the
        # dropBlock popover carries them instead.
        detail_blocks: Dict[str, object] = {}
        for block in soup.find_all('div', attrs={'property': 'author'}):
            if not block.find('div', class_='affiliations'):
                continue
            given = block.find('span', attrs={'property': 'givenName'})
            family = block.find('span', attrs={'property': 'familyName'})
            key = ' '.join(p.get_text(strip=True)
                           for p in (given, family) if p).strip()
            if key:
                detail_blocks.setdefault(key, block)

        for span in soup.find_all('span', attrs={'property': 'author'}):
            # Skip the inner spans that live inside a dropBlock__holder
            # (nested author markup for the popover) — the outer span
            # is the canonical one and its role is 'listitem'.
            if span.get('role') != 'listitem':
                continue

            given = span.find('span', attrs={'property': 'givenName'})
            family = span.find('span', attrs={'property': 'familyName'})
            name_parts = [p.get_text(strip=True) for p in (given, family) if p]
            name = ' '.join(name_parts).strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)

            affiliations: List[str] = []
            emails: List[str] = []
            # ⚠️ The dropBlock popover is built by JavaScript, so in the
            # served response it does not exist. That HTML carries the same
            # information in a second author block elsewhere in the article --
            # <div property="author"> with a <div class="affiliations"> inside
            # -- which is not a descendant of this span. Without looking there,
            # reading the raw response yields authors with no affiliation at
            # all: measured on 10.1145/3712285.3771783, 732 words lost.
            holder = span.find('div', class_='dropBlock__holder') \
                or detail_blocks.get(name)
            if holder:
                for aff in holder.find_all('div', attrs={'property': 'affiliation'}):
                    aff_name = aff.find('span', attrs={'property': 'name'})
                    if aff_name:
                        # Text minus the trailing <a> email link
                        aff_copy = BeautifulSoup(str(aff_name), 'html.parser')
                        for a in aff_copy.find_all('a', attrs={'property': 'email'}):
                            a.decompose()
                        aff_text = re.sub(
                            r'\s+', ' ',
                            aff_copy.get_text(' ', strip=True),
                        ).strip()
                        if aff_text:
                            affiliations.append(aff_text)
                for a in holder.find_all('a', attrs={'property': 'email'}):
                    email = cls._email_from_anchor(a)
                    if email and email not in emails:
                        emails.append(email)
            detailed.append({
                'author': name,
                'affiliations': affiliations,
                'emails': emails,
            })
        return names, detailed

    @staticmethod
    def _decode_cf_email(encoded: str) -> str:
        """Undo Cloudflare's e-mail obfuscation.

        ACM serves addresses as <span class="__cf_email__" data-cfemail="…">,
        which the browser decodes with JavaScript -- which is why the served
        HTML contains no address at all (0 hits for "gatech.edu" on a page
        with eight of them). The scheme is a single-byte XOR: the first octet
        is the key.
        """
        try:
            data = bytes.fromhex(encoded)
        except Exception:
            return ''
        if len(data) < 2:
            return ''
        key = data[0]
        try:
            return ''.join(chr(b ^ key) for b in data[1:])
        except Exception:
            return ''

    @classmethod
    def _email_from_anchor(cls, a) -> str:
        """The address behind an ACM e-mail link, obfuscated or not."""
        span = a.find('span', class_='__cf_email__')
        if span and span.get('data-cfemail'):
            decoded = cls._decode_cf_email(span['data-cfemail'])
            if '@' in decoded:
                return decoded
        href = (a.get('href') or '')
        if href.startswith('/cdn-cgi/l/email-protection#'):
            decoded = cls._decode_cf_email(href.split('#', 1)[1])
            if '@' in decoded:
                return decoded
        text = a.get_text(strip=True)
        if '@' in text:
            return text
        return href.replace('mailto:', '').strip()

    @classmethod
    def _extract_abstract(cls, soup: BeautifulSoup) -> str:
        """Every abstract the page carries, as markdown, in document order.

        ⚠️ Atypon articles routinely have **more than one**, each a
        ``section[role="doc-abstract"]``: ACM prints "Highlights" beside the
        abstract (8 paragraphs in 10.1145/3728480), PNAS prints
        "Significance" (``#executive-summary-abstract``). Reading only
        ``#abstract`` silently drops whichever one is not it -- and those are
        the plain-language summaries, the part written for readers outside
        the field.

        Each section is rendered through the body walk, so its paragraphs,
        lists and formulas go through the same pipeline as the article. The
        section's own heading becomes a bold lead-in rather than a heading,
        because the workflow already prints this under "## Abstract" --
        except for a section actually titled "Abstract", which needs no lead.
        """
        sections = soup.find_all(attrs={'role': 'doc-abstract'})
        if not sections:
            single = (soup.find('section', attrs={'id': 'abstract'})
                      or soup.find(attrs={'property': 'abstract'}))
            sections = [single] if single is not None else []
        if not sections:
            return ''

        parts: List[str] = []
        for section in sections:
            # ⚠️ Skip the machine-written one. Atypon marks it
            # `data-ai-generated` and gives it role="doc-abstract" like the
            # rest, so a blanket "every doc-abstract" rule pulls in an
            # AI summary plus its disclaimer and its UI text ("Click here to
            # comment on the accuracy…") -- measured on
            # 10.1145/3712285.3771783. The publisher's own flag is the
            # judgement here; we do not second-guess it by reading the prose.
            if section.has_attr('data-ai-generated'):
                print("  ⏭️  跳过 AI 生成的摘要（data-ai-generated）")
                continue
            # A copy: this soup is also what the body walk runs on, and the
            # heading has to come out before rendering.
            fragment = BeautifulSoup(str(section), 'html.parser')
            heading = fragment.find(['h2', 'h3'])
            title = heading.get_text(' ', strip=True) if heading else ''
            if heading is not None:
                heading.decompose()
            blocks = cls._walk(fragment, 2, {})
            text = '\n\n'.join(b for b in blocks if b).strip()
            if not text:
                continue
            if title and title.lower() not in ('abstract', 'abstract.'):
                parts.append(f"**{title}.** {text}")
            else:
                parts.append(text)
        return '\n\n'.join(parts)

    @classmethod
    def _convert_paragraph_to_md(cls, html_fragment: str) -> str:
        """Run an HTML fragment through the shared pandoc pipeline."""
        if not html_fragment:
            return ''
        md = convert_html_to_markdown(html_fragment)
        md = cleanup_markdown(md)
        md = remove_newlines_in_paragraph(md, '', 'p')
        return re.sub(r'\s+', ' ', md).strip()

    # ------------------------------------------------------------------
    # Full text
    # ------------------------------------------------------------------
    #
    # ACM (Atypon) ships the whole article in the server's response --
    # sections, LaTeX and all -- so everything below parses the raw HTML
    # and never touches the live DOM.
    #
    # Shapes that matter (10.1145/3728480 is the reference sample):
    #
    #   div.core-container   several per page; the body is the one holding
    #                        <section id="sec-N">, the back matter is the one
    #                        holding #footnotes / #appendix / #bibliography
    #   div[role=paragraph]  a paragraph. ⚠️ It can CONTAIN display formulas
    #                        and statement figures, so it is walked, not
    #                        flattened
    #   span.core-tex        the LaTeX itself, author's own source. Inline
    #                        spans carry their \( \) delimiters
    #   div.display-formula  block formula + an optional div.label ("(9)")
    #   figure.statement     Lemma / Theorem / Proof / Algorithm. The
    #                        data-type attribute names which, and the page
    #                        renders it indented
    #   figure[data-type=algorithm]  an IMAGE, not text (jds-2025-03-algo1.jpg)

    #: Block-level children a container walk must stop and handle, rather
    #: than sweeping into the surrounding inline text.
    _BLOCK_TAGS = frozenset({'section', 'figure', 'table', 'ul', 'ol', 'h1',
                             'h2', 'h3', 'h4', 'h5', 'h6'})

    @classmethod
    def _wraps_blocks(cls, el) -> bool:
        """True when *el* contains block content rather than running text."""
        for descendant in el.find_all(True, recursive=True):
            if descendant.name in cls._BLOCK_TAGS or cls._is_block_div(descendant):
                return True
        return False

    @staticmethod
    def _is_block_div(el) -> bool:
        if getattr(el, 'name', None) != 'div':
            return False
        classes = el.get('class') or []
        role = el.get('role') or ''
        return ('display-formula' in classes
                or role == 'list' 
                # ⚠️ div.figure-wrap has no role and no heading: it is a plain
                # div holding <header> (the "Table 1:" label) + <figure>. Left
                # to the inline path it goes to pandoc as raw HTML, and the
                # whole table lands in the markdown as one long <table> tag.
                or 'figure-wrap' in classes
                or role in ('paragraph', 'doc-footnote')
                or 'biblioentry' in classes)

    # -- math ----------------------------------------------------------

    @staticmethod
    def _inline_tex(latex: str) -> str:
        """``\\(x\\)`` → ``$x$``. Empty when there is nothing left."""
        tex = (latex or '').strip()
        tex = re.sub(r'^\\\(', '', tex)
        tex = re.sub(r'\\\)$', '', tex)
        tex = re.sub(r'\s+', ' ', tex).strip()
        return f"${tex}$" if tex else ''

    @classmethod
    def _display_tex(cls, latex: str, label: str = '') -> str:
        """Render a block formula.

        ⚠️ Only the ``equation`` / ``equation*`` wrapper is stripped and
        replaced by ``$$``. ``align``, ``array`` and friends are emitted as
        they stand -- they are display environments in their own right, and
        wrapping them in ``$$`` produces LaTeX that will not compile. This
        article alone uses equation, equation*, align, align*, aligned,
        array and bmatrix.
        """
        tex = (latex or '').strip()
        tex = tex.replace('\\nonumber', ' ')
        match = re.match(r'^\\begin\{equation\*?\}(.*)\\end\{equation\*?\}$',
                         tex, re.DOTALL)
        if match:
            inner = match.group(1).strip()
            if label:
                inner += f"\\tag{{{cls._bare_label(label)}}}"
            return '$$\n' + inner + '\n$$' if inner else ''
        # ⚠️ The label has to be carried into align too, or the numbering
        # disappears for most of the article -- this sample numbers 9
        # equations and 7 of them are `align`. \tag goes just inside the
        # environment, which is where amsmath accepts it.
        if label and '\\tag' not in tex:
            tex = re.sub(r'(\s*)\\end\{(\w+\*?)\}\s*$',
                         lambda m: f"\\tag{{{cls._bare_label(label)}}}"
                                   f"{m.group(1)}\\end{{{m.group(2)}}}",
                         tex, count=1)
        return tex

    @staticmethod
    def _bare_label(label: str) -> str:
        """``(9)`` → ``9``; the ``\\tag`` macro adds its own parentheses."""
        return (label or '').strip().strip('()').strip()

    @staticmethod
    def _latex_from_mathml(math_el) -> str:
        """LaTeX for one ``<math>``, or '' when it converts to nothing.

        Atypon serves either form and the handler cannot tell in advance
        which: ACM writes the author's ``span.core-tex`` LaTeX, PNAS ships
        MathML only (152 ``<math>`` elements in 10.1073/pnas.1522200113 and
        not one line of LaTeX source). So both paths live here rather than in
        one publisher's subclass.
        """
        try:
            latex = mathml_to_latex_pandoc(str(math_el)) or ''
        except Exception as exc:
            print(f"  ⚠️  MathML 转换失败: {type(exc).__name__}: {exc}")
            return ''
        latex = latex.strip()
        # The shared converter returns a display or inline wrapper depending
        # on the source; strip whichever it used, the caller re-wraps.
        for opener, closer in (('$$', '$$'), ('\\[', '\\]'),
                               ('\\(', '\\)'), ('$', '$')):
            if (latex.startswith(opener) and latex.endswith(closer)
                    and len(latex) > len(opener) + len(closer)):
                latex = latex[len(opener):-len(closer)].strip()
                break
        return re.sub(r'\s+', ' ', latex).strip()

    @classmethod
    def _stash_inline_math(cls, fragment, formulas: List[str]) -> None:
        """Replace each inline formula with a token, appending its markdown.

        ⚠️ Display formulas are skipped: the body walk reaches them through
        ``div.display-formula``, and converting them here as well prints each
        one twice.
        """
        for math_el in fragment.find_all('math'):
            if math_el.find_parent('div', class_='display-formula') is not None:
                continue
            latex = cls._latex_from_mathml(math_el)
            if not latex:
                math_el.decompose()
                continue
            formulas.append(f"${latex}$")
            math_el.replace_with(f"DPMATH{len(formulas) - 1:04d}ZZ")

        for span in fragment.find_all('span', class_='core-tex'):
            tex = cls._inline_tex(span.get_text())
            target = span.parent if (span.parent is not None
                                     and span.parent.get('role') == 'math'
                                     and len(span.parent.find_all(recursive=False)) == 1) else span
            if not tex:
                target.replace_with('')
                continue
            formulas.append(tex)
            target.replace_with(f"DPMATH{len(formulas) - 1:04d}ZZ")

    @classmethod
    def _inline_md(cls, element) -> str:
        """Markdown for one inline run, with the LaTeX carried through.

        Each ``span.core-tex`` becomes an opaque token before pandoc runs
        and is swapped back afterwards -- pandoc would otherwise escape the
        backslashes in the author's source.
        """
        fragment = BeautifulSoup(f'<div>{element}</div>', 'html.parser')
        # ⚠️ pandoc turns an unknown attribute into a bracketed span
        # ("[Lemma 1.]{data-style=\"small-caps\"}"), which lands in the
        # markdown as literal noise. These attributes are presentational.
        for styled in fragment.find_all(attrs={'data-style': True}):
            del styled['data-style']
        # Same story for target="_blank" on reference links, which pandoc
        # renders as a trailing {target="_blank"}.
        for anchor in fragment.find_all('a', attrs={'target': True}):
            del anchor['target']
        # ⚠️ Footnote markers come through EMPTY -- ACM fills the number in
        # with JavaScript, so the response holds <a href="#fn5"><sup></sup></a>
        # and pandoc renders a linkless "[](#fn5)". The number is in the href.
        for ref in fragment.find_all('a', attrs={'role': 'doc-noteref'}):
            marker = ref.get_text(' ', strip=True)
            if marker:
                # The printed marker, in brackets: "1", "*", "†" as the page
                # shows it. ⚠️ Bracketed because the marker is a superscript
                # on the page and butts straight against the preceding word
                # in plain text ("…[[7], [48]].1"). ⚠️ And taken from the
                # anchor rather than from the href, whose digits are an
                # element id -- using those renumbers the footnotes whenever
                # the two disagree.
                ref.replace_with(f"[{marker}]" if not marker.startswith('[')
                                 else marker)
                continue
            number = re.search(r'(\d+)$', ref.get('href') or '')
            ref.replace_with(f"[{number.group(1)}]" if number else '')
        for roled in fragment.find_all(attrs={'role': True}):
            del roled['role']
        # Screen-reader duplicates of the caption ("A rendering of 33
        # interacting rocket thrusters."), which otherwise read as stray prose.
        for hidden in fragment.select('.sr-only'):
            hidden.decompose()
        # Inline images are ACM's inline math: little SVGs sitting in the
        # sentence. They get the same placeholder treatment as the floats, so
        # the markdown points at the downloaded file instead of dl.acm.org.
        # An inline image with no download slot is a formula/symbol picture
        # (see _is_downloadable_asset). Point at the publisher's copy --
        # ⚠️ absolutised, because the page's src is site-relative and a
        # "/cms/…" link in the markdown resolves to nothing on disk.
        for img in fragment.find_all('img'):
            if img.get('data-dp-asset'):
                continue
            src_url = cls._absolute((img.get('src') or '').strip())
            if not src_url:
                img.decompose()
                continue
            # ⚠️ Rewrite the tag and let pandoc render it. Substituting the
            # markdown text here instead gets it escaped on the way through
            # ("\![\](https://…)"), because to pandoc it is literal text.
            img['src'] = src_url
            for attr in ('width', 'height', 'loading', 'class'):
                if attr in img.attrs:
                    del img[attr]
        for img in fragment.find_all('img', attrs={'data-dp-asset': True}):
            # ⚠️ An opaque token, not "[INLINEFIG_4]": pandoc escapes literal
            # brackets, and the escaped form never matches on the way back.
            img.replace_with(f"DPINLINEFIG{img['data-dp-asset'].split('_')[-1]}ZZ")
        formulas: List[str] = []
        cls._stash_inline_math(fragment, formulas)
        md = cls._convert_paragraph_to_md('<p>' + fragment.div.decode_contents() + '</p>')
        for index, tex in enumerate(formulas):
            md = md.replace(f"DPMATH{index:04d}ZZ", tex)
        return md.strip()

    # -- body walk -----------------------------------------------------

    @classmethod
    def _walk(cls, node, level: int, figures: Dict[str, str]) -> List[str]:
        """Render *node*'s children in document order.

        *level* is the heading depth the node's own ``<h2>`` should take, so
        appendix subsections can be pushed one level down without their
        markup saying anything about it.
        """
        blocks: List[str] = []
        inline_buffer: List[str] = []

        def flush():
            if not inline_buffer:
                return
            md = cls._inline_md(''.join(inline_buffer))
            inline_buffer.clear()
            if md:
                blocks.append(cls._escape_block_start(md))

        for child in node.children:
            if isinstance(child, NavigableString):
                if str(child).strip():
                    inline_buffer.append(str(child))
                continue
            name = child.name
            classes = child.get('class') or []
            role = child.get('role') or ''

            if name in ('script', 'style'):
                continue

            if not (name in cls._BLOCK_TAGS or cls._is_block_div(child)):
                if name == 'div' and cls._wraps_blocks(child):
                    # A structural wrapper with block content inside: recurse
                    # instead of handing it to pandoc as inline HTML.
                    # ⚠️ ACM's Highlights sit in <div id="highlightsAccordion">,
                    # and as inline it came out as a literal
                    # "::: {#highlightsAccordion}" fence in the markdown.
                    flush()
                    blocks.extend(cls._walk(child, level, figures))
                    continue
                inline_buffer.append(str(child))
                continue

            flush()

            if re.fullmatch(r'h[1-6]', name or ''):
                depth = min(6, level + int(name[1]) - 2)
                # ⚠️ Through _inline_md, not get_text: section titles carry
                # math too ("4.3 Box-constrained \(\ell _\infty\) Regression"),
                # and get_text would print the raw delimiters.
                text = cls._inline_md(child.decode_contents())
                if text:
                    blocks.append('#' * max(2, depth) + ' ' + text)
            elif name == 'section':
                blocks.extend(cls._walk(child, level, figures))
            elif 'display-formula' in classes:
                blocks.extend(cls._render_display_formula(child))
            elif 'figure-wrap' in classes:
                blocks.extend(cls._render_figure_wrap(child, level, figures))
            elif name == 'figure':
                blocks.extend(cls._render_figure(child, level, figures))
            elif role == 'paragraph':
                blocks.extend(cls._walk(child, level, figures))
            elif role == 'list':
                blocks.extend(cls._render_div_list(child, level, figures))
            elif role == 'doc-footnote':
                blocks.extend(cls._render_footnote(child))
            elif 'biblioentry' in classes:
                blocks.extend(cls._render_biblioentry(child))
            else:
                md = cls._convert_paragraph_to_md(str(child))
                if md:
                    blocks.append(md)

        flush()
        return blocks

    @staticmethod
    def _escape_block_start(md: str) -> str:
        """Escape a first character that would turn a paragraph into a block.

        ⚠️ Measured on PNAS 10.1073/pnas.1522200113, whose footnote markers
        include "#": the note came out as ``# A few coins with Hebrew
        characters…`` -- a top-level heading in the middle of the notes.
        Headings in this handler only ever come from ``<h*>`` elements, so a
        paragraph starting with one of these characters is always literal.
        """
        return '\\' + md if md[:1] in ('#', '>', '|') else md

    @classmethod
    def _render_display_formula(cls, div) -> List[str]:
        label_div = div.find('div', class_='label')
        # ⚠️ No separator: the label is one token with markup inside it
        # ("(P<sub>ϵ</sub>)"), and ' ' would split it into "(P ϵ)".
        label = label_div.get_text('', strip=True) if label_div else ''

        span = div.find('span', class_='core-tex')
        if span is not None:
            tex = cls._display_tex(span.get_text(), label)
            return [tex] if tex else []

        # MathML instead of LaTeX source (PNAS). The converter gives a bare
        # expression, so the $$ wrapper and the \tag are added here.
        math_el = div.find('math')
        if math_el is None:
            # ⚠️ Older articles have no formula source at all: the equation
            # IS a JPEG (10.1073/pnas.0601855103 renders all five that way).
            # Before this branch the images were downloaded -- they take a
            # numbering slot -- and then never referenced, so the equations
            # were simply missing from the markdown while the figure numbers
            # jumped 1, 2, 5, 6, 8.
            img = div.find('img')
            key = img.get('data-dp-asset') if img is not None else ''
            if key:
                # No alt text: "Figure 7" under an equation is wrong, and the
                # number is only the download slot.
                return [f"DPINLINEFIG{key.split('_')[-1]}ZZ"]
            return []
        latex = cls._latex_from_mathml(math_el)
        if not latex:
            return []
        if label:
            latex += f"\\tag{{{cls._bare_label(label)}}}"
        return ['$$\n' + latex + '\n$$']

    @classmethod
    def _render_figure_wrap(cls, wrap, level: int,
                            figures: Dict[str, str]) -> List[str]:
        """``div.figure-wrap`` = the float's number plus the float itself.

        The number ("Table 1:", "Figure 3:") lives in a ``<header>`` beside
        the ``<figure>``, in ``span.core-label`` -- it is nowhere inside the
        figure, so it has to be read here and handed down.
        """
        # ACM wraps the number in span.core-label; PNAS writes it straight
        # into the header's div.label ("Table 1."). Take whichever is there.
        label_el = (wrap.find('span', class_='core-label')
                    or (wrap.find('header').find('div', class_='label')
                        if wrap.find('header') is not None else None))
        label = label_el.get_text(' ', strip=True) if label_el else ''
        blocks: List[str] = []
        for figure in wrap.find_all('figure', recursive=True):
            blocks.extend(cls._render_figure(figure, level, figures, label))
        if not blocks and label:
            blocks.append(f"**{label}**")
        return blocks

    @classmethod
    def _render_table(cls, figure, label: str) -> List[str]:
        """Caption, the table itself, then its notes.

        ``figcaption`` holds ``div.caption`` (the prose) and ``div.notes``
        (the table's footnote: "* Numerically unstable; † MI300A is always
        unified"). ⚠️ The notes must be pulled out before the caption is
        rendered, or the symbol definitions end up glued to the end of the
        caption sentence -- and they are exactly what makes the numbers in
        the table readable.
        """
        blocks: List[str] = []
        # ⚠️ The notes are looked for in the whole <figure>, not just inside
        # the caption: ACM nests them in <figcaption>, PNAS puts them beside
        # it. Either way they must come out BEFORE the caption is rendered,
        # or the symbol definitions end up glued to the caption sentence.
        notes_el = figure.find('div', class_='notes')
        notes = ''
        if notes_el is not None:
            notes = cls._inline_md(notes_el.decode_contents())
            notes_el.extract()
        caption_el = figure.find('figcaption')
        caption = ''
        if caption_el is not None:
            caption = cls._inline_md(caption_el.decode_contents())
            caption_el.extract()

        heading = ' '.join(part for part in (label, caption) if part)
        if heading:
            blocks.append(f"**{heading}**")

        wrap = figure.find('div', class_='table-wrap') or figure
        table = wrap.find('table')
        if table is not None:
            md = cls._convert_table_to_md(table)
            if md:
                blocks.append(md)
        else:
            # ⚠️ Not every "table" is markup. PNAS ships Table 1 of
            # 10.1073/pnas.1522200113 as a JPEG, and with only the <table>
            # branch the figure rendered as a caption with nothing under it.
            for img in figure.find_all('img'):
                key = img.get('data-dp-asset')
                if key:
                    # No alt text: the caption right above already says
                    # "Table 1.", and "![Figure 4]" under it would contradict
                    # it -- the number in the key is the download slot, not
                    # the float's printed number.
                    blocks.append(f"DPINLINEFIG{key.split('_')[-1]}ZZ")
        if notes:
            blocks.append(notes)
        return blocks

    @classmethod
    def _convert_table_to_md(cls, table) -> str:
        """The table as a GitHub-style pipe table, cells already converted.

        Two things make this its own function rather than a call to the
        paragraph pipeline:

        ⚠️ **Each cell is replaced by an opaque token before pandoc sees the
        table.** A cell's markdown is produced first (it may hold math, bold
        or a footnote marker); handing that markdown back to pandoc as table
        input gets it escaped a second time -- measured on this article:
        ``**FP64**`` came out as ``\\*\\*FP64\\*\\*`` and ``^*^`` as
        ``\\^\\\\\\*\\^``.

        ⚠️ **The writer is ``gfm``, not the default.** pandoc's markdown
        writer prefers simple/multiline tables, whose alignment depends on
        column widths and breaks as soon as a cell is long; a pipe table
        survives any cell content. Cell markdown is flattened to one line for
        the same reason -- a newline inside a pipe row ends the table.
        """
        import pypandoc

        fragment = BeautifulSoup(str(table), 'html.parser')
        # ACM's inline border styling produces nothing in markdown and bloats
        # every cell; the alignment hints are not markdown either.
        for el in fragment.find_all(True):
            for attr in ('style', 'data-xml-align', 'data-xml-valign',
                         'class', 'width', 'height'):
                if attr in el.attrs:
                    del el[attr]

        cells: List[str] = []
        for cell in fragment.find_all(['td', 'th']):
            md = re.sub(r'\s+', ' ', cls._inline_md(cell.decode_contents())).strip()
            cells.append(md)
            cell.clear()
            cell.append(NavigableString(f"DPCELL{len(cells) - 1:04d}ZZ"))

        try:
            md = pypandoc.convert_text(str(fragment), 'gfm', format='html',
                                       extra_args=['--wrap=none'])
        except Exception as exc:
            print(f"  ⚠️  表格转换失败: {type(exc).__name__}: {exc}")
            return ''
        for index, text in enumerate(cells):
            md = md.replace(f"DPCELL{index:04d}ZZ", text)
        return md.strip()

    @classmethod
    def _render_figure(cls, figure, level: int, figures: Dict[str, str],
                       label: str = '') -> List[str]:
        """A statement block, an image, or both.

        Lemma / Theorem / Proof are set off from the running text on the
        page; a blockquote is the markdown that says the same thing without
        inventing a heading level for something that is not a section.
        """
        if 'table' in (figure.get('class') or []) or figure.find('table'):
            return cls._render_table(figure, label)

        # The screen-reader description repeats the caption; keep one of them.
        for hidden in figure.select('.sr-only'):
            hidden.decompose()

        caption_el = figure.find('figcaption')
        caption = ''
        if caption_el is not None:
            caption = cls._inline_md(caption_el.decode_contents())
            caption_el.extract()
        if label:
            caption = ' '.join(part for part in (label, caption) if part)

        image_md: List[str] = []
        for img in figure.find_all('img'):
            key = img.get('data-dp-asset')
            img.extract()
            if not key:
                continue
            number = key.split('_')[-1]
            image_md.append(f"[FIGURE_{number}]")

        body = cls._walk(figure, level, figures)

        out: List[str] = []
        heading = caption or cls._statement_heading(figure)
        if heading:
            out.append(heading if heading.startswith('**') else f"**{heading}**")
        out.extend(image_md)
        if body:
            # One blockquote for the whole statement: the page indents it as
            # a single block, and separate quotes would read as separate
            # statements.
            quoted = '\n\n'.join(body)
            out.append('\n'.join('> ' + line if line else '>'
                                  for line in quoted.split('\n')))
        return [b for b in out if b]

    @staticmethod
    def _statement_heading(figure) -> str:
        """A heading like "Algorithm 1." for a statement with no caption.

        ACM gives the algorithm figures no ``<figcaption>`` -- the number
        lives only in ``data-type`` plus the element id (``algorithm1``).
        """
        data_type = (figure.get('data-type') or '').strip()
        if not data_type:
            return ''
        number = re.search(r'(\d+)$', figure.get('id') or '')
        label = data_type[:1].upper() + data_type[1:]
        return f"{label} {number.group(1)}." if number else f"{label}."

    @classmethod
    def _render_div_list(cls, div, level: int, figures: Dict[str, str]) -> List[str]:
        """An Atypon list built from divs, not ``<ul>``.

        ``<div role="list">`` wraps ``<div role="listitem">``, each holding an
        optional ``div.label`` ("*i*)") and a ``div.content``. The label is
        the publisher's own numbering, so it is kept verbatim and the item is
        written as a markdown list item -- renumbering it would silently
        disagree with the cross-references in the text.
        """
        items: List[str] = []
        for item in div.find_all(attrs={'role': 'listitem'}, recursive=False):
            label_el = item.find('div', class_='label')
            label = ''
            if label_el is not None:
                label = cls._inline_md(label_el.decode_contents())
                label_el.extract()
            # ⚠️ A label that is just a bullet glyph is the markdown bullet
            # said twice ("- • Information geometric…"). Numbered labels
            # ("i)", "1.") are the publisher's own and are kept, because the
            # running text refers to them.
            if label.strip() in ('•', '·', '-', '–', '—', '*'):
                label = ''
            body = cls._walk(item, level, figures)
            text = '\n\n'.join(b for b in body if b)
            if not text:
                continue
            first, _, rest = text.partition('\n')
            lines = [f"- {label} {first}".replace('-  ', '- ')]
            if rest:
                # Continuation lines are indented so they stay in the item.
                lines.extend('  ' + line if line else '' for line in rest.split('\n'))
            items.append('\n'.join(lines))
        return ['\n'.join(items)] if items else []

    @classmethod
    def _render_footnote(cls, div) -> List[str]:
        label_div = div.find('div', class_='label')
        label = label_div.get_text(' ', strip=True) if label_div else ''
        if label_div is not None:
            label_div.extract()
        text = ' '.join(cls._walk(div, 2, {}))
        if not text:
            return []
        return [cls._escape_block_start(f"{label} {text}".strip() if label else text)]

    @classmethod
    def _render_biblioentry(cls, div) -> List[str]:
        label_div = div.find('div', class_='label')
        label = label_div.get_text(' ', strip=True) if label_div else ''
        content = div.find('div', class_='citation-content')
        if content is None:
            return []
        text = cls._inline_md(content.decode_contents())
        if not text:
            return []
        return [f"{label} {text}".strip()]

    # -- locating the article ------------------------------------------

    #: Back-matter sections that belong in the body markdown, by id. The
    #: reference list and the supplemental files are NOT here -- the workflow
    #: wants those as data, not prose.
    BACK_MATTER_IDS = ('footnotes', 'appendix')

    @classmethod
    def _back_matter_nodes(cls, soup: BeautifulSoup) -> List:
        """The back-matter sections to append after the body, in order."""
        nodes = []
        for section_id in cls.BACK_MATTER_IDS:
            node = soup.find('section', id=section_id)
            if node is not None:
                nodes.append(node)
        return nodes

    @classmethod
    def _body_container(cls, soup: BeautifulSoup):
        """The ``div.core-container`` that holds the numbered sections.

        ⚠️ Walking the sections directly is not the same thing: a float can
        be parked **between** them, as a sibling of the sections rather than
        inside one. PNAS does exactly that with Fig. 1 of
        10.1073/pnas.1522200113 -- with a section-only walk the figure was
        neither numbered nor rendered, and nothing in the log said so.
        """
        sections = cls._body_sections(soup)
        if not sections:
            return None
        container = sections[0].find_parent('div', class_='core-container')
        return container if container is not None else None

    @staticmethod
    def _body_sections(soup: BeautifulSoup) -> List:
        """The numbered body sections, in order.

        ACM stacks several ``div.core-container`` blocks on the page (nav,
        metadata, abstract, body, back matter). The body is the one holding
        ``section[id^=sec-]``; picking it by id keeps the widgets out
        without a blocklist.
        """
        return [s for s in soup.find_all('section', id=re.compile(r'^sec-\d+$'))
                if s.find_parent('section', id=re.compile(r'^sec-\d+$')) is None]

    @classmethod
    def _number_assets(cls, soup: BeautifulSoup) -> None:
        """Tag every article image with the key the downloader will use.

        The figure scan and the body walk are two independent passes; if
        each counted for itself they would drift apart the first time one of
        them skipped an image (ACS has been bitten by exactly that). Both
        read the number off the markup instead.
        """
        body = cls._body_container(soup)
        containers = [body] if body is not None else cls._body_sections(soup)
        appendix = soup.find('section', id='appendix')
        if appendix is not None:
            containers.append(appendix)
        index = 0
        for container in containers:
            for img in container.find_all('img'):
                if not cls._is_downloadable_asset(img):
                    continue
                index += 1
                img['data-dp-asset'] = f'fig_{index}'

    @staticmethod
    def _is_downloadable_asset(img) -> bool:
        """Whether *img* is worth saving next to the markdown.

        Yes for anything inside a ``<figure>`` (the floats, including a table
        printed as a picture) and for a **display** formula's image.

        ❌ No for an image sitting in the running text. Old Atypon articles
        render every formula as a picture -- inline ones included -- and a
        symbol-sized JPEG is not usable on its own: recovering the formula
        would mean OCR'ing it. Those are referenced at the publisher's URL
        instead, the same call SPIE's handler makes.
        """
        if img.find_parent('figure') is not None:
            return True
        return img.find_parent('div', class_='display-formula') is not None

    @classmethod
    def extract_figures_from_html(cls, html_content: str) -> Dict[str, dict]:
        """``{'fig_N': {'url': large, 'original_url': medium}}``.

        ⚠️ Not every ACM image is a "Figure": this article's only one is an
        **algorithm** rendered as a JPEG. They are numbered by document
        order regardless of what they depict, which is what the downloader
        keys on.
        """
        if not html_content:
            return {}
        soup = BeautifulSoup(html_content, 'html.parser')
        cls._number_assets(soup)
        figures: Dict[str, dict] = {}
        for img in soup.find_all('img', attrs={'data-dp-asset': True}):
            medium = (img.get('src') or '').strip()
            large = (img.get('data-viewer-src') or '').strip()
            best = large or medium
            if not best:
                continue
            figures[img['data-dp-asset']] = {
                'url': cls._absolute(best),
                'original_url': cls._absolute(medium) if medium and medium != best else None,
            }
        return figures

    @classmethod
    def _absolute(cls, url: str) -> str:
        if not url:
            return ''
        if url.startswith('http'):
            return url
        return cls.SITE_BASE + ('' if url.startswith('/') else '/') + url

    @classmethod
    def extract_supplemental_from_html(cls, html_content: str) -> Tuple[List[str], Dict[str, str]]:
        """``(urls, {url: description})`` from the Supplemental Material section."""
        if not html_content:
            return [], {}
        soup = BeautifulSoup(html_content, 'html.parser')
        section = soup.find('section', id='supplementary-materials')
        if section is None:
            return [], {}
        urls: List[str] = []
        descriptions: Dict[str, str] = {}
        for item in section.find_all('div', class_='core-supplementary-material'):
            anchor = item.find('a', href=True)
            if anchor is None:
                continue
            url = cls._absolute(anchor['href'])
            if url in urls:
                continue
            urls.append(url)
            # ⚠️ Two parts, and the second is the interesting one:
            # div.heading is the file type plus the paper's own title, while
            # the sibling div holds what the file actually is ("Recording of
            # the presentation of ... at SC25."). Taking only the heading
            # threw that away.
            description = item.find('div', class_='core-description')
            parts: List[str] = []
            if description is not None:
                for piece in description.find_all('div', recursive=False):
                    text = re.sub(r'\s+', ' ', piece.get_text(' ', strip=True)).strip()
                    if text and text not in parts:
                        parts.append(text)
            if parts:
                descriptions[url] = ' — '.join(parts)
        return urls, descriptions

    @classmethod
    def extract_article_text_from_html(cls, html_content: str) -> Tuple[str, str]:
        """``(abstract_md, body_md)`` for an ACM article page.

        ``body_md`` is the numbered sections plus the footnotes and the
        appendices -- everything the publisher prints as the paper. The
        reference list and the supplemental material are returned by their
        own extractors, because the workflow needs them as data, not prose.

        An empty ``body_md`` is a real answer: many ACM conference papers
        are still gated and the landing page carries the abstract alone.
        """
        if not html_content:
            return '', ''

        soup = BeautifulSoup(html_content, 'html.parser')
        cls._number_assets(soup)
        abstract_md = cls._extract_abstract(soup)

        figures: Dict[str, str] = {}
        blocks: List[str] = []
        body = cls._body_container(soup)
        for container in ([body] if body is not None else cls._body_sections(soup)):
            blocks.extend(cls._walk(container, 2, figures))

        for node in cls._back_matter_nodes(soup):
            rendered = cls._walk(node, 2, figures)
            # A section with nothing but its heading is not worth printing:
            # an empty "## Footnote" reads as a failed extraction.
            if len(rendered) > 1:
                blocks.extend(rendered)

        body_md = '\n\n'.join(b for b in blocks if b).strip()
        return abstract_md, body_md

    # ------------------------------------------------------------------
    # PublisherHandler contract
    # ------------------------------------------------------------------

    async def extract_metadata(self, page) -> dict:
        html = await self.get_page_html(page)
        if not html:
            return {}
        soup = BeautifulSoup(html, 'html.parser')

        title = self._extract_title(soup)
        doi = self._extract_doi_from_html(soup) or (self.doi or '')
        year = self._extract_year(soup)
        journal = self._extract_journal(soup)
        abstract_md = self._extract_abstract(soup)
        authors, detailed = self._extract_authors(soup)

        corr_emails: List[str] = []
        for entry in detailed:
            for email in entry.get('emails', []):
                if email not in corr_emails:
                    corr_emails.append(email)

        return {
            'title': title,
            'doi': doi,
            'authors': authors,
            'author_with_affiliations': detailed,
            'year': year,
            'journal': journal,
            'abstract': abstract_md,
            'corresponding_author_emails': corr_emails,
        }

    async def get_pdf_url(self, doi: str) -> Optional[str]:
        """Construct the canonical ACM PDF URL — always ``/doi/pdf/{doi}``."""
        doi = (doi or self.doi or '').strip()
        if not doi:
            return None
        return f"{self.ACM_BASE}/doi/pdf/{doi}"

    async def get_supplemental_url(self, doi: str) -> Optional[str]:
        # ACM lists supplemental files inside the article page itself; they
        # are collected in extract_all, so there is no separate URL to open.
        return None

    async def extract_references(self, html: str) -> list:
        """Reference strings from ``section#bibliography``.

        Each entry is ``div.label`` ("[1]") plus ``div.citation-content``;
        the sibling ``div.external-links`` holds Google Scholar / Crossref
        buttons and is left out.
        """
        if not html:
            return []
        soup = BeautifulSoup(html, 'html.parser')
        section = soup.find('section', id='bibliography')
        if section is None:
            return []
        references: List[str] = []
        for entry in section.find_all('div', class_='biblioentry'):
            rendered = self._render_biblioentry(entry)
            references.extend(rendered)
        return references

    async def get_figures(self, json_data: dict) -> dict:
        return {}

    async def get_fulltext_url(self, page) -> str:
        try:
            return page.url or ''
        except Exception:
            return f"{self.ACM_BASE}/doi/{self.doi}" if self.doi else ''

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        """Run the ACM handler through the unified publisher contract.

        Abstract-only: figure_urls / supplemental_urls are always empty.
        """
        page, managed_playwright, managed_browser, managed_context = await init_extract_all_page(
            self, page, doi, 'ACMHandler'
        )
        doi = self.doi
        set_actual_base_url(self, page)

        try:
            metadata = await self.extract_metadata(page)
            metadata['doi'] = doi or metadata.get('doi', '')

            fulltext_html = await self.get_page_html(page)

            metadata['references'] = await self.extract_references(fulltext_html)

            figure_urls = self.extract_figures_from_html(fulltext_html)
            supplemental_urls, supplemental_descriptions = (
                self.extract_supplemental_from_html(fulltext_html))

            _, body_md = self.extract_article_text_from_html(fulltext_html)
            print(f"  ✓ 正文: {len(body_md):,} 字符")
            print(f"  ✓ 参考文献: {len(metadata['references'])} 条")
            print(f"  ✓ 图片: {len(figure_urls)} 个")
            print(f"  ✓ 补充材料: {len(supplemental_urls)} 个")
            if not body_md:
                print("  ⚠️  页面上没有正文 —— 这篇多半仍是登录墙后的，"
                      "md 只会有摘要")

            pdf_url = await self.get_pdf_url(doi)

            return {
                'metadata': metadata,
                'links': {
                    'pdf_url': pdf_url,
                    'figure_urls': figure_urls,
                    'supplemental_urls': supplemental_urls,
                    'supplemental_descriptions': supplemental_descriptions,
                },
                'fulltext_data': fulltext_html,
                'journal_name': 'acm',
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
    # Markdown rendering
    # ------------------------------------------------------------------

    def convert_to_markdown(self, metadata: dict, article_text, **kwargs) -> str:
        figure_filenames = kwargs.get('figure_filenames') or {}
        figure_urls = kwargs.get('figure_urls') or {}
        supplemental_urls = kwargs.get('supplemental_urls') or []
        supplemental_descriptions = kwargs.get('supplemental_descriptions') or {}
        supplemental_downloads = kwargs.get('supplemental_downloads') or []

        title = metadata.get('title') or 'ACM Article'
        md_parts: List[str] = [f"# {title}", '']

        # Authors with affiliations
        detailed = metadata.get('author_with_affiliations') or []
        if detailed:
            md_parts.extend(['## Authors', ''])
            for entry in detailed:
                name = entry.get('author', '')
                affs = entry.get('affiliations') or []
                emails = entry.get('emails') or []
                md_parts.append(f"- **{name}**")
                for aff in affs:
                    md_parts.append(f"  - {aff}")
                for em in emails:
                    md_parts.append(f"  - {em}")
            md_parts.append('')
        elif metadata.get('authors'):
            md_parts.extend(['## Authors', ''])
            md_parts.append(', '.join(metadata['authors']))
            md_parts.append('')

        # Publication metadata block
        md_parts.extend(['## Publication', ''])
        for key, label in (
            ('journal', '**Journal:**'),
            ('year', '**Year:**'),
            ('doi', '**DOI:**'),
        ):
            val = metadata.get(key)
            if val:
                md_parts.append(f"{label} {val}")
                md_parts.append('')

        abstract = (metadata.get('abstract') or '').strip()
        body_md = ''
        if isinstance(article_text, str) and article_text.strip():
            if article_text.lstrip().startswith('<'):
                abstract_from_body, body_md = self.extract_article_text_from_html(article_text)
                if not abstract:
                    abstract = abstract_from_body
            else:
                body_md = article_text.strip()

        md_parts.extend(['---', '', '## Abstract', ''])
        md_parts.append(abstract or '[No abstract available.]')
        md_parts.append('')

        if body_md:
            md_parts.extend(['---', '', self._resolve_figures(
                body_md, figure_filenames, figure_urls), ''])
        else:
            # Say so rather than let a missing body read as a rendering bug.
            md_parts.extend([
                '---',
                '',
                '*ACM 没有在页面上提供正文（多半仍在登录墙后），以上只有摘要。*',
                '',
            ])

        if supplemental_urls or supplemental_downloads:
            md_parts.extend(['---', '',
                             f"## {self.SUPPLEMENTAL_HEADING}", ''])
            for url in supplemental_urls:
                description = supplemental_descriptions.get(url, '')
                name = url.rsplit('/', 1)[-1]
                # ⚠️ The download entries are already paths relative to the
                # paper directory ("supplemental/supplemental--x.pdf"), so
                # they are used as they stand -- prefixing the folder again
                # produces a link to a file that does not exist.
                local = next((str(f) for f in supplemental_downloads
                              if str(f).endswith(name)), '')
                target = local or url
                # ⚠️ Angle brackets: ACM's own filenames contain "[1]",
                # which would otherwise close the markdown link early.
                md_parts.append(f"- [{description or name}](<{target}>)")
            md_parts.append('')

        references = metadata.get('references') or []
        if references:
            md_parts.extend(['---', '', '## References', ''])
            for reference in references:
                md_parts.extend([reference, ''])

        return '\n'.join(md_parts).rstrip() + '\n'

    @staticmethod
    def _resolve_figures(body_md: str, figure_filenames: dict,
                         figure_urls: dict) -> str:
        """Turn ``[FIGURE_N]`` placeholders into images.

        ⚠️ A placeholder whose file did not download falls back to the
        remote URL rather than vanishing: a missing image should be visible
        in the markdown, not silently absent.
        """
        def replace(match, inline: bool):
            number = match.group(1)
            filename = figure_filenames.get(number) or figure_filenames.get(int(number)) \
                if figure_filenames else None
            # An inline graphic is a symbol inside a sentence, so it gets no
            # "Figure N" alt text -- that would read as a float.
            alt = '' if inline else f"Figure {number}"
            if filename:
                return f"![{alt}]({filename})"
            info = figure_urls.get(f'fig_{number}')
            url = info.get('url') if isinstance(info, dict) else info
            if url:
                return f"![{alt}]({url})"
            return f"*[Figure {number} 未下载]*"

        body_md = re.sub(r'DPINLINEFIG(\d+)ZZ',
                         lambda m: replace(m, inline=True), body_md)
        return re.sub(r'\[FIGURE_(\d+)\]',
                      lambda m: replace(m, inline=False), body_md)

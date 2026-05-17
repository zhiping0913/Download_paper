"""
Shared extraction patterns reused across multiple publishers.

Functions placed here are publisher-agnostic building blocks that
Nature, IOP, Springer, Elsevier, and other publishers can compose
into their own handlers.
"""

import re
from html import unescape

from bs4 import BeautifulSoup, NavigableString

from json_to_md_converter import (
    cleanup_markdown,
    convert_html_to_markdown,
    mathml_to_latex_pandoc,
    remove_newlines_in_paragraph,
)


# ------------------------------------------------------------------
# Math / HTML → Markdown helpers
# ------------------------------------------------------------------

def strip_inline_math_delimiters(latex: str) -> str:
    """Return the body of a single inline math expression."""
    latex = (latex or '').strip()
    if latex.startswith('$') and latex.endswith('$') and not latex.startswith('$$'):
        return latex[1:-1].strip()
    return latex


def convert_mathml(math_tag, display: bool = False) -> str:
    """Convert a <math> tag (MathML) to LaTeX via pandoc."""
    latex = mathml_to_latex_pandoc(str(math_tag))
    if not latex:
        return ''

    if display:
        latex_body = strip_inline_math_delimiters(latex)
        return f"$$\n{latex_body}\n$$"
    return latex


def prepare_mathjax_html_fragment(html_fragment: str, placeholder_prefix: str = "MATH") -> tuple[str, list[str]]:
    """Collapse MathJax CHTML markup to placeholders before pandoc conversion.

    Returns (processed_html, formulas_list).
    The caller should convert the HTML to Markdown, then replace
    ``{prefix}NNNMATHEND`` with the stored LaTeX formulas.
    """
    soup = BeautifulSoup(html_fragment, 'html.parser')
    formulas = []

    def stash_formula(latex: str) -> str:
        formulas.append(latex)
        return f"{placeholder_prefix}{len(formulas) - 1:03d}MATHEND"

    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()

    # Collapse xref-bibr links to plain [number] brackets.
    for a_tag in soup.select('a.xref-bibr'):
        sup_tag = a_tag.find('sup')
        ref_text = sup_tag.get_text(' ', strip=True) if sup_tag else a_tag.get_text(' ', strip=True)
        if ref_text:
            a_tag.replace_with(NavigableString(f"[{ref_text}]"))

    # Collapse xref-fig / xref-table links to plain text.
    for a_tag in soup.select('a.xref-fig, a.xref-table'):
        a_tag.replace_with(NavigableString(a_tag.get_text(' ', strip=True)))

    # Inline formulas rendered as MathJax CHTML with assistive MathML.
    for formula in soup.select('span.inline-formula'):
        math_tag = formula.find('math')
        latex = convert_mathml(math_tag) if math_tag else ''
        if latex:
            formula.replace_with(NavigableString(f" {stash_formula(latex)} "))

    for container in soup.find_all('mjx-container'):
        math_tag = container.find('math')
        latex = convert_mathml(math_tag) if math_tag else ''
        if latex:
            container.replace_with(NavigableString(f" {stash_formula(latex)} "))

    # MathJax 2.x rendered images: <img role="math" alt="$a$" src="data:image/png;base64,..." />
    for img in soup.find_all('img', {'role': 'math'}):
        alt = img.get('alt', '').strip()
        if alt:
            # Alt text already contains LaTeX with delimiters (e.g. "$a$"); pass as-is.
            img.replace_with(NavigableString(f" {stash_formula(alt)} "))
        else:
            img.decompose()

    # MathJax 2.x rendered images wrapped in links: <a xmlns:xlink ...><img role="math" .../></a>
    for a_tag in soup.find_all('a', {'xmlns:xlink': True}):
        a_tag.unwrap()

    return str(soup), formulas


def convert_html_fragment_to_markdown(html_fragment: str, placeholder_prefix: str = "MATH") -> str:
    """Convert an HTML fragment to Markdown, restoring MathJax formulas.

    Args:
        html_fragment: Raw HTML string.
        placeholder_prefix: Prefix used in ``{prefix}NNNMATHEND`` placeholders.

    Returns:
        Cleaned Markdown string (single line / collapsed whitespace).
    """
    prepared_html, formulas = prepare_mathjax_html_fragment(html_fragment, placeholder_prefix)
    md = convert_html_to_markdown(prepared_html)
    for index, latex in enumerate(formulas):
        md = md.replace(f"{placeholder_prefix}{index:03d}MATHEND", latex)
    md = cleanup_markdown(md)
    md = remove_newlines_in_paragraph(md, "", "p")
    md = re.sub(r'\s+', ' ', md).strip()
    return md


# ------------------------------------------------------------------
# Generic article body / abstract extraction
# ------------------------------------------------------------------

def find_generic_article_body(soup) -> BeautifulSoup | None:
    """Try to locate article body content using selectors common across publishers.

    Supports IOP Science, Nature, Elsevier, Springer, and other publishers.
    """
    selectors = [
        # IOP Science full-text wrapper
        'div.wd-jnl-art-full-text',
        'div.wd-jnl-art-full-text.article-text',
        # IOP Science / general
        'div.article-text',
        'div.article-body', 'div.article-content',
        'div#article-body', 'section.article-body',
        # Nature
        'div.main-content',
        # Springer / shared
        'div.c-article-body', 'article.c-article',
        'div[itemprop="articleBody"]',
        # Generic
        'article', 'div.content', 'div#content',
    ]
    for selector in selectors:
        match = soup.select_one(selector)
        if match:
            return match
    return None


def extract_abstract_with_fallbacks(soup, paragraph_converter=None) -> str:
    """Extract an abstract from HTML, trying multiple publisher selectors.

    Args:
        soup: Parsed BeautifulSoup document.
        paragraph_converter: Optional callable(str) -> str that converts
            a raw HTML paragraph to Markdown.  If *None*, the paragraph
            text is returned as-is (no formula conversion).

    Returns:
        Abstract string (Markdown if *paragraph_converter* is provided).
    """
    # Nature-style: <section data-title="Abstract">
    abstract_section = soup.find('section', {'data-title': 'Abstract'})
    # IOP/Springer fallback
    if not abstract_section:
        abstract_section = soup.find('div', {'class': 'c-article-section__content'})
    # Cambridge-style
    if not abstract_section:
        abstract_section = soup.find('div', class_='article-abstract')

    if abstract_section:
        content_div = abstract_section.find('div', {'class': 'c-article-section__content'}) or abstract_section
        paragraphs = content_div.find_all('p')
        converted = []
        for p in paragraphs:
            if paragraph_converter:
                md = paragraph_converter(str(p))
                if md:
                    converted.append(md)
            else:
                text = p.get_text(' ', strip=True)
                if text:
                    converted.append(text)
        if converted:
            return "\n\n".join(converted)

    # Meta description last resort
    for meta_name in ('dc.description', 'description', 'citation_abstract'):
        meta_tag = soup.find('meta', {'name': meta_name})
        if meta_tag and meta_tag.get('content', '').strip():
            return meta_tag['content'].strip()

    return ''


# ------------------------------------------------------------------
# BibTeX reference formatting
# ------------------------------------------------------------------

_STOP_WORDS = {'a', 'an', 'the', 'on', 'in', 'of', 'for', 'to', 'and',
               'with', 'from', 'by', 'at', 'or', 'as', 'is', 'its', 'not'}


def generate_bibtex_key(authors: list, year: str, title: str) -> str:
    """Generate a BibTeX citation key: LastNameYearFirstMeaningfulWord.

    Example: ``Tolenis2025Complex``
    """
    last_name = 'Unknown'
    if authors:
        first_author = authors[0].strip()
        if ',' in first_author:
            last_name = first_author.split(',')[0].strip()
        else:
            parts = first_author.split()
            if parts:
                last_name = parts[-1]
        last_name = re.sub(r'[^a-zA-Z]', '', last_name)

    year_str = year or '0000'
    year_match = re.search(r'(\d{4})', str(year_str))
    year_str = year_match.group(1) if year_match else '0000'

    title_word = 'Ref'
    if title:
        words = [w for w in re.findall(r'[a-zA-Z]+', title)
                 if w.lower() not in _STOP_WORDS]
        if words:
            title_word = words[0].capitalize()

    return f"{last_name}{year_str}{title_word}"


def _pick(parts: dict, *keys: str) -> str:
    """Return the first non-empty value from *keys* in *parts*."""
    for k in keys:
        v = parts.get(k, '')
        if v:
            return v
    return ''


def format_as_bibtex(parts: dict, *, key: str = None) -> str:
    """Convert parsed citation parts into a standard BibTeX entry.

    Args:
        parts: Dict with keys like ``citation_author``, ``citation_title``,
            ``citation_journal_title``, ``citation_volume``, ``citation_firstpage``,
            ``citation_lastpage``, ``citation_publication_date``, ``citation_doi``,
            ``citation_conference_title``.  Short keys (without ``citation_``
            prefix) are also accepted as fallbacks.
        key: Optional pre-computed BibTeX key. If omitted, one is generated
            from the authors / year / title.

    Returns:
        Formatted BibTeX string with 2-space indentation.
    """
    authors_raw = _pick(parts, 'citation_author', 'author')
    title = _pick(parts, 'citation_title', 'title')
    journal = _pick(parts, 'citation_journal_title', 'journal', 'journal_title')
    conference = _pick(parts, 'citation_conference_title', 'conference', 'conference_title')
    volume = _pick(parts, 'citation_volume', 'volume')
    pages = _pick(parts, 'citation_firstpage', 'firstpage',
                  'citation_pages', 'pages') or ''
    lastpage = _pick(parts, 'citation_lastpage', 'lastpage') or ''
    year = _pick(parts, 'citation_publication_date', 'publication_date', 'date', 'year')
    doi = _pick(parts, 'citation_doi', 'doi')

    # Determine entry type
    if conference:
        entry_type = 'inproceedings'
        venue_key = 'booktitle'
        venue_value = conference
    elif journal:
        entry_type = 'article'
        venue_key = 'journal'
        venue_value = journal
    else:
        entry_type = 'misc'

    # Extract year
    year_match = re.search(r'(\d{4})', str(year))
    year_str = year_match.group(1) if year_match else year

    # Parse author list
    author_list = [a.strip() for a in authors_raw.split(';') if a.strip()]
    formatted_authors = []
    for author in author_list:
        if author.lower() == 'others':
            formatted_authors.append('others')
            continue
        if ',' in author:
            formatted_authors.append(author)
        else:
            name_parts = author.rsplit(None, 1)
            if len(name_parts) == 2:
                formatted_authors.append(f"{name_parts[1]}, {name_parts[0]}")
            else:
                formatted_authors.append(author)

    # Generate key if not provided
    if key is None:
        key = generate_bibtex_key(formatted_authors, year_str, title)

    # Build BibTeX entry
    lines = [f"@{entry_type}{{{key},"]
    lines.append(f"  author = {{{' and '.join(formatted_authors)}}},")
    if title:
        lines.append(f"  title = {{{title}}},")
    if entry_type in ('article', 'inproceedings'):
        lines.append(f"  {venue_key} = {{{venue_value}}},")
    if volume:
        lines.append(f"  volume = {{{volume}}},")
    if pages:
        if lastpage:
            lines.append(f"  pages = {{{pages}--{lastpage}}},")
        else:
            lines.append(f"  pages = {{{pages}}},")
    if year_str:
        lines.append(f"  year = {{{year_str}}},")
    if doi:
        lines.append(f"  doi = {{{doi}}}")
    lines.append("}")

    return "\n".join(lines)


def format_citation_as_text(parts: dict, *, index: int = None) -> str:
    """Format parsed citation parts as a readable numbered reference string.

    Example: ``[1] Author1, Author2. Title. Journal Volume, Pages (Year). doi:10.xxx``

    Uses the same fallback key logic as ``format_as_bibtex``.
    """
    authors_raw = _pick(parts, 'citation_author', 'author')
    title = _pick(parts, 'citation_title', 'title')
    journal = _pick(parts, 'citation_journal_title', 'journal', 'journal_title')
    volume = _pick(parts, 'citation_volume', 'volume')
    pages = _pick(parts, 'citation_firstpage', 'firstpage',
                  'citation_pages', 'pages') or ''
    lastpage = _pick(parts, 'citation_lastpage', 'lastpage') or ''
    year = _pick(parts, 'citation_publication_date', 'publication_date', 'date', 'year')
    doi = _pick(parts, 'citation_doi', 'doi')

    year_match = re.search(r'(\d{4})', str(year))
    year_str = year_match.group(1) if year_match else year

    # Format authors as "First Last, First2 Last2"
    author_list = [a.strip() for a in authors_raw.split(';') if a.strip()]
    if author_list and author_list[0].lower() == 'others':
        author_list = author_list[1:] + ['others']
    author_str = ', '.join(author_list)

    # Build components
    parts_list = []
    if title:
        parts_list.append(title.rstrip('.'))
    if journal:
        jpart = journal
        if volume:
            jpart += f" {volume}"
        if pages:
            if lastpage:
                jpart += f", {pages}--{lastpage}"
            else:
                jpart += f", {pages}"
        parts_list.append(jpart)
    if year_str:
        parts_list.append(f"({year_str})")
    if doi:
        parts_list.append(f"doi:{doi}")

    ref_text = f"{author_str}. " if author_str else ""
    ref_text += ". ".join(parts_list)

    prefix = f"[{index}] " if index is not None else ""
    return f"{prefix}{ref_text}"

def parse_citation_reference_string(ref_str: str, *, bibtex_key: str = None) -> str:
    """Parse a ``citation_reference`` meta tag value into a BibTeX entry.

    Semi-colon separated ``key=value`` pairs → ``format_as_bibtex``.
    Falls back to plain text if parsing fails.
    """
    parts = {}
    for segment in ref_str.split(';'):
        if '=' not in segment:
            continue
        k, v = segment.split('=', 1)
        k = k.strip()
        v = re.sub(r'\s+', ' ', unescape(v or '')).strip()
        if k and v:
            parts[k] = v

    if not parts:
        return re.sub(r'\s+', ' ', unescape(ref_str or '')).strip()

    return format_as_bibtex(parts, key=bibtex_key)

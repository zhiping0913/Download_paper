"""
Generic utility functions for paper extraction
These functions are publisher-agnostic and can be reused across different publishers
"""

import json
import re
import requests
from pathlib import Path
from datetime import datetime

# ============================================================================
# Semantic Scholar API Configuration
# ============================================================================
S2_API_URL = "https://api.semanticscholar.org/graph/v1/paper/DOI:"

# ============================================================================
# Crossref API Configuration
# ============================================================================
CROSSREF_API_URL = "https://api.crossref.org/works"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json'
}


# ============================================================================
# API Functions
# ============================================================================

def fetch_semanticscholar(doi: str) -> dict:
    """Fetch paper metadata from Semantic Scholar API"""
    s2_fields = 'title,year,venue,authors'
    try:
        s2_res = requests.get(
            f"{S2_API_URL}{doi}",
            params={'fields': s2_fields},
            headers=HEADERS,
            timeout=15
        )
        if s2_res.status_code == 200:
            data = s2_res.json() or {}
            if data:
                print(f"  ✓ Semantic Scholar: {data.get('title', 'N/A')[:50]}... ({data.get('year', 'N/A')})")
            return data
    except Exception as e:
        print(f"  ⚠️  Semantic Scholar exception {doi}: {e}")
    return {}


def fetch_crossref(doi: str) -> dict:
    """Fetch paper metadata from Crossref API

    Extracts: publisher, publication date (year/month/day), authors, ISBN, ISSN, references

    Returns dict with keys:
        - title
        - authors (list of dicts with 'name', 'given', 'family')
        - publisher
        - year (publication year)
        - date_parts ([year, month, day])
        - isbn (list)
        - issn (list)
        - references (list of reference objects)
        - volume, issue, pages
        - journal (container-title)
    """
    try:
        url = f"{CROSSREF_API_URL}/{doi}"
        response = requests.get(url, headers=HEADERS, timeout=15)

        if response.status_code == 200:
            data = response.json()
            work = data.get('message', {})

            if not work:
                return {}

            # Extract key information
            result = {
                'title': work.get('title', [''])[0] if work.get('title') else '',
                'type': work.get('type', ''),
                'publisher': work.get('publisher', ''),
                'authors': [],
                'year': None,
                'date_parts': None,
                'isbn': work.get('ISBN', []),
                'issn': work.get('ISSN', []),
                'references': work.get('reference', []),
                'volume': work.get('volume'),
                'issue': work.get('issue'),
                'pages': work.get('page'),
                'journal': work.get('container-title', [''])[0] if work.get('container-title') else '',
                'doi': work.get('DOI', doi),
            }

            # Extract authors
            if work.get('author'):
                for author in work['author']:
                    result['authors'].append({
                        'name': f"{author.get('given', '')} {author.get('family', '')}".strip(),
                        'given': author.get('given', ''),
                        'family': author.get('family', ''),
                    })

            # Extract publication date
            if work.get('published-online'):
                date_parts = work['published-online'].get('date-parts', [])
                if date_parts and date_parts[0]:
                    result['date_parts'] = date_parts[0]
                    result['year'] = date_parts[0][0] if date_parts[0] else None

            # Fallback to issued date if published-online not available
            if not result['year'] and work.get('issued'):
                date_parts = work['issued'].get('date-parts', [])
                if date_parts and date_parts[0]:
                    result['date_parts'] = date_parts[0]
                    result['year'] = date_parts[0][0] if date_parts[0] else None

            if result['title']:
                print(f"  ✓ Crossref: {result['title'][:50]}... ({result['year'] or 'N/A'})")

            return result
        else:
            print(f"  ⚠️  Crossref API error {response.status_code} for DOI {doi}")
            return {}

    except Exception as e:
        print(f"  ⚠️  Crossref exception {doi}: {e}")
        return {}


# ============================================================================
# File Organization Functions
# ============================================================================

def _clean_title_for_directory(title: str) -> str:
    """Clean title for use as directory name, handling formulas and HTML tags."""
    if not title:
        return 'paper'

    # Remove HTML/XML tags (including MathML and other markup)
    # Add space before removing tag to preserve word boundaries
    title = re.sub(r'<[^>]+>', ' ', title)

    # Replace common mathematical symbols with text representations
    replacements = {
        '−': '-',           # minus sign (U+2212) -> ASCII hyphen-minus
        '±': 'pm',          # plus-minus
        '×': 'x',           # multiplication
        '÷': 'div',         # division
        '≈': 'approx',      # approximately equal
        '≠': 'ne',          # not equal
        '≤': 'le',          # less than or equal
        '≥': 'ge',          # greater than or equal
        '→': 'to',          # arrow
        '←': 'from',        # left arrow
        '↔': 'iff',         # bidirectional arrow
        'α': 'alpha',
        'β': 'beta',
        'γ': 'gamma',
        'δ': 'delta',
        'ε': 'epsilon',
        'ζ': 'zeta',
        'η': 'eta',
        'θ': 'theta',
        'λ': 'lambda',
        'μ': 'mu',
        'ν': 'nu',
        'π': 'pi',
        'ρ': 'rho',
        'σ': 'sigma',
        'τ': 'tau',
        'φ': 'phi',
        'χ': 'chi',
        'ψ': 'psi',
        'ω': 'omega',
        'Ω': 'Omega',
        'Σ': 'Sigma',
        '∫': 'integral',
        '∂': 'partial',
        '∇': 'nabla',
        '∞': 'infinity',
    }

    for symbol, name in replacements.items():
        title = title.replace(symbol, f' {name} ')

    # Remove remaining problematic characters for filenames
    # Keep only alphanumeric, spaces, hyphens, underscores, and parentheses
    title = re.sub(r'[/\\:*?"<>|]', '', title)

    # Collapse multiple spaces and trim
    title = re.sub(r'\s+', ' ', title).strip()

    # Limit length but keep it readable
    title = title[:150].strip()

    # If title becomes empty after cleaning, use placeholder
    if not title:
        title = 'paper'

    return title



def organize_paper_output(output_dir: Path, metadata: dict, s2_data: dict) -> Path:
    """
    Create organized paper directory structure
    Format: {year}--{title}/
    Returns the new output directory

    Priority: metadata (from handler) > s2_data (from Semantic Scholar)
    This ensures that for books/chapters, we use the correct extracted title, not cached S2 data
    Handles mathematical formulas and HTML tags in titles gracefully.
    """
    try:
        # Prioritize metadata from handler over s2_data
        year = metadata.get('year') or s2_data.get('year') or '0000'
        title = metadata.get('title') or s2_data.get('title') or 'paper'

        # Safety check: ensure year and title are strings
        if not isinstance(year, str):
            year = str(year) if year else '0000'
        if not isinstance(title, str):
            title = str(title) if title else 'paper'

        # Clean title: remove HTML tags and convert math symbols
        title_clean = _clean_title_for_directory(title)

        # Create directory: {year}--{title}
        dir_name = f"{year}--{title_clean}"
        paper_dir = output_dir / dir_name
        paper_dir.mkdir(parents=True, exist_ok=True)

        print(f"  📁 Created paper directory: {dir_name}/")
        return paper_dir
    except Exception as e:
        print(f"  ⚠️  Failed to create directory: {e}")
        import traceback
        traceback.print_exc()
        return output_dir


def save_metadata_json(paper_dir: Path, metadata: dict, s2_data: dict, doi: str,
                      pdf_filename: str = None, supplemental_files: list = None):
    """Save paper metadata as JSON file"""
    try:
        year = s2_data.get('year') or metadata.get('year') or '0000'
        title = s2_data.get('title') or metadata.get('title') or 'paper'

        metadata_json = {
            'doi': doi,
            'title': title,
            'year': year,
            'authors': [item['author'] for item in metadata.get('author_with_affiliations', [])] or metadata.get('authors', []),
            'abstract': metadata.get('abstract', ''),
            'journal': metadata.get('journal', ''),
            'volume': metadata.get('volume'),
            'issue': metadata.get('issue'),
            'pages': metadata.get('pages'),
            'corresponding_author_emails': metadata.get('corresponding_author_emails', []),
            'extracted_at': datetime.now().isoformat(),
            'pdf': pdf_filename,
            'supplemental': supplemental_files if supplemental_files else []
        }

        # Add additional_doi field for books with multiple chapters
        if metadata.get('additional_doi'):
            metadata_json['additional_doi'] = metadata['additional_doi']

        # Add ISBN field if present
        if metadata.get('ISBN'):
            metadata_json['ISBN'] = metadata['ISBN']

        # Add ISSN field if present
        if metadata.get('ISSN'):
            metadata_json['ISSN'] = metadata['ISSN']

        # Save as metadata.json (canonical filename)
        json_file = paper_dir / "metadata.json"
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(metadata_json, f, ensure_ascii=False, indent=2)

        print(f"  ✓ Metadata saved: {json_file.name}")
        return json_file
    except Exception as e:
        print(f"  ⚠️  Failed to save metadata: {e}")
        return None


# ============================================================================
# Publisher Detection
# ============================================================================

def format_references_as_bibtex(references: list) -> str:
    """Convert a list of reference strings into a BibTeX-formatted code block.

    For each reference, attempts to extract a DOI and query Semantic Scholar
    for structured metadata. If no DOI is found or the API call fails, falls
    back to parsing the reference string directly.

    Returns:
        A string containing the full `` ```bibtex `` code block ready for
        insertion into a Markdown document.
    """
    bibtex_entries = []

    for ref_str in references:
        if not ref_str or not ref_str.strip():
            continue

        ref_str = ref_str.strip()

        # If already looks like BibTeX, use as-is
        if ref_str.startswith('@'):
            bibtex_entries.append(ref_str)
            continue

        # Try to find a DOI in the reference string
        doi_match = re.search(r'(10\.\d{4,}/[^\s"\'\]]+)', ref_str)
        if doi_match:
            doi = doi_match.group(1).rstrip('.')
            s2_data = fetch_semanticscholar(doi)
            if s2_data and s2_data.get('title'):
                entry = _build_bibtex_from_s2(s2_data, doi)
                if entry:
                    bibtex_entries.append(entry)
                    continue

        # Fallback: parse as citation_reference string
        try:
            from publisher.wildcard import parse_citation_reference_string
            parsed = parse_citation_reference_string(ref_str)
            if parsed and not parsed.startswith('@'):
                # If it didn't parse well, keep as plain text
                bibtex_entries.append(ref_str)
            else:
                bibtex_entries.append(parsed)
        except Exception:
            bibtex_entries.append(ref_str)

    if not bibtex_entries:
        return ""

    return "```bibtex\n" + "\n\n".join(bibtex_entries) + "\n```"


def _build_bibtex_from_s2(s2_data: dict, doi: str) -> str:
    """Build a BibTeX entry from Semantic Scholar API response."""
    from publisher.wildcard import generate_bibtex_key

    title = s2_data.get('title', '')
    year = s2_data.get('year', '')
    venue = s2_data.get('venue', '')
    if not venue:
        venue = ''

    authors = s2_data.get('authors', [])
    author_names = [a.get('name', '') for a in authors]

    # Generate key
    key = generate_bibtex_key(author_names, str(year) if year else '', title)

    # Format authors as "Last, First"
    formatted_authors = []
    for name in author_names:
        if ',' in name:
            formatted_authors.append(name)
        else:
            parts = name.rsplit(None, 1)
            if len(parts) == 2:
                formatted_authors.append(f"{parts[1]}, {parts[0]}")
            else:
                formatted_authors.append(name)

    lines = ["@article{" + key + ","]
    if formatted_authors:
        lines.append(f"  author = {{{' and '.join(formatted_authors)}}},")
    if title:
        lines.append(f"  title = {{{title}}},")
    if venue:
        # venue from S2 might be journal name or conference
        lines.append(f"  journal = {{{venue}}},")
    if year:
        lines.append(f"  year = {{{year}}},")
    if doi:
        lines.append(f"  doi = {{{doi}}}")
    lines.append("}")

    return "\n".join(lines)


def _build_bibtex_from_crossref(crossref_data: dict, doi: str) -> str:
    """Build a BibTeX entry from Crossref API response."""
    from publisher.wildcard import generate_bibtex_key

    title = crossref_data.get('title', '')
    year = crossref_data.get('year')
    journal = crossref_data.get('journal', '')
    publisher = crossref_data.get('publisher', '')
    volume = crossref_data.get('volume')
    issue = crossref_data.get('issue')
    pages = crossref_data.get('pages')

    authors = crossref_data.get('authors', [])
    author_names = [a.get('name', '') for a in authors if a.get('name')]

    # Generate key
    key = generate_bibtex_key(author_names, str(year) if year else '', title)

    # Format authors as "Last, First"
    formatted_authors = []
    for author in authors:
        family = author.get('family', '')
        given = author.get('given', '')
        if family:
            name = f"{family}, {given}" if given else family
            formatted_authors.append(name)

    lines = ["@article{" + key + ","]
    if formatted_authors:
        lines.append(f"  author = {{{' and '.join(formatted_authors)}}},")
    if title:
        lines.append(f"  title = {{{title}}},")
    if journal:
        lines.append(f"  journal = {{{journal}}},")
    elif publisher:
        lines.append(f"  publisher = {{{publisher}}},")
    if volume:
        lines.append(f"  volume = {{{volume}}},")
    if issue:
        lines.append(f"  issue = {{{issue}}},")
    if pages:
        lines.append(f"  pages = {{{pages}}},")
    if year:
        lines.append(f"  year = {{{year}}},")
    if doi:
        lines.append(f"  doi = {{{doi}}}")
    lines.append("}")

    return "\n".join(lines)


def detect_publisher_from_url(url: str) -> str:
    """
    Detect publisher from URL domain
    Returns: 'aps', 'nature', 'elsevier', 'iop', 'cambridge', etc.
    """
    url_lower = url.lower()

    if 'pubs.aip.org' in url_lower:
        return 'aip'
    elif 'aip.scitation.org' in url_lower:
        return 'aip'
    elif '10.1063' in url_lower:
        return 'aip'
    elif 'journals.aps.org' in url_lower:
        return 'aps'
    elif 'iopscience.iop.org' in url_lower:
        return 'iop'
    elif '10.1088' in url_lower:
        return 'iop'
    elif 'cambridge.org' in url_lower:
        return 'cambridge'
    elif '10.1017' in url_lower:
        return 'cambridge'
    elif 'nature.com' in url_lower:
        return 'nature'
    elif 'sciencedirect.com' in url_lower:
        return 'elsevier'
    elif 'arxiv.org' in url_lower:
        return 'arxiv'
    else:
        return 'unknown'

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


# ============================================================================
# File Organization Functions
# ============================================================================

def organize_paper_output(output_dir: Path, metadata: dict, s2_data: dict) -> Path:
    """
    Create organized paper directory structure
    Format: {year}--{title}/
    Returns the new output directory
    """
    try:
        year = s2_data.get('year') or metadata.get('year') or '0000'
        title = s2_data.get('title') or metadata.get('title') or 'paper'

        # Clean title of special characters
        title_clean = re.sub(r'[/\\:*?"<>|]', '-', title)[:80].strip()

        # Create directory: {year}--{title}
        dir_name = f"{year}--{title_clean}"
        paper_dir = output_dir / dir_name
        paper_dir.mkdir(parents=True, exist_ok=True)

        print(f"  📁 Created paper directory: {dir_name}/")
        return paper_dir
    except Exception as e:
        print(f"  ⚠️  Failed to create directory: {e}")
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

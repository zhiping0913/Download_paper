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
        title_clean = re.sub(r'[/\\:*?"<>|]', '-', title)[:120]

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
        title_clean = re.sub(r'[/\\:*?"<>|]', '-', title)[:120]

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

        # Save as {year}--{title}.json
        json_file = paper_dir / f"{year}--{title_clean}.json"
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

def detect_publisher_from_url(url: str) -> str:
    """
    Detect publisher from URL domain
    Returns: 'aps', 'nature', 'elsevier', etc.
    """
    url_lower = url.lower()

    if 'journals.aps.org' in url_lower:
        return 'aps'
    elif 'nature.com' in url_lower:
        return 'nature'
    elif 'sciencedirect.com' in url_lower:
        return 'elsevier'
    elif 'arxiv.org' in url_lower:
        return 'arxiv'
    else:
        return 'unknown'

"""
Core module: Generic utilities and functions for paper extraction
"""

from .utilities import (
    fetch_semanticscholar,
    fetch_crossref,
    format_references_as_bibtex,
    organize_paper_output,
    save_metadata_json,
    detect_publisher_from_url,
    block_mathjax,
    S2_API_URL,
    CROSSREF_API_URL,
    HEADERS,
    _build_bibtex_from_crossref,
)
from .network_capture import setup_response_capture

__all__ = [
    'fetch_semanticscholar',
    'fetch_crossref',
    'format_references_as_bibtex',
    'organize_paper_output',
    'save_metadata_json',
    'detect_publisher_from_url',
    'block_mathjax',
    'S2_API_URL',
    'CROSSREF_API_URL',
    'HEADERS',
    'setup_response_capture',
    '_build_bibtex_from_crossref',
]

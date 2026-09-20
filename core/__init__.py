"""
Core module: Generic utilities and functions for paper extraction
"""

from .utilities import (
    fetch_semanticscholar,
    fetch_crossref,
    organize_paper_output,
    save_metadata_json,
    save_crossref_json,
    detect_publisher_from_url,
    S2_API_URL,
    CROSSREF_API_URL,
    HEADERS,
)
from .network_capture import setup_response_capture

__all__ = [
    'fetch_semanticscholar',
    'fetch_crossref',
    'organize_paper_output',
    'save_metadata_json',
    'save_crossref_json',
    'detect_publisher_from_url',
    'S2_API_URL',
    'CROSSREF_API_URL',
    'HEADERS',
    'setup_response_capture',
]

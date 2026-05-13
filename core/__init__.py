"""
Core module: Generic utilities and functions for paper extraction
"""

from .utilities import (
    fetch_semanticscholar,
    organize_paper_output,
    save_metadata_json,
    add_equation_numbers,
    mathml_to_latex_pandoc,
    extract_text_without_math,
    detect_publisher_from_url,
    S2_API_URL,
    HEADERS
)
from .network_capture import setup_response_capture

__all__ = [
    'fetch_semanticscholar',
    'organize_paper_output',
    'save_metadata_json',
    'add_equation_numbers',
    'mathml_to_latex_pandoc',
    'extract_text_without_math',
    'detect_publisher_from_url',
    'S2_API_URL',
    'HEADERS',
    'setup_response_capture',
]

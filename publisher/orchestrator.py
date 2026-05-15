#!/usr/bin/env python3
"""
Multi-Publisher Extraction Orchestrator
Routes to appropriate publisher handler based on DOI/URL
"""

from publisher.base import PublisherHandler
from publisher import APSHandler, AIPHandler, CambridgeHandler
from publisher.nature import NatureHandler


def detect_publisher_from_url(url: str) -> str:
    """
    Detect publisher from URL or DOI pattern

    Returns: 'aps' | 'nature' | 'aip' | 'unknown'
    """
    url_lower = url.lower()

    # Nature/Springer detection
    if 'nature.com' in url_lower:
        return 'nature'
    elif 'springer.com' in url_lower:
        return 'nature'
    elif '10.1038' in url_lower:  # Nature DOI
        return 'nature'
    elif 's41' in url_lower:  # Nature journal ID pattern
        return 'nature'

    # APS detection
    elif 'journals.aps.org' in url_lower:
        return 'aps'
    elif '10.1103' in url_lower:  # APS DOI
        return 'aps'
    elif 'prl' in url_lower or 'pre' in url_lower or 'pra' in url_lower:
        return 'aps'

    # Cambridge University Press detection
    elif 'cambridge.org' in url_lower:
        return 'cambridge'
    elif '10.1017' in url_lower:
        return 'cambridge'

    # AIP Publishing detection
    elif 'pubs.aip.org' in url_lower:
        return 'aip'
    elif 'aip.scitation.org' in url_lower:
        return 'aip'
    elif '10.1063' in url_lower:
        return 'aip'

    # ArXiv (treat as generic, default to APS-like handling)
    elif 'arxiv.org' in url_lower:
        return 'arxiv'

    return 'unknown'


def get_publisher_handler(publisher: str, **kwargs) -> PublisherHandler:
    """
    Factory function to instantiate appropriate publisher handler

    Args:
        publisher: Publisher name ('aps', 'nature', etc.)
        **kwargs: Additional arguments for handler initialization

    Returns:
        PublisherHandler instance
    """
    if publisher == 'nature':
        return NatureHandler(**kwargs)
    elif publisher == 'aip':
        return AIPHandler(**kwargs)
    elif publisher == 'cambridge':
        return CambridgeHandler(**kwargs)
    elif publisher == 'aps':
        return APSHandler(**kwargs)
    elif publisher == 'arxiv':
        # ArXiv uses APS-like handler
        kwargs.setdefault('journal_prefix', 'arxiv')
        return APSHandler(**kwargs)
    else:
        # Default to APS handler for unknown
        return APSHandler(**kwargs)


async def extract_metadata_multi_publisher(page, publisher: str = None) -> dict:
    """
    Extract metadata using appropriate publisher handler

    Automatically detects publisher if not specified

    Args:
        page: Playwright page object
        publisher: Publisher name (auto-detect if None)

    Returns:
        Dictionary with extracted metadata
    """
    # Auto-detect if not specified
    if not publisher:
        publisher = detect_publisher_from_url(page.url)

    # Get appropriate handler
    handler = get_publisher_handler(publisher)

    # Extract metadata using handler
    metadata = await handler.extract_metadata(page)

    return metadata, handler, publisher


__all__ = [
    'detect_publisher_from_url',
    'get_publisher_handler',
    'extract_metadata_multi_publisher'
]

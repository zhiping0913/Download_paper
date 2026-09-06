#!/usr/bin/env python3
"""
Multi-Publisher Extraction Orchestrator
Routes to appropriate publisher handler based on DOI/URL
"""

from publisher.base import PublisherHandler
from publisher import APSHandler, AIPHandler, CambridgeHandler
from publisher.nature import NatureHandler
from publisher.iop import IOPHandler
from publisher.springer_book import SpringerBookHandler
from publisher.optica import OpticaHandler
from publisher.science import ScienceHandler
from publisher.sciencedirect import ScienceDirectHandler
from publisher.oup import OupHandler
from publisher.oup_book import OupBookHandler
from publisher.mdpi import MDPIHandler
from publisher.acm import ACMHandler
from publisher.ieee import IEEEHandler


def detect_publisher_from_url(url: str) -> str:
    """
    Detect publisher from URL or DOI pattern

    Returns: 'aps' | 'nature' | 'aip' | 'unknown'

    Detection order is important: more specific matches first to avoid false positives.
    For example, AIP's DOI (10.1063) must be checked before APS's loose substring matches.
    """
    url_lower = url.lower()

    # Springer Book/Chapter detection (BEFORE general Springer/Nature detection)
    # Springer book paths include /book/, /chapter/, /referencework/, /referenceworkentry/
    if ('springer.com/book' in url_lower
            or 'springer.com/chapter' in url_lower
            or 'springer.com/referencework' in url_lower):
        return 'springer_book'

    # Nature/Springer detection (most specific first)
    if 'nature.com' in url_lower:
        return 'nature'
    elif 'springer.com' in url_lower:
        return 'nature'
    elif '10.1038' in url_lower:  # Nature DOI
        return 'nature'
    elif 's41' in url_lower:  # Nature journal ID pattern
        return 'nature'

    # IOP Publishing detection (very specific)
    elif 'iopscience.iop.org' in url_lower:
        return 'iop'
    elif '10.1088' in url_lower:
        return 'iop'

    # Optica Publishing detection
    elif 'opg.optica.org' in url_lower or 'opticapublishing' in url_lower:
        return 'optica'
    elif '10.1364' in url_lower:
        return 'optica'

    # AIP Publishing detection (BEFORE APS loose matching!)
    # Must check specific URLs and DOI prefix before APS patterns
    elif 'pubs.aip.org' in url_lower:
        return 'aip'
    elif 'aip.scitation.org' in url_lower:
        return 'aip'
    elif 'physicstoday.aip.org' in url_lower:
        return 'aip'
    elif '10.1063' in url_lower:  # AIP DOI - check BEFORE APS loose patterns
        return 'aip'

    # Cambridge University Press detection
    elif 'cambridge.org' in url_lower:
        return 'cambridge'
    elif '10.1017' in url_lower:
        return 'cambridge'

    # Oxford University Press detection
    elif 'academic.oup.com' in url_lower:
        return 'oup'
    elif '10.1093' in url_lower:
        return 'oup'

    # MDPI detection
    elif 'mdpi.com' in url_lower:
        return 'mdpi'
    elif '10.3390' in url_lower:
        return 'mdpi'

    # ACM Digital Library — abstract-only support, headed access required
    elif 'dl.acm.org' in url_lower:
        return 'acm'
    elif '10.1145' in url_lower:
        return 'acm'

    # IEEE Xplore
    elif 'ieeexplore.ieee.org' in url_lower:
        return 'ieee'
    elif '10.1109' in url_lower:
        return 'ieee'

    # Science / AAAS vs ScienceDirect / Elsevier
    # -----------------------------------------
    # Both publishers contain the string "science", so we MUST match on the
    # full discriminating substrings — not just "science":
    #   * Science (AAAS):       DOI prefix 10.1126, domain science.org
    #   * ScienceDirect (RELX): DOI prefix 10.1016, domain sciencedirect.com
    #                                              (+ linkinghub.elsevier.com redirect)
    # "science.org" and "sciencedirect.com" are non-overlapping (no ".org" in
    # sciencedirect.com), so the two branches cannot misclassify each other
    # regardless of order. We test ScienceDirect first because its primary
    # redirect host (linkinghub.elsevier.com) also has a 10.1016 DOI.
    elif 'sciencedirect.com' in url_lower:
        return 'sciencedirect'
    elif 'linkinghub.elsevier.com' in url_lower:
        return 'sciencedirect'
    elif '10.1016' in url_lower:
        return 'sciencedirect'
    elif 'science.org' in url_lower:
        return 'science'
    elif '10.1126' in url_lower:
        return 'science'

    # APS detection (least specific - loose substring matching)
    # WARNING: These patterns are vague and may match other content.
    # Must be checked AFTER more specific publishers.
    elif 'journals.aps.org' in url_lower:
        return 'aps'
    elif '10.1103' in url_lower:  # APS DOI
        return 'aps'
    elif 'prl' in url_lower or 'pra' in url_lower:
        # Note: removed 'pre' check as it matches too many words (e.g., "Compressing")
        # Only check well-defined APS journal abbreviations
        return 'aps'

    # EDP Sciences (routes through NatureHandler fallbacks)
    elif 'epj-conferences.org' in url_lower:
        return 'nature'
    elif '10.1051' in url_lower:
        return 'nature'

    # Pleiades Publishing — delivered via link.springer.com, handled by NatureHandler
    elif '10.1134' in url_lower:
        return 'nature'

    # ArXiv (treat as generic, default to APS-like handling)
    elif 'arxiv.org' in url_lower:
        return 'arxiv'

    return 'unknown'


def get_publisher_handler(publisher: str, **kwargs) -> PublisherHandler:
    """
    Factory function to instantiate appropriate publisher handler

    Args:
        publisher: Publisher name ('aps', 'nature', 'springer_book', etc.)
        **kwargs: Additional arguments for handler initialization

    Returns:
        PublisherHandler instance
    """
    if publisher == 'nature':
        return NatureHandler(**kwargs)
    elif publisher == 'springer_book':
        return SpringerBookHandler(**kwargs)
    elif publisher == 'iop':
        return IOPHandler(**kwargs)
    elif publisher == 'optica':
        return OpticaHandler(**kwargs)
    elif publisher == 'aip':
        return AIPHandler(**kwargs)
    elif publisher == 'cambridge':
        return CambridgeHandler(**kwargs)
    elif publisher == 'oup':
        return OupHandler(**kwargs)
    elif publisher == 'oup_book':
        return OupBookHandler(**kwargs)
    elif publisher == 'mdpi':
        return MDPIHandler(**kwargs)
    elif publisher == 'acm':
        return ACMHandler(**kwargs)
    elif publisher == 'ieee':
        return IEEEHandler(**kwargs)
    elif publisher == 'science':
        return ScienceHandler(**kwargs)
    elif publisher == 'sciencedirect':
        return ScienceDirectHandler(**kwargs)
    elif publisher == 'aps':
        return APSHandler(**kwargs)
    elif publisher == 'arxiv':
        # ArXiv uses APS-like handler
        kwargs.setdefault('journal_prefix', 'arxiv')
        return APSHandler(**kwargs)
    else:
        # Default to oup handler for unknown
        return OupHandler(**kwargs)


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

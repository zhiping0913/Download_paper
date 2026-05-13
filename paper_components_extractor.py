#!/usr/bin/env python3
"""
Unified paper component extraction module
Consolidates all extract_*.py functions for extracting different parts of academic papers
Functions for extracting metadata, references, supplementary materials, etc.
"""

import json
import re
import html
from pathlib import Path
from bs4 import BeautifulSoup


# ============================================================================
# Metadata Extraction
# ============================================================================

def extract_metadata_from_html(html_content: str) -> dict:
    """Extract JSON-LD metadata from HTML content"""
    soup = BeautifulSoup(html_content, 'html.parser')
    ld_json_script = soup.find('script', {'type': 'application/ld+json'})

    if not ld_json_script:
        print("❌ JSON-LD metadata not found")
        return {}

    try:
        metadata = json.loads(ld_json_script.string)
        main_entity = metadata.get('mainEntity', {})
        print(f"✓ JSON-LD metadata parsed successfully")
        return main_entity
    except json.JSONDecodeError as e:
        print(f"❌ JSON parsing error: {e}")
        return {}


def format_metadata_to_markdown(metadata: dict) -> str:
    """Convert metadata to markdown format"""
    md_parts = []

    # Title
    headline = metadata.get('headline', '')
    if headline:
        md_parts.append(f"# {headline}\n")

    # Authors
    authors = metadata.get('author', [])
    if authors:
        md_parts.append("## Authors\n")
        for author in authors:
            name = author.get('name', '')
            orcid = author.get('url', '')
            email = author.get('email', '')
            affiliations = author.get('affiliation', [])

            if name:
                md_parts.append(f"- **{name}**")
                if orcid:
                    md_parts.append(f"  ORCID: {orcid.split('/')[-1]}")
                if affiliations:
                    for aff in affiliations:
                        aff_address = aff.get('address', {})
                        aff_text = aff_address.get('name', '') if isinstance(aff_address, dict) else ''
                        if aff_text:
                            md_parts.append(f"  {aff_text}")

        # Corresponding authors
        corresponding = [a for a in authors if a.get('email')]
        if corresponding:
            md_parts.append("\n**Corresponding authors:**")
            for author in corresponding:
                name = author.get('name', '')
                email = author.get('email', '')
                if email:
                    md_parts.append(f"- {email}")
        md_parts.append("")

    # Publication info
    md_parts.append("## Publication\n")

    doi = metadata.get('sameAs', '')
    if doi:
        doi_short = doi.replace('https://doi.org/', '')
        md_parts.append(f"**DOI:** {doi_short}\n")

    is_part_of = metadata.get('isPartOf', {})
    if is_part_of:
        journal_name = is_part_of.get('name', '')
        if journal_name:
            md_parts.append(f"**Journal:** {journal_name}\n")

    date_published = metadata.get('datePublished', '')
    if date_published:
        year = date_published.split('-')[0]
        md_parts.append(f"**Year:** {year}\n")

    volume = is_part_of.get('volumeNumber', '') if is_part_of else ''
    if volume:
        md_parts.append(f"**Volume:** {volume}\n")

    page_start = metadata.get('pageStart', '')
    page_end = metadata.get('pageEnd', '')
    if page_start and page_end:
        md_parts.append(f"**Pages:** {page_start}-{page_end}\n")

    keywords = metadata.get('keywords', [])
    if keywords:
        md_parts.append(f"**Keywords:** {', '.join(keywords)}\n")

    md_parts.append("\n---\n")
    return "\n".join(md_parts)


def save_metadata_to_file(metadata_md: str, output_file: str):
    """Save metadata to file"""
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(metadata_md)
    print(f"✓ Saved to: {output_file}")


# ============================================================================
# References Extraction
# ============================================================================

def extract_references_from_html(html_content: str) -> list:
    """Extract references from HTML content"""
    soup = BeautifulSoup(html_content, 'html.parser')

    # Try Nature format first
    refs_list = soup.find('ol', {'class': 'c-article-references'})
    if not refs_list:
        print("❌ References not found")
        return []

    references = []
    for item in refs_list.find_all('li', {'class': 'c-article-references__item'}):
        counter = item.get('data-counter', '')
        number = counter.replace('.', '').strip()

        ref_text_p = item.find('p', {'class': 'c-article-references__text'})
        if not ref_text_p:
            continue

        ref_text = ref_text_p.get_text(strip=True)
        ref_text = html.unescape(ref_text)

        # Extract DOI if available
        doi = None
        doi_link = item.find('a', {'data-doi': True})
        if doi_link:
            doi = doi_link.get('data-doi')

        # Format reference
        formatted_ref = f"[{number}] {ref_text}"
        if doi:
            formatted_ref += f" DOI: {doi}"

        references.append(formatted_ref)

        if len(references) <= 5 or len(references) % 10 == 0:
            print(f"✓ Reference {number}: {len(ref_text)} chars")

    return references


def save_references(references: list, output_file: str):
    """Save references to file"""
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("# References\n\n")
        for ref in references:
            f.write(ref + "\n\n")

    print(f"\n✅ References saved")
    print(f"   Output: {output_file}")
    print(f"   Count: {len(references)} references")


# ============================================================================
# Supplementary Materials Extraction
# ============================================================================

def extract_supplementary_information_from_html(html_content: str) -> str:
    """Extract supplementary information from HTML content"""
    soup = BeautifulSoup(html_content, 'html.parser')
    supp_section = soup.find('section', {'data-title': 'Supplementary information'})

    if not supp_section:
        print("❌ Supplementary information section not found")
        return ""

    content_div = supp_section.find('div', {'class': 'c-article-section__content'})
    if not content_div:
        print("❌ Supplementary information content not found")
        return ""

    items = content_div.find_all('div', {'class': 'c-article-supplementary__item'})
    if not items:
        print("❌ No supplementary items found")
        return ""

    print(f"✓ Found {len(items)} supplementary items")

    md_parts = []
    for i, item in enumerate(items, 1):
        title = item.find('a', {'class': 'print-link'})
        if title:
            title_text = title.get_text(strip=True)
            href = title.get('href', '')
            md_parts.append(f"{i}. [{title_text}]({href})")

    if not md_parts:
        print("❌ No valid items found")
        return ""

    result = "\n".join(md_parts)
    print(f"✓ Supplementary information converted: {len(result)} chars")
    return result


# ============================================================================
# Extended Data Extraction
# ============================================================================

def extract_extended_data_from_html(html_content: str) -> str:
    """Extract extended data figures and tables from HTML content"""
    soup = BeautifulSoup(html_content, 'html.parser')
    extended_section = soup.find('section', {'data-title': 'Extended data figures and tables'})

    if not extended_section:
        print("❌ Extended data section not found")
        return ""

    items = extended_section.find_all('div', {'class': 'c-article-supplementary__item'})
    if not items:
        print("❌ No extended data items found")
        return ""

    print(f"✓ Found {len(items)} extended data items")

    md_parts = []
    for i, item in enumerate(items, 1):
        title = item.find('a', {'class': 'print-link'})
        if title:
            title_text = title.get_text(strip=True)
            href = title.get('href', '')

            # Get description
            desc_div = item.find('div', {'class': 'c-article-supplementary__description'})
            desc_text = ""
            if desc_div:
                desc_text = desc_div.get_text(strip=True)
                desc_text = re.sub(r'\s+', ' ', desc_text)

            md_parts.append(f"{i}. [{title_text}]({href})")
            if desc_text:
                md_parts.append(f"\n   {desc_text}\n")

    if not md_parts:
        print("❌ No valid items found")
        return ""

    result = "\n".join(md_parts)
    print(f"✓ Extended data converted: {len(result)} chars")
    return result


# ============================================================================
# Abstract Extraction
# ============================================================================

def extract_abstract_from_html(html_content: str) -> str:
    """Extract abstract from HTML content"""
    soup = BeautifulSoup(html_content, 'html.parser')

    # Try to find abstract section
    abstract_section = soup.find('section', {'data-title': 'Abstract'})
    if not abstract_section:
        abstract_section = soup.find('div', {'class': re.compile('.*abstract.*', re.I)})

    if not abstract_section:
        print("❌ Abstract section not found")
        return ""

    abstract_text = abstract_section.get_text(strip=True)
    return abstract_text


# ============================================================================
# Acknowledgements Extraction
# ============================================================================

def extract_acknowledgements_from_html(html_content: str) -> str:
    """Extract acknowledgements from HTML content"""
    soup = BeautifulSoup(html_content, 'html.parser')

    ack_section = soup.find('section', {'data-title': 'Acknowledgements'})
    if not ack_section:
        print("❌ Acknowledgements section not found")
        return ""

    ack_text = ack_section.get_text(strip=True)
    return ack_text


# ============================================================================
# Author Information Extraction
# ============================================================================

def extract_author_information_from_html(html_content: str) -> str:
    """Extract author information from HTML content"""
    soup = BeautifulSoup(html_content, 'html.parser')

    author_section = soup.find('section', {'data-title': 'Author information'})
    if not author_section:
        print("❌ Author information section not found")
        return ""

    author_text = author_section.get_text(strip=True)
    return author_text


# ============================================================================
# Data Availability Extraction
# ============================================================================

def extract_data_availability_from_html(html_content: str) -> str:
    """Extract data availability from HTML content"""
    soup = BeautifulSoup(html_content, 'html.parser')

    data_section = soup.find('section', {'data-title': 'Data availability'})
    if not data_section:
        print("❌ Data availability section not found")
        return ""

    data_text = data_section.get_text(strip=True)
    return data_text


__all__ = [
    # Metadata
    'extract_metadata_from_html',
    'format_metadata_to_markdown',
    'save_metadata_to_file',
    # References
    'extract_references_from_html',
    'save_references',
    # Supplementary
    'extract_supplementary_information_from_html',
    # Extended Data
    'extract_extended_data_from_html',
    # Abstract
    'extract_abstract_from_html',
    # Acknowledgements
    'extract_acknowledgements_from_html',
    # Author Info
    'extract_author_information_from_html',
    # Data Availability
    'extract_data_availability_from_html',
]

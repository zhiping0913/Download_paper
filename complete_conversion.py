#!/usr/bin/env python3
"""
Complete paper conversion workflow:
1. Extract metadata
2. Extract abstract
3. Extract references
4. Extract supplementary materials
5. Merge into complete document
"""

import argparse
import html
import json
import re
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
        print("✓ JSON-LD metadata parsed successfully")
        return main_entity
    except json.JSONDecodeError as e:
        print(f"❌ JSON parsing error: {e}")
        return {}


def format_metadata_to_markdown(metadata: dict) -> str:
    """Convert metadata to markdown format"""
    md_parts = []

    headline = metadata.get('headline', '')
    if headline:
        md_parts.append(f"# {headline}\n")

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

        corresponding = [a for a in authors if a.get('email')]
        if corresponding:
            md_parts.append("\n**Corresponding authors:**")
            for author in corresponding:
                email = author.get('email', '')
                if email:
                    md_parts.append(f"- {email}")
        md_parts.append("")

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

        doi = None
        doi_link = item.find('a', {'data-doi': True})
        if doi_link:
            doi = doi_link.get('data-doi')

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

    print("\n✅ References saved")
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

    abstract_section = soup.find('section', {'data-title': 'Abstract'})
    if not abstract_section:
        abstract_section = soup.find('div', {'class': re.compile('.*abstract.*', re.I)})

    if not abstract_section:
        print("❌ Abstract section not found")
        return ""

    return abstract_section.get_text(strip=True)


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

    return ack_section.get_text(strip=True)


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

    return author_section.get_text(strip=True)


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

    return data_section.get_text(strip=True)


def run_conversion_workflow(html_file: str, output_dir: str):
    """Execute complete paper conversion workflow"""

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Read HTML content once
    with open(html_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    print("=" * 80)
    print("🔄 Complete Paper Conversion Workflow")
    print("=" * 80)

    # 0. Extract metadata
    print("\n0️⃣  Extracting paper metadata...")
    metadata = extract_metadata_from_html(html_content)
    metadata_md = format_metadata_to_markdown(metadata)

    if metadata_md:
        print(f"✓ Metadata extracted: {len(metadata_md)} characters")
    else:
        print("⚠️  No metadata found")

    # 1. Extract abstract
    print("\n1️⃣  Extracting abstract...")
    abstract = extract_abstract_from_html(html_content)
    if abstract:
        abstract_md = f"## Abstract\n\n{abstract}\n"
        print(f"✓ Abstract extracted: {len(abstract)} characters")
    else:
        abstract_md = ""
        print("⚠️  No abstract found")

    # 2. Extract references
    print("\n2️⃣  Extracting references...")
    references_list = extract_references_from_html(html_content)
    if references_list:
        references_md = "## References\n\n" + "\n\n".join(references_list) + "\n"
        print(f"✓ References extracted: {len(references_list)} items")
    else:
        references_md = ""
        print("⚠️  No references found")

    # 3. Extract supplementary information
    print("\n3️⃣  Extracting supplementary information...")
    supp_md = extract_supplementary_information_from_html(html_content)
    if supp_md:
        supp_section = f"## Supplementary information\n\n{supp_md}\n"
        print(f"✓ Supplementary info extracted: {len(supp_md)} characters")
    else:
        supp_section = ""
        print("⚠️  No supplementary information found")

    # 4. Extract extended data
    print("\n4️⃣  Extracting extended data figures and tables...")
    extended_md = extract_extended_data_from_html(html_content)
    if extended_md:
        extended_section = f"## Extended data\n\n{extended_md}\n"
        print(f"✓ Extended data extracted: {len(extended_md)} characters")
    else:
        extended_section = ""
        print("⚠️  No extended data found")

    # 5. Extract data availability
    print("\n5️⃣  Extracting data availability...")
    data_avail = extract_data_availability_from_html(html_content)
    if data_avail:
        data_avail_section = f"## Data availability\n\n{data_avail}\n"
        print(f"✓ Data availability extracted: {len(data_avail)} characters")
    else:
        data_avail_section = "## Data availability\n\nData available upon request from corresponding authors.\n"
        print("⚠️  No data availability info found")

    # 6. Extract acknowledgements
    print("\n6️⃣  Extracting acknowledgements...")
    ack_md = extract_acknowledgements_from_html(html_content)
    if ack_md:
        ack_section = f"## Acknowledgements\n\n{ack_md}\n"
        print(f"✓ Acknowledgements extracted: {len(ack_md)} characters")
    else:
        ack_section = ""
        print("⚠️  No acknowledgements found")

    # 7. Merge into complete document
    print("\n7️⃣  Merging into complete document...")
    complete_doc = f"""{metadata_md}
{abstract_md}{data_avail_section}{ack_section}{extended_section}{supp_section}{references_md}"""

    complete_file = output_path / "paper_complete.md"
    with open(complete_file, 'w', encoding='utf-8') as f:
        f.write(complete_doc)

    print("\n" + "=" * 80)
    print("📊 Conversion Statistics")
    print("=" * 80)
    metadata_lines = len(metadata_md.splitlines()) if metadata_md else 0
    abstract_lines = len(abstract_md.splitlines()) if abstract_md else 0
    ref_lines = len(references_md.splitlines()) if references_md else 0
    data_avail_lines = len(data_avail_section.splitlines()) if data_avail_section else 0
    ack_lines = len(ack_section.splitlines()) if ack_section else 0
    extended_lines = len(extended_section.splitlines()) if extended_section else 0
    supp_lines = len(supp_section.splitlines()) if supp_section else 0
    total_chars = len(complete_doc)

    print(f"Metadata: {metadata_lines} lines")
    print(f"Abstract: {abstract_lines} lines")
    print(f"Data availability: {data_avail_lines} lines")
    print(f"Acknowledgements: {ack_lines} lines")
    print(f"Extended data: {extended_lines} lines")
    print(f"Supplementary info: {supp_lines} lines")
    print(f"References: {ref_lines} lines")
    print(f"Total characters: {total_chars:,}")
    print(f"\n✅ Workflow complete!")
    print(f"   Output file: {complete_file}")
    print(f"\nDocument structure:")
    print(f"   1. Metadata (title, authors, affiliations, keywords)")
    print(f"   2. Abstract")
    print(f"   3. Data availability")
    print(f"   4. Acknowledgements")
    print(f"   5. Extended data")
    print(f"   6. Supplementary information")
    print(f"   7. References")


def find_default_html_file(project_root: Path) -> Path:
    """Find a usable default HTML file when the legacy path is absent."""
    captured_data = project_root / "captured_data"
    legacy_default = captured_data / "page_000.html"
    if legacy_default.exists():
        return legacy_default

    candidates = sorted(captured_data.glob("**/*.html"))
    for preferred_name in ("page.html", "page_000.html", "headless_initial.html"):
        for candidate in candidates:
            if candidate.name == preferred_name:
                return candidate

    if candidates:
        return candidates[0]
    return legacy_default


def main():
    """Main function"""
    project_root = Path(__file__).resolve().parent
    default_html_file = find_default_html_file(project_root)
    default_output_dir = default_html_file.parent if default_html_file.exists() else project_root / "captured_data"

    parser = argparse.ArgumentParser(description="Convert captured paper HTML into a complete Markdown document.")
    parser.add_argument("html_file", nargs="?", default=str(default_html_file), help="Path to captured paper HTML.")
    parser.add_argument("output_dir", nargs="?", default=str(default_output_dir), help="Directory for paper_complete.md.")
    args = parser.parse_args()

    html_file = Path(args.html_file)
    output_dir = Path(args.output_dir)

    if not html_file.exists():
        raise FileNotFoundError(f"HTML file not found: {html_file}")

    run_conversion_workflow(html_file, output_dir)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Complete paper conversion workflow:
1. Extract metadata
2. Extract abstract
3. Extract references
4. Extract supplementary materials
5. Merge into complete document
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent))

from paper_components_extractor import (
    extract_metadata_from_html,
    format_metadata_to_markdown,
    extract_abstract_from_html,
    extract_references_from_html,
    extract_supplementary_information_from_html,
    extract_extended_data_from_html,
    extract_data_availability_from_html,
    extract_acknowledgements_from_html,
)


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
    extended_lines = len(extended_section.splitlines()) if extended_section else 0
    supp_lines = len(supp_section.splitlines()) if supp_section else 0
    total_chars = len(complete_doc)

    print(f"Metadata: {metadata_lines} lines")
    print(f"Abstract: {abstract_lines} lines")
    print(f"Data availability: 2 lines")
    print(f"Acknowledgements: 2 lines")
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




def main():
    """Main function"""
    html_file = "/home/zhiping/Projects/Download_paper/captured_data/page_000.html"
    output_dir = "/home/zhiping/Projects/Download_paper/captured_data"

    run_conversion_workflow(html_file, output_dir)


if __name__ == "__main__":
    main()

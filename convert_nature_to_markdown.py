#!/usr/bin/env python3
"""
Convert Nature article to Markdown
Extracts metadata, figures, references and converts to markdown format
"""

import asyncio
import sys
sys.path.insert(0, '/home/zhiping/Projects/Download_paper')

from playwright.async_api import async_playwright
from publisher.nature import NatureHandler
from pathlib import Path


async def convert_nature_to_markdown(url: str, output_dir: str = None):
    """
    Convert Nature article to Markdown

    Args:
        url: Nature article URL
        output_dir: Output directory (default: current directory)
    """

    if output_dir is None:
        output_dir = "/home/zhiping/Projects/Download_paper/nature_articles"

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    print("\n" + "="*80)
    print("🔄 CONVERTING NATURE ARTICLE TO MARKDOWN")
    print("="*80)
    print(f"📍 URL: {url}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1920, 'height': 1080})

        # Load page
        print("📄 Loading page...")
        await page.goto(url, wait_until="load", timeout=20000)
        print("✅ Page loaded\n")

        # Initialize handler
        handler = NatureHandler()

        # Extract metadata
        print("Step 1️⃣  Extracting Metadata...")
        metadata = await handler.extract_metadata(page)
        print(f"  ✅ Title: {metadata.get('title', 'N/A')[:60]}...")
        print(f"  ✅ Authors: {len(metadata.get('authors', []))} found")
        print(f"  ✅ DOI: {metadata.get('doi', 'N/A')}")

        # Extract figures
        print("\nStep 2️⃣  Extracting Figures...")
        figures = await handler.get_figures(page)
        print(f"  ✅ Figures: {len(figures)} found")

        # Extract references
        print("\nStep 3️⃣  Extracting References...")
        references = await handler.extract_references(page)
        print(f"  ✅ References: {len(references)} found")

        # Get article content
        print("\nStep 4️⃣  Extracting Article Content...")
        article_html = await page.evaluate("() => document.body.innerHTML")
        print(f"  ✅ Content extracted: {len(article_html)} bytes")

        # Generate markdown
        print("\nStep 5️⃣  Generating Markdown...")
        markdown = handler.convert_to_markdown(metadata)

        # Add more details to markdown
        markdown += "\n---\n\n"
        markdown += "## Figures\n\n"

        if figures:
            for fig_id, fig_data in list(figures.items()):
                caption = fig_data.get('caption', 'No caption')
                url_full = fig_data.get('url', '')
                markdown += f"### {fig_id}\n"
                markdown += f"**Caption**: {caption}\n"
                markdown += f"**URL**: {url_full}\n\n"

        # Save markdown
        artid = metadata.get('doi', 'unknown').replace('/', '_').replace('10.1038_', '')
        md_file = Path(output_dir) / f"{artid}.md"

        with open(md_file, 'w', encoding='utf-8') as f:
            f.write(markdown)

        print(f"\n✅ Markdown saved: {md_file}")
        print(f"   Size: {len(markdown)} characters")
        print(f"   Lines: {len(markdown.splitlines())}")

        # Print preview
        print("\n" + "="*80)
        print("📋 MARKDOWN PREVIEW (First 500 chars)")
        print("="*80)
        print(markdown[:500])
        print("\n... (truncated)")

        await browser.close()

        return {
            'output_file': str(md_file),
            'metadata': metadata,
            'figures_count': len(figures),
            'references_count': len(references),
            'markdown_size': len(markdown)
        }


async def main():
    """Main entry point"""

    # Convert Nature Physics paper
    url = "https://www.nature.com/articles/s41567-019-0584-7"

    result = await convert_nature_to_markdown(url)

    print("\n" + "="*80)
    print("✅ CONVERSION COMPLETE")
    print("="*80)
    print(f"\n📁 Output file: {result['output_file']}")
    print(f"📊 Metadata fields: {sum(1 for v in result['metadata'].values() if v)}")
    print(f"🖼️  Figures: {result['figures_count']}")
    print(f"📚 References: {result['references_count']}")
    print(f"📄 Markdown size: {result['markdown_size']} characters\n")


if __name__ == "__main__":
    asyncio.run(main())

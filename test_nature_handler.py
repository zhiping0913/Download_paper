#!/usr/bin/env python3
"""
Test NatureHandler implementation with two Nature papers
Paper 1: https://www.nature.com/articles/s41586-026-10400-2 (Nature 2026)
Paper 2: https://www.nature.com/articles/s41567-019-0584-7 (Nature Physics 2019)
"""

import asyncio
import sys
sys.path.insert(0, '/home/zhiping/Projects/Download_paper')

from playwright.async_api import async_playwright
from publisher.nature import NatureHandler


async def test_nature_handler(url: str, paper_name: str):
    """Test NatureHandler on a specific paper"""

    print("\n" + "="*80)
    print(f"TESTING: {paper_name}")
    print(f"URL: {url}")
    print("="*80)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1920, 'height': 1080})

        # Navigate to paper
        print("\n📍 Loading page...")
        await page.goto(url, wait_until="load", timeout=20000)
        print(f"✅ Loaded: {page.url}")

        # Test NatureHandler
        handler = NatureHandler()

        # Test metadata extraction
        print("\n📊 Extracting Metadata...")
        metadata = await handler.extract_metadata(page)

        # Test figure extraction
        print("\n🖼️  Extracting Figures...")
        figures = await handler.get_figures(page)

        # Test reference extraction
        print("\n📚 Extracting References...")
        references = await handler.extract_references(page)

        # Test PDF URL detection
        print("\n📄 Looking for PDF...")
        pdf_url = await handler.get_pdf_url(page)

        # Generate markdown preview
        print("\n📝 Generating Markdown...")
        markdown = handler.convert_to_markdown(metadata)

        # Print summary
        print("\n" + "="*80)
        print("EXTRACTION SUMMARY")
        print("="*80)
        print(f"\n✅ Title: {metadata.get('title', 'N/A')[:70]}")
        print(f"✅ Authors: {len(metadata.get('authors', []))} found")
        if metadata.get('authors'):
            print(f"   First author: {metadata['authors'][0][:60]}")
        print(f"✅ Journal: {metadata.get('journal', 'N/A')}")
        print(f"✅ DOI: {metadata.get('doi', 'N/A')}")
        print(f"✅ Year: {metadata.get('year', 'N/A')}")

        if metadata.get('abstract'):
            print(f"✅ Abstract: {metadata['abstract'][:80]}...")

        print(f"\n✅ Figures extracted: {len(figures)}")
        if figures:
            first_fig = list(figures.values())[0]
            print(f"   Caption: {first_fig['caption'][:60]}...")
            print(f"   URL: {first_fig['url'][:80]}...")

        print(f"\n✅ References extracted: {len(references)}")
        if references:
            print(f"   First ref: {references[0][:80]}...")

        print(f"\n{'✅' if pdf_url else '⚠️'} PDF URL: {pdf_url if pdf_url else 'Not found'}")

        print(f"\n✅ Markdown generated: {len(markdown)} characters")
        print(f"   First 200 chars:\n{markdown[:200]}")

        await browser.close()

        return {
            'paper': paper_name,
            'url': url,
            'metadata': metadata,
            'figures': len(figures),
            'references': len(references),
            'markdown_size': len(markdown),
            'pdf_found': bool(pdf_url)
        }


async def main():
    print("\n" + "🧪 NATURE HANDLER TEST SUITE 🧪".center(80))

    papers = [
        ("https://www.nature.com/articles/s41586-026-10400-2", "Nature (2026) - HHG Paper"),
        ("https://www.nature.com/articles/s41567-019-0584-7", "Nature Physics (2019) - ENZ Paper"),
    ]

    results = []
    for url, name in papers:
        try:
            result = await test_nature_handler(url, name)
            results.append(result)
        except Exception as e:
            print(f"\n❌ Error testing {name}: {e}")
            import traceback
            traceback.print_exc()

    # Summary
    print("\n\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)

    for result in results:
        print(f"\n{result['paper']}")
        print(f"  ✅ Metadata fields: {sum(1 for v in result['metadata'].values() if v)}")
        print(f"  ✅ Figures: {result['figures']}")
        print(f"  ✅ References: {result['references']}")
        print(f"  ✅ Markdown size: {result['markdown_size']} chars")
        print(f"  {'✅' if result['pdf_found'] else '⚠️'} PDF available: {result['pdf_found']}")

    print("\n✅ Test suite complete!")


if __name__ == "__main__":
    asyncio.run(main())

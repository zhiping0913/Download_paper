#!/usr/bin/env python3
"""
Nature Article ID (artid) Extraction
Trace redirects and extract artid like s41567-019-0584-7
Map artid to DOI and other metadata
"""

import asyncio
import re
from playwright.async_api import async_playwright


async def extract_nature_artid(doi_url: str):
    """
    Extract Nature article ID (artid) from redirect chain

    Example:
        DOI: 10.1038/s41567-019-0584-7
        URL: https://www.nature.com/articles/s41567-019-0584-7
        artid: s41567-019-0584-7
    """

    print("\n" + "="*80)
    print(f"EXTRACTING NATURE ARTICLE ID (artid)")
    print("="*80)
    print(f"📍 Starting URL: {doi_url}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # Track all navigations
        navigation_chain = []
        artid = None

        # Listen to navigation events
        async def handle_route(route):
            """Intercept all requests to track redirects"""
            request = route.request
            nav_info = {
                'url': request.url,
                'method': request.method,
                'type': request.resource_type
            }
            navigation_chain.append(nav_info)
            await route.continue_()

        await page.route("**/*", handle_route)

        # Also listen to response events
        responses_log = []

        async def handle_response(response):
            """Track responses including redirects"""
            resp_info = {
                'url': response.url,
                'status': response.status,
                'headers': dict(response.headers)
            }
            responses_log.append(resp_info)

            # Print redirect information
            if 300 <= response.status < 400:
                location = response.headers.get('location', 'N/A')
                print(f"  {response.status} {response.url[:60]}")
                print(f"      ↓ Location: {location[:60]}")

        page.on("response", handle_response)

        # Navigate to DOI
        try:
            response = await page.goto(doi_url, wait_until="load", timeout=20000)
        except:
            pass

        final_url = page.url

        print("\n" + "-"*80)
        print("📊 REDIRECT CHAIN ANALYSIS")
        print("-"*80)

        # Extract artid from final URL
        print(f"\n✅ Final URL: {final_url}")

        # Pattern 1: Extract from nature.com/articles/s41567-019-0584-7
        match = re.search(r'/articles/(s\d+-\d+-\d+)', final_url)
        if match:
            artid = match.group(1)
            print(f"✅ Article ID (artid): {artid}")

        # Pattern 2: Extract from DOI pattern
        doi_pattern_match = re.search(r'10\.1038/(s\d+-\d+-\d+)', final_url)
        if doi_pattern_match and not artid:
            artid = doi_pattern_match.group(1)
            print(f"✅ Article ID (from DOI): {artid}")

        # Reconstruct DOI from artid
        if artid:
            doi = f"10.1038/{artid}"
            print(f"✅ DOI from artid: {doi}")

            # Extract journal from artid (first digits after s)
            journal_match = re.search(r's(\d+)', artid)
            if journal_match:
                journal_code = journal_match.group(1)
                journal_map = {
                    '41586': 'Nature',
                    '41567': 'Nature Physics',
                    '41563': 'Nature Materials',
                    '41929': 'Nature Electronics',
                    '41557': 'Nature Chemistry',
                    '41578': 'Nature Reviews Physics',
                    '41570': 'Nature Reviews Chemistry',
                    '41579': 'Nature Reviews Materials',
                }
                journal = journal_map.get(journal_code, f'Nature (code {journal_code})')
                print(f"✅ Journal: {journal}")

        # Show redirect chain
        print("\n" + "-"*80)
        print("🔗 FULL REDIRECT CHAIN")
        print("-"*80)

        for i, resp in enumerate(responses_log, 1):
            status = resp['status']
            url = resp['url']

            if 300 <= status < 400:
                location = resp['headers'].get('location', 'N/A')
                print(f"{i}. [{status}] {url[:70]}")
                print(f"   → {location[:70]}")
            else:
                print(f"{i}. [{status}] {url[:70]}")

        # Extract metadata while we're here
        print("\n" + "-"*80)
        print("📋 ADDITIONAL METADATA")
        print("-"*80)

        metadata = await page.evaluate("""() => {
            const data = {
                title: null,
                doi: null,
                journal: null,
            };

            // Get meta tags
            const titleMeta = document.querySelector('meta[name="citation_title"]');
            if (titleMeta) data.title = titleMeta.getAttribute('content');

            const doiMeta = document.querySelector('meta[name="citation_doi"]');
            if (doiMeta) data.doi = doiMeta.getAttribute('content');

            const journalMeta = document.querySelector('meta[name="citation_journal_title"]');
            if (journalMeta) data.journal = journalMeta.getAttribute('content');

            return data;
        }""")

        if metadata['title']:
            print(f"✅ Title: {metadata['title'][:70]}...")
        if metadata['doi']:
            print(f"✅ DOI (from meta): {metadata['doi']}")
        if metadata['journal']:
            print(f"✅ Journal (from meta): {metadata['journal']}")

        # Summary
        print("\n" + "="*80)
        print("📌 SUMMARY")
        print("="*80)

        summary = {
            'artid': artid,
            'final_url': final_url,
            'title': metadata['title'],
            'doi': metadata['doi'] or (f"10.1038/{artid}" if artid else None),
            'journal': metadata['journal'],
            'redirect_steps': len([r for r in responses_log if 300 <= r['status'] < 400])
        }

        print(f"\nExtracted Information:")
        print(f"  Article ID (artid): {summary['artid']}")
        print(f"  DOI: {summary['doi']}")
        print(f"  Journal: {summary['journal']}")
        print(f"  Redirect steps: {summary['redirect_steps']}")
        print(f"  Final URL: {summary['final_url']}\n")

        await browser.close()

        return summary


async def batch_extract_nature_artids():
    """Extract artid from multiple Nature papers"""

    print("\n" + "="*80)
    print("BATCH EXTRACTING NATURE ARTICLE IDs")
    print("="*80)

    test_dois = [
        "10.1038/s41586-026-10400-2",  # Nature 2026
        "10.1038/s41567-019-0584-7",   # Nature Physics 2019
    ]

    results = []

    for doi in test_dois:
        url = f"https://doi.org/{doi}"
        result = await extract_nature_artid(url)
        results.append(result)

    # Print comparison table
    print("\n" + "="*80)
    print("EXTRACTION RESULTS TABLE")
    print("="*80)
    print(f"\n{'DOI':<25} | {'artid':<20} | {'Journal':<20}")
    print("-" * 70)

    for result in results:
        doi = result['doi'] or 'N/A'
        artid = result['artid'] or 'N/A'
        journal = result['journal'] or 'N/A'
        print(f"{doi:<25} | {artid:<20} | {journal:<20}")

    print("\n✅ Batch extraction complete!")

    return results


async def main():
    """Main entry point"""

    # Test with both papers
    results = await batch_extract_nature_artids()

    # Also test direct extraction with second paper as example
    print("\n\n" + "="*80)
    print("DETAILED ANALYSIS - NATURE PHYSICS PAPER")
    print("="*80)

    result = await extract_nature_artid("https://doi.org/10.1038/s41567-019-0584-7")


if __name__ == "__main__":
    asyncio.run(main())

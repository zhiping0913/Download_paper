#!/usr/bin/env python3
"""
Deep dive into Nature page structure
Extract and analyze meta tags, JSON-LD, and article content structure
"""

import asyncio
import json
from playwright.async_api import async_playwright
from pathlib import Path


async def analyze_nature_html_structure(doi: str = "10.1038/s41586-026-10400-2"):
    """Analyze HTML structure and metadata locations"""

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={'width': 1920, 'height': 1080})

        print(f"\n🔬 Deep HTML Analysis: {doi}")
        print("=" * 80)

        await page.goto(f"https://doi.org/{doi}", wait_until="load", timeout=20000)
        print(f"✅ Loaded: {page.url}")

        # Extract metadata
        metadata_analysis = await page.evaluate("""() => {
            const analysis = {
                meta_tags: {},
                json_ld: [],
                article_structure: {
                    title: null,
                    authors: [],
                    abstract: null,
                    figures: [],
                    references: [],
                    supplementary: [],
                    formulas: []
                }
            };

            // Extract meta tags
            document.querySelectorAll('meta').forEach(meta => {
                const name = meta.getAttribute('name') || meta.getAttribute('property') || '';
                const content = meta.getAttribute('content') || '';

                if (name && content) {
                    const key = name.toLowerCase();
                    if (key.includes('author') || key.includes('title') ||
                        key.includes('description') || key.includes('abstract') ||
                        key.includes('doi') || key.includes('date')) {
                        analysis.meta_tags[name] = content.substring(0, 200);
                    }
                }
            });

            // Extract JSON-LD
            document.querySelectorAll('script[type="application/ld+json"]').forEach(script => {
                try {
                    const data = JSON.parse(script.textContent);
                    analysis.json_ld.push({
                        type: data['@type'] || 'unknown',
                        keys: Object.keys(data).slice(0, 10)
                    });
                } catch (e) {}
            });

            // Analyze article structure
            const article = document.querySelector('article');
            if (article) {
                // Title
                const titleEl = article.querySelector('h1, [class*="title"]');
                if (titleEl) analysis.article_structure.title = titleEl.textContent.substring(0, 100);

                // Authors
                const authors = article.querySelectorAll('[class*="author"]');
                authors.forEach((el, i) => {
                    if (i < 5) analysis.article_structure.authors.push(el.textContent.substring(0, 50));
                });

                // Abstract
                const abstractEl = article.querySelector('[class*="abstract"]');
                if (abstractEl) analysis.article_structure.abstract = abstractEl.textContent.substring(0, 150);

                // Figures
                article.querySelectorAll('figure, [class*="figure"]').forEach((el, i) => {
                    if (i < 3) {
                        const caption = el.querySelector('figcaption, [class*="caption"]')?.textContent || '';
                        analysis.article_structure.figures.push({
                            idx: i + 1,
                            caption: caption.substring(0, 100),
                            tag: el.tagName
                        });
                    }
                });

                // References
                const refSection = article.querySelector('[class*="reference"]');
                if (refSection) {
                    const refItems = refSection.querySelectorAll('li, [class*="reference"]');
                    for (let i = 0; i < Math.min(3, refItems.length); i++) {
                        analysis.article_structure.references.push(
                            refItems[i].textContent.substring(0, 100)
                        );
                    }
                }
            }

            // Look for supplementary material
            const suppLinks = document.querySelectorAll('a[href*="supplement"], a[href*="supp"]');
            suppLinks.forEach(link => {
                analysis.article_structure.supplementary.push({
                    text: link.textContent,
                    href: link.href.substring(0, 150)
                });
            });

            // Look for formulas
            const mathElements = document.querySelectorAll('math, [class*="math"], .katex, svg[class*="formula"]');
            analysis.article_structure.formulas = {
                count: mathElements.length,
                types: Array.from(new Set(Array.from(mathElements).map(el => el.tagName))).slice(0, 5)
            };

            return analysis;
        }""")

        print("\n📋 META TAGS FOUND:")
        print("=" * 80)
        for name, content in metadata_analysis['meta_tags'].items():
            print(f"  {name}:")
            print(f"    {content}\n")

        print("\n📦 JSON-LD STRUCTURED DATA:")
        print("=" * 80)
        for ld in metadata_analysis['json_ld']:
            print(f"  Type: {ld['type']}")
            print(f"  Keys: {', '.join(ld['keys'][:5])}\n")

        print("\n📄 ARTICLE STRUCTURE:")
        print("=" * 80)
        print(f"  Title: {metadata_analysis['article_structure']['title']}")
        print(f"  Authors found: {len(metadata_analysis['article_structure']['authors'])}")
        print(f"  Figures found: {len(metadata_analysis['article_structure']['figures'])}")
        print(f"  References found: {len(metadata_analysis['article_structure']['references'])}")
        print(f"  Supplementary items: {len(metadata_analysis['article_structure']['supplementary'])}")
        print(f"  Formula elements: {metadata_analysis['article_structure']['formulas']['count']}")
        if metadata_analysis['article_structure']['formulas']['types']:
            print(f"  Formula types: {metadata_analysis['article_structure']['formulas']['types']}")

        # Extract full JSON-LD for inspection
        print("\n🔍 EXTRACTING FULL JSON-LD CONTENT:")
        print("=" * 80)

        json_ld_content = await page.evaluate("""() => {
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            const results = [];
            scripts.forEach((script, i) => {
                try {
                    results.push({
                        index: i,
                        content: JSON.parse(script.textContent)
                    });
                } catch (e) {
                    results.push({ index: i, error: e.message });
                }
            });
            return results;
        }""")

        for item in json_ld_content:
            if 'error' not in item:
                print(f"\nJSON-LD #{item['index']}:")
                print(json.dumps(item['content'], indent=2)[:1000] + "...")
            else:
                print(f"JSON-LD #{item['index']}: Error - {item['error']}")

        # Extract the contextual API response we saw earlier
        print("\n📡 CHECKING FOR API RESPONSES IN PAGE:")
        print("=" * 80)

        api_data = await page.evaluate("""() => {
            // Check if window has any API data
            const data = {};

            // Look for common API response storage
            if (window.__INITIAL_STATE__) {
                data['__INITIAL_STATE__'] = typeof window.__INITIAL_STATE__;
            }
            if (window.__data__) {
                data['__data__'] = typeof window.__data__;
            }
            if (window.contextual) {
                data['contextual'] = typeof window.contextual;
            }

            // Look for React/Vue app data
            const rootEl = document.querySelector('#root, #app, [data-react-root]');
            if (rootEl) {
                data['root_element_type'] = rootEl.tagName;
                data['root_element_attrs'] = Array.from(rootEl.attributes)
                    .map(a => `${a.name}=${a.value.substring(0, 50)}`)
                    .slice(0, 5);
            }

            return data;
        }""")

        print("Window objects found:")
        for key, val in api_data.items():
            print(f"  {key}: {val}")

        await browser.close()

        # Save detailed report
        report = {
            'doi': doi,
            'url': page.url,
            'meta_tags': metadata_analysis['meta_tags'],
            'json_ld_structure': metadata_analysis['json_ld'],
            'article_structure': metadata_analysis['article_structure'],
            'window_objects': api_data
        }

        output_file = "/home/zhiping/Projects/Download_paper/nature_html_analysis.json"
        with open(output_file, 'w') as f:
            json.dump(report, f, indent=2)

        print(f"\n✅ Detailed analysis saved to: {output_file}")


if __name__ == "__main__":
    asyncio.run(analyze_nature_html_structure())

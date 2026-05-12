#!/usr/bin/env python3
"""
Analyze Nature journal API structure and network requests
Monitor all XHR/fetch requests during page load to understand:
- Redirect chain from DOI URL
- API endpoints used
- Metadata locations (authors, abstract, content, figures, etc.)
"""

import asyncio
import json
import sys
from pathlib import Path
from playwright.async_api import async_playwright, Page
import re
from datetime import datetime


class NatureAPIAnalyzer:
    def __init__(self):
        self.requests_log = []
        self.responses_log = []
        self.redirect_chain = []
        self.metadata_locations = {
            'authors': [],
            'abstract': [],
            'main_content': [],
            'figures': [],
            'references': [],
            'supplementary': [],
            'formulas': []
        }

    async def capture_request(self, request):
        """Log all requests"""
        req_info = {
            'timestamp': datetime.now().isoformat(),
            'method': request.method,
            'url': request.url,
            'type': request.resource_type,
            'headers': dict(request.headers),
        }
        self.requests_log.append(req_info)
        print(f"📤 {request.method:6s} {request.resource_type:12s} {request.url}")

    async def capture_response(self, response):
        """Log all responses and capture JSON data"""
        try:
            resp_info = {
                'timestamp': datetime.now().isoformat(),
                'url': response.url,
                'status': response.status,
                'type': response.request.resource_type,
                'headers': dict(response.headers),
                'size': len(await response.body()) if response.ok else None,
            }
            self.responses_log.append(resp_info)

            # Track redirects
            if 300 <= response.status < 400:
                location = response.headers.get('location', '')
                if location:
                    self.redirect_chain.append({
                        'from': response.url,
                        'to': location,
                        'status': response.status
                    })

            # Try to parse JSON responses
            if 'application/json' in response.headers.get('content-type', ''):
                try:
                    data = await response.json()
                    resp_info['json_size'] = len(str(data))

                    # Analyze JSON structure for metadata
                    self._analyze_json_structure(data, response.url)
                except:
                    pass

            status_emoji = "✅" if 200 <= response.status < 300 else "⚠️"
            print(f"{status_emoji} {response.status:3d} {response.url[:100]}")

        except Exception as e:
            pass  # Silently ignore response capture errors

    def _analyze_json_structure(self, data, url):
        """Analyze JSON structure for metadata fields"""
        url_lower = url.lower()

        def search_fields(obj, path=""):
            """Recursively search for metadata fields"""
            if isinstance(obj, dict):
                for key, value in obj.items():
                    new_path = f"{path}.{key}" if path else key

                    # Check for metadata fields
                    if any(x in key.lower() for x in ['author', 'creator']):
                        self.metadata_locations['authors'].append({
                            'endpoint': url,
                            'path': new_path,
                            'type': type(value).__name__
                        })
                    elif any(x in key.lower() for x in ['abstract', 'summary']):
                        self.metadata_locations['abstract'].append({
                            'endpoint': url,
                            'path': new_path,
                            'type': type(value).__name__
                        })
                    elif any(x in key.lower() for x in ['figure', 'image', 'graphic']):
                        self.metadata_locations['figures'].append({
                            'endpoint': url,
                            'path': new_path,
                            'type': type(value).__name__
                        })
                    elif any(x in key.lower() for x in ['reference', 'citation', 'references']):
                        self.metadata_locations['references'].append({
                            'endpoint': url,
                            'path': new_path,
                            'type': type(value).__name__
                        })
                    elif any(x in key.lower() for x in ['supplement', 'supplementary', 'supp']):
                        self.metadata_locations['supplementary'].append({
                            'endpoint': url,
                            'path': new_path,
                            'type': type(value).__name__
                        })
                    elif any(x in key.lower() for x in ['body', 'content', 'article', 'text', 'html']):
                        self.metadata_locations['main_content'].append({
                            'endpoint': url,
                            'path': new_path,
                            'type': type(value).__name__
                        })
                    elif any(x in key.lower() for x in ['formula', 'math', 'equation', 'mathml', 'latex']):
                        self.metadata_locations['formulas'].append({
                            'endpoint': url,
                            'path': new_path,
                            'type': type(value).__name__
                        })

                    # Recurse
                    if isinstance(value, (dict, list)):
                        search_fields(value, new_path)
            elif isinstance(obj, list):
                for i, item in enumerate(obj[:5]):  # Limit recursion depth
                    search_fields(item, f"{path}[{i}]")

        search_fields(data)

    async def analyze_nature_doi(self, doi: str = "10.1038/s41586-026-10400-2"):
        """Main analysis function"""
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={'width': 1920, 'height': 1080}
            )
            page = await context.new_page()

            # Set up listeners before navigation
            page.on("request", self.capture_request)
            page.on("response", self.capture_response)

            print(f"\n🌐 Analyzing Nature paper: {doi}")
            print(f"📍 Starting URL: https://doi.org/{doi}")
            print("=" * 80)

            # Navigate to DOI with shorter timeout and load event
            try:
                response = await page.goto(
                    f"https://doi.org/{doi}",
                    wait_until="load",
                    timeout=20000
                )

                # Capture redirect chain from page history
                print(f"\n🔗 Redirect Chain:")
                print(f"  Final URL: {page.url}")
                self.redirect_chain.append({
                    'from': f"https://doi.org/{doi}",
                    'to': page.url,
                    'final': True
                })

            except Exception as e:
                print(f"⚠️ Navigation error: {e}")
                print(f"   Final URL reached: {page.url}")
                self.redirect_chain.append({
                    'from': f"https://doi.org/{doi}",
                    'to': page.url,
                    'final': True
                })

            # Wait a bit more to catch any lazy-loaded requests
            print("\n⏳ Waiting for additional requests...")
            await page.wait_for_timeout(2000)

            # Get current page title and metadata
            print(f"\n📄 Page Title: {await page.title()}")

            # Try to extract visible metadata from HTML
            print("\n🔍 Analyzing page HTML for metadata...")
            await self._analyze_page_html(page)

            await browser.close()

    async def _analyze_page_html(self, page: Page):
        """Analyze HTML content for metadata"""
        # Check for meta tags
        meta_tags = await page.query_selector_all('meta')
        print(f"   Found {len(meta_tags)} meta tags")

        # Look for common metadata patterns
        metadata_found = await page.evaluate("""() => {
            const data = {};

            // Get all meta tags
            document.querySelectorAll('meta').forEach(meta => {
                const name = meta.getAttribute('name') || meta.getAttribute('property') || '';
                const content = meta.getAttribute('content') || '';
                if (content && (name.toLowerCase().includes('author') ||
                               name.toLowerCase().includes('description') ||
                               name.toLowerCase().includes('abstract'))) {
                    data[name] = content.substring(0, 100);
                }
            });

            // Look for JSON-LD structured data
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            data['json_ld_count'] = scripts.length;

            // Look for common article content wrappers
            data['article_elements'] = {
                'article': document.querySelectorAll('article').length,
                'main': document.querySelectorAll('main').length,
                '[role="main"]': document.querySelectorAll('[role="main"]').length,
                '.article': document.querySelectorAll('.article').length,
            };

            return data;
        }""")

        print(f"   Meta tags found: {len([k for k in metadata_found.keys() if k != 'article_elements' and k != 'json_ld_count'])}")
        print(f"   JSON-LD scripts: {metadata_found.get('json_ld_count', 0)}")
        print(f"   Article elements: {metadata_found.get('article_elements', {})}")

    def generate_report(self, output_file: str = None):
        """Generate analysis report"""
        if output_file is None:
            output_file = "/home/zhiping/Projects/Download_paper/nature_api_analysis.md"

        report = []
        report.append("# Nature Journal API Structure Analysis\n")
        report.append(f"**Date**: {datetime.now().isoformat()}\n")
        report.append(f"**Paper**: 10.1038/s41586-026-10400-2\n\n")

        # Redirect Chain
        report.append("## 1. Redirect Chain\n\n")
        if self.redirect_chain:
            for i, redirect in enumerate(self.redirect_chain, 1):
                status = redirect.get('status', '(automatic)')
                report.append(f"Step {i}: `{redirect['from']}` → `{redirect['to']}` ({status})\n")
            report.append(f"\n**Final URL**: {self.redirect_chain[-1]['to']}\n\n")
        else:
            report.append("No explicit redirects captured (may be handled by Playwright)\n\n")

        # API Endpoints Used
        report.append("## 2. Network Requests Summary\n\n")
        report.append(f"**Total Requests**: {len(self.requests_log)}\n")
        report.append(f"**Total Responses**: {len(self.responses_log)}\n\n")

        # Group by type
        request_types = {}
        for req in self.requests_log:
            rt = req['type']
            request_types[rt] = request_types.get(rt, 0) + 1

        report.append("### Request Types\n\n")
        for rt, count in sorted(request_types.items(), key=lambda x: -x[1]):
            report.append(f"- **{rt}**: {count} requests\n")
        report.append("\n")

        # API Endpoints (XHR/Fetch)
        api_endpoints = [r for r in self.requests_log if r['type'] in ['xhr', 'fetch']]
        report.append(f"### API Endpoints ({len(api_endpoints)} requests)\n\n")
        for req in api_endpoints:
            report.append(f"- `{req['method']}` {req['url']}\n")
        report.append("\n")

        # Metadata Locations
        report.append("## 3. Metadata Locations\n\n")

        for metadata_type, locations in self.metadata_locations.items():
            if locations:
                report.append(f"### {metadata_type.upper()}\n\n")
                for loc in locations:
                    report.append(f"- **Endpoint**: `{loc['endpoint']}`\n")
                    report.append(f"  **Path**: `{loc['path']}`\n")
                    report.append(f"  **Type**: {loc['type']}\n\n")

        # Response Status Summary
        report.append("## 4. Response Status Summary\n\n")
        status_codes = {}
        for resp in self.responses_log:
            status = resp['status']
            status_codes[status] = status_codes.get(status, 0) + 1

        for status in sorted(status_codes.keys()):
            report.append(f"- **{status}**: {status_codes[status]} responses\n")
        report.append("\n")

        # Detailed Requests Log
        report.append("## 5. Detailed Requests Log\n\n")
        report.append("```\n")
        for req in self.requests_log:
            report.append(f"{req['method']:6s} {req['type']:12s} {req['url']}\n")
        report.append("```\n\n")

        # Detailed Responses Log
        report.append("## 6. Detailed Responses Log\n\n")
        for resp in self.responses_log:
            if resp['type'] in ['xhr', 'fetch']:
                report.append(f"**{resp['status']}** {resp['url']}\n")
                if 'json_size' in resp:
                    report.append(f"- JSON size: {resp['json_size']} bytes\n")
                report.append("\n")

        report_text = "".join(report)

        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(report_text)

        print(f"\n✅ Report saved to: {output_file}")
        return report_text


async def main():
    analyzer = NatureAPIAnalyzer()

    try:
        print("\n" + "=" * 80)
        print("NATURE JOURNAL API STRUCTURE ANALYSIS")
        print("=" * 80)

        await analyzer.analyze_nature_doi()

        # Generate report
        report = analyzer.generate_report()

        print("\n" + "=" * 80)
        print("ANALYSIS COMPLETE")
        print("=" * 80)
        print(f"\n📊 Summary:")
        print(f"  - Total API requests: {len(analyzer.requests_log)}")
        print(f"  - Total responses: {len(analyzer.responses_log)}")
        print(f"  - Metadata locations found: {sum(len(v) for v in analyzer.metadata_locations.values())}")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())

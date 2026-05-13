#!/usr/bin/env python3
"""
增强的网络监听脚本 - 捕获论文数据 + 提取元数据
"""

import json
import asyncio
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright

OUTPUT_DIR = "captured_data"


async def extract_metadata_from_page(page) -> dict:
    """从页面的meta标签中提取所有元数据"""

    metadata = {
        'title': None,
        'authors': [],
        'author_institutions': [],
        'abstract': None,
        'journal': None,
        'publication_date': None,
        'doi': None,
        'volume': None,
        'issue': None,
        'pages': None,
        'publisher': None,
        'pdf_url': None,
    }

    try:
        # 使用JavaScript提取所有meta标签
        metas = await page.evaluate("""() => {
            const result = {};
            document.querySelectorAll('meta').forEach(meta => {
                const name = meta.getAttribute('name') || meta.getAttribute('property');
                const content = meta.getAttribute('content');
                if (name && content) {
                    if (!result[name]) result[name] = [];
                    result[name].push(content);
                }
            });
            return result;
        }""")

        # 提取各个字段
        if 'citation_title' in metas:
            metadata['title'] = metas['citation_title'][0]

        if 'citation_author' in metas:
            metadata['authors'] = metas['citation_author']

        if 'citation_author_institution' in metas:
            metadata['author_institutions'] = metas['citation_author_institution']

        if 'citation_abstract' in metas or 'description' in metas:
            abstract = metas.get('citation_abstract') or metas.get('description')
            if abstract:
                metadata['abstract'] = abstract[0]

        if 'citation_journal_title' in metas:
            metadata['journal'] = metas['citation_journal_title'][0]

        if 'citation_publication_date' in metas:
            metadata['publication_date'] = metas['citation_publication_date'][0]

        if 'citation_doi' in metas:
            metadata['doi'] = metas['citation_doi'][0]

        if 'citation_volume' in metas:
            metadata['volume'] = metas['citation_volume'][0]

        if 'citation_issue' in metas:
            metadata['issue'] = metas['citation_issue'][0]

        if 'citation_firstpage' in metas:
            metadata['pages'] = metas['citation_firstpage'][0]
            if 'citation_lastpage' in metas:
                metadata['pages'] += f"-{metas['citation_lastpage'][0]}"

        if 'citation_publisher' in metas:
            metadata['publisher'] = metas['citation_publisher'][0]

        if 'citation_pdf_url' in metas:
            metadata['pdf_url'] = metas['citation_pdf_url'][0]

    except Exception as e:
        print(f"⚠️  提取meta标签时出错: {e}")

    return metadata


async def monitor_network(url: str):
    """监听网络并捕获论文数据"""

    Path(OUTPUT_DIR).mkdir(exist_ok=True)

    captured = {
        'metadata': {},
        'document': None,
        'json_responses': [],
        'all_requests': [],
        'timeline': []
    }

    async with async_playwright() as p:
        print("🌐 启动Chrome...")

        try:
            # 连接到已运行的Chrome
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
            print("✓ 已连接到Chrome\n")
        except Exception as e:
            print(f"❌ 无法连接到Chrome port 9222: {e}")
            print("   请运行: /opt/google/chrome/chrome --remote-debugging-port=9222 &\n")
            return None

        contexts = browser.contexts
        if contexts:
            context = contexts[0]
            print("✓ 使用现有context")
        else:
            context = await browser.new_context()
            print("✓ 创建新context")

        page = await context.new_page()
        print(f"✓ 在已登录Chrome中打开新标签页\n")

        # 响应处理函数
        async def handle_response(response):
            rtype = response.request.resource_type
            status = response.status
            url_str = response.url
            ts = datetime.now().isoformat()

            captured['timeline'].append({
                'timestamp': ts,
                'type': rtype,
                'status': status,
                'url': url_str,
                'method': response.request.method
            })
            captured['all_requests'].append(url_str)

            if status == 200:
                print(f"[{status}] {rtype:10s} {url_str[:70]}")

            # 捕获HTML文档
            if rtype == 'document' and status == 200:
                try:
                    html = await response.text()
                    captured['document'] = {
                        'url': url_str,
                        'timestamp': ts,
                        'size': len(html),
                    }
                    print(f"  ✓ 原始HTML保存: {len(html)} 字节")
                except:
                    pass

            # 捕获JSON/API响应
            elif rtype in ('xhr', 'fetch') and status == 200:
                try:
                    ctype = response.headers.get('content-type', '')
                    if 'json' in ctype.lower():
                        jdata = await response.json()
                        jstr = json.dumps(jdata)

                        kws = ['abstract', 'article', 'fulltext', 'front', 'back']
                        has_paper = any(kw in jstr.lower() for kw in kws)

                        if has_paper or len(jstr) > 2000:
                            print(f"  ✓✓ API数据: {len(jstr)} 字节")

                            jpath = Path(OUTPUT_DIR) / f"api_response_{len(captured['json_responses']):03d}.json"
                            with open(jpath, 'w', encoding='utf-8') as f:
                                json.dump(jdata, f, indent=2, ensure_ascii=False)

                            captured['json_responses'].append({
                                'url': url_str,
                                'timestamp': ts,
                                'size': len(jstr),
                                'file': str(jpath),
                            })
                except:
                    pass

        page.on("response", handle_response)

        # 导航到URL
        print(f"📄 访问: {url}")
        print("=" * 80)

        try:
            await page.goto(url, wait_until='networkidle', timeout=60000)
            print("✓ 页面加载完成")
        except Exception as e:
            print(f"⚠️  {type(e).__name__}: {str(e)[:100]}")

        # 等待额外请求
        await asyncio.sleep(3)

        # 提取元数据
        print("\n📋 提取页面元数据...")
        metadata = await extract_metadata_from_page(page)
        captured['metadata'] = metadata

        # 显示提取的元数据
        if metadata['title']:
            print(f"  ✓ 标题: {metadata['title']}")
        if metadata['authors']:
            print(f"  ✓ 作者: {len(metadata['authors'])} 位")
            for author in metadata['authors'][:3]:
                print(f"     - {author}")
        if metadata['journal']:
            print(f"  ✓ 期刊: {metadata['journal']}")
        if metadata['doi']:
            print(f"  ✓ DOI: {metadata['doi']}")

        # 保存最终HTML
        try:
            final_html = await page.content()
            rpath = Path(OUTPUT_DIR) / f"rendered_page_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
            with open(rpath, 'w', encoding='utf-8') as f:
                f.write(final_html)
            print(f"\n✓ 渲染后HTML保存: {len(final_html)} 字节")
        except:
            pass

        # 统计
        print("\n" + "=" * 80)
        print("📊 网络监听统计")
        print(f"  总请求数: {len(captured['all_requests'])}")
        print(f"  JSON API响应: {len(captured['json_responses'])}")

        # 保存报告
        rpt_path = Path(OUTPUT_DIR) / f"capture_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(rpt_path, 'w', encoding='utf-8') as f:
            json.dump(captured, f, indent=2, ensure_ascii=False)
        print(f"\n📋 完整报告: {rpt_path}")

        await browser.close()
        return captured


async def main():
    print("=" * 80)
    print("🔍 论文网络监听 + 元数据提取")
    print("=" * 80 + "\n")

    url = "https://doi.org/10.1103/PhysRevLett.109.245005"

    result = await monitor_network(url)

    if result:
        print("\n✅ 任务完成!")
        print("\n🎯 提取的元数据:")
        print(json.dumps(result['metadata'], indent=2, ensure_ascii=False)[:500] + "...")


if __name__ == "__main__":
    asyncio.run(main())

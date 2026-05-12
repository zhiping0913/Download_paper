#!/usr/bin/env python3
"""
Step 1: 使用有登录态的有头浏览器访问Nature页面并保存HTML
模仿 complete_paper_extraction.py 的流程
"""

import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

async def capture_nature_html(doi: str, output_dir: str = None):
    """
    使用 complete_paper_extraction.py 的有头浏览器访问 Nature 页面

    Args:
        doi: DOI (e.g., "10.1038/s41586-026-10400-2")
        output_dir: 输出目录
    """

    if output_dir is None:
        output_dir = "/home/zhiping/Projects/Download_paper/nature_capture"

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    print("="*80)
    print("Step 1️⃣  使用有登录态的浏览器访问 Nature 页面")
    print("="*80)

    try:
        # 连接到现有的 Chrome 实例（complete_paper_extraction.py 已启动）
        async with async_playwright() as p:
            print("\n🔗 连接到 Chrome (CDP port 9222)...")
            print("   ⚠️  确保已运行: python complete_paper_extraction.py <DOI>")

            try:
                browser = await p.chromium.connect_over_cdp("http://localhost:9222")
                print("✓ 已连接到 complete_paper_extraction.py 的 Chrome 实例")
            except Exception as e:
                print(f"\n❌ 无法连接到 Chrome: {e}")
                print("\n必须先启动 complete_paper_extraction.py 来启动有登录态的 Chrome:")
                print("  python complete_paper_extraction.py 10.1038/s41586-026-10400-2")
                print("\n然后再运行这个脚本")
                return None

            # 获取或创建页面
            pages = browser.contexts[0].pages if browser.contexts else []
            if pages:
                page = pages[0]
                print("✓ 使用现有页面")
            else:
                page = await browser.new_page()
                print("✓ 创建新页面")

            # 访问 Nature 论文
            doi_url = f"https://doi.org/{doi}"
            print(f"\n📄 访问: {doi_url}")
            print("   (会被重定向到 nature.com)")

            await page.goto(doi_url, wait_until="networkidle")

            # 获取最终 URL
            final_url = page.url
            print(f"\n✓ 最终 URL: {final_url}")

            # 等待主要内容加载
            print("\n⏳ 等待页面完全加载...")
            await page.wait_for_load_state("networkidle")

            # 捕获 HTML
            print("📸 捕获 HTML 内容...")
            html_content = await page.content()

            # 获取元数据
            print("📋 提取基本元数据...")
            metadata = await page.evaluate("""() => {
                const meta = {};
                document.querySelectorAll('meta').forEach(m => {
                    const name = m.getAttribute('name') || m.getAttribute('property') || '';
                    if (name.includes('title') || name.includes('author') || name.includes('citation')) {
                        meta[name] = m.getAttribute('content');
                    }
                });

                // 提取标题
                const titleEl = document.querySelector('h1, .title, [class*="title"]');
                const title = titleEl ? titleEl.textContent.trim().substring(0, 100) : '';

                return {
                    title: title,
                    url: window.location.href,
                    meta_count: Object.keys(meta).length
                };
            }""")

            print(f"  ✓ 标题: {metadata['title'][:80]}...")
            print(f"  ✓ Meta 标签数: {metadata['meta_count']}")

            # 保存 HTML
            html_filename = f"{doi.replace('/', '_')}_page.html"
            html_path = output_path / html_filename

            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(html_content)

            print(f"\n✅ HTML 已保存:")
            print(f"   文件: {html_filename}")
            print(f"   大小: {len(html_content):,} 字节")
            print(f"   路径: {html_path}")

            # 保存元数据 JSON
            metadata_filename = f"{doi.replace('/', '_')}_metadata.json"
            metadata_path = output_path / metadata_filename

            metadata['html_size'] = len(html_content)
            metadata['html_file'] = str(html_path)
            metadata['doi'] = doi

            with open(metadata_path, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)

            print(f"✅ 元数据已保存: {metadata_filename}")

            # 分析 HTML 结构
            print("\n📊 HTML 结构分析:")
            structure_info = await page.evaluate("""() => {
                return {
                    main_content_div: !!document.querySelector('[class*="main-content"]'),
                    article_tags: document.querySelectorAll('article').length,
                    section_tags: document.querySelectorAll('section').length,
                    div_with_content: Array.from(document.querySelectorAll('div')).filter(d =>
                        d.className.includes('content') || d.className.includes('main')
                    ).length,
                    p_tags: document.querySelectorAll('p').length,
                    heading_tags: document.querySelectorAll('h1, h2, h3, h4, h5, h6').length,
                    figure_tags: document.querySelectorAll('figure').length,
                    img_tags: document.querySelectorAll('img').length
                };
            }""")

            print(f"  - main-content div: {'✓ 找到' if structure_info['main_content_div'] else '✗ 未找到'}")
            print(f"  - <article> 标签: {structure_info['article_tags']}")
            print(f"  - <section> 标签: {structure_info['section_tags']}")
            print(f"  - Content div: {structure_info['div_with_content']}")
            print(f"  - <p> 段落: {structure_info['p_tags']}")
            print(f"  - 标题: {structure_info['heading_tags']}")
            print(f"  - <figure> 标签: {structure_info['figure_tags']}")
            print(f"  - <img> 图片: {structure_info['img_tags']}")

            print("\n" + "="*80)
            print("✅ Step 1 完成！已保存 Nature 页面 HTML")
            print("="*80)

            return {
                'html_path': str(html_path),
                'metadata_path': str(metadata_path),
                'html_size': len(html_content),
                'doi': doi,
                'url': final_url,
                'structure': structure_info
            }

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        return None


async def main():
    # Nature 论文 DOI
    doi = "10.1038/s41586-026-10400-2"

    print(f"开始 Step 1: 捕获 Nature 页面 HTML")
    print(f"DOI: {doi}\n")

    result = await capture_nature_html(doi)

    if result:
        print(f"\n📁 输出文件:")
        print(f"   HTML: {result['html_path']}")
        print(f"   Metadata: {result['metadata_path']}")
        print(f"\n📊 文件信息:")
        print(f"   HTML 大小: {result['html_size']:,} 字节")
        print(f"   最终 URL: {result['url']}")


if __name__ == "__main__":
    asyncio.run(main())

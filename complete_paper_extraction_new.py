#!/usr/bin/env python3
"""
论文完整提取系统 - 统一外部接口

功能：
1. 通用浏览器管理和网络监听
2. 自动检测出版商
3. 路由到相应的出版商处理器
4. 管理文件下载和输出

架构：
- 本文件：通用流程 + 出版商路由
- publisher/aps.py：APS 特定逻辑
- publisher/nature.py：Nature 特定逻辑
- publisher/orchestrator.py：出版商检测和工厂
"""

import json
import asyncio
import re
import sys
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright

# 导入出版商相关模块
from publisher.orchestrator import (
    detect_publisher_from_url,
    get_publisher_handler,
    extract_metadata_multi_publisher
)

# 导入核心工具
from core import (
    fetch_semanticscholar,
    organize_paper_output,
    save_metadata_json,
    add_equation_numbers,
    mathml_to_latex_pandoc,
    extract_text_without_math
)

OUTPUT_DIR = "captured_data"


# ============================================================================
# 通用网络监听和捕获（从 complete_paper_extraction.py 提取）
# ============================================================================

async def capture_network_data(page, url: str) -> dict:
    """
    通用网络监听函数 - 捕获页面加载过程中的所有数据

    返回数据结构：
    {
        'document': {...},           # HTML 文档
        'json_responses': [...],     # API JSON 响应列表
        'fulltext_data': {...},      # 正文 JSON 数据
        'supplemental_data': {...},  # 补充数据
        'abstract_html': str,        # 摘要页面 HTML
        'journal_prefix': str,       # 期刊前缀（如 prl, pre）
    }
    """

    captured = {
        'document': None,
        'json_responses': [],
        'fulltext_data': None,
        'supplemental_data': None,
        'abstract_html': None,
        'journal_prefix': None,
        'url': url,
        'timestamp': datetime.now().isoformat()
    }

    def handle_response(response):
        """处理每个网络响应"""
        try:
            url_str = response.url
            status = response.status
            rtype = response.request.resource_type
            ts = datetime.now().isoformat()

            # 捕获 HTML 文档
            if rtype == 'document' and status == 200:
                try:
                    html = None

                    async def get_html():
                        try:
                            return await response.text()
                        except:
                            return None

                    # 这里需要同步处理异步代码
                    # 由于这是 callback，我们不能直接等待
                    # 所以我们在页面加载后单独获取 HTML

                except:
                    pass

            # 捕获 JSON/API 响应
            elif rtype in ('xhr', 'fetch') and status == 200:
                try:
                    ctype = response.headers.get('content-type', '')
                    if 'json' in ctype.lower():
                        # 异步获取 JSON - 需要在事件循环中处理
                        # 这里我们先记录 URL，稍后处理

                        # 特别标记 fulltext 和 supplemental
                        if '/fulltext/' in url_str:
                            captured['fulltext_url'] = url_str
                        elif '/supplemental/' in url_str:
                            captured['supplemental_url'] = url_str

                        captured['json_responses'].append({
                            'url': url_str,
                            'timestamp': ts,
                            'resource_type': rtype
                        })
                except:
                    pass

        except:
            pass

    page.on("response", handle_response)

    # 导航到 URL
    print(f"📄 访问: {url}")
    print("=" * 80)

    try:
        await page.goto(url, wait_until='networkidle', timeout=60000)
        print("✓ 页面加载完成")
    except Exception as e:
        print(f"⚠️  {type(e).__name__}: {str(e)[:100]}")

    # 等待额外请求
    await asyncio.sleep(3)

    # 获取最终 HTML
    try:
        html = await page.content()
        captured['document'] = {
            'url': page.url,
            'title': await page.title(),
            'size': len(html)
        }
        captured['html'] = html
    except Exception as e:
        print(f"⚠️  无法获取 HTML: {e}")

    return captured


async def complete_extraction_workflow(doi: str):
    """
    完整论文提取工作流

    1. 启动浏览器
    2. 导航到 DOI URL
    3. 检测出版商
    4. 调用相应的处理器
    5. 返回结果
    """

    print("=" * 80)
    print("🔍 论文完整提取工作流")
    print("=" * 80)
    print(f"📌 DOI: {doi}\n")

    # 启动浏览器
    async with async_playwright() as p:
        # 尝试连接到现有的 Chrome（通过 CDP）
        browser = None

        # 首先尝试连接现有的 Chrome (port 9222)
        try:
            print("🔗 尝试连接到现有的 Chrome 实例 (CDP port 9222)...")
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
            print("✓ 已连接到现有的 Chrome")
        except Exception as e:
            print(f"⚠️  无法连接到现有 Chrome: {str(e)[:50]}")
            print("正在启动新的 Chrome 实例...")

            # 启动新的 Chrome
            browser = await p.chromium.launch(
                headless=False,
                args=[
                    "--remote-debugging-port=9222",
                    "--disable-blink-features=AutomationControlled"
                ]
            )
            print(f"✓ Chrome 已启动")

        # 获取或创建页面
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        pages = context.pages if context.pages else []

        if pages:
            page = pages[0]
            print("✓ 使用现有页面")
        else:
            page = await context.new_page()
            print("✓ 创建新页面")

        # 将 DOI 转换为 URL
        doi_url = f"https://doi.org/{doi}"

        # Step 1: 提取元数据
        print("\nStep 1️⃣  提取页面元数据...")
        print("=" * 80)

        try:
            # 首先导航并等待重定向
            await page.goto(doi_url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_load_state('networkidle')

            # 获取最终 URL（出版商网站）
            final_url = page.url
            print(f"✓ 最终 URL: {final_url}")

            # 检测出版商
            publisher = detect_publisher_from_url(final_url)
            print(f"✓ 检测出版商: {publisher}")

            # 获取处理器
            handler = get_publisher_handler(publisher)

            # 提取元数据
            metadata = await handler.extract_metadata(page)
            print(f"  ✓ 作者: {len(metadata.get('authors', []))} 位")
            print(f"  ✓ 标题: {metadata.get('title', '')[:60]}...")
            print(f"  ✓ 期刊: {metadata.get('journal', '')}")
            print(f"  ✓ 摘要: {len(metadata.get('abstract', ''))} 字符")

        except Exception as e:
            print(f"❌ 元数据提取失败: {e}")
            return None

        # Step 1.5: 获取 Semantic Scholar 数据
        print("\nStep 1.5️⃣  获取Semantic Scholar元数据...")
        print("=" * 80)

        try:
            if metadata.get('doi'):
                ss_data = fetch_semanticscholar(metadata['doi'])
                if ss_data:
                    metadata.update(ss_data)
                    print(f"  ✓ Semantic Scholar: {ss_data.get('title', '')[:60]}... ({ss_data.get('year', 'N/A')})")
        except Exception as e:
            print(f"⚠️  Semantic Scholar 查询失败: {e}")

        # Step 2: 网络数据捕获（通用）
        print("\nStep 2️⃣  监听网络请求并捕获数据...")
        print("=" * 80)

        try:
            captured = await capture_network_data(page, final_url)
            print(f"  ✓ 捕获 {len(captured.get('json_responses', []))} 个JSON响应")
        except Exception as e:
            print(f"⚠️  网络捕获失败: {e}")
            captured = {}

        # Step 3: 调用处理器进行内容转换
        print("\nStep 3️⃣  转换为Markdown并下载图片...")
        print("=" * 80)

        try:
            # 获取 HTML 内容
            article_html = captured.get('html', '')

            # 调用处理器的转换方法
            markdown = handler.convert_to_markdown(
                metadata=metadata,
                article_html=article_html,
                add_figure_refs=True
            )

            print(f"  ✓ Markdown已生成")
        except Exception as e:
            print(f"⚠️  Markdown生成失败: {e}")
            markdown = ""

        # Step 4: 下载 PDF（处理器负责）
        print("\nStep 4️⃣  下载论文PDF...")
        print("=" * 80)

        try:
            # 调用处理器的 PDF 下载方法
            pdf_url = await handler.get_pdf_url(page)
            if pdf_url:
                print(f"  📥 下载 PDF...")
                print(f"     链接: {pdf_url}")
            else:
                print(f"  ⚠️  未找到 PDF 链接")
        except Exception as e:
            print(f"⚠️  PDF 下载失败: {e}")

        # 步骤 5: 下载补充材料
        print("\nStep 5️⃣  下载补充材料...")
        print("=" * 80)

        try:
            supp_url = await handler.get_supplemental_url(page)
            if supp_url:
                print(f"  ✓ 找到补充材料: {supp_url[:80]}...")
            else:
                print(f"  ⚠️  未找到补充材料")
        except Exception as e:
            print(f"⚠️  补充材料查询失败: {e}")

        # 保存结果
        print("\n" + "=" * 80)
        print("📊 完成统计")
        print("=" * 80)

        output_info = {
            'doi': doi,
            'publisher': publisher,
            'title': metadata.get('title'),
            'authors': len(metadata.get('authors', [])),
            'journal': metadata.get('journal'),
            'year': metadata.get('year'),
            'markdown_lines': len(markdown.splitlines()) if markdown else 0,
            'timestamp': datetime.now().isoformat()
        }

        print(f"  📄 Markdown 行数: {output_info['markdown_lines']}")
        print(f"  📊 作者: {output_info['authors']}")
        print(f"  💾 期刊: {output_info['journal']}")

        return {
            'metadata': metadata,
            'markdown': markdown,
            'handler': handler,
            'publisher': publisher,
            'info': output_info
        }


async def main():
    """主入口"""
    if len(sys.argv) < 2:
        print("使用方法: python paper_extraction.py <DOI>")
        print("示例: python paper_extraction.py 10.1038/s41586-026-10400-2")
        sys.exit(1)

    doi = sys.argv[1]
    result = await complete_extraction_workflow(doi)

    if result:
        print("\n✅ 提取成功！")
    else:
        print("\n❌ 提取失败！")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

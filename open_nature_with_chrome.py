#!/usr/bin/env python3
"""
从complete_paper_extraction.py中打开Chrome访问Nature文章
"""

import asyncio
import sys
import json
from pathlib import Path
import subprocess
import time

sys.path.insert(0, '/home/zhiping/Projects/Download_paper')

from playwright.async_api import async_playwright


async def check_chrome_ready():
    """检查Chrome是否就绪"""
    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex(('127.0.0.1', 9222))
        sock.close()
        return result == 0
    except:
        return False


async def open_nature_page(url: str):
    """打开Nature页面并获取内容"""

    print("="*80)
    print("🌐 OPENING NATURE PAGE WITH CHROME")
    print("="*80)
    print(f"📍 URL: {url}\n")

    # 检查Chrome是否就绪
    if not await check_chrome_ready():
        print("⚠️  Chrome 未运行，正在启动...")
        chrome_launcher = Path(__file__).parent / "chrome_launcher.py"
        if chrome_launcher.exists():
            subprocess.Popen([sys.executable, str(chrome_launcher)])
            # 等待Chrome启动
            for i in range(30):
                await asyncio.sleep(1)
                if await check_chrome_ready():
                    print("✓ Chrome 已就绪\n")
                    break
        else:
            print("⚠️  chrome_launcher.py 未找到\n")

    async with async_playwright() as p:
        try:
            # 连接到已启动的Chrome
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
            print("✓ 已连接到Chrome\n")

        except Exception as e:
            print(f"❌ 无法连接到Chrome port 9222: {e}")
            print("   请运行: python chrome_launcher.py\n")
            return False

        try:
            # 获取或创建context
            contexts = browser.contexts
            if contexts:
                context = contexts[0]
                print("✓ 使用现有context\n")
            else:
                context = await browser.new_context(
                    accept_downloads=True,
                    viewport={'width': 1920, 'height': 1080}
                )
                print("✓ 创建新context\n")

            # 创建新页面（新标签页）
            page = await context.new_page()

            print("📄 打开页面...")
            await page.goto(url, wait_until='load', timeout=60000)
            print("✓ 页面已加载\n")

            # 获取页面信息
            print("="*80)
            print("📋 PAGE INFORMATION")
            print("="*80)

            # 页面标题
            title = await page.title()
            print(f"\n📌 页面标题: {title}")

            # 当前URL
            current_url = page.url
            print(f"📍 当前URL: {current_url}")

            # 页面内容大小
            html = await page.content()
            print(f"📊 HTML大小: {len(html)} 字节")

            # 获取纯文本内容
            body_text = await page.evaluate("() => document.body.innerText")
            print(f"📝 纯文本大小: {len(body_text)} 字符")

            # 提取主要元素
            print("\n" + "-"*80)
            print("🔍 页面元素分析:")
            print("-"*80)

            # 文章标题
            h1 = await page.query_selector('h1')
            if h1:
                h1_text = await h1.text_content()
                print(f"\n✓ 文章标题:")
                print(f"  {h1_text.strip()[:100]}")

            # 摘要
            abstract = await page.query_selector('[class*="abstract"]')
            if abstract:
                abstract_text = await abstract.text_content()
                print(f"\n✓ 摘要 ({len(abstract_text)} 字符):")
                print(f"  {abstract_text.strip()[:150]}...")

            # 作者
            authors_elems = await page.query_selector_all('a[data-test="author-link"]')
            print(f"\n✓ 作者元素: {len(authors_elems)} 个")

            # 图表
            figures = await page.query_selector_all('figure')
            print(f"✓ 图表: {len(figures)} 个")

            # Meta标签
            print("\n" + "-"*80)
            print("🏷️  Meta标签信息:")
            print("-"*80)

            meta_info = await page.evaluate("""() => {
                const metas = document.querySelectorAll('meta');
                const info = {};

                metas.forEach(meta => {
                    const name = meta.getAttribute('name') || meta.getAttribute('property');
                    const content = meta.getAttribute('content');
                    if (name && content) {
                        info[name] = content;
                    }
                });

                return info;
            }""")

            # 显示重要的meta信息
            important_keys = ['citation_title', 'citation_doi', 'citation_author',
                            'og:title', 'og:description', 'article-access']

            for key in important_keys:
                if key in meta_info:
                    print(f"✓ {key}:")
                    print(f"  {meta_info[key][:100]}")

            # 保存内容
            print("\n" + "="*80)
            print("💾 SAVING CONTENT")
            print("="*80)

            output_dir = Path("/home/zhiping/Projects/Download_paper/nature_page_content")
            output_dir.mkdir(parents=True, exist_ok=True)

            # 保存HTML
            html_file = output_dir / "s41567-019-0584-7_page.html"
            with open(html_file, 'w', encoding='utf-8') as f:
                f.write(html)
            print(f"\n✓ HTML已保存: {html_file}")
            print(f"  大小: {len(html)} 字节")

            # 保存纯文本
            text_file = output_dir / "s41567-019-0584-7_page_text.txt"
            with open(text_file, 'w', encoding='utf-8') as f:
                f.write(body_text)
            print(f"✓ 纯文本已保存: {text_file}")
            print(f"  大小: {len(body_text)} 字符")

            # 保存Meta信息
            meta_file = output_dir / "s41567-019-0584-7_meta.json"
            with open(meta_file, 'w', encoding='utf-8') as f:
                json.dump(meta_info, f, ensure_ascii=False, indent=2)
            print(f"✓ Meta信息已保存: {meta_file}")

            # 截屏
            screenshot_file = output_dir / "s41567-019-0584-7_screenshot.png"
            await page.screenshot(path=str(screenshot_file), full_page=True)
            print(f"✓ 截屏已保存: {screenshot_file}")

            # 页面结构分析
            print("\n" + "-"*80)
            print("📊 页面结构分析:")
            print("-"*80)

            structure = await page.evaluate("""() => {
                return {
                    title: document.title,
                    h1: document.querySelectorAll('h1').length,
                    h2: document.querySelectorAll('h2').length,
                    h3: document.querySelectorAll('h3').length,
                    paragraphs: document.querySelectorAll('p').length,
                    images: document.querySelectorAll('img').length,
                    links: document.querySelectorAll('a').length,
                    sections: document.querySelectorAll('section').length,
                    articles: document.querySelectorAll('article').length
                };
            }""")

            for key, value in structure.items():
                print(f"  {key}: {value}")

            print("\n" + "="*80)
            print("✅ 页面内容已获取")
            print("="*80)
            print(f"\n📁 所有内容已保存到: {output_dir}/")

            # 关闭页面
            await page.close()

            return {
                'title': title,
                'url': current_url,
                'html_size': len(html),
                'text_size': len(body_text),
                'meta_count': len(meta_info),
                'output_dir': str(output_dir),
                'files': {
                    'html': str(html_file),
                    'text': str(text_file),
                    'meta': str(meta_file),
                    'screenshot': str(screenshot_file)
                }
            }

        except Exception as e:
            print(f"❌ 出错: {e}")
            import traceback
            traceback.print_exc()
            return False


async def main():
    """主函数"""
    url = "https://www.nature.com/articles/s41567-019-0584-7"
    result = await open_nature_page(url)

    if result and isinstance(result, dict):
        print(f"\n📊 最终总结:")
        print(f"  标题: {result['title']}")
        print(f"  URL: {result['url']}")
        print(f"  HTML大小: {result['html_size']} 字节")
        print(f"  文本大小: {result['text_size']} 字符")
        print(f"  Meta标签: {result['meta_count']} 个")


if __name__ == "__main__":
    asyncio.run(main())

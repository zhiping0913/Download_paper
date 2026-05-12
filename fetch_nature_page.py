#!/usr/bin/env python3
"""
使用Playwright + Chrome打开Nature文章页面
根据chrome_launcher.py配置Chrome
"""

import asyncio
import sys
import os
sys.path.insert(0, '/home/zhiping/Projects/Download_paper')

from chrome_launcher import launch_chrome
from playwright.async_api import async_playwright
import time


async def fetch_nature_page_with_chrome():
    """使用已启动的Chrome访问Nature页面"""

    url = "https://www.nature.com/articles/s41567-019-0584-7"

    print("="*80)
    print("🌐 FETCHING NATURE PAGE WITH CHROME")
    print("="*80)
    print(f"📍 URL: {url}\n")

    # 连接到Chrome的远程调试端口
    print("🔗 连接到Chrome远程调试端口...")
    try:
        async with async_playwright() as p:
            # 连接到已启动的Chrome实例 (端口9222)
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")

            print("✅ 已连接到Chrome\n")

            # 创建新上下文
            context = await browser.new_context(viewport={'width': 1920, 'height': 1080})

            # 创建新页面（新标签页）
            page = await context.new_page()

            print(f"📄 打开页面: {url}")
            await page.goto(url, wait_until="load", timeout=30000)

            print("✅ 页面已加载\n")

            # 获取页面信息
            print("="*80)
            print("📋 PAGE CONTENT ANALYSIS")
            print("="*80)

            # 获取标题
            title = await page.title()
            print(f"\n📌 页面标题: {title}")

            # 获取URL
            current_url = page.url
            print(f"📍 当前URL: {current_url}")

            # 获取页面内容大小
            html = await page.content()
            print(f"📊 HTML大小: {len(html)} 字节")

            # 提取主要内容
            print("\n" + "-"*80)
            print("📖 页面主要内容:")
            print("-"*80)

            # 文章标题
            article_title = await page.query_selector('h1, [class*="title"]')
            if article_title:
                title_text = await article_title.text_content()
                print(f"\n✓ 文章标题:")
                print(f"  {title_text[:100]}")

            # 摘要
            abstract_elem = await page.query_selector('[class*="abstract"], [class*="summary"]')
            if abstract_elem:
                abstract_text = await abstract_elem.text_content()
                print(f"\n✓ 摘要 ({len(abstract_text)} 字符):")
                print(f"  {abstract_text[:200]}...")

            # 作者信息
            authors = await page.query_selector_all('[class*="author"], [data-test*="author"]')
            print(f"\n✓ 检测到 {len(authors)} 个作者元素")

            # 图表
            figures = await page.query_selector_all('figure, [class*="fig"], img[alt*="Fig"]')
            print(f"✓ 检测到 {len(figures)} 个图表/图片")

            # 参考文献
            references = await page.query_selector_all('[class*="ref"], [class*="citation"]')
            print(f"✓ 检测到 {len(references)} 个参考文献元素")

            # 获取所有文本内容
            print("\n" + "-"*80)
            print("📝 完整页面文本内容:")
            print("-"*80)

            body_text = await page.evaluate("() => document.body.innerText")
            print(f"\n总文本长度: {len(body_text)} 字符\n")
            print("前2000字符预览:")
            print(body_text[:2000])
            if len(body_text) > 2000:
                print("\n... (内容已截断)")

            # 获取所有meta标签信息
            print("\n" + "-"*80)
            print("🏷️  Meta标签信息:")
            print("-"*80)

            metas = await page.query_selector_all('meta')
            meta_info = {}
            for meta in metas:
                name = await meta.get_attribute('name')
                property_attr = await meta.get_attribute('property')
                content = await meta.get_attribute('content')

                key = name or property_attr
                if key and content:
                    meta_info[key] = content[:80]

            # 显示重要的meta标签
            important_metas = ['citation_title', 'citation_doi', 'citation_author',
                             'article-access', 'og:title', 'og:description']

            for meta_key in important_metas:
                if meta_key in meta_info:
                    print(f"✓ {meta_key}: {meta_info[meta_key]}")

            # 保存完整内容到文件
            print("\n" + "="*80)
            print("💾 SAVING CONTENT")
            print("="*80)

            output_dir = "/home/zhiping/Projects/Download_paper/nature_page_content"
            os.makedirs(output_dir, exist_ok=True)

            # 保存HTML
            html_file = f"{output_dir}/s41567-019-0584-7_full.html"
            with open(html_file, 'w', encoding='utf-8') as f:
                f.write(html)
            print(f"\n✓ HTML已保存: {html_file}")

            # 保存纯文本
            text_file = f"{output_dir}/s41567-019-0584-7_text.txt"
            with open(text_file, 'w', encoding='utf-8') as f:
                f.write(body_text)
            print(f"✓ 纯文本已保存: {text_file}")

            # 保存meta信息
            import json
            meta_file = f"{output_dir}/s41567-019-0584-7_meta.json"
            with open(meta_file, 'w', encoding='utf-8') as f:
                json.dump(meta_info, f, ensure_ascii=False, indent=2)
            print(f"✓ Meta信息已保存: {meta_file}")

            # 截屏
            screenshot_file = f"{output_dir}/s41567-019-0584-7_screenshot.png"
            await page.screenshot(path=screenshot_file, full_page=True)
            print(f"✓ 截屏已保存: {screenshot_file}")

            print("\n" + "="*80)
            print("✅ 页面获取完成")
            print("="*80)
            print(f"\n📁 所有文件已保存到: {output_dir}/")

            # 关闭浏览器
            await browser.close()

            return {
                'html_size': len(html),
                'text_size': len(body_text),
                'title': title,
                'url': current_url,
                'files_saved': {
                    'html': html_file,
                    'text': text_file,
                    'meta': meta_file,
                    'screenshot': screenshot_file
                }
            }

    except Exception as e:
        print(f"❌ 错误: {str(e)}")
        import traceback
        traceback.print_exc()
        return None


async def main():
    """主函数"""

    # 首先启动Chrome
    print("🚀 启动Chrome浏览器...\n")
    chrome_proc = launch_chrome(use_user_config=False, headless=False)

    # 等待Chrome完全启动
    time.sleep(3)

    # 连接并获取页面
    result = await fetch_nature_page_with_chrome()

    if result:
        print(f"\n📊 结果摘要:")
        print(f"  HTML大小: {result['html_size']} 字节")
        print(f"  文本大小: {result['text_size']} 字节")
        print(f"  页面标题: {result['title']}")
        print(f"  当前URL: {result['url']}")


if __name__ == "__main__":
    asyncio.run(main())

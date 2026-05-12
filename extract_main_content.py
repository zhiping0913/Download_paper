#!/usr/bin/env python3
"""
只转换 <div class="main-content" data-nosnippet=""> 部分的正文内容
从s41567-019-0584-7_page.html第1147行开始
"""

import sys
import json
from pathlib import Path
sys.path.insert(0, '/home/zhiping/Projects/Download_paper')

from json_to_md_converter import convert_html_to_markdown, cleanup_markdown
from complete_paper_extraction import extract_text_without_math
from bs4 import BeautifulSoup, NavigableString


def extract_main_content_to_markdown(html_file: str, output_file: str):
    """
    只转换main-content div中的正文内容为Markdown
    """

    print("="*80)
    print("🔄 CONVERTING MAIN-CONTENT DIV TO MARKDOWN")
    print("="*80)

    # 读取HTML
    with open(html_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    # 解析HTML
    soup = BeautifulSoup(html_content, 'html.parser')

    # 查找 main-content div
    main_content_div = soup.find('div', {'class': 'main-content', 'data-nosnippet': ''})

    if not main_content_div:
        print("❌ 未找到 main-content div")
        return None

    print("✓ 已找到 main-content div")
    print(f"  HTML长度: {len(str(main_content_div))} 字节")

    markdown_parts = []

    # 遍历main-content中的所有section
    sections = main_content_div.find_all('section', recursive=False)
    print(f"✓ 检测到 {len(sections)} 个section")

    for section_idx, section in enumerate(sections, 1):
        # 获取section标题
        section_title = section.get('data-title', '')

        if section_title:
            markdown_parts.append(f"## {section_title}\n\n")
            print(f"  Section {section_idx}: {section_title}")

        # 查找article-section
        article_section = section.find('div', {'class': 'c-article-section'})
        if not article_section:
            continue

        # 获取section内容
        content_div = article_section.find('div', {'class': 'c-article-section__content'})
        if not content_div:
            continue

        # 提取所有段落和子标题
        h2 = article_section.find('h2', {'class': 'c-article-section__title'})
        if h2 and h2.text.strip():
            title_text = h2.text.strip()
            if title_text not in markdown_parts:
                markdown_parts.append(f"### {title_text}\n\n")

        # 提取所有h3子标题
        h3_elements = content_div.find_all('h3', {'class': 'c-article__sub-heading'})
        for h3 in h3_elements:
            h3_text = h3.text.strip()
            if h3_text:
                markdown_parts.append(f"#### {h3_text}\n\n")

        # 提取所有段落
        paragraphs = content_div.find_all('p', recursive=True, limit=100)
        print(f"    提取 {len(paragraphs)} 个段落")

        for para_idx, para in enumerate(paragraphs, 1):
            # 获取段落HTML
            para_html = str(para)

            try:
                # 处理公式
                text_with_formulas = extract_text_without_math(para_html)

                if text_with_formulas and len(text_with_formulas.strip()) > 20:
                    # 使用convert_html_to_markdown转换
                    md_text = convert_html_to_markdown(para_html)

                    if md_text:
                        # 清理markdown
                        md_text = cleanup_markdown(md_text)
                        markdown_parts.append(f"{md_text}\n\n")

                        if para_idx <= 3 or para_idx % 10 == 0:
                            print(f"      ✓ 段落 {para_idx}: {len(md_text)} 字符")

            except Exception as e:
                print(f"      ⚠️  段落 {para_idx} 处理错误: {str(e)[:40]}")
                continue

        # 查找图表
        figures = content_div.find_all('figure')
        if figures:
            print(f"    检测到 {len(figures)} 个图表")
            for fig_idx, fig in enumerate(figures, 1):
                fig_caption = fig.find('figcaption')
                if fig_caption:
                    caption_text = fig_caption.get_text(strip=True)
                    if caption_text:
                        markdown_parts.append(f"**{caption_text[:100]}**\n\n")

    # 最终清理
    final_markdown = "".join(markdown_parts)
    final_markdown = cleanup_markdown(final_markdown)

    # 保存到文件
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(final_markdown)

    file_size = len(final_markdown)
    line_count = len(final_markdown.splitlines())

    print(f"\n✅ Markdown已保存: {output_file}")
    print(f"   大小: {file_size} 字符")
    print(f"   行数: {line_count} 行")

    return {
        'output_file': output_file,
        'size': file_size,
        'lines': line_count
    }


def main():
    """主函数"""

    html_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_page.html"
    output_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_main_content.md"

    result = extract_main_content_to_markdown(html_file, output_file)

    if result:
        print("\n" + "="*80)
        print("📊 转换完成")
        print("="*80)
        print(f"输出文件: {result['output_file']}")
        print(f"文件大小: {result['size']} 字符 ({result['lines']} 行)")
        print(f"\n已转换的内容:")
        print(f"  ✓ 来自: <div class=\"main-content\" data-nosnippet=\"\">")
        print(f"  ✓ 包含: 所有正文段落、子标题、公式、图表说明")
        print(f"  ✓ 格式: Markdown with LaTeX formulas")


if __name__ == "__main__":
    main()

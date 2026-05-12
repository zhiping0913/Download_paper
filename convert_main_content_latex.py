#!/usr/bin/env python3
"""
转换 <div class="main-content" data-nosnippet=""> 中的正文内容为Markdown
只使用 json_to_md_converter.py 中的函数
将公式转换为LaTeX
"""

import sys
from pathlib import Path
sys.path.insert(0, '/home/zhiping/Projects/Download_paper')

from json_to_md_converter import (
    convert_html_to_markdown,
    clean_html_body,
    cleanup_markdown,
    traverse_json_recursive
)
from bs4 import BeautifulSoup


def extract_main_content_html(html_file: str) -> str:
    """提取main-content div的HTML"""
    with open(html_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, 'html.parser')
    main_content_div = soup.find('div', {'class': 'main-content', 'data-nosnippet': ''})

    if main_content_div:
        return str(main_content_div)
    return ""


def convert_main_content_to_markdown(html_file: str, output_file: str):
    """
    使用json_to_md_converter.py中的函数转换main-content为Markdown
    """

    print("="*80)
    print("🔄 转换 main-content 正文为Markdown (仅用json_to_md_converter函数)")
    print("="*80)

    # 1. 提取main-content的HTML
    print("\n1️⃣  提取 main-content div...")
    main_content_html = extract_main_content_html(html_file)

    if not main_content_html:
        print("❌ 未找到 main-content div")
        return None

    print(f"✓ 已提取: {len(main_content_html)} 字节")

    # 2. 清理HTML
    print("\n2️⃣  清理HTML (使用clean_html_body)...")
    cleaned_html = clean_html_body(main_content_html)
    print(f"✓ 已清理: {len(cleaned_html)} 字节")

    # 3. 转换为Markdown (使用convert_html_to_markdown)
    print("\n3️⃣  转换HTML为Markdown (使用convert_html_to_markdown)...")
    markdown_content = convert_html_to_markdown(cleaned_html)
    print(f"✓ 转换完成: {len(markdown_content)} 字符")

    # 4. 最终清理 (使用cleanup_markdown - 处理LaTeX命令)
    print("\n4️⃣  清理Markdown并处理LaTeX (使用cleanup_markdown)...")
    final_markdown = cleanup_markdown(markdown_content)
    print(f"✓ 清理完成: {len(final_markdown)} 字符")

    # 5. 保存文件
    print("\n5️⃣  保存Markdown文件...")
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
        'lines': line_count,
        'html_input': len(main_content_html)
    }


def main():
    """主函数"""

    html_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_page.html"
    output_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_main_latex.md"

    result = convert_main_content_to_markdown(html_file, output_file)

    if result:
        print("\n" + "="*80)
        print("📊 转换结果总结")
        print("="*80)
        print(f"\n输入:")
        print(f"  HTML大小: {result['html_input']} 字节")
        print(f"  来源: <div class=\"main-content\" data-nosnippet=\"\"> (第1147行)")

        print(f"\n输出:")
        print(f"  文件: {result['output_file']}")
        print(f"  大小: {result['size']} 字符")
        print(f"  行数: {result['lines']} 行")

        print(f"\n使用的函数 (json_to_md_converter.py):")
        print(f"  1. clean_html_body() - 清理HTML")
        print(f"  2. convert_html_to_markdown() - HTML → Markdown")
        print(f"  3. cleanup_markdown() - 处理LaTeX命令")

        print(f"\n功能:")
        print(f"  ✓ 公式转换为LaTeX格式")
        print(f"  ✓ 移除不兼容的LaTeX命令 (\\mspace, \\ensuremath等)")
        print(f"  ✓ HTML实体转换为文本字符")


if __name__ == "__main__":
    main()

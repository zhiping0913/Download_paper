#!/usr/bin/env python3
"""
改进版: 转换main-content为Markdown，移除不必要的换行和属性
使用json_to_md_converter.py的函数，加上额外的清理
"""

import sys
import re
from pathlib import Path
sys.path.insert(0, '/home/zhiping/Projects/Download_paper')

from json_to_md_converter import (
    convert_html_to_markdown,
    clean_html_body,
    cleanup_markdown
)
from bs4 import BeautifulSoup


def remove_pandoc_syntax(markdown: str) -> str:
    """移除Pandoc特殊语法和HTML属性"""

    # 移除 ::: {...} 块语法
    markdown = re.sub(r'::: \{[^}]*\}\n', '', markdown)
    markdown = re.sub(r':::\n', '', markdown)

    # 移除行内的HTML属性（在markdown链接中保留）
    # 例如: ^[9](/articles/...){#ref-link...}^ -> ^[9](/articles/...)^
    markdown = re.sub(r'\}\{[^}]*\}([,\s])', r'\1', markdown)

    # 移除剩余的HTML属性
    markdown = re.sub(r'\s+(?:data-\w+|aria-\w+|test|track-\w+|title)=["\'][^"\']*["\']', '', markdown)
    markdown = re.sub(r'\s+(?:data-\w+|aria-\w+|test|track-\w+)=\S+', '', markdown)

    return markdown


def merge_wrapped_lines(markdown: str) -> str:
    """
    合并被参考文献标记分割的段落
    例如: "词汇^[9](...){...}\n词汇" -> "词汇^[9](...)词汇"
    """

    # 合并被上标脚注分割的行
    # 模式: 行末是 }^ 或 }$，下一行不是空行
    lines = markdown.split('\n')
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]
        result.append(line)

        # 如果当前行以 }^ 结尾，且下一行存在且不是空行
        if line.rstrip().endswith('}^') or line.rstrip().endswith('},['):
            if i + 1 < len(lines) and lines[i + 1].strip() and not lines[i + 1].startswith('#'):
                # 移除当前行末的换行，与下一行合并
                result[-1] = result[-1].rstrip() + ' '
                i += 1
                result[-1] += lines[i]

        i += 1

    return '\n'.join(result)


def clean_reference_formatting(markdown: str) -> str:
    """清理参考文献格式中的多余属性和换行"""

    # 修复被分割的参考文献链接
    # 合并: ](\n  href="/..."{...
    markdown = re.sub(r'\]\(\n\s+href=', '](', markdown)

    # 移除link文本中的多行属性
    # 例如: ](/articles/...){#ref-link-section-d53767785e602\naria-label=...
    #       track=...}^
    def fix_reference_link(match):
        full_text = match.group(0)
        # 提取URL部分
        url_match = re.search(r'\]\(([^)]+)\)', full_text)
        if url_match:
            url = url_match.group(1)
            # 去掉属性，保留URL
            return f']({url})^'
        return full_text

    markdown = re.sub(r'\]\([^)]*\)[^)]*\}^', fix_reference_link, markdown, flags=re.DOTALL)

    # 移除剩余的属性字符串
    markdown = re.sub(r'\n\s+(?:aria-label|data-test|test|track-\w+)=', ' ', markdown)

    return markdown


def convert_main_content_to_markdown_clean(html_file: str, output_file: str):
    """
    转换main-content为清洁的Markdown
    """

    print("="*80)
    print("🔄 转换 main-content 为Markdown (改进版 - 清理不必要的换行)")
    print("="*80)

    # 1. 提取main-content的HTML
    print("\n1️⃣  提取 main-content div...")
    with open(html_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, 'html.parser')
    main_content_div = soup.find('div', {'class': 'main-content', 'data-nosnippet': ''})

    if not main_content_div:
        print("❌ 未找到 main-content div")
        return None

    main_content_html = str(main_content_div)
    print(f"✓ 已提取: {len(main_content_html)} 字节")

    # 2. 清理HTML
    print("\n2️⃣  清理HTML...")
    cleaned_html = clean_html_body(main_content_html)
    print(f"✓ 已清理: {len(cleaned_html)} 字节")

    # 3. 转换为Markdown
    print("\n3️⃣  转换HTML为Markdown...")
    markdown_content = convert_html_to_markdown(cleaned_html)
    print(f"✓ 转换完成: {len(markdown_content)} 字符")

    # 4. 清理Markdown中的LaTeX命令
    print("\n4️⃣  清理LaTeX命令...")
    markdown_content = cleanup_markdown(markdown_content)

    # 5. 移除Pandoc特殊语法
    print("\n5️⃣  移除Pandoc特殊语法...")
    markdown_content = remove_pandoc_syntax(markdown_content)

    # 6. 清理参考文献格式
    print("\n6️⃣  清理参考文献格式...")
    markdown_content = clean_reference_formatting(markdown_content)

    # 7. 合并被分割的行
    print("\n7️⃣  合并被分割的段落...")
    markdown_content = merge_wrapped_lines(markdown_content)

    # 8. 清理多余的空行
    print("\n8️⃣  清理多余空行...")
    # 移除连续的空行（保留最多一个）
    markdown_content = re.sub(r'\n\n\n+', '\n\n', markdown_content)

    # 移除行尾的多余空格
    lines = markdown_content.split('\n')
    lines = [line.rstrip() for line in lines]
    markdown_content = '\n'.join(lines)

    # 保存文件
    print("\n9️⃣  保存Markdown文件...")
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(markdown_content)

    file_size = len(markdown_content)
    line_count = len(markdown_content.splitlines())

    print(f"\n✅ Markdown已保存: {output_file}")
    print(f"   大小: {file_size} 字符")
    print(f"   行数: {line_count} 行")

    return {
        'output_file': output_file,
        'size': file_size,
        'lines': line_count,
    }


def main():
    """主函数"""

    html_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_page.html"
    output_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_main_clean.md"

    result = convert_main_content_to_markdown_clean(html_file, output_file)

    if result:
        print("\n" + "="*80)
        print("📊 转换完成 - 改进版结果")
        print("="*80)
        print(f"\n输出: {result['output_file']}")
        print(f"大小: {result['size']} 字符 ({result['lines']} 行)")
        print(f"\n✨ 清理内容:")
        print(f"  ✓ 移除Pandoc特殊语法 (:::)")
        print(f"  ✓ 移除HTML属性 (data-*, aria-*, test等)")
        print(f"  ✓ 合并被参考文献分割的段落")
        print(f"  ✓ 清理多余空行")
        print(f"  ✓ 保持LaTeX公式格式")


if __name__ == "__main__":
    main()

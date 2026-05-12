#!/usr/bin/env python3
"""
简化引用格式
将 ^[2](/articles/...#ref-CR2) 改为 [2]
"""

import re
from pathlib import Path


def simplify_citations(markdown_content: str) -> str:
    """
    简化引用格式
    ^[N](/articles/...#ref-CRN) -> [N]
    [N](/articles/...#ref-CRN) -> [N]
    """

    # 处理 ^[数字](URL) 格式
    markdown_content = re.sub(
        r'\^\[(\d+)\]\([^)]*\)',
        r'[\1]',
        markdown_content
    )

    # 处理 [数字](URL) 格式（没有 ^ 的情况）
    # 但要保留普通链接 [文本](URL)
    # 只简化引用链接 [数字](URL)
    markdown_content = re.sub(
        r'\[(\d+)\]\(/articles/[^)]*#ref-[^)]*\)',
        r'[\1]',
        markdown_content
    )

    return markdown_content


def main():
    """主函数"""

    input_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_main_final.md"
    output_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_main_simplified.md"

    print("="*80)
    print("🔧 简化引用格式")
    print("="*80)

    print(f"\n读取文件: {input_file}")
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()

    original_size = len(content)

    print("转换引用格式...")
    print("  从: ^[2](/articles/s41567-019-0584-7#ref-CR2)")
    print("  到: [2]")

    content = simplify_citations(content)

    new_size = len(content)
    reduction = original_size - new_size

    print(f"\n✓ 文件大小: {original_size} → {new_size} 字符 (减少 {reduction} 字符)")

    print(f"\n保存文件: {output_file}")
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(content)

    line_count = len(content.splitlines())
    print(f"\n✅ 完成")
    print(f"   文件大小: {new_size} 字符")
    print(f"   行数: {line_count} 行")


if __name__ == "__main__":
    main()

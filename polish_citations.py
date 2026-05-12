#!/usr/bin/env python3
"""
进一步清理引用格式
1. 移除引用后的 ^ 符号
2. 清理剩余的引用URL格式
"""

import re
from pathlib import Path


def fully_simplify_citations(markdown_content: str) -> str:
    """
    完全简化引用格式
    """

    # 1. 处理 [数字](#ref-CRN) 格式
    markdown_content = re.sub(
        r'\[(\d+)\]\(#ref-CR\d+\)',
        r'[\1]',
        markdown_content
    )

    # 2. 处理 [数字](#ref-CRN "..." ) 格式（带引号）
    markdown_content = re.sub(
        r'\[(\d+)\]\(#ref-CR\d+\s+"[^"]*"\)',
        r'[\1]',
        markdown_content
    )

    # 3. 移除引用后的 ^ 符号（如果有的话）
    # [2]^ -> [2]
    markdown_content = re.sub(r'\]\^', ']', markdown_content)

    # 4. 清理多个连续的引用之间的空格
    # [2]  [3] -> [2][3]
    markdown_content = re.sub(r'\]\s+\[', '][', markdown_content)

    return markdown_content


def main():
    """主函数"""

    input_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_main_simplified.md"
    output_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_main_polished.md"

    print("="*80)
    print("✨ 进一步精化引用格式")
    print("="*80)

    print(f"\n读取文件: {input_file}")
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()

    original_size = len(content)

    print("最终清理...")
    print("  1. 移除 [2](#ref-CR2) 中的URL")
    print("  2. 移除 ^ 符号")
    print("  3. 合并连续引用 [2] [3] -> [2][3]")

    content = fully_simplify_citations(content)

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

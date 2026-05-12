#!/usr/bin/env python3
"""
最后的修复：合并被不必要分割的段落行
保持每个段落在一行或必要时换行
"""

import re
from pathlib import Path


def merge_paragraph_lines(markdown_content: str) -> str:
    """
    合并段落内的行，保留段落间的空行
    """

    lines = markdown_content.split('\n')
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # 如果是标题、代码或空行，直接保留
        if not line.strip() or line.startswith('#') or line.startswith('```'):
            result.append(line)
            i += 1
            continue

        # 收集这个段落的所有行（直到遇到空行或标题）
        paragraph_lines = [line]
        i += 1

        while i < len(lines):
            next_line = lines[i]

            # 遇到空行、标题或代码块时停止
            if not next_line.strip() or next_line.startswith('#') or next_line.startswith('```'):
                break

            # 行太长（超过120字符）时可能是已经是完整行，保留分割
            if len(paragraph_lines[-1]) > 120:
                result.append(paragraph_lines[-1])
                paragraph_lines = []

            paragraph_lines.append(next_line)
            i += 1

        # 合并段落行
        if paragraph_lines:
            merged_paragraph = ' '.join(line.strip() for line in paragraph_lines if line.strip())

            # 对于很长的段落，按120字符断行以保持可读性
            if len(merged_paragraph) > 120:
                # 按句子或短语分割
                # 但尽量避免在引用或特殊地方分割
                words = merged_paragraph.split()
                current_line = []
                current_length = 0

                for word in words:
                    word_len = len(word) + 1  # +1 for space
                    if current_length + word_len > 120 and current_line:
                        result.append(' '.join(current_line))
                        current_line = [word]
                        current_length = word_len
                    else:
                        current_line.append(word)
                        current_length += word_len

                if current_line:
                    result.append(' '.join(current_line))
            else:
                result.append(merged_paragraph)

    return '\n'.join(result)


def main():
    """主函数"""

    input_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_main_polished.md"
    output_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_main.md"

    print("="*80)
    print("🔧 最后修复：合并段落行")
    print("="*80)

    print(f"\n读取文件: {input_file}")
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()

    original_size = len(content)
    original_lines = len(content.splitlines())

    print(f"原始文件: {original_size} 字符，{original_lines} 行")

    print("\n合并段落内的不必要换行...")
    print("  保持: 标题、空行、代码块")
    print("  合并: 段落的短行")
    print("  智能: 超长行按120字符折行")

    content = merge_paragraph_lines(content)

    new_size = len(content)
    new_lines = len(content.splitlines())

    print(f"\n✓ 新文件: {new_size} 字符，{new_lines} 行")
    print(f"  行数减少: {original_lines - new_lines} 行 ({100*(original_lines-new_lines)/original_lines:.1f}%)")

    print(f"\n保存文件: {output_file}")
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(content)

    print(f"\n✅ 完成")
    print(f"   输出: {output_file}")
    print(f"   大小: {new_size} 字符，{new_lines} 行")


if __name__ == "__main__":
    main()

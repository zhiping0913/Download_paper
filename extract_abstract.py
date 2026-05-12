#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从Nature HTML中提取并转换Abstract
使用与正文相同的转换函数处理，包括公式和引用
"""

import re
from pathlib import Path
from bs4 import BeautifulSoup
import sys

sys.path.insert(0, '/home/zhiping/Projects/Download_paper')

from json_to_md_converter import convert_html_to_markdown, cleanup_markdown
from convert_by_paragraph import convert_paragraph


def extract_abstract_from_html(html_file: str) -> str:
    """从HTML中提取abstract并转换为Markdown"""

    with open(html_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, 'html.parser')

    # 找到abstract部分
    abstract_section = soup.find('section', {'data-title': 'Abstract'})
    if not abstract_section:
        print("❌ 未找到Abstract部分")
        return ""

    # 找到abstract的内容段落
    abstract_content = abstract_section.find('div', {'class': 'c-article-section__content'})
    if not abstract_content:
        print("❌ 未找到Abstract内容")
        return ""

    # 获取第一个<p>标签（abstract通常是一个段落）
    abstract_p = abstract_content.find('p')
    if not abstract_p:
        print("❌ 未找到Abstract段落")
        return ""

    # 获取HTML内容
    abstract_html = str(abstract_p)

    print("✓ 找到Abstract HTML")

    # 使用convert_paragraph函数转换（使用与正文相同的转换流程）
    try:
        # 转换
        md = convert_html_to_markdown(abstract_html)

        # 清理LaTeX命令
        md = cleanup_markdown(md)

        # 移除段落内的换行（但保留段落级的结构）
        md = re.sub(r'\s+', ' ', md)

        # 清理首尾空格
        md = md.strip()

        print(f"✓ Abstract转换完成: {len(md)} 字符")

        return md

    except Exception as e:
        print(f"⚠️  转换错误: {str(e)[:50]}")
        return ""


def save_abstract_to_markdown(abstract_md: str, main_file: str, output_file: str):
    """
    将Abstract插入到main前面，生成新的Markdown文件
    """

    with open(main_file, 'r', encoding='utf-8') as f:
        main_content = f.read()

    # 生成带有Abstract的完整内容
    complete_content = f"""## Abstract

{abstract_md}

{main_content}"""

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(complete_content)

    print(f"✓ 已保存到: {output_file}")


def main():
    """主函数"""
    html_file = "/home/zhiping/Projects/Download_paper/captured_data/page_000.html"
    main_file = "/home/zhiping/Projects/Download_paper/captured_data/nature_main_by_paragraph.md"
    output_file = "/home/zhiping/Projects/Download_paper/captured_data/nature_main_with_abstract.md"

    print("=" * 80)
    print("📖 提取并转换Abstract")
    print("=" * 80)

    print("\n1️⃣  从HTML中提取Abstract...")
    abstract_md = extract_abstract_from_html(html_file)

    if abstract_md:
        print("\n2️⃣  插入Abstract到Main前面...")
        save_abstract_to_markdown(abstract_md, main_file, output_file)

        print("\n📋 Abstract预览:")
        print(abstract_md[:200] + "...")

        print("\n✅ 完成！")
        print(f"   输出: {output_file}")
    else:
        print("\n❌ Abstract提取失败")


if __name__ == "__main__":
    main()

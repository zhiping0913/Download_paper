#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从Nature HTML中提取引用列表
格式: [编号] 作者. 标题. 期刊 年份. DOI: xxx（如果有的话）
"""

import re
from pathlib import Path
from bs4 import BeautifulSoup
import html


def extract_references_from_html(html_file: str) -> list:
    """从HTML中提取引用"""

    with open(html_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, 'html.parser')

    # 找到引用列表
    refs_list = soup.find('ol', {'class': 'c-article-references'})
    if not refs_list:
        print("❌ 未找到引用列表")
        return []

    references = []

    # 遍历每个引用项
    for item in refs_list.find_all('li', {'class': 'c-article-references__item'}):
        # 提取编号
        counter = item.get('data-counter', '')
        number = counter.replace('.', '').strip()

        # 提取引用文本
        ref_text_p = item.find('p', {'class': 'c-article-references__text'})
        if not ref_text_p:
            continue

        # 获取纯文本（去掉HTML标签但保留格式）
        ref_text = ref_text_p.get_text(strip=True)
        # 反转义HTML实体
        ref_text = html.unescape(ref_text)

        # 提取DOI（如果有）
        doi = None
        doi_link = item.find('a', {'data-doi': True})
        if doi_link:
            doi = doi_link.get('data-doi')

        # 格式化引用
        formatted_ref = f"[{number}] {ref_text}"
        if doi:
            formatted_ref += f" DOI: {doi}"

        references.append(formatted_ref)

        # 打印进度
        if len(references) <= 5 or len(references) % 10 == 0:
            print(f"✓ 引用 {number}: {len(ref_text)} 字符")

    return references


def save_references(references: list, output_file: str):
    """保存引用到文件"""

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write("# References\n\n")
        for ref in references:
            f.write(ref + "\n\n")

    print(f"\n✅ 保存完成")
    print(f"   输出: {output_file}")
    print(f"   引用数: {len(references)} 条")


def main():
    html_file = "/home/zhiping/Projects/Download_paper/captured_data/page_000.html"
    output_file = "/home/zhiping/Projects/Download_paper/captured_data/nature_references.md"

    print("=" * 80)
    print("📖 提取Nature论文引用")
    print("=" * 80)

    print("\n1️⃣  从HTML中提取引用...")
    references = extract_references_from_html(html_file)

    if references:
        print(f"✓ 找到 {len(references)} 条引用")

        print("\n2️⃣  保存引用...")
        save_references(references, output_file)

        print("\n📋 前5条引用预览:")
        for ref in references[:5]:
            print(f"   {ref[:100]}...")
    else:
        print("❌ 未找到任何引用")


if __name__ == "__main__":
    main()

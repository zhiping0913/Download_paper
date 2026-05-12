#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从Nature HTML中提取Supplementary information
"""

import re
from pathlib import Path
from bs4 import BeautifulSoup
import sys

sys.path.insert(0, '/home/zhiping/Projects/Download_paper')


def extract_supplementary_information_from_html(html_file: str) -> str:
    """从HTML中提取Supplementary information"""

    with open(html_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, 'html.parser')

    # 找到Supplementary information部分
    supp_section = soup.find('section', {'data-title': 'Supplementary information'})
    if not supp_section:
        print("❌ 未找到Supplementary information部分")
        return ""

    # 找到内容div
    content_div = supp_section.find('div', {'class': 'c-article-section__content'})
    if not content_div:
        print("❌ 未找到Supplementary information内容")
        return ""

    # 获取所有supplementary items
    items = content_div.find_all('div', {'class': 'c-article-supplementary__item'})
    if not items:
        print("❌ 未找到Supplementary information项目")
        return ""

    print(f"✓ 找到 {len(items)} 个supplementary项目")

    # 提取内容
    md_parts = []
    for i, item in enumerate(items, 1):
        title = item.find('a', {'class': 'print-link'})
        if title:
            title_text = title.get_text(strip=True)
            href = title.get('href', '')
            md_parts.append(f"{i}. [{title_text}]({href})")

    if not md_parts:
        print("❌ 没有有效的项目")
        return ""

    result = "\n".join(md_parts)
    print(f"✓ Supplementary information转换完成: {len(result)} 字符")

    return result


def main():
    """主函数"""
    html_file = "/home/zhiping/Projects/Download_paper/captured_data/page_000.html"

    print("=" * 80)
    print("📖 提取Supplementary information")
    print("=" * 80)

    print("\n1️⃣  从HTML中提取Supplementary information...")
    supp_md = extract_supplementary_information_from_html(html_file)

    if supp_md:
        print("\n📋 Supplementary information内容:")
        print(supp_md)
        print("\n✅ 完成！")
    else:
        print("\n❌ 提取失败")


if __name__ == "__main__":
    main()

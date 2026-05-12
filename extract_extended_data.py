#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从Nature HTML中提取Extended data figures and tables
"""

import re
from pathlib import Path
from bs4 import BeautifulSoup
import sys

sys.path.insert(0, '/home/zhiping/Projects/Download_paper')


def extract_extended_data_from_html(html_file: str) -> str:
    """从HTML中提取Extended data figures and tables"""

    with open(html_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, 'html.parser')

    # 找到Extended data figures and tables部分
    extended_section = soup.find('section', {'data-title': 'Extended data figures and tables'})
    if not extended_section:
        print("❌ 未找到Extended data figures and tables部分")
        return ""

    # 找到所有supplementary items
    items = extended_section.find_all('div', {'class': 'c-article-supplementary__item'})
    if not items:
        print("❌ 未找到Extended data项目")
        return ""

    print(f"✓ 找到 {len(items)} 个Extended data项目")

    # 提取内容
    md_parts = []
    for i, item in enumerate(items, 1):
        title = item.find('a', {'class': 'print-link'})
        if title:
            title_text = title.get_text(strip=True)
            href = title.get('href', '')

            # 获取描述
            desc_div = item.find('div', {'class': 'c-article-supplementary__description'})
            desc_text = ""
            if desc_div:
                desc_text = desc_div.get_text(strip=True)
                # 清理过度空格
                desc_text = re.sub(r'\s+', ' ', desc_text)

            # 组合内容
            md_parts.append(f"{i}. [{title_text}]({href})")
            if desc_text:
                md_parts.append(f"\n   {desc_text}\n")

    if not md_parts:
        print("❌ 没有有效的项目")
        return ""

    result = "\n".join(md_parts)
    print(f"✓ Extended data转换完成: {len(result)} 字符")

    return result


def main():
    """主函数"""
    html_file = "/home/zhiping/Projects/Download_paper/captured_data/page_000.html"

    print("=" * 80)
    print("📖 提取Extended data figures and tables")
    print("=" * 80)

    print("\n1️⃣  从HTML中提取Extended data...")
    extended_md = extract_extended_data_from_html(html_file)

    if extended_md:
        print("\n📋 Extended data内容预览:")
        print(extended_md[:500] + "...")
        print("\n✅ 完成！")
    else:
        print("\n❌ 提取失败")


if __name__ == "__main__":
    main()

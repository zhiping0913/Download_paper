#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从Nature HTML中提取Author information部分
包括: 作者贡献、从属机构、对应作者等
"""

import re
from pathlib import Path
from bs4 import BeautifulSoup
import sys

sys.path.insert(0, '/home/zhiping/Projects/Download_paper')


def extract_author_information_from_html(html_file: str) -> str:
    """从HTML中提取Author information部分并转换为Markdown"""

    with open(html_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, 'html.parser')

    # 找到Author information部分
    author_section = soup.find('section', {'data-title': 'Author information'})
    if not author_section:
        print("❌ 未找到Author information部分")
        return ""

    # 找到内容div
    content_div = author_section.find('div', {'class': 'c-article-section__content'})
    if not content_div:
        print("❌ 未找到Author information内容")
        return ""

    # 提取关键信息
    md_parts = []

    # 1. 贡献声明
    author_notes = content_div.find('ol', {'class': 'c-article-author-information__list'})
    if author_notes:
        items = author_notes.find_all('li', {'class': 'c-article-author-information__item'})
        if items:
            for item in items:
                p = item.find('p')
                if p:
                    text = p.get_text(strip=True)
                    md_parts.append(f"**Equal contribution:** {text}")

    # 2. 作者和从属机构
    affiliations_heading = content_div.find('h3', string='Authors and Affiliations')
    if affiliations_heading:
        aff_list = affiliations_heading.find_next('ol', {'class': 'c-article-author-affiliation__list'})
        if aff_list:
            md_parts.append("\n### Authors and Affiliations\n")
            for li in aff_list.find_all('li'):
                address = li.find('p', {'class': 'c-article-author-affiliation__address'})
                authors = li.find('p', {'class': 'c-article-author-affiliation__authors-list'})
                if address and authors:
                    address_text = address.get_text(strip=True)
                    authors_text = authors.get_text(strip=True)
                    md_parts.append(f"- **{address_text}**\n  {authors_text}\n")

    # 3. 贡献
    contributions_heading = content_div.find('h3', string='Contributions')
    if contributions_heading:
        contrib_p = contributions_heading.find_next('p')
        if contrib_p:
            md_parts.append("\n### Contributions\n")
            contrib_text = contrib_p.get_text(strip=True)
            md_parts.append(contrib_text)

    # 4. 对应作者
    corresponding_heading = content_div.find('h3', string='Corresponding authors')
    if corresponding_heading:
        corr_p = corresponding_heading.find_next('p', {'id': 'corresponding-author-list'})
        if corr_p:
            md_parts.append("\n### Corresponding authors\n")
            # 提取文本，移除email标签
            corr_text = corr_p.get_text(strip=True)
            # 清理多余的空格
            corr_text = re.sub(r'\s+', ' ', corr_text)
            md_parts.append(corr_text)

    if not md_parts:
        print("❌ 没有找到有效的Author information内容")
        return ""

    result = "\n".join(md_parts)
    print(f"✓ Author information转换完成: {len(result)} 字符")
    return result


def main():
    """主函数"""
    html_file = "/home/zhiping/Projects/Download_paper/captured_data/page_000.html"

    print("=" * 80)
    print("📖 提取Author information")
    print("=" * 80)

    print("\n1️⃣  从HTML中提取Author information...")
    author_info_md = extract_author_information_from_html(html_file)

    if author_info_md:
        print("\n📋 Author information内容（前200字）:")
        print(author_info_md[:200] + "...")
        print("\n✅ 完成！")
    else:
        print("\n❌ 提取失败")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从Nature HTML中提取JSON-LD元数据
包括: 论文基本信息、出版信息、作者信息、机构、关键词、摘要
生成Markdown格式的元数据部分
"""

import json
import re
from pathlib import Path
from bs4 import BeautifulSoup
import sys

sys.path.insert(0, '/home/zhiping/Projects/Download_paper')


def extract_metadata_from_html(html_file: str) -> dict:
    """从HTML中提取JSON-LD元数据"""

    with open(html_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, 'html.parser')

    # 找到JSON-LD脚本标签
    ld_json_script = soup.find('script', {'type': 'application/ld+json'})
    if not ld_json_script:
        print("❌ 未找到JSON-LD元数据")
        return {}

    try:
        metadata = json.loads(ld_json_script.string)
        main_entity = metadata.get('mainEntity', {})
        print(f"✓ 成功解析JSON-LD元数据")
        return main_entity
    except json.JSONDecodeError as e:
        print(f"❌ JSON解析错误: {e}")
        return {}


def format_metadata_to_markdown(metadata: dict) -> str:
    """将元数据转换为Markdown格式（简洁版）"""

    md_parts = []

    # 标题
    headline = metadata.get('headline', '')
    if headline:
        md_parts.append(f"# {headline}\n")

    # 作者信息
    authors = metadata.get('author', [])
    if authors:
        md_parts.append("## Authors\n")
        for author in authors:
            name = author.get('name', '')
            orcid = author.get('url', '')
            email = author.get('email', '')
            affiliations = author.get('affiliation', [])

            if name:
                md_parts.append(f"- **{name}**")
                if orcid:
                    md_parts.append(f"  ORCID: {orcid.split('/')[-1]}")
                if affiliations:
                    for aff in affiliations:
                        aff_address = aff.get('address', {})
                        aff_text = aff_address.get('name', '') if isinstance(aff_address, dict) else ''
                        if aff_text:
                            md_parts.append(f"  {aff_text}")

        # 对应作者
        corresponding = [a for a in authors if a.get('email')]
        if corresponding:
            md_parts.append("\n**Corresponding authors:**")
            for author in corresponding:
                name = author.get('name', '')
                email = author.get('email', '')
                if email:
                    md_parts.append(f"- {email}")
        md_parts.append("")

    # 出版信息
    md_parts.append("## Publication\n")

    doi = metadata.get('sameAs', '')
    if doi:
        doi_short = doi.replace('https://doi.org/', '')
        md_parts.append(f"**DOI:** {doi_short}\n")

    is_part_of = metadata.get('isPartOf', {})
    if is_part_of:
        journal_name = is_part_of.get('name', '')
        if journal_name:
            md_parts.append(f"**Journal:** {journal_name}\n")

    date_published = metadata.get('datePublished', '')
    if date_published:
        year = date_published.split('-')[0]
        md_parts.append(f"**Year:** {year}\n")

    volume = is_part_of.get('volumeNumber', '') if is_part_of else ''
    if volume:
        md_parts.append(f"**Volume:** {volume}\n")

    page_start = metadata.get('pageStart', '')
    page_end = metadata.get('pageEnd', '')
    if page_start and page_end:
        md_parts.append(f"**Pages:** {page_start}-{page_end}\n")

    keywords = metadata.get('keywords', [])
    if keywords:
        md_parts.append(f"**Keywords:** {', '.join(keywords)}\n")

    md_parts.append("\n---\n")

    # 摘要
    description = metadata.get('description', '')
    if description:
        md_parts.append("## Abstract\n")
        md_parts.append(description)
        md_parts.append("\n")

    md_parts.append("\n---\n")

    return "\n".join(md_parts)


def save_metadata_to_file(metadata_md: str, output_file: str):
    """保存元数据到文件"""

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(metadata_md)

    print(f"✓ 已保存到: {output_file}")


def main():
    """主函数"""
    html_file = "/home/zhiping/Projects/Download_paper/captured_data/page_000.html"
    output_file = "/home/zhiping/Projects/Download_paper/captured_data/nature_metadata.md"

    print("=" * 80)
    print("📖 提取论文元数据")
    print("=" * 80)

    print("\n1️⃣  从HTML中提取JSON-LD元数据...")
    metadata = extract_metadata_from_html(html_file)

    if not metadata:
        print("\n❌ 提取失败")
        return

    print("\n2️⃣  转换为Markdown格式...")
    metadata_md = format_metadata_to_markdown(metadata)

    print("\n3️⃣  保存到文件...")
    save_metadata_to_file(metadata_md, output_file)

    print("\n" + "=" * 80)
    print("📊 提取统计")
    print("=" * 80)

    authors_count = len(metadata.get('author', []))
    keywords = metadata.get('keywords', [])
    institutions = set()
    for author in metadata.get('author', []):
        for aff in author.get('affiliation', []):
            aff_address = aff.get('address', {})
            aff_text = aff_address.get('name', '') if isinstance(aff_address, dict) else ''
            if aff_text:
                institutions.add(aff_text)

    print(f"论文标题: {metadata.get('headline', '')[:50]}...")
    print(f"DOI: {metadata.get('sameAs', '').replace('https://doi.org/', '')}")
    print(f"作者数: {authors_count} 人")
    print(f"机构数: {len(institutions)} 个")
    print(f"关键词: {len(keywords)} 个")
    print(f"元数据字符数: {len(metadata_md):,} 字符")
    print(f"\n✅ 完成！")
    print(f"   输出文件: {output_file}")


if __name__ == "__main__":
    main()

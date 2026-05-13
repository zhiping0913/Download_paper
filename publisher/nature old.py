#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Nature期刊论文提取模块
集合所有Nature相关的提取功能
"""

import json
import re
from pathlib import Path
from bs4 import BeautifulSoup


class NatureHandler:
    """Nature期刊论文数据提取处理器"""

    def __init__(self, html_file: str):
        """初始化处理器

        Args:
            html_file: Nature期刊论文HTML文件路径
        """
        self.html_file = html_file
        with open(html_file, 'r', encoding='utf-8') as f:
            self.html_content = f.read()
        self.soup = BeautifulSoup(self.html_content, 'html.parser')

    # ==================== 元数据提取 ====================

    def extract_metadata(self) -> str:
        """提取论文元数据（标题、作者、出版信息等）"""
        ld_json_script = self.soup.find('script', {'type': 'application/ld+json'})
        if not ld_json_script:
            return ""

        try:
            metadata = json.loads(ld_json_script.string)
            main_entity = metadata.get('mainEntity', {})
            return self._format_metadata(main_entity)
        except json.JSONDecodeError:
            return ""

    def _format_metadata(self, metadata: dict) -> str:
        """将元数据格式化为Markdown"""
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

        return "\n".join(md_parts)

    # ==================== 引用提取 ====================

    def extract_references(self) -> str:
        """提取论文引用"""
        ref_section = self.soup.find('ol', {'class': 'c-article-references'})
        if not ref_section:
            return ""

        items = ref_section.find_all('li', {'class': 'c-article-references__item'})
        if not items:
            return ""

        md_parts = ["# References\n"]
        for item in items:
            counter = item.get('data-counter', '')
            text_p = item.find('p', {'class': 'c-article-references__text'})
            doi_link = item.find('a', {'data-doi': True})

            if text_p:
                text = text_p.get_text(strip=True)
                doi = doi_link.get('data-doi', '') if doi_link else ''

                if doi:
                    md_parts.append(f"[{counter}] {text} DOI: {doi}")
                else:
                    md_parts.append(f"[{counter}] {text}")

        return "\n".join(md_parts)

    # ==================== 附加内容提取 ====================

    def extract_data_availability(self) -> str:
        """提取Data availability部分"""
        data_avail_section = self.soup.find('section', {'data-title': 'Data availability'})
        if not data_avail_section:
            return ""

        content_div = data_avail_section.find('div', {'class': 'c-article-section__content'})
        if not content_div:
            return ""

        paragraphs = content_div.find_all('p')
        if not paragraphs:
            return ""

        md_parts = []
        for p in paragraphs:
            text = p.get_text(strip=True)
            if text:
                md_parts.append(text)

        return "\n\n".join(md_parts) if md_parts else ""

    def extract_acknowledgements(self) -> str:
        """提取Acknowledgements部分"""
        ack_section = self.soup.find('section', {'data-title': 'Acknowledgements'})
        if not ack_section:
            return ""

        content_div = ack_section.find('div', {'class': 'c-article-section__content'})
        if not content_div:
            return ""

        paragraphs = content_div.find_all('p')
        if not paragraphs:
            return ""

        md_parts = []
        for p in paragraphs:
            text = p.get_text(strip=True)
            if text:
                md_parts.append(text)

        return "\n\n".join(md_parts) if md_parts else ""

    # ==================== 补充材料提取 ====================

    def extract_extended_data(self) -> str:
        """提取Extended data figures and tables"""
        extended_section = self.soup.find('section', {'data-title': 'Extended data figures and tables'})
        if not extended_section:
            return ""

        items = extended_section.find_all('div', {'class': 'c-article-supplementary__item'})
        if not items:
            return ""

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
                    desc_text = re.sub(r'\s+', ' ', desc_text)

                # 组合内容
                md_parts.append(f"{i}. [{title_text}]({href})")
                if desc_text:
                    md_parts.append(f"\n   {desc_text}\n")

        return "\n".join(md_parts) if md_parts else ""

    def extract_supplementary_information(self) -> str:
        """提取Supplementary information"""
        supp_section = self.soup.find('section', {'data-title': 'Supplementary information'})
        if not supp_section:
            return ""

        content_div = supp_section.find('div', {'class': 'c-article-section__content'})
        if not content_div:
            return ""

        items = content_div.find_all('div', {'class': 'c-article-supplementary__item'})
        if not items:
            return ""

        md_parts = []
        for i, item in enumerate(items, 1):
            title = item.find('a', {'class': 'print-link'})
            if title:
                title_text = title.get_text(strip=True)
                href = title.get('href', '')
                md_parts.append(f"{i}. [{title_text}]({href})")

        return "\n".join(md_parts) if md_parts else ""


def main():
    """测试函数"""
    html_file = "/home/zhiping/Projects/Download_paper/captured_data/page_000.html"
    handler = NatureHandler(html_file)

    print("=" * 80)
    print("🧪 Nature处理器测试")
    print("=" * 80)

    print("\n1. 提取元数据...")
    metadata = handler.extract_metadata()
    print(f"✓ 元数据: {len(metadata)} 字符")

    print("\n2. 提取引用...")
    references = handler.extract_references()
    print(f"✓ 引用: {len(references)} 字符")

    print("\n3. 提取Data availability...")
    data_avail = handler.extract_data_availability()
    print(f"✓ Data availability: {len(data_avail)} 字符")

    print("\n4. 提取Acknowledgements...")
    ack = handler.extract_acknowledgements()
    print(f"✓ Acknowledgements: {len(ack)} 字符")

    print("\n5. 提取Extended data...")
    extended = handler.extract_extended_data()
    print(f"✓ Extended data: {len(extended)} 字符")

    print("\n6. 提取Supplementary information...")
    supp = handler.extract_supplementary_information()
    print(f"✓ Supplementary information: {len(supp)} 字符")

    print("\n✅ 测试完成")


if __name__ == "__main__":
    main()

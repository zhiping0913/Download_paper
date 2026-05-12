#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从Nature HTML中提取并转换Acknowledgements
"""

import re
from pathlib import Path
from bs4 import BeautifulSoup
import sys

sys.path.insert(0, '/home/zhiping/Projects/Download_paper')

from json_to_md_converter import convert_html_to_markdown, cleanup_markdown


def extract_acknowledgements_from_html(html_file: str) -> str:
    """从HTML中提取Acknowledgements并转换为Markdown"""

    with open(html_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, 'html.parser')

    # 找到Acknowledgements部分
    ack_section = soup.find('section', {'data-title': 'Acknowledgements'})
    if not ack_section:
        print("❌ 未找到Acknowledgements部分")
        return ""

    # 找到内容div
    content_div = ack_section.find('div', {'class': 'c-article-section__content'})
    if not content_div:
        print("❌ 未找到Acknowledgements内容")
        return ""

    # 获取所有段落
    paragraphs = content_div.find_all('p')
    if not paragraphs:
        print("❌ 未找到Acknowledgements段落")
        return ""

    print(f"✓ 找到 {len(paragraphs)} 个段落")

    # 转换每个段落
    converted_paras = []
    for p in paragraphs:
        p_html = str(p)

        try:
            # 使用与正文相同的转换流程
            md = convert_html_to_markdown(p_html)
            md = cleanup_markdown(md)
            md = re.sub(r'\s+', ' ', md)
            md = md.strip()

            if md:
                converted_paras.append(md)

        except Exception as e:
            print(f"⚠️  段落转换错误: {str(e)[:50]}")

    if not converted_paras:
        print("❌ 没有有效的段落转换")
        return ""

    # 合并所有段落
    result = "\n\n".join(converted_paras)
    print(f"✓ Acknowledgements转换完成: {len(result)} 字符")

    return result


def main():
    """主函数"""
    html_file = "/home/zhiping/Projects/Download_paper/captured_data/page_000.html"

    print("=" * 80)
    print("📖 提取并转换Acknowledgements")
    print("=" * 80)

    print("\n1️⃣  从HTML中提取Acknowledgements...")
    ack_md = extract_acknowledgements_from_html(html_file)

    if ack_md:
        print("\n📋 Acknowledgements内容:")
        print(ack_md[:100] + "...")
        print("\n✅ 完成！")
    else:
        print("\n❌ 提取失败")


if __name__ == "__main__":
    main()

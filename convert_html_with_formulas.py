#!/usr/bin/env python3
"""
使用complete_paper_extraction.py和json_to_md_converter.py的函数
将Nature HTML转换为高质量Markdown（含公式支持）
"""

import sys
import json
from pathlib import Path
sys.path.insert(0, '/home/zhiping/Projects/Download_paper')

# 导入必要的函数
from json_to_md_converter import (
    convert_html_to_markdown,
    clean_html_body,
    cleanup_markdown
)
from complete_paper_extraction import (
    extract_text_without_math,
    mathml_to_latex_pandoc
)
from bs4 import BeautifulSoup


def extract_nature_html_to_markdown_with_formulas(html_file: str, meta_file: str, output_file: str):
    """
    从Nature HTML提取内容并转换为Markdown（支持公式）
    使用complete_paper_extraction.py和json_to_md_converter.py中的函数
    """

    print("="*80)
    print("🔄 CONVERTING NATURE HTML TO MARKDOWN WITH FORMULAS")
    print("="*80)

    # 1. 读取HTML和Meta文件
    with open(html_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    with open(meta_file, 'r', encoding='utf-8') as f:
        meta_info = json.load(f)

    print("\n✓ 已读取HTML文件和Meta信息")

    # 2. 解析HTML
    soup = BeautifulSoup(html_content, 'html.parser')
    print("✓ 已解析HTML")

    markdown_parts = []

    # 3. 添加标题和出版信息
    title = meta_info.get('citation_title', 'Unknown Title')
    markdown_parts.append(f"# {title}\n\n")

    # 出版信息
    markdown_parts.append("## Publication Information\n\n")
    pub_info = [
        ('Journal', meta_info.get('citation_journal_title', 'N/A')),
        ('DOI', meta_info.get('citation_doi', 'N/A')),
        ('Published', meta_info.get('citation_publication_date', 'N/A')),
        ('Volume', meta_info.get('citation_volume', 'N/A')),
        ('Issue', meta_info.get('citation_issue', 'N/A')),
        ('Pages', f"{meta_info.get('citation_firstpage', 'N/A')}-{meta_info.get('citation_lastpage', 'N/A')}"),
    ]

    for key, value in pub_info:
        if value and value != 'N/A':
            markdown_parts.append(f"- **{key}**: {value}\n")

    markdown_parts.append("\n---\n\n")

    # 4. 添加摘要
    markdown_parts.append("## Abstract\n\n")
    abstract = meta_info.get('dc.description') or meta_info.get('description')
    if abstract:
        # 处理摘要中的公式
        abstract_processed = extract_text_without_math(abstract)
        markdown_parts.append(abstract_processed + "\n\n")

    markdown_parts.append("---\n\n")

    # 5. 提取主要内容
    print("✓ 正在提取文章内容...")
    markdown_parts.append("## Content\n\n")

    # 寻找article或main标签
    article = soup.find('article')
    if not article:
        article = soup.find('main')
    if not article:
        article = soup.find(class_=lambda x: x and 'article' in x.lower())

    if article:
        # 提取所有段落
        paragraphs = article.find_all('p', limit=50)

        for para in paragraphs:
            # 获取段落HTML
            para_html = str(para)

            try:
                # 使用extract_text_without_math处理段落中的公式
                text_with_math = extract_text_without_math(para_html)

                if text_with_math and len(text_with_math) > 20:
                    # 使用convert_html_to_markdown进一步处理
                    md_text = convert_html_to_markdown(para_html)

                    if md_text:
                        # 清理Markdown
                        md_text = cleanup_markdown(md_text)
                        markdown_parts.append(f"{md_text}\n\n")
            except Exception as e:
                print(f"  ⚠️  处理段落时出错: {str(e)[:60]}")
                continue

    print("✓ 已提取文章内容")

    # 6. 提取图表
    print("✓ 正在提取图表...")
    markdown_parts.append("---\n\n")
    markdown_parts.append("## Figures\n\n")

    figures = soup.find_all('figure')
    for idx, fig in enumerate(figures[:15], 1):
        fig_caption = fig.find('figcaption')
        if fig_caption:
            caption_html = str(fig_caption)
            # 处理caption中的公式
            caption_text = extract_text_without_math(caption_html)

            if caption_text:
                caption_text = cleanup_markdown(caption_text)
                markdown_parts.append(f"### Figure {idx}\n\n")
                markdown_parts.append(f"{caption_text[:200]}\n\n")

    print(f"✓ 已提取 {min(len(figures), 15)} 个图表")

    # 7. 添加关键词和版权
    markdown_parts.append("---\n\n")
    markdown_parts.append("## Metadata\n\n")

    keywords = meta_info.get('dc.subject', 'N/A')
    if keywords and keywords != 'N/A':
        markdown_parts.append(f"**Keywords**: {keywords}\n\n")

    copyright_text = meta_info.get('dc.copyright', meta_info.get('prism.copyright', 'N/A'))
    if copyright_text and copyright_text != 'N/A':
        markdown_parts.append(f"**Copyright**: {copyright_text}\n\n")

    pdf_url = meta_info.get('citation_pdf_url', 'N/A')
    if pdf_url and pdf_url != 'N/A':
        markdown_parts.append(f"**PDF**: [{pdf_url}]({pdf_url})\n\n")

    # 8. 最终清理
    print("✓ 正在进行最终清理...")
    final_markdown = "".join(markdown_parts)

    # 应用cleanup_markdown进行最终处理
    final_markdown = cleanup_markdown(final_markdown)

    # 保存到文件
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(final_markdown)

    file_size = len(final_markdown)
    line_count = len(final_markdown.splitlines())

    print(f"\n✅ Markdown已保存: {output_file}")
    print(f"   大小: {file_size} 字符")
    print(f"   行数: {line_count} 行")

    return {
        'output_file': output_file,
        'size': file_size,
        'lines': line_count,
        'title': title
    }


def main():
    """主函数"""

    html_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_page.html"
    meta_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_meta.json"
    output_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_with_formulas.md"

    result = extract_nature_html_to_markdown_with_formulas(html_file, meta_file, output_file)

    print("\n" + "="*80)
    print("📊 转换结果总结")
    print("="*80)
    print(f"标题: {result['title'][:60]}...")
    print(f"输出文件: {result['output_file']}")
    print(f"文件大小: {result['size']} 字符 ({result['lines']} 行)")
    print(f"\n使用的函数:")
    print(f"  - convert_html_to_markdown() from json_to_md_converter.py")
    print(f"  - extract_text_without_math() from complete_paper_extraction.py")
    print(f"  - mathml_to_latex_pandoc() from complete_paper_extraction.py")
    print(f"  - cleanup_markdown() from json_to_md_converter.py")


if __name__ == "__main__":
    main()

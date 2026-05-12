#!/usr/bin/env python3
"""
从Nature HTML页面提取关键内容并转换为高质量Markdown
"""

import json
from pathlib import Path
from bs4 import BeautifulSoup


def extract_nature_content_to_markdown(html_file: str, meta_file: str, output_file: str):
    """
    从Nature HTML提取关键内容，转换为Markdown
    """

    print("="*80)
    print("🔄 CONVERTING NATURE HTML TO MARKDOWN")
    print("="*80)

    # 读取HTML
    with open(html_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    # 读取Meta信息
    with open(meta_file, 'r', encoding='utf-8') as f:
        meta_info = json.load(f)

    # 解析HTML
    soup = BeautifulSoup(html_content, 'html.parser')

    markdown = ""

    # 1. 标题
    title = meta_info.get('citation_title', 'Unknown Title')
    markdown += f"# {title}\n\n"
    print(f"✓ 标题: {title[:60]}...")

    # 2. 出版信息
    markdown += "## Publication Information\n\n"

    publication_info = {
        'Journal': meta_info.get('citation_journal_title', 'N/A'),
        'DOI': meta_info.get('citation_doi', 'N/A'),
        'Published Date': meta_info.get('citation_publication_date', 'N/A'),
        'Online Date': meta_info.get('citation_online_date', 'N/A'),
        'Volume': meta_info.get('citation_volume', 'N/A'),
        'Issue': meta_info.get('citation_issue', 'N/A'),
        'Pages': f"{meta_info.get('citation_firstpage', 'N/A')}-{meta_info.get('citation_lastpage', 'N/A')}",
        'Article Type': meta_info.get('citation_article_type', 'N/A'),
        'PDF URL': meta_info.get('citation_pdf_url', 'N/A'),
    }

    for key, value in publication_info.items():
        if value and value != 'N/A':
            markdown += f"- **{key}**: {value}\n"

    markdown += "\n---\n\n"
    print("✓ 出版信息已添加")

    # 3. 作者信息
    markdown += "## Authors\n\n"

    # 从HTML中提取作者
    author_section = soup.find('section', {'data-test': 'author-list'})
    if not author_section:
        author_section = soup.find(class_=lambda x: x and 'author' in x.lower())

    if author_section:
        author_links = author_section.find_all('a', {'data-test': 'author-link'})
        if author_links:
            for author in author_links:
                author_name = author.get_text(strip=True)
                if author_name:
                    markdown += f"- {author_name}\n"
    else:
        # 使用Meta信息中的作者
        author = meta_info.get('citation_author', 'N/A')
        if author and author != 'N/A':
            markdown += f"- {author}\n"

    markdown += "\n---\n\n"
    print("✓ 作者信息已添加")

    # 4. 摘要
    markdown += "## Abstract\n\n"

    abstract = meta_info.get('dc.description') or meta_info.get('description')
    if abstract:
        markdown += abstract + "\n\n"
        print(f"✓ 摘要: {len(abstract)} 字符")

    markdown += "---\n\n"

    # 5. 主要内容
    markdown += "## Content\n\n"

    # 提取文章主体
    article = soup.find('article')
    if not article:
        article = soup.find('main')
    if not article:
        article = soup.find(class_=lambda x: x and 'article' in x.lower())

    if article:
        # 提取段落
        paragraphs = article.find_all('p', limit=30)
        for para in paragraphs:
            text = para.get_text(strip=True)
            if text and len(text) > 20:  # 过滤掉短文本
                # 移除citation数字
                import re
                text = re.sub(r'\d+', '', text)  # 移除reference数字
                markdown += f"{text}\n\n"

    print("✓ 主要内容已提取")

    # 6. 图表
    markdown += "---\n\n"
    markdown += "## Figures\n\n"

    # 提取figure
    figures = soup.find_all('figure')
    print(f"✓ 检测到 {len(figures)} 个图表")

    for idx, fig in enumerate(figures[:10], 1):  # 限制10个
        fig_caption = fig.find('figcaption')
        if fig_caption:
            caption_text = fig_caption.get_text(strip=True)
            markdown += f"### Figure {idx}\n"
            markdown += f"{caption_text[:150]}\n\n"

    # 7. 关键词（如果有）
    markdown += "---\n\n"
    markdown += "## Keywords\n\n"

    keywords = meta_info.get('dc.subject', 'N/A')
    if keywords and keywords != 'N/A':
        markdown += f"{keywords}\n\n"

    # 8. 版权信息
    markdown += "---\n\n"
    markdown += "## Copyright\n\n"

    copyright_text = meta_info.get('dc.copyright', meta_info.get('prism.copyright', 'N/A'))
    if copyright_text and copyright_text != 'N/A':
        markdown += copyright_text + "\n\n"

    # 保存Markdown
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(markdown)

    file_size = len(markdown)
    line_count = len(markdown.splitlines())

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
    output_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_cleaned.md"

    result = extract_nature_content_to_markdown(html_file, meta_file, output_file)

    print("\n" + "="*80)
    print("📊 转换结果")
    print("="*80)
    print(f"文件: {result['output_file']}")
    print(f"大小: {result['size']} 字符 ({result['lines']} 行)")


if __name__ == "__main__":
    main()

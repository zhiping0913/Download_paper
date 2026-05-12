#!/usr/bin/env python3
"""
按段落分割转换：
1. 从HTML中提取main-content
2. 按 </p> 分割成独立段落
3. 对每段单独转换（避免Pandoc跨段落的换行问题）
4. 每段清除内部换行
5. 重新组合
"""

import sys
import re
from pathlib import Path
sys.path.insert(0, '/home/zhiping/Projects/Download_paper')

from json_to_md_converter import convert_html_to_markdown, cleanup_markdown
from bs4 import BeautifulSoup


def extract_paragraphs_from_html(html_file: str):
    """从HTML中提取main-content里的段落和公式"""

    with open(html_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, 'html.parser')
    # 尝试找到 main-content div（支持多种属性组合）
    main_content_div = soup.find('div', {'class': 'main-content'})

    if not main_content_div:
        # 备选方案：查找 data-nosnippet 属性的版本
        main_content_div = soup.find('div', {'class': 'main-content', 'data-nosnippet': ''})

    if not main_content_div:
        print("❌ 未找到 main-content div")
        return []

    main_content_html = str(main_content_div)

    # 按 </p> 和 </div> 分割段落和公式
    # 但要保留 <p> 标签和 <div class="c-article-equation"> 的内容
    items = []

    # 找到所有 <p>...</p> 配对
    p_pattern = r'<p[^>]*>(.*?)</p>'
    matches = list(re.finditer(p_pattern, main_content_html, re.DOTALL))

    # 找到所有 <div class="c-article-equation">...</div> 配对
    div_pattern = r'<div[^>]*class="c-article-equation"[^>]*>(.*?)</div>\s*</div>'
    div_matches = list(re.finditer(div_pattern, main_content_html, re.DOTALL))

    # 合并并排序（按出现顺序）
    all_items = []
    for match in matches:
        all_items.append((match.start(), 'p', match.group(0)))
    for match in div_matches:
        all_items.append((match.start(), 'div', match.group(0)))

    all_items.sort(key=lambda x: x[0])

    for _, item_type, content in all_items:
        items.append(content)

    print(f"✓ 找到 {len(matches)} 个段落和 {len(div_matches)} 个公式")
    return items


def convert_paragraph(p_html: str) -> str:
    """
    转换单个段落：
    1. 转换为Markdown
    2. 移除内部换行
    3. 清理LaTeX命令
    4. 完全简化引用和HTML属性
    """

    try:
        # 转换
        md = convert_html_to_markdown(p_html)

        # 清理LaTeX命令
        md = cleanup_markdown(md)

        # 移除段落内的换行（但保留段落级的结构）
        # 将多个空格替换为单个空格
        md = re.sub(r'\s+', ' ', md)

        # 清理首尾空格
        md = md.strip()

        return md

    except Exception as e:
        print(f"⚠️  转换错误: {str(e)[:50]}")
        return ""


def convert_by_paragraph(html_file: str, output_file: str):
    """按段落分割转换"""

    print("="*80)
    print("📖 按段落分割转换HTML")
    print("="*80)

    # 1. 提取段落
    print("\n1️⃣  从HTML中提取段落...")
    paragraphs = extract_paragraphs_from_html(html_file)
    print(f"✓ 找到 {len(paragraphs)} 个段落")

    # 2. 对每个段落单独转换
    print("\n2️⃣  逐段转换...")
    converted_paragraphs = []

    for idx, p_html in enumerate(paragraphs, 1):
        md = convert_paragraph(p_html)

        if md:
            converted_paragraphs.append(md)

            if idx <= 3 or idx % 10 == 0:
                print(f"  ✓ 段落 {idx}: {len(md)} 字符")

    print(f"  ✓ 共转换 {len(converted_paragraphs)} 个有效段落")

    # 3. 组合段落
    print("\n3️⃣  组合段落...")
    final_markdown = "\n\n".join(converted_paragraphs)

    # 4. 最后清理
    print("\n4️⃣  最后清理...")

    # 移除过多的空行
    final_markdown = re.sub(r'\n\n\n+', '\n\n', final_markdown)

    # 添加标题
    final_markdown = "## Main\n\n" + final_markdown

    # 5. 保存
    print("\n5️⃣  保存文件...")
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(final_markdown)

    file_size = len(final_markdown)
    line_count = len(final_markdown.splitlines())

    print(f"\n✅ 转换完成")
    print(f"   输出: {output_file}")
    print(f"   大小: {file_size} 字符")
    print(f"   行数: {line_count} 行")

    return {
        'output_file': output_file,
        'size': file_size,
        'lines': line_count,
        'paragraphs': len(converted_paragraphs)
    }


def main():
    """主函数"""

    html_file = "/home/zhiping/Projects/Download_paper/captured_data/page_000.html"
    output_file = "/home/zhiping/Projects/Download_paper/captured_data/nature_main_by_paragraph.md"

    result = convert_by_paragraph(html_file, output_file)

    if result:
        print("\n" + "="*80)
        print("📊 转换结果")
        print("="*80)
        print(f"\n转换方法: 按段落分割，逐段转换")
        print(f"段落数: {result['paragraphs']}")
        print(f"文件大小: {result['size']} 字符")
        print(f"行数: {result['lines']} 行")
        print(f"\n优势:")
        print(f"  ✓ 按原始HTML段落结构转换")
        print(f"  ✓ 避免跨段落的Pandoc换行问题")
        print(f"  ✓ 每段单独清理，保证质量")
        print(f"  ✓ 段落之间只有双换行，清洁简洁")


if __name__ == "__main__":
    main()

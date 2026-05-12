#!/usr/bin/env python3
"""
终极版: 彻底清理HTML，然后再转换为Markdown
在转换前就移除所有HTML标签和属性
"""

import sys
import re
from pathlib import Path
sys.path.insert(0, '/home/zhiping/Projects/Download_paper')

from json_to_md_converter import (
    convert_html_to_markdown,
    cleanup_markdown
)
from bs4 import BeautifulSoup


def extract_text_from_html(html_str: str) -> str:
    """
    从HTML中提取纯文本，移除所有标签和属性
    但保留段落结构
    """

    soup = BeautifulSoup(html_str, 'html.parser')

    # 移除script和style标签
    for tag in soup(['script', 'style', 'svg']):
        tag.decompose()

    # 提取文本
    text = soup.get_text(separator=' ', strip=True)

    return text


def clean_extracted_html(html_str: str) -> str:
    """
    在转换前彻底清理HTML
    """

    # 1. 移除所有HTML属性（但保留标签）
    # 移除 href="..." 以外的所有属性
    html_str = re.sub(r'\s+(?!href=)[a-z\-]+=["\'][^"\']*["\']', '', html_str, flags=re.IGNORECASE)
    html_str = re.sub(r'\s+(?!href=)[a-z\-]+=\S+(?=[\s>])', '', html_str, flags=re.IGNORECASE)

    # 2. 移除特定的HTML类和ID属性
    html_str = re.sub(r'\s+(?:class|id|data-\w+|aria-\w+|test|track)=["\'][^"\']*["\']', '', html_str)

    # 3. 移除figure, figcaption, nav, script, style等非内容标签
    html_str = re.sub(r'<(?:figure|figcaption|nav|script|style|svg|path|use)[^>]*>.*?</(?:figure|figcaption|nav|script|style|svg|path|use)>',
                     '', html_str, flags=re.IGNORECASE | re.DOTALL)

    # 4. 移除图片标签的src属性
    html_str = re.sub(r'<img[^>]*src=["\'][^"\']*["\'][^>]*>', '', html_str, flags=re.IGNORECASE)

    # 5. 保留简单的Markdown友好结构
    # 保留: <p>, <h1-6>, <strong>, <em>, <sup>, <sub>, <a>
    # 移除其他容器标签
    html_str = re.sub(r'<(?:div|section|article|aside|main|header|footer|nav|form|label|span)[^>]*>', '',
                     html_str, flags=re.IGNORECASE)
    html_str = re.sub(r'</(?:div|section|article|aside|main|header|footer|nav|form|label|span)>', '',
                     html_str, flags=re.IGNORECASE)

    # 6. 清理empty tags
    html_str = re.sub(r'<(\w+)>\s*</\1>', '', html_str, flags=re.IGNORECASE)

    # 7. 添加段落间的换行
    html_str = re.sub(r'</p>\s*<p>', '\n\n', html_str, flags=re.IGNORECASE)
    html_str = re.sub(r'</h\d>\s*(?=<)', '\n\n', html_str, flags=re.IGNORECASE)

    return html_str


def convert_main_content_final(html_file: str, output_file: str):
    """
    最终版: 彻底清理HTML后转换
    """

    print("="*80)
    print("🔧 终极版: 彻底清理HTML然后转换为Markdown")
    print("="*80)

    # 1. 读取HTML
    print("\n1️⃣  读取HTML文件...")
    with open(html_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    # 2. 提取main-content div
    print("2️⃣  提取 main-content div...")
    soup = BeautifulSoup(html_content, 'html.parser')
    main_content_div = soup.find('div', {'class': 'main-content', 'data-nosnippet': ''})

    if not main_content_div:
        print("❌ 未找到 main-content div")
        return None

    main_content_html = str(main_content_div)
    print(f"✓ 已提取: {len(main_content_html)} 字节")

    # 3. 彻底清理HTML
    print("\n3️⃣  彻底清理HTML...")
    cleaned_html = clean_extracted_html(main_content_html)
    print(f"✓ 已清理: {len(cleaned_html)} 字节")

    # 4. 转换为Markdown
    print("\n4️⃣  转换为Markdown...")
    markdown_content = convert_html_to_markdown(cleaned_html)
    print(f"✓ 转换完成: {len(markdown_content)} 字符")

    # 5. 清理LaTeX命令
    print("\n5️⃣  清理LaTeX命令...")
    markdown_content = cleanup_markdown(markdown_content)

    # 6. 最后的清理
    print("\n6️⃣  最后的清理...")

    # 移除所有Pandoc特殊语法
    markdown_content = re.sub(r'::: \{[^}]*\}\n', '', markdown_content)
    markdown_content = re.sub(r':::\n', '', markdown_content)

    # 移除所有剩余的HTML属性
    markdown_content = re.sub(r'\{[^}]*(?:#|data-|aria-|test|track)[^}]*\}', '', markdown_content)
    markdown_content = re.sub(r'\s+(?:href|src)=["\'][^"\']*["\']', '', markdown_content)

    # 合并被参考文献破坏的短行
    lines = markdown_content.split('\n')
    merged_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # 如果当前行很短（<60字符）且以^结尾，或者下一行不以#开头
        # 则尝试合并
        if (line.strip() and
            len(line) < 60 and
            not line.strip().startswith('#') and
            i + 1 < len(lines) and
            lines[i + 1].strip() and
            not lines[i + 1].strip().startswith('#')):

            # 检查是否应该合并（中间没有参考文献标记的断裂）
            if not line.rstrip().endswith('^') or line.count('[') == line.count(']'):
                merged_lines.append(line + ' ')
                i += 1
                if i < len(lines):
                    merged_lines.append(lines[i])
                i += 1
                continue

        merged_lines.append(line)
        i += 1

    markdown_content = '\n'.join(merged_lines)

    # 清理多余空行
    markdown_content = re.sub(r'\n\n\n+', '\n\n', markdown_content)

    # 移除行尾空格
    lines = markdown_content.split('\n')
    lines = [line.rstrip() for line in lines]
    markdown_content = '\n'.join(lines)

    # 保存文件
    print("\n7️⃣  保存Markdown文件...")
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(markdown_content)

    file_size = len(markdown_content)
    line_count = len(markdown_content.splitlines())

    print(f"\n✅ Markdown已保存: {output_file}")
    print(f"   大小: {file_size} 字符")
    print(f"   行数: {line_count} 行")

    return {
        'output_file': output_file,
        'size': file_size,
        'lines': line_count,
    }


def main():
    """主函数"""

    html_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_page.html"
    output_file = "/home/zhiping/Projects/Download_paper/nature_page_content/s41567-019-0584-7_main_final.md"

    result = convert_main_content_final(html_file, output_file)

    if result:
        print("\n" + "="*80)
        print("📊 最终转换完成")
        print("="*80)
        print(f"\n输出: {result['output_file']}")
        print(f"大小: {result['size']} 字符 ({result['lines']} 行)")
        print(f"\n✨ 最终清理:")
        print(f"  ✓ 彻底移除所有HTML标签（除了必要的）")
        print(f"  ✓ 移除所有HTML属性")
        print(f"  ✓ 移除所有Pandoc特殊语法")
        print(f"  ✓ 合并被分割的段落行")
        print(f"  ✓ 清理多余换行和空格")


if __name__ == "__main__":
    main()

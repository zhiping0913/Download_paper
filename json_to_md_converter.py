#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
递归分析APS API JSON结构，转换为层级化Markdown
"""

import json
import re
import os
from pathlib import Path
import pypandoc

def clean_html_body(html, klass=None):
    """
    清理HTML body内容
    - 保留citation references的内容，去掉标签
    - 移除多余空白
    """
    # 保留 ref-target 和 multi-ref-content 的内容，移除标签
    html = re.sub(r'<span[^>]*(?:ref-target|multi-ref-content)[^>]*>(.*?)</span>', r'\1', html)
    html = re.sub(r'<button[^>]*>(.*?)</button>', r'\1', html)
    html = re.sub(r'<i>(.*?)</i>', r'*\1*', html)

    # 移除多余HTML标签的属性
    html = re.sub(r'<[^>]*class="[^"]*"[^>]*>', lambda m: '<' + re.sub(r'\s+class="[^"]*"', '', m.group(0))[1:], html)

    return html

def remove_newlines_in_paragraph(text, klass=None, body_type=None):
    """
    对于所有block元素内的换行进行清除，但保留数学环境的结构
    """
    # 提取数学环境（$$...$$）
    math_blocks = []
    def extract_math(match):
        math_blocks.append(match.group(0))
        return f"__MATH_BLOCK_{len(math_blocks)-1}__"

    # 保存数学块
    text = re.sub(r'\$\$[^$]*\$\$', extract_math, text, flags=re.DOTALL)

    # 现在清除所有换行
    text = re.sub(r'\n+', ' ', text)
    text = re.sub(r'\s+', ' ', text)

    # 恢复数学块
    for i, math in enumerate(math_blocks):
        # 清除数学块内的换行，但保留结构
        math_clean = re.sub(r'\n+', ' ', math)
        text = text.replace(f"__MATH_BLOCK_{i}__", math_clean)

    return text.strip()

def convert_html_to_markdown(html_content):
    """
    使用pypandoc将HTML转换为Markdown

    关键处理:
    1. 提取并保存MathJax LaTeX公式
    2. 移除引用链接的title和data属性
    3. 使用Pandoc转换
    4. 恢复LaTeX公式，合并引用
    """
    if not html_content or not html_content.strip():
        return ""

    try:
        # 1. 提取并保存MathJax LaTeX公式（在clean_html_body之前，因为它会移除class属性）
        # <span class="mathjax-tex">\(formula\)</span> → MATHJAX000MATHEND
        mathjax_formulas = []
        def extract_mathjax(match):
            mathjax_formulas.append(match.group(1))  # 保存完整的 \(...\)
            idx = len(mathjax_formulas) - 1
            return f"MATHJAX{idx:03d}MATHEND"

        html_content = re.sub(r'<span[^>]*class="mathjax-tex"[^>]*>(.*?)</span>', extract_mathjax, html_content)

        # 2. 清理HTML
        html_content = clean_html_body(html_content)

        # 3. 处理引用链接：只保留数字，移除title和data属性
        # <a title="..." href="#ref-CR9">9</a> → <a href="#ref-CR9">9</a>
        html_content = re.sub(r'<a[^>]*title="[^"]*"[^>]*>', lambda m: re.sub(r'\s+title="[^"]*"', '', m.group(0)), html_content)
        html_content = re.sub(r'<a[^>]*data-[^>]*>', lambda m: re.sub(r'\s+data-[^=]*="[^"]*"', '', m.group(0)), html_content)

        # 4. 使用pypandoc转换
        md = pypandoc.convert_text(
            html_content,
            'md',
            format='html',
            extra_args=['--wrap=none']
        )

        md = md.strip()

        # 5. 恢复MathJax公式
        for i, formula in enumerate(mathjax_formulas):
            # 从 MATHJAX000MATHEND 替换回原始公式
            placeholder = f"MATHJAX{i:03d}MATHEND"
            md = md.replace(placeholder, formula)

        # 6. 后处理：
        # 合并连续的引用标记
        md = merge_superscript_citations(md)

        # 7. 包装inline LaTeX公式（转换 \(...\) 为 $\(...\)$）
        # 但不要处理 $$...$$ 的块级公式
        md = re.sub(r'(?<!\$)\\\(([^)]+)\\\)(?!\$)', r'$\\\(\1\\\)$', md)

        return md
    except Exception as e:
        print(f"⚠️ pypandoc转换错误: {e}")
        return html_content


def merge_superscript_citations(text):
    """
    处理Pandoc产生的上标引用格式
    ^[9](#ref-CR9),[10](#ref-CR10),[11](#ref-CR11)^ → [9,10,11]
    """
    import re

    # 匹配模式：^[...内容...]^
    def process_superscript_cite(match):
        cite_block = match.group(1)  # 获取 ^...^ 内部的内容

        # 从 [N](#ref-XXX) 模式中提取数字
        citations = re.findall(r'\[(\d+)\]', cite_block)

        if not citations:
            return match.group(0)  # 如果没有找到数字，保留原样

        # 去重并排序
        unique_citations = sorted(set(int(c) for c in citations))

        # 返回合并后的形式
        return '[' + ','.join(str(c) for c in unique_citations) + ']'

    # 处理 ^[...]^ 格式
    result = re.sub(r'\^([^\^]*)\^', process_superscript_cite, text)

    return result

def traverse_json_recursive(data, depth=0, parent_type=None, skip_section_header=False):
    """
    递归遍历JSON结构，生成Markdown
    skip_section_header: 是否跳过 front/back 等section标题
    """
    md_output = []

    if isinstance(data, dict):
        # 处理单个对象

        # 特殊处理图片: 在FIG标记后添加图片引用
        if data.get("type") == "fig":
            fig_id = data.get("id", "")

            # 获取图片标题(caption)
            caption_text = ""
            if "components" in data and isinstance(data["components"], list):
                for component in data["components"]:
                    if component.get("type") == "fig-caption":
                        caption_text = component.get("body", "")
                        break

            # 从caption中提取图片编号 (e.g., "FIG. 1." or "Fig. 1." -> "1")
            fig_match = re.search(r'[Ff][Ii][Gg]\.\s*(\d+)', caption_text)
            if fig_match:
                fig_num = fig_match.group(1)
                # 添加图标记和图片引用
                md_text = convert_html_to_markdown(caption_text)
                md_text = remove_newlines_in_paragraph(md_text, "", "fig-caption")
                md_text = re.sub(r'\[\]\{#[^}]*\}', '', md_text).strip()

                if md_text:
                    # 查找图文本后的位置，插入图片引用
                    # 在第一行(通常是"FIG. X." 或 "Fig. X.")后插入图片
                    lines = md_text.split('\n')
                    if lines and re.search(r'[Ff][Ii][Gg]\.\s*\d+', lines[0]):
                        # 在FIG行后插入空行和图片引用
                        md_output.append(f"{lines[0]}\n\n")
                        md_output.append(f"![Figure {fig_num}](figure_{fig_num}.png)\n\n")
                        # 添加剩余的caption文本
                        if len(lines) > 1:
                            remaining = '\n'.join(lines[1:]).strip()
                            if remaining:
                                md_output.append(f"{remaining}\n\n")
                    else:
                        md_output.append(f"{md_text}\n\n")
            return "".join(md_output)

        # 如果有body，转换它
        if "body" in data and data["body"]:
            klass = data.get("klass", "")
            body_type = data.get("type", "")

            # 转换HTML到Markdown
            md_text = convert_html_to_markdown(data["body"])

            # 移除换行
            md_text = remove_newlines_in_paragraph(md_text, klass, body_type)

            # 过滤掉空标记如 []{#acknowledgements}
            md_text = re.sub(r'\[\]\{#[^}]*\}', '', md_text).strip()

            if md_text:
                # 根据type和klass添加适当的标记
                if body_type == "p" and klass == "article-fulltext-paragraph":
                    md_output.append(f"{md_text}\n\n")
                elif body_type == "h1":
                    md_output.append(f"# {md_text}\n\n")
                elif body_type == "h2":
                    md_output.append(f"## {md_text}\n\n")
                elif body_type == "h3":
                    md_output.append(f"### {md_text}\n\n")
                else:
                    md_output.append(f"{md_text}\n\n")

        # 递归处理嵌套的components
        if "components" in data and isinstance(data["components"], list):
            for component in data["components"]:
                nested_md = traverse_json_recursive(
                    component,
                    depth + 1,
                    parent_type=data.get("type"),
                    skip_section_header=True
                )
                md_output.append(nested_md)

        # 处理其他可能的嵌套结构
        for key, value in data.items():
            if key not in ["body", "components", "id", "type", "klass", "sectioned", "expandable", "media", "style"]:
                if isinstance(value, (dict, list)):
                    nested_md = traverse_json_recursive(value, depth, parent_type, skip_section_header=True)
                    if nested_md.strip():
                        # 跳过 front/back 等section header
                        if key not in ["front", "back"] and not skip_section_header:
                            md_output.append(f"**{key}:**\n")
                        md_output.append(nested_md)

    elif isinstance(data, list):
        # 处理数组
        for item in data:
            item_md = traverse_json_recursive(item, depth, parent_type)
            md_output.append(item_md)

    return "".join(md_output)


def cleanup_markdown(md_content: str) -> str:
    """
    清理Markdown中的不兼容命令和HTML实体 - 跨发布商通用

    处理:
    - \\mspace{...} 命令 (APS LaTeX)
    - \\ensuremath{...} 命令 (LaTeX, KaTeX不支持)
    - \\slash 命令 (LaTeX, KaTeX不支持) -> 转换为 /
    - HTML实体转换为纯文本字符

    Args:
        md_content: Markdown内容

    Returns:
        清理后的Markdown内容
    """
    # 移除 \mspace 命令
    md_content = re.sub(r'\\mspace\{[^}]+\}', '', md_content)

    # 移除不兼容KaTeX的 \ensuremath 命令，保留其中的内容
    # \ensuremath{\propto} -> \propto
    md_content = re.sub(r'\\ensuremath\{([^}]*)\}', r'\1', md_content)

    # 转换不兼容KaTeX的 \slash 命令为 /
    # }}\slash{{ -> }}/{{
    md_content = re.sub(r'\\slash', '/', md_content)

    # 转换HTML实体为纯文本字符
    md_content = md_content.replace('&lt;', '<')
    md_content = md_content.replace('&gt;', '>')
    md_content = md_content.replace('&amp;', '&')
    md_content = md_content.replace('&quot;', '"')
    md_content = md_content.replace('&apos;', "'")

    return md_content


def convert_json_data_to_markdown(data: dict) -> str:
    """
    将JSON数据转换为Markdown文本

    Args:
        data: 从APS API返回的JSON数据对象

    Returns:
        转换后的Markdown文本
    """
    return traverse_json_recursive(data, skip_section_header=False)


def main():
    import sys
    # 文件路径 - 支持命令行参数
    if len(sys.argv) > 1:
        json_file = sys.argv[1]
    else:
        json_file = "1.json"

    json_path = Path("/home/zhiping/Projects/Download_paper/captured_data/test") / json_file
    output_dir = Path("/home/zhiping/Projects/Download_paper/captured_data/test")

    # 生成输出文件名
    output_name = json_file.replace(".json", ".md")
    output_path = output_dir / output_name

    # 创建输出目录
    output_dir.mkdir(parents=True, exist_ok=True)

    print("📖 读取JSON文件...")
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    print("🔄 递归转换JSON结构...")
    md_content = convert_json_data_to_markdown(data)

    # 添加header
    header = ""

    final_content = header + md_content

    print("💾 保存Markdown文件...")
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(final_content)

    print(f"✅ 转换完成！")
    print(f"📁 输出文件: {output_path}")
    print(f"📊 文件大小: {len(final_content)} 字符")
    print(f"📝 行数: {len(final_content.splitlines())} 行")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTML/Markdown 转换工具函数

通用函数：HTML→Markdown转换、LaTeX清理、MathML→LaTeX等。
APS JSON 递归转换已移至 publisher/aps.py。
"""

import re
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

def cleanup_markdown(md_content: str) -> str:
    """
    清理Markdown中的不兼容命令和HTML实体 - 跨发布商通用

    处理:
    - \\mspace{...} 命令 (APS LaTeX)
    - \\ensuremath{...} 命令 (LaTeX, KaTeX不支持)
    - \\slash 命令 (LaTeX, KaTeX不支持) -> 转换为 /
    - HTML引用链接 [N](/path/to/ref#id){...} -> [N]
    - 残留的<div>标签
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

    # 清理HTML引用链接，保留引用数字
    # [N](/articles/path#ref-CR5){#ref-link-...} -> [N]
    md_content = re.sub(r'\[(\d+)\]\([^)]*\)\{[^}]*\}', r'[\1]', md_content)
    # [N](/path#ref) -> [N] (不带{...}后缀的情况)
    md_content = re.sub(r'\[(\d+)\]\([^)]*#[^)]*\)(?!\{)', r'[\1]', md_content)

    # 清理上标引用格式 ^[5],[10],[15]...^ -> [5,10,15...]
    def clean_superscript_refs(match):
        ref_text = match.group(1)
        # 提取所有数字
        numbers = re.findall(r'\[?(\d+)\]?', ref_text)
        # 去重并排序
        unique_nums = sorted(set(int(n) for n in numbers if n))
        return '[' + ','.join(str(n) for n in unique_nums) + ']'

    md_content = re.sub(r'\^\[([^\]]*(?:\],[^\]]*)*)\]\^', clean_superscript_refs, md_content)

    # 移除残留的<div>标签
    # <div>...</div> 或 <div class="...">...</div> 等
    md_content = re.sub(r'</?div[^>]*>', '', md_content)

    # Unescape citation brackets that pandoc escaped: \[1--8\] → [1--8]
    md_content = re.sub(r'\\\[(\d+(?:[,\-\s]+\d+)*)\\\]', r'[\1]', md_content)
    # Unescape nested citation brackets (IOP/others): \[[1],[2]--[4]\] → [1],[2]--[4]
    md_content = re.sub(r'\\\[((?:\[[^\]]*\][,\-–\s]*)+)\\\]', r'[\1]', md_content)

    # Replace \mbox{...} with \text{...} for KaTeX compatibility
    md_content = re.sub(r'\\mbox\{', r'\\text{', md_content)

    # 转换HTML实体为纯文本字符
    md_content = md_content.replace('&lt;', '<')
    md_content = md_content.replace('&gt;', '>')
    md_content = md_content.replace('&amp;', '&')
    md_content = md_content.replace('&quot;', '"')
    md_content = md_content.replace('&apos;', "'")

    return md_content


def mathml_to_latex_pandoc(mathml_html: str) -> str:
    """Convert MathML to LaTeX using pandoc"""
    try:
        html_wrapped = f"<p>{mathml_html}</p>"
        latex_md = pypandoc.convert_text(
            html_wrapped,
            to='gfm',
            format='html',
            extra_args=['--mathjax']
        )
        result = latex_md.strip()
        result = re.sub(r'^<p>(.*)</p>$', r'\1', result, flags=re.DOTALL).strip()
        return result
    except:
        return None



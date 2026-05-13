#!/usr/bin/env python3
"""
完整论文提取脚本 - 统一工作流
从DOI到完整Markdown的端到端解决方案

功能：
1. 连接到已登录Chrome（通过CDP）
2. 提取元数据（作者、单位、摘要等）
3. 监听网络请求捕获原始JSON（包含MathML）
4. 下载高分辨率图片
5. 转换为完整Markdown（公式转为LaTeX）
"""

import json
import asyncio
import re
import requests
import sys
from pathlib import Path
from datetime import datetime
from html import unescape
from playwright.async_api import async_playwright

# 导入转换工具
try:
    import pypandoc
except:
    import subprocess
    subprocess.check_call(['pip', 'install', 'pypandoc', '-q'])
    import pypandoc

# 导入核心模块 (Phase 2 refactoring)
from core import (
    fetch_semanticscholar,
    organize_paper_output,
    save_metadata_json,
    add_equation_numbers,
    mathml_to_latex_pandoc,
    extract_text_without_math
)
from publisher import APSHandler
from publisher.nature import NatureHandler
from publisher.orchestrator import (
    detect_publisher_from_url,
    get_publisher_handler,
    extract_metadata_multi_publisher
)
from publisher.aps import (
    extract_figure_assets_from_fulltext,
    get_supplemental_links,
    extract_supplemental_descriptions,
    extract_metadata_from_page,
    extract_references_from_html
)

# 导入 APS 特定函数
# 注意：这些函数在本文件中定义，但也在 publisher/aps.py 中有对应的接口
# 在未来的重构中，这些函数应该从 publisher/aps.py 导入

OUTPUT_DIR = "captured_data"


# ============================================================================
# Publisher Detection and Handler Factory
# ============================================================================

def detect_publisher(url: str) -> str:
    """
    Detect which publisher based on URL domain or DOI

    Returns: 'aps' | 'nature' | 'unknown'

    Note: This is a wrapper around orchestrator.detect_publisher_from_url
    for backward compatibility. New code should use the orchestrator module.
    """
    return detect_publisher_from_url(url)


def get_publisher_handler_factory(publisher: str):
    """
    Factory function to get appropriate publisher handler

    Note: This is a wrapper around orchestrator.get_publisher_handler
    for backward compatibility. New code should use the orchestrator module.
    """
    return get_publisher_handler(publisher)

# ============================================================================
# Semantic Scholar API 配置
# ============================================================================
S2_API_URL = "https://api.semanticscholar.org/graph/v1/paper/DOI:"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json'
}


# ============================================================================
# 第1部分：元数据提取 - 从 publisher.aps 导入
# ============================================================================

# extract_metadata_from_page 已移至 publisher/aps.py




async def capture_network_data(page, url: str) -> dict:
    """监听网络请求并捕获JSON API响应，同时保存abstract HTML和其他关键响应"""

    captured = {
        'json_responses': [],
        'document': None,
        'timeline': [],
        'abstract_html': None,      # 保存abstract页面HTML
        'fulltext_data': None,      # 保存fulltext JSON（包含文本和Acknowledgements）
        'supplemental_data': None,  # 保存supplemental信息
        'journal_prefix': None,     # 从URL中提取的期刊前缀（prl, pre, pra等）
    }

    async def handle_response(response):
        rtype = response.request.resource_type
        status = response.status
        url_str = response.url
        ts = datetime.now().isoformat()

        captured['timeline'].append({
            'timestamp': ts,
            'type': rtype,
            'status': status,
            'url': url_str,
            'method': response.request.method
        })

        if status == 200:
            print(f"[{status}] {rtype:10s} {url_str[:70]}")

        # 捕获HTML文档（包括abstract页面）
        if rtype == 'document' and status == 200:
            try:
                html = await response.text()

                # 从URL中提取期刊前缀（prl, pre, pra等）
                if 'journals.aps.org/' in url_str and not captured['journal_prefix']:
                    match = re.search(r'journals\.aps\.org/([a-z]+)/', url_str)
                    if match:
                        captured['journal_prefix'] = match.group(1)
                        print(f"  ✓ 识别期刊: {captured['journal_prefix']}")

                # 保存abstract页面HTML用于References提取
                if '/abstract/' in url_str or '/prl/abstract/' in url_str:
                    captured['abstract_html'] = html
                    print(f"  ✓ 保存abstract HTML: {len(html)} 字节")

                # 保存主要HTML文档到文件
                captured['document'] = {
                    'url': url_str,
                    'timestamp': ts,
                    'size': len(html),
                }

                # 保存HTML文件
                html_filename = f"page_{len(captured['json_responses']):03d}.html"
                html_path = Path(OUTPUT_DIR) / html_filename
                with open(html_path, 'w', encoding='utf-8') as f:
                    f.write(html)
                captured['document']['file'] = str(html_path)

                print(f"  ✓ HTML文档: {len(html)} 字节")
                print(f"    保存到: {html_filename}")
            except:
                pass

        # 捕获JSON/API响应
        elif rtype in ('xhr', 'fetch') and status == 200:
            try:
                ctype = response.headers.get('content-type', '')
                if 'json' in ctype.lower():
                    jdata = await response.json()
                    jstr = json.dumps(jdata)

                    kws = ['abstract', 'article', 'fulltext', 'front', 'back']
                    has_paper = any(kw in jstr.lower() for kw in kws)

                    if has_paper or len(jstr) > 2000:
                        print(f"  ✓✓ API数据: {len(jstr)} 字节")

                        jpath = Path(OUTPUT_DIR) / f"api_response_{len(captured['json_responses']):03d}.json"
                        with open(jpath, 'w', encoding='utf-8') as f:
                            json.dump(jdata, f, indent=2, ensure_ascii=False)

                        captured['json_responses'].append({
                            'url': url_str,
                            'timestamp': ts,
                            'size': len(jstr),
                            'file': str(jpath),
                        })

                        # 特别保存fulltext和supplemental数据
                        if '/fulltext/' in url_str:
                            captured['fulltext_data'] = jdata
                            print(f"  ✓ 保存fulltext数据: {len(jstr)} 字节")
                        elif '/supplemental/' in url_str:
                            captured['supplemental_data'] = jdata
                            print(f"  ✓ 保存supplemental数据: {len(jstr)} 字节")
            except:
                pass

    page.on("response", handle_response)

    # 导航到URL
    print(f"📄 访问: {url}")
    print("=" * 80)

    try:
        await page.goto(url, wait_until='networkidle', timeout=60000)
        print("✓ 页面加载完成")
    except Exception as e:
        print(f"⚠️  {type(e).__name__}: {str(e)[:100]}")

    # 等待额外请求
    await asyncio.sleep(3)

    return captured


# ============================================================================
# 第3部分：公式处理（保留）
# ============================================================================

def add_equation_numbers(markdown: str) -> str:
    """为Markdown中的display equations添加编号 (1), (2), etc."""
    # 首先清理display equations中的多余格式问题
    # 处理 },{}$$ -> }$$ (pypandoc转换产生的问题)
    markdown = re.sub(r',\{\}\$\$', '$$', markdown)  # 修复 ,{}$$ -> $$
    markdown = re.sub(r'\}\{\}\$\$', '$$', markdown)  # 修复 }{}$$ -> }$$
    # 处理末尾的 ,} 问题（在}$$之前多余的逗号）
    markdown = re.sub(r',\}\$\$\s*\(', '}$$ (', markdown)  # 修复 ,}$$ ( -> }$$ (

    lines = markdown.split('\n')
    result_lines = []
    eq_counter = 0
    i = 0

    while i < len(lines):
        line = lines[i]

        # 检查是否行包含display equation开始符 $$
        if '$$' in line:
            # 计算这一行中$$的个数
            dollar_count = line.count('$$')

            # 如果有偶数个$$，说明公式开始和结束都在这一行
            if dollar_count >= 2:
                # 公式完整在一行内
                eq_counter += 1
                # 在最后的$$后加编号
                modified_line = line.rstrip()
                if modified_line.endswith('$$'):
                    modified_line = modified_line[:-2] + f'$$ ({eq_counter})'
                result_lines.append(modified_line)
                i += 1
            else:
                # 公式开始但未结束，需要找到结束的$$
                result_lines.append(line)
                j = i + 1
                while j < len(lines):
                    next_line = lines[j]
                    result_lines.append(next_line)
                    if '$$' in next_line:
                        # 找到结束$$
                        eq_counter += 1
                        # 在结束$$后加编号
                        if next_line.rstrip().endswith('$$'):
                            result_lines[-1] = next_line.rstrip()[:-2] + f'$$ ({eq_counter})'
                        elif '$$' in next_line:
                            # 如果$$不在最后但在行中，也要加编号
                            modified_line = next_line.replace('$$', f'$$ ({eq_counter})', 1)
                            result_lines[-1] = modified_line
                        break
                    j += 1
                i = j + 1
        else:
            result_lines.append(line)
            i += 1

    return '\n'.join(result_lines)





def mathml_to_latex_pandoc(mathml_html: str) -> str:
    """MathML转LaTeX"""
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

        # 清理不支持的LaTeX命令
        # 移除 \mspace{...} 命令（KaTeX不支持）
        result = re.sub(r'\\mspace\{[^}]+\}', '', result)

        return result
    except:
        return None


def extract_text_without_math(html_str: str) -> str:
    """提取文本并转换内联公式 - 完整HTML清理"""
    def replace_inline_formula(match):
        math_section = match.group(0)
        math_match = re.search(r'<math[^>]*>.*?</math>', math_section, re.DOTALL)
        if math_match:
            math_html = math_match.group(0)
            latex = mathml_to_latex_pandoc(math_html)
            if latex:
                return latex
        return match.group(0)

    # 1. 处理 <span class="inline-formula"> 中的 MathML
    result = re.sub(
        r'<span class="inline-formula">[^<]*<math[^>]*>.*?</math>[^<]*</span>',
        replace_inline_formula,
        html_str,
        flags=re.DOTALL
    )

    # 2. 处理直接嵌入的 <math> 标签（图片注解中常见）
    def convert_math_tag(match):
        math_html = match.group(0)
        latex = mathml_to_latex_pandoc(math_html)
        if latex:
            return f" {latex} "
        return match.group(0)

    result = re.sub(
        r'<math[^>]*>.*?</math>',
        convert_math_tag,
        result,
        flags=re.DOTALL
    )

    # 3. 完整的HTML标签清理
    result = re.sub(r'<button[^>]*>', '', result, flags=re.DOTALL)
    result = re.sub(r'</button>', '', result, flags=re.DOTALL)
    result = re.sub(r'<a[^>]*>', '', result, flags=re.DOTALL)  # 移除 <a ...>
    result = re.sub(r'</a>', '', result, flags=re.DOTALL)       # 移除 </a>
    result = re.sub(r'<!-- .*? -->', '', result, flags=re.DOTALL)  # 移除HTML注释
    result = re.sub(r'<[hH][123456][^>]*>', '', result)  # 移除 <h1-h6>
    result = re.sub(r'</[hH][123456]>', '', result)
    result = re.sub(r'<span[^>]*>', '', result)
    result = re.sub(r'</span>', '', result)
    result = re.sub(r'<i[^>]*>', '', result)               # 移除 <i ...>
    result = re.sub(r'</i>', '', result)                   # 移除 </i>
    result = re.sub(r'</?[a-zA-Z][^>]*>', '', result)      # 移除所有其他HTML标签

    result = unescape(result)

    # 修复：在公式前后添加空格（避免 "are$" 这样的问题）
    # 如果一个非空白字符后面直接跟 $，或 $ 后面直接跟非空白字符，加空格
    result = re.sub(r'([^\s\$])\$', r'\1 $', result)  # "are$" → "are $"
    result = re.sub(r'\$([^\s\$])', r'$ \1', result)  # "$\Delta" → "$ \Delta"

    result = re.sub(r'\s+', ' ', result).strip()
    return result




# ============================================================================
# 第4部分：图片下载
# ============================================================================


async def download_pdf(page, doi: str, output_dir: Path, journal_prefix: str = None, publisher: str = None) -> str:
    """下载论文PDF - 配合Chrome下载设置"""
    try:
        # 根据出版商构造正确的PDF URL
        if publisher == 'nature':
            # Nature论文：使用DOI直接获取PDF
            pdf_url = f"https://doi.org/{doi}"
        elif publisher == 'aps':
            # APS论文：使用APS特定的PDF格式
            if not journal_prefix:
                journal_prefix = 'prl'  # PhysRevLett默认值
            pdf_url = f"https://journals.aps.org/{journal_prefix}/pdf/{doi}"
        else:
            # 未知发布商：尝试APS格式作为默认值
            if not journal_prefix:
                journal_prefix = 'prl'
            pdf_url = f"https://journals.aps.org/{journal_prefix}/pdf/{doi}"

        print(f"  📥 下载 PDF...")
        print(f"     链接: {pdf_url}")

        # 由于Chrome设置为下载PDF，我们需要监听下载事件
        pdf_filename = None

        async def handle_download(download):
            nonlocal pdf_filename
            # 获取建议的文件名
            pdf_filename = download.suggested_filename
            # 获取下载的文件路径
            pdf_path_temp = await download.path()

            # 复制到输出目录
            final_filename = f"paper.pdf"
            final_path = output_dir / final_filename

            import shutil
            shutil.copy(str(pdf_path_temp), str(final_path))

            pdf_size_mb = final_path.stat().st_size / (1024 * 1024)
            print(f"    ✓ 保存: {final_filename} ({pdf_size_mb:.2f} MB)")
            return final_filename

        # 注册下载监听器
        page.on("download", handle_download)

        # 导航到PDF链接（会自动下载）
        try:
            await page.goto(pdf_url, timeout=15000, wait_until='commit')
        except:
            # 下载开始时页面加载会中断，这是正常的
            pass

        # 等待下载完成
        await asyncio.sleep(3)

        if pdf_filename:
            return "paper.pdf"
        else:
            print(f"    ⚠️  未捕获到下载事件，尝试从Downloads查找...")
            # 降级方案：从Downloads目录查找
            try:
                downloads_dir = Path.home() / "Downloads"
                pdf_files = sorted(downloads_dir.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
                if pdf_files:
                    latest_pdf = pdf_files[0]
                    import time
                    mtime = latest_pdf.stat().st_mtime
                    current_time = time.time()
                    if current_time - mtime < 60:  # 60秒内
                        final_path = output_dir / "paper.pdf"
                        import shutil
                        shutil.copy(str(latest_pdf), str(final_path))
                        pdf_size_mb = final_path.stat().st_size / (1024 * 1024)
                        print(f"    ✓ 从Downloads移动: paper.pdf ({pdf_size_mb:.2f} MB)")
                        return "paper.pdf"
            except:
                pass

        return None

    except Exception as e:
        print(f"    ⚠️  PDF下载失败: {str(e)[:100]}")
        return None


    return None


async def download_supplemental_materials(supplemental_links: list, output_dir: Path, year: str, title: str, context, descriptions: dict = None) -> tuple:
    """在浏览器中打开新标签页下载补充材料文件（保持登录态）

    Args:
        supplemental_links: 补充材料链接列表
        output_dir: 输出目录
        year: 论文年份
        title: 论文标题
        context: Playwright browser context（已enable downloads）
        descriptions: 补充材料的描述字典 {filename: description}

    Returns:
        tuple: (成功下载的文件数量, 下载文件的描述字典 {filename: description})
    """
    if not supplemental_links:
        return 0, {}

    import urllib.parse
    import shutil

    if descriptions is None:
        descriptions = {}

    # 清理标题中的特殊字符
    title_clean = re.sub(r'[/\\:*?"<>|]', '-', title)[:120]
    prefix = f"{year}--{title_clean}"

    downloaded_count = 0
    downloaded_descriptions = {}

    for i, link in enumerate(supplemental_links, 1):
        try:
            url = link.get('url', link.get('href', ''))
            if not url:
                continue

            # 从URL中提取文件名
            parsed_url = urllib.parse.urlparse(url)
            filename = parsed_url.path.split('/')[-1]

            if not filename:
                filename = f"supplemental_{i}"

            # 生成输出文件名
            output_filename = f"{prefix}--Supplemental--{filename}"
            output_path = output_dir / output_filename

            print(f"  📥 下载补充材料 ({i}/{len(supplemental_links)}): {filename}")
            print(f"     URL: {url}")

            # 创建新页面用于下载
            download_page = await context.new_page()

            # 设置下载事件处理
            downloaded_file = None

            async def on_download(download):
                nonlocal downloaded_file
                # 获取下载路径（默认是临时目录）
                downloaded_file = await download.path()

            download_page.on("download", on_download)

            # 导航到链接（会自动触发下载）
            try:
                await download_page.goto(url, timeout=30000, wait_until='commit')
            except:
                # 下载开始时页面加载会中断，这是正常的
                pass

            # 等待下载完成
            await asyncio.sleep(2)

            # 如果捕获到下载，复制文件
            if downloaded_file and Path(downloaded_file).exists():
                try:
                    shutil.copy(str(downloaded_file), str(output_path))
                    file_size_mb = output_path.stat().st_size / (1024 * 1024)
                    print(f"    ✓ 已保存: {output_filename} ({file_size_mb:.2f} MB)")
                    downloaded_count += 1

                    # 记录该文件的描述（如果有）
                    if filename in descriptions:
                        downloaded_descriptions[filename] = descriptions[filename]
                    elif output_filename in descriptions:
                        downloaded_descriptions[output_filename] = descriptions[output_filename]

                except Exception as e:
                    print(f"    ⚠️  复制文件失败: {str(e)[:100]}")
            else:
                print(f"    ⚠️  未捕获到下载事件: {filename}")

            await download_page.close()

        except Exception as e:
            print(f"    ⚠️  处理链接失败: {str(e)[:100]}")

    if downloaded_count > 0:
        print(f"\n  ✓ 成功下载 {downloaded_count} 个补充材料")

    return downloaded_count, downloaded_descriptions



async def download_figure(page, fig_url: str, fig_num: int, output_dir: Path) -> str:
    """下载高分辨率图片 - 使用API响应中的URL"""
    try:
        if not fig_url:
            return None

        # 构建完整URL（如果是相对URL）
        if fig_url.startswith('/'):
            fig_url = f"https://journals.aps.org{fig_url}"

        print(f"  📥 下载 Figure {fig_num}: {fig_url}")

        await page.goto(fig_url, wait_until='networkidle', timeout=30000)
        img_elements = await page.query_selector_all('img')

        if img_elements:
            img_src = await img_elements[0].get_attribute('src')
            if img_src:
                response = await page.goto(img_src, wait_until='networkidle', timeout=30000)
                image_data = await response.body()
                img_filename = f"figure_{fig_num}.png"

                img_path = output_dir / img_filename
                with open(img_path, 'wb') as f:
                    f.write(image_data)
                print(f"    ✓ 保存: {img_filename}")
                return img_filename

    except Exception as e:
        print(f"    ❌ 下载失败: {e}")

    return None


# ============================================================================
# 第5部分：内容处理和Markdown生成
# ============================================================================

def extract_pdf_link_from_html(html_content: str) -> str:
    """
    从abstract页面HTML中提取PDF下载链接
    查找: <a href="/pre/pdf/10.1103/PhysRevE.101.033202" class="sm-primary-button">PDF</a>
    """
    if not html_content:
        return None

    try:
        # 查找PDF链接的href属性
        # 格式: href="/{journal}/pdf/{doi}"
        match = re.search(r'href="(/[a-z]+/pdf/[^"]+)"[^>]*class="[^"]*primary-button', html_content)
        if match:
            pdf_path = match.group(1)
            # 构建完整URL
            pdf_url = f"https://journals.aps.org{pdf_path}"
            return pdf_url

        # 备选方案：尝试查找任何PDF链接
        match = re.search(r'href="(/[a-z]+/pdf/10\.[^"]+)"', html_content)
        if match:
            pdf_path = match.group(1)
            pdf_url = f"https://journals.aps.org{pdf_path}"
            return pdf_url

    except Exception as e:
        print(f"  ⚠️  从HTML提取PDF链接失败: {e}")

    return None


async def json_to_markdown_complete(json_file: str, doi: str, metadata: dict,
                                   page, output_dir: Path, output_file: str = None,
                                   fulltext_data: dict = None, supplemental_html: str = None,
                                   supplemental_links: list = None) -> str:
    """
    使用json_to_md_converter进行递归JSON转换 (v3.0)
    - 正文和Acknowledgements由json_to_md_converter处理
    - 保留标题、作者、发表信息、摘要、图片、References等metadata处理
    """
    from json_to_md_converter import convert_json_data_to_markdown

    with open(json_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    md_content = ""

    # ===== 标题 =====
    title = metadata.get('title') or "Academic Paper"
    md_content += f"# {title}\n\n"

    # ===== 作者 =====
    if metadata.get('author_with_affiliations'):
        md_content += "## Authors\n\n"
        for item in metadata['author_with_affiliations']:
            author = item['author']
            affiliations = item['affiliations']
            md_content += f"- **{author}**\n"
            for aff in affiliations:
                md_content += f"  {aff}\n"
            md_content += "\n"

        # 在作者列表之后显示所有通讯作者邮箱
        if metadata.get('corresponding_author_emails'):
            md_content += "**Corresponding authors:**\n"
            for email in metadata['corresponding_author_emails']:
                if email and 'feedback@aps.org' not in email:
                    md_content += f"- {email}\n"
            md_content += "\n"

        md_content += "\n"

    # ===== 发表信息 =====
    md_content += "## Publication\n\n"
    if metadata.get('journal'):
        md_content += f"**Journal:** {metadata['journal']}\n\n"
    if metadata.get('year'):
        md_content += f"**Year:** {metadata['year']}\n\n"
    if metadata.get('volume'):
        md_content += f"**Volume:** {metadata['volume']}"
        if metadata.get('issue'):
            md_content += f", Issue {metadata['issue']}"
        md_content += "\n\n"
    if metadata.get('pages'):
        md_content += f"**Pages:** {metadata['pages']}\n\n"
    if doi:
        md_content += f"**DOI:** {doi}\n\n"
    md_content += "---\n\n"

    # ===== 摘要 =====
    if metadata.get('abstract'):
        md_content += "## Abstract\n\n"
        md_content += f"{metadata['abstract']}\n\n"
        md_content += "---\n\n"

    # ===== 正文和Acknowledgements - 使用json_to_md_converter递归转换 =====
    md_content += "## Article Text\n\n"
    try:
        article_md = convert_json_data_to_markdown(data)
        md_content += article_md
    except Exception as e:
        print(f"  ⚠️  JSON转换错误: {e}")
        # 降级方案：如果转换失败，使用空内容
        md_content += ""

    md_content += "\n---\n\n"

    # ===== 图片提取和下载 =====
    figure_map = {}  # 追踪图号 -> 文件名的映射
    figure_assets = extract_figure_assets_from_fulltext(fulltext_data)
    if figure_assets:
        for fig_id in sorted(figure_assets.keys(), key=lambda x: int(x[1:]) if x[1:].isdigit() else 0):
            fig_url = figure_assets[fig_id].get('url')
            fig_caption = figure_assets[fig_id].get('caption', '')

            if fig_url:
                # 提取图号（f1 -> 1, f2 -> 2等）
                fig_num = fig_id[1:] if fig_id.startswith('f') else fig_id

                img_filename = await download_figure(page, fig_url, int(fig_num), output_dir)
                if img_filename:
                    figure_map[fig_num] = img_filename  # 记录图片映射

    # 在正文中的"FIG. X."处插入图片链接
    for fig_num, img_filename in figure_map.items():
        # 搜索"FIG. X."（注意X可能是多位数字）
        pattern = rf'FIG\.\s+{re.escape(fig_num)}\.'
        replacement = f'FIG. {fig_num}. ![Figure {fig_num}]({img_filename})'
        md_content = re.sub(pattern, replacement, md_content)

    # ===== 下载链接 =====
    md_content += "## Download Links\n\n"
    pdf_link = extract_pdf_link_from_html(supplemental_html)
    if pdf_link:
        md_content += f"**Paper PDF:** [Download PDF]({pdf_link})\n\n"

    md_content += "---\n\n"

    # ===== References =====
    references = metadata.get('references', [])
    if references and len(references) > 0:
        md_content += "## References\n\n"
        for i, ref in enumerate(references, 1):
            md_content += f"[{i}] {ref}\n\n"

    # 添加公式编号
    md_content = add_equation_numbers(md_content)

    # 清理不被KaTeX支持的LaTeX命令
    md_content = re.sub(r'\\mspace\{[^}]+\}', '', md_content)

    # Save markdown
    if output_file:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(md_content)
        print(f"\n✅ Markdown已保存: {output_file}")

    return md_content


# ============================================================================
# 第6部分：主工作流
# ============================================================================

async def complete_extraction_workflow(doi: str, output_file: str = None):
    """完整提取工作流"""

    output_path = Path(OUTPUT_DIR)
    output_path.mkdir(exist_ok=True)

    # 清理旧的captured_data中的HTML文件
    print("🧹 清理旧的HTML捕获文件...")
    for html_file in output_path.glob('*.html'):
        try:
            html_file.unlink()
            print(f"  删除: {html_file.name}")
        except Exception as e:
            print(f"  ⚠️  无法删除 {html_file.name}: {e}")

    print("\n" + "=" * 80)
    print("🔍 论文完整提取工作流")
    print("=" * 80)
    print(f"📌 DOI: {doi}\n")

    # 构建URL
    url = f"https://doi.org/{doi}"

    # 检查Chrome是否就绪
    def check_chrome_ready():
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex(('127.0.0.1', 9222))
            sock.close()
            return result == 0
        except:
            return False

    if not check_chrome_ready():
        print("⚠️  Chrome 未运行，正在启动...")
        import subprocess
        chrome_launcher = Path(__file__).parent / "chrome_launcher.py"
        if chrome_launcher.exists():
            subprocess.Popen([sys.executable, str(chrome_launcher)])
            # 等待Chrome启动
            for i in range(30):
                await asyncio.sleep(1)
                if check_chrome_ready():
                    print("✓ Chrome 已就绪\n")
                    break
        else:
            print("⚠️  chrome_launcher.py 未找到\n")

    async with async_playwright() as p:
        try:
            # 连接到已登录的Chrome
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
            print("✓ 已连接到Chrome\n")

        except Exception as e:
            print(f"❌ 无法连接到Chrome port 9222: {e}")
            print("   请运行: python chrome_launcher.py\n")
            return False

        try:
            contexts = browser.contexts
            if contexts:
                context = contexts[0]
                print("✓ 使用现有context\n")
            else:
                # 创建context时指定下载目录
                context = await browser.new_context(
                    accept_downloads=True
                )
                print("✓ 创建新context (accept_downloads=True)\n")

            page = await context.new_page()

            # 第1步：获取元数据
            print("Step 1️⃣  提取页面元数据...")
            print("=" * 80)

            # 先访问页面
            try:
                await page.goto(url, wait_until='networkidle', timeout=60000)
            except:
                pass

            # 获取最终 URL（重定向后）
            final_url = page.url
            print(f"✓ 最终 URL: {final_url}")

            # 根据最终 URL 检测出版商
            publisher = detect_publisher_from_url(final_url)
            print(f"✓ 检测出版商: {publisher.upper()}\n")

            metadata = await extract_metadata_from_page(page)

            if metadata['title']:
                print(f"  ✓ 标题: {metadata['title'][:60]}...")
            if metadata['authors']:
                print(f"  ✓ 作者: {len(metadata['authors'])} 位")
                for author in metadata['authors'][:3]:
                    print(f"     - {author}")
            if metadata['journal']:
                print(f"  ✓ 期刊: {metadata['journal']}")
            if metadata['abstract']:
                print(f"  ✓ 摘要: {len(metadata['abstract'])} 字符")

            print()

            # 获取Semantic Scholar元数据
            print("Step 1.5️⃣  获取Semantic Scholar元数据...")
            print("=" * 80)
            s2_data = fetch_semanticscholar(doi)
            print()

            # 从Semantic Scholar合并关键信息到metadata
            if s2_data:
                if s2_data.get('year') and not metadata.get('year'):
                    metadata['year'] = s2_data['year']
                if s2_data.get('title') and not metadata.get('title'):
                    metadata['title'] = s2_data['title']

            # 第2步：监听网络请求
            print("Step 2️⃣  监听网络请求并捕获数据...")
            print("=" * 80)

            # 创建新页面用于监听
            page2 = await context.new_page()
            captured = await capture_network_data(page2, url)

            print(f"\n  ✓ 捕获 {len(captured['json_responses'])} 个JSON响应")

            # 从captured的abstract HTML提取References
            if captured['abstract_html']:
                extracted_refs = extract_references_from_html(captured['abstract_html'])
                if extracted_refs:
                    metadata['references'] = extracted_refs
                    print(f"  ✓ 从HTML提取 {len(extracted_refs)} 条References（包含DOI链接）")
                else:
                    print(f"  ⚠️  HTML中未找到References")
            else:
                print(f"  ⚠️  未捕获abstract HTML")

            print()

            # 第3步：转换为Markdown (使用对应出版商的handler)
            print("Step 3️⃣  转换为Markdown并下载图片...")
            print("=" * 80)

            # 只有APS才需要获取补充材料链接
            supplemental_links = []
            supp_descriptions_from_api = {}

            if publisher == 'aps':
                # 获取补充材料链接
                print("  🔗 获取补充材料链接...")
                supplemental_links, supp_descriptions_from_api = await get_supplemental_links(page2, doi, journal_prefix=captured.get('journal_prefix'))
            elif publisher == 'nature':
                print("  ℹ️  Nature 论文 - 跳过 APS 特定的补充材料查询")
            else:
                print(f"  ℹ️  出版商 {publisher.upper()} - 补充材料查询未实现")

            if captured['json_responses']:
                json_file = captured['json_responses'][0]['file']

                # 确定基础输出目录
                base_output_dir = Path(output_file).parent if output_file else Path.home() / "Downloads"
                base_output_dir = base_output_dir / "papers"
                base_output_dir.mkdir(parents=True, exist_ok=True)

                # 创建组织化的论文目录
                paper_output_dir = organize_paper_output(base_output_dir, metadata, s2_data)

                # 生成新的markdown文件名
                year = s2_data.get('year') or metadata.get('year') or '0000'
                title = s2_data.get('title') or metadata.get('title') or 'paper'
                title_clean = re.sub(r'[/\\:*?"<>|]', '-', title)[:120]
                markdown_filename = f"{year}--{title_clean}.md"
                markdown_file = paper_output_dir / markdown_filename

                # 根据出版商选择相应的处理器
                if publisher == 'aps':
                    print("  📝 使用 APS Handler 生成Markdown...")
                    handler = APSHandler(journal_prefix=captured.get('journal_prefix', 'prl'))
                elif publisher == 'nature':
                    print("  📝 使用 Nature Handler 生成Markdown...")
                    from publisher.nature import NatureHandler
                    html_file = captured.get('document', {}).get('file') if captured.get('document') else None
                    handler = NatureHandler(html_file=html_file)
                else:
                    print(f"  ⚠️  出版商 {publisher.upper()} 的处理器未实现，使用 APS Handler...")
                    handler = APSHandler(journal_prefix=captured.get('journal_prefix', 'prl'))

                # 为每个出版商准备合适的数据
                if publisher == 'aps':
                    # APS: 使用fulltext JSON数据
                    fulltext_data = captured.get('fulltext_data')
                    if not fulltext_data and json_file:
                        try:
                            with open(json_file, 'r', encoding='utf-8') as f:
                                fulltext_data = json.load(f)
                        except:
                            fulltext_data = {}
                    article_data = fulltext_data
                elif publisher == 'nature':
                    # Nature: 使用HTML内容 - 从保存的文件读取
                    article_data = None
                    try:
                        if captured.get('document') and captured['document'].get('file'):
                            with open(captured['document']['file'], 'r', encoding='utf-8') as f:
                                article_data = f.read()
                                if not article_data:
                                    article_data = None
                    except:
                        pass

                    # 如果没有从文件读到，尝试从page获取
                    if not article_data:
                        try:
                            article_data = await page2.content()
                        except:
                            pass
                else:
                    # 其他出版商：尝试JSON
                    fulltext_data = captured.get('fulltext_data', {})
                    if not fulltext_data and json_file:
                        try:
                            with open(json_file, 'r', encoding='utf-8') as f:
                                fulltext_data = json.load(f)
                        except:
                            fulltext_data = {}
                    article_data = fulltext_data

                # 调用handler的convert_to_markdown方法 (不添加图片引用，先后处理)
                md = handler.convert_to_markdown(metadata, article_data, add_figure_refs=False)

                # 保存Markdown文件
                with open(markdown_file, 'w', encoding='utf-8') as f:
                    f.write(md)
                print(f"  ✓ Markdown已保存: {markdown_filename}")

                # 🖼️ 下载图片 (这是之前丢失的步骤!)
                print("  🖼️  下载图片...")
                figure_map = {}  # 追踪图号 -> 文件名的映射

                # 从fulltext_data中提取图片信息（仅APS，Nature论文跳过）
                if publisher == 'aps':
                    try:
                        from publisher.aps import extract_figure_assets_from_fulltext
                        journal_prefix = captured.get('journal_prefix', 'prl')
                        figure_assets = extract_figure_assets_from_fulltext(fulltext_data, journal_prefix=journal_prefix)

                        if figure_assets:
                            print(f"  🖼️  图片: {len(figure_assets)} 个")
                            for fig_id in sorted(figure_assets.keys(), key=lambda x: int(x[1:]) if x[1:].isdigit() else 0):
                                fig_url = figure_assets[fig_id].get('url')

                                if fig_url:
                                    # 提取图号（f1 -> 1, f2 -> 2等）
                                    fig_num = fig_id[1:] if fig_id.startswith('f') else fig_id

                                    try:
                                        img_filename = await download_figure(page2, fig_url, int(fig_num), paper_output_dir)
                                        if img_filename:
                                            figure_map[fig_num] = img_filename  # 记录图片映射
                                    except Exception as e:
                                        print(f"    ⚠️  Figure {fig_num} 下载失败: {e}")
                        else:
                            print(f"  ℹ️  未找到图片信息")
                    except Exception as e:
                        print(f"  ⚠️  图片提取异常: {e}")
                elif publisher == 'nature':
                    print(f"  ℹ️  Nature论文 - 暂不支持自动图片提取")
                else:
                    print(f"  ℹ️  {publisher.upper()}论文 - 暂不支持自动图片提取")

                # 📝 后处理: 如果成功下载了图片，重新生成markdown并添加图片引用
                if figure_map:
                    print(f"  📝 重新生成Markdown并添加图片引用...")
                    md = handler.convert_to_markdown(metadata, fulltext_data, add_figure_refs=True)
                    with open(markdown_file, 'w', encoding='utf-8') as f:
                        f.write(md)
                    print(f"  ✓ Markdown已更新图片引用")
                else:
                    print(f"  ℹ️  未下载图片，Markdown中不包含图片引用")

                # 保存元数据JSON
                save_metadata_json(paper_output_dir, metadata, s2_data, doi)

                # 第4步：下载PDF
                print("\nStep 4️⃣  下载论文PDF...")
                print("=" * 80)
                pdf_filename = await download_pdf(page2, doi, paper_output_dir,
                                                  journal_prefix=captured.get('journal_prefix'),
                                                  publisher=publisher)

                # 如果下载成功，重命名PDF为 {年份}--{标题}.pdf
                if pdf_filename:
                    try:
                        # 获取年份和标题用于重命名
                        year = s2_data.get('year') or metadata.get('year') or '0000'
                        title = s2_data.get('title') or metadata.get('title') or 'paper'
                        title_clean = re.sub(r'[/\\:*?"<>|]', '-', title)[:120]

                        # 旧文件名
                        old_pdf_path = paper_output_dir / pdf_filename
                        # 新文件名
                        new_pdf_filename = f"{year}--{title_clean}.pdf"
                        new_pdf_path = paper_output_dir / new_pdf_filename

                        # 重命名
                        if old_pdf_path.exists():
                            old_pdf_path.rename(new_pdf_path)
                            print(f"\n✅ PDF已重命名: {new_pdf_filename}")
                            pdf_filename = new_pdf_filename
                    except Exception as e:
                        print(f"\n⚠️  PDF重命名失败: {e}")

                # 第5步：下载补充材料
                if supplemental_links:
                    print("\nStep 5️⃣  下载补充材料...")
                    print("=" * 80)
                    year = s2_data.get('year') or metadata.get('year') or '0000'
                    title = s2_data.get('title') or metadata.get('title') or 'paper'
                    title_clean = re.sub(r'[/\\:*?"<>|]', '-', title)[:120]

                    # 提取补充材料的描述信息
                    supp_descriptions = supp_descriptions_from_api or extract_supplemental_descriptions(captured.get('supplemental_data'))

                    downloaded_count, downloaded_descriptions = await download_supplemental_materials(
                        supplemental_links,
                        paper_output_dir,
                        year,
                        title,
                        context,
                        supp_descriptions
                    )

                    # 将补充材料部分添加到markdown
                    if downloaded_count > 0:
                        md += "\n## Supplemental Materials\n\n"
                        for filename, description in downloaded_descriptions.items():
                            # 生成链接
                            full_filename = f"{year}--{title_clean}--Supplemental--{filename}"
                            md += f"**{filename}**: {description}\n\n"
                            md += f"[Download]({full_filename})\n\n"

                        # 保存更新后的markdown（包含补充材料描述）
                        with open(markdown_file, 'w', encoding='utf-8') as f:
                            f.write(md)
                        print(f"✅ Markdown已更新: {markdown_file}")

                    print()

                # 保存完整的元数据JSON（包含PDF和Supplemental文件信息）
                supplemental_file_list = []
                if supplemental_links and downloaded_count > 0:
                    # 收集已下载的补充材料文件名
                    year = s2_data.get('year') or metadata.get('year') or '0000'
                    title = s2_data.get('title') or metadata.get('title') or 'paper'
                    title_clean = re.sub(r'[/\\:*?"<>|]', '-', title)[:120]
                    for filename in downloaded_descriptions.keys():
                        full_filename = f"{year}--{title_clean}--Supplemental--{filename}"
                        supplemental_file_list.append(full_filename)

                save_metadata_json(paper_output_dir, metadata, s2_data, doi, pdf_filename, supplemental_file_list)

                # 统计
                lines = md.split('\n')
                figures = re.findall(r'\[Figure \d+\]', md)
                display_eqs = len(re.findall(r'\$\$', md)) // 2

                print("\n" + "=" * 80)
                print("📊 完成统计")
                print("=" * 80)
                print(f"  📄 Markdown 行数: {len(lines)}")
                print(f"  🖼️  图片: {len(figures)} 个")
                print(f"  📐 Display equations: {display_eqs} 个")
                if pdf_filename:
                    print(f"  📕 PDF: {pdf_filename}")
                if supplemental_links:
                    print(f"  📎 补充材料: {len(supplemental_links)} 个")
                print(f"  💾 输出目录: {paper_output_dir}")
                print()

                # 关闭当前论文的所有页面，保留一个空白页面保持浏览器窗口打开
                print("🧹 清理当前论文的标签页...")
                print("=" * 80)
                pages_to_close = []
                for p in context.pages:
                    try:
                        pages_to_close.append(p)
                    except:
                        pass

                for p in pages_to_close:
                    try:
                        await p.close()
                    except:
                        pass

                # 创建一个新的空白页面，保持浏览器窗口打开
                try:
                    blank_page = await context.new_page()
                    await blank_page.goto("about:blank")
                    print("  ✓ 当前论文的标签页已关闭")
                    print("  ℹ️  浏览器已保留，可继续处理下一篇论文\n")
                except:
                    print("  ✓ 当前论文的标签页已关闭")
                    print("  ⚠️  无法创建空白页面，但浏览器仍保持连接\n")

            # 不关闭浏览器，保持连接以供批处理使用
            # await browser.close()
            return True

        except Exception as e:
            print(f"❌ 错误: {e}")
            import traceback
            traceback.print_exc()

            # 关闭当前论文的页面，保留浏览器连接
            try:
                pages_to_close = []
                for p in context.pages:
                    try:
                        pages_to_close.append(p)
                    except:
                        pass
                for p in pages_to_close:
                    try:
                        await p.close()
                    except:
                        pass
            except:
                pass

            # 不关闭浏览器，保持连接以供批处理使用
            # await browser.close()
            return False


# ============================================================================
# 入口点
# ============================================================================

async def main():
    import sys

    if len(sys.argv) < 2:
        print("""
使用方法：
    python complete_paper_extraction.py <DOI> [输出文件路径]

示例：
    python complete_paper_extraction.py 10.1103/PhysRevLett.109.245005
    python complete_paper_extraction.py 10.1103/PhysRevLett.109.245005 ~/Downloads/paper.md
""")
        sys.exit(1)

    doi = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    success = await complete_extraction_workflow(doi, output_file)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())

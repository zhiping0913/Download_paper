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





# NOTE: capture_network_data has been moved to APSHandler._capture_network_data
# This function is kept for backward compatibility but delegates to the handler
async def capture_network_data(page, url: str) -> dict:
    """Legacy wrapper - use APSHandler._capture_network_data instead

    This function is kept for backward compatibility with existing code.
    New code should use APSHandler.extract_all() instead.
    """
    from publisher.aps import APSHandler
    handler = APSHandler()
    return await handler._capture_network_data(page, url)


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
# Phase 4: Unified Download Manager
# ============================================================================

async def _download_all_resources(page, links: dict, output_dir: Path, context, metadata: dict, doi: str = None) -> dict:
    """Unified download manager for all resources (PDF, figures, supplemental)

    Args:
        page: Playwright page instance
        links: dict with 'pdf_url', 'figure_urls', 'supplemental_urls'
        output_dir: Output directory for downloads
        context: Playwright browser context (for new tabs)
        metadata: Paper metadata (for supplemental material naming)
        doi: DOI for constructing PDF URL if pdf_url not provided

    Returns:
        dict with 'pdf', 'figures', 'supplemental' keys
    """
    downloads = {
        'pdf': None,
        'figures': {},
        'supplemental': []
    }

    # Download PDF
    pdf_url = links.get('pdf_url')
    if pdf_url:
        print("Step 4️⃣  下载论文PDF...")
        print("=" * 80)
        try:
            # Build PDF filename from metadata
            year = metadata.get('year', '0000')
            title = metadata.get('title', 'paper')
            title_clean = re.sub(r'[/\\:*?"<>|]', '-', title)[:120]
            pdf_filename = f"{year}--{title_clean}.pdf"

            pdf_result = await download_pdf(page, pdf_url, output_dir, pdf_filename)
            downloads['pdf'] = pdf_result
        except Exception as e:
            print(f"⚠️  PDF下载失败: {e}")

    # Download figures
    figure_urls = links.get('figure_urls', {})
    if figure_urls:
        print("\n🖼️  下载图片...")
        print(f"  📊 找到 {len(figure_urls)} 个图片")
        for fig_id, fig_info in figure_urls.items():
            try:
                fig_url = fig_info.get('url') if isinstance(fig_info, dict) else fig_info
                # Extract figure number (f1 -> 1, f2 -> 2)
                fig_num = fig_id[1:] if fig_id.startswith('f') else fig_id

                img_filename = await download_figure(page, fig_url, int(fig_num), output_dir)
                if img_filename:
                    downloads['figures'][fig_num] = img_filename
            except Exception as e:
                print(f"⚠️  Figure {fig_id} 下载失败: {e}")
    else:
        print("\n⚠️  未找到图片链接")

    # Download supplemental materials
    supp_urls = links.get('supplemental_urls', [])
    if supp_urls:
        print("\nStep 5️⃣  下载补充材料...")
        print("=" * 80)
        year = metadata.get('year', '0000')
        title = metadata.get('title', 'paper')
        title_clean = re.sub(r'[/\\:*?"<>|]', '-', title)[:120]

        supp_descriptions = links.get('supplemental_descriptions', {})

        count, descriptions = await download_supplemental_materials(
            supp_urls, output_dir, year, title, context, supp_descriptions
        )
        downloads['supplemental'] = list(descriptions.keys())

    return downloads


# ============================================================================
# Phase 4-5: Simplified Workflow
# ============================================================================


async def download_pdf(page, pdf_url: str, output_dir: Path, filename: str = "paper.pdf") -> str:
    """下载论文PDF

    Args:
        page: Playwright page instance
        pdf_url: 完整的PDF URL
        output_dir: 输出目录
        filename: 保存的文件名 (默认: paper.pdf)

    Returns:
        保存的文件名，或None如果下载失败
    """
    if not pdf_url:
        print("❌ PDF URL为空")
        return None

    try:
        print(f"  📥 下载 PDF...")
        print(f"     链接: {pdf_url}")

        pdf_downloaded = False

        async def handle_download(download):
            """处理下载事件"""
            nonlocal pdf_downloaded
            pdf_path_temp = await download.path()
            final_path = output_dir / filename

            import shutil
            shutil.copy(str(pdf_path_temp), str(final_path))

            pdf_size_mb = final_path.stat().st_size / (1024 * 1024)
            print(f"    ✓ 保存: {filename} ({pdf_size_mb:.2f} MB)")
            pdf_downloaded = True

        page.on("download", handle_download)

        try:
            await page.goto(pdf_url, timeout=15000, wait_until='commit')
        except:
            # 下载开始时页面加载会中断，这是正常的
            pass

        # 等待下载完成
        await asyncio.sleep(3)

        page.remove_listener("download", handle_download)

        if pdf_downloaded:
            return filename
        else:
            print(f"    ⚠️  未成功下载PDF")
            return None

    except Exception as e:
        print(f"    ❌ 下载失败: {e}")
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


# ============================================================================
# 第6部分：主工作流
# ============================================================================

async def complete_extraction_workflow(doi: str, output_file: str = None):
    """完整提取工作流 - Phase 4/5 重构版本

    New architecture:
    1. Connect to Chrome
    2. Navigate to DOI
    3. Detect publisher
    4. Use handler's extract_all() to get all metadata and links in one go
    5. Download all resources using unified _download_all_resources
    6. Save everything
    """

    output_path = Path(OUTPUT_DIR)
    output_path.mkdir(exist_ok=True)

    print("\n" + "=" * 80)
    print("🔍 论文完整提取工作流 (Phase 4-5)")
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
            for i in range(30):
                await asyncio.sleep(1)
                if check_chrome_ready():
                    print("✓ Chrome 已就绪\n")
                    break
        else:
            print("⚠️  chrome_launcher.py 未找到\n")

    async with async_playwright() as p:
        try:
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
                context = await browser.new_context(accept_downloads=True)
                print("✓ 创建新context (accept_downloads=True)\n")

            page = await context.new_page()

            # Step 1: Navigate and detect publisher
            print("Step 1️⃣  导航到DOI并检测出版商...")
            print("=" * 80)
            try:
                await page.goto(url, wait_until='networkidle', timeout=60000)
            except:
                pass

            final_url = page.url
            print(f"✓ 最终 URL: {final_url}")

            publisher = detect_publisher_from_url(final_url)
            print(f"✓ 检测出版商: {publisher.upper()}\n")

            # Get Semantic Scholar metadata early
            print("Step 1.5️⃣  获取Semantic Scholar元数据...")
            print("=" * 80)
            s2_data = fetch_semanticscholar(doi)
            print()

            # Step 2: Use handler's extract_all for complete extraction
            if publisher == 'aps':
                print("Step 2️⃣  使用APSHandler完整提取...")
                print("=" * 80)
                handler = APSHandler(journal_prefix='prl')
                extraction_result = await handler.extract_all(page, doi)

                metadata = extraction_result['metadata']
                links = extraction_result['links']
                fulltext_data = extraction_result['fulltext_data']
                journal_prefix = extraction_result['journal_prefix']

                # Merge with Semantic Scholar data
                if s2_data:
                    if s2_data.get('year') and not metadata.get('year'):
                        metadata['year'] = s2_data['year']
                    if s2_data.get('title') and not metadata.get('title'):
                        metadata['title'] = s2_data['title']

                print(f"  ✓ 标题: {metadata.get('title', 'N/A')[:60]}...")
                print(f"  ✓ 作者: {len(metadata.get('authors', []))} 位")
                print(f"  ✓ 期刊: {metadata.get('journal', 'N/A')}")
                print(f"  ✓ 图片: {len(links.get('figure_urls', {}))} 个")
                print(f"  ✓ 补充材料: {len(links.get('supplemental_urls', []))} 个")
                print()

                # Prepare output directory
                base_output_dir = Path(output_file).parent if output_file else Path.home() / "Downloads"
                base_output_dir = base_output_dir / "papers"
                base_output_dir.mkdir(parents=True, exist_ok=True)
                paper_output_dir = organize_paper_output(base_output_dir, metadata, s2_data)

                # Generate markdown filename
                year = s2_data.get('year') or metadata.get('year') or '0000'
                title = s2_data.get('title') or metadata.get('title') or 'paper'
                title_clean = re.sub(r'[/\\:*?"<>|]', '-', title)[:120]
                markdown_filename = f"{year}--{title_clean}.md"
                markdown_file = paper_output_dir / markdown_filename

                # Step 3: Download all resources
                downloads = await _download_all_resources(page, links, paper_output_dir, context, metadata, doi)

                # Step 3.5: Generate markdown with figures
                print("\nStep 3.5️⃣  生成Markdown...")
                print("=" * 80)
                md = handler.convert_to_markdown(metadata, fulltext_data, add_figure_refs=bool(downloads['figures']))
                with open(markdown_file, 'w', encoding='utf-8') as f:
                    f.write(md)
                print(f"  ✓ Markdown已保存: {markdown_filename}")

                # Save metadata
                save_metadata_json(paper_output_dir, metadata, s2_data, doi,
                                 downloads['pdf'], downloads['supplemental'])

                # Statistics
                print("\n" + "=" * 80)
                print("📊 完成统计")
                print("=" * 80)
                lines = md.split('\n')
                figures = re.findall(r'\[Figure \d+\]', md)
                display_eqs = len(re.findall(r'\$\$', md)) // 2
                print(f"  📄 Markdown 行数: {len(lines)}")
                print(f"  🖼️  图片: {len(figures)} 个")
                print(f"  📐 Display equations: {display_eqs} 个")
                if downloads['pdf']:
                    print(f"  📕 PDF: {downloads['pdf']}")
                if downloads['supplemental']:
                    print(f"  📎 补充材料: {len(downloads['supplemental'])} 个")
                print(f"  💾 输出目录: {paper_output_dir}")
                print()

            else:
                # For non-APS publishers (Nature, etc.)
                print(f"Step 2️⃣  使用{publisher.upper()}Handler完整提取...")
                print("=" * 80)

                if publisher == 'nature':
                    from publisher.nature import NatureHandler
                    handler = NatureHandler()
                    extraction_result = await handler.extract_all(page, doi)

                    metadata = extraction_result['metadata']
                    links = extraction_result['links']
                    fulltext_data = extraction_result['fulltext_data']
                    journal_name = extraction_result.get('journal_name', 'nature')

                    # Save HTML to captured_data
                    if fulltext_data:
                        output_dir = Path("captured_data") / doi.replace('/', '_')
                        output_dir.mkdir(parents=True, exist_ok=True)
                        html_file = output_dir / "page.html"
                        with open(html_file, 'w', encoding='utf-8') as f:
                            f.write(fulltext_data)
                        print(f"  ✓ HTML已保存: {html_file}")

                    # Merge with Semantic Scholar data
                    if s2_data:
                        if s2_data.get('year') and not metadata.get('year'):
                            metadata['year'] = s2_data['year']
                        if s2_data.get('title') and not metadata.get('title'):
                            metadata['title'] = s2_data['title']

                    print(f"  ✓ 标题: {metadata.get('title', 'N/A')[:60]}...")
                    print(f"  ✓ 作者: {len(metadata.get('authors', []))} 位")
                    print(f"  ✓ 期刊: {metadata.get('journal', 'N/A')}")
                    print(f"  ✓ 图片: {len(links.get('figure_urls', {}))} 个")
                    print(f"  ✓ 补充材料: {len(links.get('supplemental_urls', []))} 个")
                    print()

                    # Prepare output directory
                    base_output_dir = Path(OUTPUT_DIR)
                    base_output_dir.mkdir(exist_ok=True)
                    paper_output_dir = organize_paper_output(base_output_dir, metadata, s2_data)

                    # Generate markdown filename
                    year = s2_data.get('year') or metadata.get('year') or '0000'
                    title = s2_data.get('title') or metadata.get('title') or 'paper'
                    title_clean = re.sub(r'[/\\:*?"<>|]', '-', title)[:120]
                    markdown_filename = f"{year}--{title_clean}.md"
                    markdown_file = paper_output_dir / markdown_filename

                    # Step 3: Download all resources
                    downloads = await _download_all_resources(page, links, paper_output_dir, context, metadata, doi)

                    # Step 3.5: Generate markdown with figures
                    print("\nStep 3.5️⃣  生成Markdown...")
                    print("=" * 80)
                    md = handler.convert_to_markdown(metadata, fulltext_data, add_figure_refs=bool(downloads['figures']))
                    with open(markdown_file, 'w', encoding='utf-8') as f:
                        f.write(md)
                    print(f"  ✓ Markdown已保存: {markdown_filename}")

                    # Save metadata
                    save_metadata_json(paper_output_dir, metadata, s2_data, doi,
                                     downloads['pdf'], downloads['supplemental'])

                    # Statistics
                    print("\n" + "=" * 80)
                    print("📊 完成统计")
                    print("=" * 80)
                    lines = md.split('\n')
                    figures = re.findall(r'\[Figure \d+\]', md)
                    display_eqs = len(re.findall(r'\$\$', md)) // 2
                    print(f"  📄 Markdown 行数: {len(lines)}")
                    print(f"  🖼️  图片: {len(figures)} 个")
                    print(f"  📐 Display equations: {display_eqs} 个")
                    if downloads['pdf']:
                        print(f"  📕 PDF: {downloads['pdf']}")
                    if downloads['supplemental']:
                        print(f"  📎 补充材料: {len(downloads['supplemental'])} 个")
                    print(f"  💾 输出目录: {paper_output_dir}")
                    print()

                else:
                    # Other publishers - use Semantic Scholar metadata only
                    print("Step 2️⃣  使用Semantic Scholar元数据...")
                    print("=" * 80)

                    metadata = s2_data or {
                        'title': 'Unknown Paper',
                        'authors': [],
                        'journal': 'Unknown Journal',
                        'year': None
                    }
                    links = {
                        'pdf_url': None,
                        'figure_urls': {},
                        'supplemental_urls': []
                    }
                    fulltext_data = None

                    print(f"  ✓ 标题: {metadata.get('title', 'N/A')[:60]}...")
                    print(f"  ✓ 作者: {len(metadata.get('authors', []))} 位")
                    print(f"  ✓ 期刊: {metadata.get('journal', 'N/A')}")
                    print(f"  ✓ DOI: {doi}")
                    print()

            # Clean up pages
            print("\n🧹 清理标签页...")
            print("=" * 80)
            for p in context.pages:
                try:
                    await p.close()
                except:
                    pass

            try:
                blank_page = await context.new_page()
                await blank_page.goto("about:blank")
                print("  ✓ 标签页已清理")
            except:
                pass

            print()
            return True

        except Exception as e:
            print(f"❌ 错误: {e}")
            import traceback
            traceback.print_exc()

            try:
                for p in context.pages:
                    try:
                        await p.close()
                    except:
                        pass
            except:
                pass

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

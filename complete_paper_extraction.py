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
import magic
import os
import random
import re
import requests
import sys
from pathlib import Path
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse
from playwright.async_api import async_playwright

# 导入核心模块 (Phase 2 refactoring)
from core import (
    fetch_crossref,
    fetch_semanticscholar,
    organize_paper_output,
    save_metadata_json
)
from publisher.orchestrator import (
    detect_publisher_from_url,
    get_publisher_handler,
    extract_metadata_multi_publisher
)

from config import OUTPUT_DIR_DEFAULT, BATCH_SLEEP_ENABLED, BATCH_SLEEP_MIN, BATCH_SLEEP_MAX, SAVE_WITHOUT_REFERENCES

OUTPUT_DIR = OUTPUT_DIR_DEFAULT
# Publisher IDs that can be entered from the Phase 0 headless page.
HEADLESS_ACCESSIBLE_PUBLISHERS = ['nature', 'aip', 'cambridge', 'springer', 'springer_book']
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.tif', '.tiff', '.svg'}

MIME_TO_EXT = {
    'application/pdf': '.pdf',
    'application/zip': '.zip',
    'application/gzip': '.gz',
    'application/x-gzip': '.gz',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'application/msword': '.doc',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
    'application/vnd.ms-excel': '.xls',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
    'application/vnd.ms-powerpoint': '.ppt',
    'text/csv': '.csv',
    'text/plain': '.txt',
    'video/mp4': '.mp4',
    'video/mpeg': '.mpeg',
    'video/quicktime': '.mov',
    'video/x-msvideo': '.avi',
    'video/x-matroska': '.mkv',
    'video/webm': '.webm',
    'audio/mpeg': '.mp3',
    'audio/mp4': '.m4a',
    'audio/wav': '.wav',
    'audio/ogg': '.ogg',
}


def _detect_and_rename(filepath: Path) -> Path:
    """Detect file type from header bytes and rename with correct extension."""
    mime = magic.from_file(str(filepath), mime=True)
    ext = MIME_TO_EXT.get(mime, '')
    if not ext or filepath.suffix == ext:
        return filepath
    new_path = filepath.with_suffix(filepath.suffix + ext)
    filepath.rename(new_path)
    return new_path


def is_bot_challenge_page(url: str, html: str = None) -> bool:
    """Detect whether the current page is an anti-bot challenge rather than a real article.

    Checks URL patterns and page content for common bot-detection / CAPTCHA indicators.
    """
    if url:
        url_lower = url.lower()
        challenge_domains = [
            'validate.perfdrive.com',
            'distilnetworks.com',
            'distilidentify.com',
            'captcha',
            'challenge',
            'accessdenied',
            'blocked',
        ]
        for pattern in challenge_domains:
            if pattern in url_lower:
                return True

    if html:
        html_lower = html.lower()
        challenge_markers = [
            'bot manager',
            'request unsuccessful',
            'are you a bot',
            'verify you are human',
            'please verify',
            'security check',
            'ddos protection',
            'incident id',
            'radware',
            'perfdrive',
            # Cloudflare
            'cloudflare',
            'turnstile',
            'ray id',
            'cdn-cgi/challenge-platform',
            '正在进行安全验证',
            'security verification',
            'enable javascript and cookies to continue',
        ]
        marker_count = sum(1 for m in challenge_markers if m in html_lower)
        if marker_count >= 2:
            return True
        html_size = len(html)
        if html_size < 5000 and any(
            m in html_lower for m in ['verify', 'challenge', 'captcha', 'robot', 'bot']
        ):
            return True

    return False


HEADLESS_AUTH_STATE_FILE = Path(
    os.environ.get(
        "DOWNLOAD_PAPER_HEADLESS_AUTH_STATE",
        Path(__file__).resolve().parent / ".auth" / "headless_storage_state.json",
    )
).expanduser()


# ============================================================================
# Publisher Detection and Handler Factory
# ============================================================================

def detect_publisher(url: str) -> str:
    """
    Detect which publisher based on URL domain or DOI

    Returns: 'aps' | 'nature' | 'aip' | 'unknown'

    Note: This is a wrapper around orchestrator.detect_publisher_from_url
    for backward compatibility. New code should use the orchestrator module.
    """
    return detect_publisher_from_url(url)


def get_publisher_handler_factory(publisher: str, **kwargs):
    """
    Factory function to get appropriate publisher handler

    Note: This is a wrapper around orchestrator.get_publisher_handler
    for backward compatibility. New code should use the orchestrator module.
    """
    return get_publisher_handler(publisher, **kwargs)


def fetch_metadata_with_priority(doi: str) -> dict:
    """Fetch paper metadata with priority: Crossref → Semantic Scholar

    Attempts to fetch metadata from Crossref first (richer data: publisher, ISBN, references),
    then falls back to Semantic Scholar if Crossref fails.

    Args:
        doi: Digital Object Identifier (without 'https://doi.org/' prefix)

    Returns:
        dict with metadata from whichever source succeeds
    """
    print("\n🔍 获取论文元数据...")
    print("=" * 80)

    # Try Crossref first (primary source - has publisher, date, ISBN, references)
    print("  → 尝试 Crossref (优先)...")
    crossref_data = fetch_crossref(doi)

    if crossref_data and crossref_data.get('title'):
        print("  ✓ 使用 Crossref 数据\n")
        return crossref_data

    # Fallback to Semantic Scholar
    print("  → Crossref 未获取到数据，尝试 Semantic Scholar (备用)...")
    s2_data = fetch_semanticscholar(doi)

    if s2_data and s2_data.get('title'):
        print("  ✓ 使用 Semantic Scholar 数据\n")
        return s2_data

    print("  ⚠️  两个来源都未获取到元数据\n")
    return {}


# ============================================================================
# Semantic Scholar API 配置
# ============================================================================
S2_API_URL = "https://api.semanticscholar.org/graph/v1/paper/DOI:"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json'
}



def normalize_image_url(image_url: str, base_url: str = None) -> str:
    """Normalize protocol-relative and relative image URLs."""
    if not image_url:
        return image_url

    image_url = image_url.strip()
    if image_url.startswith('//'):
        return f"https:{image_url}"
    if image_url.startswith('https://www.nature.com//'):
        return image_url.replace('https://www.nature.com//', 'https://', 1)
    if base_url and not image_url.startswith(('http://', 'https://', 'data:')):
        return urljoin(base_url, image_url)
    return image_url


def original_image_filename(image_url: str, fig_num: int, default_ext: str = '.png') -> str:
    """Return a safe local filename based on the source image URL basename."""
    if image_url:
        parsed = urlparse(image_url)
        basename = Path(unquote(parsed.path or '')).name
        basename = re.sub(r'[/\\:*?"<>|\x00-\x1f]', '-', basename).strip().strip('.')

        if basename and Path(basename).suffix.lower() in IMAGE_EXTENSIONS:
            if len(basename) > 180:
                suffix = Path(basename).suffix
                basename = f"{Path(basename).stem[:180 - len(suffix)]}{suffix}"
            return basename

    return f"figure_{fig_num}{default_ext}"


# ============================================================================
# Phase 4: Unified Download Manager
# ============================================================================

async def _download_all_resources(
    page,
    links: dict,
    output_dir: Path,
    context,
    metadata: dict,
    doi: str = None,
    force_headed: bool = False,
) -> dict:
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

    download_playwright = None
    download_browser = None
    download_context = context
    download_page = page

    if not force_headed:
        download_playwright = await async_playwright().start()
        download_browser = await download_playwright.chromium.launch(headless=True)
        download_context = await download_browser.new_context(accept_downloads=True)
        download_page = await download_context.new_page()
        print("  ✓ 使用无头浏览器执行资源下载")

    try:
        # Download PDF
        pdf_url = links.get('pdf_url')
        if pdf_url:
            print("Step 4️⃣  下载论文PDF...")
            print("=" * 80)
            try:
                pdf_filename = "paper.pdf"

                pdf_result = await download_pdf(
                    download_page,
                    pdf_url,
                    output_dir,
                    pdf_filename,
                    download_context,
                    force_headed,
                )
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
                    fig_match = re.search(r'(\d+)$', str(fig_id))
                    fig_num = fig_match.group(1) if fig_match else str(fig_id)

                    img_filename = await download_figure(
                        download_page,
                        fig_url,
                        int(fig_num),
                        output_dir,
                        download_context,
                        force_headed,
                    )
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
            supp_descriptions = links.get('supplemental_descriptions', {})

            count, descriptions = await download_supplemental_materials(
                supp_urls,
                output_dir,
                download_context,
                supp_descriptions,
                download_page,
                force_headed,
            )
            downloads['supplemental'] = list(descriptions.keys())

    finally:
        if download_page is not page:
            try:
                await download_page.close()
            except:
                pass
        if download_browser is not None:
            try:
                await download_browser.close()
            except:
                pass
        if download_playwright is not None:
            try:
                await download_playwright.stop()
            except:
                pass

    return downloads


# ============================================================================
# Phase 4-5: Simplified Workflow
# ============================================================================


async def download_pdf(
    page,
    pdf_url: str,
    output_dir: Path,
    filename: str = "paper.pdf",
    context=None,
    force_headed: bool = False,
) -> str:
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

        download_page = await context.new_page() if force_headed and context is not None else page
        download_page.on("download", handle_download)

        try:
            await download_page.goto(pdf_url, timeout=15000, wait_until='commit')
        except:
            # 下载开始时页面加载会中断，这是正常的
            pass

        # 等待下载完成
        await asyncio.sleep(3)

        download_page.remove_listener("download", handle_download)
        if download_page is not page:
            await download_page.close()

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


async def download_supplemental_materials(
    supplemental_links: list,
    output_dir: Path,
    context,
    descriptions: dict = None,
    page=None,
    force_headed: bool = False,
) -> tuple:
    """在浏览器中打开新标签页下载补充材料文件（保持登录态）

    Args:
        supplemental_links: 补充材料链接列表
        output_dir: 输出目录
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

    downloaded_count = 0
    downloaded_descriptions = {}

    for i, link in enumerate(supplemental_links, 1):
        try:
            url = link if isinstance(link, str) else link.get('url', link.get('href', ''))
            if not url:
                continue

            # 从URL中提取文件名
            parsed_url = urllib.parse.urlparse(url)
            filename = urllib.parse.unquote(parsed_url.path.split('/')[-1])

            if not filename:
                filename = f"supplemental_{i}"

            # 生成输出文件名
            output_filename = f"supplemental--{filename}"
            output_path = output_dir / output_filename

            print(f"  📥 下载补充材料 ({i}/{len(supplemental_links)}): {filename}")
            print(f"     URL: {url}")

            # force-headed mode avoids navigating the article tab.
            download_page = await context.new_page() if force_headed or page is None else page

            # 设置下载事件处理
            downloaded_file = None

            async def on_download(download):
                nonlocal downloaded_file
                # 获取下载路径（默认是临时目录）
                downloaded_file = await download.path()

            download_page.on("download", on_download)

            # 导航到链接（会自动触发下载）
            response = None
            try:
                response = await download_page.goto(url, timeout=30000, wait_until='commit')
            except:
                # 下载开始时页面加载会中断，这是正常的
                pass

            # 等待下载完成
            await asyncio.sleep(2)

            # 如果捕获到下载，复制文件
            if downloaded_file and Path(downloaded_file).exists():
                try:
                    shutil.copy(str(downloaded_file), str(output_path))
                    output_path = _detect_and_rename(output_path)
                    file_size_mb = output_path.stat().st_size / (1024 * 1024)
                    print(f"    ✓ 已保存: {output_path.name} ({file_size_mb:.2f} MB)")
                    downloaded_count += 1

                    # 记录该文件的描述（如果有）
                    saved_name = output_path.name
                    if filename in descriptions:
                        downloaded_descriptions[filename] = descriptions[filename]
                    elif saved_name in descriptions:
                        downloaded_descriptions[saved_name] = descriptions[saved_name]

                except Exception as e:
                    print(f"    ⚠️  复制文件失败: {str(e)[:100]}")
            elif response:
                try:
                    content_type = response.headers.get('content-type', '').lower()

                    if response.ok and 'text/html' not in content_type:
                        output_path.write_bytes(await response.body())
                        output_path = _detect_and_rename(output_path)
                        file_size_mb = output_path.stat().st_size / (1024 * 1024)
                        print(f"    ✓ 已保存: {output_path.name} ({file_size_mb:.2f} MB)")
                        downloaded_count += 1

                        saved_name = output_path.name
                        if filename in descriptions:
                            downloaded_descriptions[filename] = descriptions[filename]
                        elif saved_name in descriptions:
                            downloaded_descriptions[saved_name] = descriptions[saved_name]
                    else:
                        print(f"    ⚠️  未捕获到下载事件: {filename}")
                except Exception as e:
                    print(f"    ⚠️  直接保存响应失败: {str(e)[:100]}")
            else:
                print(f"    ⚠️  未捕获到下载事件: {filename}")

            if download_page is not page:
                await download_page.close()

        except Exception as e:
            print(f"    ⚠️  处理链接失败: {str(e)[:100]}")

    if downloaded_count > 0:
        print(f"\n  ✓ 成功下载 {downloaded_count} 个补充材料")

    return downloaded_count, downloaded_descriptions



async def download_figure(page, fig_url: str, fig_num: int, output_dir: Path, context=None, force_headed: bool = False) -> str:
    """下载高分辨率图片 - 使用API响应中的URL"""
    try:
        if not fig_url:
            return None

        fig_url = normalize_image_url(fig_url)

        print(f"  📥 下载 Figure {fig_num}: {fig_url}")

        download_page = await context.new_page() if force_headed and context is not None else page

        response = await download_page.goto(fig_url, wait_until='networkidle', timeout=30000)
        content_type = response.headers.get('content-type', '') if response else ''
        if response and content_type.startswith('image/'):
            image_data = await response.body()
            img_filename = original_image_filename(fig_url, fig_num)
            img_path = output_dir / img_filename
            with open(img_path, 'wb') as f:
                f.write(image_data)
            print(f"    ✓ 保存: {img_filename}")
            return img_filename

        img_elements = await download_page.query_selector_all('img')

        if img_elements:
            img_src = await img_elements[0].get_attribute('src')
            if img_src:
                img_src = normalize_image_url(img_src, download_page.url)
                response = await download_page.goto(img_src, wait_until='networkidle', timeout=30000)
                image_data = await response.body()
                img_filename = original_image_filename(img_src, fig_num)

                img_path = output_dir / img_filename
                with open(img_path, 'wb') as f:
                    f.write(image_data)
                print(f"    ✓ 保存: {img_filename}")
                if download_page is not page:
                    await download_page.close()
                return img_filename

    except Exception as e:
        print(f"    ❌ 下载失败: {e}")
    finally:
        try:
            if 'download_page' in locals() and download_page is not page:
                await download_page.close()
        except:
            pass

    return None


# ============================================================================
# 第5部分：主工作流
# ============================================================================

async def complete_extraction_workflow(
    doi: str,
    output_file: str = None,
    force_headed: bool = False,
    refresh_headless_auth: bool = False,
):
    """完整提取工作流 - Phase 4/5 重构版本

    Args:
        doi: 论文的DOI标识符
        output_file: 输出目录路径 (可选，和命令行 --output 含义一致)
        force_headed: 是否强制使用有头浏览器，跳过无头预检 (默认: False)
                       - True: 跳过Phase 0，直接使用有头Chrome
                       - False: 先用无头浏览器预检，根据结果决定是否需要有头
        refresh_headless_auth: 是否通过CDP从真实Chrome刷新无头浏览器登录态

    New architecture:
    1. Phase 0 (可选): 使用无头浏览器快速预检 (除非force_headed=True)
    2. If publisher supports headless extraction, process directly from the headless page
    3. Otherwise connect to headed Chrome and navigate to DOI
    4. Detect publisher
    5. Use handler's extract_all() to get all metadata and links in one go
    6. Download all resources using unified _download_all_resources
    7. Save everything
    """

    doi = doi.strip()
    output_path = Path(output_file or OUTPUT_DIR).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    captured_data_dir = output_path / doi.replace('/', '_')
    captured_data_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 80)
    print("🔍 论文完整提取工作流 (Phase 4-5)")
    print("=" * 80)
    print(f"📌 DOI: {doi}\n")

    # 构建URL
    url = f"https://doi.org/{doi}"

    def build_headless_precheck_urls() -> list:
        """Return candidate URLs for Phase 0, avoiding a hard dependency on doi.org."""
        candidates = [url]
        publisher_hint = detect_publisher_from_url(url)

        if publisher_hint == 'nature' and '/' in doi:
            nature_article_id = doi.split('/', 1)[1].strip()
            if nature_article_id:
                nature_url = f"https://www.nature.com/articles/{nature_article_id}"
                if nature_url not in candidates:
                    candidates.append(nature_url)

        return candidates

    async def process_with_handler(page, context, handler, publisher, captured_data, force_headed_downloads):
        """Run publisher extraction and shared output/download steps."""
        print(f"Step 2️⃣  使用{publisher.upper()}Handler完整提取...")
        print("=" * 80)

        extraction_result = await handler.extract_all(captured=captured_data)

        metadata = extraction_result['metadata']
        links = extraction_result['links']
        fulltext_data = extraction_result['fulltext_data']

        # Save HTML to the per-DOI capture directory.
        if isinstance(fulltext_data, str) and fulltext_data:
            html_file = captured_data_dir / "page.html"
            with open(html_file, 'w', encoding='utf-8') as f:
                f.write(fulltext_data)
            print(f"  ✓ HTML已保存: {html_file}")

        # Merge with Crossref data (fill in missing fields)
        if crossref_data:
            if crossref_data.get('year') and not metadata.get('year'):
                metadata['year'] = str(crossref_data['year'])
            if crossref_data.get('title') and not metadata.get('title'):
                metadata['title'] = crossref_data['title']
            if crossref_data.get('publisher') and not metadata.get('publisher'):
                metadata['publisher'] = crossref_data['publisher']
            if crossref_data.get('type') and not metadata.get('type'):
                metadata['type'] = crossref_data['type']
            # Store Crossref reference data for unified BibTeX generation
            if crossref_data.get('reference'):
                metadata['_crossref_references'] = crossref_data['reference']
                print(f"  ✓ 从Crossref获取{len(crossref_data['reference'])}条参考文献")
            else:
                print(f"  ⚠️  Crossref中没有参考文献数据")

        print(f"  ✓ 标题: {metadata.get('title', 'N/A')[:60]}...")
        print(f"  ✓ 作者: {len(metadata.get('authors', []))} 位")
        print(f"  ✓ 期刊: {metadata.get('journal', 'N/A')}")
        print(f"  ✓ 图片: {len(links.get('figure_urls', {}))} 个")
        print(f"  ✓ 补充材料: {len(links.get('supplemental_urls', []))} 个")
        print()

        # Prepare output directory
        base_output_dir = output_path
        base_output_dir.mkdir(parents=True, exist_ok=True)
        paper_output_dir = organize_paper_output(base_output_dir, metadata, crossref_data)

        markdown_filename = "paper.md"
        markdown_file = paper_output_dir / markdown_filename

        # Step 3: Download all resources
        downloads = await _download_all_resources(
            page,
            links,
            paper_output_dir,
            context,
            metadata,
            doi,
            force_headed_downloads,
        )

        # Step 3.5: Check if paper has meaningful content before saving
        # Skip save only if there's no title (truly empty paper)
        # References are optional - some publishers don't provide them
        title = metadata.get('title', '').strip()
        abstract = metadata.get('abstract', '').strip()
        has_content = bool(title) or bool(abstract)

        if not has_content:
            print(f"\n⚠️  论文缺少标题和摘要，跳过保存")
            print(f"  DOI: {doi}")
            return None

        refs = metadata.get('references', [])
        if not refs and not SAVE_WITHOUT_REFERENCES:
            print(f"\n⚠️  未找到参考文献（某些出版商可能不提供）")
            if SAVE_WITHOUT_REFERENCES:
                print(f"  提示：将继续保存，因为 SAVE_WITHOUT_REFERENCES=True")
            else:
                print(f"  提示：可在 config.py 中设置 SAVE_WITHOUT_REFERENCES=True 强制保存")

        # Step 3.5: Generate markdown with figures
        print("\nStep 3.5️⃣  生成Markdown...")
        print("=" * 80)
        md = handler.convert_to_markdown(
            metadata,
            fulltext_data,
            add_figure_refs=bool(downloads['figures']),
            figure_filenames=downloads['figures'],
            supplemental_urls=links.get('supplemental_urls', []),
            supplemental_descriptions=links.get('supplemental_descriptions', {}),
            supplemental_downloads=downloads.get('supplemental', []),
            table_data=links.get('table_data', {}),
        )
        with open(markdown_file, 'w', encoding='utf-8') as f:
            f.write(md)
        print(f"  ✓ Markdown已保存: {markdown_filename}")

        # Save metadata
        save_metadata_json(paper_output_dir, metadata, crossref_data, doi,
                         downloads['pdf'], downloads['supplemental'])

        # Statistics
        print("\n" + "=" * 80)
        print("📊 完成统计")
        print("=" * 80)
        lines = md.split('\n')
        display_eqs = len(re.findall(r'\$\$', md)) // 2
        print(f"  📄 Markdown 行数: {len(lines)}")
        print(f"  🖼️  图片: {len(downloads['figures'])} 个")
        print(f"  📐 Display equations: {display_eqs} 个")
        if downloads['pdf']:
            print(f"  📕 PDF: {downloads['pdf']}")
        if downloads['supplemental']:
            print(f"  📎 补充材料: {len(downloads['supplemental'])} 个")
        print(f"  💾 输出目录: {paper_output_dir}")
        print(f"  📝 Markdown 文件: {markdown_file}")
        print()

        return str(markdown_file)

    async def cleanup_context_pages(context):
        """Close all pages in a browser context."""
        print("\n🧹 清理标签页...")
        print("=" * 80)
        for context_page in context.pages:
            try:
                await context_page.close()
            except:
                pass
        print("  ✓ 标签页已清理")
        print()

    def check_chrome_ready():
        """Check whether the headed Chrome CDP endpoint is available."""
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex(('127.0.0.1', 9222))
            sock.close()
            return result == 0
        except:
            return False

    async def ensure_headed_chrome_ready() -> bool:
        """Start the real headed Chrome profile if the CDP endpoint is not ready."""
        if check_chrome_ready():
            return True

        print("⚠️  Chrome 未运行，正在启动...")
        import subprocess
        chrome_launcher = Path(__file__).parent / "chrome_launcher.py"
        if not chrome_launcher.exists():
            print("⚠️  chrome_launcher.py 未找到\n")
            return False

        subprocess.Popen([sys.executable, str(chrome_launcher)])
        for _ in range(30):
            await asyncio.sleep(1)
            if check_chrome_ready():
                print("✓ Chrome 已就绪\n")
                return True

        print("⚠️  Chrome 启动超时，无法读取真实浏览器登录态\n")
        return False

    def summarize_storage_state(storage_state: dict) -> str:
        cookie_count = len(storage_state.get('cookies', []))
        origin_count = len(storage_state.get('origins', []))
        return f"{cookie_count} cookies, {origin_count} origins"

    def load_saved_headless_storage_state():
        """Load persisted Playwright storage_state for the headless precheck."""
        if not HEADLESS_AUTH_STATE_FILE.exists():
            print(f"  ℹ️  未找到无头登录态文件: {HEADLESS_AUTH_STATE_FILE}")
            return None

        try:
            with open(HEADLESS_AUTH_STATE_FILE, 'r', encoding='utf-8') as f:
                storage_state = json.load(f)
            print(f"  ✓ 已加载无头登录态: {summarize_storage_state(storage_state)}")
            return storage_state
        except Exception as e:
            print(f"  ⚠️  读取无头登录态失败: {type(e).__name__}: {str(e)[:100]}")
            return None

    def save_headless_storage_state(storage_state: dict):
        """Persist Playwright storage_state for future remote headless runs."""
        try:
            HEADLESS_AUTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(HEADLESS_AUTH_STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(storage_state, f, ensure_ascii=False, indent=2)
            print(f"  ✓ 无头登录态已保存: {HEADLESS_AUTH_STATE_FILE}")
        except Exception as e:
            print(f"  ⚠️  保存无头登录态失败: {type(e).__name__}: {str(e)[:100]}")

    async def export_headed_chrome_storage_state(playwright):
        """Export cookies/localStorage from the headed Chrome profile for headless use."""
        if not await ensure_headed_chrome_ready():
            return None

        try:
            headed_browser = await playwright.chromium.connect_over_cdp("http://localhost:9222")
            if not headed_browser.contexts:
                print("  ⚠️  有头Chrome没有可用context，Phase 0将使用干净无头context")
                return None

            headed_context = headed_browser.contexts[0]
            storage_state = await headed_context.storage_state()
            print(f"  ✓ 已从真实Chrome导出登录态: {summarize_storage_state(storage_state)}")
            save_headless_storage_state(storage_state)
            return storage_state
        except Exception as e:
            print(f"  ⚠️  读取真实Chrome登录态失败: {type(e).__name__}: {str(e)[:100]}")
            return None

    async def load_headless_storage_state(playwright):
        """Resolve the storage_state used by Phase 0 without requiring CDP by default."""
        if refresh_headless_auth:
            print("  🔄 正在从真实Chrome刷新无头登录态...")
            storage_state = await export_headed_chrome_storage_state(playwright)
            if storage_state:
                return storage_state
            print("  → 刷新失败，将尝试读取已有无头登录态文件")

        storage_state = load_saved_headless_storage_state()
        if storage_state:
            return storage_state

        print("  → Phase 0将使用干净无头context继续预检")
        return None

    # ========== 预获取Crossref元数据（Phase 0之前）==========
    print("\nStep 0️⃣ (Pre)  获取Crossref元数据...")
    print("=" * 80)
    crossref_data = fetch_crossref(doi)
    if crossref_data.get('title'):
        print(f"  ✓ 标题: {crossref_data['title'][:60]}...")
        print(f"  ✓ 出版商: {crossref_data.get('publisher', 'N/A')}")
        print(f"  ✓ 类型: {crossref_data.get('type', 'N/A')}")
        print(f"  ✓ 年份: {crossref_data.get('year', 'N/A')}")
        print(f"  ✓ 参考文献: {len(crossref_data.get('references', []))} 条")
    else:
        print("  ⚠️  Crossref未返回数据")
    print()

    # ========== 第1步判断：根据Crossref publisher决定是否需要Phase 0 ==========
    should_use_headless_phase0 = False
    if not force_headed:
        crossref_publisher = crossref_data.get('publisher', '').lower()
        if crossref_publisher:
            # Check if Crossref publisher contains any HEADLESS_ACCESSIBLE_PUBLISHERS
            for publisher_name in HEADLESS_ACCESSIBLE_PUBLISHERS:
                if publisher_name.lower() in crossref_publisher:
                    should_use_headless_phase0 = True
                    print(f"✓ 根据Crossref publisher '{crossref_publisher}' 判断出版商为 {publisher_name.upper()}")
                    print(f"  → 将使用Phase 0进行无头浏览器预检\n")
                    break

        if not should_use_headless_phase0:
            print(f"⊘ Crossref publisher '{crossref_publisher}' 不在无头直连列表中")
            print(f"  → 跳过Phase 0，直接使用有头浏览器\n")

    # ========== 阶段0（可选）：使用无头浏览器快速预检 ==========
    # 如果 force_headed=True，跳过此阶段直接使用有头浏览器
    # 典型使用场景：已知目标期刊必须使用有头浏览器访问

    headless_success = False
    headless_blocked = False
    headless_publisher = None
    headless_html = None

    if force_headed:
        print("\n🔧 强制有头模式 - 跳过无头浏览器预检")
        print("=" * 80)
        print("  将直接使用有头Chrome访问\n")
    elif not should_use_headless_phase0:
        print("\n⊘ 出版商不支持无头浏览器 - 跳过Phase 0")
        print("=" * 80)
        print("  将直接使用有头浏览器完整提取\n")
    else:
        print("\n📋 Phase 0️⃣  使用无头浏览器快速预检页面...")
        print("=" * 80)

        try:
            async with async_playwright() as p:
                storage_state = await load_headless_storage_state(p)
                context_kwargs = {'accept_downloads': True}
                if storage_state:
                    context_kwargs['storage_state'] = storage_state

                headless_browser = await p.chromium.launch(headless=True)
                headless_context = await headless_browser.new_context(**context_kwargs)
                headless_page = await headless_context.new_page()

                try:
                    last_precheck_error = None
                    for precheck_url in build_headless_precheck_urls():
                        print(f"  ↪ 预检访问: {precheck_url}")
                        try:
                            await headless_page.goto(precheck_url, wait_until='domcontentloaded', timeout=60000)
                            try:
                                await headless_page.wait_for_load_state('networkidle', timeout=15000)
                            except:
                                print("  ℹ️  页面主文档已加载，后台资源未完全静默，继续预检")
                            last_precheck_error = None
                            break
                        except Exception as e:
                            last_precheck_error = e
                            print(f"  ⚠️  预检访问失败: {type(e).__name__}: {str(e)[:100]}")

                    if last_precheck_error is not None:
                        raise last_precheck_error

                    # 保存无头浏览器访问结果
                    # 保存HTML
                    headless_html = await headless_page.content()
                    headless_html_file = captured_data_dir / "headless_initial.html"
                    page_html_file = captured_data_dir / "page.html"
                    with open(headless_html_file, 'w', encoding='utf-8') as f:
                        f.write(headless_html)
                    with open(page_html_file, 'w', encoding='utf-8') as f:
                        f.write(headless_html)

                    print(f"  ✓ 页面已保存: {headless_html_file.name} ({len(headless_html)} 字节)")

                    # 检测最终URL
                    final_headless_url = headless_page.url
                    print(f"  ✓ 最终URL: {final_headless_url}")

                    # 检测出版商
                    headless_publisher = detect_publisher_from_url(final_headless_url)
                    print(f"  ✓ 检测出版商: {headless_publisher.upper()}")

                    # 检测是否被反爬虫拦截
                    if is_bot_challenge_page(final_headless_url, headless_html):
                        print(f"  ⚠️  检测到反爬虫拦截页面 (validate.perfdrive.com 等)")
                        print(f"  → 无头浏览器被拦截，将回退到有头Chrome")
                        headless_success = False  # Force fallback to headed browser
                        headless_blocked = True
                        headless_publisher = None  # Prevent headless-only handler path

                    headless_success = True

                    if headless_publisher in HEADLESS_ACCESSIBLE_PUBLISHERS:
                        print()
                        print("🟢 无头直连路径：当前出版商支持无头完整提取")
                        print("=" * 80)
                        print(f"  出版商类型: {headless_publisher.upper()}")
                        print("  → 跳过有头Chrome连接，直接使用无头页面进入Handler流程")

                        handler = get_publisher_handler(
                            headless_publisher,
                            page=headless_page,
                            captured_data_dir=captured_data_dir,
                            doi=doi,
                        )

                        captured_data = None
                        if hasattr(handler, 'setup_network_capture'):
                            captured_data = handler.setup_network_capture(headless_page, doi)
                            print("✓ 网络监听已启动\n")

                        result = await process_with_handler(
                            headless_page,
                            headless_context,
                            handler,
                            headless_publisher,
                            captured_data,
                            force_headed,
                        )
                        await cleanup_context_pages(headless_context)
                        await headless_browser.close()
                        return result

                except Exception as e:
                    print(f"  ⚠️  无头浏览器访问失败: {type(e).__name__}: {str(e)[:100]}")
                    print(f"  → 这对某些需要认证或完整JavaScript渲染的出版商是正常的")
                finally:
                    try:
                        await headless_page.close()
                    except:
                        pass
                    try:
                        await headless_browser.close()
                    except:
                        pass
        except Exception as e:
            print(f"  ⚠️  无头浏览器启动失败: {e}")

        print()

        # ========== Phase 0分析：判断是否需要有头浏览器 ==========

        print("📊 Phase 0分析：评估是否需要有头浏览器...")
        print("=" * 80)

        if headless_success and headless_publisher:
            print(f"  出版商类型: {headless_publisher.upper()}")

            if headless_publisher in HEADLESS_ACCESSIBLE_PUBLISHERS:
                print(f"  ℹ️  {headless_publisher.upper()} 支持无头直连")
                print(f"  ⚠️  无头直连未完成，将尝试Handler自主管理无头访问")
            else:
                print(f"  ℹ️  {headless_publisher.upper()} 未配置无头直连")
                print(f"  💡 将使用有头浏览器进行完整提取")
        else:
            print(f"  ⚠️  无头浏览器预检失败")
            if detect_publisher_from_url(url) in HEADLESS_ACCESSIBLE_PUBLISHERS:
                print(f"  💡 DOI可识别为无头可访问出版商，将尝试Handler自主管理无头访问")
            else:
                print(f"  💡 将使用有头浏览器进行完整提取")

        print()

        fallback_publisher = headless_publisher or detect_publisher_from_url(url)
        if fallback_publisher in HEADLESS_ACCESSIBLE_PUBLISHERS and not headless_blocked:
            print("🟢 无头Handler自主管理路径：当前出版商支持无头完整提取")
            print("=" * 80)
            print(f"  出版商类型: {fallback_publisher.upper()}")
            print("  → 不连接有头Chrome，交给PublisherHandler自行创建无头页面")

            handler = get_publisher_handler(
                fallback_publisher,
                captured_data_dir=captured_data_dir,
                doi=doi,
            )

            return await process_with_handler(
                None,
                None,
                handler,
                fallback_publisher,
                None,
                False,
            )

        print("  🔵 标准路径：使用有头浏览器完整提取")

        print()

    # 检查Chrome是否就绪
    await ensure_headed_chrome_ready()

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
            print("✓ 已连接到Chrome\n")
        except Exception as e:
            print(f"❌ 无法连接到Chrome port 9222: {e}")
            print("   请运行: python chrome_launcher.py\n")
            return None

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
            publisher = detect_publisher_from_url(url)
            handler = get_publisher_handler(
                publisher,
                page=page,
                captured_data_dir=captured_data_dir,
                doi=doi,
            )
            captured_data = None
            if hasattr(handler, 'setup_network_capture'):
                captured_data = handler.setup_network_capture()
                print("✓ 网络监听已启动\n")

            try:
                await page.goto(url, wait_until='networkidle', timeout=60000)
            except:
                pass

            final_url = page.url
            print(f"✓ 最终 URL: {final_url}")

            final_publisher = detect_publisher_from_url(final_url)
            if final_publisher != publisher:
                publisher = final_publisher
                handler = get_publisher_handler(
                    publisher,
                    page=page,
                    captured_data_dir=captured_data_dir,
                    doi=doi,
                )
                captured_data = None
                if hasattr(handler, 'setup_network_capture'):
                    captured_data = handler.setup_network_capture()
                    print("✓ 网络监听已启动\n")
            else:
                handler.configure(page=page, captured_data_dir=captured_data_dir, doi=doi)

            print(f"✓ 检测出版商: {publisher.upper()}\n")

            # Step 2: Use handler's extract_all for complete extraction
            #
            # handler.extract_all() 返回一个统一的字典结构（所有出版商通用）：
            # {
            #     'metadata': {
            #         'title': str,                    # 论文标题
            #         'authors': [str],               # 作者列表
            #         'author_with_affiliations': [   # 带机构的作者信息
            #             {'author': str, 'affiliations': [str]}
            #         ],
            #         'abstract': str,                # 摘要
            #         'journal': str,                 # 期刊名称
            #         'year': str,                    # 发表年份
            #         'volume': str,                  # 卷号
            #         'issue': str,                   # 期号
            #         'pages': str,                   # 页码
            #         'doi': str,                     # DOI
            #         'publication_date': str,        # 发表日期
            #         'corresponding_author_emails': [str],  # 通讯作者邮箱
            #         'references': [str],            # 参考文献列表
            #     },
            #     'links': {
            #         'pdf_url': str,                 # PDF下载链接 (如 https://journals.aps.org/prl/pdf/...)
            #         'figure_urls': {
            #             'fig_1': {                  # 图片ID
            #                 'url': str,             # 图片完整URL
            #                 'caption': str,         # 图片标题
            #             },
            #             ...
            #         },
            #         'supplemental_urls': [str],     # 补充材料链接列表
            #         'supplemental_descriptions': {  # 补充材料描述 (可选)
            #             'filename': 'description',
            #             ...
            #         },
            #     },
            #     'fulltext_data': str|dict,          # 文章内容，格式由PublisherHandler决定
            #     'journal_prefix' / 'journal_name': str, # 可选的出版商扩展字段
            # }
            if callable(getattr(handler, 'extract_all', None)):
                result = await process_with_handler(page, context, handler, publisher, captured_data, True)
            else:
                # Other publishers - use Crossref metadata only
                print("Step 2️⃣  使用Crossref元数据...")
                print("=" * 80)

                metadata = crossref_data or {
                    'title': 'Unknown Paper',
                    'authors': [],
                    'journal': 'Unknown Journal',
                    'year': None
                }

                print(f"  ✓ 标题: {metadata.get('title', 'N/A')[:60]}...")
                print(f"  ✓ 作者: {len(metadata.get('authors', []))} 位")
                print(f"  ✓ 期刊: {metadata.get('journal', 'N/A')}")
                print(f"  ✓ DOI: {doi}")
                print()
                result = None

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
            return result

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

            return None


# ============================================================================
# 入口点
# ============================================================================

async def main():
    """Entry point with argparse support

    Examples:
        # 单个DOI
        python complete_paper_extraction.py --doi 10.1103/PhysRevLett.109.245005

        # 从文件列表
        python complete_paper_extraction.py --file doi_list.txt

        # 指定输出目录
        python complete_paper_extraction.py --doi 10.1103/PhysRevLett.109.245005 --output ~/Downloads

        # 强制使用有头浏览器（跳过无头预检）
        python complete_paper_extraction.py --doi 10.1103/PhysRevLett.109.245005 --force-headed

        # DOI列表 + 强制有头
        python complete_paper_extraction.py --file doi_list.txt --force-headed
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="完整论文提取工作流 - 从DOI到完整Markdown的端到端解决方案",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  单个DOI:
    python %(prog)s --doi 10.1103/PhysRevLett.109.245005

  从文件列表:
    python %(prog)s --file doi_list.txt

  指定输出目录:
    python %(prog)s --doi 10.1103/PhysRevLett.109.245005 --output ~/Downloads

  强制使用有头浏览器（跳过无头预检）:
    python %(prog)s --doi 10.1103/PhysRevLett.109.245005 --force-headed
        """
    )

    # 创建互斥组用于--doi和--file
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        '--doi',
        type=str,
        help='单个DOI (例如: 10.1103/PhysRevLett.109.245005)'
    )
    input_group.add_argument(
        '--file',
        type=str,
        metavar='FILE',
        help='包含DOI列表的文件 (每行一个DOI)'
    )

    parser.add_argument(
        '--output',
        type=str,
        default=OUTPUT_DIR,
        help=f'输出目录路径 (默认: {OUTPUT_DIR})'
    )

    parser.add_argument(
        '--force-headed',
        action='store_true',
        default=False,
        help='强制使用有头浏览器，跳过无头预检阶段 (默认: False，使用智能检测)'
    )

    parser.add_argument(
        '--refresh-headless-auth',
        action='store_true',
        default=False,
        help='通过本机Chrome CDP导出登录态到 .auth/headless_storage_state.json，供后续无头预检使用'
    )

    args = parser.parse_args()

    # 构建DOI列表
    dois = []
    if args.doi:
        dois = [args.doi]
        print(f"📌 单个DOI: {args.doi}\n")
    elif args.file:
        try:
            with open(args.file, 'r', encoding='utf-8') as f:
                dois = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
            print(f"📌 从文件读取 {len(dois)} 个DOI: {args.file}\n")
        except FileNotFoundError:
            print(f"❌ 文件不存在: {args.file}")
            sys.exit(1)
        except Exception as e:
            print(f"❌ 读取文件时出错: {e}")
            sys.exit(1)

    if not dois:
        print("❌ 没有有效的DOI")
        sys.exit(1)

    # 处理force_headed参数
    force_headed_mode = args.force_headed
    if force_headed_mode:
        print(f"🔧 强制有头浏览器模式启用 - 将跳过无头浏览器预检\n")
    if args.refresh_headless_auth:
        print(f"🔄 将刷新无头浏览器登录态缓存: {HEADLESS_AUTH_STATE_FILE}\n")

    # 处理输出路径
    output_dir = str(Path(args.output).expanduser().resolve())
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    print(f"📁 输出目录: {output_path}\n")

    # 处理多个DOI
    success_count = 0
    fail_count = 0

    for i, doi in enumerate(dois, 1):
        print(f"\n{'='*80}")
        print(f"处理论文 {i}/{len(dois)}: {doi}")
        print(f"{'='*80}\n")

        try:
            md_path = await complete_extraction_workflow(
                doi,
                output_file=output_dir,
                force_headed=force_headed_mode,
                refresh_headless_auth=args.refresh_headless_auth,
            )
            if md_path:
                success_count += 1
                print(f"✅ 成功: {md_path}")
            else:
                fail_count += 1
        except Exception as e:
            print(f"❌ 处理失败: {e}")
            fail_count += 1

        # 批量处理防拉黑：随机睡眠 (最后一条不需要)
        if BATCH_SLEEP_ENABLED and i < len(dois):
            sleep_seconds = random.randint(BATCH_SLEEP_MIN, BATCH_SLEEP_MAX)
            sleep_minutes = sleep_seconds / 60
            print(f"\n😴 防拉黑休眠 {sleep_seconds}s ({sleep_minutes:.1f} min)...")
            await asyncio.sleep(sleep_seconds)
            print("🚀 继续下一篇文章...\n")

    # 显示统计信息
    if len(dois) > 1:
        print(f"\n{'='*80}")
        print(f"📊 处理完成")
        print(f"{'='*80}")
        print(f"✓ 成功: {success_count}/{len(dois)}")
        print(f"✗ 失败: {fail_count}/{len(dois)}")

    sys.exit(0 if fail_count == 0 else 1)



if __name__ == "__main__":
    asyncio.run(main())

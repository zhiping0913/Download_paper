"""
APS Journal Publisher Implementation
Handles extraction from American Physical Society journals (prl, pre, pra, etc.)
"""

import asyncio
import re
import json
import requests
from pathlib import Path
from html import unescape
from datetime import datetime

try:
    import pypandoc
except:
    import subprocess
    import sys
    subprocess.check_call([sys.executable, 'install', 'pypandoc', '-q'])
    import pypandoc

from publisher.base import PublisherHandler
from json_to_md_converter import mathml_to_latex_pandoc, extract_text_without_math
from playwright.async_api import async_playwright


# ============================================================================
# APS 特定函数 - 从 complete_paper_extraction.py 提取
# ============================================================================
# 注意：这些是从 complete_paper_extraction.py 中提取的 APS 专用函数
# 保持原有逻辑，避免修改


async def aps_extract_metadata_from_page(page) -> dict:
    """从页面meta标签提取完整元数据（作者、单位、摘要等）- APS 专用"""
    from complete_paper_extraction import extract_metadata_from_page
    return await extract_metadata_from_page(page)


def aps_extract_references_from_html(html: str) -> list:
    """从HTML提取References - APS 专用"""
    from complete_paper_extraction import extract_references_from_html
    return extract_references_from_html(html)


async def aps_get_supplemental_links(page, doi: str, journal_prefix: str = None) -> tuple:
    """获取补充材料链接 - APS 专用"""
    return await get_supplemental_links(page, doi, journal_prefix)


async def get_supplemental_links(page, doi: str, journal_prefix: str = None) -> tuple:
    """获取补充材料的所有下载链接和描述信息

    Returns: (supplemental_links, descriptions_dict)
    """
    try:
        if not journal_prefix:
            journal_prefix = 'prl'  # 默认值
        supplemental_url = f"https://journals.aps.org/{journal_prefix}/supplemental/{doi}"
        print(f"  🔗 获取补充材料链接: {supplemental_url}")

        # 监听网络响应来获取描述
        supplemental_data = None

        def handle_response(response):
            nonlocal supplemental_data
            try:
                if '/supplemental/' in response.url and response.status == 200:
                    if 'application/json' in response.headers.get('content-type', ''):
                        supplemental_data = response.json()
            except:
                pass

        page.on("response", handle_response)

        await page.goto(supplemental_url, wait_until='networkidle', timeout=60000)

        # 提取所有链接
        links_js = """
        () => {
            const links = [];

            // 找所有指向PDF、doc等的链接
            document.querySelectorAll('a').forEach(a => {
                const href = a.getAttribute('href');
                const text = a.innerText || a.textContent;
                if (href && !href.includes('login') && !href.includes('scholar.google')) {
                    if (href.includes('supplemental') || href.includes('pdf') || href.includes('doc') || href.includes('zip') || href.includes('gif')) {
                        links.push({
                            text: text.trim(),
                            href: href,
                            url: new URL(href, window.location.href).href
                        });
                    }
                }
            });

            // 去重
            const seen = new Set();
            return links.filter(link => {
                if (seen.has(link.url)) return false;
                seen.add(link.url);
                return link.url.length > 0 && !link.url.includes('login');
            });
        }
        """

        supp_links = await page.evaluate(links_js)

        # 从页面HTML中提取补充材料描述
        # description通常在<p>标签中，与下载链接相关
        descriptions_js = """
        () => {
            const descriptions = {};

            // 方法1: 查找每个文件对应的<p>标签描述
            const links = document.querySelectorAll('a');
            links.forEach(link => {
                const href = link.getAttribute('href');
                if (href && (href.includes('supplemental') || href.includes('.gif') || href.includes('.pdf') || href.includes('.doc'))) {
                    const filename = href.split('/').pop();

                    // 查找最近的段落或描述
                    let element = link.parentElement;
                    let description = '';

                    // 向上查找最多5层
                    for (let i = 0; i < 5; i++) {
                        if (!element) break;

                        // 查找<p>标签中的文本
                        const pTags = element.querySelectorAll('p');
                        if (pTags.length > 0) {
                            description = pTags[0].innerText || pTags[0].textContent;
                            if (description && description.length > 10) break;
                        }

                        element = element.parentElement;
                    }

                    if (description) {
                        descriptions[filename] = description.trim();
                    }
                }
            });

            // 方法2: 如果没找到，直接获取所有<p>标签
            if (Object.keys(descriptions).length === 0) {
                const allP = document.querySelectorAll('p');
                allP.forEach(p => {
                    const text = (p.innerText || p.textContent).trim();
                    if (text.length > 20 && !text.includes('Copyright')) {
                        // 尝试匹配到文件
                        const links = p.querySelectorAll('a');
                        if (links.length > 0) {
                            links.forEach(link => {
                                const href = link.getAttribute('href');
                                if (href) {
                                    const filename = href.split('/').pop();
                                    if (!descriptions[filename]) {
                                        descriptions[filename] = text;
                                    }
                                }
                            });
                        }
                    }
                });
            }

            return descriptions;
        }
        """

        try:
            descriptions = await page.evaluate(descriptions_js) or {}
        except Exception as e:
            print(f"  ⚠️  提取描述失败: {e}")
            descriptions = {}

        if descriptions:
            print(f"  📝 从HTML提取 {len(descriptions)} 个描述")
            for filename, desc in list(descriptions.items())[:2]:
                print(f"    - {filename}: {desc[:50]}...")

        page.remove_listener("response", handle_response)

        if supp_links:
            print(f"  ✓ 找到 {len(supp_links)} 个补充材料")
            return supp_links, descriptions
        else:
            return [], {}

    except Exception as e:
        print(f"  ⚠️  获取补充材料链接失败: {e}")
        return [], {}


async def aps_download_pdf(page, doi: str, output_dir: Path, journal_prefix: str = None) -> str:
    """下载 PDF - APS 专用"""
    from complete_paper_extraction import download_pdf
    return await download_pdf(page, doi, output_dir, journal_prefix)


def aps_extract_figure_assets_from_fulltext(fulltext_data: dict, journal_prefix: str = None) -> dict:
    """从 fulltext JSON 提取图片资源 - APS 专用"""
    from complete_paper_extraction import extract_figure_assets_from_fulltext
    return extract_figure_assets_from_fulltext(fulltext_data)


async def aps_download_figure(page, fig_url: str, fig_num: int, output_dir: Path) -> str:
    """下载图片 - APS 专用"""
    from complete_paper_extraction import download_figure
    return await download_figure(page, fig_url, fig_num, output_dir)


async def aps_json_to_markdown_complete(json_file: str, doi: str, metadata: dict,
                                        journal_prefix: str, paper_output_dir, figure_map: dict = None) -> str:
    """从 JSON 转换为 Markdown - APS 专用"""
    from complete_paper_extraction import json_to_markdown_complete
    return await json_to_markdown_complete(json_file, doi, metadata, journal_prefix, paper_output_dir, figure_map)


class APSHandler(PublisherHandler):
    """Handler for American Physical Society (APS) journals"""

    def __init__(self, journal_prefix: str = 'prl'):
        """
        Initialize APS handler

        Args:
            journal_prefix: Journal code (prl, pre, pra, prb, etc.)
        """
        self.journal_prefix = journal_prefix
        self.base_url = f"https://journals.aps.org/{journal_prefix}"

    async def extract_metadata(self, page) -> dict:
        """Extract metadata from APS abstract page"""
        return await aps_extract_metadata_from_page(page)

    async def get_fulltext_url(self, doi: str) -> str:
        """Get URL for full article text API endpoint"""
        # APS fulltext endpoint format: /fulltext/{doi}
        return f"{self.base_url}/fulltext/{doi}"

    async def get_pdf_url(self, page) -> str:
        """Get PDF download URL"""
        # For APS, we use the standard pattern
        doi = await page.evaluate("() => document.querySelector('meta[name=\"citation_doi\"]')?.getAttribute('content')")
        if doi:
            return f"{self.base_url}/pdf/{doi}"
        return None

    async def get_supplemental_url(self, doi: str) -> str:
        """Construct supplemental materials URL"""
        return f"{self.base_url}/supplemental/{doi}"

    async def extract_references(self, html: str) -> list:
        """Parse references from HTML"""
        return aps_extract_references_from_html(html)

    async def get_figures(self, json_data: dict) -> dict:
        """Extract figure URLs and captions from APS JSON"""
        return extract_figure_assets_from_fulltext(json_data)

    def convert_to_markdown(self, metadata: dict, fulltext_json: dict, add_figure_refs: bool = False) -> str:
        """Convert extracted data to Markdown using fulltext JSON

        Args:
            metadata: Paper metadata dict
            fulltext_json: Full text JSON data
            add_figure_refs: If True, add figure references in markdown. If False, skip them (default).
                            Use False when figures haven't been downloaded yet.
        """
        from json_to_md_converter import convert_json_data_to_markdown, cleanup_markdown

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
        if metadata.get('doi'):
            md_content += f"**DOI:** {metadata['doi']}\n\n"
        md_content += "---\n\n"

        # ===== 摘要 =====
        if metadata.get('abstract'):
            md_content += "## Abstract\n\n"
            md_content += f"{metadata['abstract']}\n\n"
            md_content += "---\n\n"

        # ===== 正文 - 使用JSON递归转换 =====
        md_content += "## Article Text\n\n"
        if fulltext_json:
            try:
                article_md = convert_json_data_to_markdown(fulltext_json)
                md_content += article_md
            except Exception as e:
                print(f"  ⚠️  JSON转换错误: {e}")
                md_content += ""

        md_content += "\n---\n\n"

        # ===== 参考文献 =====
        if metadata.get('references'):
            md_content += "## References\n\n"
            for i, ref in enumerate(metadata['references'], 1):
                md_content += f"[{i}] {ref}\n\n"

        # 跨发布商通用的清理 (移到 json_to_md_converter.cleanup_markdown)
        md_content = cleanup_markdown(md_content)

        # ===== 后处理：在独立的 FIG./Fig. X 行后添加图片引用 =====
        # 只在add_figure_refs为True且成功下载图片时添加引用
        if add_figure_refs:
            # 只匹配独立的 "FIG. X" 或 "Fig. X" 行（不是 "Fig. X(a)" 这样的inline引用）
            # 查找行首的 Fig/FIG 标记
            def add_figure_reference(match):
                line = match.group(0)  # 完整的行
                # 提取图片编号
                fig_match = re.search(r'[Ff][Ii][Gg]\.\s*(\d+)', line)
                if fig_match:
                    fig_num = fig_match.group(1)
                    # 在该行后添加图片引用
                    return f"{line}\n\n![Figure {fig_num}](figure_{fig_num}.png)"
                return line

            # 匹配行首的 "FIG. X" 或 "Fig. X" (可能带或不带句号，但后面不是括号)
            md_content = re.sub(
                r'^([Ff][Ii][Gg]\.\s*\d+\.?)(?!\()',
                add_figure_reference,
                md_content,
                flags=re.MULTILINE
            )

        return md_content


# ============================================================================
# APS-Specific Extraction Functions
# (These will be gradually moved from complete_paper_extraction.py)
# ============================================================================

def extract_supplemental_info(html: str) -> str:
    """Extract Supplemental Material information from abstract page HTML"""
    try:
        # Look for supplemental link
        supp_match = re.search(
            r'<a[^>]*href=["\']([^"\']*supplemental[^"\']*)["\'][^>]*>([^<]+)</a>',
            html,
            re.IGNORECASE
        )
        if supp_match:
            supp_url = supp_match.group(1)
            supp_text = supp_match.group(2).strip()
            return f"[{supp_text}]({supp_url})"
        return None
    except:
        return None


def extract_references_from_html(html: str) -> list:
    """Extract References list from abstract page HTML (with DOI links)"""
    try:
        references = []
        ol = re.search(r'<ol class="references"[^>]*>(.*?)</ol>', html, re.DOTALL)
        if ol:
            ref_items = re.findall(
                r'<li[^>]*id="ref-\d+"[^>]*>(.*?)</li>',
                ol.group(1),
                re.DOTALL
            )
            for ref_item in ref_items:
                # Extract text and DOI link
                text = re.sub(r'<[^>]+>', '', ref_item).strip()
                # Also try to find DOI link if present
                doi_match = re.search(r'https://dx\.doi\.org/([^"\'<>\s]+)', ref_item)
                if doi_match:
                    doi = doi_match.group(1)
                    text = f"{text.rstrip('.')} (DOI: {doi})"
                references.append(text)
        return references
    except:
        return []


def extract_figure_assets_from_fulltext(fulltext_data: dict) -> dict:
    """
    从fulltext API响应中提取图片资源信息
    返回: {fig_id: {"url": "...", "caption": "..."}, ...}
    """
    figure_assets = {}

    if not fulltext_data:
        return figure_assets

    def search_assets(obj):
        """递归搜索所有asset对象"""
        if isinstance(obj, dict):
            # 检查是否是figure asset
            if obj.get('type') == 'figure' and 'variants' in obj:
                fig_id = obj.get('id', '')
                variants = obj.get('variants', {})

                # 优先使用large版本，其次medium
                fig_url = variants.get('large') or variants.get('medium')

                if fig_url and fig_id:
                    figure_assets[fig_id] = {
                        'url': fig_url,
                        'caption': obj.get('caption', '')
                    }

            # 递归搜索字典中的所有值
            for v in obj.values():
                search_assets(v)

        elif isinstance(obj, list):
            for item in obj:
                search_assets(item)

    search_assets(fulltext_data)
    return figure_assets


def extract_supplemental_descriptions(supplemental_data: dict) -> dict:
    """从supplemental API响应中提取每个文件的描述

    Returns: {filename: description, ...}
    """
    descriptions = {}

    if not supplemental_data:
        return descriptions

    try:
        # 尝试多种可能的JSON结构
        files = supplemental_data.get('files', [])

        # 如果没有files字段，尝试其他结构
        if not files:
            if isinstance(supplemental_data, list):
                files = supplemental_data
            elif 'data' in supplemental_data:
                files = supplemental_data.get('data', [])
            elif 'supplemental' in supplemental_data:
                files = supplemental_data.get('supplemental', [])

        if not isinstance(files, list):
            files = [files] if files else []

        for file_item in files:
            if isinstance(file_item, dict):
                # 获取文件名
                filename = file_item.get('filename', '') or file_item.get('name', '') or file_item.get('file', '')
                url = file_item.get('url', '')

                if not filename and url:
                    filename = url.split('/')[-1]

                # 获取描述
                description = (
                    file_item.get('description', '') or
                    file_item.get('desc', '') or
                    file_item.get('caption', '')
                )

                if description:
                    # 清理HTML标签
                    description = re.sub(r'<br\s*/?>', ' ', description)  # <br> -> 空格
                    description = re.sub(r'<p[^>]*>', '', description)    # 移除<p>
                    description = re.sub(r'</p>', ' ', description)        # </p> -> 空格
                    description = re.sub(r'<[^>]+>', '', description)      # 移除所有HTML标签
                    description = re.sub(r'&\w+;', lambda m: {'&lt;': '<', '&gt;': '>', '&amp;': '&', '&#x2F;': '/'}.get(m.group(0), m.group(0)), description)  # 解码HTML实体
                    description = re.sub(r'\s+', ' ', description)         # 多空格->单空格
                    from html import unescape
                    description = unescape(description).strip()

                    if filename and description:
                        descriptions[filename] = description
                        print(f"  📝 找到描述: {filename[:40]} - {description[:50]}...")

    except Exception as e:
        print(f"  ⚠️  解析supplemental描述失败: {e}")

    return descriptions


def extract_pdf_link_from_html(html_content: str) -> str:
    """Extract PDF download link from abstract page HTML"""
    try:
        pdf_match = re.search(r'href=["\']([^"\']*\.pdf)["\']', html_content)
        if pdf_match:
            pdf_url = pdf_match.group(1)
            if not pdf_url.startswith('http'):
                pdf_url = f"https://journals.aps.org{pdf_url}"
            return pdf_url
    except:
        pass
    return None


# ============================================================================
# APS Paper Conversion Functions - From convert_complete_paper.py
# ============================================================================

def get_paper_metadata_from_semantic_scholar(doi: str) -> dict:
    """从Semantic Scholar API获取论文元数据"""
    metadata = {
        'title': None,
        'authors': [],
        'journal': None,
        'publication_year': None,
        'citations_count': 0,
    }

    try:
        s2_url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
        params = {'fields': 'title,authors,abstract,year,journal,venue,citationCount'}
        headers = {'User-Agent': 'Mozilla/5.0'}

        response = requests.get(s2_url, params=params, headers=headers, timeout=15)
        if response.status_code == 200:
            data = response.json()
            metadata['title'] = data.get('title')
            metadata['journal'] = data.get('journal', {}).get('name') or data.get('venue')
            metadata['publication_year'] = data.get('year')
            metadata['citations_count'] = data.get('citationCount', 0)

            for author in data.get('authors', []):
                metadata['authors'].append(author.get('name'))
    except Exception as e:
        print(f"⚠️  获取Semantic Scholar数据失败: {e}")

    return metadata


def extract_figure_caption(comp: dict) -> tuple:
    """Extract figure number and caption - FULLY RECURSIVE"""
    def get_all_text(c):
        text_parts = []
        body = c.get('body', '')
        if body:
            text = extract_text_without_math(body)
            if text:
                text_parts.append(text)
        for nested in c.get('components', []):
            nested_text = get_all_text(nested)
            if nested_text:
                text_parts.append(nested_text)
        return " ".join(text_parts) if text_parts else None

    caption = get_all_text(comp)
    fig_num = None
    if caption:
        match = re.search(r'FIG\.\s*(\d+)', caption)
        if match:
            fig_num = match.group(1)
            caption = re.sub(r'^FIG\.\s*\d+\.\s*', '', caption)

    return fig_num, caption


async def download_figure(page, doi: str, fig_num: int, output_dir: Path) -> str:
    """Download figure using authenticated browser"""
    try:
        fig_url = f"https://journals.aps.org/prl/article/{doi}/figures/{fig_num}/large"
        print(f"  📥 下载 Figure {fig_num}...")

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


def process_component(comp: dict, doi: str = None, output_dir: Path = None,
                     downloaded_figures: dict = None) -> tuple:
    """Process component and return (text, fig_num, fig_caption)"""
    if downloaded_figures is None:
        downloaded_figures = {}

    text = ""
    comp_type = comp.get('type', 'p')
    klass = comp.get('klass', '')

    if 'figure' in klass.lower():
        fig_num, caption = extract_figure_caption(comp)
        if fig_num:
            return ("FIGURE_MARKER", fig_num, caption)

    if 'disp-eq' in klass:
        def get_math(c):
            body = c.get('body', '')
            if '<math' in body:
                match = re.search(r'<math[^>]*>.*?</math>', body, re.DOTALL)
                if match:
                    return match.group(0)
            for nested in c.get('components', []):
                result = get_math(nested)
                if result:
                    return result
            return None

        math_xml = get_math(comp)
        if math_xml:
            latex = mathml_to_latex_pandoc(math_xml)
            if latex:
                text += f"\n\n{latex}\n"
                return (text, None, None)

    if comp.get('components'):
        for nested in comp['components']:
            nested_text, fig_num, fig_caption = process_component(nested, doi, output_dir, downloaded_figures)
            if fig_num:
                text += f"FIGURE_MARKER_{fig_num}__{fig_caption}\n"
            else:
                text += nested_text

    if comp.get('body'):
        body = extract_text_without_math(comp['body'])
        if body:
            if comp_type == 'p':
                text += f"\n\n{body}\n"
            elif comp_type in ('sec', 'sec-intro'):
                text += f"\n\n## {body}\n"
            else:
                text += f"\n\n{body}\n"

    return (text, None, None)


async def json_to_markdown_complete(json_file: str, doi: str, output_file: str = None) -> str:
    """Complete conversion: metadata + content + figures"""

    print("📊 正在获取元数据...\n")
    metadata = get_paper_metadata_from_semantic_scholar(doi)

    with open(json_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    output_dir = Path(output_file).parent if output_file else Path.cwd()
    output_dir.mkdir(parents=True, exist_ok=True)

    md_content = ""

    # ===== 标题部分 =====
    title = metadata.get('title') or "Academic Paper"
    md_content += f"# {title}\n\n"

    # ===== 作者信息 =====
    if metadata.get('authors'):
        md_content += "## Authors\n\n"
        for author in metadata['authors']:
            md_content += f"- {author}\n"
        md_content += "\n"

    # ===== 期刊和发表信息 =====
    md_content += "## Publication\n\n"
    if metadata.get('journal'):
        md_content += f"**Journal:** {metadata['journal']}\n\n"
    if metadata.get('publication_year'):
        md_content += f"**Year:** {metadata['publication_year']}\n\n"
    if doi:
        md_content += f"**DOI:** {doi}\n\n"
    if metadata.get('citations_count'):
        md_content += f"**Citations:** {metadata['citations_count']}\n\n"

    md_content += "---\n\n"

    # ===== 论文正文 =====
    downloaded_figures = {}
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
            contexts = browser.contexts
            context = contexts[0] if contexts else await browser.new_context()
            page = await context.new_page()

            print("🔗 已连接Chrome\n")

            # Process front matter
            if 'front' in data:
                front = data['front']
                for comp in front.get('components', []):
                    comp_type = comp.get('type', 'p')
                    klass = comp.get('klass', '')

                    if 'figure' in klass.lower():
                        fig_num, caption = extract_figure_caption(comp)
                        if fig_num:
                            print(f"📊 处理 Figure {fig_num}...")
                            img_filename = await download_figure(page, doi, int(fig_num), output_dir)
                            if img_filename:
                                downloaded_figures[fig_num] = (img_filename, caption)

                                md_content += f"\n## Figure {fig_num}\n\n"
                                md_content += f"![Figure {fig_num}]({img_filename})\n\n"
                                if caption:
                                    md_content += f"*{caption}*\n\n"
                    else:
                        text, _, _ = process_component(comp, doi, output_dir, downloaded_figures)
                        md_content += text

            # Process back matter
            if 'back' in data:
                back = data['back']
                md_content += "\n## References\n"
                for comp in back.get('components', []):
                    text, _, _ = process_component(comp, doi, output_dir, downloaded_figures)
                    md_content += text

            await browser.close()

        except Exception as e:
            print(f"⚠️  浏览器错误: {e}")

    # Save markdown
    if output_file:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(md_content)
        print(f"\n✅ Markdown已保存: {output_file}")

    return md_content

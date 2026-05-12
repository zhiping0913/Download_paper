"""
APS Journal Publisher Implementation
Handles extraction from American Physical Society journals (prl, pre, pra, etc.)
"""

import asyncio
from publisher.base import PublisherHandler
from core import extract_text_without_math
import re
import json
from pathlib import Path


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

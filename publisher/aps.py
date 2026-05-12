"""
APS Journal Publisher Implementation
Handles extraction from American Physical Society journals (prl, pre, pra, etc.)
"""

from publisher.base import PublisherHandler
from core import extract_text_without_math
import re
import json
from pathlib import Path


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
        # This will be implemented by calling extract_metadata_from_page()
        # from the main module during transition
        pass

    async def get_fulltext_url(self, doi: str) -> str:
        """Get URL for full article text API endpoint"""
        # APS fulltext endpoint format: /fulltext/{doi}
        return f"{self.base_url}/fulltext/{doi}"

    async def get_pdf_url(self, doi: str) -> str:
        """Construct PDF download URL"""
        return f"{self.base_url}/pdf/{doi}"

    async def get_supplemental_url(self, doi: str) -> str:
        """Construct supplemental materials URL"""
        return f"{self.base_url}/supplemental/{doi}"

    async def extract_references(self, html: str) -> list:
        """Parse references from HTML"""
        return extract_references_from_html(html)

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


def extract_figure_assets_from_fulltext(fulltext_data: dict, journal_prefix: str = 'prl') -> dict:
    """Extract figure URLs and captions from APS fulltext JSON API response

    Args:
        fulltext_data: The fulltext JSON data from APS API
        journal_prefix: The journal prefix (prl, pre, pra, etc.) - defaults to 'prl'

    Note:
        Figures are stored as 'asset' objects with type='figure' containing:
        - id: figure identifier (e.g., 'f1', 'f2')
        - caption: figure caption text (with MathML)
        - variants: dict with 'thumbnail', 'medium', 'large' URLs
    """
    figure_assets = {}

    def search_figures(obj):
        """Recursively search for figure objects in the JSON structure"""
        if isinstance(obj, dict):
            # Look for asset objects with type='figure'
            if obj.get('type') == 'figure' and obj.get('asset'):
                asset = obj['asset']
                fig_id = asset.get('id') or f"fig_{len(figure_assets) + 1}"
                caption = asset.get('caption', '')
                url = ""

                # Extract figure URL from asset.variants (prefer large, fallback to medium or thumbnail)
                variants = asset.get('variants', {})
                if isinstance(variants, dict):
                    url = variants.get('large') or variants.get('medium') or variants.get('thumbnail')

                # Convert relative URL to absolute URL if needed
                if url and url.startswith('/'):
                    url = f"https://journals.aps.org{url}"

                if url:  # Only add if we have a URL
                    figure_assets[fig_id] = {'caption': caption, 'url': url}

            # Recurse into all values (including 'components')
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    search_figures(value)

        elif isinstance(obj, list):
            for item in obj:
                search_figures(item)

    search_figures(fulltext_data)
    return figure_assets


def extract_supplemental_descriptions(supplemental_data: dict) -> dict:
    """Extract supplemental file descriptions from API response"""
    descriptions = {}
    try:
        if isinstance(supplemental_data, dict):
            for key, value in supplemental_data.items():
                if isinstance(value, dict) and 'description' in value:
                    descriptions[key] = value['description']
    except:
        pass
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

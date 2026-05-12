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
        from json_to_md_converter import convert_json_data_to_markdown

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

        # 清理LaTeX命令
        md_content = re.sub(r'\\mspace\{[^}]+\}', '', md_content)

        # ===== 后处理：在FIG. X. 后添加图片引用 =====
        # 只在add_figure_refs为True且成功下载图片时添加引用
        if add_figure_refs:
            # 匹配 "FIG. 1." 并在其后插入图片
            def add_figure_reference(match):
                fig_text = match.group(0)  # e.g., "FIG. 1."
                # 提取图片编号
                fig_match = re.search(r'FIG\.\s*(\d+)', fig_text)
                if fig_match:
                    fig_num = fig_match.group(1)
                    # 返回FIG文本，加上空行和图片引用
                    return f"{fig_text}\n\n![Figure {fig_num}](figure_{fig_num}.png)"
                return fig_text

            md_content = re.sub(r'FIG\.\s*\d+\.', add_figure_reference, md_content)

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
    """
    figure_assets = {}

    def search_figures(obj, path=""):
        if isinstance(obj, dict):
            # Look for objects with figure-related keys
            if obj.get('type') == 'fig':
                fig_id = obj.get('id') or f"fig_{len(figure_assets) + 1}"
                caption = ""
                url = ""

                # Extract caption from fig-caption
                for component in obj.get('components', []):
                    if component.get('type') == 'fig-caption':
                        caption_html = component.get('body', '')
                        caption = extract_text_without_math(caption_html)
                        break

                # Extract figure URL using correct journal prefix
                if 'id' in obj:
                    url = f"https://journals.aps.org/{journal_prefix}/article/{obj['id']}/figures/1/medium"

                if caption or url:
                    figure_assets[fig_id] = {'caption': caption, 'url': url}

            # Recurse into values
            for key, value in obj.items():
                if key not in ['body', 'components']:
                    search_figures(value, f"{path}/{key}")

        elif isinstance(obj, list):
            for item in obj:
                search_figures(item, path)

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

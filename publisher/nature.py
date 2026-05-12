"""
Nature Journal Publisher Implementation
Handles extraction from Nature and Nature-family journals (Nature Physics, Nature Materials, etc.)

工作流：
  1. complete_paper_extraction.py → 进入Nature页面 → 获取页面HTML
  2. extract_html_from_page() → 用 Playwright 捕获完整HTML
  3. extract_main_content_paragraphs() → 从HTML中抓取 <div class="main-content">
  4. convert_main_content_by_paragraph() → 按段落分割转换（避免Pandoc换行问题）
     - 按 </p> 标记分割成独立段落
     - 逐段用 Pandoc 转换为 Markdown
     - 移除每段内部换行
     - 简化引用格式 [N]
     - 重新组合段落
  5. 最终得到清洁的Markdown正文
"""

from publisher.base import PublisherHandler
from core import extract_text_without_math
import re
import json
from pathlib import Path
from typing import Optional, Dict, List
from bs4 import BeautifulSoup

# 导入转换函数
try:
    from json_to_md_converter import convert_html_to_markdown, cleanup_markdown
except ImportError:
    # 如果在publisher子目录中运行，调整导入路径
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from json_to_md_converter import convert_html_to_markdown, cleanup_markdown


class NatureHandler(PublisherHandler):
    """Handler for Nature and Springer Nature journals"""

    def __init__(self, journal_name: str = 'nature'):
        """
        Initialize Nature handler

        Args:
            journal_name: Journal name (nature, nature_physics, nature_materials, etc.)
        """
        self.journal_name = journal_name
        self.base_url = "https://www.nature.com"

    async def extract_metadata(self, page) -> dict:
        """Extract metadata from Nature article page

        Extracts from:
        - Meta tags (citation_*, dc.*, prism.* prefixes)
        - JSON-LD structured data
        - HTML DOM elements

        Returns:
            Dictionary with extracted metadata
        """
        metadata = {
            'title': None,
            'authors': [],
            'author_emails': [],
            'abstract': None,
            'journal': None,
            'year': None,
            'volume': None,
            'issue': None,
            'pages': None,
            'doi': None,
            'author_with_affiliations': [],
            'corresponding_author_emails': [],
            'references': [],
        }

        print("  🔍 Extracting metadata from Nature article...")

        # Extract all meta tags
        meta_data = await page.evaluate("""() => {
            const data = {};
            document.querySelectorAll('meta').forEach(meta => {
                const name = meta.getAttribute('name') || meta.getAttribute('property') || '';
                const content = meta.getAttribute('content') || '';
                if (name && content) {
                    data[name] = content;
                }
            });
            return data;
        }""")

        # Map meta tags to metadata fields
        metadata['title'] = meta_data.get('citation_title') or meta_data.get('dc.title')
        metadata['journal'] = meta_data.get('citation_journal_title', 'Nature')
        metadata['doi'] = (meta_data.get('citation_doi') or meta_data.get('prism.doi', '').replace('doi:', ''))

        # Parse publication date (format: 2026/04/22 or 2026-04-22)
        pub_date = meta_data.get('citation_online_date', '')
        if pub_date:
            # Handle both / and - separators
            date_parts = pub_date.replace('-', '/').split('/')
            if len(date_parts) >= 1:
                metadata['year'] = date_parts[0]

        # Get first author from meta tag
        if meta_data.get('citation_author'):
            metadata['authors'].append(meta_data['citation_author'])

        print(f"  ✅ Title: {metadata['title'][:60] if metadata['title'] else 'N/A'}...")
        print(f"  ✅ Journal: {metadata['journal']}")
        print(f"  ✅ DOI: {metadata['doi']}")

        # Extract abstract from JSON-LD
        json_ld_data = await page.evaluate("""() => {
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            for (let script of scripts) {
                try {
                    const data = JSON.parse(script.textContent);
                    if (data.mainEntity) {
                        return data.mainEntity;
                    }
                } catch (e) {}
            }
            return null;
        }""")

        if json_ld_data and 'description' in json_ld_data:
            metadata['abstract'] = json_ld_data['description']
            print(f"  ✅ Abstract: {metadata['abstract'][:60]}...")

        # Extract all authors from HTML DOM (more complete than meta tags)
        all_authors = await page.evaluate("""() => {
            const authors = [];
            const elements = document.querySelectorAll('[class*="author"]');
            let uniqueAuthors = new Set();

            elements.forEach(el => {
                const text = el.textContent.trim();
                if (text && text.length > 2 && text.length < 100) {
                    uniqueAuthors.add(text);
                }
            });

            return Array.from(uniqueAuthors).slice(0, 50);
        }""")

        if all_authors:
            metadata['authors'] = all_authors
            print(f"  ✅ Authors found: {len(metadata['authors'])}")

        return metadata

    async def get_fulltext_url(self, page) -> Optional[str]:
        """Nature doesn't have a separate fulltext API, content is on the page itself"""
        return page.url

    async def get_pdf_url(self, page) -> Optional[str]:
        """Find PDF download URL

        Nature articles typically have a PDF button/link that needs to be located
        """
        print("  🔍 Looking for PDF download link...")

        # Look for PDF download link - try multiple selectors
        selectors = [
            'a[href*=".pdf"]',
            'a[title*="PDF"]',
            '[class*="pdf-download"]',
            'a[href*="pdf"]',
            'button:has-text("PDF")',
            '[data-test*="pdf"]'
        ]

        for selector in selectors:
            try:
                pdf_link = await page.query_selector(selector)
                if pdf_link:
                    href = await pdf_link.get_attribute('href')
                    if href:
                        if not href.startswith('http'):
                            href = f"https://www.nature.com{href}"
                        print(f"  ✅ Found PDF: {href[:80]}...")
                        return href
            except:
                continue

        print("  ⚠️  PDF link not found (may require subscription)")
        return None

    async def get_supplemental_url(self, page) -> Optional[str]:
        """Find supplementary materials link"""
        print("  🔍 Looking for supplementary materials...")

        selectors = [
            'a[href*="supplement"]',
            'a[href*="supp"]',
            'a:has-text("Supplementary")',
            'a:has-text("Supplemental")',
            '[class*="supplementary"] a',
            '[class*="supplemental"] a'
        ]

        for selector in selectors:
            try:
                supp_link = await page.query_selector(selector)
                if supp_link:
                    href = await supp_link.get_attribute('href')
                    if href:
                        if not href.startswith('http'):
                            href = f"https://www.nature.com{href}"
                        print(f"  ✅ Found supplementary: {href[:80]}...")
                        return href
            except:
                continue

        print("  ⚠️  Supplementary materials link not found")
        return None

    async def extract_references(self, page) -> List[str]:
        """Parse references from HTML reference list"""
        print("  🔍 Extracting references...")

        references = await page.evaluate("""() => {
            const refs = [];
            const refItems = document.querySelectorAll('[class*="reference"] li, [class*="ref-item"]');

            refItems.forEach(item => {
                const text = item.textContent.trim();
                if (text && text.length > 10) {
                    refs.push(text);
                }
            });

            return refs.slice(0, 200);  // Limit to first 200
        }""")

        if references:
            print(f"  ✅ References found: {len(references)}")
        else:
            print("  ⚠️  No references found")

        return references

    async def get_figures(self, page) -> Dict[str, dict]:
        """Extract figure URLs and captions from HTML img tags"""
        print("  🔍 Extracting figures...")

        figures = {}

        # Find all figure elements
        figure_data = await page.evaluate("""() => {
            const figs = [];
            const elements = document.querySelectorAll('figure, [class*="figure"]');

            elements.forEach((fig, idx) => {
                // Get figure image
                const img = fig.querySelector('img');
                if (!img) return;

                let src = img.getAttribute('src') || img.getAttribute('data-src');
                if (!src) return;

                // Upgrade to high-res version if possible
                if (src.includes('media.springernature.com')) {
                    src = src.replace(/w\d+h\d+/, 'lw685');
                }

                // Convert to full URL if relative
                if (!src.startsWith('http')) {
                    src = 'https://www.nature.com' + src;
                }

                // Get figure caption
                let caption = '';
                const captionEl = fig.querySelector('figcaption, [class*="caption"]');
                if (captionEl) {
                    caption = captionEl.textContent.trim();
                }

                if (src) {
                    figs.push({
                        idx: idx + 1,
                        src: src,
                        caption: caption
                    });
                }
            });

            return figs.slice(0, 100);  // Limit to first 100 figures
        }""")

        for fig in figure_data:
            fig_key = f'fig_{fig["idx"]}'
            figures[fig_key] = {
                'caption': fig['caption'],
                'url': fig['src']
            }

        if figures:
            print(f"  ✅ Figures found: {len(figures)}")
        else:
            print("  ⚠️  No figures found")

        return figures

    def convert_to_markdown(self, metadata: dict, article_html: str = None,
                          add_figure_refs: bool = False) -> str:
        """Convert extracted data to Markdown

        Args:
            metadata: Paper metadata dict
            article_html: HTML content of article (from page.content())
            add_figure_refs: If True, add figure references in markdown

        Returns:
            Markdown formatted text
        """
        md_content = ""

        # ===== Title =====
        title = metadata.get('title') or "Academic Paper"
        md_content += f"# {title}\n\n"

        # ===== Authors =====
        if metadata.get('authors'):
            md_content += "## Authors\n\n"
            for author in metadata['authors'][:30]:  # Limit display to first 30
                md_content += f"- {author}\n"
            if len(metadata['authors']) > 30:
                md_content += f"- ... and {len(metadata['authors']) - 30} more authors\n"
            md_content += "\n"

        # ===== Publication Info =====
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

        # ===== Abstract =====
        if metadata.get('abstract'):
            md_content += "## Abstract\n\n"
            md_content += f"{metadata['abstract']}\n\n"
            md_content += "---\n\n"

        # ===== Article Content (Using Paragraph-by-Paragraph Conversion) =====
        if article_html:
            md_content += "## Article Content\n\n"
            try:
                # 使用段落级转换，避免Pandoc跨段落换行问题
                article_md = self.convert_main_content_by_paragraph(article_html)
                md_content += article_md
            except Exception as e:
                print(f"  ⚠️  Could not convert article HTML: {e}")
                md_content += "[Article HTML content available but conversion failed]\n"

        md_content += "\n---\n\n"

        # ===== References =====
        if metadata.get('references'):
            md_content += "## References\n\n"
            for i, ref in enumerate(metadata['references'][:100], 1):  # First 100
                md_content += f"[{i}] {ref}\n\n"
            if len(metadata['references']) > 100:
                md_content += f"\n[... and {len(metadata['references']) - 100} more references]\n"

        return md_content

    def extract_main_content_paragraphs(self, html_str: str) -> List[str]:
        """从HTML中提取 main-content div 里的所有段落

        Args:
            html_str: HTML字符串

        Returns:
            包含 <p>...</p> 的段落列表
        """
        soup = BeautifulSoup(html_str, 'html.parser')

        # Try multiple selectors in order of preference
        main_content_div = None
        selectors = [
            ('div', {'class': re.compile(r'main-content|article-body|article-content')}),
            ('article', {}),
            ('div', {'class': re.compile(r'content|body|article')}),
            ('main', {}),
        ]

        for tag, attrs in selectors:
            if attrs:
                main_content_div = soup.find(tag, attrs)
            else:
                main_content_div = soup.find(tag)
            if main_content_div:
                print(f"  ✓ 找到内容容器: {tag}")
                break

        if not main_content_div:
            # Last resort: find all paragraphs
            print("  ⚠️  未找到主要内容容器，尝试查找所有段落")
            paragraphs = soup.find_all('p')
            if paragraphs and len(paragraphs) > 5:
                # If we find substantial paragraphs, use them
                main_content_div = soup.new_tag('div')
                for p in paragraphs:
                    main_content_div.append(p)
            else:
                print("  ❌ 未找到足够的段落内容")
                return []

        # 获取HTML字符串
        main_html = str(main_content_div)

        # 按 </p> 分割段落，但保留完整的 <p>...</p> 标签
        p_pattern = r'<p[^>]*>(.*?)</p>'
        matches = re.finditer(p_pattern, main_html, re.DOTALL)

        paragraphs = [match.group(0) for match in matches]

        print(f"  ✅ 从 main-content 中提取了 {len(paragraphs)} 个段落")
        return paragraphs

    def convert_paragraph(self, p_html: str) -> str:
        """转换单个段落

        1. 用 Pandoc 转换为 Markdown
        2. 移除内部换行
        3. 清理 LaTeX 命令
        4. 简化引用格式
        5. 清理 HTML 属性

        Args:
            p_html: 段落的HTML字符串

        Returns:
            转换后的Markdown段落
        """
        try:
            # 1. 转换为Markdown
            md = convert_html_to_markdown(p_html)

            # 2. 清理LaTeX命令
            md = cleanup_markdown(md)

            # 3. 移除段落内的换行（将多个空格替换为单个空格）
            md = re.sub(r'\s+', ' ', md)

            # 4. 完全清理引用格式
            # Pandoc脚注格式: ^[9](#ref-CR9 "title...")^ → [9]
            md = re.sub(r'\^\[(\d+)\]\([^)]*\)\^', r'[\1]', md)
            # 处理嵌套括号的情况
            md = re.sub(r'\^\[(\d+)\][^\^]*\^', r'[\1]', md)

            # 5. 移除HTML属性
            md = re.sub(r'\{[^}]*\}', '', md)

            # 6. 清理转义的字符
            # 处理转义的星号：\*text\* → *text*
            md = re.sub(r'\\\*', '*', md)
            # 处理转义的波浪线：\~ → ~
            md = re.sub(r'\\\~', '~', md)

            # 7. 移除Pandoc语法
            md = re.sub(r'::: \{[^}]*\}\n?', '', md)
            md = re.sub(r':::\n?', '', md)

            # 清理首尾空格
            md = md.strip()

            return md

        except Exception as e:
            print(f"  ⚠️  段落转换错误: {str(e)[:50]}")
            return ""

    def convert_main_content_by_paragraph(self, html_str: str) -> str:
        """按段落分割转换 main-content

        关键工作流：
        1. 从HTML中提取 <div class="main-content">
        2. 按 </p> 分割成独立段落（保持原始结构）
        3. 逐个转换每个段落（避免Pandoc跨段落换行）
        4. 移除每段内部的不必要换行
        5. 简化引用格式为 [N]
        6. 用双换行重新组合段落

        Args:
            html_str: 完整的HTML页面字符串

        Returns:
            清洁的Markdown文本
        """
        print("  📖 按段落分割转换HTML...")

        # 1. 提取段落
        paragraphs = self.extract_main_content_paragraphs(html_str)
        if not paragraphs:
            return ""

        # 2. 逐段转换
        print(f"  ⚙️  逐段转换 {len(paragraphs)} 个段落...")
        converted_paragraphs = []

        for idx, p_html in enumerate(paragraphs, 1):
            md = self.convert_paragraph(p_html)

            if md:
                converted_paragraphs.append(md)

                # 每10段或前3段打印进度
                if idx <= 3 or idx % 10 == 0:
                    chars = len(md)
                    print(f"    ✓ 段落 {idx}: {chars} 字符")

        print(f"  ✅ 共转换 {len(converted_paragraphs)} 个有效段落")

        # 3. 组合段落（双换行分隔）
        final_markdown = "\n\n".join(converted_paragraphs)

        # 4. 最后清理
        # 移除过多的空行
        final_markdown = re.sub(r'\n\n\n+', '\n\n', final_markdown)

        return final_markdown


# ============================================================================
# Nature-Specific Extraction Functions
# ============================================================================

def extract_doi_from_nature_url(url: str) -> Optional[str]:
    """Extract DOI from Nature article URL

    Example: https://www.nature.com/articles/s41586-026-10400-2
    Returns: 10.1038/s41586-026-10400-2
    """
    match = re.search(r'articles/(s\d+\-[\d\-]+)', url)
    if match:
        article_id = match.group(1)
        return f"10.1038/{article_id}"
    return None


def parse_nature_meta_tags(meta_dict: dict) -> dict:
    """Parse Nature-specific meta tags into standard format"""
    return {
        'title': meta_dict.get('citation_title'),
        'doi': meta_dict.get('citation_doi', '').replace('doi:', ''),
        'journal': meta_dict.get('citation_journal_title'),
        'author': meta_dict.get('citation_author'),
        'author_institution': meta_dict.get('citation_author_institution'),
        'date': meta_dict.get('citation_online_date'),
        'abstract': meta_dict.get('dc.description'),
    }


def detect_nature_journal(url: str) -> Optional[str]:
    """Detect Nature journal type from URL

    Returns: 'nature', 'nature_physics', 'nature_materials', etc.
    """
    if 'nature.com/articles' in url:
        # Extract journal from article ID pattern or URL
        if 's41567' in url:
            return 'nature_physics'
        elif 's41563' in url:
            return 'nature_materials'
        elif 's41586' in url:
            return 'nature'
        else:
            return 'nature'  # default
    return None


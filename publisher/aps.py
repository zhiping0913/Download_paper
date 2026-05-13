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


async def extract_metadata_from_page(page) -> dict:
    """从页面meta标签提取完整元数据（作者、单位、摘要等）

    注意：meta标签中作者和机构是交错的，一个作者可能有多个机构
    规则：当遇到下一个作者前的所有机构都属于当前作者
    """

    metadata = {
        'title': None,
        'authors': [],
        'author_with_affiliations': [],  # 作者及其多个机构
        'corresponding_author_emails': [],  # 通讯作者邮箱列表
        'abstract': None,
        'journal': None,
        'publication_date': None,
        'doi': None,
        'volume': None,
        'issue': None,
        'pages': None,
        'year': None,
        'references': [],  # 新增：References列表
    }

    try:
        # 使用JavaScript提取所有meta标签和邮箱信息
        page_data = await page.evaluate(r"""() => {
            const result = {
                metas: [],
                email: null,
                abstract: null
            };

            // 提取所有citation_开头的meta标签
            document.querySelectorAll('meta').forEach(meta => {
                const name = meta.getAttribute('name') || meta.getAttribute('property');
                const content = meta.getAttribute('content');
                if (name && content && name.startsWith('citation_')) {
                    result.metas.push({name: name, content: content});
                }
            });

            // 提取摘要（从description或og:description）
            const descriptionMeta = document.querySelector('meta[name="description"], meta[property="og:description"]');
            if (descriptionMeta) {
                result.abstract = descriptionMeta.getAttribute('content');
            }

            // 查找邮箱 - 多种方法
            let email = null;
            let allEmails = [];

            // 方法0：在contrib-notes中查找（对应作者邮箱）
            const contribNotes = document.querySelectorAll('li[id^="n"], .contrib-notes li');
            if (contribNotes.length > 0) {
                for (let note of contribNotes) {
                    const text = note.innerText || note.textContent;
                    const emailMatch = text.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/);
                    if (emailMatch) {
                        allEmails.push(emailMatch[0]);
                        if (!email) email = emailMatch[0];
                    }
                }
            }

            // 方法1：在所有links中查找mailto:
            if (!email) {
                const mailtoLinks = document.querySelectorAll('a[href^="mailto:"]');
                if (mailtoLinks.length > 0) {
                    for (let link of mailtoLinks) {
                        const href = link.getAttribute('href');
                        const emailMatch = href.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/);
                        if (emailMatch) {
                            allEmails.push(emailMatch[0]);
                            if (!email) email = emailMatch[0];
                        }
                    }
                }
            }

            // 方法2：在所有href中查找email
            if (!email) {
                const allLinks = document.querySelectorAll('a[href*="@"]');
                for (let link of allLinks) {
                    const href = link.getAttribute('href');
                    const emailMatch = href.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/);
                    if (emailMatch) {
                        allEmails.push(emailMatch[0]);
                        if (!email) email = emailMatch[0];
                    }
                }
            }

            // 方法3：在page text中查找所有email
            if (!email) {
                const bodyText = document.body.innerText;
                const emailMatches = bodyText.match(/[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g);
                if (emailMatches) {
                    allEmails = [...new Set(emailMatches)];  // 去重
                    if (!email && allEmails.length > 0) email = allEmails[0];
                }
            }

            if (email) {
                result.email = email;
            }
            result.allEmails = allEmails;

            return result;
        }""")

        meta_data = page_data['metas']

        # 分离作者/机构和其他元数据
        meta_dict = {}
        author_affiliation_list = []  # 保持顺序的 (name, content, is_author) 列表

        for item in meta_data:
            if item['name'] == 'citation_author' or item['name'] == 'citation_author_institution':
                author_affiliation_list.append(item)
            else:
                if item['name'] not in meta_dict:
                    meta_dict[item['name']] = []
                meta_dict[item['name']].append(item['content'])

        # 解析作者和机构的对应关系
        # 逻辑：当遇到新的作者时，前面的机构都属于上一个作者
        current_author = None
        current_affiliations = []

        for item in author_affiliation_list:
            if item['name'] == 'citation_author':
                # 如果已经有上一个作者，保存它的机构
                if current_author is not None:
                    metadata['author_with_affiliations'].append({
                        'author': current_author,
                        'affiliations': current_affiliations
                    })
                    metadata['authors'].append(current_author)

                # 开始新的作者
                current_author = item['content']
                current_affiliations = []

            elif item['name'] == 'citation_author_institution':
                # 机构属于当前作者
                if current_author is not None:
                    current_affiliations.append(item['content'])

        # 不要忘记最后一个作者
        if current_author is not None:
            metadata['author_with_affiliations'].append({
                'author': current_author,
                'affiliations': current_affiliations
            })
            metadata['authors'].append(current_author)

        # 打印提取的信息
        if metadata['authors']:
            print(f"  ✓ 作者: {len(metadata['authors'])} 位")
            for i, item in enumerate(metadata['author_with_affiliations'][:3], 1):
                author = item['author']
                num_aff = len(item['affiliations'])
                print(f"     {i}. {author} ({num_aff}个机构)")
            if len(metadata['author_with_affiliations']) > 3:
                print(f"     ... 等 {len(metadata['author_with_affiliations'])-3} 位")

        # 提取标题
        if 'citation_title' in meta_dict:
            metadata['title'] = meta_dict['citation_title'][0]
            print(f"  ✓ 标题: {metadata['title'][:60]}...")

        # 提取摘要（优先使用citation_abstract，其次使用description）
        if 'citation_abstract' in meta_dict:
            metadata['abstract'] = meta_dict['citation_abstract'][0]
        elif page_data.get('abstract'):
            metadata['abstract'] = page_data['abstract']

        if metadata['abstract']:
            print(f"  ✓ 摘要: {len(metadata['abstract'])} 字符")

        # 提取通讯作者邮箱（可能有多个）
        if page_data.get('allEmails'):
            metadata['corresponding_author_emails'] = list(dict.fromkeys(page_data['allEmails']))  # 去重并保持顺序
            print(f"  📧 通讯作者邮箱: {', '.join(metadata['corresponding_author_emails'])}")
        elif page_data.get('email'):
            metadata['corresponding_author_emails'] = [page_data['email']]
            print(f"  📧 通讯作者邮箱: {page_data['email']}")

        # 提取期刊
        if 'citation_journal_title' in meta_dict:
            metadata['journal'] = meta_dict['citation_journal_title'][0]
            print(f"  ✓ 期刊: {metadata['journal']}")

        # 提取DOI
        if 'citation_doi' in meta_dict:
            metadata['doi'] = meta_dict['citation_doi'][0]

        # 提取发表日期
        if 'citation_publication_date' in meta_dict:
            metadata['publication_date'] = meta_dict['citation_publication_date'][0]
            print(f"  ✓ 发表日期: {metadata['publication_date']}")

        # 提取年份
        if 'citation_year' in meta_dict:
            metadata['year'] = meta_dict['citation_year'][0]

        # 提取卷号
        if 'citation_volume' in meta_dict:
            metadata['volume'] = meta_dict['citation_volume'][0]

        # 提取期号
        if 'citation_issue' in meta_dict:
            metadata['issue'] = meta_dict['citation_issue'][0]

        # 提取页码
        if 'citation_firstpage' in meta_dict:
            metadata['pages'] = meta_dict['citation_firstpage'][0]
            if 'citation_lastpage' in meta_dict:
                metadata['pages'] += f"-{meta_dict['citation_lastpage'][0]}"

    except Exception as e:
        print(f"⚠️  提取meta标签时出错: {e}")
        import traceback
        traceback.print_exc()

    return metadata


def extract_references_from_html(html: str) -> list:
    """从abstract页面HTML提取References列表（包含DOI链接）"""
    try:
        # 查找 <ol class="references">...</ol>
        ref_match = re.search(
            r'<ol\s+class=["\']?references["\']?\s*>(.+?)</ol>',
            html,
            re.DOTALL
        )

        if not ref_match:
            return []

        refs_html = ref_match.group(1)
        references = []

        # 解析每个 <li> 为一条reference
        for li_match in re.finditer(r'<li[^>]*>(.+?)</li>', refs_html, re.DOTALL):
            ref_html = li_match.group(1)

            # 提取链接（如果存在）
            link_match = re.search(r'<a\s+href=["\']([^"\']+)["\']', ref_html)
            link_url = link_match.group(1) if link_match else None

            # 清理HTML标签，保留文本
            ref_text = re.sub(r'<[^>]+>', '', ref_html)
            # 解码HTML实体
            ref_text = unescape(ref_text)
            # 清理多余空格
            ref_text = re.sub(r'\s+', ' ', ref_text).strip()

            if ref_text:
                # 如果有链接，格式为 [text](url)；否则只有文本
                if link_url:
                    ref_entry = f"[{ref_text}]({link_url})"
                else:
                    ref_entry = ref_text
                references.append(ref_entry)

        return references
    except Exception as e:
        print(f"  ⚠️  提取References失败: {e}")
        return []


async def aps_extract_metadata_from_page(page) -> dict:
    """从页面meta标签提取完整元数据（作者、单位、摘要等）- APS 专用"""
    return await extract_metadata_from_page(page)


def aps_extract_references_from_html(html: str) -> list:
    """从HTML提取References - APS 专用"""
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

    async def _capture_network_data(self, page, url: str) -> dict:
        """Monitor network requests and capture JSON API responses

        Returns:
            dict with keys: 'json_responses', 'document', 'timeline', 'abstract_html',
                           'fulltext_data', 'supplemental_data', 'journal_prefix'
        """
        captured = {
            'json_responses': [],
            'document': None,
            'timeline': [],
            'abstract_html': None,      # Save abstract page HTML
            'fulltext_data': None,      # Save fulltext JSON (contains text and Acknowledgements)
            'supplemental_data': None,  # Save supplemental information
            'journal_prefix': None,     # Journal prefix extracted from URL (prl, pre, pra, etc.)
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

            # Capture HTML document (including abstract page)
            if rtype == 'document' and status == 200:
                try:
                    html = await response.text()

                    # Extract journal prefix from URL (prl, pre, pra, etc.)
                    if 'journals.aps.org/' in url_str and not captured['journal_prefix']:
                        match = re.search(r'journals\.aps\.org/([a-z]+)/', url_str)
                        if match:
                            captured['journal_prefix'] = match.group(1)
                            print(f"  ✓ 识别期刊: {captured['journal_prefix']}")

                    # Save abstract page HTML for References extraction
                    if '/abstract/' in url_str or '/prl/abstract/' in url_str:
                        captured['abstract_html'] = html
                        print(f"  ✓ 保存abstract HTML: {len(html)} 字节")

                    # Save main HTML document to file
                    captured['document'] = {
                        'url': url_str,
                        'timestamp': ts,
                        'size': len(html),
                    }

                    # Save HTML file to OUTPUT_DIR
                    output_dir = Path("captured_data")
                    output_dir.mkdir(exist_ok=True)
                    html_filename = f"page_{len(captured['json_responses']):03d}.html"
                    html_path = output_dir / html_filename
                    with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(html)
                    captured['document']['file'] = str(html_path)

                    print(f"  ✓ HTML文档: {len(html)} 字节")
                    print(f"    保存到: {html_filename}")
                except:
                    pass

            # Capture JSON/API responses
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

                            output_dir = Path("captured_data")
                            output_dir.mkdir(exist_ok=True)
                            jpath = output_dir / f"api_response_{len(captured['json_responses']):03d}.json"
                            with open(jpath, 'w', encoding='utf-8') as f:
                                json.dump(jdata, f, indent=2, ensure_ascii=False)

                            captured['json_responses'].append({
                                'url': url_str,
                                'timestamp': ts,
                                'size': len(jstr),
                                'file': str(jpath),
                            })

                            # Save fulltext and supplemental data specially
                            if '/fulltext/' in url_str:
                                captured['fulltext_data'] = jdata
                                print(f"  ✓ 保存fulltext数据: {len(jstr)} 字节")
                            elif '/supplemental/' in url_str:
                                captured['supplemental_data'] = jdata
                                print(f"  ✓ 保存supplemental数据: {len(jstr)} 字节")
                except:
                    pass

        page.on("response", handle_response)

        # Navigate to URL
        print(f"📄 访问: {url}")
        print("=" * 80)

        try:
            await page.goto(url, wait_until='networkidle', timeout=60000)
            print("✓ 页面加载完成")
        except Exception as e:
            print(f"⚠️  {type(e).__name__}: {str(e)[:100]}")

        # Wait for additional requests
        await asyncio.sleep(3)

        return captured

    async def extract_all(self, page, doi: str) -> dict:
        """Execute complete extraction flow

        Returns:
            dict with keys: 'metadata', 'links', 'fulltext_data', 'journal_prefix'
            where 'links' contains: 'pdf_url', 'figure_urls', 'supplemental_urls'
        """
        # 1. Extract metadata
        metadata = await self.extract_metadata(page)

        # 2. Capture network data to get JSON
        url = f"https://doi.org/{doi}"
        captured = await self._capture_network_data(page, url)

        # 3. Get fulltext data
        fulltext_data = captured.get('fulltext_data')
        if not fulltext_data and captured.get('json_responses'):
            try:
                json_file = captured['json_responses'][0]['file']
                with open(json_file, 'r', encoding='utf-8') as f:
                    fulltext_data = json.load(f)
            except:
                fulltext_data = {}

        # 4. Extract all links from fulltext data
        links = {
            'pdf_url': f"https://journals.aps.org/{self.journal_prefix}/pdf/{doi}",
            'figure_urls': extract_figure_assets_from_fulltext(fulltext_data),
            'supplemental_urls': []
        }

        # 5. Get supplemental links if available
        try:
            supp_links, supp_descriptions = await get_supplemental_links(page, doi, self.journal_prefix)
            links['supplemental_urls'] = supp_links
            links['supplemental_descriptions'] = supp_descriptions
        except:
            pass

        return {
            'metadata': metadata,
            'links': links,
            'fulltext_data': fulltext_data,
            'journal_prefix': self.journal_prefix
        }

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



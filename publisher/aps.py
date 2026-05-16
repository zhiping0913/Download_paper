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

from publisher.base import PublisherHandler
from core.network_capture import setup_response_capture
from core.utilities import fetch_semanticscholar, _build_bibtex_from_s2
from json_to_md_converter import mathml_to_latex_pandoc, extract_text_without_math
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup


# ============================================================================
# APS 特定函数 - 从 complete_paper_extraction.py 提取
# ============================================================================
# 注意：这些是从 complete_paper_extraction.py 中提取的 APS 专用函数
# 保持原有逻辑，避免修改


def _clean_aps_reference_text(text: str) -> str:
    """Normalize APS reference text extracted from the abstract page."""
    text = re.sub(r'\s+', ' ', text or '').strip()
    text = re.sub(r'\s+([,.;:])', r'\1', text)
    text = re.sub(r'\(\s+', '(', text)
    text = re.sub(r'\s+\)', ')', text)
    return re.sub(r'\s+', ' ', text).strip()


def _extract_aps_references_from_html(html_content: str) -> list:
    """Extract references from APS abstract HTML.

    APS references are rendered as:
    <ol class="references"><li id="c1">...</li></ol>
    """
    if not html_content:
        return []

    soup = BeautifulSoup(html_content, 'html.parser')
    references_list = soup.select_one('ol.references')
    if not references_list:
        return []

    for tag in references_list(['script', 'style', 'noscript', 'svg']):
        tag.decompose()

    references = []
    for item in references_list.find_all('li', id=re.compile(r'^c\d+$'), recursive=False):
        text = _clean_aps_reference_text(item.get_text(' ', strip=True))
        if len(text) < 20:
            continue

        doi_link = item.find('a', href=re.compile(r'https?://(?:dx\.)?doi\.org/10\.'))
        if doi_link:
            href = doi_link.get('href', '').strip()
            if href and href not in text:
                text = f"{text} DOI: {href}"

        references.append(text)

    return references


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


class APSHandler(PublisherHandler):
    """Handler for American Physical Society (APS) journals"""

    def __init__(self, journal_prefix: str = 'prl', page=None, captured_data_dir=None, doi: str = None):
        """
        Initialize APS handler

        Args:
            journal_prefix: Journal code (prl, pre, pra, prb, etc.)
        """
        super().__init__(page=page, captured_data_dir=captured_data_dir, doi=doi)
        self.journal_prefix = journal_prefix
        self.base_url = f"https://journals.aps.org/{journal_prefix}"

    async def extract_metadata(self, page) -> dict:
        """Extract metadata from APS abstract page"""
        return await extract_metadata_from_page(page)

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
        return _extract_aps_references_from_html(html)

    async def get_figures(self, json_data: dict) -> dict:
        """Extract figure URLs and captions from APS JSON"""
        return extract_figure_assets_from_fulltext(json_data)

    def setup_network_capture(self, page=None, doi: str = None):
        """Set up network event listener to capture responses

        Call this before page.goto(), so it captures all subsequent network traffic.
        Returns a dict that will be populated with captured data.

        Args:
            page: Playwright page object
            doi: DOI for organizing output files

        Returns:
            dict that will be populated with captured data
        """
        page = page or self.page
        doi = doi or self.doi
        if page is None:
            raise ValueError("APSHandler.setup_network_capture() requires a Playwright page")
        if doi is None:
            raise ValueError("APSHandler.setup_network_capture() requires a DOI")

        self.configure(page=page, doi=doi)

        output_dir = self.captured_data_dir or Path("captured_data") / doi.replace('/', '_')
        captured = {
            'json_responses': [],
            'document': None,
            'documents': [],
            'timeline': [],
            'abstract_html': None,      # Save abstract page HTML
            'fulltext_data': None,      # Save fulltext JSON (contains text and Acknowledgements)
            'supplemental_data': None,  # Save supplemental information
            'journal_prefix': None,     # Journal prefix extracted from URL (prl, pre, pra, etc.)
        }

        def should_save_json(response, jdata, jstr):
            kws = ['abstract', 'article', 'fulltext', 'front', 'back']
            has_paper = any(kw in jstr.lower() for kw in kws)
            return has_paper or len(jstr) > 2000

        def on_document(response, html, entry, captured):
            url_str = response.url
            if 'journals.aps.org/' in url_str and not captured['journal_prefix']:
                match = re.search(r'journals\.aps\.org/([a-z]+)/', url_str)
                if match:
                    captured['journal_prefix'] = match.group(1)
                    print(f"  ✓ 识别期刊: {captured['journal_prefix']}")

            # APS pages can load iframe documents; keep only the article page.
            is_article_html = doi in html or 'citation_doi' in html
            if '/abstract/' in url_str and is_article_html:
                captured['abstract_html'] = html
                print(f"  ✓ 保存abstract HTML: {len(html)} 字节")

        def on_json(response, jdata, jstr, entry, captured):
            url_str = response.url
            if '/fulltext/' in url_str:
                captured['fulltext_data'] = jdata
                print(f"  ✓ 保存fulltext数据: {len(jstr)} 字节")
            elif '/supplemental/' in url_str:
                captured['supplemental_data'] = jdata
                print(f"  ✓ 保存supplemental数据: {len(jstr)} 字节")

        return setup_response_capture(
            page,
            output_dir,
            captured=captured,
            json_should_save=should_save_json,
            on_document=on_document,
            on_json=on_json,
        )

    async def _capture_network_data(self, page, url: str) -> dict:
        """Monitor network requests and capture JSON API responses

        DEPRECATED: Use setup_network_capture() in Step 1 instead.
        This method is kept for backward compatibility.

        Returns:
            dict with keys: 'json_responses', 'document', 'timeline', 'abstract_html',
                           'fulltext_data', 'supplemental_data', 'journal_prefix'
        """
        # Extract DOI from URL
        doi = url.replace('https://doi.org/', '').split('?')[0]

        # Set up network capture (listener will capture from this point onward)
        captured = self.setup_network_capture(page, doi)

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

    def load_captured_data(self) -> dict:
        """Load APS response data from the configured captured data directory."""
        captured = {
            'json_responses': [],
            'document': None,
            'timeline': [],
            'abstract_html': None,
            'fulltext_data': None,
            'supplemental_data': None,
            'journal_prefix': self.journal_prefix,
        }

        if not self.captured_data_dir or not self.captured_data_dir.exists():
            return captured

        html_files = sorted(self.captured_data_dir.glob("*.html"))
        html_files.sort(key=lambda p: (
            'abstract' not in p.name.lower(),
            'journals.aps.org' not in p.name.lower(),
            p.name,
        ))
        if html_files:
            for html_path in html_files:
                try:
                    html = html_path.read_text(encoding='utf-8')
                    if captured['abstract_html'] is None and ('citation_doi' in html or 'ol class="references"' in html):
                        captured['abstract_html'] = html
                    if captured['document'] is None:
                        captured['document'] = {
                            'file': str(html_path),
                            'size': len(html),
                        }
                except Exception:
                    pass

        for json_path in sorted(self.captured_data_dir.glob("api_response_*.json")):
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                jstr = json.dumps(data)
                item = {
                    'file': str(json_path),
                    'size': len(jstr),
                }
                captured['json_responses'].append(item)

                filename = json_path.name.lower()
                content = jstr.lower()
                if captured['fulltext_data'] is None and ('fulltext' in filename or 'fulltext' in content):
                    captured['fulltext_data'] = data
                elif captured['supplemental_data'] is None and ('supplemental' in filename or 'supplemental' in content):
                    captured['supplemental_data'] = data
            except Exception:
                continue

        if captured['fulltext_data'] is None and captured['json_responses']:
            try:
                with open(captured['json_responses'][0]['file'], 'r', encoding='utf-8') as f:
                    captured['fulltext_data'] = json.load(f)
            except Exception:
                pass

        return captured

    async def extract_all(self, page=None, doi: str = None, captured: dict = None) -> dict:
        """Execute complete extraction flow

        Args:
            page: Playwright page object (should already be navigated to DOI)
            doi: DOI of the paper
            captured: Optional dict with already-captured network data from setup_network_capture()
                     If not provided, will call _capture_network_data() for backward compatibility

        Returns:
            dict with keys: 'metadata', 'links', 'fulltext_data', 'journal_prefix'
            where 'links' contains: 'pdf_url', 'figure_urls', 'supplemental_urls'
        """
        page = page or self.page
        doi = doi or self.doi
        if page is None:
            raise ValueError("APSHandler.extract_all() requires a Playwright page")
        if doi is None:
            raise ValueError("APSHandler.extract_all() requires a DOI")

        self.configure(page=page, doi=doi)

        # 1. Extract metadata
        metadata = await self.extract_metadata(page)

        # 2. Use provided captured data or capture it ourselves
        if captured is None:
            captured = self.load_captured_data()
            if not captured.get('fulltext_data') and not captured.get('json_responses'):
                # Backward compatibility: capture network data if not provided
                url = f"https://doi.org/{doi}"
                captured = await self._capture_network_data(page, url)
        else:
            # Network capture is already running, just wait for additional requests
            await asyncio.sleep(3)

        # 3. Extract references from the headed APS abstract page.
        if not metadata.get('references'):
            reference_html_candidates = []
            if captured.get('abstract_html'):
                reference_html_candidates.append(captured['abstract_html'])
            try:
                current_html = await page.content()
                if current_html:
                    reference_html_candidates.append(current_html)
            except Exception:
                pass

            for html in reference_html_candidates:
                references = await self.extract_references(html)
                if references:
                    metadata['references'] = references
                    print(f"  ✓ 参考文献: {len(references)} 条")
                    # Generate BibTeX entries via Semantic Scholar
                    bibtex_refs = []
                    for ref_text in references:
                        doi_match = re.search(r'(10\.\d{4,}/[^\s"\'\]]+)', ref_text)
                        if doi_match:
                            doi_ref = doi_match.group(1).rstrip('.')
                            try:
                                s2_data = fetch_semanticscholar(doi_ref)
                                if s2_data and s2_data.get('title'):
                                    bibtex_refs.append(_build_bibtex_from_s2(s2_data, doi_ref))
                                else:
                                    bibtex_refs.append(None)
                            except Exception:
                                bibtex_refs.append(None)
                        else:
                            bibtex_refs.append(None)
                    metadata['_refs_bibtex'] = bibtex_refs
                    break

        # 4. Get fulltext data
        fulltext_data = captured.get('fulltext_data')
        if not fulltext_data and captured.get('json_responses'):
            try:
                json_file = captured['json_responses'][0]['file']
                with open(json_file, 'r', encoding='utf-8') as f:
                    fulltext_data = json.load(f)
            except:
                fulltext_data = {}

        # 5. Extract all links from fulltext data
        links = {
            'pdf_url': f"https://journals.aps.org/{self.journal_prefix}/pdf/{doi}",
            'figure_urls': extract_figure_assets_from_fulltext(fulltext_data),
            'supplemental_urls': []
        }

        # 6. Get supplemental links if available
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

    def convert_to_markdown(self, metadata: dict, fulltext_json: dict,
                            add_figure_refs: bool = False,
                            figure_filenames: dict = None, **kwargs) -> str:
        """Convert extracted data to Markdown using fulltext JSON

        Args:
            metadata: Paper metadata dict
            fulltext_json: Full text JSON data
            add_figure_refs: If True, add figure references in markdown. If False, skip them (default).
                            Use False when figures haven't been downloaded yet.
            figure_filenames: Mapping of figure number to downloaded local filename.
        """
        from json_to_md_converter import convert_json_data_to_markdown, cleanup_markdown

        figure_filenames = figure_filenames or {}
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

        # ===== 补充材料 =====
        supp_urls = kwargs.get('supplemental_urls', [])
        supp_descriptions = kwargs.get('supplemental_descriptions', {})
        supp_downloads = kwargs.get('supplemental_downloads', [])
        if supp_descriptions:
            md_content += "---\n\n## Supplemental Material\n\n"
            for filename, desc in supp_descriptions.items():
                # Find matching downloaded file
                downloaded_file = ''
                for df in supp_downloads:
                    if filename in df:
                        downloaded_file = df
                        break
                md_content += f"{desc}\n\n"
                if downloaded_file:
                    md_content += f"**Downloaded:** [{downloaded_file}]({downloaded_file})\n\n"
        elif supp_urls:
            md_content += "---\n\n## Supplemental Material\n\n"
            for url in supp_urls:
                if isinstance(url, dict):
                    md_content += f"- [{url.get('text', url.get('url', 'Supplement'))}]({url.get('url', '')})\n"
                else:
                    md_content += f"- [Supplemental Material]({url})\n"
            md_content += "\n"

        md_content += "\n---\n\n"

        # ===== 参考文献 =====
        if metadata.get('references'):
            md_content += "## References\n\n"
            bibtex_refs = metadata.get('_refs_bibtex', [])
            for i, ref in enumerate(metadata['references']):
                idx = i + 1
                md_content += f"[{idx}] {ref}\n\n"
                if i < len(bibtex_refs) and bibtex_refs[i]:
                    md_content += f"```bibtex\n{bibtex_refs[i]}\n```\n\n"

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
                    img_filename = figure_filenames.get(fig_num, f"figure_{fig_num}.png")
                    return f"{line}\n\n![Figure {fig_num}]({img_filename})"
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


def extract_figure_assets_from_fulltext(fulltext_data: dict) -> dict:
    """
    从fulltext API响应中提取图片资源信息
    返回: {fig_id: {"url": "...", "caption": "..."}, ...}

    Note: 确保返回完整的URL（如果是相对路径则自动补全）
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
                    # 如果是相对路径，构建完整URL
                    if fig_url.startswith('/'):
                        fig_url = f"https://journals.aps.org{fig_url}"

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

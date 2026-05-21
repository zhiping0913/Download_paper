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
from html_to_md_converter import cleanup_markdown, convert_html_to_markdown, mathml_to_latex_pandoc, remove_newlines_in_paragraph
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


async def get_supplemental_links(page, doi: str, journal_prefix: str = None,
                                 captured_data_dir: Path = None) -> tuple:
    """获取补充材料的所有下载链接和描述信息

    Args:
        page: Playwright page object
        doi: Paper DOI
        journal_prefix: APS journal prefix (e.g., 'prl', 'pre')
        captured_data_dir: If set, save supplemental page HTML to this directory

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

        # 保存补充材料页面HTML
        if captured_data_dir:
            try:
                supp_html = await page.content()
                supp_file = Path(captured_data_dir) / "supplemental.html"
                with open(supp_file, 'w', encoding='utf-8') as f:
                    f.write(supp_html)
                print(f"  ✓ 补充材料页面已保存: {supp_file}")
            except Exception as e:
                print(f"  ⚠️  保存补充材料页面失败: {e}")

        # 提取所有链接 — APS 补充材料页面上每个文件对应一个 <a data-id="filename">
        links_js = """
        () => {
            const links = [];
            document.querySelectorAll('a[data-id]').forEach(a => {
                const href = a.getAttribute('href');
                const filename = a.getAttribute('data-id');
                if (href && filename) {
                    links.push({
                        text: filename,
                        href: href,
                        url: new URL(href, window.location.href).href
                    });
                }
            });
            return links;
        }
        """

        supp_links = await page.evaluate(links_js)

        # 从页面HTML中提取补充材料描述
        descriptions_js = """
        () => {
            const descriptions = {};
            document.querySelectorAll('a[data-id]').forEach(a => {
                const filename = a.getAttribute('data-id');
                if (!filename) return;

                // 查找文件项容器中的描述文本
                let item = a.closest('[class*="supplemental"]') || a.closest('li') || a.parentElement;
                if (item) {
                    const text = item.innerText || item.textContent;
                    // 去除文件名自身，剩余部分作为描述
                    let desc = text.replace(filename, '').trim();
                    if (desc.length > 5) {
                        descriptions[filename] = desc;
                    }
                }
            });
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

        # 5. Update journal_prefix from captured data (detected from URL) or DOI
        detected_prefix = captured.get('journal_prefix')
        if not detected_prefix:
            # Fallback: extract from DOI: 10.1103/PhysRevApplied.18.024026
            doi_match = re.search(r'10\.1103/([A-Za-z]+)', doi or '')
            if doi_match:
                journal_name = doi_match.group(1)
                APS_JOURNAL_PREFIXES = {
                    'PhysRevLett': 'prl',
                    'PhysRevApplied': 'prapplied',
                    'PhysReviewX': 'prx',
                    'PhysRevA': 'pra',
                    'PhysRevB': 'prb',
                    'PhysRevC': 'prc',
                    'PhysRevD': 'prd',
                    'PhysRevE': 'pre',
                    'PhysRevResearch': 'prresearch',
                    'PhysRevFluids': 'prfluids',
                    'PhysRevMaterials': 'prmaterials',
                    'PhysRevAccelBeams': 'prab',
                    'PhysRevPhysEducRes': 'prper',
                    'PhysRevSTAB': 'prstab',
                    'PhysRevSTPER': 'prstper',
                    'RevModPhys': 'rmp',
                }
                mapped = APS_JOURNAL_PREFIXES.get(journal_name)
                if mapped:
                    detected_prefix = mapped
        if detected_prefix:
            self.journal_prefix = detected_prefix
            print(f"  ✓ 期刊前缀: {self.journal_prefix}")

        # 6. Extract PDF URL from <a class="sm-primary-button">, fallback to journal pattern
        pdf_url = None
        try:
            pdf_url = await page.evaluate("""
                () => {
                    const btn = document.querySelector('a.sm-primary-button[href*="/pdf/"]');
                    if (!btn) return null;
                    const href = btn.getAttribute('href');
                    if (!href) return null;
                    if (href.startsWith('http')) return href;
                    return new URL(href, window.location.origin).href;
                }
            """)
        except:
            pass

        if not pdf_url:
            pdf_url = f"https://journals.aps.org/{self.journal_prefix}/pdf/{doi}"

        links = {
            'pdf_url': pdf_url,
            'figure_urls': extract_figure_assets_from_fulltext(fulltext_data),
            'supplemental_urls': []
        }

        # 6. Get supplemental links if available
        try:
            supp_links, supp_descriptions = await get_supplemental_links(
                page, doi, self.journal_prefix, captured_data_dir=self.captured_data_dir
            )
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
        from html_to_md_converter import cleanup_markdown

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

        # 跨发布商通用的清理 (移到 html_to_md_converter.cleanup_markdown)
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


# ============================================================================
# APS Paper Conversion Functions
# ============================================================================
# These functions traverse the APS fulltext JSON structure and convert it
# to Markdown. They were moved from html_to_md_converter.py.


def traverse_json_recursive(data, depth=0, parent_type=None, skip_section_header=False):
    """
    递归遍历JSON结构，生成Markdown
    skip_section_header: 是否跳过 front/back 等section标题
    """
    md_output = []

    if isinstance(data, dict):
        # 处理单个对象

        # 特殊处理图片: 在FIG标记后添加图片引用
        if data.get("type") == "fig":
            fig_id = data.get("id", "")

            # 获取图片标题(caption)
            caption_text = ""
            if "components" in data and isinstance(data["components"], list):
                for component in data["components"]:
                    if component.get("type") == "fig-caption":
                        caption_text = component.get("body", "")
                        break

            # 从caption中提取图片编号 (e.g., "FIG. 1." or "Fig. 1." -> "1")
            fig_match = re.search(r'[Ff][Ii][Gg]\.\s*(\d+)', caption_text)
            if fig_match:
                fig_num = fig_match.group(1)
                # 添加图标记和图片引用
                md_text = convert_html_to_markdown(caption_text)
                md_text = remove_newlines_in_paragraph(md_text, "", "fig-caption")
                md_text = re.sub(r'\[\]\{#[^}]*\}', '', md_text).strip()

                if md_text:
                    # 查找图文本后的位置，插入图片引用
                    # 在第一行(通常是"FIG. X." 或 "Fig. X.")后插入图片
                    lines = md_text.split('\n')
                    if lines and re.search(r'[Ff][Ii][Gg]\.\s*\d+', lines[0]):
                        # 在FIG行后插入空行和图片引用
                        md_output.append(f"{lines[0]}\n\n")
                        md_output.append(f"![Figure {fig_num}](figure_{fig_num}.png)\n\n")
                        # 添加剩余的caption文本
                        if len(lines) > 1:
                            remaining = '\n'.join(lines[1:]).strip()
                            if remaining:
                                md_output.append(f"{remaining}\n\n")
                    else:
                        md_output.append(f"{md_text}\n\n")
            return "".join(md_output)

        # 如果有body，转换它
        if "body" in data and data["body"]:
            klass = data.get("klass", "")
            body_type = data.get("type", "")

            # 转换HTML到Markdown
            md_text = convert_html_to_markdown(data["body"])

            # 移除换行
            md_text = remove_newlines_in_paragraph(md_text, klass, body_type)

            # 过滤掉空标记如 []{#acknowledgements}
            md_text = re.sub(r'\[\]\{#[^}]*\}', '', md_text).strip()

            if md_text:
                # 根据type和klass添加适当的标记
                if body_type == "p" and klass == "article-fulltext-paragraph":
                    md_output.append(f"{md_text}\n\n")
                elif body_type == "h1":
                    md_output.append(f"# {md_text}\n\n")
                elif body_type == "h2":
                    md_output.append(f"## {md_text}\n\n")
                elif body_type == "h3":
                    md_output.append(f"### {md_text}\n\n")
                else:
                    md_output.append(f"{md_text}\n\n")

        # 递归处理嵌套的components
        if "components" in data and isinstance(data["components"], list):
            for component in data["components"]:
                nested_md = traverse_json_recursive(
                    component,
                    depth + 1,
                    parent_type=data.get("type"),
                    skip_section_header=True
                )
                md_output.append(nested_md)

        # 处理其他可能的嵌套结构
        for key, value in data.items():
            if key not in ["body", "components", "id", "type", "klass", "sectioned", "expandable", "media", "style"]:
                if isinstance(value, (dict, list)):
                    nested_md = traverse_json_recursive(value, depth, parent_type, skip_section_header=True)
                    if nested_md.strip():
                        # 跳过 front/back 等section header
                        if key not in ["front", "back"] and not skip_section_header:
                            md_output.append(f"**{key}:**\n")
                        md_output.append(nested_md)

    elif isinstance(data, list):
        # 处理数组
        for item in data:
            item_md = traverse_json_recursive(item, depth, parent_type)
            md_output.append(item_md)

    return "".join(md_output)


def convert_json_data_to_markdown(data: dict) -> str:
    """
    将JSON数据转换为Markdown文本

    Args:
        data: 从APS API返回的JSON数据对象

    Returns:
        转换后的Markdown文本
    """
    return traverse_json_recursive(data, skip_section_header=False)


def extract_text_without_math(html_str: str) -> str:
    """Extract text and convert inline formulas"""
    def replace_inline_formula(match):
        math_section = match.group(0)
        math_match = re.search(r'<math[^>]*>.*?</math>', math_section, re.DOTALL)
        if math_match:
            math_html = math_match.group(0)
            latex = mathml_to_latex_pandoc(math_html)
            if latex:
                return latex
        return match.group(0)

    result = re.sub(
        r'<span class="inline-formula">[^<]*<math[^>]*>.*?</math>[^<]*</span>',
        replace_inline_formula,
        html_str,
        flags=re.DOTALL
    )

    result = re.sub(r'<button[^>]*>.*?</button>', '', result, flags=re.DOTALL)
    result = re.sub(r'<span[^>]*>', '', result)
    result = re.sub(r'</span>', '', result)
    result = unescape(result)
    result = re.sub(r'\s+', ' ', result).strip()
    return result


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

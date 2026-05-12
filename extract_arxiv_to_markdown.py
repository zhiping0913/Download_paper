#!/usr/bin/env python3
"""
从arXiv获取论文PDF并转换为Markdown
支持Nature论文的合法开放获取版本转换
"""

import asyncio
import re
import subprocess
import tempfile
from pathlib import Path
import aiohttp
from playwright.async_api import async_playwright


async def get_arxiv_pdf_url(arxiv_id: str) -> str:
    """获取arXiv论文PDF URL"""
    # arXiv PDF格式: https://arxiv.org/pdf/{arxiv_id}
    return f"https://arxiv.org/pdf/{arxiv_id}.pdf"


async def download_arxiv_pdf(arxiv_id: str, output_path: str) -> bool:
    """下载arXiv PDF"""
    url = await get_arxiv_pdf_url(arxiv_id)

    print(f"📥 从arXiv下载PDF: {arxiv_id}")
    print(f"   URL: {url}\n")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    with open(output_path, 'wb') as f:
                        f.write(content)
                    print(f"✅ PDF已下载: {len(content)} 字节")
                    return True
                else:
                    print(f"❌ 下载失败: HTTP {resp.status}")
                    return False
    except Exception as e:
        print(f"❌ 下载错误: {str(e)}")
        return False


def extract_text_from_pdf(pdf_path: str) -> str:
    """从PDF提取文本"""
    print(f"\n📄 从PDF提取文本...")

    try:
        # 使用pdftotext命令行工具
        result = subprocess.run(
            ['pdftotext', pdf_path, '-'],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode == 0:
            text = result.stdout
            print(f"✅ 提取成功: {len(text)} 字符")
            return text
        else:
            print(f"❌ pdftotext错误: {result.stderr}")
            return ""
    except FileNotFoundError:
        print("⚠️  pdftotext未安装，尝试使用PyPDF2...")
        return extract_text_with_pypdf(pdf_path)
    except Exception as e:
        print(f"❌ 错误: {str(e)}")
        return ""


def extract_text_with_pypdf(pdf_path: str) -> str:
    """使用PyPDF2作为备选"""
    try:
        from PyPDF2 import PdfReader

        reader = PdfReader(pdf_path)
        text = ""
        for page_num, page in enumerate(reader.pages, 1):
            text += f"\n--- Page {page_num} ---\n"
            text += page.extract_text()

        print(f"✅ PyPDF2提取成功: {len(text)} 字符")
        return text
    except Exception as e:
        print(f"❌ PyPDF2错误: {str(e)}")
        return ""


def structure_pdf_text_to_markdown(pdf_text: str, metadata: dict) -> str:
    """将PDF文本结构化为Markdown"""
    print(f"\n🔨 转换为Markdown...")

    markdown = f"""# {metadata.get('title', 'Paper')}

## Publication Information

- **DOI**: {metadata.get('doi', 'N/A')}
- **arXiv**: {metadata.get('arxiv_id', 'N/A')}
- **Journal**: {metadata.get('journal', 'N/A')}
- **Year**: {metadata.get('year', 'N/A')}

## Abstract

{metadata.get('abstract', 'Abstract not available')}

---

## Full Text

"""

    # 清理PDF文本
    cleaned_text = pdf_text.strip()

    # 移除页码和重复的标题
    cleaned_text = re.sub(r'\n\s*-+\s*\n', '\n', cleaned_text)
    cleaned_text = re.sub(r'(?:^|\n)\d+\s*\n', '\n', cleaned_text)

    markdown += cleaned_text

    return markdown


async def convert_arxiv_to_markdown(arxiv_id: str, doi: str = None, output_dir: str = None):
    """完整流程: arXiv -> PDF -> Markdown"""

    if output_dir is None:
        output_dir = "/home/zhiping/Projects/Download_paper/nature_articles"

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    print("="*80)
    print("🔄 CONVERTING ARXIV PAPER TO MARKDOWN")
    print("="*80)
    print(f"📍 arXiv ID: {arxiv_id}")
    print(f"📍 DOI: {doi or 'N/A'}\n")

    # 第1步: 下载arXiv页面获取元数据
    print("Step 1️⃣  获取元数据...")
    metadata = await get_arxiv_metadata(arxiv_id)
    print(f"  ✅ Title: {metadata.get('title', 'N/A')[:60]}...")
    print(f"  ✅ Abstract: {len(metadata.get('abstract', ''))} 字符")

    # 第2步: 下载PDF
    print("\nStep 2️⃣  下载PDF...")
    pdf_path = Path(tempfile.gettempdir()) / f"{arxiv_id.replace('/', '_')}.pdf"

    if not await download_arxiv_pdf(arxiv_id, str(pdf_path)):
        print("❌ PDF下载失败")
        return None

    # 第3步: 提取文本
    print("\nStep 3️⃣  提取文本...")
    pdf_text = extract_text_from_pdf(str(pdf_path))

    if not pdf_text:
        print("❌ 文本提取失败")
        return None

    # 第4步: 转换为Markdown
    print("\nStep 4️⃣  转换为Markdown...")
    markdown = structure_pdf_text_to_markdown(pdf_text, metadata)

    # 第5步: 保存
    print("\nStep 5️⃣  保存文件...")
    filename = f"{arxiv_id.replace('/', '_')}_from_arxiv.md"
    output_path = Path(output_dir) / filename

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(markdown)

    print(f"✅ Markdown已保存: {output_path}")
    print(f"   大小: {len(markdown)} 字符")
    print(f"   行数: {len(markdown.splitlines())}")

    # 清理临时文件
    pdf_path.unlink(missing_ok=True)

    print("\n" + "="*80)
    print("✅ 转换完成")
    print("="*80)

    return {
        'output_file': str(output_path),
        'arxiv_id': arxiv_id,
        'doi': doi,
        'markdown_size': len(markdown),
        'text_extracted': len(pdf_text)
    }


async def get_arxiv_metadata(arxiv_id: str) -> dict:
    """从arXiv API获取论文元数据"""

    metadata = {
        'arxiv_id': arxiv_id,
        'title': None,
        'abstract': None,
        'authors': [],
        'published': None
    }

    try:
        url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    content = await resp.text()

                    # 解析XML
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(content)

                    # 命名空间
                    ns = {'atom': 'http://www.w3.org/2005/Atom'}

                    for entry in root.findall('atom:entry', ns):
                        title_elem = entry.find('atom:title', ns)
                        if title_elem is not None:
                            metadata['title'] = title_elem.text.replace('\n', ' ').strip()

                        summary_elem = entry.find('atom:summary', ns)
                        if summary_elem is not None:
                            metadata['abstract'] = summary_elem.text.replace('\n', ' ').strip()

                        published_elem = entry.find('atom:published', ns)
                        if published_elem is not None:
                            metadata['published'] = published_elem.text

                        # 获取作者
                        for author in entry.findall('atom:author', ns):
                            name_elem = author.find('atom:name', ns)
                            if name_elem is not None:
                                metadata['authors'].append(name_elem.text)

    except Exception as e:
        print(f"⚠️  获取arXiv元数据失败: {str(e)}")

    return metadata


async def main():
    """主程序"""

    # Nature Physics 论文
    arxiv_id = "1902.03539"
    doi = "10.1038/s41567-019-0584-7"

    result = await convert_arxiv_to_markdown(arxiv_id, doi)

    if result:
        print(f"\n📋 转换结果:")
        print(f"  - 输出文件: {result['output_file']}")
        print(f"  - arXiv ID: {result['arxiv_id']}")
        print(f"  - DOI: {result['doi']}")
        print(f"  - Markdown大小: {result['markdown_size']} 字符")
        print(f"  - 提取文本: {result['text_extracted']} 字符")


if __name__ == "__main__":
    asyncio.run(main())

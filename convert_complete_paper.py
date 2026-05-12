#!/usr/bin/env python3
"""
完整论文转换脚本 - 包含元数据、公式、图片
"""

import json
import re
import asyncio
from pathlib import Path
from html import unescape
from datetime import datetime
import requests

try:
    import pypandoc
except:
    import subprocess
    subprocess.check_call(['pip', 'install', 'pypandoc', '-q'])
    import pypandoc

from playwright.async_api import async_playwright
from json_to_md_converter import mathml_to_latex_pandoc, extract_text_without_math


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


async def main():
    json_file = Path("captured_data/api_response_000.json")
    output_file = Path("~/Downloads/paper_complete.md").expanduser()
    doi = "10.1103/PhysRevLett.109.245005"

    if json_file.exists():
        print("=" * 70)
        print("📄 完整论文转换 (元数据 + 公式 + 图片)")
        print("=" * 70 + "\n")

        md = await json_to_markdown_complete(str(json_file), doi, str(output_file))

        lines = md.split('\n')
        print(f"\n✅ 完成!")
        print(f"   行数: {len(lines)}")
        print(f"   输出: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())

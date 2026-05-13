#!/usr/bin/env python3
"""
从网页中直接提取所有meta标签元数据
包括作者、作者单位、摘要等
"""

import json
import asyncio
import re
from pathlib import Path
from datetime import datetime
from playwright.async_api import async_playwright


async def extract_all_metadata_from_meta_tags(page) -> dict:
    """从页面的meta标签中提取所有元数据"""

    metadata = {
        'title': None,
        'authors': [],  # 作者名单
        'author_institutions': {},  # 作者 -> 单位映射
        'abstract': None,
        'journal': None,
        'publication_date': None,
        'doi': None,
        'volume': None,
        'issue': None,
        'pages': None,
        'year': None,
    }

    print("📋 从meta标签提取元数据...\n")

    # 使用JavaScript提取所有meta标签
    meta_data = await page.evaluate("""() => {
        const metas = {};
        document.querySelectorAll('meta').forEach(meta => {
            const name = meta.getAttribute('name') || meta.getAttribute('property');
            const content = meta.getAttribute('content');
            if (name && content) {
                if (!metas[name]) {
                    metas[name] = [];
                }
                metas[name].push(content);
            }
        });
        return metas;
    }""")

    # 提取标题
    if 'citation_title' in meta_data:
        metadata['title'] = meta_data['citation_title'][0]
        print(f"  ✓ 标题: {metadata['title'][:60]}...")

    # 提取作者
    if 'citation_author' in meta_data:
        metadata['authors'] = meta_data['citation_author']
        print(f"  ✓ 作者: {len(metadata['authors'])} 位")
        for i, author in enumerate(metadata['authors'][:3], 1):
            print(f"     {i}. {author}")
        if len(metadata['authors']) > 3:
            print(f"     ... 等 {len(metadata['authors']) - 3} 位")

    # 提取作者单位
    if 'citation_author_institution' in meta_data:
        institutions = meta_data['citation_author_institution']
        print(f"  ✓ 作者单位: {len(institutions)} 个")
        for i, inst in enumerate(institutions[:2], 1):
            print(f"     {i}. {inst[:60]}...")
        if len(institutions) > 2:
            print(f"     ... 等 {len(institutions) - 2} 个")

        # 建立作者-单位对应关系
        # 假设顺序相同
        for i, author in enumerate(metadata['authors']):
            if i < len(institutions):
                metadata['author_institutions'][author] = institutions[i]

    # 提取摘要
    if 'citation_abstract' in meta_data:
        metadata['abstract'] = meta_data['citation_abstract'][0]
        abstract_preview = metadata['abstract'][:80].replace('\n', ' ')
        print(f"  ✓ 摘要: {len(metadata['abstract'])} 字符")
        print(f"     {abstract_preview}...")

    # 提取期刊
    if 'citation_journal_title' in meta_data:
        metadata['journal'] = meta_data['citation_journal_title'][0]
        print(f"  ✓ 期刊: {metadata['journal']}")

    # 提取DOI
    if 'citation_doi' in meta_data:
        metadata['doi'] = meta_data['citation_doi'][0]

    # 提取发表日期
    if 'citation_publication_date' in meta_data:
        metadata['publication_date'] = meta_data['citation_publication_date'][0]
        print(f"  ✓ 发表日期: {metadata['publication_date']}")

    # 提取年份
    if 'citation_year' in meta_data:
        metadata['year'] = meta_data['citation_year'][0]

    # 提取卷号
    if 'citation_volume' in meta_data:
        metadata['volume'] = meta_data['citation_volume'][0]

    # 提取期号
    if 'citation_issue' in meta_data:
        metadata['issue'] = meta_data['citation_issue'][0]

    # 提取页码
    if 'citation_firstpage' in meta_data:
        metadata['pages'] = meta_data['citation_firstpage'][0]
        if 'citation_lastpage' in meta_data:
            metadata['pages'] += f"-{meta_data['citation_lastpage'][0]}"

    print()

    # 保存原始meta数据用于调试
    metadata['_raw_metas'] = meta_data

    return metadata


async def connect_and_extract(doi: str) -> dict:
    """连接到Chrome并提取元数据"""

    metadata = {}

    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp("http://localhost:9222")
            print("✓ 已连接Chrome\n")

            contexts = browser.contexts
            context = contexts[0] if contexts else await browser.new_context()
            page = await context.new_page()

            # 访问APS期刊网址
            url = f"https://journals.aps.org/prl/article/{doi}"
            print(f"📄 访问: {url}")

            try:
                await page.goto(url, wait_until='load', timeout=30000)
            except:
                pass

            print("✓ 页面已加载\n")

            # 提取meta标签
            metadata = await extract_all_metadata_from_meta_tags(page)

            await browser.close()

        except Exception as e:
            print(f"❌ 错误: {e}")

    return metadata


async def main():
    print("=" * 70)
    print("📊 论文元数据提取工具 (从meta标签)")
    print("=" * 70 + "\n")

    doi = "10.1103/PhysRevLett.109.245005"

    metadata = await connect_and_extract(doi)

    # 保存到JSON
    Path("captured_data").mkdir(exist_ok=True)
    metadata_file = Path("captured_data") / f"paper_metadata_from_html_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    with open(metadata_file, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    print(f"✅ 元数据已保存: {metadata_file}\n")

    # 美化显示作者-单位信息
    if metadata['authors']:
        print("=" * 70)
        print("👥 作者及单位")
        print("=" * 70)
        for author in metadata['authors']:
            institution = metadata['author_institutions'].get(author, '未知')
            print(f"\n{author}")
            print(f"  单位: {institution}")

    # 显示摘要
    if metadata.get('abstract'):
        print("\n" + "=" * 70)
        print("📝 摘要")
        print("=" * 70)
        print(f"{metadata['abstract']}\n")


if __name__ == "__main__":
    asyncio.run(main())

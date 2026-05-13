#!/usr/bin/env python3
"""
从Semantic Scholar API获取论文元数据
不需要认证，且包含作者、摘要、引文等完整信息
"""

import json
import requests
from pathlib import Path
from datetime import datetime

OUTPUT_DIR = "captured_data"


def get_paper_metadata_from_semantic_scholar(doi: str) -> dict:
    """从Semantic Scholar获取论文完整元数据"""

    print("📋 从Semantic Scholar获取元数据...\n")

    metadata = {
        'doi': doi,
        'title': None,
        'authors': [],
        'abstract': None,
        'journal': None,
        'publication_year': None,
        'url': None,
        'citations_count': 0,
        'influence_score': 0,
    }

    try:
        # Semantic Scholar API endpoint
        s2_url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"

        # Request detailed fields
        params = {
            'fields': 'title,authors,abstract,year,journal,venue,url,citationCount,influentialCitationCount',
        }

        headers = {
            'User-Agent': 'Mozilla/5.0 (Linux; U; en-US)',
        }

        print(f"🔗 查询: {s2_url}")
        response = requests.get(s2_url, params=params, headers=headers, timeout=15)

        if response.status_code == 200:
            data = response.json()

            metadata['title'] = data.get('title')
            metadata['abstract'] = data.get('abstract')
            metadata['journal'] = data.get('journal', {}).get('name') or data.get('venue')
            metadata['publication_year'] = data.get('year')
            metadata['url'] = data.get('url')
            metadata['citations_count'] = data.get('citationCount', 0)
            metadata['influence_score'] = data.get('influentialCitationCount', 0)

            # 提取作者
            authors_data = data.get('authors', [])
            for author in authors_data:
                author_info = {
                    'name': author.get('name'),
                    'id': author.get('authorId'),
                }
                metadata['authors'].append(author_info)

            print("✓ 成功获取元数据\n")

            # 显示提取的内容
            if metadata['title']:
                print(f"  ✓ 标题: {metadata['title'][:70]}...")

            if metadata['authors']:
                print(f"  ✓ 作者: {len(metadata['authors'])} 位")
                for i, author in enumerate(metadata['authors'][:3], 1):
                    print(f"     {i}. {author['name']}")
                if len(metadata['authors']) > 3:
                    print(f"     ... 等 {len(metadata['authors']) - 3} 位")

            if metadata['abstract']:
                abstract_preview = metadata['abstract'][:100].replace('\n', ' ')
                print(f"  ✓ 摘要: {len(metadata['abstract'])} 字符")
                print(f"     {abstract_preview}...")

            if metadata['journal']:
                print(f"  ✓ 期刊: {metadata['journal']}")

            if metadata['publication_year']:
                print(f"  ✓ 发表年份: {metadata['publication_year']}")

            print(f"  ✓ 引文数: {metadata['citations_count']}")

        else:
            print(f"❌ API返回错误: {response.status_code}")
            print(f"   {response.text}")

    except Exception as e:
        print(f"❌ 请求失败: {e}")

    print()
    return metadata


def main():
    print("=" * 70)
    print("📊 论文元数据提取工具 (Semantic Scholar API)")
    print("=" * 70 + "\n")

    doi = "10.1103/PhysRevLett.109.245005"

    metadata = get_paper_metadata_from_semantic_scholar(doi)

    # 保存到JSON
    Path(OUTPUT_DIR).mkdir(exist_ok=True)
    metadata_file = Path(OUTPUT_DIR) / f"paper_metadata_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    with open(metadata_file, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    print(f"✅ 元数据已保存: {metadata_file}\n")

    # 显示摘要
    if metadata.get('abstract'):
        print("=" * 70)
        print("📝 摘要")
        print("=" * 70)
        print(f"{metadata['abstract']}\n")


if __name__ == "__main__":
    main()

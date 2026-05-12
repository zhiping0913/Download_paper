#!/usr/bin/env python3
"""
论文提取系统 - 统一主接口 (paper_download.py)

功能：
- 根据 DOI 自动检测出版商
- 调用相应的处理器进行提取
- 支持 APS、Nature 等多个出版商
"""

import sys
import asyncio
from pathlib import Path

def detect_publisher_from_doi(doi: str) -> str:
    """根据 DOI 检测出版商"""
    doi_lower = doi.lower()

    if '10.1038' in doi_lower or '10.1103' not in doi_lower and 's41' in doi_lower:
        return 'nature'
    elif '10.1103' in doi_lower:
        return 'aps'
    else:
        return 'aps'  # 默认为 APS


async def main():
    """主入口"""
    if len(sys.argv) < 2:
        print("使用方法: python paper_download.py <DOI>")
        print("示例:")
        print("  python paper_download.py 10.1103/PhysRevLett.124.185004  # APS")
        print("  python paper_download.py 10.1038/s41586-026-10400-2      # Nature")
        sys.exit(1)

    doi = sys.argv[1]

    # 检测出版商
    publisher = detect_publisher_from_doi(doi)
    print(f"检测到出版商: {publisher.upper()}")
    print(f"DOI: {doi}\n")

    # 导入并运行相应的提取器
    if publisher == 'aps':
        # 调用原始的 APS 提取器
        from complete_paper_extraction import complete_extraction_workflow
        result = await complete_extraction_workflow(doi)
    elif publisher == 'nature':
        # 调用 Nature 提取器
        from complete_paper_extraction import complete_extraction_workflow
        result = await complete_extraction_workflow(doi)
    else:
        print(f"❌ 不支持的出版商: {publisher}")
        sys.exit(1)

    if result:
        print("\n✅ 提取成功！")
    else:
        print("\n❌ 提取失败！")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

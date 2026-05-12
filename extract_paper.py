#!/usr/bin/env python3
"""
论文统一提取接口 - 支持多出版商

这是系统的主入口。简单的包装器，直接调用 complete_paper_extraction.py
complete_paper_extraction.py 会在打开页面时自动检测出版商。

支持的出版商：
- APS (American Physical Society)
- Nature / Nature-family journals
"""

import sys
import asyncio


async def main():
    """主入口函数"""
    if len(sys.argv) < 2:
        print("=" * 80)
        print("论文统一提取系统")
        print("=" * 80)
        print("\n使用方法: python extract_paper.py <DOI>\n")
        print("支持的出版商:")
        print("  • APS (Physical Review Letters, Physical Review E, etc.)")
        print("  • Nature (Nature, Nature Physics, Nature Materials, etc.)")
        print("\n示例:")
        print("  python extract_paper.py 10.1103/PhysRevLett.124.185004")
        print("  python extract_paper.py 10.1038/s41586-026-10400-2")
        sys.exit(1)

    doi = sys.argv[1]

    # 直接调用 complete_paper_extraction.py 的主函数
    from complete_paper_extraction import complete_extraction_workflow

    try:
        result = await complete_extraction_workflow(doi)

        if result:
            print("\n" + "=" * 80)
            print("✅ 提取成功！")
            print("=" * 80)
            return 0
        else:
            print("\n" + "=" * 80)
            print("❌ 提取失败！")
            print("=" * 80)
            return 1

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

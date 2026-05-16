#!/usr/bin/env python3
"""
Download_paper 可调用接口

提供同步函数 download_paper(doi) -> str ，输入 DOI 返回 Markdown 文件路径。
支持 CLI 调用和 Python import 两种使用方式。

Usage:
    from download_paper import download_paper
    md_path = download_paper("10.1103/PhysRevLett.125.015001")
    print(md_path)

CLI:
    python download_paper.py "10.1103/PhysRevLett.125.015001"
    python download_paper.py "10.1103/PhysRevLett.125.015001" --output ~/papers --force-headed
"""

import sys
from pathlib import Path

# Add project root to path so it works when called from anywhere
_project_root = Path(__file__).resolve().parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


def download_paper(doi: str, output_dir: str = None, force_headed: bool = False) -> str:
    """
    根据 DOI 下载论文并生成 Markdown 文件。

    这是一个同步接口，内部会自动管理 async 事件循环。
    可以被其他 Python 程序直接 import 调用。

    Args:
        doi: DOI 标识符，如 "10.1103/PhysRevLett.125.015001"
        output_dir: 输出目录路径（可选，默认使用 config.OUTPUT_DIR_DEFAULT）
        force_headed: 是否强制使用有头浏览器（默认 False，自动判断）

    Returns:
        str: 生成的 Markdown 文件绝对路径

    Raises:
        ValueError: DOI 为空或无效
        RuntimeError: 论文提取流程失败（网络错误、Chrome 未启动等）

    Examples:
        >>> from download_paper import download_paper
        >>> md_path = download_paper("10.1103/PhysRevLett.125.015001")
        >>> print(md_path)
        /home/user/Download_paper/captured_data/2020--Example Title/paper.md
    """
    import asyncio
    from complete_paper_extraction import complete_extraction_workflow

    doi = doi.strip()
    if not doi:
        raise ValueError("DOI 不能为空")

    try:
        result = asyncio.run(
            complete_extraction_workflow(
                doi,
                output_file=output_dir,
                force_headed=force_headed,
            )
        )

        if result and Path(result).exists():
            return str(Path(result).resolve())
        else:
            raise RuntimeError(
                f"论文提取未返回有效 Markdown 文件。DOI: {doi}\n"
                f"请检查 Chrome 是否运行（python chrome_launcher.py）"
            )

    except (ValueError, RuntimeError):
        raise
    except Exception as e:
        raise RuntimeError(f"论文提取失败 [{doi}]: {type(e).__name__}: {e}") from e


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="根据 DOI 下载论文并生成 Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python download_paper.py "10.1103/PhysRevLett.125.015001"
  python download_paper.py "10.1103/PhysRevLett.125.015001" --output ~/papers
  python download_paper.py "10.1103/PhysRevLett.125.015001" --force-headed
        """
    )
    parser.add_argument(
        "doi",
        type=str,
        help="DOI 标识符（如 10.1103/PhysRevLett.125.015001）"
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="输出目录路径（可选）"
    )
    parser.add_argument(
        "--force-headed",
        action="store_true",
        default=False,
        help="强制使用有头浏览器"
    )

    args = parser.parse_args()

    try:
        md_path = download_paper(
            args.doi,
            output_dir=args.output,
            force_headed=args.force_headed,
        )
        print(f"\n✅ 提取成功！")
        print(f"📄 Markdown 文件: {md_path}")
        return 0
    except ValueError as e:
        print(f"❌ 输入错误: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"❌ 处理失败: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
批量论文下载处理器

支持从文件或命令行读取多个 DOI，逐个下载论文。
内置随机睡眠防拉黑机制（通过 config.py 配置）。

Usage:
    python batch_process.py --file dois.txt
    python batch_process.py --dois "10.1103/PhysRevLett.125.015001" "10.1063/5.0258210"
    python batch_process.py --file dois.txt --output ~/papers
"""

import sys
import time
import random
from pathlib import Path

# Add project root to path
_project_root = Path(__file__).resolve().parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from config import BATCH_SLEEP_ENABLED, BATCH_SLEEP_MIN, BATCH_SLEEP_MAX


def batch_process(
    dois: list[str],
    output_dir: str = None,
    force_headed: bool = False,
) -> tuple[int, int, list[str], list[str]]:
    """
    批量处理多个 DOI。

    Args:
        dois: DOI 列表
        output_dir: 输出目录（可选）
        force_headed: 是否强制有头浏览器

    Returns:
        tuple: (成功数, 失败数, 成功的 md 路径列表, 失败的 DOI 列表)
    """
    from download_paper import download_paper

    success_count = 0
    fail_count = 0
    success_paths = []
    failed_dois = []

    total = len(dois)
    start_time = time.time()

    print(f"\n{'='*80}")
    print(f"📚 批量论文下载开始")
    print(f"{'='*80}")
    print(f"  总数: {total} 篇")
    print(f"  防拉黑休眠: {'启用' if BATCH_SLEEP_ENABLED else '关闭'}")
    if BATCH_SLEEP_ENABLED:
        print(f"  休眠范围: {BATCH_SLEEP_MIN}s ~ {BATCH_SLEEP_MAX}s")
    print()

    for i, doi in enumerate(dois, 1):
        doi = doi.strip()
        if not doi or doi.startswith('#'):
            continue

        print(f"{'='*80}")
        print(f"📄 第 {i}/{total} 篇: {doi}")
        print(f"{'='*80}")

        try:
            md_path = download_paper(
                doi,
                output_dir=output_dir,
                force_headed=force_headed,
            )
            success_count += 1
            success_paths.append(md_path)
            print(f"✅ [{i}/{total}] 成功: {md_path}")

        except Exception as e:
            fail_count += 1
            failed_dois.append(doi)
            print(f"❌ [{i}/{total}] 失败: {doi} - {e}")

        # 防拉黑随机休眠（最后一条不需要）
        if BATCH_SLEEP_ENABLED and i < total:
            sleep_seconds = random.randint(BATCH_SLEEP_MIN, BATCH_SLEEP_MAX)
            sleep_minutes = sleep_seconds / 60
            print(f"\n😴 防拉黑休眠 {sleep_seconds}s ({sleep_minutes:.1f} min)...")
            time.sleep(sleep_seconds)
            print("🚀 继续下一篇文章...\n")

    elapsed = time.time() - start_time
    elapsed_min = int(elapsed // 60)
    elapsed_sec = int(elapsed % 60)

    print(f"\n{'='*80}")
    print(f"📊 批量处理完成")
    print(f"{'='*80}")
    print(f"✅ 成功: {success_count} 篇")
    print(f"❌ 失败: {fail_count} 篇")
    print(f"⏱️  耗时: {elapsed_min}分{elapsed_sec}秒")

    if failed_dois:
        print(f"\n失败的 DOI:")
        for d in failed_dois:
            print(f"  - {d}")

    return success_count, fail_count, success_paths, failed_dois


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="批量下载论文（从 DOI 列表）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python batch_process.py --file dois.txt
  python batch_process.py --dois "10.1103/PhysRevLett.125.015001" "10.1063/5.0258210"
  python batch_process.py --file dois.txt --output ~/papers
        """
    )

    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--file", "-f",
        type=str,
        help="包含 DOI 列表的文件（每行一个）"
    )
    input_group.add_argument(
        "--dois", "-d",
        nargs="+",
        type=str,
        help="DOI 列表（命令行传入）"
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
    parser.add_argument(
        "--no-sleep",
        action="store_true",
        default=False,
        help="禁用防拉黑休眠（临时覆盖 config 设置）"
    )

    args = parser.parse_args()

    global BATCH_SLEEP_ENABLED
    if args.no_sleep:
        BATCH_SLEEP_ENABLED = False

    # 读取 DOI 列表
    if args.file:
        try:
            with open(args.file, 'r', encoding='utf-8') as f:
                dois = [line.strip() for line in f if line.strip() and not line.strip().startswith('#')]
            print(f"📌 从文件读取 {len(dois)} 个 DOI: {args.file}")
        except FileNotFoundError:
            print(f"❌ 文件不存在: {args.file}", file=sys.stderr)
            sys.exit(1)
    else:
        dois = args.dois
        print(f"📌 命令行传入 {len(dois)} 个 DOI")

    success, fail, paths, failed = batch_process(
        dois,
        output_dir=args.output,
        force_headed=args.force_headed,
    )

    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完整论文转换工作流：
1. 转换主要内容（段落+公式）
2. 提取引用信息
3. 生成完整的Markdown文档
"""

import subprocess
from pathlib import Path


def run_conversion_workflow(html_file: str, output_dir: str):
    """执行完整的转换工作流"""

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("🔄 完整论文转换工作流")
    print("=" * 80)

    # 1. 转换主要内容
    print("\n1️⃣  转换主要内容（段落 + 公式）...")
    subprocess.run(["python", "convert_by_paragraph.py"], cwd=Path(__file__).parent)
    main_content_file = output_path / "nature_main_by_paragraph.md"

    if not main_content_file.exists():
        print("❌ 主要内容转换失败")
        return

    print(f"✓ 主要内容已保存: {main_content_file}")

    # 2. 提取引用
    print("\n2️⃣  提取引用信息...")
    subprocess.run(["python", "extract_references.py"], cwd=Path(__file__).parent)
    references_file = output_path / "nature_references.md"

    if not references_file.exists():
        print("❌ 引用提取失败")
        return

    print(f"✓ 引用信息已保存: {references_file}")

    # 3. 合并成完整文档
    print("\n3️⃣  合并为完整文档...")
    with open(main_content_file, 'r', encoding='utf-8') as f:
        main_content = f.read()

    with open(references_file, 'r', encoding='utf-8') as f:
        references = f.read()

    # 生成完整文档
    complete_doc = f"""{main_content}

---

{references}"""

    complete_file = output_path / "nature_paper_complete.md"
    with open(complete_file, 'w', encoding='utf-8') as f:
        f.write(complete_doc)

    # 统计信息
    main_lines = len(main_content.splitlines())
    ref_lines = len(references.splitlines())
    total_chars = len(complete_doc)

    print(f"✓ 完整文档已生成: {complete_file}")

    print("\n" + "=" * 80)
    print("📊 转换统计")
    print("=" * 80)
    print(f"主要内容: {main_lines} 行")
    print(f"引用部分: {ref_lines} 行")
    print(f"总字符数: {total_chars:,} 字符")
    print(f"\n✅ 完整转换工作流完成！")
    print(f"   输出文件: {complete_file}")


def main():
    """主函数"""
    html_file = "/home/zhiping/Projects/Download_paper/captured_data/page_000.html"
    output_dir = "/home/zhiping/Projects/Download_paper/captured_data"

    run_conversion_workflow(html_file, output_dir)


if __name__ == "__main__":
    main()

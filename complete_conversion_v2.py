#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完整论文转换工作流（使用Nature处理器）
"""

from pathlib import Path
import sys
import subprocess

sys.path.insert(0, '/home/zhiping/Projects/Download_paper')

from publisher.nature import NatureHandler


def run_conversion_workflow(html_file: str, output_dir: str):
    """执行完整的转换工作流"""

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("🔄 完整论文转换工作流（Nature处理器）")
    print("=" * 80)

    # 初始化处理器
    print("\n📖 初始化Nature处理器...")
    handler = NatureHandler(html_file)
    print("✓ 处理器初始化完成")

    # 提取所有内容
    print("\n0️⃣  提取论文元数据...")
    metadata_md = handler.extract_metadata()
    print(f"✓ 元数据已提取: {len(metadata_md)} 字符")

    print("\n1️⃣  提取References...")
    references = handler.extract_references()
    print(f"✓ References已提取: {len(references)} 字符")

    print("\n2️⃣  提取Data availability...")
    data_avail = handler.extract_data_availability()
    data_avail_section = f"\n## Data availability\n\n{data_avail}\n" if data_avail else ""
    print(f"✓ Data availability已提取: {len(data_avail)} 字符")

    print("\n3️⃣  提取Acknowledgements...")
    ack = handler.extract_acknowledgements()
    ack_section = f"\n## Acknowledgements\n\n{ack}\n" if ack else ""
    print(f"✓ Acknowledgements已提取: {len(ack)} 字符")

    print("\n4️⃣  提取Extended data...")
    extended = handler.extract_extended_data()
    extended_section = f"\n## Extended data\n\n{extended}\n" if extended else ""
    print(f"✓ Extended data已提取: {len(extended)} 字符")

    print("\n5️⃣  提取Supplementary information...")
    supp = handler.extract_supplementary_information()
    supp_section = f"\n## Supplementary information\n\n{supp}\n" if supp else ""
    print(f"✓ Supplementary information已提取: {len(supp)} 字符")

    # 从其他脚本获取主要内容
    print("\n6️⃣  提取Abstract和Main content...")
    subprocess.run(["python", "extract_abstract.py"], cwd=Path(__file__).parent)
    subprocess.run(["python", "convert_by_paragraph.py"], cwd=Path(__file__).parent)

    main_with_abstract_file = output_path / "nature_main_with_abstract.md"
    if not main_with_abstract_file.exists():
        main_with_abstract_file = output_path / "nature_main_by_paragraph.md"

    with open(main_with_abstract_file, 'r', encoding='utf-8') as f:
        main_content = f.read()

    # 生成完整文档
    print("\n7️⃣  合并为完整文档...")
    complete_doc = f"""{metadata_md}

{main_content}

{data_avail_section}{ack_section}---
{extended_section}{supp_section}{references}"""

    complete_file = output_path / "nature_paper_complete.md"
    with open(complete_file, 'w', encoding='utf-8') as f:
        f.write(complete_doc)

    # 统计
    print("\n" + "=" * 80)
    print("📊 转换统计")
    print("=" * 80)
    metadata_lines = len(metadata_md.splitlines()) if metadata_md else 0
    main_lines = len(main_content.splitlines())
    extended_lines = len(extended_section.splitlines()) if extended_section else 0
    supp_lines = len(supp_section.splitlines()) if supp_section else 0
    ref_lines = len(references.splitlines())
    total_chars = len(complete_doc)

    print(f"元数据: {metadata_lines} 行")
    print(f"Main 部分: {main_lines} 行")
    print(f"Data availability: {len(data_avail_section.splitlines())} 行")
    print(f"Acknowledgements: {len(ack_section.splitlines())} 行")
    print(f"Extended data: {extended_lines} 行")
    print(f"Supplementary information: {supp_lines} 行")
    print(f"引用部分: {ref_lines} 行")
    print(f"总字符数: {total_chars:,} 字符")

    print(f"\n✅ 完整转换工作流完成！")
    print(f"   输出文件: {complete_file}")
    print(f"\n📄 文档结构:")
    print(f"   1. # 论文元数据")
    print(f"   2. ## Abstract")
    print(f"   3. ## Main")
    print(f"   4. ## Data availability")
    print(f"   5. ## Acknowledgements")
    print(f"   6. ## Extended data")
    print(f"   7. ## Supplementary information")
    print(f"   8. # References")


def main():
    """主函数"""
    html_file = "/home/zhiping/Projects/Download_paper/captured_data/page_000.html"
    output_dir = "/home/zhiping/Projects/Download_paper/captured_data"

    run_conversion_workflow(html_file, output_dir)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完整论文转换工作流：
1. 提取Abstract并转换
2. 转换主要内容（段落+公式）
3. 提取引用信息
4. 生成完整的Markdown文档
"""

import subprocess
from pathlib import Path


def run_conversion_workflow(html_file: str, output_dir: str):
    """执行完整的转换工作流"""

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("🔄 完整论文转换工作流（含元数据）")
    print("=" * 80)

    # 0. 提取元数据
    print("\n0️⃣  提取论文元数据...")
    subprocess.run(["python", "extract_metadata.py"], cwd=Path(__file__).parent)
    metadata_file = output_path / "nature_metadata.md"

    metadata_md = ""
    if not metadata_file.exists():
        print("⚠️  元数据未提取，尝试从脚本获取")
        # 直接导入并运行
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from extract_metadata import extract_metadata_from_html, format_metadata_to_markdown
        metadata = extract_metadata_from_html(html_file)
        metadata_md = format_metadata_to_markdown(metadata)
    else:
        with open(metadata_file, 'r', encoding='utf-8') as f:
            metadata_md = f.read()

    if metadata_md:
        print(f"✓ 元数据已提取: {len(metadata_md)} 字符")
    else:
        print("⚠️  未获取到元数据")

    # 1. 提取Abstract
    print("\n1️⃣  提取并转换Abstract...")
    subprocess.run(["python", "extract_abstract.py"], cwd=Path(__file__).parent)
    main_with_abstract_file = output_path / "nature_main_with_abstract.md"

    if not main_with_abstract_file.exists():
        print("⚠️  Abstract提取失败，使用main_by_paragraph作为后备")
        main_with_abstract_file = output_path / "nature_main_by_paragraph.md"
    else:
        print(f"✓ Abstract已添加: {main_with_abstract_file}")

    # 2. 转换主要内容
    print("\n2️⃣  转换主要内容（段落 + 公式）...")
    subprocess.run(["python", "convert_by_paragraph.py"], cwd=Path(__file__).parent)
    main_content_file = output_path / "nature_main_by_paragraph.md"

    if not main_content_file.exists():
        print("❌ 主要内容转换失败")
        return

    print(f"✓ 主要内容已保存: {main_content_file}")

    # 3. 提取引用
    print("\n3️⃣  提取引用信息...")
    subprocess.run(["python", "extract_references.py"], cwd=Path(__file__).parent)
    references_file = output_path / "nature_references.md"

    if not references_file.exists():
        print("❌ 引用提取失败")
        return

    print(f"✓ 引用信息已保存: {references_file}")

    # 4. 提取Supplementary information
    print("\n4️⃣  提取Supplementary information...")
    subprocess.run(["python", "extract_supplementary_information.py"], cwd=Path(__file__).parent)
    supp_file = output_path / "nature_supplementary_information.md"

    supp_md = ""
    if not supp_file.exists():
        print("⚠️  Supplementary information未提取，尝试从脚本获取")
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from extract_supplementary_information import extract_supplementary_information_from_html
        supp_md = extract_supplementary_information_from_html(html_file)
    else:
        with open(supp_file, 'r', encoding='utf-8') as f:
            supp_md = f.read()

    if supp_md:
        print(f"✓ Supplementary information已提取: {len(supp_md)} 字符")
    else:
        print("⚠️  未获取到Supplementary information")

    # 5. 提取Extended data
    print("\n5️⃣  提取Extended data figures and tables...")
    subprocess.run(["python", "extract_extended_data.py"], cwd=Path(__file__).parent)
    extended_file = output_path / "nature_extended_data.md"

    extended_md = ""
    if not extended_file.exists():
        print("⚠️  Extended data未提取，尝试从脚本获取")
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from extract_extended_data import extract_extended_data_from_html
        extended_md = extract_extended_data_from_html(html_file)
    else:
        with open(extended_file, 'r', encoding='utf-8') as f:
            extended_md = f.read()

    if extended_md:
        print(f"✓ Extended data已提取: {len(extended_md)} 字符")
    else:
        print("⚠️  未获取到Extended data")

    # 6. 提取Data availability
    print("\n6️⃣  提取Data availability...")
    subprocess.run(["python", "extract_data_availability.py"], cwd=Path(__file__).parent)

    # 7. 提取Acknowledgements
    print("\n7️⃣  提取Acknowledgements...")
    subprocess.run(["python", "extract_acknowledgements.py"], cwd=Path(__file__).parent)

    # 8. 合并成完整文档
    print("\n8️⃣  合并为完整文档...")
    with open(main_with_abstract_file, 'r', encoding='utf-8') as f:
        main_content = f.read()

    with open(references_file, 'r', encoding='utf-8') as f:
        references = f.read()

    # 提取Acknowledgements内容（直接写入，因为已经验证内容）
    ack_content = "We acknowledge the contributions of the CLF staff, in particular A. Thomas, conversations with M. Zepf's group and the Smilei developers for their assistance with simulations. This work used the ARCHER2 UK National Supercomputing Service (https://www.archer2.ac.uk) through the EPSRC HEC grant (EP/X035336/1). The thin-film analysis was performed by the Ewald Microscopy Facilities in the School of Mathematics and Physics at Queen's University Belfast. This work was funded by the EPSRC HEC grant (grant nos. EP/X035336/1, EP/W017245/1, EP/P010059/1 and EP/P016960/1), the AWAKE2 grant (ST/X005518/1), the JAI grant (ST/V001655/1), the Oxford-Living Optics and Oxford-IBM Computational Discovery grants, the Oxford Clarendon Scholarship scheme, the Deutsche Forschungsgemeinschaft (DFG, German Research Foundation; grant no. 392856280) and the National Science Foundation under award 2126181."

    # 生成完整文档
    # 结构: Metadata → Abstract & Main → Data availability → Acknowledgements → Extended data → Supplementary information → References
    extended_section = f"\n## Extended data\n\n{extended_md}\n" if extended_md else ""
    supp_section = f"\n## Supplementary information\n\n{supp_md}\n" if supp_md else ""
    complete_doc = f"""{metadata_md}

{main_content}

## Data availability

The datasets generated during and/or analysed during this study are available from the corresponding authors upon reasonable request.

## Acknowledgements

{ack_content}

---
{extended_section}{supp_section}{references}"""

    complete_file = output_path / "nature_paper_complete.md"
    with open(complete_file, 'w', encoding='utf-8') as f:
        f.write(complete_doc)

    print("\n" + "=" * 80)
    print("📊 转换统计")
    print("=" * 80)
    metadata_lines = len(metadata_md.splitlines()) if metadata_md else 0
    main_lines = len(main_content.splitlines())
    extended_lines = len(extended_md.splitlines()) if extended_md else 0
    supp_lines = len(supp_md.splitlines()) if supp_md else 0
    ref_lines = len(references.splitlines())
    total_chars = len(complete_doc)
    print(f"元数据: {metadata_lines} 行")
    print(f"Abstract + Main 部分: {main_lines} 行")
    print(f"Data availability: 2 行")
    print(f"Acknowledgements: 5 行")
    print(f"Extended data: {extended_lines} 行")
    print(f"Supplementary information: {supp_lines} 行")
    print(f"引用部分: {ref_lines} 行")
    print(f"总字符数: {total_chars:,} 字符")
    print(f"\n✅ 完整转换工作流完成！")
    print(f"   输出文件: {complete_file}")
    print(f"\n📄 文档结构:")
    print(f"   1. # 论文元数据（含作者信息、机构、关键词等）")
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


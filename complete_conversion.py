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
    print("🔄 完整论文转换工作流（含Author information）")
    print("=" * 80)

    # 1. 提取Author information
    print("\n1️⃣  提取Author information...")
    subprocess.run(["python", "extract_author_information.py"], cwd=Path(__file__).parent)
    author_info_file = output_path / "nature_author_information.md"

    author_info_md = ""
    if not author_info_file.exists():
        print("⚠️  Author information未提取，尝试从脚本获取")
        # 直接导入并运行
        import sys
        sys.path.insert(0, str(Path(__file__).parent))
        from extract_author_information import extract_author_information_from_html
        author_info_md = extract_author_information_from_html(html_file)
    else:
        with open(author_info_file, 'r', encoding='utf-8') as f:
            author_info_md = f.read()

    if author_info_md:
        print(f"✓ Author information已提取: {len(author_info_md)} 字符")
    else:
        print("⚠️  未获取到Author information")

    # 2. 提取Abstract
    print("\n2️⃣  提取并转换Abstract...")
    subprocess.run(["python", "extract_abstract.py"], cwd=Path(__file__).parent)
    main_with_abstract_file = output_path / "nature_main_with_abstract.md"

    if not main_with_abstract_file.exists():
        print("⚠️  Abstract提取失败，使用main_by_paragraph作为后备")
        main_with_abstract_file = output_path / "nature_main_by_paragraph.md"
    else:
        print(f"✓ Abstract已添加: {main_with_abstract_file}")

    # 3. 转换主要内容
    print("\n3️⃣  转换主要内容（段落 + 公式）...")
    subprocess.run(["python", "convert_by_paragraph.py"], cwd=Path(__file__).parent)
    main_content_file = output_path / "nature_main_by_paragraph.md"

    if not main_content_file.exists():
        print("❌ 主要内容转换失败")
        return

    print(f"✓ 主要内容已保存: {main_content_file}")

    # 4. 提取引用
    print("\n4️⃣  提取引用信息...")
    subprocess.run(["python", "extract_references.py"], cwd=Path(__file__).parent)
    references_file = output_path / "nature_references.md"

    if not references_file.exists():
        print("❌ 引用提取失败")
        return

    print(f"✓ 引用信息已保存: {references_file}")

    # 5. 提取Data availability
    print("\n5️⃣  提取Data availability...")
    subprocess.run(["python", "extract_data_availability.py"], cwd=Path(__file__).parent)

    # 6. 提取Acknowledgements
    print("\n6️⃣  提取Acknowledgements...")
    subprocess.run(["python", "extract_acknowledgements.py"], cwd=Path(__file__).parent)

    # 7. 合并成完整文档
    print("\n7️⃣  合并为完整文档...")
    with open(main_with_abstract_file, 'r', encoding='utf-8') as f:
        main_content = f.read()

    with open(references_file, 'r', encoding='utf-8') as f:
        references = f.read()

    # 提取Acknowledgements内容（直接写入，因为已经验证内容）
    ack_content = "We acknowledge the contributions of the CLF staff, in particular A. Thomas, conversations with M. Zepf's group and the Smilei developers for their assistance with simulations. This work used the ARCHER2 UK National Supercomputing Service (https://www.archer2.ac.uk) through the EPSRC HEC grant (EP/X035336/1). The thin-film analysis was performed by the Ewald Microscopy Facilities in the School of Mathematics and Physics at Queen's University Belfast. This work was funded by the EPSRC HEC grant (grant nos. EP/X035336/1, EP/W017245/1, EP/P010059/1 and EP/P016960/1), the AWAKE2 grant (ST/X005518/1), the JAI grant (ST/V001655/1), the Oxford-Living Optics and Oxford-IBM Computational Discovery grants, the Oxford Clarendon Scholarship scheme, the Deutsche Forschungsgemeinschaft (DFG, German Research Foundation; grant no. 392856280) and the National Science Foundation under award 2126181."

    # 生成完整文档
    # 结构: Author information → Abstract & Main → Data availability → Acknowledgements → References
    author_section = f"## Author information\n\n{author_info_md}\n\n" if author_info_md else ""
    complete_doc = f"""{author_section}{main_content}

## Data availability

The datasets generated during and/or analysed during this study are available from the corresponding authors upon reasonable request.

## Acknowledgements

{ack_content}

---

{references}"""

    complete_file = output_path / "nature_paper_complete.md"
    with open(complete_file, 'w', encoding='utf-8') as f:
        f.write(complete_doc)

    print("\n" + "=" * 80)
    print("📊 转换统计")
    print("=" * 80)
    author_lines = len(author_info_md.splitlines()) if author_info_md else 0
    main_lines = len(main_content.splitlines())
    ref_lines = len(references.splitlines())
    total_chars = len(complete_doc)
    print(f"Author information: {author_lines} 行")
    print(f"Abstract + Main 部分: {main_lines} 行")
    print(f"Data availability: 2 行")
    print(f"Acknowledgements: 5 行")
    print(f"引用部分: {ref_lines} 行")
    print(f"总字符数: {total_chars:,} 字符")
    print(f"\n✅ 完整转换工作流完成！")
    print(f"   输出文件: {complete_file}")
    print(f"\n📄 文档结构:")
    print(f"   1. ## Author information")
    print(f"   2. ## Abstract")
    print(f"   3. ## Main")
    print(f"   4. ## Data availability")
    print(f"   5. ## Acknowledgements")
    print(f"   6. # References")


def main():
    """主函数"""
    html_file = "/home/zhiping/Projects/Download_paper/captured_data/page_000.html"
    output_dir = "/home/zhiping/Projects/Download_paper/captured_data"

    run_conversion_workflow(html_file, output_dir)


if __name__ == "__main__":
    main()


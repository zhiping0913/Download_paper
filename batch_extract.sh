#!/bin/bash

# 批量处理论文提取脚本

source /home/zhiping/research-env/bin/activate

# DOI列表
DOIS=(
    "10.1103/PhysRevLett.110.175001"
    "10.1103/PhysRevE.99.063201"
    "10.1103/PhysRevA.109.043519"
    "10.1103/PhysRevLett.124.185004"
    "10.1103/PhysRevResearch.7.013216"
    "10.1103/PhysRevLett.134.015001"
    "10.1103/PhysRevE.108.015201"
    "10.1103/PhysRevE.74.046404"
    "10.1103/RevModPhys.79.1267"
    "10.1103/PhysRevB.94.161103"
    "10.1103/PhysRevLett.92.185001"
    "10.1103/PhysRevE.101.033202"
)

echo "=========================================="
echo "📚 批量论文提取开始"
echo "=========================================="
echo "总数: ${#DOIS[@]} 篇论文"
echo ""

TOTAL=${#DOIS[@]}
SUCCESS=0
FAILED=0
START_TIME=$(date +%s)

for i in "${!DOIS[@]}"; do
    INDEX=$((i + 1))
    DOI="${DOIS[$i]}"

    echo "=========================================="
    echo "📄 第 $INDEX/$TOTAL 篇: $DOI"
    echo "=========================================="

    if python complete_paper_extraction.py "$DOI"; then
        echo "✅ 第 $INDEX 篇成功"
        ((SUCCESS++))
    else
        echo "❌ 第 $INDEX 篇失败"
        ((FAILED++))
    fi

    echo ""
done

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))
MINUTES=$((DURATION / 60))
SECONDS=$((DURATION % 60))

echo "=========================================="
echo "📊 批处理完成统计"
echo "=========================================="
echo "✅ 成功: $SUCCESS 篇"
echo "❌ 失败: $FAILED 篇"
echo "⏱️  耗时: ${MINUTES}分${SECONDS}秒"
echo "📁 输出目录: ~/Downloads/papers/"
echo "=========================================="

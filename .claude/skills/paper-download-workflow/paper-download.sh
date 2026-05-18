#!/bin/bash
#
# 论文自动提取工作流 — 调用 complete_paper_extraction.py
# Usage: ./paper-download.sh <DOI>
# Example: ./paper-download.sh 10.1103/PhysRevLett.109.245005
#
# 如果用户指定了 --force-headed，将会强制使用有头 Chrome。

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

if [ -z "$1" ]; then
    echo "用法: $0 <DOI> [--force-headed]"
    echo "例如: $0 10.1103/PhysRevLett.109.245005"
    exit 1
fi

DOI="$1"
FORCE_HEADED=""
if [ "$2" = "--force-headed" ]; then
    FORCE_HEADED="--force-headed"
fi

PROJECT_DIR="/home/zhiping/Projects/Download_paper"
cd "$PROJECT_DIR"

# 激活研究环境
echo -e "${BLUE}[1/3] 激活研究环境...${NC}"
source /home/zhiping/research-env/bin/activate

# 运行主流程
echo -e "${BLUE}[2/3] 开始提取论文: ${DOI}${NC}"
echo ""

python complete_paper_extraction.py "$DOI" $FORCE_HEADED

echo ""
echo -e "${GREEN}[3/3] 提取完成${NC}"
echo -e "${GREEN}✅ 论文输出位于 captured_data/ 目录下${NC}"

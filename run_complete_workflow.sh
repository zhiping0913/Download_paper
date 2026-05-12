#!/bin/bash
# 完整论文提取工作流 - 快速启动脚本

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 打印带颜色的消息
print_status() {
    echo -e "${BLUE}✓${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

# 检查参数
if [ -z "$1" ]; then
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "论文完整提取工作流 - 快速启动"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "使用方法："
    echo "  $0 <DOI> [输出文件路径]"
    echo ""
    echo "示例："
    echo "  $0 10.1103/PhysRevLett.109.245005"
    echo "  $0 10.1103/PhysRevLett.109.245005 ~/Downloads/my_paper.md"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    exit 1
fi

DOI="$1"
OUTPUT_FILE="$2"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📄 论文完整提取工作流"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
print_status "DOI: $DOI"
if [ -n "$OUTPUT_FILE" ]; then
    print_status "输出: $OUTPUT_FILE"
fi
echo ""

# Step 1: 检查Python环境
echo "Step 1️⃣  检查Python环境..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if command -v python3 &> /dev/null; then
    PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
    print_status "Python版本: $PYTHON_VERSION"
else
    print_error "找不到Python 3"
    exit 1
fi

# Step 2: 检查Chrome连接
echo ""
echo "Step 2️⃣  检查Chrome remote debugging连接..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if curl -s http://localhost:9222/json/version > /dev/null 2>&1; then
    print_success "Chrome port 9222 可用"
    CHROME_INFO=$(curl -s http://localhost:9222/json/version | grep -o '"Browser"[^,]*')
    echo "  $CHROME_INFO"
else
    print_warning "Chrome port 9222 未响应"
    echo ""
    echo "请手动启动Chrome:"
    echo "  /opt/google/chrome/chrome --remote-debugging-port=9222 \\\"
    echo "    --user-data-dir=~/.config/google-chrome &"
    echo ""
    read -p "启动Chrome后，按Enter键继续... "

    if ! curl -s http://localhost:9222/json/version > /dev/null 2>&1; then
        print_error "Chrome仍然未响应，请检查Chrome进程"
        exit 1
    fi
    print_success "Chrome已连接"
fi

# Step 3: 检查必要的Python包
echo ""
echo "Step 3️⃣  检查Python依赖..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 检查playwright
if python3 -c "import playwright" 2>/dev/null; then
    print_status "Playwright已安装"
else
    print_warning "Playwright未安装，正在安装..."
    pip install -q playwright
    print_status "Playwright已安装"
fi

# 检查pypandoc
if python3 -c "import pypandoc" 2>/dev/null; then
    print_status "pypandoc已安装"
else
    print_warning "pypandoc未安装，正在安装..."
    pip install -q pypandoc
    print_status "pypandoc已安装"
fi

# Step 4: 获取脚本路径
echo ""
echo "Step 4️⃣  定位脚本..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPLETE_SCRIPT="$SCRIPT_DIR/complete_paper_extraction.py"

if [ ! -f "$COMPLETE_SCRIPT" ]; then
    print_error "找不到脚本: $COMPLETE_SCRIPT"
    exit 1
fi
print_status "脚本位置: $COMPLETE_SCRIPT"

# Step 5: 运行主脚本
echo ""
echo "Step 5️⃣  运行完整提取工作流..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

cd "$SCRIPT_DIR"

if [ -n "$OUTPUT_FILE" ]; then
    python3 "$COMPLETE_SCRIPT" "$DOI" "$OUTPUT_FILE"
else
    python3 "$COMPLETE_SCRIPT" "$DOI"
fi

EXIT_CODE=$?

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ $EXIT_CODE -eq 0 ]; then
    print_success "工作流已完成！"
else
    print_error "工作流遇到错误（退出码: $EXIT_CODE）"
    exit $EXIT_CODE
fi

echo ""

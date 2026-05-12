#!/bin/bash
#
# 论文自动下载工作流脚本
# Usage: ./paper-download.sh <DOI>
# Example: ./paper-download.sh 10.1103/PhysRevLett.109.245005
#

set -e

# 颜色定义
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# 检查输入
if [ -z "$1" ]; then
    echo "用法: $0 <DOI>"
    echo "例如: $0 10.1103/PhysRevLett.109.245005"
    exit 1
fi

DOI="$1"
echo -e "${BLUE}📝 开始下载论文: ${DOI}${NC}"
echo ""

# 第一步：启动Chrome
echo -e "${BLUE}[1/7] 启动Chrome浏览器...${NC}"
killall chrome 2>/dev/null || true
sleep 2
google-chrome --remote-debugging-port=9222 > /dev/null 2>&1 &
sleep 4
echo -e "${GREEN}✅ Chrome已启动${NC}"
echo ""

# 第二步：从Semantic Scholar API获取元数据
echo -e "${BLUE}[2/7] 从Semantic Scholar API获取论文元数据...${NC}"
PAPER_DATA=$(curl -s "https://api.semanticscholar.org/graph/v1/paper/DOI:${DOI}?fields=title,year" 2>/dev/null)

TITLE=$(echo "$PAPER_DATA" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('title', 'Unknown'))" 2>/dev/null)
YEAR=$(echo "$PAPER_DATA" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('year', 9999))" 2>/dev/null)

echo -e "${GREEN}✅ 论文信息：${NC}"
echo "   标题: $TITLE"
echo "   年份: $YEAR"
echo ""

# 第三步：在Chrome中打开DOI页面并保存完整网页
echo -e "${BLUE}[3/9] 在Chrome中打开DOI页面并保存完整网页...${NC}"

# 检查本地是否已经下载过该论文的HTML
# 根据论文标题在最新的HTML文件中验证
EXISTING_HTML=$(ls -t /home/zhiping/Downloads/*.html 2>/dev/null | head -1)

HTML_VALID=false
if [ -n "$EXISTING_HTML" ] && [ -f "$EXISTING_HTML" ]; then
    # 检查这个HTML是否在15分钟内修改
    FILE_AGE=$(find "$EXISTING_HTML" -mmin -15 2>/dev/null)

    if [ -n "$FILE_AGE" ]; then
        # 验证HTML内容是否包含当前论文的关键信息（标题或DOI）
        # 检查是否包含论文标题的部分关键词（不区分大小写）
        if grep -qi "$(echo "$TITLE" | cut -d' ' -f1-3)" "$EXISTING_HTML" 2>/dev/null; then
            echo -e "${GREEN}✅ 检测到本地已有该论文的HTML缓存（最近保存）${NC}"
            SAVED_HTML="$EXISTING_HTML"
            HTML_VALID=true
        fi
    fi
fi

# 如果本地没有验证有效的HTML缓存，则需要保存
if [ "$HTML_VALID" = false ]; then
    if [ -n "$EXISTING_HTML" ]; then
        echo -e "${BLUE}   本地HTML不匹配当前论文，重新保存...${NC}"
    else
        echo -e "${BLUE}   本地未找到缓存的HTML，正在保存...${NC}"
    fi

    google-chrome --new-tab "https://doi.org/${DOI}" > /dev/null 2>&1 &
    sleep 6

    # 获取Chrome窗口ID并激活
    CHROME_WINDOW=$(xdotool search --name "chrome" | head -1)
    if [ -n "$CHROME_WINDOW" ]; then
        xdotool windowactivate $CHROME_WINDOW 2>/dev/null || true
        sleep 1

        # 滚动到页面底部确保所有内容加载
        for i in {1..10}; do
            xdotool key End
            sleep 0.3
        done
        sleep 2

        # 保存完整网页
        xdotool key ctrl+s
        sleep 2
        xdotool key Return
        sleep 4
        echo -e "${GREEN}✅ 已保存完整网页${NC}"
    else
        echo -e "${RED}⚠️  无法找到Chrome窗口${NC}"
    fi

    # 查找最新保存的HTML文件
    SAVED_HTML=$(ls -t /home/zhiping/Downloads/*.html 2>/dev/null | head -1)
fi
echo ""

# 第四步：从保存的HTML中提取补充材料链接
echo -e "${BLUE}[4/9] 从保存的HTML中提取补充材料信息...${NC}"

SUPPLEMENTARY_URLS=""
if [ -n "$SAVED_HTML" ] && [ -f "$SAVED_HTML" ]; then
    # 使用grep提取补充材料链接（补充材料通常在supplemental URL中）
    SUPPLEMENTARY_URLS=$(grep -oP 'href="[^"]*supplemental[^"]*"' "$SAVED_HTML" 2>/dev/null | sed 's/href="//;s/"$//' | head -5)

    if [ -n "$SUPPLEMENTARY_URLS" ]; then
        echo -e "${GREEN}✅ 检测到补充材料链接：${NC}"
        echo "$SUPPLEMENTARY_URLS" | while read url; do
            echo "   $url"
        done
    else
        echo -e "${GREEN}✅ 未在页面中检测到补充材料链接${NC}"
    fi
else
    echo -e "${RED}⚠️  未找到保存的HTML文件${NC}"
fi
echo ""

# 第五步：在Chrome中打开PDF链接
echo -e "${BLUE}[5/9] 在Chrome中打开PDF链接...${NC}"
PDF_URL="https://journals.aps.org/prl/pdf/${DOI}"
google-chrome --new-tab "$PDF_URL" > /dev/null 2>&1 &
sleep 8
echo -e "${GREEN}✅ 已打开PDF链接${NC}"
echo ""

# 第六步：在Chrome中保存PDF
echo -e "${BLUE}[6/9] 保存PDF文件...${NC}"
CHROME_WINDOW=$(xdotool search --name "chrome" | head -1)
if [ -n "$CHROME_WINDOW" ]; then
    xdotool windowactivate $CHROME_WINDOW 2>/dev/null || true
    sleep 1
    xdotool key ctrl+s
    sleep 4
    xdotool key Return
    sleep 6
    echo -e "${GREEN}✅ 已执行保存命令${NC}"
else
    echo -e "${RED}⚠️  无法找到Chrome窗口${NC}"
fi
echo ""

# 第七步：使用pdfplumber检查PDF中的补充材料关键字
echo -e "${BLUE}[7/9] 检查PDF中的补充材料关键字...${NC}"
sleep 2

# 查找最新下载的PDF（用于检查）
TEMP_PDF=$(ls -t /home/zhiping/Downloads/*.pdf 2>/dev/null | head -1)

if [ -n "$TEMP_PDF" ] && [ -f "$TEMP_PDF" ]; then
    # 使用Python和pdfplumber检查PDF
    SUPPLEMENTARY_CHECK=$(PDF_PATH="$TEMP_PDF" python3 << 'PYTHON_EOF'
import os
try:
    import pdfplumber

    pdf_path = os.environ.get('PDF_PATH')
    supplementary_keywords = [
        'supplementary', 'supporting information', 'additional data',
        'extended data', 'appendix', 'supplemental', 'additional files',
        'supporting material', 'supplemental material', 'online resource',
        'electronic supplementary'
    ]

    found_keywords = []

    with pdfplumber.open(pdf_path) as pdf:
        full_text = ""
        for page in pdf.pages:
            full_text += page.extract_text().lower()

    for keyword in supplementary_keywords:
        if keyword.lower() in full_text:
            found_keywords.append(keyword)

    if found_keywords:
        print("FOUND:" + ",".join(found_keywords))
    else:
        print("NOT_FOUND")

except ImportError:
    print("PDFPLUMBER_NOT_INSTALLED")
except Exception as e:
    print("ERROR:" + str(e))
PYTHON_EOF
    )

    if [[ $SUPPLEMENTARY_CHECK == FOUND* ]]; then
        FOUND_KEYWORDS="${SUPPLEMENTARY_CHECK#FOUND:}"
        echo -e "${GREEN}⚠️  检测到潜在的补充材料关键字：${NC}"
        echo -e "${GREEN}${FOUND_KEYWORDS}${NC}"
        echo ""
        echo -e "${BLUE}ℹ️  这篇论文可能存在补充材料，请访问论文页面核对！${NC}"
    elif [[ $SUPPLEMENTARY_CHECK == "NOT_FOUND" ]]; then
        echo -e "${GREEN}✅ 未在PDF中检测到补充材料关键字${NC}"
    elif [[ $SUPPLEMENTARY_CHECK == "PDFPLUMBER_NOT_INSTALLED" ]]; then
        echo -e "${RED}⚠️  pdfplumber未安装，跳过补充材料检查${NC}"
        echo -e "${BLUE}   安装: pip install pdfplumber${NC}"
    else
        echo -e "${RED}❌ 检查出错: $SUPPLEMENTARY_CHECK${NC}"
    fi
else
    echo -e "${RED}⚠️  未找到PDF进行检查${NC}"
fi
echo ""

# 第八步：查找下载的文件
echo -e "${BLUE}[8/9] 查找并验证下载的文件...${NC}"
sleep 2
DOWNLOADED_PDF=$(find /home/zhiping/Downloads -name "PhysRevLett*${YEAR:2}.pdf" -o -name "*${DOI##*/}.pdf" 2>/dev/null | head -1)

if [ -z "$DOWNLOADED_PDF" ]; then
    # 尝试其他方式查找
    DOWNLOADED_PDF=$(ls -t /home/zhiping/Downloads/*.pdf 2>/dev/null | head -1)
fi

if [ -n "$DOWNLOADED_PDF" ] && [ -f "$DOWNLOADED_PDF" ]; then
    echo -e "${GREEN}✅ 找到下载的文件: $(basename $DOWNLOADED_PDF)${NC}"
else
    echo -e "${RED}❌ 未找到下载的文件${NC}"
    exit 1
fi
echo ""

# 第九步：重命名并移动文件
echo -e "${BLUE}[9/9] 重命名并组织文件...${NC}"

# 清理文件名中的特殊字符
CLEAN_TITLE=$(echo "$TITLE" | sed 's/[/:*?"<>|]//g' | sed 's/\.$//')

# 创建目标目录
DEST_DIR="/home/zhiping/Research/Papers/$YEAR"
mkdir -p "$DEST_DIR"

# 构建目标路径
NEW_FILENAME="${YEAR}--${CLEAN_TITLE}.pdf"
DEST_PATH="$DEST_DIR/$NEW_FILENAME"

# 移动文件
mv "$DOWNLOADED_PDF" "$DEST_PATH"

# 验证
if [ -f "$DEST_PATH" ]; then
    SIZE=$(stat -c%s "$DEST_PATH" 2>/dev/null)
    echo -e "${GREEN}✅ 论文成功保存！${NC}"
    echo "   位置: $DEST_PATH"
    echo "   大小: $((SIZE / 1024)) KB"
    echo ""
    echo -e "${GREEN}🎉 所有步骤完成！${NC}"
else
    echo -e "${RED}❌ 文件移动失败${NC}"
    exit 1
fi

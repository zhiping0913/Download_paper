#!/usr/bin/env bash
# ============================================================================
# launch.sh — 完整环境变量清单 + Download_paper 启动示例
# ============================================================================
#
# 用法：
#   1) 直接跑（用当前默认值）：
#        bash examples/launch.sh
#
#   2) 需要调时先编辑本文件里的 export 行，注释是每个变量的默认值
#      和覆盖的等待点；改完再跑。
#
#   3) 只跑一次不想改文件也可以覆盖具体一项：
#        DP_PAGE_LOAD_TIMEOUT=120 bash examples/launch.sh
#
# 提示：所有 export 都可以注释掉；程序未见 env var 时会回退到硬编码默认值。
# ============================================================================

set -euo pipefail

# 定位到项目根目录（不管从哪里调）
cd "$(dirname "$0")/.."

# ---------------------------------------------------------------------------
# 1. 等待时间（秒）—— 全部由 complete_paper_extraction.py 的 _env_seconds() 读取
#    空串 / 非数字 / 负数 / 0 都会回退到默认值。
# ---------------------------------------------------------------------------

# 页面加载超时：headed / headless page.goto、每处 wait_for_load_state('networkidle')、
# APIRequestContext.get、PDF goto 都用这个。
export DP_PAGE_LOAD_TIMEOUT=120          # 默认 60

# Cloudflare Turnstile 自动点击的总预算。派生的 initial-poll = max(2, total/7.5)，
# 保证没挑战的页面在 initial-poll 秒内快速返回。
export DP_CLOUDFLARE_TIMEOUT=600         # 默认 30 (initial-poll ≈ 4)

# PDF 导航后 asyncio.sleep 的时长，等浏览器 tab 触发 download 事件。
export DP_PDF_WAIT=10                   # 默认 10

# 补充材料每个链接的 goto + Playwright download-event wait_for + 内联音频
# body-fetch 都用这个。
export DP_SUPPLEMENTAL_TIMEOUT=60       # 默认 60

# 图片 CDN 的初次 goto、auto-solve 后的重取、img_src fallback 重取。
export DP_FIGURE_TIMEOUT=60             # 默认 60

# ---------------------------------------------------------------------------
# 2. 批处理间隔（秒）—— 相邻两篇论文之间的随机 sleep
# ---------------------------------------------------------------------------

export BATCH_SLEEP_MIN=30               # 默认 30
export BATCH_SLEEP_MAX=60               # 默认 60

# ---------------------------------------------------------------------------
# 3. Chrome / 浏览器
# ---------------------------------------------------------------------------

# Chrome 可执行文件路径。留空 → config.py 按平台自动探测
# (Linux: /opt/google/chrome/chrome / /usr/bin/google-chrome …；
#  macOS: /Applications/Google\ Chrome.app/…；Windows: Program Files 下的 chrome.exe)
# export CHROME_PATH=/opt/google/chrome/chrome

# Chrome CDP 调试端口 (headed 复用 profile 用)。默认 9222
# export CHROME_DEBUG_PORT=9222

# Chrome 真实用户数据目录（有头模式使用真实 profile 过 Cloudflare 更稳）
# 默认自动检测当前用户 ~/.config/google-chrome；如脚本以非 profile 所有者运行需手动指定
# Chrome 真实用户数据目录：默认走当前用户 ~/.config/google-chrome
# 注意：必须与运行脚本的用户一致，否则权限不对导致 profile 加载失败
# Chrome 专用 profile 目录（非默认路径才能开 CDP 调试端口）
# 环境变量优先，默认使用 root 下的专用 scraping profile
export CHROME_USER_DATA_DIR="${CHROME_USER_DATA_DIR:-/root/.config/google-chrome-scraping}"

# Chrome 临时 profile 目录的父目录 (chrome_launcher.py 生成 --user-data-dir=… 时用)
# 默认 → 系统 tmp
# export CHROME_PROFILE_ROOT=/tmp/download_paper_chrome_profiles

# ---------------------------------------------------------------------------
# 4. 无头浏览器登录态缓存文件路径
#    (--refresh-headless-auth 会写这个文件，Phase 0 从这里加载 cookies)
# ---------------------------------------------------------------------------

# export DOWNLOAD_PAPER_HEADLESS_AUTH_STATE="$PWD/.auth/headless_storage_state.json"

# ---------------------------------------------------------------------------
# 5. 启用 venv (可选)
# ---------------------------------------------------------------------------

if [ -f "/home/zhiping/research-env/bin/activate" ]; then
    # shellcheck disable=SC1091
    source /home/zhiping/research-env/bin/activate
fi

# ---------------------------------------------------------------------------
# 6. 打印生效的环境变量清单
# ---------------------------------------------------------------------------

echo "──────────────────────────────────────────────────────"
echo " Download_paper env vars in effect"
echo "──────────────────────────────────────────────────────"
for v in DP_PAGE_LOAD_TIMEOUT DP_CLOUDFLARE_TIMEOUT DP_PDF_WAIT \
         DP_SUPPLEMENTAL_TIMEOUT DP_FIGURE_TIMEOUT \
         BATCH_SLEEP_MIN BATCH_SLEEP_MAX \
         CHROME_PATH CHROME_DEBUG_PORT CHROME_USER_DATA_DIR CHROME_PROFILE_ROOT \
         DOWNLOAD_PAPER_HEADLESS_AUTH_STATE; do
    val="${!v:-<default>}"
    printf "  %-38s = %s\n" "$v" "$val"
done
echo "──────────────────────────────────────────────────────"

# ---------------------------------------------------------------------------
# 7. 运行提取
# ---------------------------------------------------------------------------
# 三种输入方式任选一种（互斥）：
#   --doi   单篇 DOI
#   --file  纯 DOI 列表 (每行一个)
#   --json  更丰富的批量输入 (含 link / header)
#
# 其它可选开关：
#   --output PATH             覆盖默认输出目录
#   --force-headed            跳过无头预检，直接开有头 Chrome
#   --refresh-headless-auth   通过本机 Chrome CDP 把登录态刷入
#                             .auth/headless_storage_state.json
# ---------------------------------------------------------------------------

# 如果传了参数就用参数，否则默认跑 examples/examples.json
if [ $# -gt 0 ]; then
    python complete_paper_extraction.py "$@"
else
    python complete_paper_extraction.py --json examples/examples.json --force-headed
fi

# 其它示例（改成你要的那条即可）：
# python complete_paper_extraction.py --doi 10.1103/PhysRevLett.109.245005
# python complete_paper_extraction.py --file doi_list.txt --force-headed
# python complete_paper_extraction.py --json examples/examples.json --output ~/Downloads

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

# ★ 抓取用的 profile 目录。
# 用一个**专用目录**，不要指向你日常上网的那个 —— Chrome 的 profile 锁一次只
# 允许一个进程持有，指向日常 profile 且浏览器正开着时，launcher 起的 Chrome 会
# 把 URL 转发给现有实例后自杀，CDP 端口永远不响应（表现为"Chrome 开了但不导航"）。
# 不设这个变量 → 每次启动新建临时目录（无 cookie 累积，每篇都要过 Cloudflare）。
export CHROME_USER_DATA_DIR="${CHROME_USER_DATA_DIR:-${HOME}/.config/google-chrome-scraping}"

# profile 里的具体 profile 名，默认 Default
# export CHROME_PROFILE=Default

# Chrome 临时 profile 目录的父目录 (未设 CHROME_USER_DATA_DIR 时用)
# 默认 → 系统 tmp
# export CHROME_PROFILE_ROOT=/tmp/download_paper_chrome_profiles

# ---------------------------------------------------------------------------
# 3b. 抓取 profile 的定期重置
# ---------------------------------------------------------------------------
# CDP 驱动的 profile 会逐篇累积自动化指纹（以及 Cloudflare 给它打的标记），
# 抓到一定篇数后挑战就过不去了。设置下面这个值，程序会每 N 篇自动：
#   关掉 Chrome → 删除抓取 profile → 从干净 profile 重新播种 → 下一篇重启
# 0 或不设 = 从不重置（默认）。
export CHROME_PROFILE_REFRESH_EVERY=5

# 播种来源（干净的、人类在用的 profile）。不设则按平台自动探测：
#   Linux   ~/.config/google-chrome
#   macOS   ~/Library/Application Support/Google/Chrome
#   Windows %LOCALAPPDATA%\Google\Chrome\User Data
# export CHROME_PROFILE_SOURCE_DIR="${HOME}/.config/google-chrome"
#
# 安全护栏（重置是 rm -rf，程序会在这三种情况下拒绝执行）：
#   · CHROME_USER_DATA_DIR 未设置
#   · 抓取目录与来源目录相同        ← 防止删掉你自己的 Chrome 数据
#   · 抓取目录位于来源 profile 内部
#
# 只复制这些：Cookies、Cookies-journal、Login Data、Preferences、
#            Secure Preferences、Web Data、Local State
# ⚠️  Local State 必须跟 Cookies 一起复制 —— 它是 cookie 的加密密钥。
# 不复制 History / Cache / 扩展 / Sessions —— 甩掉累积状态正是重置的目的。

# ---------------------------------------------------------------------------
# 3c. 首次运行：播种抓取 profile
# ---------------------------------------------------------------------------
# 程序只在**重置**时播种；profile 完全不存在时得先给它一份 cookie，
# 否则第一篇就要从零过 Cloudflare。文件集合与程序内的重置保持一致。
LIVE_PROFILE="${CHROME_PROFILE_SOURCE_DIR:-${HOME}/.config/google-chrome}"
SEED_PROFILE="${CHROME_PROFILE:-Default}"
if [ ! -d "${CHROME_USER_DATA_DIR}/${SEED_PROFILE}" ] \
   && [ -d "${LIVE_PROFILE}/${SEED_PROFILE}" ]; then
    echo "⚙️  首次运行：从 ${LIVE_PROFILE} 播种抓取 profile..."
    mkdir -p "${CHROME_USER_DATA_DIR}/${SEED_PROFILE}"
    for f in Cookies Cookies-journal "Login Data" Preferences \
             "Secure Preferences" "Web Data"; do
        [ -e "${LIVE_PROFILE}/${SEED_PROFILE}/${f}" ] \
            && cp -p "${LIVE_PROFILE}/${SEED_PROFILE}/${f}" \
                     "${CHROME_USER_DATA_DIR}/${SEED_PROFILE}/" 2>/dev/null || true
    done
    [ -e "${LIVE_PROFILE}/Local State" ] \
        && cp -p "${LIVE_PROFILE}/Local State" "${CHROME_USER_DATA_DIR}/" 2>/dev/null || true
    echo "   ✓ 播种完成"
fi

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
         CHROME_PATH CHROME_DEBUG_PORT CHROME_USER_DATA_DIR CHROME_PROFILE \
         CHROME_PROFILE_ROOT CHROME_PROFILE_REFRESH_EVERY CHROME_PROFILE_SOURCE_DIR \
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

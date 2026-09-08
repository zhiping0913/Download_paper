#!/usr/bin/env bash
# ============================================================================
# launch.sh — Download_paper 完整环境变量清单 + 启动示例
# ============================================================================
#
# 用法：
#   1) 直接跑（用文件里的设置）：
#        bash examples/launch.sh
#
#   2) 传参覆盖默认输入：
#        bash examples/launch.sh --doi 10.1063/5.0256231
#        bash examples/launch.sh --json examples/spie.json --force-headed
#
#   3) 只改一次某个变量，不动文件：
#        DP_PAGE_LOAD_TIMEOUT=240 bash examples/launch.sh
#
# 每个 export 都可以注释掉；程序读不到就用硬编码默认值。注释里的「默认 N」
# 就是那个默认值。
# ============================================================================

set -euo pipefail

# 定位到项目根目录（不管从哪里调用）
cd "$(dirname "$0")/.."

# ---------------------------------------------------------------------------
# 1. Chrome —— 程序会用到【两个】Chrome 实例，各占一个端口
# ---------------------------------------------------------------------------
#
#   主实例      CHROME_DEBUG_PORT      打开论文页面、提取正文；整批论文复用同一个
#   一次性实例  CHROME_PDF_DEBUG_PORT  只用来下载 PDF；每次现从真实 profile 复制
#                                      一份，用完即删，从不被 Playwright 接管
#
# 为什么要两个：主实例自打开正文页起就被 Playwright 接管，带上了自动化指纹；
# 而且正文域名过掉的 Cloudflare 对 PDF 域名（如 pdf.sciencedirectassets.com）
# 并不算数 —— clearance cookie 绑定在签发它的主机上。
#
# 下载顺序取决于有头还是无头：
#   有头 → 先一次性实例，失败再回退主实例导航
#   无头 → 先主实例，失败了才起一次性实例
#          （能无头进去的出版商本来就没拦我们，每篇弹个窗口就白跑无头了）

# Chrome 可执行文件。留空 → config.py 按平台自动探测
# (Linux: /opt/google/chrome/chrome、/usr/bin/google-chrome …)
# export CHROME_PATH=/opt/google/chrome/chrome

export CHROME_DEBUG_PORT=9222            # 主实例 CDP 端口。默认 9222
export CHROME_PDF_DEBUG_PORT=9333        # 一次性实例 CDP 端口。默认 9333
                                         # 两个端口必须不同；被占用会自动顺延
                                         # (9333 → 9334 → …)，并发跑不会互抢

# export DP_PDF_FRESH_CHROME=0           # 设 0 彻底禁用一次性实例（两种模式都禁）
# export CHROME_PDF_PROFILE_ROOT=/tmp    # 一次性 profile 的落脚处。默认系统 tmp

# ★ 抓取用的 profile 目录 —— 用一个专用目录，别指向你日常上网那个。
# Chrome 的 profile 锁一次只允许一个进程持有：指向日常 profile 且浏览器正开着时，
# launcher 起的 Chrome 会把 URL 转发给现有实例然后自杀，CDP 端口永远不响应
# （表现就是「Chrome 开了但不导航」）。
# 不设这个变量 → 每次新建临时目录，没有 cookie 累积，每篇都要重过 Cloudflare。
export CHROME_USER_DATA_DIR="${CHROME_USER_DATA_DIR:-${HOME}/.config/google-chrome-scraping}"

# export CHROME_PROFILE=Default          # profile 名。默认 Default
# export CHROME_PROFILE_ROOT=/tmp/dp     # 临时 profile 的父目录（未设 USER_DATA_DIR 时用）
# export CHROME_DOWNLOAD_DIR="${HOME}/Downloads"   # Chrome 默认下载目录
# export USE_CHROME_MODE=persistent      # persistent（复用 profile）| remote（连已在跑的）
# export HEADLESS=false                  # true/false。Cloudflare 站点建议 false

# ---------------------------------------------------------------------------
# 2. 抓取 profile 的定期重置
# ---------------------------------------------------------------------------
# CDP 驱动的 profile 会逐篇累积自动化指纹（以及 Cloudflare 给它打的标记），
# 抓到一定篇数后挑战就过不去了。设置下面这个值，程序会每 N 篇自动：
#   关掉 Chrome → 删除抓取 profile → 从干净 profile 重新播种 → 下一篇重启
export CHROME_PROFILE_REFRESH_EVERY=5    # 0 或不设 = 从不重置（默认）

# 播种来源（干净的、人类在用的 profile）。不设则按平台自动探测：
#   Linux ~/.config/google-chrome
#   macOS ~/Library/Application Support/Google/Chrome
#   Windows %LOCALAPPDATA%\Google\Chrome\User Data
# export CHROME_PROFILE_SOURCE_DIR="${HOME}/.config/google-chrome"
#
# 安全护栏（重置是 rm -rf，程序在这三种情况下拒绝执行）：
#   · CHROME_USER_DATA_DIR 未设置
#   · 抓取目录 == 来源目录        ← 防止删掉你自己的 Chrome 数据
#   · 抓取目录在来源 profile 内部
#
# 只复制这些：Cookies、Cookies-journal、Login Data、Preferences、
#            Secure Preferences、Web Data、Local State
# ⚠️  Local State 必须跟 Cookies 一起复制 —— 它是 cookie 的加密密钥，
#     只复制其一的话所有 cookie 都解不开。
# 不复制 History / Cache / 扩展 / Sessions —— 甩掉累积状态正是重置的目的。

# ---------------------------------------------------------------------------
# 3. 首次运行：播种抓取 profile
# ---------------------------------------------------------------------------
# 程序只在【重置】时播种；profile 完全不存在时得先给它一份 cookie，
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
# 4. 等待时间（秒）—— 由 _env_seconds() 读取
#    空串 / 非数字 / 负数 / 0 都回退默认值
# ---------------------------------------------------------------------------

# 页面加载：page.goto、wait_for_load_state('networkidle')、API 请求都用这个
export DP_PAGE_LOAD_TIMEOUT=120          # 默认 120

# Cloudflare 挑战自动点击的总预算。派生的 initial-poll = max(2, total/7.5)，
# 保证没挑战的页面能快速返回
export DP_CLOUDFLARE_TIMEOUT=600         # 默认 600

# PDF 导航后 sleep 的时长，等浏览器触发 download 事件
export DP_PDF_WAIT=10                    # 默认 10

# PDF「下载已开始」判据：download 事件多久没来就算没开始
export DP_PDF_DOWNLOAD_TIMEOUT=30        # 默认 30

# PDF「下载已完成」判据：已开始后允许它慢慢下多久（慢网速调大这个）
export DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT=180      # 默认 180

# 补充材料：每个链接的 goto + download 事件等待
export DP_SUPPLEMENTAL_TIMEOUT=60                # 默认 60
export DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT=600  # 默认 600

# 图片 CDN 的 goto / 重取
export DP_FIGURE_TIMEOUT=60              # 默认 60

# ---------------------------------------------------------------------------
# 5. 重试
# ---------------------------------------------------------------------------

export DP_MAX_RETRIES=5                  # 通用下载（含 PDF）。默认 5
export DP_IMG_MAX_RETRIES=3              # 图片。默认 3
export DP_SUPP_MAX_RETRIES=5             # 补充材料。默认 5
export DP_RETRY_DELAY=1                  # 每次重试之间等几秒。默认 1

# ---------------------------------------------------------------------------
# 6. 批处理间隔（秒）—— 相邻两篇论文之间的随机 sleep
# ---------------------------------------------------------------------------

export BATCH_SLEEP_MIN=30                # 默认 30
export BATCH_SLEEP_MAX=60                # 默认 60

# ---------------------------------------------------------------------------
# 7. 输出目录
# ---------------------------------------------------------------------------

# export CAPTURED_DATA_DIR=captured_data                    # 子目录名
# export OUTPUT_DIR_DEFAULT="${PWD}/captured_data"          # 完整输出路径

# ---------------------------------------------------------------------------
# 8. 无头登录态缓存
# ---------------------------------------------------------------------------
# --refresh-headless-auth 会写这个文件，Phase 0 无头预检从这里加载 cookies。
# export DOWNLOAD_PAPER_HEADLESS_AUTH_STATE="${PWD}/.auth/headless_storage_state.json"

# ---------------------------------------------------------------------------
# 9. 启用 venv（可选）
# ---------------------------------------------------------------------------

if [ -f "/home/zhiping/research-env/bin/activate" ]; then
    # shellcheck disable=SC1091
    source /home/zhiping/research-env/bin/activate
fi

# ---------------------------------------------------------------------------
# 10. 打印生效的环境变量
# ---------------------------------------------------------------------------

echo "──────────────────────────────────────────────────────"
echo " Download_paper env vars in effect"
echo "──────────────────────────────────────────────────────"
for v in CHROME_PATH CHROME_DEBUG_PORT CHROME_PDF_DEBUG_PORT \
         CHROME_USER_DATA_DIR CHROME_PROFILE CHROME_PROFILE_ROOT \
         CHROME_PDF_PROFILE_ROOT DP_PDF_FRESH_CHROME CHROME_DOWNLOAD_DIR \
         CHROME_PROFILE_REFRESH_EVERY CHROME_PROFILE_SOURCE_DIR \
         USE_CHROME_MODE HEADLESS \
         DP_PAGE_LOAD_TIMEOUT DP_CLOUDFLARE_TIMEOUT DP_PDF_WAIT \
         DP_PDF_DOWNLOAD_TIMEOUT DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT \
         DP_SUPPLEMENTAL_TIMEOUT DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT \
         DP_FIGURE_TIMEOUT \
         DP_MAX_RETRIES DP_IMG_MAX_RETRIES DP_SUPP_MAX_RETRIES DP_RETRY_DELAY \
         BATCH_SLEEP_MIN BATCH_SLEEP_MAX \
         CAPTURED_DATA_DIR OUTPUT_DIR_DEFAULT \
         DOWNLOAD_PAPER_HEADLESS_AUTH_STATE; do
    printf "  %-42s = %s\n" "$v" "${!v:-<default>}"
done
echo "──────────────────────────────────────────────────────"

# ---------------------------------------------------------------------------
# 11. 运行提取
# ---------------------------------------------------------------------------
# 输入三选一（互斥）：
#   --doi   单篇 DOI
#   --file  纯 DOI 列表（每行一个）
#   --json  批量输入，每篇必须有 "doi"，可选 "link"（跳过 doi.org 直接开这个
#           地址）和 "header"（合并进请求头，例如 Referer）
#
# 其它开关：
#   --output PATH             覆盖输出目录
#   --force-headed            跳过无头预检，直接开有头 Chrome
#   --refresh-headless-auth   通过本机 Chrome CDP 把登录态刷进
#                             .auth/headless_storage_state.json
#
# 无头 / 有头是自动判断的：Phase 0 先用无头访问，出版商在
# HEADLESS_ACCESSIBLE_PUBLISHERS 里就全程无头（ACS、Nature、AIP、Cambridge、
# OUP、Springer 等）；否则转有头。加 --force-headed 可跳过这个判断。
# ---------------------------------------------------------------------------

if [ $# -gt 0 ]; then
    python complete_paper_extraction.py "$@"
else
    python complete_paper_extraction.py --json examples/examples.json --force-headed
fi

# 其它示例：
# python complete_paper_extraction.py --doi 10.1021/acs.nanolett.8b05070   # ACS，全程无头
# python complete_paper_extraction.py --doi 10.3788/PI.2023.R05            # researching.cn
# python complete_paper_extraction.py --json examples/spie.json            # 用 link 指定 SPIE 版
# python complete_paper_extraction.py --file doi_list.txt --force-headed
# python complete_paper_extraction.py --json examples/examples.json --output ~/Downloads

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
#   主实例      CHROME_DEBUG_PORT      打开论文页面、提取正文；每篇结束后关闭重开
#   辅助实例    CHROME_AUX_DEBUG_PORT  取数阶梯的最底层：PDF、图片、补充材料、
#                                      API 页面；用 aux_dir，每次重建，用完即删，
#                                      从不被 Playwright 接管
#                                      （旧名 CHROME_PDF_DEBUG_PORT 仍可用）
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
export CHROME_AUX_DEBUG_PORT=9333        # 辅助实例 CDP 端口。默认 9333
                                         # 两个端口必须不同；被占用会自动顺延
                                         # (9333 → 9334 → …)，并发跑不会互抢

# export DP_PDF_FRESH_CHROME=0           # 设 0 彻底禁用一次性实例（两种模式都禁）
                                         # 换句话说：摘掉取数阶梯的 fresh 层，
                                         # referer 层也会连带失效（见第 2b 节）

# ★ 抓取 profile 的根目录。程序在它下面建两个一次性目录：
#     main_dir  正文页的共享实例
#     aux_dir   辅助（一次性）实例：PDF、图片、补充材料、API 页面
# 两个都是每次开浏览器「先删再建」，绝不复用被自动化污染过的 profile，
# 所以不需要指向你日常上网那个 profile（那个只作为播种来源，程序只读不写）。
# 不设 → 默认 /tmp/dp_profiles_xxxxxx，后缀按启动时间做种随机生成，
#        所以并发跑多个批次天然互不干扰（每个进程一个 root）。
# ⚠️ 显式指定时，并发的批次要各给各的 —— 两个 Chrome 抢同一个 profile 锁会打架。
export CHROME_PROFILE_ROOT="${CHROME_PROFILE_ROOT:-/tmp/dp_profiles}"

# export CHROME_PROFILE=Default          # profile 名。默认 Default
# export CHROME_DOWNLOAD_DIR="${HOME}/Downloads"   # Chrome 默认下载目录
# export USE_CHROME_MODE=persistent      # persistent（复用 profile）| remote（连已在跑的）
# export HEADLESS=false                  # true/false。Cloudflare 站点建议 false

# ---------------------------------------------------------------------------
# 2. 抓取 profile 的来源
# ---------------------------------------------------------------------------
# CDP 驱动的 profile 会逐篇累积自动化指纹（以及 Cloudflare 给它打的标记），
# 抓到一定篇数后挑战就过不去了。所以抓取 profile **永不复用**：每次开浏览器
# 都是「删掉旧的 → 重建」，每篇论文结束后浏览器也会关掉，下一篇重来一遍。
#
# 重建时拿什么填：
#   FRESH_PROFILE=0（默认）且下面的源目录有效 → 从真实 profile 播种 cookie
#   FRESH_PROFILE=1 或源目录无效              → 空 profile（零登录态，
#                                              指纹最干净，但没有机构订阅）
# export FRESH_PROFILE=1

# ★ 播种时剔除 bot manager 的 cookie（`__uzm*` / `__ss*` / perfdrive 整套），
# 只保留订阅态。同一份 Cookies 里两者都有：不剔除的话，每个「全新」的一次性
# Chrome 都戴着刚被标记过的那张身份证出门 —— 实测 FRESH_PROFILE=1（完全无 cookie）
# 能直接下到 IOP / ScienceDirect 的 PDF，而播种过的 profile 撞 perfdrive。
# 这正是「既要订阅态、又不要案底」的折中。设 0 恢复整份照抄，便于 A/B。
# export DP_SEED_DROP_BOT_COOKIES=1

# ★ cookie 解密用的密钥存放方式。Linux 上 Chrome 的 cookie 密钥在系统钥匙环里，
# 接不到就只能解开极少数条目（实测 1712 条只剩 22 条、出版商 cookie 一条不剩）。
# 程序在 Linux + 有 DBUS 会话时自动加 --password-store=gnome-libsecret；
# 取值因机器而异（kwallet5 在某些机器上无效），置空则完全不加。
# export CHROME_PASSWORD_STORE=gnome-libsecret

# 播种来源（干净的、人类在用的 profile）。不设则按平台自动探测：
#   Linux ~/.config/google-chrome
#   macOS ~/Library/Application Support/Google/Chrome
#   Windows %LOCALAPPDATA%\Google\Chrome\User Data
# export CHROME_PROFILE_SOURCE_DIR="${HOME}/.config/google-chrome"
#
# 安全护栏：重建是 rm -rf，所以目标目录一旦解析成【真实 Chrome profile】
# （日常 profile、播种来源、或平台默认路径），程序直接拒绝，改用临时目录。
# 你自己的 Chrome 数据不会被碰。
#
# 只复制这些：Cookies、Cookies-journal、Login Data、Preferences、
#            Secure Preferences、Web Data、Local State
# ⚠️  Local State 必须跟 Cookies 一起复制 —— 它是 cookie 的加密密钥，
#     只复制其一的话所有 cookie 都解不开。
# 不复制 History / Cache / 扩展 / Sessions —— 甩掉累积状态正是重建的目的。

# ---------------------------------------------------------------------------
# 2b. 取数阶梯（4 层回退）—— PDF / 图片 / 补充材料 / API 页面共用同一套顺序
# ---------------------------------------------------------------------------
#
#   request   裸 HTTP，带上正文页会话的 cookies（按目标 host 作用域过滤）
#   tab       在已打开论文的那个浏览器里开新标签页 —— 会话、指纹都与正文页相同
#   fresh     全新播种 profile 的一次性 Chrome（辅助实例 aux_dir，用完即删）
#   referer   一次性 Chrome 先打开【来路页】，再从它「点击」跳到目标（仅下载类）
#
# 某一层失败会自动往下回退，全部注释掉也能正常跑 —— 默认顺序就是对的。
#
# ⚠️ 取值是「从哪一层开始」，而且是【截断】语义 —— 指定那层连同其后所有层：
#       DP_FETCH_PDF=fresh    →  ('fresh', 'referer')      不是「只有 fresh」
#       DP_FETCH_PDF=referer  →  ('referer',)              要只跑一层就指定最后那层
#       DP_FETCH_PDF=request  →  ('request','tab','fresh','referer')
#
# ⚠️ 不设的时候走的是【调用方给的完整顺序】，而不是 request→tab→fresh→referer：
#       有头 PDF   ('fresh', 'tab', 'referer')     ← 先一次性 Chrome
#       无头 PDF   ('tab', 'fresh', 'referer')
#       其它资源   ('tab', 'fresh')
#    有头之所以把 fresh 排在 tab 前面是有实测支撑的：主实例自打开正文页起就被
#    Playwright 接管、带着自动化指纹，而且正文域名过掉的 Cloudflare 对 PDF 域名
#    （如 pdf.sciencedirectassets.com）并不算数 —— clearance cookie 绑在签发它的
#    主机上。「截断」表达不了这种重排，所以两种语义并存；一旦在这里显式设了值，
#    拿到的就是截断顺序，那个重排就没了。没有特殊理由就让它空着。
#
# 写错值不会报错，会被忽略并退回默认顺序（启动脚本里的一个拼写错误不该中断下载）。

# export DP_FETCH_ORDER=tab              # 全局：四类资源统一从这一层开始
# export DP_FETCH_PDF=tab                # 单类覆盖，优先级高于 DP_FETCH_ORDER
# export DP_FETCH_FIGURE=request
# export DP_FETCH_SUPPLEMENT=request
# export DP_FETCH_API=tab                # IOP 的 /data 这类页面

# 第 4 层的「来路页」从哪来：
#   普通流程 → 主流程钉住的 metadata['_landing_url']，零配置，白拿这一层
#   --json   → 顶层 "referer" 键；没有则找 "header" 里的 referer（不分大小写）
#   两者都没有 → 直接跳过这一层（不拿 "link" 顶替：pdf-only 模式下 link 不一定给，
#               给了也不一定是本文的文章页）
#
# ⚠️ 这一层必须靠【点击】而不是设 HTTP 头。实测（本地服务器抓真实请求头）：直接启动
#    到 PDF 链接会发出「无 Referer + Sec-Fetch-Site: none」，形同凭空直达；而用 CDP 的
#    Network.setExtraHTTPHeaders 补一个 Referer，又会造出「有 Referer 却缺
#    Sec-Fetch-User」的组合，比不带更可疑。只有受信任的点击能同时给出 Referer、
#    same-origin 和 Sec-Fetch-User: ?1，与真人点击逐字段一致。
#
# 还有两个老开关是同一套阶梯的另一种表达，它们各自在别处定义（别在这里重复设）：
#   DP_PDF_FRESH_CHROME=0  = 摘掉 fresh 层（连带 referer 也没得用）—— 见第 1 节
#   DP_HTTP_FIRST=0        = 摘掉 request 层 —— 见第 3 节

# ---------------------------------------------------------------------------
# 3. 等待时间（秒）—— 由 _env_seconds() 读取
#    空串 / 非数字 / 负数 / 0 都回退默认值
# ---------------------------------------------------------------------------

# 页面加载：page.goto、wait_for_load_state('networkidle')、API 请求都用这个
export DP_PAGE_LOAD_TIMEOUT=120          # 默认 120

# Cloudflare 挑战自动点击的总预算。派生的 initial-poll = max(2, total/7.5)，
# 保证没挑战的页面能快速返回
export DP_CLOUDFLARE_TIMEOUT=60          # 默认 60。注意它同时决定「等 widget 出现」
                                         # 的窗口（本值/7.5，此处 8s）——没有挑战的
                                         # 页面要等满这个窗口才能断定无挑战，所以别
                                         # 随手调大

# PDF 快路径探测：一次性 Chrome 启动后、附着 CDP 点验证框之前，先等这么久
# 看 PDF 会不会自己落盘。必须短 —— 会弹验证框的出版商永远不会在这个窗口里
# 落盘，等待只是在推迟点框（ScienceDirect 实测：3 秒下首次点框在 +11 秒）。
export DP_PDF_FASTPATH_WAIT=5            # 默认 5。等 Chrome 启动导航自己出结果：
                                         # 文件落盘就完全不附着 CDP；若先出现页面
                                         # （挑战/付费墙）则立即转 CDP，不白等

# PDF「下载已开始」判据：download 事件多久没来就算没开始
export DP_PDF_DOWNLOAD_TIMEOUT=30        # 默认 30

# PDF「下载已完成」判据：已开始后允许它慢慢下多久（慢网速调大这个）
export DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT=600       # 默认 60；慢网/超大 PDF 再调大

# 补充材料：每个链接的 goto + download 事件等待
export DP_SUPPLEMENTAL_TIMEOUT=60                # 默认 60
export DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT=120  # 默认 120；大 DOCX/MP4 再调大

# 图片 / 补充材料：先直接 HTTP 请求（UA + Referer），拿不到网页以外的真文件才回退浏览器。
# export DP_HTTP_FIRST=0                 # 设 0 = 跳过直接请求，全部走浏览器。默认 1
                                         # 即：摘掉取数阶梯的 request 层（见第 2b 节）
# export DP_HTTP_USER_AGENT="Mozilla/5.0 ..."   # 直接请求用的 UA。默认一个 Linux Chrome UA

# 图片 CDN 的 goto / 重取
export DP_FIGURE_TIMEOUT=60              # 默认 60

# ---------------------------------------------------------------------------
# 4. 重试
# ---------------------------------------------------------------------------

export DP_MAX_RETRIES=5                  # 通用下载（含 PDF）。默认 5
export DP_IMG_MAX_RETRIES=3              # 图片。默认 3
export DP_SUPP_MAX_RETRIES=5             # 补充材料。默认 5
export DP_RETRY_DELAY=90                 # PDF：每次重试前固定等这么久（不递增）。默认 90

# ---------------------------------------------------------------------------
# 5. 批处理间隔（秒）—— 相邻两篇论文之间的随机 sleep
# ---------------------------------------------------------------------------

export BATCH_SLEEP_MIN=30                # 默认 30
export BATCH_SLEEP_MAX=60                # 默认 60

# ---------------------------------------------------------------------------
# 6. 输出目录
# ---------------------------------------------------------------------------

# export CAPTURED_DATA_DIR=captured_data                    # 子目录名
# export OUTPUT_DIR_DEFAULT="${PWD}/captured_data"          # 完整输出路径

# ---------------------------------------------------------------------------
# 7. 无头登录态缓存
# ---------------------------------------------------------------------------
# --refresh-headless-auth 会写这个文件，Phase 0 无头预检从这里加载 cookies。
# export DOWNLOAD_PAPER_HEADLESS_AUTH_STATE="${PWD}/.auth/headless_storage_state.json"

# ---------------------------------------------------------------------------
# 8. 启用 venv（可选）
# ---------------------------------------------------------------------------

if [ -f "/home/zhiping/research-env/bin/activate" ]; then
    # shellcheck disable=SC1091
    source /home/zhiping/research-env/bin/activate
fi

# ---------------------------------------------------------------------------
# 9. 打印生效的环境变量
# ---------------------------------------------------------------------------

echo "──────────────────────────────────────────────────────"
echo " Download_paper env vars in effect"
echo "──────────────────────────────────────────────────────"
for v in CHROME_PATH CHROME_DEBUG_PORT CHROME_AUX_DEBUG_PORT \
         CHROME_PROFILE_ROOT CHROME_PROFILE \
         DP_PDF_FRESH_CHROME CHROME_DOWNLOAD_DIR \
         FRESH_PROFILE CHROME_PROFILE_SOURCE_DIR CHROME_PASSWORD_STORE \
         DP_FETCH_ORDER DP_FETCH_PDF DP_FETCH_FIGURE \
         DP_FETCH_SUPPLEMENT DP_FETCH_API \
         DP_HTTP_FIRST DP_HTTP_USER_AGENT \
         USE_CHROME_MODE HEADLESS \
         DP_PAGE_LOAD_TIMEOUT DP_CLOUDFLARE_TIMEOUT DP_PDF_FASTPATH_WAIT \
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
# 10. 运行提取
# ---------------------------------------------------------------------------
# 输入三选一（互斥）：
#   --doi   单篇 DOI
#   --file  纯 DOI 列表（每行一个）
#   --json  批量输入，每篇必须有 "doi"，可选 "link"（跳过 doi.org 直接开这个
#           地址）、"pdf_link"（直接给 PDF 地址，连论文页面都不访问）和
#           "header"（合并进请求头，例如 Referer）
#
# 其它开关：
#   --output PATH             覆盖输出目录
#   --force-headed            跳过无头预检，直接开有头 Chrome
#   --pdf-only                只下 PDF：跳过图片/补充材料下载和 Markdown 生成，
#                             metadata.json / crossref.json 照常写
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
# python complete_paper_extraction.py --file doi_list.txt --pdf-only        # 只要 PDF
# python complete_paper_extraction.py --json pdf_links.json                 # 给了 pdf_link
#   → pdf_links.json: {"article":[{"doi":"10.1364/...","pdf_link":"https://.../x.pdf"}]}
#     给了 pdf_link 就自动进入 pdf-only，跳过预检和 doi.org，元数据走 Crossref

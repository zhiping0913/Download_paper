# Download_paper — AI 友好的学术论文自动提取工具

## 项目简介

为解决 AI agent 在自主科研探索过程中**获取文献难**、**给 PDF 却难以正确识别文档格式和公式**等痛点，本推出本项目，便于 AI 高效获取和处理论文。

本项目通过**无头或有头浏览器直接访问论文网页**，从网页中提取格式正确的论文全文，并转为 **AI 友好的 Markdown 文件**进行保存，同时下载论文的 **PDF、全部高清原图和补充材料**以供研究。

### 优势

- ✅ **全文** — 不依赖摘要或片段，提取完整文章正文
- ✅ **排版正确** — 保留章节层级、段落结构
- ✅ **公式正确** — LaTeX 公式完整保留，MathJax/MathML 无缝转换
- ✅ **补充材料完整** — 自动发现并下载数据集、视频等附件
- ✅ **高清原图** — 优先获取期刊提供的高分辨率版本
- ✅ **Markdown 化** — 最终输出 AI 原生友好的 `.md` 文件
- ✅ **引用保真** — 参考文献按原网页的写法原样输出，并保留其中的链接（不再生成 BibTeX 代码块）

### 网络要求

本项目要求在有相关期刊访问权限的网络内使用（如**校园网**或机构 VPN）。

---

## 工作流说明

本文档说明 `complete_paper_extraction.py` 的主工作流、各个 publisher 的接口契约，以及新增 publisher 时需要遵守的边界。

## 环境变量快速配置

程序所有设备相关的路径、端口、超时、重试等参数都支持通过环境变量覆盖。在新机器上运行时，先根据本机情况 `export` 以下变量，再启动脚本即可，无需修改源码。

**Chrome / 浏览器相关：**

```bash
export CHROME_PATH=/opt/google/chrome/chrome          # Chrome 可执行文件路径
export CHROME_PROFILE=Default                          # profile 名称
export CHROME_DEBUG_PORT=9222                          # 主实例 CDP 端口
export CHROME_AUX_DEBUG_PORT=9333                      # 辅助（一次性）实例 CDP 端口
                                                       # 旧名 CHROME_PDF_DEBUG_PORT 仍可用
export DP_PDF_FRESH_CHROME=1                           # 0 = 禁用一次性实例
export CHROME_DOWNLOAD_DIR=/root/Downloads             # Chrome 默认下载目录
# ❌ HEADLESS / USE_CHROME_MODE 已删除：没有任何代码读它们。有头还是无头由
#    Phase 0 按出版商和预检结果自己判（HEADLESS_ACCESSIBLE_PUBLISHERS），
#    要强制有头用 --force-headed
export DP_HTTP_USER_AGENT="Mozilla/5.0 ..."            # 直接请求用的 UA（可选）

# 取数阶梯：request（带 cookies 的裸 HTTP）→ tab（现有浏览器新标签页）
#         → fresh（一次性 Chrome）→ referer（一次性 Chrome 先开来路页再点击跳转，仅下载类）
# 取值是「从哪一层开始」，失败自动向下回退
export DP_FETCH_ORDER=tab                              # 全局默认（默认 tab）
export DP_FETCH_PDF=tab                                # 单类覆盖：pdf
export DP_FETCH_FIGURE=tab                             #           图片
export DP_FETCH_SUPPLEMENT=tab                         #           补充材料
export DP_FETCH_API=tab                                #           API / 页面（如 IOP 的 /data）
export DP_HTTP_FIRST=1                                 # 旧开关：0 = 跳过 request 这一层

# profile（见下节「程序怎么用你的 Chrome profile」）
export CHROME_PROFILE_ROOT=/tmp/dp_profiles_ab12cd     # 抓取 profile 的根目录（可选）
export CHROME_PROFILE_SOURCE_DIR=/root/.config/google-chrome   # 播种来源（可选）
export FRESH_PROFILE=0                                 # 1 = 不播种，用空 profile
export DP_SEED_DROP_BOT_COOKIES=1                      # 播种时剔除反爬 cookie（0 = 整份照抄）
```

### 程序怎么用你的 Chrome profile

**你的真实 profile 只被读取，永远不会被写入或删除。** 程序抓取时用的是另外两个
一次性目录：

```
$CHROME_PROFILE_ROOT/
├── main_dir   ← 主实例（打开论文页、提取正文）
└── aux_dir    ← 辅助（一次性）实例：PDF、图片、补充材料、API 页面
```

**每次拉起 Chrome 都会重建这两个目录**（先 `rm -rf` 再新建），批量任务里每篇论文
结束后浏览器也会关掉，下一篇重来一遍。原因是被 CDP 驱动过的 profile 会累积自动化
指纹，用着用着 Cloudflare 就不放行了 —— 所以干脆不复用。

❌ 反检测补丁 `_stealth_js` 与 `DP_STEALTH_JS` **已整个删除**。实测（纯 CDP 读、
不注入任何补丁）我们自己启动的 Chrome 本来就报 `navigator.webdriver === false`
（boolean，实例无自有属性，`Navigator.prototype` 上是 `[native code]` getter）——
与真人逐项一致；`true` 只出现在带 `--enable-automation` 启动的 Chrome，那是
Playwright **自己启动**浏览器时才加的。补丁反而把这个正常的 `false` 改成真人不可能
的 `undefined`，并注入假 plugins（实测 `plugins=0`，所以那个分支一直在跑，而不是
此前记载的"永不执行"）。加 `--disable-blink-features=AutomationControlled` 也没有
任何区别。

重建时往里面填什么，只看两个条件：

| 条件 | 结果 |
|---|---|
| `FRESH_PROFILE=0`（默认）且 `CHROME_PROFILE_SOURCE_DIR` 有效 | 从你的真实 profile 复制 cookies 等文件（**播种**） |
| `FRESH_PROFILE=1`，或源目录无效 | 空 profile（零登录态） |

- **「有效」** = 该目录存在**且**含 `CHROME_PROFILE` 指定的子目录（默认 `Default`）。
  路径写错会被当作「没有源」，不会静默产出一个没 cookie 的 profile。
- **播种复制哪些文件**：`Cookies`、`Cookies-journal`、`Login Data`、`Preferences`、
  `Secure Preferences`、`Web Data`、`Local State`。不复制 History / Cache / 扩展 /
  Sessions —— 甩掉累积状态正是重建的目的。
  ⚠️ `Local State` 必须跟 `Cookies` 一起复制：它是 cookie 的加密密钥，只复制其一
  的话所有 cookie 都解不开。
- **什么时候该用 `FRESH_PROFILE=1`**：站点按浏览器指纹打分时（如 SPIE 走 Imperva
  Incapsula），播种进来的 cookie 反而扣分。代价是零登录态 —— 依赖机构订阅才能看
  全文的文章别开这个开关。

**`CHROME_PROFILE_ROOT` 默认值**是 `/tmp/dp_profiles_xxxxxx`，后缀按启动时间 + pid
做种随机生成，**每个进程一个**，所以并发跑多个批次天然互不干扰。
⚠️ 如果你显式指定了 root，并发的批次必须各给各的 —— 两个 Chrome 抢同一个 profile
锁会打架。

**运行结束时会自动清理**（正常退出、异常、Ctrl-C 都会走到）：自动生成的 root 整个
删除；你显式指定的 root 只清掉里面的 `main_dir`/`aux_dir`（连同旧名 `pdf_dir`），root 本身保留。被活着的
Chrome 占用的目录会跳过。

**安全护栏**：目标目录一旦解析成真实 Chrome profile（日常 profile、播种来源、或平台
默认路径），程序直接拒绝并改用临时目录 —— 重建是 `rm -rf`，不能让它落到你自己的
Chrome 数据上。

> **程序会用到两个 Chrome 实例，各占一个端口**，两个端口都可用环境变量指定：
>
> | 实例 | 端口变量 | 默认 | 用途 |
> |---|---|---|---|
> | 主实例 | `CHROME_DEBUG_PORT` | 9222 | 打开论文页面、提取正文；用 `main_dir`，每篇论文结束后关闭重开 |
> | 辅助（一次性）实例 | `CHROME_AUX_DEBUG_PORT` | 9333 | 取数阶梯最底层：PDF、图片、补充材料、API 页面；用 `aux_dir`，每次重建、用完即删 |
>
> 旧名 `CHROME_PDF_DEBUG_PORT` 仍会被读取（改名是因为它已不只用于 PDF）。
> 两个端口必须不同。辅助实例的端口若被占用会自动顺延（9333 → 9334 → …），
> 所以同时跑多个任务不会互相抢浏览器。有头运行时 PDF 优先走辅助实例，
> 无头运行时反过来 —— 先用主实例，失败了才起辅助实例（见「PDF 下载顺序」）。

**输出目录：**

```bash
export CAPTURED_DATA_DIR=captured_data                 # 捕获数据子目录名
export OUTPUT_DIR_DEFAULT=/home/coze/Download_paper/captured_data  # 完整输出目录路径
```

⚠️ **Windows：输出根目录越深，文章目录名就越短。** Windows 拒绝任何超过 MAX_PATH(260)
的路径，而且报的是 `[Errno 2] No such file or directory` —— 对一个明明存在的目录说
"不存在"，极具误导性。实测过一次：156 字符的标题放在
`C:\Users\…\captured_data` 下，补充材料路径达 272 字符（超 12），于是 `paper.pdf`
和图片都下来了、**只有补充材料全军覆没**。

现在 `organize_paper_output()` 会按根目录的实际长度反推标题上限（Linux 不受影响，
仍是 150）。所以在 Windows 上把 `OUTPUT_DIR_DEFAULT` 指向较短的路径（如 `D:\papers`），
能换来更完整的目录名；根目录深到连下限都放不下时，程序会明确告警而不是静默截断。

**超时配置（单位：秒）：**

```bash
export DP_PAGE_LOAD_TIMEOUT=120
export DP_CLOUDFLARE_TIMEOUT=60               # 见下方说明，别随手调大
export DP_PDF_DOWNLOAD_TIMEOUT=30             # 判「下载是否开始」
export DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT=60    # 判「下载是否完成」，慢网再调大
export DP_SUPPLEMENTAL_TIMEOUT=60
export DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT=120  # 大文件 DOCX/MP4 再调大
export DP_FIGURE_TIMEOUT=60
export DP_INPAGE_FETCH_TIMEOUT=90             # 页面内 fetch / 读响应体的死锁断路器
export DP_HTTP_TOTAL_TIMEOUT=600              # 单个直接下载的总时限（视频靠它兜底）
```

⚠️ `DP_INPAGE_FETCH_TIMEOUT` 和上面几个不是一类东西。`page.evaluate()`、
`response.body()`、`download.save_as()` 都**不接受 `timeout=`、也不受
`set_default_timeout` 管辖** —— 网络一抖，页面内的 `fetch()` promise 永不
settle，整个批次就停在那里不动了（而不是报错重试）。这个值是**死锁断路器**：
慢链路调**大**，别调小。详见 CLAUDE.md「卡死防线」。

⚠️ 下载落盘一律用 `download.save_as()`，**不要** `path()` + `shutil.copy`：`path()`
给的是 Playwright 自己 artifacts 目录里的文件，页面一关就被删，于是「取到路径」到
「复制走」之间的每一行都可能把一次**已经成功**的下载弄丢（实测在 IOP 上丢过一次）。

**重试配置：**

```bash
export DP_MAX_RETRIES=5
export DP_RETRY_DELAY=90                      # PDF：每次重试前固定等这么久（不递增）
export DP_IMG_RETRY_DELAY=10                  # 图片：单独一个小得多的间隔
export DP_IMG_MAX_RETRIES=3
export DP_SUPP_MAX_RETRIES=5
export DP_REFERER_PAGE_RETRIES=1              # 第 4 层来路页被拦时重载几次
```

⚠️ `DP_RETRY_DELAY` 是**一篇之内唯一的节流** —— `BATCH_SLEEP` 只隔开篇与篇。而这个
等待此前只写在 `except` 分支里，阶梯各层却是「返回 `None`」报告失败的，于是重试实际上
**零间隔连打**。对按 IP 计分的拦截器（IOP 的 Radware）来说那是最差的形状：实测零间隔
连打 5 次一次都没成。两次运行都是**封锁一解除就成功**，而不是第几次重试才成功：
零节流那次在第 4 次、约 4–5 分钟时成功；60 秒节流那次在第 5 次、约 6 分钟时成功，
且那一次完全没有出现挑战页。所以默认给到 **90 秒**（4 次间隔 ≈ 6 分钟纯等待）。

⚠️ 图片**不共用**这个值，走 `DP_IMG_RETRY_DELAY`（默认 10 秒）：取不到的图就是不存在，
一篇几十张，没理由让它们反复付 PDF 的反爬节流。实测高清位 40 秒、回退位 20 秒。

**启动示例：**

```bash
cd /path/to/Download_paper
export CHROME_PROFILE_SOURCE_DIR=/root/.config/google-chrome   # 播种来源（可选）
export CHROME_DEBUG_PORT=9222
export DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT=600
export DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT=1200
python3 -u complete_paper_extraction.py --doi 10.1063/5.0256231
```

> **提示**：
> - 涉及 Cloudflare / AIP / Radware 等反爬验证的站点，建议先用你**日常的 Chrome**
>   手动访问一次并过掉验证，让 cookies 落入真实 profile —— 抓取时会从那里播种。
>   注意别去改抓取用的 `main_dir`/`aux_dir`，它们每次启动都会被重建。
> - 路径类变量建议使用绝对路径，避免 relative path 在不同工作目录下解析错误。
> - 未设置的变量会自动使用上表中的默认值。

## 主工作流

入口函数是：

```python
complete_extraction_workflow(doi, output_file=None, force_headed=False,
                             link=None, extra_headers=None,
                             pdf_only=False, pdf_link=None)
```

**pdf-only 模式**（`--pdf-only`，或 `--json` 里给 `pdf_link`）：老文章和会议短文的
网页常常没有正文，此时只要 PDF。`pdf_only=True` 时主流程照常访问论文页面拿到 PDF
链接并下载，但跳过图片/补充材料下载和 Markdown 生成；`pdf_link` 更进一步，连论文
页面都不访问 —— 跳过 Phase 0 预检和 `doi.org/{doi}`，元数据全部取自 Step 0 的
Crossref 响应，只为取文件起一次浏览器。两层的有头/无头都由同一个 Crossref 出版商
闸门决定（`_crossref_headless_publisher()`，主流程判 Phase 0 用的也是它）：不在
`HEADLESS_ACCESSIBLE_PUBLISHERS` 里的出版商直接用有头一次性 Chrome，`--force-headed`
无条件优先。两种情况都照常写 `metadata.json` 和
`crossref.json`；PDF 未下成则返回 `None`（批次记为失败），但 `metadata.json` 里
记着 `pdf_link`，可据此重试。

主流程只负责统一调度，不直接处理具体出版商的网页结构。它的职责是：

1. 准备输出目录。
   默认输出到项目下的 `captured_data`。每篇论文会先建立 DOI 缓存目录：
   `output_dir / doi.replace("/", "_")`。

2. 构造 DOI 跳转 URL。

   ```text
   https://doi.org/{doi}
   ```

3. 获取元数据并进行两阶段 publisher 判断。
   
   **阶段一（Crossref 元数据决策）**：先通过 Crossref API 获取 DOI 的元数据，检查 `publisher` 字段是否包含以下出版商名称：
   
   ```python
   HEADLESS_ACCESSIBLE_PUBLISHERS = ["nature", "aip", "cambridge", "springer", "oup"]
   ```
   
   - 如果匹配且 `force_headed=False`：进入阶段二（无头预检）。
   - 如果不匹配或 `force_headed=True`：跳过阶段二，直接使用有头 Chrome。
   
   **阶段二（Phase 0 无头预检）**：启动无头 Chromium 访问 DOI，根据最终跳转 URL 进行备选 publisher 判断。
   这是对阶段一的补充，确保在 Crossref 信息不完整或延迟高的情况下仍可做出正确决策。

4. 根据 URL/DOI 判断 publisher。
   规则位于 `publisher/orchestrator.py` 的 `detect_publisher_from_url()`。

5. 创建对应的 `PublisherHandler`。

6. 调用 handler 的统一接口：

   ```python
   await handler.extract_all(captured=captured_data)
   handler.convert_to_markdown(...)
   ```

7. 下载 PDF、图片、补充材料。

8. 保存 Markdown 和 metadata JSON。

### PDF 下载用独立 Chrome

共享浏览器从打开论文页起就被 Playwright 接管，带上了自动化指纹；而且不少出版商的
PDF 在**另一个域名**上（ScienceDirect 的 `pdf.sciencedirectassets.com`），论文页过了
Cloudflare 也不算数 —— clearance cookie 绑定在签发它的主机上。

所以 PDF 由 `chrome_session.py` 单独起一个 Chrome 下载：独立端口、独立 profile
（`aux_dir`，每次重建）、全程不接 Playwright，用完即删。

同一个实例现在也是图片、补充材料和 API 页面的最后一层 —— 它们的失败原因是同一个
（IOP 的 /data 页要人手点、Science 的补充材料要过 Cloudflare），所以用同一条出路。
`open_url_in_fresh_chrome(..., want_html=True)` 让它除了下载文件之外还能交出页面 HTML。

`chrome_session.py` 合并了原来的 `chrome_launcher.py`(启动/关闭)、`cf_bypass_cdp.py`
(纯 CDP 过挑战) 与 `fresh_chrome.py`(一次性实例) —— 三者做的是同一件事，且各自
重复实现了写下载偏好、轮询 CDP 端口、拼 Chrome 参数。主要接口：

- `launch_chrome()` / `kill_chrome()` — 共享实例的启动与关闭
- `bypass_cloudflare_cdp()` / `has_cf_clearance_cdp()` — 纯 CDP 过挑战
- `open_url_via_cdp(url, port, ...)` — 「不接 Playwright、纯 CDP 打开并过挑战」，
  论文页预载与 PDF 下载共用
- `open_url_in_fresh_chrome(url, ...)` — 起新 Chrome + 打开页面，返回 session
- `prepare_profile_dir()` — 「先删再建，再决定播种或留空」，两个实例共用这一条路径
- `cleanup_profile_root()` / `sweep_stale_profiles()` — 运行结束时清理、启动时扫掉
  上次崩溃留下的目录（只删没有活进程持有的）
- `seed_profile()` / `write_chrome_preferences()` — profile 播种与偏好写入

环境变量：`CHROME_AUX_DEBUG_PORT`(默认 9333，被占用会自动顺延；旧名
`CHROME_PDF_DEBUG_PORT` 仍兼容)、
`CHROME_PROFILE_ROOT`、`CHROME_PROFILE_SOURCE_DIR`、`CHROME_PROFILE`、
`FRESH_PROFILE`、`DP_PDF_FRESH_CHROME=0`(关闭该路径，直接走 Playwright)

## Publisher 判断

当前 `detect_publisher_from_url()` 的主要规则：

- `10.1038`、`nature.com`、`springer.com`、`s41...` -> `nature`
- `10.1103`、`journals.aps.org`、`prl/pre/pra` -> `aps`
- `10.1063`、`pubs.aip.org`、`aip.scitation.org` -> `aip`
- `10.1088`、`iopscience.iop.org` -> `iop`
- `10.1017`、`cambridge.org` -> `cambridge`
- `10.1093`、`academic.oup.com` -> `oup`
- `10.3390`、`mdpi.com` -> `mdpi`
- `10.3788`、`researching.cn` -> `researching` **(中国激光杂志社；域名判断需排在 DOI 之前)**
- `10.1117`、`spiedigitallibrary.org` -> `spie` **(正文走 POST API；`10.3788` 归中国激光杂志社，需用 `--json` 的 `link` 指定 SPIE 页面)**
- `10.1002`、`onlinelibrary.wiley.com` -> `wiley` **(view-source 取 LaTeX；表格 rowspan 需按网格渲染)**
- `10.1021`、`pubs.acs.org` -> `acs` **(Silverchair 平台；图/表/Scheme 共用包装但独立编号)**
- `10.1109`、`ieeexplore.ieee.org` -> `ieee` **(REST 接口取正文/引用/补充材料/脚注，公式为 LaTeX 原文；PDF 走通用取数阶梯，`get_pdf_url()` 返回 `/stampPDF/getPDF.jsp`，直接回字节)**
- `10.1145`、`dl.acm.org` -> `acm` **(开放获取的文章抓全文：正文/公式/算法图/补充材料/参考文献；仍被登录墙挡住的只有摘要。必须有头访问)**
- `sciencedirect.com`、`10.1016` -> `nature` (Elsevier 回退)
- `epj-conferences.org`、`10.1051` -> `nature` (EDP Sciences 回退)
- `arxiv.org` -> `arxiv`
- 其他 -> `unknown`

handler 创建由 `get_publisher_handler()` 负责：

- `nature` -> `NatureHandler`
- `ieee` -> `IEEEHandler`
- `acs` -> `ACSHandler`
- `wiley` -> `WileyHandler`
- `spie` -> `SPIEHandler`
- `researching` -> `ResearchingHandler`
- `aps` -> `APSHandler`
- `aip` -> `AIPHandler`
- `iop` -> `IOPHandler`
- `cambridge` -> `CambridgeHandler`
- `oup` -> `OupHandler`
- `arxiv` -> 带 `journal_prefix="arxiv"` 的 `APSHandler`
- `unknown` -> 默认 `APSHandler`

## 浏览器路径

当前主流程有三条路径。

### 1. 无头直连路径

如果 `force_headed=False`，主流程会先启动无头 Chromium 访问 DOI，并保存：

```text
captured_data/{doi}/page_raw.html   原始 HTTP 响应（JS 运行前）
captured_data/{doi}/page.html       渲染后 DOM —— 只在与原始响应不同时才写
```

（`headless_initial.html` 已取消：它存的就是上面两个之中的一个，实测存档里
5/5 与 `page_raw.html` 逐字节相同。）

Phase 0 会先访问 DOI resolver URL。如果 DOI 可以直接识别为 Nature，且 DOI resolver 访问失败，会继续尝试 Nature 文章直连 URL：

```text
https://www.nature.com/articles/{doi_suffix}
```

如果最终 publisher 在：

```python
HEADLESS_ACCESSIBLE_PUBLISHERS = ["nature", "aip", "cambridge", "springer"]
```

中，主流程直接把这个无头 `page` 传给对应 handler，然后进入统一处理阶段。

无头预检的登录态来自**播种的 profile**：`prepare_profile_dir()` 在建这个无头
context 之前，已经把真实 Chrome profile 的整份 Cookies 库复制过来（并剔除反爬
条目），所以订阅态本来就在。

❌ `.auth/headless_storage_state.json` 和 `--refresh-headless-auth` **已删除**：
导出的 storage_state 没有比播种的 profile 多任何东西，却要为此连一次用户自己的
Chrome，而 `storage_state()` 的实现是把一个页面导航到该浏览器碰过的每一个
origin。

### 2. 无头 Handler 自主管理路径

如果无头预检没有完整跑完，但 DOI 或最终 URL 可以识别为无头可访问 publisher，主流程不会连接有头 Chrome，而是创建一个没有 `page` 的 handler：

```python
handler = get_publisher_handler(
    publisher,
    captured_data_dir=captured_data_dir,
    doi=doi,
)
```

这时 publisher 需要在自己的 `extract_all()` 里处理 `page is None` 的情况。Nature 当前支持这种模式：当没有收到 `page` 时，它会自己创建无头浏览器访问 DOI。

### 3. 标准有头路径

如果 publisher 不在 `HEADLESS_ACCESSIBLE_PUBLISHERS`，或者用户传入 `--force-headed`，主流程会使用有头 Chrome。

流程是：

1. 检查 `127.0.0.1:9222` 是否已有 Chrome。
2. 如果没有，通过 `chrome_session.launch_chrome()` 启动。
3. 使用 Playwright CDP 连接：

   ```text
   http://localhost:9222
   ```

4. 创建页面。
5. 根据 DOI 初步判断 publisher。
6. 创建 handler，并在 `page.goto()` 前启动网络监听。
7. 跳转 DOI。
8. 根据最终 URL 再判断一次 publisher，必要时重建 handler。
9. 进入统一处理阶段。

APS 当前只能通过这条有头路径访问；IOP 也通过此路径。

### 路径决策总结

| 条件 | 提取阶段 | 下载阶段 | 说明 |
|------|---------|---------|------|
| `force_headed=False`, publisher 在 `HEADLESS` 列表内 | headless（共用 precheck page） | headless（新建） | Nature、AIP、Cambridge、OUP |
| `force_headed=False`, publisher **不在** `HEADLESS` 列表内 | headed CDP | headed（复用 context） | APS、IOP |
| `force_headed=True`, publisher **不在** `HEADLESS` 列表内 | headed CDP | headed（复用 context） | 用户显式要求有头，与上一条行为一致 |
| `force_headed=True`, publisher 在 `HEADLESS` 列表内 | headless（Handler 自主管理） | headless（新建） | `force_headed` 被忽略 —— 无头可访问 publisher 仍走无头 |

核心原则：提取和下载阶段使用同一个浏览器模式，`force_headed` 标识贯穿全流程。

## PublisherHandler 接口

所有 publisher 都继承 `publisher/base.py` 中的 `PublisherHandler`。

初始化参数统一为：

```python
PublisherHandler(page=None, captured_data_dir=None, doi=None)
```

含义：

- `page`：可选 Playwright 页面。有头模式或无头直连模式会传入；无头自主管理模式可以是 `None`。
- `captured_data_dir`：当前 DOI 的响应缓存目录。
- `doi`：当前论文 DOI。

handler 可以通过 `configure()` 更新上下文：

```python
handler.configure(page=page, captured_data_dir=captured_data_dir, doi=doi)
```

主流程真正依赖的核心接口是：

```python
async def extract_all(self, page=None, doi=None, captured=None) -> dict

def convert_to_markdown(self, metadata, article_text, **kwargs) -> str
```

其他抽象方法用于 publisher 内部组织，例如：

- `extract_metadata()`
- `get_fulltext_url()`
- `get_pdf_url()`
- `get_supplemental_url()`
- `extract_references()`
- `get_figures()`

## extract_all 返回契约

所有 publisher 的 `extract_all()` 必须返回统一结构：

```python
{
    "metadata": {
        "title": str,
        "authors": [str],
        "author_with_affiliations": [
            {
                "author": str,
                "affiliations": [str],
            }
        ],
        "abstract": str,
        "journal": str,
        "year": str,
        "volume": str,
        "issue": str,
        "pages": str,
        "doi": str,
        "publication_date": str,
        "corresponding_author_emails": [str],
        "references": [str],
    },
    "links": {
        "pdf_url": str,
        "figure_urls": {
            "fig_1": {
                "url": str,
                "caption": str,
            }
        },
        "supplemental_urls": [str],
        "supplemental_descriptions": {
            "filename": "description",
        },
    },
    "fulltext_data": str | dict,
    "journal_prefix" or "journal_name": str,
    "access": bool,          # 可选，缺省 True
}
```

主流程不关心 `fulltext_data` 是 HTML 还是 JSON。APS 当前返回 JSON，Nature 当前返回 HTML。具体转换逻辑由各自的 `convert_to_markdown()` 实现。

### `access`（可选）

handler 若能从页面上**看出**出版商拒绝了这篇文章，就返回 `access: False`；主流程随即跳过
PDF、图片、补充材料和 Markdown，只保留 `html/` 与 `crossref.json`。这些资源受同一道
权限闸门管辖，继续下去只会把重试预算烧光，最后仍是 404 或被重定向回落地页。

⚠️ **看不出来就别返回这个键**。缺省是 True，代价是白下一次；而错误的 `False` 会**静默
跳过一篇本来能拿到的文章**，且日志里看不出异常。宁可多下一次，不可漏一篇。

当前实现：

| Publisher | 判定依据 | 无权限的取值 |
|---|---|---|
| Science | `data-article-access` 属性 | `"no"`（已见取值还有 `"free"`、`"full"`） |
| ScienceDirect | `div.content-meta-access-label` 的文本 | `Abstract only`、`No access`，**或该元素不存在** |

## 统一处理阶段

无论 publisher 是 APS 还是 Nature，只要进入 `process_with_handler()`，后续流程一致：

1. 调用 `handler.extract_all(captured=captured_data)`。
2. 取出 `metadata`、`links`、`fulltext_data`，以及可选的 `access`（缺省 True）。
3. 使用 Semantic Scholar 数据补全缺失的 `year/title`。
4. 创建最终论文目录：

   ```text
   {year}--{title}/
   ```

   并把抓取过程中的 HTML / API 响应搬进 `html/`。

   **若 `access` 为 False，到此为止**：保存 `crossref.json` 后直接返回，跳过下面的
   5–8 步。目录里只留 `html/` 和 `crossref.json`。

5. 调用 `_download_all_resources()` 下载资源。
6. 调用 `handler.convert_to_markdown()` 生成 Markdown。
7. 保存 `.md`。
8. 调用 `save_metadata_json()` 保存元数据 JSON。
9. 调用 `save_crossref_json()` 保存 Crossref 原始响应为 `crossref.json`。
10. 打印统计信息。

## 下载策略

PDF、图片和补充材料由主流程统一下载。publisher 只负责在 `links` 中提供 URL。

`_download_all_resources()` 会根据 `force_headed` 决定下载方式：

- `force_headed=False`：新建一个无头 Chromium 专门下载资源。
- `force_headed=True`：复用有头 Chrome context，并在需要时新建页面下载，避免破坏当前文章页面。

因此，publisher handler 不应该自己下载 PDF、图片或补充材料。它只负责发现链接和描述。

### 文件类型检测

部分下载链接（如 figshare 的 `ndownloader.figstatic.com/files/{id}`）不含文件扩展名。`download_supplemental_materials()` 在保存文件后，会调用 `_detect_and_rename()` 通过 `python-magic` 读取文件头字节检测 MIME 类型，自动补齐正确的扩展名（`.pdf`、`.zip`、`.docx` 等）。MIME 到扩展名的映射定义在 `MIME_TO_EXT` 字典中。

## APS 当前实现

APS 由 `publisher/aps.py` 的 `APSHandler` 处理。

关键点：

- APS 不在 `HEADLESS_ACCESSIBLE_PUBLISHERS` 中，默认需要有头 Chrome。
- `setup_network_capture()` 会监听 APS 的 abstract、fulltext、supplemental 响应。
- `extract_all()` 依赖有头页面和捕获到的 JSON/HTML。
- 正文来自 APS fulltext JSON。
- references 从 abstract HTML 的 `ol.references` 提取。
- APS JSON → Markdown 正文转换由 `publisher/aps.py` 中的 `convert_json_data_to_markdown()` 完成。

## Nature 当前实现

Nature 由 `publisher/nature.py` 的 `NatureHandler` 处理。

关键点：

- Nature 在 `HEADLESS_ACCESSIBLE_PUBLISHERS` 中，可以无头访问。
- 如果主流程没有传入 `page`，`NatureHandler.extract_all()` 会自己创建无头浏览器访问 DOI。
- 正文来自页面 HTML。
- metadata、authors、images、references、supplementary 等由 Nature handler 从 HTML、JSON-LD、meta 标签中提取。
- Markdown 转换由 Nature handler 按页面 HTML 结构处理。

## AIP 当前实现

AIP 由 `publisher/aip.py` 的 `AIPHandler` 处理。

- `10.1063`、`pubs.aip.org`、`aip.scitation.org` 会被识别为 `aip`。
- AIP 在 `HEADLESS_ACCESSIBLE_PUBLISHERS` 中，可以直接使用 Phase 0 的无头页面。

### metadata 提取

从 HTML `<head>` 中的 `citation_*` meta 标签提取：

- `citation_author` + `citation_author_institution` → 作者及机构
- `citation_title`、`citation_doi`、`citation_journal_title`
- `citation_volume`、`citation_issue`、`citation_publication_date`
- `citation_pdf_url` → PDF 下载链接，传给主流程下载

### 正文提取

`extract_article_text_from_html()` 一次遍历完成摘要和正文提取，摘要与正文走同一套公式转换管道（MathML → LaTeX）。方法返回 `(abstract_md, body_md)` 元组，不再用独立的 `extract_main_abstract_from_html()` 产生重复解析。

注意：部分新版 AIP 文章所有 section 的 `data-section-parent-id` 都是 `"0"`（旧文章只有摘要 section 是 0）。当前代码通过检测 wrapper 内是否包含 `<section class="abstract" aria-label="Main abstract">` 来识别摘要，不依赖 `parent-id`。

⚠️ 正文遍历会跳过参考文献那一节，否则它会被当成普通章节再渲染一遍，在
`## Article Text` 里留下第二个（且残缺的）REFERENCES 块。标题按名字跳过；对应的
`article-section-wrapper` 按内容跳过 —— 判据是其中的 `div.mixed-citation`，也就是
`extract_references_from_html()` 依赖的同一个标志，因此不受标题措辞与大小写影响。

### 图片提取

`extract_figures_from_html()` 从 `.fig-section` 容器中提取图片 URL 和标题，通过主流程统一下载并插入 Markdown。

### 参考文献提取

`extract_references_from_html()` 从 `.mixed-citation` 容器提取参考文献，移除 Google Scholar/Crossref/ADS 等外部链接，保留 DOI 链接，转为 Markdown 格式。

### 补充材料提取

`_extract_supplemental_links_from_html()` 从内联渲染的 figshare widget（`#articlefulltext_figshare`）中提取 `ndownloader.figstatic.com` 下载链接，传回主流程下载。

### Markdown 生成

`convert_to_markdown()` 生成完整 Markdown，结构为：

```markdown
# 标题
**Authors:**
作者名
机构

**DOI:** 10.1063/...

## Publication
## Abstract
## Article Text
## References
```

## IOP 当前实现

IOP 由 `publisher/iop.py` 的 `IOPHandler` 处理。

- `10.1088`、`iopscience.iop.org` 会被识别为 `iop`。
- IOP **不在** `HEADLESS_ACCESSIBLE_PUBLISHERS` 中，默认需要有头 Chrome，经过标准有头路径访问。

### metadata 提取

从 HTML `<head>` 中的 `citation_*` meta 标签提取：

- `citation_author` → 作者姓名，机构从 DOM 元素提取
- `citation_title`、`citation_doi`、`citation_journal_title`
- `citation_volume`、`citation_issue`
- `citation_publication_date`、`citation_online_date` → 年份（优先 publication_date）
- `citation_pdf_url` → PDF 下载链接，传给主流程下载
- `citation_abstract` → 摘要文本
- `citation_keywords` → 关键词列表

### 正文提取

`extract_article_text_from_html()` 复用 `wildcard.find_generic_article_body()` 查找 `div.wd-jnl-art-full-text` 主内容容器，遍历其中的 heading/paragraph section，通过统一的公式转换管道（MathML → LaTeX）处理数学公式。摘要通过 `wildcard.extract_abstract_with_fallbacks()` 从 `<section data-title="Abstract">` 提取。

### 图片提取

`extract_figures_from_html()` 从 `div.wd-jnl-fig` 容器提取图片 URL 和标题，fallback 到 body 内的 `<img>` 标签。

### 参考文献提取

`extract_references_from_html()` 从 `meta[name="citation_reference"]` 提取参考文献，通过 `wildcard.parse_citation_reference_string()` + `wildcard.format_as_bibtex()` 转为 BibTeX 格式。

### Markdown 生成

`convert_to_markdown()` 生成完整 Markdown，结构为：

```markdown
# 标题
**Authors:**
作者列表

**DOI:** 10.1088/...

## Publication
## Abstract
## Article Text
## Figures
## References
```

参考文献以 ` ```bibtex ` 代码块输出。

## Cambridge 当前实现

Cambridge 由 `publisher/cambridge.py` 的 `CambridgeHandler` 处理。

- `10.1017`、`cambridge.org` 会被识别为 `cambridge`。
- Cambridge 在 `HEADLESS_ACCESSIBLE_PUBLISHERS` 中，可以直接使用 Phase 0 的无头页面。

### metadata 提取

从 HTML `<head>` 中的 `citation_*` meta 标签提取：

- `citation_author` → 作者姓名（无作者机构 meta 标签，机构从 DOM 提取）
- `citation_title`、`citation_doi`、`citation_journal_title`
- `citation_volume`、`citation_firstpage`、`citation_publication_date`
- `citation_pdf_url` → PDF 下载链接，传给主流程下载
- `citation_abstract` → 摘要文本
- `citation_keywords` → 关键词列表
- `citation_author_orcid` → 作者 ORCID

作者机构通过 `<div data-test-author="Name" class="row author">` DOM 元素提取，其中 `<dt class="title">` 为作者名（带 `*` 表示通讯作者），`<dd>` 内包含机构名称。通讯作者邮箱从 `<div class="corresp">` 和 `mailto:` 链接提取。

### 正文提取

`extract_article_text_from_html()` 一次遍历 `<div class="body">` 内的所有 section（`<div class="sec intro">`, `<div class="sec methods">` 等），返回 `(abstract_md, body_md)` 元组。正文通过统一的公式转换管道（MathML → LaTeX）处理 `<mjx-container>` 数学公式。摘要单独从 `<div class="article-abstract">` 提取。

### 图片提取

`extract_figures_from_html()` 从 `<section>` 内的 `<div class="fig-ada">` + `<div class="figure-thumb">` 组合中提取图片 URL 和标题。标题来自 `<div class="caption">` 内的 `<span class="label">` 和 `<p class="p">`，图片 URL 从 `<img data-src="...">` 获取。通过主流程统一下载并插入 Markdown。

### 参考文献提取

`extract_references_from_html()` 从 `<div id="references-list">` 容器中提取参考文献，保留 DOI 链接，通过统一公式管道转为 Markdown 格式。

### 补充材料提取

`_extract_supplemental_links_from_html()` 从 `<div class="notes supplementary-material">` 中提取补充材料链接。

### Markdown 生成

`convert_to_markdown()` 生成完整 Markdown，结构为：

```markdown
# 标题
**Authors:**
作者名
机构

**Email:** xxx@xxx
**DOI:** 10.1017/...

## Publication
## Abstract
## Article Text
## Supplemental Material
## References
```

## OUP 当前实现

OUP (Oxford University Press) 由 `publisher/oup.py` 的 `OupHandler` 处理。

- `10.1093`、`academic.oup.com` 会被识别为 `oup`。
- OUP 在 `HEADLESS_ACCESSIBLE_PUBLISHERS` 中，可以直接使用 Phase 0 的无头页面。

### metadata 提取

从 HTML `<head>` 中的 `citation_*` meta 标签提取，包括 `citation_author` / `citation_author_institution` 配对（按出现顺序匹配作者与机构）、`citation_title`、`citation_doi`、`citation_journal_title`、`citation_volume`/`citation_issue`、`citation_firstpage`/`citation_lastpage`、`citation_publication_date`、`citation_pdf_url`。

### 正文提取

`extract_article_text_from_html()` 遍历 `<div data-widgetname="ArticleFulltext">` 容器的直接子节点：

- `<h2 class="abstract-title">` + `<section class="abstract">` → 摘要
- `<h2 class="section-title">` / `<h3>` / `<h4>` → 各级标题
- `<p class="chapter-para">` → 段落（含内联公式、xref-bibr/xref-fig 链接）
- `<ul class="roman-lower">` / `<ol>` → 列表，递归处理嵌套 `<p>`
- `<div class="formula-wrap">` → 显示公式，`<span class="label title-label">(A1)</span>` 转为 `\tag{A1}`
- `<div class="block-child-p">` → 内联文字与多个 formula-wrap 混排块，通过占位符再注入
- `<div class="table-full-width-wrap">` → 表格（含标题 + caption + 渲染 `<table>` 的 `.table-overflow` 版本）
- `<div data-content-id="figN">` → 图注（图片由 `extract_figures_from_html()` 处理）

公式通过 `<mjx-assistive-mml>` 内的 MathML 走 `mathml_to_latex_pandoc` 转 LaTeX。

### 图片提取

`extract_figures_from_html()` 从 `<div data-content-id="figN">` 内的 `<a class="download-slide">` 提取高清 URL。OUP 这个 `href` 是个 `/DownloadFile/DownloadImage.aspx?image=...` 重定向器，里面嵌入了 Silverchair CDN 的真实地址。`_clean_download_slide_url()` 剥掉重定向器前缀，并删除会话相关的 `sec`/`ar`/`xsltPath`/`imagename`/`siteId` 查询参数（保留 `Expires`/`Signature`/`Key-Pair-Id`，CDN 验签需要），得到可以直接下载的链接。

### 参考文献提取

`extract_references_from_html()` 遍历 `<div id="ref-auto-bib{N}" class="ref-content">`：

1. 把 citation 内容转成网页显示的样子（`<div class="surname">`/`<div class="given-names">`/`<div class="year">`/`<div class="source">`/... 用空格连起来），剥掉 `Crossref`/`Search ADS` 等 citation-links 装饰。
2. 从 `<div class="pub-id">` 抓 DOI。
3. 与 Crossref `references` 数据按 DOI 匹配，给每条引用追加一个只含 `year` + `doi` 的 `@misc{bibN, ...}` BibTeX 块。

### 补充材料提取

`extract_supplemental_from_html()` 找两个地方：

- `<div class="dataSuppLink">` 里的 `<a href="...stz656_supplemental_file.zip">` —— 真实下载链接。
- `<h2>SUPPORTING INFORMATION</h2>` / `<h2>Supplementary data</h2>` 后面的 `<p>` —— 拿来当文件描述（通常是 `<strong>filename.ext</strong>` 形式）。

### Markdown 生成

`convert_to_markdown()` 输出结构：

```markdown
# 标题
## Authors          # 含机构
## Publication      # 期刊 / 卷期 / 页 / DOI
## Abstract
## Article Text     # 标题 + 段落 + 公式 + 表格 + 嵌入图
## Acknowledgements # 单独抽出
## Supporting Information   # SUPPORTING INFO 文字 + 下载链接
## References       # 每条 + 对应 BibTeX 块
```

## wildcard.py — 共享提取模块

`publisher/wildcard.py` 提供跨 publisher 共享的提取函数，被 NatureHandler、IOPHandler、AIPHandler、CambridgeHandler、OupHandler 共同引用：

| 函数 | 用途 |
|------|------|
| `find_generic_article_body(soup)` | CSS 选择器级联查找文章正文容器 |
| `extract_abstract_with_fallbacks(soup)` | 多策略摘要提取 |
| `format_citation_as_text(parts)` | 解析后的引用 dict → 可读引文文本 |
| `generate_reference_text_from_crossref(ref)` | Crossref reference 对象 → 可读引文文本 |
| `prepare_mathjax_html_fragment(html)` | MathJax CHTML → placeholder 折叠 |
| `convert_html_fragment_to_markdown(html)` | HTML → Markdown + 公式还原 |
| `convert_mathml(mathml_str)` | MathML → LaTeX（通过 pandoc） |

新增 publisher 时应优先复用 wildcard 中的函数，减少重复代码。

## 顶层 API 文件

### complete_paper_extraction.py

主入口，可通过 `asyncio.run()` 作为 Python API 调用：

```python
import asyncio
from complete_paper_extraction import complete_extraction_workflow
md_path = asyncio.run(complete_extraction_workflow("10.1103/PhysRevLett.125.015001"))
```

也支持 CLI：`python complete_paper_extraction.py --doi "10.1103/PhysRevLett.125.015001"`
（⚠️ DOI 必须走 `--doi`，位置参数不被接受 —— `--doi/--file/--json` 是 `required=True`
的互斥组，漏掉就是 argparse 直接 exit 2）

### 批量处理

❌ `batch_process.py` 已删除 —— 主程序自己就是批处理器：

```bash
python complete_paper_extraction.py --file dois.txt
```

`--file` 走的是同一个循环，且**比那个脚本多做了两件要紧的事**：整批共享一个
`BrowserSession`，以及每篇之间 `retire_headed_browser()` 重建 profile。
那个脚本是逐篇调 `complete_extraction_workflow()` 而不传 `browser_session`，
等于把这两条都绕过去了。随机睡眠防拉黑（`config.py` 的 `BATCH_SLEEP_*`）也在主程序里。

## 反爬虫检测

`is_bot_challenge_page(url, html)` 在 Phase 0 无头预检阶段检测页面是否为反爬虫拦截页面（Radware Bot Manager、Cloudflare Turnstile、Distil Networks 等）。检测到拦截后自动回退到有头 Chrome 路径，并设置 `headless_blocked` 标志防止无头 handler 路径被错误触发。

## 参考文献格式化

**每篇文章只访问一次 Crossref**（Step 0），响应原样存为 `crossref.json`。不再逐条
参考文献去查 Crossref / Semantic Scholar —— 那样一篇综述就是几百次请求，且网络抖动
会决定某条有没有 BibTeX。

**不再输出 BibTeX 代码块**。这些 Markdown 是给 AI agent 读的，尊重原文的引文写法、
保留其中的链接即可；重排成 `@article{...}` 既丢信息又增噪声。

各 publisher 统一输出 `[n] 引文原文`。页面本身没有给出引文文本时（部分出版商只在
meta 里放裸 DOI），用 `wildcard.generate_reference_text_from_crossref()` 从 Step 0
那一次响应里**离线**生成可读文本，同样不产生网络请求。

`SAVE_WITHOUT_REFERENCES` 配置项（`config.py`）控制参考文献为空时是否仍然保存 Markdown。

## 新增 Publisher 的接入方式

新增 publisher 时按这个顺序做：

1. 新建 `publisher/{name}.py`，继承 `PublisherHandler`。
2. 实现 `extract_all()` 和 `convert_to_markdown()`。
3. 复用 `publisher/wildcard.py` 中的共享函数（正文查找、公式转换、BibTeX 格式化等）。
4. 如果需要缓存网页或 API 响应，实现 `setup_network_capture()`。
5. 在 `publisher/orchestrator.py` 的 `detect_publisher_from_url()` 中增加 DOI/URL 识别规则。
6. 在 `get_publisher_handler()` 中返回新的 handler。
7. 在 `publisher/__init__.py` 中导出新的 handler。
8. 如果该 publisher 可以无头完整访问，把名字加入 `HEADLESS_ACCESSIBLE_PUBLISHERS`。
9. 保持 `extract_all()` 返回统一结构，避免修改主流程。

核心原则：`complete_paper_extraction.py` 保持 publisher 不敏感；具体网页结构、API 响应、HTML 转 Markdown 的逻辑都放在各自的 `PublisherHandler` 中。

# Download_paper 项目指南

本文档自动加载。详细版本见 `docs/CLAUDE.md`。

## 核心原则

**始终通过浏览器访问论文页面**，使用 `complete_paper_extraction.py`，不使用 `curl`/`wget`/`requests` 直接 HTTP 请求期刊网站。

例外：**图片和补充材料**先用 `requests` 直接下（带浏览器 UA + 文章页作 Referer），
拿不到才回退浏览器。这类文件几乎都放在不设防的 CDN 上（连 ScienceDirect 的
`ars.els-cdn.com` 都直接给），逐个开标签页纯属浪费。PDF 和论文页面**不在**例外之列。
见 `_http_download_to()`；`DP_HTTP_FIRST=0` 可整体关闭，`DP_HTTP_USER_AGENT` 覆盖 UA。
⚠️ 判定成功看**字节**不看状态码：200 但内容是 HTML（Cloudflare 挑战页 / 登录页）是最常见
的失败形态，照存就会得到一个其实是网页的 `.jpg`

**所有元素使用同一套公式转换管道** — 正文段落、图注、表格单元格等所有包含潜在 LaTeX 公式的元素，都必须通过 `_convert_iop_paragraph_to_md()` (IOP) 或对应的公式转换函数处理，不能直接使用 `get_text()` 提取纯文本。

## 快速命令

```bash
source /home/zhiping/research-env/bin/activate
cd /home/zhiping/Projects/Download_paper
# ⚠️ DOI 必须用 --doi 传，不是位置参数：--doi/--file/--json 是一个
#    required=True 的互斥组，少了就直接 argparse 报错退出（exit 2）
python complete_paper_extraction.py --doi "<DOI>"
python complete_paper_extraction.py --doi "<DOI>" --force-headed  # 有头模式
python complete_paper_extraction.py --doi "<DOI>" --pdf-only      # 只下 PDF，不生成 md
python complete_paper_extraction.py --file dois.txt         # 批量（主程序自己就会批处理）
```

## 跳过补充材料（`--supplemental=False` / `DP_SUPPLEMENTAL=0`）

默认 `True`，照常下。`False` 时**只跳过下载**，链接仍写进 Markdown ——
产出目录里「本来就没有补充材料」和「叫我们别下」长得一模一样，所以跳过时
明确打印跳过了几个。

- 📌 **书籍是这个开关的由来**：OUP 的书把**每一章**都列成补充材料 PDF。实测
  `10.1093/acprof:oso/9780198562641.001.0001` 有 **19 个** `.ag.pdf`、约 60 MB，
  占掉整次运行的大部分墙钟时间 —— 而要的只是书本身
- ⚠️ 命令行优先于环境变量；两个都不给才用默认。`--supplemental` 不带值等于 `True`

## pdf-only 模式

很多老文章和会议短文的网页**根本没有正文**，生成 md 是白费功夫。两层，越往下访问越少：

| | 怎么触发 | 还访问论文页面吗 | 产出 |
|---|---|---|---|
| 第一层 | `--pdf-only` | 是，照常走全流程拿 PDF 链接 | `paper.pdf` + `metadata.json` + `crossref.json` |
| 第二层 | `--json` 里给 `pdf_link` | **否** | 同上 |

- 第一层只是在 `_download_all_resources` 拿到 PDF 后直接 return（图片、key image、
  补充材料都是给 md 引用的，不生成 md 就没有下载的理由），并跳过 `convert_to_markdown`
- 第二层走 `_pdf_link_direct_download()`：跳过 Phase 0 预检和 `doi.org/{doi}`，
  元数据**全部取自 Step 0 那一次 Crossref 响应**（目录名要的 title/year 就在里面），
  只为取文件起**一次**浏览器。有头时就是那个一次性 Chrome（profile 照常播种），
  无头时是 `_download_all_resources` 自建的那个
- 有头/无头**两层都按同一个 Crossref 出版商闸门**判：`_crossref_headless_publisher()`
  把 `crossref_data['publisher']` 按词边界匹配 `HEADLESS_ACCESSIBLE_PUBLISHERS` ——
  主流程用它决定要不要 Phase 0，第二层用它决定直接上有头还是无头。不在表里的
  （ScienceDirect、SPIE、IOP、APS…）PDF 域名多半要点验证框，直接给有头；
  `--force-headed` 无条件优先。判定只读 Step 0 已拿到的响应，不额外发请求
- ⚠️ **词边界不能省**：`oup` 会在 "Optica Publishing **Group**" 里误命中，
  把 Optica 的文章错判成可无头直连
- ⚠️ 两层都**以 PDF 为准**：PDF 没下来就返回 None（批次记为失败）。但 `metadata.json`
  照常落盘且记着 `pdf_link`，可以据此重试
- `--force-headed`、`FRESH_PROFILE`、`BATCH_SLEEP`、各种 `DP_*` 超时全部照旧 ——
  两层都走同一个批次循环，没有另一套旋钮

## 常见提取陷阱

编写或修改 publisher handler 时注意：

- **图注必须有公式** — 图注 `<p>` 必须走公式转换管道（`_convert_iop_paragraph_to_md`），不能 `get_text()`
- **表格单元格必须有公式** — `<td>`/`<th>` 需走 `_process_table_cell`（IOP）或对应转换，不能 `get_text()`
- **去重** — IOP 图注的 `<p>` 内已包含 `**Fig. N:**`，需用 `re.sub(r'^\*\*Fig\.?\s*\d+[.:]\*\*\s*', '', caption)` 去重
- **不要手动拼接 HTML** — 始终用 BeautifulSoup 解析后操作

## 可复用函数

`html_to_md_converter.py` 中的函数可独立调用：
- `convert_html_to_markdown(html)` — HTML→MD（pandoc，含 MathJax 公式）
- `cleanup_markdown(md)` — 清理 LaTeX 不兼容命令、HTML 实体
- `mathml_to_latex_pandoc(mathml)` — MathML→LaTeX

`publisher/wildcard.py` 中的共享工具：
- `find_generic_article_body(soup)` — 正文容器查找
- `extract_abstract_with_fallbacks(soup)` — 摘要提取
- `format_as_bibtex(parts)` — BibTeX 格式化
- `convert_html_fragment_to_markdown(html)` — HTML→MD+公式还原

## 支持的 Publisher

| DOI 前缀 | Handler | 浏览器 | 覆盖范围 |
|---|---|---|---|
| `10.1038` | NatureHandler | 无头 | 完整 |
| `10.1103` | APSHandler | 有头 | 完整 |
| `10.1063` | AIPHandler | 无头 | 完整 |
| `10.1088` | IOPHandler | 有头 | 完整 |
| `10.1017` | CambridgeHandler | 无头 | 完整 |
| `10.1093` | OupHandler | 无头 | 完整 |
| `10.1145` | ACMHandler | **有头** | **abstract-only** — 见下 |
| `10.1109` | IEEEHandler | 有头 | 完整（REST 接口） |
| `10.1021` | ACSHandler | 有头 | 完整 |
| `10.1002` | WileyHandler | 有头 | 完整 |
| `10.1117` / spiedigitallibrary.org | SPIEHandler | 有头 | 完整 |
| `10.3788` / researching.cn | ResearchingHandler | 有头 | 完整 |
| opticsjournal.net | OpticsJournalHandler | 有头 | 完整（无补充材料） |
| jstage.jst.go.jp | JStageHandler | 无头 | **abstract + PDF** — 见下 |

### APS 的四个教训（2026-09-20，另一台机器的实跑日志）

- ⚠️ **PDF 链接必须用页面 URL 里的 DOI 拼**：APS 路径大小写敏感。DOI 列表给
  `10.1103/physreva.79.020103`，拼出的 `/pra/pdf/10.1103/physreva.79.020103` 不是文章；
  而页面停在 `/pra/abstract/10.1103/PhysRevA.79.020103`。`_url_doi()` 就是干这个的
  （`_fetch_fulltext_json` 一直在用），`extract_all` 的兜底漏了它 —— 同一份日志里
  补充材料链接是对的，因为那条从 URL 生成
- ❌ **APS 自带的那套旧监听器已删**（`core/network_capture`，Nature 仍在用）。它每条响应
  打一行 `[200] …`、**把看到的每个 HTML 文档写进论文目录**（一次被挑战的运行留下
  4 个 0.25 MB 的 Cloudflare 挑战页），拿不到数据时还会自己再导航一次。现在统一用
  主流程那份捕获
- 📌 **补充材料只看捕获**：有就用、没有就是没有，不再访问 `/supplemental/{doi}`。
  捕获为空时明确打印——那种情况整个提取本来就降级了，从同一个被拦的会话去访问
  只会再拿回一张挑战页
- ⚠️ **预载不要在 widget 还没渲染出来时就判失败**。实测那台机器：预载 60 秒里状态行
  只有一条 `title='Just a moment...' iframes=0`，**Turnstile 根本还没出现**，三次兜底
  点击点在 `.main-content` 的空白处；随后 Playwright 那轮找到了真的 300×65 iframe，
  点一次就过。现在「检测到挑战且到点时仍停在挑战页」会再等一轮
  （`DP_CHALLENGE_EXTRA_WAIT`，默认与超时等长，**只延长一次**）

### access 判定（handler 可选返回，缺省 True）

handler 若能从页面上看出出版商拒绝了这篇文章，就在 `extract_all()` 的返回里带上
`access: False`。主流程随即跳过 **PDF、图片、补充材料、Markdown**，只保留 `html/`
和 `crossref.json` —— 这些资源受同一道权限闸门管辖，继续下去只会烧光重试预算，
最后仍是 404 或被重定向回落地页。

- ⚠️ **看不出来就别返回这个键**。缺省 True 的代价是白下一次；错误的 `False` 会
  **静默跳过一篇本来能拿到的文章**，日志里还看不出异常
- **Science**：读 `data-article-access`。**只在等于 `"no"` 时判无权限**（否定式，
  不是白名单）—— 实测取值还有 `"free"`、`"full"`，而 2017 年那篇存档**根本没有这个
  属性**。写成白名单的话，Science 日后新增一个取值就会让整批文章被静默跳过
- ❌ **ScienceDirect 的判定已默认停用**（`DP_SD_ACCESS_CHECK=1` 才开）。原判据是读
  `div.content-meta-access-label`，`Abstract only`、`No access`、**或该元素不存在**
  都判无权限 —— 坏就坏在最后那一条：实测 `10.1016/j.rinp.2021.104097`（用户确有
  访问权限）被判成无权限。⚠️ **成因不是"标签藏在选择器够不到的地方"**（我最初这么
  写过，是猜的）：抓下来的两份 HTML 里，`content-meta-access-label` 与
  `content-meta-labels` **出现次数都是 0** —— 原始响应里没有，渲染后 DOM 里也没有，
  连侧栏推荐那些同类名的 `<span>` 都没有。这一篇根本就没有 access 标签元素，
  而检测器把"没有标签"直接当成了"没有权限"。（存档是某一时刻的快照，不能排除标签
  在更晚阶段才注入，但至少在 handler 读取的那一刻它不存在。）
  而 `access: False` 会连 PDF、图片、补充材料、Markdown 一起跳过，**日志里看不出
  异常**，等于静默丢掉一篇本来拿得到的文章
- 📌 处理方式是**不返回这个键**，而不是返回 True：契约里「看不出来就别返回」正是
  为此而设，主流程 `extraction_result.get('access', True)` 读到缺键即当作有权限。
  检测代码保留在 `_access_opinion` / `detect_access_from_html` 里，将来修好再开
- ⚠️ **ScienceDirect 必须限定标签名和容器**，不能用裸的 `.content-meta-access-label`：
  「推荐文章」侧栏里每条推荐都带一个**同类名的 `<span>`**，显示的是**别人文章**的权限。
  实测一份存档页有 5 处命中 —— 本文 1 个 `<div>`、邻居 4 个 `<span>`，其中两个写着
  "Open access"。用裸类名去取，一篇无权限的文章只要侧栏推荐了开放获取论文就会被
  判成有权限

### 删掉的死配置：`USE_CHROME_MODE` / `HEADLESS`

两个都**没有任何代码读取** —— 只是被定义、被打印。而每次运行都会打出来的那行
`- Chrome模式: persistent` 还在说反话：它的说明是「persistent = 复用现有 profile」，
可抓取 profile 是**每篇论文先删再建**的（`prepare_profile_dir`），从不复用。
`HEADLESS` 同理：有头/无头由 Phase 0 按出版商和预检结果自己判
（`HEADLESS_ACCESSIBLE_PUBLISHERS` / `--force-headed`），环境变量插不上手。

📌 **一个打印出来、看着像在生效、实际谁都不读的开关，比没有更糟** —— 它会让人
据此调参、据此解释现象。

### SPIE 正文 API：族只问一次，`hasAccess` 就是结论

❌ **旧行为是三连撞**：判定出的族先试，失败就把另外两个族也 POST 一遍。理由曾是
「问错族不报错，只回 hasAccess=False 的空壳，看着像没权限」—— 但**页面已经说了是
哪个族**，第二三次只是在重问一个已有答案的问题，而对象是本仓最严的 Imperva。

实测 `10.1117/12.209459`（1994 年的会议论文，只有 PDF 版）：捕获里已经是
`hasAccess=True` 且**没有 `fullTextHtml`**，旧代码把它读成"族判错了吧"，又发了
两次 POST，最后三条日志都在说没有正文。

现在：

| 响应 | 处理 |
|---|---|
| `hasAccess=True` + 有 `fullTextHtml` | 用它 |
| `hasAccess=True` + **没有** `fullTextHtml` | **这篇没有 HTML 正文**，收工 |
| `hasAccess=False` | 没有正文权限，收工 |
| **没收到响应**（超时/网络波动/异常） | **只有这一种才重试**，`FULLTEXT_TRIES=3` |

- ⚠️ **关键在于把"服务器说没有"和"根本没连上"分开**。`_post_fulltext` 原来两种都
  返回 `''`，长得一模一样 —— 那正是旧循环敢继续撞下两个族的原因。现在没响应返回
  `None`，收到就返回 payload，重试条件才写得对
- 📌 **捕获里若已有结论就一次都不发**。实测该篇：`↪ 请求正文 API` **0 次**
  （旧代码 3 次），PDF 727,866 字节照常拿到
- 📌 「SPIE 说这里没有正文」也落盘成 `fulltexthtml.json`（125 字节）——
  产出目录应该能离线说明**为什么**没有正文，而不是留一片空白

### SPIE 的补充材料

- ❌ 旧注释写着「没见过带补充材料的 SPIE 文章」，**已被 `10.1117/1.APN.4.3.036004` 推翻**
- 📌 **补充材料有自己的 DOI：文章 DOI + `.sNN`**，正文里这样引用：
  `<a target="xrefwindow" href="https://doi.org/10.1117/1.APN.4.3.036004.s01">Supplementary Material</a>`
  实测这篇引用了 **3 次**（都指向同一个 `.s01`），而 **landing page 里 `supplemental`
  出现 0 次** —— 所以判据是 `.sNN` 后缀，不是链接文字（文字受语言和措辞影响）
- ⚠️ **必须限定是本文的**：`supp_doi.startswith(本文DOI + '.s')`。参考文献里若引用了别人
  论文的 `.sNN`，不加这条就会把别人的补充材料当成我们的下下来
- ⚠️ 同一份文件正文里引用多次，**要去重**
- 📌 另有 `/api/{family}/article/supplemental` 端点，**页面加载时自己会调**。实测
  `10.1117/12.3071462` 预载成功那次捕到了，`supplemental.json` 正常落盘：
  `{"hasAccess":true,"data":{"urlId":"…","supplementalFiles":[]}}`
- ❌ **我一度以为"从没捕到过"，那是错的** —— 那几次的预载都被 Imperva 拦下走了
  Fallback，而 **Fallback 的 `result["responses"]` 当时被直接丢弃**（`absorb_cdp`
  只在第一次预载那个分支里调用）。所以不是页面没发，是我们没收。已修
- ⚠️ 条目在 `data.supplementalFiles`（**不是** `data` 本身、也不是 `data.items`）。
  实测形状（`10.1117/1.APN.4.3.036004`）：

  ```json
  {"fileNameRemote": "APN_4_3_036004_ds001.pdf",
   "url": "/journals/supplementalcontent/10.1117//1.APN.4.3.036004/APN_4_3_036004_ds001.pdf",
   "sequence": 1, "title": null, "abstract": null,
   "doi": "10.1117/1.APN.4.3.036004.s01"}
  ```

  条目里的 `doi` **就是正文链接里的那个 `.s01`** —— 两条路找的是同一份文件
  （都是 453,957 字节、PDF 1.7、5 页），但 API 这条给的是**出版商自己的文件名**
  （`APN_4_3_036004_ds001.pdf`，而不是从 DOI 推的 `1.APN.4.3.036004.s01.pdf`），
  所以优先用它。没有文件的文章回一个**空列表**，那是答案不是落空
- 📌 **SPIE 有两种补充材料，可以同时存在**：正文里的 `.sNN` DOI，以及**会议海报**
- **海报的判据在 landing page 的内嵌 JSON：`"hasPoster":true`**。36 篇存档零例外 ——
  26 篇会议论文是 `false`、2 篇是 `true`（`12.3071462`、`12.2668643`）、4 篇期刊论文
  **根本没有这个字段**（期刊没有海报）。**绝不能靠"撞一下看有没有"**来判断
- ⚠️ **这个标记只在原始响应里**：`page_raw.html` 有 `hasPoster":true`×1、
  `ViewPoster?urlId=`×2，而 SPIE 的 `landing` 一直读 `page.content()` ——
  这个功能本会**静默失效**。已把 `landing`、`extract_metadata`、`get_pdf_url`
  全改成 `get_page_html()`，SPIE 现在零 `page.content()`
- 下载链接优先取页面自己给的 `/proceedings/ViewPoster?urlId=<doi>&download=true`
  （`DownloadPoster` 在所有存档里出现 **0 次**，虽然它也能下），取不到才按 DOI 构造
- ⚠️ **SPIE 的 `convert_to_markdown` 原本没有补充材料段**（它以前没有补充材料）。
  文件下到磁盘、md 里却不提，等于"下了没人知道"。补上了，且**只在真有内容时才输出标题**
- ✅ 端到端实测：`✓ 补充材料（正文中的 .sNN DOI）: 1 个` →
  `supplemental--1.APN.4.3.036004.s01.pdf`，453,957 字节、PDF 1.7、5 页；
  md 里三处引用都渲染成了指向该 DOI 的链接。海报：`10.1117/12.3071462` →
  `supplemental--10.1117_12.3071462_poster.pdf`，750,717 字节、PDF 1.5、1 页，
  md 里有 `## Supplemental Material` 段

### J-STAGE（`jstage.jst.go.jp`，日本各学会期刊）

**abstract + PDF only.** 目前见到的每篇都只有抄録和 PDF，出版商不提供正文，
所以没有正文/图片/补充材料提取，接口留空而不是猜。

- ⚠️ **只按域名路由，绝不按 DOI 前缀**：J-STAGE 托管几百个学会，各自前缀不同
  （レーザー研究是 `10.2184`，别家另有），列前缀迟早漏且会错
- 📌 **同一篇有日/英两个页面，而且不是同一条记录的翻译**：
  `doi.org/{doi}` 落到 `…/_article/-char/ja` 还是 `/-char/en` 取决于浏览器，
  两页各有自己的标题和作者写法：

  | | `meta[title]` | `meta[authors]` |
  |---|---|---|
  | `-char/ja` | 高強度レーザーパルスによる非線形Compton 散乱の モンテカルロ法 | 瀬戸 慧大 |
  | `-char/en` | Monte Carlo Method for Nonlinear Compton Scattering … | Keita SETO |

- ⚠️ **`citation_*` 两页完全相同、而且永远是日语** —— 所以英文标题**不可能**从
  `citation_title` 拿到，必须真的去访问另一个语言的页面。语言相关的值一律取
  `meta[name=title]` / `meta[name=authors]`，不取 `citation_*`
- ⚠️ **作者列表优先取 DOM**（`div.global-authors-name-tags a.customTooltip`，
  一个作者一个锚点），`meta[authors]` 只作兜底：它把所有作者塞在一个字符串里，
  而多作者时的分隔符尚无样本，拆分就是在猜
- 两份 HTML 都落盘（`page_ja.html` / `page_en.html`）—— 每份都有对方没有的东西
- **PDF 从落地 URL 构造**：`_article` → `_pdf`，`/-char/...` 全部丢掉。⚠️ 不能拿 DOI 拼
  —— 路径里是期刊自己的卷/页标识（`/article/lsj/51/5/51_337/`），`10.2184/lsj.51.5_337`
  推不出来
- 产出：`metadata['title']` 是**「日语 English」合并串**（metadata.json 检索两种语言都
  命中）、`authors` 是 `["瀬戸 慧大", "Keita SETO"]`；而**目录名只用日语标题** ——
  靠新增的 `metadata['_dir_title']`，`organize_paper_output` 优先读它
- md 里**不输出 `## Article Text` 空段**：空标题看着像提取失败，而这里是出版商本来
  就没有正文。`fulltext_data` 返回 `''`，所以也不会生成 `page.html`
- 引用文献**原样照抄** `citation_reference`（「1）」编号、中日英混排都保留），只进
  `paper.md`；`metadata.json` 不写 references（`crossref.json` 里有 Crossref 自己那份）
- ✅ 实测 `10.2184/lsj.51.5_337`：目录 `2023--高強度レーザー…`、PDF 0.88 MB、
  md 114 行、40 条引用文献、抄録 450 字符、无头直连零挑战

### 找回预载页面：`targetId` 匹配是假的，只能按 URL

预载在 Playwright 连接**之前**打开文章页，之后要在 Playwright 的 pages 里把那个
tab 找回来。原先有三条路，**只有第三条真能用**：

- ❌ **按 CDP `targetId`**（曾被注释称为"精确、与 URL 无关、适用于任何 publisher"）
  —— **实测两组 id 毫不相交**。同一浏览器、同一时刻、同一个 J-STAGE 页面：

  ```
  Chrome /json（= 预载的 ws_url 末段）  D77BDB56D0CEDB5ABC2E3EF2438BE303
  Playwright 会话里 Target.getTargetInfo  2CA3FA09A46E6AAD4567ACD33E2260E6
  ```

  三次枚举全部不命中。函数已删除。⚠️ 我曾以"Playwright 还没枚举到 tab"解释它偶发
  落空并加了 3 次重试 —— **那是猜的，且重试在重复一个不可能成功的比较**。
  浏览器级会话（`new_browser_cdp_session` + `Target.getTargets`）确实能拿到 /json
  那套 id，但要映射回 Playwright 的 Page 仍然只能靠 URL
- ❌ **`url in pg.url`** —— `url` 是 `https://doi.org/{doi}`，而页面早已重定向。只有
  最终 URL 仍含 `doi.org/<doi>` 才可能命中，**没有出版商是这样**
- ✅ **按 URL**（`_pick_page_by_url`）：**精确 URL 优先**（容忍尾斜杠），其次
  **URL 里含 DOI**（IOP/Optica 这类有效；J-STAGE 用 `lsj/49/6/49_349`、
  ScienceDirect 用 PII，都不含 DOI）。精确 URL 来自
  `PageCapture.article_url()` —— 捕获里那份文章响应自己的 URL
- ❌ **不做 host 兜底**：`want_host` 取自 `landed_url or url`，唯一能走到它的情形是
  `landed_url` 为空 —— 那时 host 是 `doi.org`，不可达。⚠️ 我一度用"它会选中上一篇
  的残留 tab"为它的删除辩护，**那个实测来自被探针污染的浏览器**；生产是一篇一个
  Chrome、跑完即关，不会有别篇的 tab
- 📌 找不到页面**是安全答案**：捕获里有文章就离线继续；捕获里没有，才由
  `_pick_article_page`（逐个读页面 HTML）去认
- ✅ 实测 Optica：`✓ 按URL 完全一致选定页面: …/fulltext.cfm?uri=oe-30-1-389` →
  `✓ 找到预载页面，直接复用`，`paper.md` md5 `7d3143c4` 不变

⚠️ **探针不要占用主端口**：我的探针用 `launch_chrome(headless=True)` 在 9222 上留了
一个**无头** Chrome，随后主流程（本该有头）复用了那个端口，于是以无头身份撞上
Optica 的 Radware —— 拦截页 URL 的 `sst=` 参数里明写着 `HeadlessChrome/152.0.0.0`，
产出降级成 `2021--Optica Article`。调试脚本请换 `CHROME_DEBUG_PORT`。

### 无头判据：先看 Crossref 的 `link` 域名，再退回 publisher 名

`_crossref_headless_publisher()` 原来只按 **Crossref 的 publisher 名**做词边界匹配。

- ❌ **平台托管多个出版社时，这个判据必然失效**：Crossref 记的是**出版社**而不是
  **平台**。J-STAGE 上是 `Laser Society of Japan`（几百个学会，列不完），
  `10.2184/lsj.49.6_349` 因此永远匹配不上 —— 而它的
  `message.link[0].URL`（`https://www.jstage.jst.go.jp/…/_pdf`）直接说明文章住在哪。
  Springer、Wiley 是同一种形状
- 📌 现在的顺序：**① `link` 第一条的域名**，交给 `detect_publisher_from_url`
  （与落地之后用的是同一个检测器，两处不会漂）；**② 没有 link 或域名未知**时才看
  publisher 名
- ⚠️ **link 认出一个"已知但不在无头名单里"的出版商时直接返回 None，不回退看名字** ——
  那是一个**答案**（该用有头），回退会让更模糊的名字匹配推翻更可靠的域名判断
- ⚠️ 名字那条路**保留词边界**：`oup` 不能在 "Optica Publishing **Group**" 里误命中
- ⚠️ **日志要说清是哪个来源决定的**。改完后那句还在打印
  `根据Crossref publisher 'laser society of japan' 判断出版商为 JSTAGE` ——
  一个**不可能**得出该结论的输入。credit 错了输入的日志，会让错误判断一直隐形
- ✅ 实测六种输入：J-STAGE(link) → `jstage`；Optica(link) → None（正确，它要有头）；
  Nature(link) → `nature`；无 link 名字命中 → `oup`；无 link 名字不中 → None；
  link 域名未知 → 回退名字 → `oup`。端到端：J-STAGE 从有头变 **🟢 无头直连**，
  AIP 仍无头（367 行、12 图不变），Optica 仍有头（md5 `7d3143c4` 不变）

### ⚠️ `metadata.json` 的 title/year 改为 handler 优先

`save_metadata_json` 原来是 `s2_data.get('title') or metadata.get('title')`
（Crossref 优先），而 `organize_paper_output` 一直是 handler 优先 —— 于是**目录名和
metadata.json 可能对同一篇论文给出不同的标题**。J-STAGE 正是被它咬到：handler 特意
拼出的「日语 English」被 Crossref 的英文标题静默顶掉。两处现已统一为 handler 优先。

⚠️ 影响面**超出 J-STAGE**：其它出版商的 `metadata.json` 里 title/year 以前取 Crossref、
现在取页面。实测 Optica `10.1364/OE.444043` 的 title/year 不变、`paper.md` md5 也不变。

### researching.cn（中国激光杂志社，`10.3788`）

- `doi.org/10.3788/...` 就跳到这里。Photonics Insights 等与 SPIE 联合出版的，
  SPIE 上还有一份镜像 —— 想要 SPIE 版就在 `--json` 里传 `link`（见 examples/spie.json）
- ⚠️ **路由顺序**：SPIE 那份的 URL 路径里**含 `10.3788`**，所以域名判断必须排在
  DOI 判断前面，否则 SPIE 链接会被抢回 researching
- ⚠️ 图片是**懒加载**：`src` 是 `loading.gif`，真实地址在 `lay-src`
- ⚠️ **整页没有任何 `<h*>` 标签**：章节标题是 `p.text_index`，形如
  "2.1.1 Physics origin"，层级只能从编号推
- 正文根是 `div.text_area`（`div#mainView` 还包着站点导航和页脚）
- 关键词是一串**没有分隔符的 `<a>`**，按文本切会连成一坨
- ⚠️ `p.figure` 是**所有 float 的图注**（图和表共用）。图注跟在 `p.text_pic` 后面，
  由 `_render_figure` 一起输出；**表格没有 `p.text_pic`**，无脑丢掉 `p.figure`
  会把表标题（"Table 1. Summary of..."）吞掉而表格本身照常渲染。靠前一个兄弟
  节点区分（见 `_render_float_caption`）
- 表格的图注在**表格后面**，这是原页面顺序，不调整
- 有的文章页面里确实**没有 `<table>` 元素**（表格在原网页就没渲染），
  那就尊重原页面，不重建

### opticsjournal.net（中国光学期刊网，中文刊）

- 中国激光杂志社的**另一个站点**，登的是中文刊（中国激光、光学学报……）。
  `10.3788` 的 DOI 解析到 researching.cn，**不解析到这里**，所以只能按域名路由，
  域名判断必须排在 `10.3788` 的 DOI 判断前面
- 整页服务端渲染，元数据全在 `citation_*`；PDF 就是 `citation_pdf_url`
  （`/Articles/GetArticlePDF/<id>`）。注意 `citation_doi` 写成 `doi:10.3788/...`，要剥前缀
- ⚠️ **两个摘要**：`div.abstract-cn` 有两块，标题分别是「摘要」和「Abstract」，
  `dc.description` 只有中文那份。英文那份用 **`<title>` 当小标题**
  （Significance / Progress / Conclusions and Prospects），拍平就丢了结构
- ⚠️ 中文摘要块里混着**语音播报组件** —— `<audio>` 的降级文本「您的浏览器不支持 audio 元素」
  和「AI语音播报」链接，不滤掉就成了摘要开头两行
- 正文根是 `div.fullText-con`，章节是 `h2`，编号和标题之间是**表意空格 U+3000**
  （"1　引言"）。层级只能从编号推——所有章节都是 `h2`
- 图是 `div.ArticleFigure-list`，⚠️ **懒加载**：`src` 是 `/NV_LEGCY/images/` 的占位图，
  真实地址在 `data-src`
- 表是 `div.tableDirectory`，⚠️ **表格嵌套**：`div.tableDiv` 里是 `table#topTable`，
  它唯一的单元格里才是真表格。渲染外层会得到一个 1×1 的格子，整张表拍平在里面
- 图和表都有**两个 `<h4>` 图注**（中文「图 1. 」/「表 1. 」+ 英文「Fig. 1. 」/「Table 1. 」），
  两份都留——英文那份常带中文压缩掉的细节
- 公式是 MathML（`<disp-formula>` 和行内 `<math>`），没有 annotation，走 pandoc
- ⚠️ **章节标题写成 `<p><h2>1　引言</h2></p>`**，又是一处无效标记。HTML5 规定 `<h2>`
  会先把未闭合的 `<p>` 关掉，所以浏览器和 lxml 看到的 h2 是段落的**兄弟**，而
  `html.parser` 把它留在 `<p>` 里 —— 实测 `10.3788/CJL231490` 的 **5 个章节标题
  全部退化成正文**，整篇没有一个 `##`。正文和图片都改用
  `wildcard.parse_article_html()`（两处必须用同一种解析，否则文档顺序会漂）
- 📌 **这家的 publisher token 在主流程里是 `researching` 而不是 `opticsjournal`**：
  判定发生在重定向落地**之前**，按 `doi.org/10.3788` 猜。两个 token 现在都在
  `RAW_HTML_PUBLISHERS` 里，所以行为一致；但拿它做别的判断前要知道这件事
- 目前没见过带补充材料的文章，`get_supplemental_url()` 返回 None

### SPIE (`10.1117`、spiedigitallibrary.org)

- landing page 只有元数据（`citation_*` + `ld+json`），正文在
  `/api/{family}/article/fulltexthtml`（POST，body `{"urlId": "<doi>"}`，DOI 不分大小写）。
  响应存 `fulltexthtml.json`
- 📌 **这个请求页面自己会发，不必我们再发一次**。实测 `10.1117/1.OE.64.11.115106`：
  预载 sink 里有 `POST /api/journals/article/fulltexthtml` 200、**118,628 字符**，
  而 handler 随后又请求了同一个资源。预载发生在 Playwright 连接**之前**、handler 的
  POST 在**之后**，所以那条必定是页面自己发的。现在 `fetch_fulltext_html()` 先查捕获，
  命中就用（照常落 `fulltexthtml.json`），只有解析失败 / `hasAccess=False` / 捕获为空
  才回落到主动 POST，并打印原因
- ⚠️ 这条对 SPIE 尤其值钱：它跑 **Imperva**、是本仓最严的一家，而那次
  `_post_fulltext` 是正文页上**唯一**的页面内 `fetch()`。捕获命中时，SPIE 正文页
  只剩导航和被动监听。实测复用后 `paper.md` 逐字节相同（264 行、5 图、正文 27,704 字符）
- ⚠️ 测 SPIE 用专属 profile：`CHROME_PROFILE_SOURCE_DIR=/home/zhiping/.config/google-chrome-spie`
  （`FRESH_PROFILE=0` 播种）。这与下面「SPIE 用 `FRESH_PROFILE=1`」那条不冲突 ——
  后者针对的是**日常 profile**（播种会带进 bot manager 的案底），而这个是专为 SPIE 备的
- ⚠️ **接口按内容族分三个**：`journals` / `proceedings` / `ebooks`。**问错了族不报错**，
  只回一个 `hasAccess=False` 的空壳，看着像没权限（会议论文集 `10.1117/12.x` 就踩过）。
  族名从 landing page 的 `<meta name="citation_article_type">` 读——SPIE 自己声明的；
  缺失时按 URL 路径段兜底（`/conference-proceedings-of-spie/` → proceedings）。
  **不要去解析 JS bundle**：里面那个映射的变量名（`ar`/`ir`/`lr`）每次构建都会变
- ⚠️ 正文里 **`div.fig.panel` 不都是插图** —— 行内/独立公式也用它包，且用同一个
  `FigureImages/` 目录存 jpg。带 `<h2 class="label">` 的才是真插图（本例 37 个里只有 4 个）。
  无标签的当公式图渲染，不能编号成 `Fig. N`
- ⚠️ **图注里也嵌公式图**，而真正的图片链接在 **caption 之后**（div 里最后一个链接）。
  取第一个 `FigureImages` 锚点会抓到图注里的公式 —— 必须跳过 `div.caption` 内的锚点
- 公式 jpg **不下载**，在 md 里引用远程 URL：下载键是按 panel 编号的，一个图注里有好几张
  塞不进去；而且公式图片本身没法用（要还原公式得对 PDF 做 OCR）
- 正文 API 被拒时（`hasAccess=False`）landing page 上图还在，`extract_figures_from_landing()`
  兜底。它靠 `DetailFigure-module__buttonContainer`（"Download"/"Full-size Image" 按钮）
  认图，不能直接扫 `FigureImages/` —— 那页上有 ~50 个，绝大多数是公式
- ⚠️ **必须在文章自己的页面上调这个 API**：实测在 SPIE 首页上调用没有响应，
  在文章 `.full` 页上调用返回 926 KB
- ⚠️ `10.3788` **不是 SPIE 的前缀**，是中国激光杂志社，doi.org 会跳到
  researching.cn；Photonics Insights 这类是 SPIE 联合出版的镜像。所以
  **不按 `10.3788` 路由**，要用 SPIE 版就在 `--json` 里传 `link`
- 正文里 **每个 float 都包在 `<p>` 里**（`div.fig`/`div.disp-formula`/
  `div.article-table` 的父节点都是 `<p>`），段落只按行内渲染会把公式、图片
  占位、表格全部拍平成正文
- 章节号和标题是**两个独立的 heading**（`<h2 class="label">2.1.</h2>` +
  `<h3>标题</h3>`），要合并；表格的 label 在表**外面**，接到 caption 上
- `<!-- named anchor -->` 是注释，bs4 的 Comment 是 NavigableString 子类，
  不特判会把"named anchor"当正文输出
- 图片优先 `FigureImages/`（高清），`WebImages/` 是预览

### Wiley (`10.1002`, onlinelibrary.wiley.com)

- 服务端渲染，无正文 API。用 view-source 取源码（同 Optica），存 `source.html`
- 公式：`<annotation encoding="application/x-tex">` 里就是作者原始 LaTeX，MathJax
  一跑连 annotation 带 MathML 一起换掉 —— 所以必须用源码。没有 annotation 时
  退回 MathML→LaTeX
- PDF **不是** `citation_pdf_url`（那是阅读器），要用
  `/doi/pdfdirect/...?download=true`
- ⚠️ 这个链接必须**基于最终落地 URL 构造，不能拿原始 DOI 拼**：Wiley 会把部分 DOI
  重定向到**另一个 DOI**（`10.1002/andp.200910370` 落到
  `/doi/10.1002/andp.200952110-1106`），用原始 DOI 拼 pdfdirect 直接 404。
  `_pdfdirect_from_landing()` 取落地 URL 的路径，把 `/doi/` 后面的视图段
  （`full`/`abs`/`epdf`/`pdf`）换成 `pdfdirect` 再加 `?download=true`；
  落地 URL 不可用时才退回 DOI 拼接。落地 URL 在 `extract_all` 开头就钉住
  （`self._landing_url`），因为 handler 后面可能自己导航
- 表格有 `rowspan`（Table 2 的数据集列 rowspan=4）。按位置读单元格会让后续每行
  整体左移，表格看着正常但数值全串到错误的列 —— 必须按网格放置
- `div.article-section__table-footnotes` 是表下的 `Note:`，要跟着表格走
- 参考文献：`ul.rlist.separator` 的每个 `<li>` 去标记取文字即可
- 公式有**三种**形态，优先级：MathML annotation > 图片
  1. `<math>` 里带 `<annotation encoding="application/x-tex">` → 直接是 LaTeX
  2. 老文章（2010 年的 10.1002/cssc.201000245）没有 MathML，是预渲染 GIF
     `tex2gif-eqn-N.gif`，编号在 `span.inline-equation__label`（写作 `((1))`）
  3. **`<math>` 是空的**（De Gruyter 转到 Wiley 的刊，如 10.1515/nanoph-2021-0059），
     公式图在 `span.fallback__mathEquation` 的 `data-altimg` 里
- ⚠️ `span.fallback__mathEquation` **每个公式都有**，不能无脑当占位符删掉，也不能
  无脑当图片用 —— 旁边 `<math>` 有内容时用 LaTeX，空的时候才用图
  （见 `_needs_equation_image()`）。2、3 两种情况不抓的话整篇公式全丢
- `div.graphical-abstract` = Graphical Abstract，配图即 key_image
- 图片/公式 GIF 的编号由 `_number_assets()` **在解析前统一打到 `data-dp-asset`**：
  图片扫描和正文遍历是两次独立解析，各自计数迟早会错位（ACS 就踩过），
  从标记里读编号就不会

### ACS (`10.1021`, pubs.acs.org)

- Silverchair 平台，服务端渲染，无正文 API：元数据来自 `<meta name="citation_*">`，
  正文在 `div.article-body div.content`
- **图/表/Scheme 共用 `div.fig.fig-section` 包装，但各自独立编号** —— 同一篇里
  "Figure 1" 和 "Scheme 1" 并存。图片占位符必须用**文档顺序**编号（与
  `extract_figures_from_html` 的 key 一致），用 `data-id` 里的数字会撞车
- 标签要读渲染出来的 `div.label`（"Scheme 11."），不能假定是 "Figure"
- 公式在 `span.mathFormula`；MathJax 跑过之后该 span 被清空，只剩
  `mjx-assistive-mml` 里的 MathML —— **绝不能把 assistive MathML 当噪声删掉**
- 图片全尺寸链接 = 内联图去掉 `m_` 前缀。"Download to Slide" 的 `image=` 参数
  ACS 自己都会写错（本例 Figure 2 指向一个无关的行内公式 GIF），只在 basename
  一致时才采信
- `div.fig-modal` / `div.table-modal` 是灯箱副本，会重复整段图注和脚注，要删
- 不能用「跳到下一个 h2」来切段：摘要 h2、正文、各个后置 h2 是**同级兄弟**，而
  正文自己没有 h2，那样会把整篇论文丢掉。改为按标记直接删除不需要的小节

### PDF 下载顺序（有头 / 无头）

- **有头和无头现在都是 `tab` 优先**，一次性 Chrome 退到第二层
- ❌ **曾经有头是「先一次性 Chrome」，这条已被实测推翻**。当时的理由是「共享浏览器
  自打开正文页起就被 Playwright 接管，带自动化指纹」，听上去成立，方向却是反的：
  一次性 Chrome **根本没有自动化指纹**（Chrome 自己的启动导航去取页面，`_stealth_js`
  只注入 Playwright 的 context，它拿不到），可它偏偏是被拒的那一层。实测 EPL
  `10.1209/0295-5075/122/14004` 全流程：**4 次尝试里一次性 Chrome 每次都落到
  `validate.perfdrive.com`**，而最后拿到 PDF 的是**共享的 Playwright 浏览器**——全场
  最「自动化」的那一个——且**全程没有任何挑战**
- 📌 **结论：bot manager 在这里判的是「会话连续性」，不是指纹**。刚读完正文的浏览器
  要 PDF，给；一个没有历史、没有 Referer 的全新浏览器直接要 `/article/{doi}/pdf`，
  那是直取资源的爬虫形状，拦。把 `fresh` 排在前面，代价是白付 3 次失败 + 约 4.5 分钟
  重试节流才轮到 `tab`
- 无头本来就该 `tab` 优先：能无头访问到的出版商本来就没在拦我们，每篇都弹一个窗口
  就失去无头的意义了。现在两种模式的理由统一了
- ⚠️ 但**正文域名过的 Cloudflare 对 PDF 域名（如 `pdf.sciencedirectassets.com`）
  不算数**这一条仍然成立 —— 那是跨主机的 clearance 问题，与本条排序无关，
  `fresh`/`referer` 两层留着正是为它
- ⚠️ 一次性 Chrome **跟随本次运行的模式**：无头运行时它也必须无头启动
  （`headless` 一路传到 `spawn_chrome`，由 `chrome_argv` 加 `--headless=new`）。
  这条链路早先不传该参数，兜底一触发就弹窗——上面那句「失去无头的意义」是意图，
  不是当时的实现
- ⚠️ **无头浏览器也要自己的 profile**（`$CHROME_PROFILE_ROOT/headless_dir`，用
  `launch_persistent_context` 而非 `launch`）。`always_open_pdf_externally` 存在
  Preferences 里，缺了它**真实 Chrome 会把 PDF 渲染在内置阅读器里**，download 事件
  永不触发，每篇白等 30 秒再落到兜底。Playwright 自带的 Chromium 没有 PDF 阅读器，
  导航到 PDF 必然下载——所以这个缺陷在改用 `CHROME_PATH` 之前一直被掩盖着
- 补充材料没有独立浏览器路径，一直用传进去的 page/context，所以无头时本来就是无头下载
- `DP_PDF_FRESH_CHROME=0` 两种模式下都彻底禁用一次性 Chrome
- ⚠️ **下载目录的基线必须是空的**。一次性 Chrome 的下载目录由 `mkdtemp` 每次新建，
  里面出现的任何文件都属于本次下载。`bypass_cloudflare_cdp` 曾用 `os.listdir` 给
  `_dl_baseline` 播种，于是「在附着期间就落盘」的文件被当成已存在、永远不算新文件，
  循环空等到 `DP_CLOUDFLARE_TIMEOUT` 耗尽（当时 launch.sh 下是 600 秒，现默认 60）才由收尾的 5 秒
  兜底捡回来。Optica 这类**现场渲染几秒**的出版商每次都踩：实测 PDF 阶段
  **128 秒 → 6 秒**，且改为循环首轮正常检出；ScienceDirect 不受影响（点框仍 +11 秒、
  完成 +16 秒）
- ⚠️ **一次性 Chrome 的「快路径探测」必须短**。`open_url_in_fresh_chrome` 在启动浏览器后、
  附着 CDP 点验证框**之前**，先等一小段看 PDF 会不会自己落盘（`fast_path_wait_s`，默认 3 秒）。
  这个顺序不能反：下载可能在附着前就完成，而挑战流程的目录基线是附着后才取的，
  反过来会把已成功的下载误判为失败。但它**曾经是 20 秒**——对 ScienceDirect 这类必然弹框的
  出版商，文件永远不会落盘，那 20 秒纯粹是在推迟点框（其后附着还要最多 10 秒找 tab + 固定 2 秒）。
  未被挑战的 PDF 在启动后 1~2 秒内就落盘，短探测不会有损失

### 批量处理就用主程序的 `--file`（`batch_process.py` 已删除）

`--file` 和那个脚本走的不是同一套：主程序的批次循环**整批共享一个
`BrowserSession`**，并在每篇之间 `retire_headed_browser()` 重建 profile；
而 `batch_process.py` 是逐篇调 `complete_extraction_workflow()` 且**不传
`browser_session`**，两条都绕过去了 —— 也就是说它跑长批次时 profile 不会逐篇重建，
与「抓取 profile 永不复用」正好相反。`BATCH_SLEEP` 防拉黑休眠本来也在主程序里。

### 两个 context 之间不再搬运 cookie（已整条删除）

- 📌 **不靠搬运也有登录态**：无头 context（整批建一次）和有头浏览器（每篇重建）
  **各自**在建的时候由 `prepare_profile_dir` → `seed_profile` 从真实 Chrome profile
  播种。日志里无头 context 那句 `载入 0 个cookies` 指的是从 `storage_state` 载入的
  数量 —— 它的订阅态全部来自播种
- ⚠️ **而同步搬的是"这一篇累积的全部 cookie"（实测 1,291 条）**，其中就有
  ShieldSquare / Radware / perfdrive 在本次访问里写下的信誉状态 —— 正是
  `_strip_bot_cookies` 在播种时刻意撕掉的那份案底。它被倒进**整批唯一长寿**的无头
  context，于是撕掉的案底又被拼回去，**而且逐篇累加**
- ❌ 早先 headed→headless 那一侧还用 `storage_state()`：它为了收集 localStorage 会
  开一个页面**挨个导航到这个 context 碰过的每一个 origin**（一篇下来几十个），
  表现为关浏览器前闪过一个飞速滚屏的新 tab。那是整篇干完之后又回去批量触碰各站点
- 📌 `latest_headed_state` 一并删除。**`--refresh-headless-auth`、
  `.auth/headless_storage_state.json`、`DOWNLOAD_PAPER_HEADLESS_AUTH_STATE`
  也全部删除** —— 导出的 storage_state 没有比播种的 profile 多任何东西，却要为此
  连一次用户自己的 Chrome，而 `storage_state()` 的实现正是把一个页面导航到该浏览器
  碰过的每一个 origin。登录态现在只有一条路：**播种的 profile**
- ✅ 实测：AIP `10.1063/5.0326077` 仍走 `🟢 无头直连路径`（无头预检靠的就是订阅态），
  43 条参考文献、12 图、367 行；Optica `10.1364/OE.444043` md5 仍是 `7d3143c4`；
  两篇的 `cookie同步` 日志行均为 0

### 浏览器会「播放/显示」的文件，用 `Network.loadNetworkResource` 取

图片、视频、音频导航过去时 Chrome **渲染**它们，不下载：download 事件永不触发。
视频更糟 —— 播放器只按需取 **range**，所以连捕获里都只有第一块。

- ❌ **实测踩过的最坏形态**：IOP 的 `ppcf045005_suppdata.mp4`（1,140,156 字节）
  从捕获里取到 **26,044 字节**，而且那一份**完整、`loadingFinished` 已触发、
  与自己的 `Content-Length` 一致** —— 除了「它是 206 分片」之外每一项检查都通过，
  存到磁盘和好文件一模一样。判据是 `status == 206` 或带 `Content-Range`
- 📌 **正解是 `Network.loadNetworkResource` + `IO.read`**（`download_via_cdp_stream`）：
  让浏览器的网络栈整个取一遍，不经播放器 → 没有 range；带这个 tab 的 cookie、IP
  和 TLS 指纹 → 正是 `fresh` 层想要的身份；**纯 CDP 命令，渲染进程里什么都不跑**
  → 页面看不到脚本痕迹。实测：mp4 **1,140,156 字节**、jpg **96,193 字节**，与
  期望逐字节一致
- 📌 顺序是「等下载事件 → 取流 → 从捕获取字节」，且**取流必须排在挑战流程之后**：
  点击过了，clearance cookie 就在这个 profile 里，`includeCredentials` 会带上
- ⚠️ 取流照样过三道校验：拿回 HTML（挑战页/登录页）→ 拒绝；声明长度对不上 → 拒绝；
  空 body → 拒绝
- ⚠️ **`result.setdefault("ws_url", …)` 是空操作**：那个 dict 构造时就带着
  `ws_url=None`，`setdefault` 只在**键不存在**时才写。取流因此一直拿到空 socket、
  静默失败。要显式赋值
- ⚠️ **别在 `result["responses"]` 上边迭代边 `await _send`**：`_send` 是本模块唯一
  读 socket 的地方，它会把新事件写进同一个字典 → `dictionary changed size during
  iteration`，被外层吞成一句「CDP 连接异常」。遍历快照

### `DP_FETCH_SUPPLEMENT` 曾经不生效（两条没看阶梯的路）

设 `fresh` 却仍从 request 开始，因为补充材料下载有**四条路，只有两条看了阶梯**：

| 顺序 | 做什么 | 原来 |
|---|---|---|
| ① | `_http_download_to`（裸 HTTP + 会话 cookie） | ✅ `'request' in ladder` |
| ② | `context.request.get()`（Playwright APIRequestContext） | ❌ **无条件执行** |
| ③ | `download_page.goto(url)`（开标签页） | ❌ **无条件执行** |
| ④ | 一次性 Chrome | ✅ `'fresh' in ladder` |

②归入 `request` 档（无标签页、无导航，就是一次 fetch），③归入 `tab` 档。
⚠️ ③ **不能只拦住那句 `goto`**：后面还有三处在等它的结果 —— 等 `download` 事件
（没有导航就永远等不到，会白烧满 `DP_SUPPLEMENTAL_TIMEOUT`）、
`auto_solve_bot_challenge`（在空白页上找验证框）、内联音频的 body 等待。
统统跟着 `_use_tab` 一起关。

### 「下载完成」只有一个判据（`complete_downloads_in`）

三条下载路径此前各判各的，**而且只有两条是对的**：

| 路径 | 判据 | |
|---|---|---|
| `tab` 层（共享浏览器） | Playwright 的 `Download` API，`save_as()` 自己知道何时完成 | ✅ **不能也不需要合并** —— 它有 API，另两条只能看文件系统 |
| `fresh` 层挑战循环（`_await_download`） | 扫目录、跳 `.crdownload`、等大小稳定 | ✅ 一直正确 |
| `fresh` 层收尾（`_finalize_downloaded_pdf`） | 拿 `.crdownload` 路径**去掉后缀猜**最终名 | ❌ 坏的那个 |

后两条是**同一件事做了两遍**，而错的那遍在最后一步，把已经下好的文件丢掉。
现在"什么算完成"只有一处定义：`core.utilities.complete_downloads_in()`
（跳过 `.crdownload`/`.tmp` 与 0 字节，按大小排序），等多久、要不要等大小稳定
由调用方自己决定。

⚠️ `tab` 那条**刻意不合并**：硬合并会让一个知道传输何时结束的 API 退化成猜文件系统。

### `.crdownload` 完成后 Chrome 会改成**服务器给的**文件名

❌ **实测（用户报告，Cambridge PDF）**：日志写 `⏳ 等待下载完成（上限 600s）` →
约一分钟后 `⏰ 下载在 600s 内未完成`，而**实际只等了一分钟，且下载已经成功**。

- 成因：`base` 是把 `.crdownload` 去掉得到的（`Unconfirmed 821706`），可 Chrome 完成
  时改成的是**服务器给的文件名**（`S0263…pdf`）——`Unconfirmed 821706` 这个名字它
  从来不用。于是 `os.path.isfile(base)` 永远为假，`src` 一消失就从
  `if not os.path.isfile(src): break` 退出，循环外只看见 `src` 仍以 `.crdownload`
  结尾，就报了超时
- ⚠️ **那条消息两处都错**：时间不是 600 秒（是文件消失的那一刻），结论也不是"未完成"
  （是已完成）。而且**下载到的文件被直接丢弃** —— 又一次「产出成功、程序报失败」
- 📌 修法是**去看目录**而不是靠字符串推名字（`_newest_complete_download()`）：
  下载目录每次尝试都新建且为空，里面任何完整文件都属于本次下载，多个就取最大的
- ⚠️ **两种失败要分开报**，它们该采取的行动不同：
  `⏰ 下载在 Ns 内未完成`（等满预算）vs `⚠️ 下载文件在 Ns 后消失，且目录里没有成品`
- ✅ 四种情形逐项验过（可控的假下载目录）：改名成真实文件名 → 4 s 拿到；文件消失
  但无成品 → 如实报；一直没完成 → 等满预算才报超时；Chrome 保留 stem
  （`paper.pdf.crdownload` → `paper.pdf`）→ 原有路径不变。
  另跑 Cambridge `10.1017/hpl.2019.36` 端到端：PDF 7.12 MB、md 1,683 行

### `.crdownload` 的完成预算按文件种类给

❌ **实测踩过（用户报告，10.1126/sciadv.abn7627）**：`DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT=500`
设了却不生效，一个需要 1–2 分钟的补充材料 ZIP 在 **45 秒**被放弃 —— 那 45 秒来自
`DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT`。

- 成因：一次性 Chrome 拿到文件后统一走 `_finalize_downloaded_pdf()`，而它把 PDF
  的预算**写死**在函数里。讽刺的是它自己的 docstring 早就写着
  「Nothing here is PDF-specific, and the messages must not claim otherwise」——
  名字和注释都提醒过，预算却还是 PDF 的
- 📌 现在预算由**调用方按种类**传：PDF → `DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT`；
  补充材料 → `DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT`；图片 →
  `DP_FIGURE_TIMEOUT * 3`（**保持收紧**：一张图拖这么久就不会来了，而一篇有几十张，
  这个上限要反复付）
- ⚠️ 日志把上限打出来（`上限 500s`），否则"设了没生效"这种事只能靠读源码发现
- ✅ 用户实测：改后那个 ZIP 完整落盘

### 补充材料的两个等待都默认 300 秒

补充材料是大文件所在：实测 APS `10.1103/PhysRevX.7.041003` 一个 37 MB 视频、
AIP `10.1063/5.0321661` 六个 7–10 MB 的视频，本仓还见过 200+ MB 的
（`10.1103/PhysRevLett.127.114801`）。

| 旋钮 | 旧 | 新 | 管什么 |
|---|---|---|---|
| `DP_SUPPLEMENTAL_TIMEOUT` | 60 s | **300 s** | 导航 + 等下载事件触发 |
| `DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT` | 120 s | **300 s** | 下载**已开始**后等写完 |

- ⚠️ **两个必须一起放宽**。只放宽前一个，大视频会在"传输正在进行"时被后一个
  砍掉 —— 一个正常工作的下载在 120 秒被丢弃，和在 60 秒被丢弃是同一类损失
- ⚠️ **这两个是"预算"，不是"死锁断路器"**，别把它们和下面这两条混为一谈
  （后者**没有**跟着改，也不该改）：`DP_HTTP_TOTAL_TIMEOUT`（600 s，request 层
  总时限）、以及涓流时 `sock.shutdown()` 的看门狗。预算调大是让好的下载跑完；
  断路器不动，是保证坏的连接仍然有界 —— **两个不同的钟，各管一种故障**

### 图片 / 补充材料默认走 `request` 层（2026-09-21）

核心原则里「图片和补充材料先用 requests 直接下」**一直没生效**：
`_fetch_ladder('figure')` 和 `_fetch_ladder('supplement')` 取的都是**裸默认**
`('tab','fresh')`，`'request'` 不在里面，于是那两个 `if 'request' in ladder`
分支除非有人设 `DP_FETCH_FIGURE` / `DP_FETCH_SUPPLEMENT` 否则**从不执行**。
现在两者默认都是 `('request','tab','fresh')`。

- ⚠️ 顺带补上 `DP_HTTP_FIRST` 的作用域：它原本只在**显式配置**那条路上被检查，
  默认值里的 `'request'` 不受它管 —— 不补的话它会变成一个说了不算的开关
- ✅ 实测三篇带 mp4 的：APS `10.1103/PhysRevX.7.041003`（pdf 812,331 B +
  video.mp4 37,477,380 B / ffprobe 31.28 s）、IOP `10.1088/1361-6587/aaa57d`
  （1,140,156 B / 20.58 s）、AIP `10.1063/5.0321661`（6 视频 + 1 docx）。
  无截断、无 0 字节，magic 嗅探全部正确
- 📌 **这一层不只是省个标签页，它还带着正文页会话的 cookie**：实测 APS 那个
  mp4 的**冷请求是 403**（不带 cookie 的 HEAD 回 `text/html`），程序这层能拿到
  是因为 `_cookies_for_requests` 按 host 把 cookie 带上了
- ✅ 回退机制在真实场景里生效过：figshare 的 `ndownloader.../files/<id>` 对裸
  请求回的是网页，日志打印「直接请求拿到的是网页（text/html），回退到浏览器」，
  随后浏览器层拿到真文件 —— 没有这条判据就会得到 7 个其实是网页的 `.mp4`

### AIP 的 figshare 补充材料

- ❌ **闸门判错了东西**：`_extract_supplemental_links_from_html` 的第二条路要求
  先从 `div#articlefulltext_figshare` 的 `<a>` 里找出 article id 才肯调
  `_fetch_figshare_collection` —— 而那个函数**根本不用这个 id**，它自己从页面
  正文的 `10.60893/figshare.<刊>.c.<id>` 正则出 **collection id**。
  这道闸门以前能过，仅仅因为 figshare 的 JS 已经把那些 `<a>` 注入进了渲染后
  DOM；改读服务器响应后 wrapper 是空的，于是 `10.1063/5.0321661` 报
  「补充材料: 0 个」，而它实际有 **6 个视频 + 1 个 DOCX**
- 📌 **不要用那个 widget XHR**：`widgets.figshare.com/public/files?articleResourceDOI=…`
  确实回同一份清单（还带 `size`），但它是 figshare widget **跨域 iframe** 发的，
  而我们只附着主页面的 CDP target，OOPIF 的网络事件不在这个 session 里 ——
  靠它会时有时无。而且它给视频的是 `s3-…/video_preview.mp4?X-Amz-Expires=3600`
  的**预览版**（降质、1 小时过期），collection API 给的 `ndownloader` 才是原件
- ⚠️ figshare 按 `/files/<id>` 发文件，落盘名只有一串数字。出版商自己的标题
  （"Supplement Video 1"）只存在于 descriptions 里，而那份 descriptions 是按
  **URL** 建索引的、与落盘名对不上 —— 所以 `downloads['supplemental_descriptions']`
  另存一份**按落盘名**索引的，md 才配得上号
- ⚠️ AIP 的 `convert_to_markdown` 原本**没有补充材料段**（同 SPIE 旧病）：
  文件下到磁盘、md 里只字不提。已补，且只在真有内容时才输出标题

### 取数阶梯（所有资源共用一套回退顺序）

PDF、图片、补充材料、API/页面（如 IOP 的 `/data`）走的是**同一条三层阶梯**：

| 层 | 做什么 |
|---|---|
| `request` | 裸 HTTP，带上**正文页会话的 cookies**（按目标 host 作用域过滤） |
| `tab` | 在已打开论文的浏览器里**新开一个标签页**，用完即关 |
| `fresh` | 全新播种 profile 的一次性 Chrome（辅助实例，`aux_dir`） |
| `referer` | 一次性 Chrome **先打开来路页**，再从它**点击跳转**到目标（仅下载类） |

- 环境变量给的是「**从哪一层开始**」，失败自动向下回退：全局 `DP_FETCH_ORDER`，
  单类覆盖 `DP_FETCH_{PDF,FIGURE,SUPPLEMENT,API}`，取值 `request|tab|fresh|referer`
- ⚠️ **`referer` 层为什么必须用「点击」而不是设 HTTP 头**：实测（本地服务器抓真实请求头）
  —— 直接启动到 PDF 链接会发出「无 Referer + `Sec-Fetch-Site: none`」的请求，形同凭空直达；
  而用 CDP 的 `Network.setExtraHTTPHeaders` 补一个 Referer，又会造出「有 Referer 却**缺**
  `Sec-Fetch-User`」的组合，比不带更可疑。只有**受信任的点击**能同时给出 Referer、
  `same-origin` 和 `Sec-Fetch-User: ?1`，与真人点击逐字段一致
- 实现在 `chrome_session._download_via_referer_click()`：在 `document` 上挂**捕获阶段**的
  一次性监听器，`preventDefault()` 掐掉页面自己的跳转，再 `location.href` 跳到目标。
  两点是关键：捕获 + `preventDefault` 使得**点在哪都行**（哪怕正好点中「在线阅读器」链接
  —— Wiley 那类点了只开阅读器的场景正因此可绕过）；而 `preventDefault` **不会**消耗
  用户激活，所以 `Sec-Fetch-User` 仍在（这点是实测确认的，不是推断）
- 来路页从哪来：普通流程用主流程钉住的 `metadata['_landing_url']`，**零配置**；
  `--json` 里可用顶层 `referer` 指定，缺省读 `header.referer`。**都没有就不走这一层**
  —— 不拿 `link` 顶替（pdf-only 模式下 `link` 不一定给，给了也不一定是本文的文章页）
- ✅ **已在真实 IOP 验证可用**：`DP_FETCH_PDF=referer` 强制只走这一层，
  `10.1088/0741-3335/51/3/035013` 拿到 694,344 字节的真 PDF（13 页），
  与不带 Referer 的对照组字节数完全一致
- ⚠️ **附着前必须等文档真正提交**。`/json` 对**正在加载**的标签页报告的是**待定 URL**，
  照它附着会落在**预提交的 `about:blank` 文档**上；把点击监听器挂在那上面，真正的页面
  一提交就连监听器一起销毁，于是点击落空、却只报一句「没拿到文件」。实测踩过：
  日志显示「来路页实际停在 about:blank」、点击后停在文章页。修法是轮询
  `location.href` + `document.readyState`，确认已离开 `about:` 且解析完成再挂
- ⚠️ **「点击后没导航」不等于失败**。目标若以 `Content-Disposition: attachment` 响应，
  浏览器按**下载**处理，标签页本来就不动。成败只看下载目录是否落盘 ——
  早先把这句报成「未发生导航」，差点据此误判该层无效
- ⚠️ **来路页自己也可能是拦截页，那时这一层的前提就已经没了**。从验证码页点击，
  发出去的请求 Referer 是验证码页 —— 比不带 Referer 更糟，而且会静默烧掉一次尝试。
  实测 IOP `10.1088/1361-6587/aaa57d`：连续 **3 次外层尝试全死在这里**，每次都已先付掉
  两次 Chrome 启动 + 两轮 20 秒验证码等待；第 4 次成功的唯一原因，就是它的来路页恰好
  加载出来了。所以**重载来路页的重试必须放在这一层内部**（`_REFERER_PAGE_RETRIES`，
  一次导航），交给外层重试等于整条阶梯从头再来
- ⚠️ **判定拦截页只能匹配主机名或完整路径段，绝不能拿 URL 做子串匹配**。
  `core/utilities.url_looks_like_bot_challenge()` 里 `captcha`/`challenge`/`blocked`
  这类弱词用子串匹配看着等价，实则会把 `/article/…/challenges-in-tokamak-control`、
  `10.1002/challenge.20250101` 判成拦截页 —— 实测 4 个普通文章 URL **全中招**。
  判据放在 `core/utilities`（`chrome_session` 也要用，而它 import 主文件会成环），
  主文件的 `is_bot_challenge_page()` 在它之上再加 HTML 标记判断
- ⚠️ 而且两个调用方的**误报代价不对称**，所以不能图省事共用整张表：无头预检误报只是
  升级到有头（可容忍）；referer 层误报是**拒掉一个本来好好的来路页**、白白重载、
  再放弃一条能走通的路 —— 比它要防的问题更糟
- ⚠️ **`fresh` 层的启动导航若落在拦截器页面，立即放弃本层**。继续走 CDP 流程不但无益，
  而且有害：那套流程是按**主机名**去找启动时的 tab，而拦截页的主机名不等于目标的，
  于是匹配失败（日志里那句「未找到启动时打开的 tab」），接着它**新建一个 tab 直开目标
  URL** —— 等于从一个刚被标记的浏览器再发一次**不带 Referer** 的请求。实测 IOP：
  这一层连续 3 次各付掉一次 Chrome 启动 + 20 秒验证码等待，而那三次**不可能成功**
- 📌 同一份日志还说明了**这四层为什么必须各留各的**：那次直连 `/pdf` 的导航
  **4 次全被拦**（`fresh` 层无一例外），真正拿到文件的是「先开文章页、再点击跳转」。
  也就是说，成败的变量不是「第几次重试」，而是**走的哪条路**
- ❌ **「重载来路页就能清掉拦截」是错的，已被数据否定**。这个假设曾让
  `_REFERER_PAGE_RETRIES` 默认为 3；实测一轮 5 次尝试共重载 **10 次，无一次清掉**。
  所以默认降为 **1** —— 只做前提校验、不点击，**不再为一个从未奏效的重载多发请求**。
  想再试可用 `DP_REFERER_PAGE_RETRIES` 调大，但要先有证据
- ⚠️ **一篇之内的重试原本完全没有节流**。`retry_download` 的 `sleep` 只写在
  `except` 分支里，而阶梯各层报告失败的方式是**返回 None**（不抛异常），于是那句
  `await asyncio.sleep(retry_delay)` 从来没被执行过；`BATCH_SLEEP` 只隔开**篇与篇**，
  不隔开重试。对按 IP 计分的拦截器来说，「零间隔连打 5 次」是最差的形状 ——
  实测唯一成功的那次，恰恰发生在已经过去好几分钟之后。现在每次重试前**固定等
  `DP_RETRY_DELAY`（默认 90 秒），不递增**
- 📌 **90 秒是按实测标定的，不是拍脑袋**。两次运行都是「封锁一解除就成功」，而不是
  「第几次重试才成功」：零节流那次在第 4 次、约 4–5 分钟时成功；60 秒节流那次在第 5 次、
  约 6 分钟时成功，且**那一次明显是干净的**——完全没有 Cloudflare 挑战，一次性 Chrome 的
  启动导航也直接落到 PDF 而不是 `validate.perfdrive.com`。这种**锐利的转折**是惩罚到期的
  样子，概率抽样不会这样。4 次间隔 × 90 秒 ≈ 6 分钟纯等待，对观测窗口留了余量。n=2
- ⚠️ **图片不共用这个值**，走 `DP_IMG_RETRY_DELAY`（默认 10 秒）。图片和 PDF 不是同一个
  问题：取不到的图就是不存在，而一篇有几十张，让它们反复付 PDF 的反爬节流纯属浪费。
  实测代价：高清位 40 秒、回退位 20 秒（若共用 90 秒则是 360 / 180 秒）
- ⚠️ **提速不等于改进**。让阶梯更快放弃（`fresh` 层遇拦截即退）在单层上是对的，
  但它同时压缩了整轮的墙钟时间；如果对手的惩罚按时间衰减，**跑得更快就是跑反方向**。
  改动这条链路时，请把「每轮耗时」和「请求条数」当成两个独立的指标一起看
- **默认 `tab`**。`request` 那层对普通 CDN 很有效，但对正在挑战你的站点基本无效——
  Cloudflare 的 clearance cookie 绑定 UA、IP 和 **TLS 指纹**，`requests` 三样都对不上
- ⚠️ **显式配置是截断语义**：从指定那层起、连同其后各层。`DP_FETCH_PDF=fresh`
  得到的是 `('fresh','referer')` 而**不是**只有 fresh；要真的只跑一层，得指定最后
  那层（`DP_FETCH_PDF=referer` → `('referer',)`）
- 而调用方给的 `default` 是**完整顺序**：PDF 默认 `('tab','fresh','referer')`，
  **有头无头一样**（理由见上面「PDF 下载顺序」——`fresh` 优先那版已被实测推翻）。
  截断语义表达不了「重排」，所以 `fetch_ladder()` 仍把「截断」和「默认顺序」
  分成两种语义，只是 PDF 这一类目前不再需要按模式重排
- ⚠️ **浏览器层必须校验拿回来的是不是目标页面**。挑战页、登录页、404 都是合法 HTML，
  「非空」什么也证明不了。`fetch_html_via_ladder(expect=...)` 收一个子串或谓词；
  不校验的话挑战页会被当成正常页解析出「0 个结果」，而不是继续回退
- ⚠️ 但 `expect` **不能写成「必须含目标内容」**：IOP 大量文章本来就没有补充材料，
  若要求页面含 `#supplementarydata`，这些文章会把整条阶梯走完，最后还报错误结论。
  判据应是「是真页面且不是挑战页」，有没有内容交给解析器答
- 📌 **但真正的省法是压根别去**。「页面回来了却是空的」这种浪费，最好在**发起之前**
  就避免，而不是靠 `expect` 在回来之后补救。IOP 的补充材料就是这么办的：绝大多数
  文章根本没有补充材料，而**文章页自己会说**——有内容时才渲染
  `<a id="supplDataLink" href="/article/{doi}/data" ...>`。`iop.extract_all` 以它为闸门，
  没有这个 anchor 就不碰 `/data`，于是多数文章不再为一个空页面走完整条阶梯（在 IOP
  上还顺带少惹一次 Radware 挑战）
- ⚠️ 这类闸门要**只在「拿到了标记性 HTML 且其中没有该标记」时才跳过**。HTML 取空时
  必须照常访问 —— 否则一次 `content()` 抖动就会让所有文章静默失去补充材料，
  那比白跑一次严重得多
- 阶梯放在 **`core/utilities.py`** 而不是主文件：publisher handler 也要用，而依赖方向
  是单向的（主文件 → publisher），handler 反向 import 主文件会造出本仓第一个循环依赖
- 兼容：`DP_HTTP_FIRST=0` = 跳过 `request` 层；`DP_PDF_FRESH_CHROME=0` = 摘掉 `fresh` 层
- ⚠️ **`fresh` 这一层必须跟随本次运行的有头/无头模式**，即
  `headless=not handler.is_headed_run()`。无头的一次性 Chrome 是我们能拿出的
  **最可疑**的浏览器，而这一层的全部意义恰恰是「看起来像从没被自动化过的真人」。
  实测：有头运行 IOP 的 `/data` 页时误以无头启动，直接撞上
  `Radware Bot Manager Captcha`，阶梯各层全灭
- **怎么拿这个状态**：handler 问不到 Playwright（`Browser` 上没有 headless 标志，
  而靠 UA 判断在 Chrome 的新无头模式下已失效），所以由主流程在 `extract_all`
  **之前**挂到 `handler._force_headed`（与 `_landing_url` / `_raw_server_html`
  同一套惯例），handler 统一经基类的 `is_headed_run()` 读取
### springer_book：读捕获了，但 MathJax 拦截摘不掉

三处 `page.content()` 已改（落地页、`extract_metadata`、**目录分页页** —— 分页页
和章节页一样走 `goto_and_capture_document`），A/B 三轮 `paper.md` **md5 完全相同**
（`4441dd1e`，2076 行、431 行公式）。

- ⚠️ **但它进不了 `RAW_HTML_PUBLISHERS`，而且那一轮 noblock 是空跑**：拦截的决策点在
  **导航之前**，那时只有 DOI。而 `detect_publisher_from_url('10.1007/…')` 答
  **`unknown`** —— 识别靠的是 URL 路径里的 `springer.com/book`，重定向落地后才知道。
  于是三轮日志里 `不拦 MathJax` 都是 **0 次**，`DP_RAW_HTML_PUBLISHERS=springer_book`
  一点作用都没有
- ⚠️ **不能靠 `10.1007` 前缀去补**：Springer 的期刊也是这个前缀，走的是 NatureHandler。
  按前缀猜会把期刊一起判成 book
- 📌 所以**不要把 `springer_book` 加进那个集合** —— 加了是空操作，却会让人以为拦截
  已经摘掉了。章节走 NatureHandler 读原始响应，拦截留着只是多一层保险，不伤产出
- 📌 **书籍默认不下载补充材料**（`SUPPLEMENTAL_DEFAULT = False`）：书的"补充材料"
  就是它自己的每一章。显式的 `--supplemental` / `DP_SUPPLEMENTAL` **压过**这个默认，
  所以必须能区分"没设置"和"设成了 True"，这就是 `DP_SUPPLEMENTAL_SET` 的用处

- ⚠️ **绕过主流程的路径要自己传**：`springer_book` 会直接构造 `NatureHandler`
  并调它的 `extract_all()`，`process_with_handler` 根本不会为那个内层 handler 运行，
  于是 `_force_headed` 永远挂不上。这类嵌套调用必须手动把状态传过去
- `fetch_html_via_ladder(headless=...)` 的默认是 **True**：忘记传时宁可在无头批次里
  不弹窗（较安静的那个错误答案），但对有头运行它依然是错的 —— **显式传**

### 卡死防线（`evaluate` / `body()` / 下载落盘 都没有超时）

Playwright 里这几个调用**都不接受 `timeout=`**，也不受 `set_default_timeout` 管辖：
`page.evaluate()`、`response.body()`、`download.save_as()`、`download.path()`。
别处的等待全都有上限，所以「一次网络抖动把整批任务钉死」的地方就只剩它们 ——
而它们恰好在最热的路径上：每一张图、每一次页面内 API 取数、每一个补充材料、
每一个 PDF。防线在 `core/utilities.py`（publisher 也要用，依赖方向单向）。

⚠️ **`page.content()` 是第五个**，一样没有 `timeout=`。渲染进程崩溃时（Chrome 显示
"Something went wrong while displaying this page. **Error code: 9**"）它既不成功也不失败，
就一直等。实测 ScienceDirect `10.1016/j.jcpx.2019.100006`：预载已经捕到 10 条 API
响应、`page_raw.html` 也已落盘，随后标签页崩溃，整次运行**停在
`🛡️ 复用预载页面…` 再也不动** —— 需要的东西全在磁盘上。用 `content_with_timeout()`
（超时返回 `''`，不抛异常，因为所有调用点本来就把空 HTML 当"这页不行"）。

📌 **清点一次就会发现漏网**：报"某家已零 `page.content()`"之前要把
`extract_metadata` 这条路径也数进去 —— Cambridge、IEEE 的 `extract_metadata` 和 APS 的
参考文献兜底都是这么躲过检查的（已改）。取数阶梯的 `tab` 层和挑战通过后的重载读 DOM
也都包上了超时。当前剩余的 `content()` 只有三类：`get_page_html()` 自己的契约回落、
各家**声明过的**最后手段/纯归档、以及**尚未改造的 8 家**
（`acm`/`mdpi`/`oup`/`oup_book`/`opticsjournal`/`researching`/`science`/`springer_book`
—— 它们还在拦 MathJax，靠拦截保住公式，现状是自洽的）

📌 **但真正的修法不是加超时，是别去问那个页面**。原来的流程是"先让实时页面自证
（`page.content()`），失败了才回头看捕获" —— 顺序反了：捕获是**已经在手、已经落盘**的。
现在是：`_pick_page_by_url()` 只读 `page.url`（Playwright 自己的状态，不往渲染进程发请求，
**不可能卡**）选出页面，**捕获里有文章就直接离线继续**，只有捕获拿不出文章时才值得去问
实时页面。常规路径上主流程对正文页**一次 `content()` 都不调**。

⚠️ 其中 `download.path()` **本仓已不再使用**，`download_path_with_timeout()` 也随之
删除 —— 不是因为它会卡（那一层早就包住了），而是因为它交出的路径指向随页面消失的
临时产物，见下面那条。要落盘就用 `save_as()`，别把 `path()` 请回来。

⚠️ 还有**第四处，且不在 Playwright 里**：`_http_download_to()` 的流式循环。
`requests` 的读超时只在**套接字空闲**时触发，服务器一点一点吐字节就永远不算空闲。
实测：每 0.4 秒 1 字节的响应，在 `timeout=(15, 2)` 下**连流 24 秒，2 秒读超时一次都没响**。
补充材料里的视频最容易踩 —— 它们是唯一大到能让劣化链路「看着一直在动」好几个小时的东西。
所以那个循环另有一条 `DP_HTTP_TOTAL_TIMEOUT` 总时限，与读超时是**两个不同的钟**，
各管一种故障。

这条总时限**踩了两个坑，都是实测出来的**。改这段之前务必先读完：

- ⚠️ **只在循环体里看表是死代码**。`iter_content(chunk_size=64KB)` 会**阻塞在 urllib3
  内部**直到攒够一整块才 yield，所以涓流时循环体**一次都不会执行**。实测：60 秒里每
  0.3 秒 1 字节，迭代次数是 **0**；最后结束它的是「服务器自己停了 + 读超时」，与循环
  里那句判断毫无关系。第一版修复就是这么写的，看着合理，实则从未运行
- ⚠️ **关闭不等于取消**。要唤醒一个已经阻塞在 `recv` 的线程，只有
  `sock.shutdown(SHUT_RDWR)` 管用。同一个涓流服务器、都在 1.5 秒时动手，四种手段实测：

  | 手段 | 结果 |
  |---|---|
  | `r.raw.close()` | 8.81 s —— **没打断**（等于什么都没做） |
  | `r.close()` | 8.81 s —— **没打断** |
  | `r.raw._fp.close()` | 8.81 s —— **没打断** |
  | `sock.shutdown(SHUT_RDWR)` | **1.50 s —— 打断了** |

  所以看门狗线程走 shutdown；`close()` 留在后面只是为了归还连接池
- **两条分支都要，各管一种形状**：看门狗管「涓流」（循环体根本进不去），循环内判断管
  「稳定但永远传不完」（块正常到达，只是到不了头）。3 秒上限下两者都实测在 3.0 s 中断
- ⚠️ **`requests` 不校验 Content-Length**。服务器中途挂断时迭代**不会抛异常**，截断的
  传输看起来和完整的一模一样 —— 视频就会留下一个损坏文件却报成功，而本来能拿对的
  浏览器各层再也不会跑。所以落盘前比对声明长度（仅在没有 `Content-Encoding` 时可比，
  gzip 会改变长度）。中途**静默但不挂断**的服务器则由读超时兜住 —— 两种都有界，
  但走的是两条不同的路

- **为什么是「卡住」而不是「失败」**：连接停住不报错，页面内的 `fetch()` promise 永不
  settle，`evaluate` 就永不返回。而 `retry_download` **只在抛异常时重试** —— 于是一次
  卡死会把重试、回退低清链接、`fresh` 那一层**全部跳过**，后面什么都不再发生。
  这就是「这张图没下来」（没事，会重试）和「批次停在凌晨三点」（要命）的区别
- 两层都要，各自补对方的盲区：

  | 层 | 做什么 | 补的是对方哪个盲区 |
  |---|---|---|
  | 页面内 `AbortController` | 浏览器自己掐断请求，落进 snippet 原有的 `catch` | 悬着的传输会让**下一次** `goto(wait_until='networkidle')` 永远等不到 idle |
  | `asyncio.wait_for` | 真正的兜底 | 渲染进程本身卡住时，页面内的 `setTimeout` 根本跑不起来 |

- `DP_INPAGE_FETCH_TIMEOUT`（默认 90 s）和 `DP_HTTP_TOTAL_TIMEOUT`（默认 600 s）都是
  **死锁断路器，不是性能旋钮**：只该在传输真的不会结束时触发，不该去管「慢」。
  慢链路调**大**，别调小 —— 一个 500 MB 的真视频必须还能下完
- 图片的总时限单独收紧成 `DP_FIGURE_TIMEOUT * 3`：一张图拖这么久就不会来了，而一篇论文
  有几十张，这个上限是要反复付的
- ⚠️ **落盘一律用 `download.save_as()`，不要 `path()` + `shutil.copy`**。
  `path()` 返回的是 Playwright **自己 artifacts 目录**里的文件，而那个文件在
  页面/上下文关闭时就被删掉 —— 于是「取到路径」到「复制走」之间的每一行，都是一个
  **已经下载成功的文件会凭空消失**的窗口。而这些调用都在 `on('download')` 事件回调里，
  **没有任何地方 await 它们**，外层却会关掉 `download_page`（有头时那是新建标签页）。
  实测 IOP：`tab` 层已经拿到 PDF，却死在
  `[Errno 2] ... /tmp/playwright-artifacts-.../...`，还被当成「网络波动」去重试 ——
  等于把一次成功的下载丢了。补充材料那条更宽：取路径与复制相隔上百行，中间还夹着
  一次 `close()`
- `save_as()` 在 Download 仍存活时落盘，窗口不存在。⚠️ 但它**和 `path()` 一样没有超时**，
  所以照样要包一层（`download_save_as_with_timeout()`），否则只是把一种卡死换成另一种。
  它返回 `None`，**唯一诚实的成功判据是文件本身**：存在且非空
- `evaluate_with_timeout()` **抛**异常、`read_body_with_timeout()` **返回 `b''`** ——
  差异是故意的，为的是两者都不必改调用点：前者每处都已有 `except Exception` 兜底，且
  `retry_download` 认异常；后者每处都已把空 body 当成「没拿到，继续下一个」
- ⚠️ **`INPAGE_ABORT_JS` 必须拼进函数体*里面***：
  `"""async (u) => {""" + INPAGE_ABORT_JS + """ ...rest... }"""`。
  拼在箭头函数**前面**会让整个表达式变成两条语句，而 Playwright 对「看起来不是函数」的
  表达式是按表达式求值的 —— 每次调用、每个出版商都会立刻 SyntaxError。
  用 node 逐个 `--check` 过一遍再提交（7 处 snippet 全部验过）
- ⚠️ 毫秒值用 `.replace('__MS__', ...)` 而**不是** `%`：这些 snippet 满是 JS 花括号
- ⚠️ **空 body 绝不能算保存成功**。`download_figure` 里一旦返回文件名就等于告诉
  `retry_download`「成了」，于是重试、回退低清链接、`fresh` 层全被跳过，只留下一个
  0 字节的 `.jpg` —— 事后还分辨不出它和真图的区别

### IOP 表格：符号表、脚注、以及一次没人读的重复转换

用户报告 `10.1088/2515-7647/ac9e2f` 的 `### List of symbols` / `### List of
abbreviations` 在 md 里是**空白**，Table 9 的脚注也不见了。一个报告牵出三层：

- ❌ **`div.tableBox` 两条路都不认**。那两节的内容是**没有 `data-toolbar-type` 的
  普通 `<table>`**（30 行 / 110 行），而正文遍历只递归 `div.article-text`、只渲染
  `table[data-toolbar-type="table"]`。现在正文遍历认 `div.tableBox`
- ⚠️ 改完暴露出**标题兜底在向前扫整个文档**：`find_previous('strong')` 抓到了分页
  按钮，符号表标题成了 `**Next**`。搜索范围收进表格自己的容器
  （`div.boxout` / `div.tableBox`），且必须在表格**之前**；找不到就**不输出标题行**
  —— 原来会输出一个空的 `****`，看着像渲染 bug
- ❌ **IOP 把表格脚注放在表格外面**：`div.boxout` 里表格之后的 `<p><small>`。只提取
  `<table>` 必然丢，而它们正是"这些数字什么意思"的定义（Table 9:
  `^*^ Kerr coefficient is defined in the paper as K = Δn/λE²`）。
  `_table_footnotes()` 取它们，走同一套公式管道
- ⚠️ **脚注必须加在 `_table_element_to_md()` 里**：IOP 的表格由**两个不同的遍历**
  到达（`_walk_iop_body` 的 `div.boxout` 分支，以及裸 `<table>` 分支），只有这个函数
  是两条路的公共点。我第一版加在 `extract_tables_from_html()` 上，离线测试显示
  "脚注有了"，**md 里却什么都没有** —— 因为那条路的产出没人读（见下）
- ❌ **`metadata['_tables']` 从头到尾没有任何代码读取**。md 里的表格来自正文遍历，
  而 `extract_tables_from_html()` 的结果赋给 `_tables` 后就再没被碰过。在表格多的
  文章上这是**单项最贵的开销**：实测 46 秒，把 6,428 个单元格**第二遍**转成 Markdown
  然后扔掉。已删除该调用
- 📌 **教训**：这种"算出来、存进 metadata、没人读"的死代码不会报错，只会让每篇多花
  一倍时间；更糟的是它会**让人把修复加在错误的路径上还以为修好了**。改之前先确认
  产出到底从哪条路来
- ✅ 实测该篇：md **3,634 → 3,804 行**，表格行 **823 → 963**，Kerr 脚注 1 处、符号表
  2 处、图片 8 个不变；另一篇无表格的 IOP（`10.1088/0741-3335/51/3/035013`）
  115 行、33 条参考文献，不受影响

### `RAW_HTML_PUBLISHERS` 的 handler 不回落渲染后 DOM

`get_page_html()` 的回落链现在是：**捕获 → view-source 重取 → 空**。不再有
`page.content()` 那一层。

- ⚠️ **渲染后 DOM 不是原始响应的"弱化版"，它是另一份文档**：MathJax 已经把 TeX
  换成 SVG，剩下的只有无障碍朗读串。拿它产出的 md **看着完整、公式全丢**，而且
  事后分辨不出它和正常产出的区别 —— 混进语料库比缺一篇更糟
- 📌 **而且它几乎救不回什么**：捕获没拿到、view-source 重取也失败，说明页面本身
  多半就没加载出来，实时 DOM 大概率同样是空的
- 📌 这不是新规矩，是补齐：Cambridge 和 Wiley 的 **handler 级**回落早就因为同一
  理由删掉了，漏的是契约里低一层的这处
- ⚠️ **只对集合里的 handler 生效**。没有声明 `PUBLISHER`、本来就是按渲染后 DOM 写的
  handler 仍保留 `content()` 回落，`content_with_timeout` 那层也必须留着 ——
  那条路径的卡死风险依旧存在
- ✅ 实测：IOP `10.1088/2515-7647/ac9e2f` 正常路径不受影响（3,634 行、823 行表格、
  8 图）；构造 view-source 失败的场景，IOP 返回空并打印
  `⛔ view-source 也失败 —— 返回空正文，不读渲染后 DOM`，而未声明 `PUBLISHER` 的
  handler 仍拿到 DOM

### pandoc 的 `--mathjax` 写法按版本探测，不写死

新版 pandoc 对 `--mathjax` 每次转换都打一行
`[WARNING] Deprecated: --mathjax. Use --math-method=mathjax[:URL] instead.`
—— 而这个调用是**按公式**发生的，公式多的文章会把日志埋掉。

- ⚠️ **但两种写法在版本间不重叠，哪个都不能写死**。实测本机 pandoc **3.1.3**：
  `--mathjax` 正常且**无 warning**，`--math-method=mathjax` 直接
  `Unknown option --math-method.` —— 直接换写法会让旧版机器全部报错
- 📌 `_mathjax_args()`：进程内探测**一次**（拿一个 `<p>x</p>` 试），**新写法优先**，
  失败退回 `--mathjax`，两个都不行就不带 math 参数（宁可转换质量降级，也不要不转）
- ✅ 实测：本机探测出 `('--mathjax',)`、`MathML→LaTeX` 仍是 `$E = mc^{2}$`；
  模拟新版 pandoc（让新写法成功）探测出 `('--math-method=mathjax',)`；
  表格产出与基准**逐字节相同**

### 表格多的文章曾像卡死：每个单元格都在起 pandoc

❌ **实测**：IOP `10.1088/2515-7647/ac9e2f`（*"…data tables and best practices"*，
HTML 1.8 MB、**6,428 个表格单元格**）停在 `Step 3.5️⃣ 生成Markdown` 不动。
**不是死锁** —— CPU 一直在 10% 左右，只是慢到看不出在动。`faulthandler` 的栈：

```
extract_tables_from_html → _process_table_cell → convert_html_fragment_to_markdown
  → pypandoc.convert_text → _validate_formats → get_pandoc_formats()
      └─ subprocess.communicate
```

两个叠加的原因：

1. **`pypandoc` 每次转换前都另起一个 pandoc 去问"支持哪些格式"**，答案一个进程内
   永不改变 → 每格 **2 次**进程启动。`html_to_md_converter` 里 memoize 掉
2. **`_process_table_cell(str(cell))` 传的是整个 `<td>…</td>`**，不是内容。改成
   `decode_contents()`
3. 片段级 `lru_cache` —— 单元格重复率高（2,276 个 pandoc 片段里只有 1,803 个不同）

**100.4s → 45.9s，pandoc 6,428 → 2,972 次，表格输出与基准逐字节相同。**

- ❌ **试过"纯文本跳过 pandoc"的快路径（28.8s），已回退**。它不等价：pandoc 的
  Markdown writer 会规范化标点、转义标记字符 —— 实测 `100–150 ps` 出来是
  `100--150 ps`、`&lt;38 MHz` 出来是 `\<38 MHz`。手工复制那套转义是在猜，而
  **表示悄悄漂移会让新旧产出再也无法比对**。宁可慢一倍
- ⚠️ 判断"是不是死锁"要看 **CPU 时间是否在增长**（`/proc/<pid>/stat` 的
  utime+stime），不要只看没有输出。这次正是靠它区分"慢"与"卡"
- ⚠️ `pypandoc.convert_text` **本身没有超时** —— 这次只是慢，但 pandoc 真挂住时
  整个批次会无声停住，与本仓那一串「没有 timeout= 的调用」同源。尚未处理

### 我们自己造的文件名也要有上限（`safe_download_name`）

❌ **实测报错**：`OSError: [Errno 36] File name too long:
'/tmp/dp_dl_.../div-class-title-51-5-w-monol…'` —— 某个 PDF 的 URL 路径段里塞着
整篇标题（实测 basename **469 字节**），而 `download_via_cdp_stream` /
`_save_captured_body` 直接拿 basename 当文件名。

- ⚠️ **最坏的时机**：字节**已经取回来了**才在写盘这一步失败，于是整层报「未拿到文件」
- 📌 `core.utilities.safe_download_name()`：按**字节**截到 200（ext4 每组件 255，留出
  `.crdownload`、`_1` 这类后缀的余量），**保留扩展名**（下游的 `_detect_and_rename`
  和媒体类型判断都看它），并且不切断多字节 UTF-8 字符
- ⚠️ **它和产出目录那套上限不是一回事**，别混：`MAX_STEM_BYTES`（补充材料）、
  图片的 `name_cap`、`WINDOWS_CHILD_RESERVE` 是**一组**，共同约束**最终产出路径**；
  `safe_download_name` 只管**临时下载目录里由 URL 推出来的名字**
- ✅ 实测：469 字节的 basename 走完取流路径，落盘 50,016 字节、文件名由调用方给的
  `paper.pdf`，不再有 Errno 36

### Windows 路径预算（`organize_paper_output`）

Windows 拒绝任何超过 **MAX_PATH(260)** 的路径，且报的是
`[Errno 2] No such file or directory` —— 对一个**明明存在**的目录说「不存在」。
实测：156 字符的标题放在 `C:\Users\…\captured_data`（55 字符）下，补充材料路径达
**272 字符**（超 12），于是 `paper.pdf` 和图片都下来了、**只有补充材料全军覆没**。

- ⚠️ **原有的长度保护全是「单个组件」上限，挡不住这个**：标题 150 字符、补充材料
  stem 200 字节（ext4 的 255 字节/组件）、图片名 180 字符 —— 它们各自都合法，
  加起来仍然超。Windows 限的是**整条路径**
- 所以 `organize_paper_output()` **按输出根目录的实际长度反推**标题上限：
  `259 - len(root) - 1 - WINDOWS_CHILD_RESERVE - len("{year}--")`，
  钳在 `[20, 150]`；根目录深到连下限都放不下时**明确告警**，不静默截断。
  Linux 不受影响，仍是 150（否则会悄悄改变既有语料的目录名）
- ⚠️ **预留值必须按"最长的尾部"算，不是"最深的那条路径"** —— 这两者不是一回事，
  我第一版就栽在这：只算了补充材料（更深），漏了图片（更长，且直接躺在论文目录下）：

  | 尾部 | 长度 |
  |---|---|
  | `\supplemental\` + stem(80) + `.docx` + `_100` | 103 |
  | 图片名（Windows 上限 80）+ 分隔符 | 81 |
  | `\html\page_raw.html` | 19 |

  `WINDOWS_CHILD_RESERVE = 104` 覆盖三者
- ⚠️ **三个常数是一组，改一个就要重算这张表**：`WINDOWS_CHILD_RESERVE`
  （`core/utilities.py`）、`MAX_STEM_BYTES`（补充材料）、`name_cap`（图片）。
  第一版把预留写成 96 而最坏尾部是 100 —— **差 4 个字符，等于把要修的 bug 放回去**，
  是逐项相加才发现的，不是看出来的

### 一份 HTML 只落一次（`page.html` 已删除）

- 产出目录里正文页只有 **`page_raw.html`** —— 服务器发的那份。**不再写 `page.html`**
- ❌ 它原本是"渲染后 DOM，且只在与原始响应不同时才写"。全部 handler 转完后，
  **没有任何东西能产出既不同、又是新视图的那份**，实测：

  | handler | 会写进 page.html 的是 | 实测 |
  |---|---|---|
  | 其余 17 家 | 与原始响应相同 | 分支恒为 no-op |
  | IEEE | REST body | 与它已落盘的 `rest.html` **逐字节相同**（68,270 B）|
  | SPIE | fulltext HTML | 与 `fulltexthtml.json` 里的 `fullTextHtml` **62 行逐行相同** |

  也就是说它只是把目录里已有的文件再抄一份，名字却承诺"第二个视图"
- 同时删掉的还有 `PageCapture.land()` 的 `rendered_html` 参数 —— 两个调用者早就不传了
- ⚠️ 旧目录里残留的 `page.html` / `source.html` 不会被自动清理（实测存档里有 74 个），
  看时间戳，别把上次运行的产物当成本次的
- 📌 **已改造的 handler 不再专门取一份渲染后 DOM 来归档**。ScienceDirect 和 Optica 原本
  为此调一次 `page.content()`，产出的 `page.html` 与 `page_raw.html` 字节相同、而且没人读。
  现在 `fulltext_data` 直接交回原始响应，`page.html` 自然不再出现
- ⚠️ **也不再有"退回渲染后 DOM"的最后手段**（Cambridge、Wiley 原本有，且是声明过的）。
  那份 DOM 上 MathJax 已经把公式换掉，产出**看着完整、公式全丢**；现在返回**空正文**
  并打印，理由同「宁可保留一个空正文的 md」
- 📌 **主流程也不再读渲染后 DOM**。`navigate_with_capture` 原本每次导航后都
  `page.content()` 一次（喂挑战检测、并在无头路径上落 `page.html`），两件都不需要它：
  挑战页的标记就在**服务器发的那份**里（`_cf_chl_opt`、`Just a moment` 标题、
  `challenge-platform` 路径，均实测于 `page_raw.html`），而 `page_raw.html` 就是归档。
  挑战通过后的重载同理，直接取监听器新收到的那份文档
- 📌 **全流程只剩一处 `content()`**：`_looks_like_article_page`，且**只在捕获里没有文章时**
  才调用 —— 那时问页面正是唯一的办法
- ⚠️ `fulltext_data == raw_server_html` 这个判断**不泄露**（纯字符串比较，两个值分别来自
  handler 和捕获）。它现在只为**尚未改造的 8 家**存在；对已改造的 11 家恒真、永远走
  "不另存"。**最后一家改完时，这个分支连同 `page.html` 一起删掉**
- ⚠️ 旧目录里残留的 `page.html` / `source.html` 不会被自动清理 —— 看时间戳，别把上次运行的
  产物当成本次的
- ⚠️ handler 大多把原始响应原样交回当 `fulltext_data`，于是两个名字装同样的字节 ——
  实测存档 **57 篇里 30 篇** `page.html` 与 `page_raw.html` 逐字节相同。现在"有 page.html"
  才真的意味着存在第二个视图
- ❌ `headless_initial.html` **已取消**：它写的是 `raw or rendered`，也就是上面两个之中
  的一个，实测 **5/5** 与 `page_raw.html` 逐字节相同
- ⚠️ 正文页的 HTML **只由主流程落盘**。handler 侧剩下的写入都不是正文页：
  Wiley / Optica 的 `source.html` 仅在 view-source 救援触发时写（写了就说明预载捕获落空，
  是个要查的信号）；Cambridge 的 `page_shell.html` / `page_fr.html` 是重取阶梯的证据；
  IOP / APS 写的是**补充材料页**

### 预载的 DOI 判定看捕获，不看页面

- ❌ 旧判据只读 `document.body.innerText`。**客户端渲染的页面永远过不了**：
  IEEE 把书目元数据以 JSON 塞在 `<script>`（`xplGlobal.document.metadata`）里，
  Angular 之后才填 DOM。实测 `10.1109/pac.1997.752724`：标题正确、`cf=✗`、
  `body=2465` —— DOI 判据和 `>5000 字` 兜底**都不可能触发**，于是每篇 IEEE
  都空等满 60 秒，最后报一句根本没发生的「挑战未在 60s 内通过」
- ❌ **第一版修复是在页面里查 `outerHTML`，那是错的方向**：它要让渲染进程把整份
  DOM 序列化成几百 KB 再传出来 —— 又重又不像真人浏览器的动作，而要找的 DOI
  本来就在**已经到手的响应**里
- 📌 现在**先看捕获**（`Network.getResponseBody`，纯 CDP 命令，渲染进程里什么
  都不跑、页面无从观察），捕获里没有文档时才问页面，且**只问 `innerText`**
  —— 循环本来每轮就在读它。实测 IEEE：`✅ DOI […] 见于捕获的响应，挑战通过
  （页面 3793 字）`，仍远低于 5000 的兜底门槛
- ⚠️ 日志要说清**哪一份**答的。旧措辞「已出现在页面」配上 `（0 字）` 看着像 bug，
  其实是服务器发的那份在说话 —— 而那正是好情况
- 📌 同一条原则也用到了 `want_html`（取数阶梯 `fresh` 层取 HTML 页面）：一次性
  Chrome 既然已经先附着再导航，响应就在手，优先从捕获里取（用
  `pick_raw_article_html` 选，不是 `[-1]`），取不到才回落
  `fetch_page_html_via_cdp()` 的 `outerHTML`
- ⚠️ **轮询循环里仍有页面内调用**，去不掉：`document.title`、`body.innerText`、
  挑战 DOM 标记 —— 挑战 widget 是脚本**注入**的，服务器发的那份里没有它

### 交给调用方判定的那份 HTML，也必须用 `pick_raw_article_html()`

❌ **实测踩过（2026-09-20）**：把预检从"读渲染后 DOM"改成"读捕获的文档"时，
`navigate_with_capture` 返回的是 `capture.documents[-1]` —— 而它一路传给
`is_bot_challenge_page()` 做拦截判定。后果是 **AIP 每一篇都被判成拦截页、
每一篇都退到有头**，而日志里同时印着 `DEBUG: is_challenge_page=False`、
真文章 URL、真标题、`page_raw.html` 419,015 字符。

- 📌 成因就是 `[-1]`：监听器记下**每一个** ok 的 HTML 文档 —— doi.org 跳转、
  挑战页、以及文章拉进来的**每个 iframe 文档**（AIP 文章页有 10 个）。
  `[-1]` 取的是"谁最后加载完"，于是某个带 `perfdrive` 字样的第三方 iframe
  成了判定依据。把**服务器发的那份**拿去跑判定：21 个挑战标记**命中 0 个**
- ⚠️ 同一个坑本仓早就写过（见下一节「不要用 `[-1]` 取它」），我改的时候只把
  **落盘**那条路换成了选择器，漏了**交给调用方判定**的这一份 ——
  一个坑两个出口，只堵一个等于没堵
- 判定的那份和落盘的那份现在是**同一份**；`[-1]` 留作兜底（真被挑战时本来就
  该让调用方看到挡板页）
- 📌 **排查方法值得记**：同一台机器、同一 IP、二十分钟内跑三种代码 ——
  旧代码（拦 MathJax、读渲染后 DOM）✅ 无头；今天的 HEAD ❌ 被判拦截；
  HEAD + 本修复 ✅ 无头、367 行与旧代码逐项一致。三次对照才把"IP 信誉"
  这个看似合理的解释排除掉 —— 我一度据此下过结论，是错的

### 原始响应的捕获与选取（`_raw_server_html`）

正文页的响应由 `page.on('response')` **被动**捕获——不发额外请求、不在页面里执行任何东西，
拿到的就是 view-source 所见的 JS 前字节（已用受控实验证实：故意写坏的属性引号与
`<HTML>` 大小写在其中原样保留，而 `page.content()` 会把它们规范化）。

- ⚠️ **不要用 `[-1]` 取它**。监听器会记下**每一个** ok 的 HTML document：doi.org 的重定向跳转、
  挑战页、iframe 文档，最后才是文章。取"最后一个"是假设文章排在末尾，而这不保证——
  一旦猜错，调用方会**静默地**去解析一张挑战页，然后报告「0 个图」而不是报错。
  用 `core/utilities.pick_raw_article_html()`：在带 `citation_*` 或本文 DOI 的候选里
  挑**内容最多**的那一份，一个都没有就在全部候选里挑最长的
- ⚠️ **判据是「体量」，不是「位置」，也不是「第一个带标记的」**。设防的出版商会对
  **同一个 URL** 答两次：过 bot 检查**前**一次、**后**一次。实测 ScienceDirect 的
  `…/pii/S221137972100245X?via=ihub` 就有两个文档响应，只有第二个带元数据 ——
  而**两个都可能带 `citation_*`**，只按标记挑就会拿到过检前的空壳，最后报出一篇
  没有作者、没有图的文章。空壳短、真页面长，体量才是能用的信号
- 📌 早先那版是「最后一个带标记的，否则 `[-1]`」，并承诺「永不比 `[-1]` 差」。
  这个承诺已被**故意**去掉 —— 空壳若恰好排在后面，`[-1]` 正是选错的那一个
- ⚠️ **绝不可把 `page.content()` 塞进这个列表**。CF 旁路路径曾经这么做（"至少有点东西"），
  结果是渲染后 DOM 冒充原始响应：Optica、Cambridge 读 `_raw_server_html` 正是为了拿到
  MathJax 尚未破坏的 TeX，喂给它们渲染后的 DOM 等于**悄悄取消了这项保护**，而且让
  `get_page_html()` 自己的回落机制失效（它本来就会在没有原始响应时调 `page.content()`）。
  什么都不放，反而让来源诚实
- ⚠️ **`fulltext_data` 会绕过你在 `extract_all` 里做的选择**：主流程把它原样传回
  `convert_to_markdown`，所以 handler 即使自己读了原始响应，落到 md 的那一步仍可能
  在解析渲染后 DOM。ScienceDirect 就这样漏了 2 个公式 —— MathJax 3 的 CHTML 不带任何
  annotation，剩下的只有无障碍朗读串，于是摘要里出现
  `$\text{[math: 10 to the 17th power times watts divided by centimeters squared]}$`，
  而**同一份原始响应里就有源 MathML**。SD 现在把原始 HTML 钉在 `self._extraction_html`
  上，`convert_to_markdown` 优先读它；`page.html` 仍存渲染后 DOM，只作诊断用
- 📌 **IOP 已不再读实时 DOM**：`extract_metadata` 与 `extract_all` 都改用
  `self.get_page_html(page)`。实测 13 篇 IOP 产物，仅凭 `page_raw.html` 提取出的图数与
  磁盘落盘图数 **13/13 一致**，正文/参考文献数也与改前逐项相同 —— 所以正文页上只剩
  **导航 + 被动监听**
- ⚠️ 但**离线化不等于不被打分**：ShieldSquare 的 profiler 在页面加载时就已同页运行
  （`perfdrive`/`__uzm` 在抓到的 HTML 里都在），而被动捕获本身也要求页面真的加载过。
  省掉的是「加载之后还在页面里动手」，不是「这次访问有没有被评分」
- ⚠️ **老存档里的 `page_raw.html` 不都是原始响应**。`9149ab4` 之前，CF 旁路路径会把
  `page.content()` 塞进 `_headed_raw_html`，于是渲染后 DOM 会冒充原始响应落盘。
  实测 12 份 IOP 存档里有 2 份带**真实的** `<mjx-container>` 元素（剔除
  `<style>`/`<script>` 后仍在），两份的写入时间都早于该提交。拿老存档做离线回归时
  要先看时间戳。⚠️ 反过来，**光用子串 `mjx-container` 判定是错的** —— MathJax 的
  `<style id="MJX-CHTML-styles">` 里全是同名 CSS 选择器，按子串数会得到「29 处」
  而元素其实只有 1 处；这个误判本仓踩过一次

### `block_mathjax` 已整个删除

用 `page.route` + `route.abort()` 掐掉 MathJax 脚本，是**页面内干预**——被中止的
子资源请求在页面里看得见。现在全仓**没有任何 `page.route`**。

- 📌 **理由不是"用得少了"，是它已经什么都不保护**。所有 handler 都读捕获的原始响应，
  而原始响应里 MathJax 根本没跑过。对**没有 handler 的出版商**，兜底是主程序把
  `page_raw.html` 落盘、供日后照着写 handler —— 那份也是响应。拦与不拦，我们要看的
  字节完全一样
- ❌ **我一度报告过"它还守着 `10.1007`(Springer) 和未知出版商"，这个说法站不住**。
  `10.1007` 之所以会被拦，只是因为**判定发生在导航之前**、那时只有 DOI 而
  `detect_publisher_from_url` 答 `unknown` —— 可真正接手的是 `springer_book`
  和 `NatureHandler`，两个都读原始响应。那是**判定时机的产物，不是谁的需求**
- `10.1515`（De Gruyter）同理，且目前没有相关文章；日后要抓就写专门的 handler
- ⚠️ **唯一真的变了的地方**：`get_page_html()` 最后那层回落 —— 捕获为空**且**
  view-source 也失败时读 `page.content()`，那份 DOM 现在没有任何东西拦着 MathJax。
  它本来就会打印降级提示，现在提示词改成「公式可能已被 MathJax 替换」
- `RAW_HTML_PUBLISHERS` **保留**，但含义只剩一条：**谁享受 view-source 救援**。
  ⚠️ token 是从 handler 的 `PUBLISHER` 读的，**光写在集合里没用** —— IEEE 和 SPIE
  在集合里却一直没声明 `PUBLISHER`，等于从没拿到过救援，已补上
- `should_block_mathjax()` 和 `DP_RAW_HTML_PUBLISHERS` 一并删除（那个环境变量是为
  逐家 A/B 存在的，八家做完就没有用处了）

### 有头 / 无头必须行为一致

两者只是同一个 Chrome 的两种模式，**流程应当一样**；只有"浏览器本身怎么开"允许按模式分叉
（例如一次性 Chrome 必须跟随本次运行的模式，见 `is_headed_run()`）。

- ⚠️ **主文件里「加载页面」这一段有两套实现**：有头走预载 + 主流程监听器，无头走预检；
  而**无头可直连的出版商在 `:3604` 之后直接 `process_with_handler(...)` 然后 return**，
  连主流程那段都不经过。于是"补在主流程"对它是无效的
- ❌ 实际踩过：`_captured_api` 只有预载 CDP 供货，**无头下 handler 的"复用捕获"是恒假死代码**。
  症状不是报错，而是**静默少拿东西**（APS 会照旧去访问 supplemental 页面，SD 会重发正文请求）
- 现在三条路径都收 API 响应：预载 CDP、主流程 `_headed_on_response`、预检 `_headless_on_response`；
  判据只有一份（`core/utilities.api_harvest_patterns` / `url_wants_api_harvest`），
  `chrome_session` 里的同名私有名字是再导出
- 📌 钉到 handler 上的状态统一走 **`pin_capture_on_handler()`**，两条分支都调它 ——
  这是针对"下次又只落一边"的结构性防护，新增状态改一处即可
- ✅ 实测 `10.1017/hpl.2019.36`（无头直连）：`✓ 预检捕获 API 响应 6 条（最大 268,124 字符）`，
  与关闭捕获的对照组相比 md 913 行、参考文献 390 条完全一致

### 每一次 CDP 加载的响应都要收（`absorb_preload_capture`）

- ⚠️ **不是某一家出版商的事，是主程序的职责**。任何一家只要走到 Fallback，
  以前都等于没捕获，handler 会重发页面刚刚取过的请求
- ❌ 两个漏洞（都已修）：**Fallback 那次 `bypass_cloudflare_cdp` 的
  `result["responses"]` 根本没人接**；第一次预载**只在 `success` 时才吸收**
- 📌 第二条尤其要紧：**SPIE 对没有 referer 的冷启动 tab 大概率拦**，所以"预载失败"
  是常态不是例外 —— 而失败的那次加载里，页面往往**已经把 fulltext 和 supplemental
  都取回来了**，只是 DOI 校验没过。现在两处都在**读取成败之前**就先吸收
- 收下失败加载的响应是安全的：文档由 `pick_raw_article_html` **按体量在带标记的候选里挑**，
  挑战页又短又没有 `citation_*`，只会多一个必输的候选；API 按 requestId/URL 分别存，
  挑战页产生不了 `/article/fulltexthtml` 这种键。两次加载的捕获**累加**
- ✅ 实测 `10.1117/1.APN.4.3.036004`：`预载捕获 API 响应 2 条（最大 138,704 字符）`
  → 正文与补充材料**都复用捕获、零重复请求**，补充材料落为出版商自己的文件名

### 预载期间的响应捕获（`bypass_cloudflare_cdp` → `result["responses"]`）

有头运行时，正文页是**纯 CDP 预载**打开的——在 Playwright 连接**之前**。于是
`page.on('response')` 那个监听器**永远看不到主文档**：它诞生时页面早已加载完毕。
后果曾经是三处彼此看似无关的毛病，其实同一个病根：

| 症状 | 原来的绕法 |
|---|---|
| IOP 每次都打印「未捕获到原始响应，改用 view-source 重取」 | 页面内再 `fetch()` 一次（两次 `page.evaluate`） |
| APS 的 `abstract_html` 被静默跳过 | 退回读实时页面（`aps.py` 注释里有记载） |
| `page_raw.html` 不落盘 | 无 |

现在预载自己把响应记下来，交回 `result["responses"]`，主流程并入 `_headed_raw_html`，
由 `pick_raw_article_html()` 选出正文那一份。**实测 EPL `10.1209/0295-5075/122/14004`：
「未捕获到原始响应」一行消失，`page_raw.html` 正常落盘 217,068 字节**，正文页只剩
导航与被动监听。

- ⚠️ **但这个捕获是有竞态的，会漏掉主文档，别当成必然成立**。tab 是用
  `_create_new_tab(debug_port, url)` **直接开在目标 URL 上**的，Chrome 立刻开始取主
  文档，而我们要等连上 WebSocket、发完 `Network.enable` 之后才收得到事件 —— 主文档
  的 `Network.responseReceived` 如果赶在 enable 之前发生，就再也补不回来。
  实测 ScienceDirect `10.1016/j.rinp.2021.104097`：**收到 110 条响应事件，
  Document 0 份**，于是 `page_raw.html` 没有生成（该篇 body 稳定耗时 14 秒，
  比 EPL 那篇的 6 秒慢，主文档反而更早就到齐了）
- 📌 这对 ScienceDirect 暂时无害 —— 它的正文走 `/sdfe/arp/pii/{pii}/body` API，
  本就不依赖主文档响应。但「同一 URL 会答两次、要挑过检后那一份」这件事，
  在捕获落空时**根本轮不到 `pick_raw_article_html` 去挑**
- 🔧 要根治就得把顺序倒过来：先建空白 tab → 附着并 `Network.enable` → 再
  `Page.navigate` 到目标。⚠️ 注意不能退回「开在 `chrome://newtab` 再用 JS 导航」那
  条老路——本仓记过，WebUI 页面会**静默拒绝**跳转到公网，轮询会一直看到
  `title='New Tab' body=0`

- **事件只能在 `_send` 里收**：它是本模块唯一读 socket 的地方（`if "id" not in resp`
  那一行原本直接丢弃事件），所以捕获挂在那里，35 个调用点一个都不用改
- ⚠️ **用 `contextvars.ContextVar` 而不是模块全局**：正文预载与 PDF 一次性 Chrome 都
  会进 `bypass_cloudflare_cdp`，asyncio 下可能同时在飞。全局会把一个页面的响应混进
  另一个的字典里 —— 那比捕获不到更糟，因为混出来的东西**看着像一份有效捕获**
- ⚠️ **`result["responses"]` 必须在字典构造那一处就种好**。该函数有 5 个成功出口、
  外加超时、最外层 except 和「拿不到 ws_url」的提前返回。只在成功路径上加键，
  其余路径的调用方 `.get("responses")` 会**静默拿到空**
- ⚠️ **`Network.enable` 不再是可有可无的**。它旁边那句注释曾说 `Page.enable`/
  `Network.enable` 是多余的、可以考虑删掉 —— 现在删了就会悄无声息地清空所有捕获
- ⚠️ **取 body 必须在 socket 关闭前**：Chrome 把响应体放在渲染进程的缓冲里并会**驱逐**，
  `async with` 一退出就再也取不到。所以每个出口在 `return` 前都调
  `_harvest_document_bodies()`；超时路径也调 —— 没通过判定的页面照样可能已经
  交付了一份完好的文档
- **Document 全取，XHR/Fetch 只取名单上的**（`_DEFAULT_API_HARVEST`：ScienceDirect
  的 `/sdfe/arp/`、IEEE 的 `/rest/document/`、APS 的 `/fulltext/10.` 与
  `/supplemental/10.`）。图片、CSS、字体也各有 requestId，逐个 `getResponseBody`
  会让每篇论文多出几十次往返，而没有任何东西会读它们
- ⚠️ **APS 那两条带着 DOI 的 `10.`**：裸的 `/fulltext/` 会命中别家出版商的阅读视图，
  每篇白付一次往返。`DP_HARVEST_API=0` 整体关掉，给逗号分隔的列表则整体覆盖
- ⚠️ **复用捕获的一方也必须照常落盘**。APS 复用 fulltext 时若跳过写 `fulltext.json`，
  产出目录就悄悄变得不可离线重建了——落盘文件是交接接口，不是缓存
- 📌 **按 requestId 存，不是按 URL**。设防的出版商对**同一个 URL** 会答两次（过检前、
  过检后），按 URL 存后者会覆盖前者、或前者挡住后者；按 requestId 两份都在，
  再交给 `pick_raw_article_html` 挑体量大的那份
- ✅ **与旧路子逐字比对过**：同一篇文章，CDP 捕获得到 217,068 字节，页面内
  view-source 得到 217,074 字节，**整份文档只差 4 行**，且差异是服务端 JSON 配置的
  **键序**与一个脚本标签；`citation_` 364/364、`math/tex` 134/134、`supplDataLink` 1/1、
  `article-text` 16/16 完全一致。所以它是那个 hack 的**忠实替代**，不是近似品

### 不是 Cloudflare 的验证框

- ⚠️ **判据只认 Cloudflare，别家的框连点击逻辑都进不去**。`_is_challenge_title()` 匹配 CF
  各语言标题，`_CHALLENGE_DOM_JS` 匹配 `#challenge-form` / `[id^="cf-chl"]` /
  `cdn-cgi/challenge-platform` —— **Imperva（SPIE）一条都不沾**，于是 `is_challenge`
  恒为假，`_CHALLENGE_CLICK_TARGET_JS` 一次都没被调用过
- 实测 SPIE 日志里那个框长这样：`📊 title='' cf=✗ iframes=1 body=0`，连续出现 4 次，
  随后才是文章 —— **那几次是用户手动点掉的**，不是程序过的
- 📌 所以加了一条**与厂商无关**的判据：**正文为空、有 iframe、连续两轮**（≈4 秒）。
  ⚠️ 刻意收窄：要求 body **完全为空**而不是"很短"，两轮是为了排除"还在加载"。
  误报的代价是往一个空白页上点一次
- 📌 命中时**把每个 iframe 的 src 打印出来**（`_report_iframe_sources`）。这是写出精确
  判据的唯一途径 —— 只有它能告诉我们那个框是 Imperva 的 `_Incapsula_Resource`、
  还是 hCaptcha / reCAPTCHA。iframe 选择器也补上了这几家
- ⚠️ 这条路径**尚未在真机上被触发验证过**，下次 SPIE 再弹框时看日志里的
  `🤖 空白页 + iframe 持续 2 轮` 和 `🔎 iframe:` 两行

### handler 额外打开的页面（`wildcard.goto_and_capture_document`）

有些 handler 必须开正文页以外的页面：Nature 的行内表格页、Springer 书的章节页。
**导航是允许的，用 `page.content()` 读回来不是** —— 那些页面里同样有公式，MathJax 一样吃掉。

- 统一走 `goto_and_capture_document(page, url, tries=3)`：挂临时监听器 → `goto` → 取
  **原始文档**（最大的那份）→ 摘掉监听器
- **捕获为什么会落空**（这就是重试的理由）：导航抛异常（超时/网络抖动）；响应不是
  200 `text/html`（重定向进验证页、403、意外 content-type）；页面来自 bfcache，
  **根本没有文档响应**。每次重试都是**重新 goto**（不是 reload —— 第一次可能落在别处）
- ⚠️ 三次都拿不到就**跳过这张表**，不回落渲染后 DOM：宁可缺一张表，也不要一张公式
  已被替换的表混进 md
- 表格页**要落盘**（`html/table_<id>.html`）—— md 就是从它构建的，不落盘产出不可溯源
- ⚠️ **`springer_book` 直接构造 `NatureHandler` 并自己导航到章节页**，
  `process_with_handler` 对它不运行，所以主流程会钉的东西都得手动钉：
  原本只钉了 `_force_headed`，现在把章节页的原始响应也钉上
  （否则内层 handler 的 `get_page_html()` 会静默回落到渲染后 DOM）

### 带 referer 的预载（`--json` 的 `referer`）

凭空起一个浏览器、直奔文章页，发出的请求**没有 Referer、`Sec-Fetch-Site: none`** ——
"从天而降"正是 bot manager 要抓的形状。用户实测：**同一个浏览器里点"+"打开同一个网址就不会被拦**，
甚至从被拦的 Cloudflare 页面点"+"重开也不会。

- ⚠️ **但"+"打开同样不带 Referer**，所以起作用的多半是**会话连续性**（这个浏览器已经和该站
  打过交道、拿到了 cookie），而不是 Referer 本身 —— 与 IOP 那次的结论一致
- 📌 "先开来路页、再点击跳转"**两样一起给**：来路页那次加载收下站点 cookie，点击又带上 Referer
- ⚠️ **必须是点击，不能是设头**。`Network.setExtraHTTPHeaders` 或 `Page.navigate` 的
  `referrer` 参数会造出"有 Referer 但**缺** `Sec-Fetch-User`"的组合 —— 真人点击永远不会
  产生这种形状，比不带更可疑。复用下载阶梯那套已验证的 `_REFERER_CLICK_JS` + 受信任点击
- ⚠️ **来路页自己是拦截页就不点**（那样 Referer 是验证码页），点击失败退回直接导航
- ❌ **点击之后必须等页面真的离开来路页**。预载循环有个兜底判据「body > 5000 字且非挑战页」，
  而期刊某期的**目录页轻松满足**。实测：不等的话预载在还站在来路页时就宣布
  `✅ 未触发挑战，直接访问成功`，**把来路页当文章捕获**（155,459 字符、0 条 API），
  产出一篇 **144 行**的 md（正确是 303 行）。表现是"看着成功"，不是报错
- 📌 **来路页本身被拦也照点**（只在预载这一侧）。下载阶梯的 referer 层要的是一个文件，
  从验证码页点过去 Referer 就是验证码页 —— 实测 IOP 连续 3 次死在那里，所以那一层规避。
  **预载要的是会话**：那次加载收下的 cookie 才是关键，这正是"从被拦的页面点 + 也能过"
- ⚠️ 而且**拦截往往看不出来**：实测这次预载的 5 个文档响应里，来路页是 **403**、
  `link.aps.org` 也是 **403**，但 URL 都是正常的文章/目录地址 ——
  `url_looks_like_bot_challenge()` 只按主机名和完整路径段判，**看不到**这种情况。
  所以那道"来路页是拦截页就不点"的护栏在这里几乎不会触发
- 📌 **按 loaderId 划分响应**（`PageCapture.restrict_to_article_load`）：Chrome 每次主框架
  导航都发一个新的 loaderId，这正是 DevTools 导航时清空 Network 面板的依据。只保留
  文章那次加载的 API 响应。⚠️ **文档一份不丢**（文章是谁正是靠在文档间挑出来的，
  先过滤文档会变成循环论证）；**没有 loaderId 的一律保留**（Playwright 监听器拿不到
  这个字段，按"未知就丢"会清空整个无头捕获）
- ⚠️ **但实测里它一条都没丢，因为轮不到它**：5 个文档响应最后只取到 1 份 body ——
  前面几份在导航后就被 Chrome 从渲染进程缓冲里**驱逐**了。所以这层过滤是**备而不用**的
  保险（驱逐时机不保证），不是这次成功的原因
- ✅ 修好后实测 `10.1103/PhysRevX.7.041003` + `referer=https://journals.aps.org/prx/issues/7/4`：
  `📄 已离开来路页 → https://link.aps.org/doi/…` → 预载捕获 API **4 条**、文档 1 份、
  正文与补充材料都复用捕获、303 行、5 图、2 补充材料
- 用法：`--json` 顶层 `referer`（缺省读 `header.referer`），**预载和 Fallback 两条路都用**

### 挑战页点到哪里（`_CHALLENGE_CLICK_TARGET_JS`）

预载循环认出挑战页之后分两条路：`_find_turnstile_iframe_cdp()` 找到 Turnstile
iframe 就点它，找不到才走兜底。

- ⚠️ **「没有 iframe」不等于「没有 widget」**。实测 Wiley 的拦截页
  （`cvId: '3'`、`cType: 'managed'`，`10.1002/lpor.200810005`）：
  `document.querySelectorAll('iframe').length` 是 **0**，而 `body.innerText`
  有 **271 字**的验证界面——widget 渲染出来了，主文档里却没有 iframe
- ❌ 旧的兜底**两处都挡住了自己**：分支条件要求 `iframe_count == 0`（页面上有任何
  无关 iframe 都会把这次尝试否掉），点击目标只有 `#captcha-box, .cf-turnstile`
  ——这两个在 cvId 3 的页面上**都不存在**。于是 `found=False`，**一次点击都没发出**，
  而且**一句话都不打印**，就这么空转到超时。现在两条都改了，且"找不到目标"会明确打印
- 四级目标，证据从强到弱：① CF 用过的容器（`#captcha-box`/`.cf-turnstile`/
  `[id^="cf-chl"]`/`#challenge-stage`/`#challenge-form`/`#turnstile-wrapper`）
  → ② **open shadow root 里的 iframe**（`querySelectorAll` 不穿透 shadow DOM，
  这正是"widget 存在而 iframes=0"的一种成因；closed root 任何脚本都够不到）
  → ③ **形状像 widget 的元素**（约 300×65，纯几何，不依赖语言和类名）
  → ④ 拦截页自己的 `.main-content`
- 点击点固定取 `(left + 32, 垂直居中)`——CF 目前所有尺寸的 widget，复选框都在那里
- ⚠️ 抓到的拦截页 HTML 多半是**服务器响应**，里面**看不到 widget**：widget 由
  bootstrap 脚本 `head.appendChild()` 进来的 `/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1`
  注入。判断手里那份是不是响应：看那个 `<script src=…orchestrate…>` 元素在不在，
  不在就是响应。要看 widget 得在 DevTools Console 里取 `document.documentElement.outerHTML`

### ❌ `_stealth_js` 已整个删除（`DP_STEALTH_JS` 一并去掉）

**起因**：用户在真实的 Cloudflare 拦截页上执行 `console.log(navigator.webdriver)`，
读到 **`undefined`** —— 而真人浏览器只会是 `false`。

**实测（纯 CDP 读，不注入任何补丁）**：我们自己启动的 Chrome **本来就报 `false`**：

```
value=false  type=boolean  实例自有属性=无
Navigator.prototype 上: function get webdriver() { [native code] }
```

- 📌 **不需要任何 flag**。`--disable-blink-features=AutomationControlled` 加不加，
  结果**完全相同** —— `webdriver=true` 只出现在带 `--enable-automation` 启动的
  Chrome 上，那是 **Playwright 自己启动浏览器**时加的；本仓是我们起 Chrome、
  Playwright 只用 CDP 连上来，所以从来就没有过这个问题
- ⚠️ 那个 `undefined` **完全是补丁自己造的**：它把一个正常的 `false` 改成真人不可能
  的取值，还在**实例**上新建 getter（原型上有、实例上也有，且实例那个是访问器）
- ❌ **并且 CLAUDE.md 此前记错了一条**：原文说「伪造 plugins 的分支永不执行，真实
  Chrome 报 5 个」。改完后在**有头 Playwright context** 里实测 **`plugins` 是 0**
  —— 那个分支**一直在执行**，往页面里注入 3 个假 plugin 对象。假 plugin 数组比
  `webdriver` 更好认（对象缺正确原型，一戳就破）。所以删它比原先以为的更值
- ✅ 删除后同一环境实测：`webdriver=false`（boolean、实例无自有属性、原型
  `[native code]`）、`permissions.query.toString()` 恢复成
  `function query() { [native code] }`、`languages=zh-CN,zh`

### profile 生命周期（`chrome_session.prepare_profile_dir`）

- **抓取 profile 永不复用**。每次开浏览器都是「先删再建」，两个实例（正文页的共享实例、
  PDF 的一次性实例）走的是同一个函数。每篇论文结束后
  `retire_headed_browser()` 会关掉浏览器，下一篇重来一遍 ——
  Chrome 对 user-data-dir 是独占锁，不关就替换不掉
- 重建时填什么，只看两个条件：

  | 条件 | 结果 |
  |---|---|
  | `FRESH_PROFILE=0` 且 `CHROME_PROFILE_SOURCE_DIR` 有效 | 从真实 profile 播种（Cookies + Local State + Preferences…），**并剔除反爬 cookie** |
  | `FRESH_PROFILE=1`，或源目录无效 | 空 profile（零登录态） |

  「有效」= 目录存在**且**含 `CHROME_PROFILE` 那个子目录；路径写错当作没有，
  不会静默产出一个没 cookie 的 profile
- ⚠️ **播种会连 bot manager 的案底一起带过去** —— 同一个 `Cookies` 文件里既有机构
  订阅态，也有 ShieldSquare/Radware 自己的信誉状态（`__uzm*`、`__ss*`、
  `.iop.org` 的 `uzmx`/`uzmxj`、以及 `validate.perfdrive.com` 整套）。于是每个
  「全新」的一次性 Chrome 都戴着**刚被标记过的那张身份证**出门。实测：
  `FRESH_PROFILE=1`（完全无 cookie）下 IOP 与 ScienceDirect 的 PDF 直接下得到，
  而播种过的 profile 撞 perfdrive
- 所以 `seed_profile()` 会在**副本**上删掉这些行（`_strip_bot_cookies`），
  留下订阅态、丢掉案底。`DP_SEED_DROP_BOT_COOKIES=0` 恢复旧的全有或全无行为，便于 A/B
- ⚠️ **匹配一定要用 `GLOB`，不能用 `LIKE`**：SQL 的 `LIKE` 里 `_` 是单字符通配符，
  所以 `name LIKE '__ss%'` 会连 `session`、`sessionid`、`passport_csrf_token` 一起扫中 ——
  本机实测会误删 15 条无关 cookie（含用户自己的 claude.ai 会话）。`GLOB` 把 `_` 当字面字符
- ⚠️ 只动副本，**真实 profile 全程只读**。改这段时请连同「剪完后删掉副本的
  `Cookies-journal`」一并保留：journal 若非空，删掉的行会被回滚回来，方案静默失效
- ⚠️ **光复制 Cookies 文件没用，还要能解密**。Linux 上 cookie 的密钥在系统钥匙环里
  （`Local State` 里**没有** `os_crypt.encrypted_key`），Chrome 接不到钥匙环就退回
  basic 密钥，解不开的条目直接丢弃。实测：播种过去的 1712 条只剩 22 条可见，
  247 条出版商 cookie **一条不剩**——有头无头都一样。加上
  `--password-store=gnome-libsecret` 后恢复到 1304 条、其中 184 条是出版商的。
  由 `chrome_password_store_args()` 统一提供：仅 Linux 且有 DBUS 会话时启用
  （否则 Chrome 可能卡在等钥匙环解锁），取值因机器而异（本机 `kwallet5` 无效），
  用 `CHROME_PASSWORD_STORE` 覆盖，置空则不加
- ⚠️ **安全护栏**：目标目录解析成真实 Chrome profile（日常 profile / 播种来源 /
  平台默认路径）时 `prepare_profile_dir` 直接抛 `ValueError`，`launch_chrome` 退回临时目录。
  没有这条，`CHROME_USER_DATA_DIR` 忘了设就会 `rm -rf` 掉用户自己的 Chrome 数据
- ⚠️ **端口上有残留 Chrome 时不能复用** —— 上次运行崩了/被杀，Chrome 还占着调试端口，
  新运行接管它就等于继承了那个被污染的 profile（现象：设了 `FRESH_PROFILE=1`
  却还是被 SPIE 拦）。所以先 `kill_chrome()` 再起
- 启动时 `sweep_stale_profiles()` 扫掉 `/tmp/chrome_fresh_*`、`/tmp/chrome_aux_*`、
  `/tmp/chrome_pdf_*`（旧名，改名前留下的目录仍归我们清）：
  只删**没有活进程持有**的（读 `/proc/*/cmdline` 的 `--user-data-dir=` 判断），
  并行跑的另一个批次不受影响
- SPIE 走 Imperva Incapsula（不是 Cloudflare，那套 Turnstile 点击逻辑对它无效），
  播种进来的 cookie 反而扣分，用 `FRESH_PROFILE=1`；代价是零登录态，
  靠机构订阅的文章别开
- **两个 Chrome 实例、两个端口 + 两个 profile 目录**，都能用环境变量改：

  | | 端口 | profile 目录 |
  |---|---|---|
  | 主实例（正文页） | `CHROME_DEBUG_PORT`（默认 9222） | `$CHROME_PROFILE_ROOT/main_dir` |
  | 辅助（一次性）实例 | `CHROME_AUX_DEBUG_PORT`（默认 9333，被占用自动顺延；旧名 `CHROME_PDF_DEBUG_PORT` 仍读） | `$CHROME_PROFILE_ROOT/aux_dir` |

  只有 `CHROME_PROFILE_ROOT` 一个旋钮，两个目录名固定、都是一次性的。
  默认 `/tmp/dp_profiles_xxxxxx` —— 后缀按启动时间+pid 做种随机生成，每个进程一个，
  所以并发跑多个批次天然隔离。⚠️ 显式指定 root 时，并发的批次要各给各的，
  否则两个 Chrome 抢同一个 profile 锁
- 运行结束时 `cleanup_profile_root()` 收尾（挂在 `_cleanup_chrome_launcher()` 上，
  正常退出/异常/SIGINT 都会走到）：**自动生成的 root 整个删掉**；用户显式指定的
  root 只清 `main_dir`/`aux_dir`（连同旧名 `pdf_dir`），root 本身保留（那是用户选的路径）；
  被活着的 Chrome 占用的目录跳过不动

### APS (`10.1103`, journals.aps.org)

正文页自己会取两个 JSON，**两个都在预载捕获里**，handler 直接读，不再重复请求：

| 端点 | 用途 | 落盘 |
|---|---|---|
| `/{prefix}/fulltext/{doi}` | 正文组件树 | `fulltext.json` |
| `/{prefix}/supplemental/{doi}` | 补充材料清单 | `supplemental.json` |

- ⚠️ **有头运行时 `setup_network_capture()` 那个 Playwright 监听器看不到它们** ——
  它是在 CDP 预载把页面加载完**之后**才连上的。所以 `captured['fulltext_data']`
  一直是空的，handler 每篇都要在页面内再 fetch 一次
- 补充材料的 JSON 很干净：`description`（一段 HTML 散文）+ `components[]`
  （每个文件一条 `id` / `link.url`）。**够用**，所以不再导航到 `/supplemental/{doi}`
  去爬 `a[data-id]` 和 innerText 正则。实测 `10.1103/PhysRevX.7.041003`：
  改用 JSON 后 `paper.md` **逐字节相同**（含那段 467 字符的说明）
- ⚠️ **「捕获里说没有」和「根本没有捕获」是两个答案**。前者（有捕获、其中没有
  supplemental 响应）判定该文章没有补充材料，**不再访问那个页面**；后者（无头运行、
  `DP_HARVEST_API=0`）必须照旧走页面。把两者混为一谈会让整批文章静默丢掉补充材料
- ⚠️ 两条路都要**照常落盘**（`_cache_json`）。复用捕获时若跳过写文件，产出目录就
  悄悄变得不可离线重建 —— 落盘文件是交接接口，不是缓存
- 落在 `/abstract/{doi}` 的文章**不会 XHR 正文**，那时捕获理应为空，
  `_fetch_fulltext_json()` 的页面内请求仍然是答案，不能删
- **正文页上已没有 `page.evaluate`**：元数据（`citation_*`、description、
  通讯作者邮箱）和 PDF 按钮都改从 `get_page_html()` 解析 —— 这些全是服务端渲染的
  （实测 `page_raw.html`：`citation_doi` 1、`mailto:` 2、`a.sm-primary-button` 10）。
  `extract_metadata_from_page(page, html)` 不给 html 时仍走原来的 JS，供脱离主流程的调用
- ⚠️ 用 `get_text()` 兜邮箱时**必须先删 `<script>/<style>`**：`innerText` 从来看不到它们，
  而 `get_text()` 看得到，统计脚本里的地址会盖过作者的
- ⚠️ APS 的 `fulltext_data` 是 dict 不是 str，所以主流程**不写 `page.html`**，
  目录里只有 `page_raw.html`

### Cambridge 把表格渲染成图片（`div.table-wrap-ada`）

❌ **实测（用户报告，`10.1017/hpl.2022.24`）**：md 里**没有任何表格**，表格图也没下 ——
而页面上有 `Table 1 Design structures of the coatings.` 和它的 `Note:`。

- 📌 **这篇全文 `<table>` 数量是 0**。Cambridge 把表格渲染成图片：
  `<div class="table-wrap-ada" id="tabN">` 里是 `div.caption`、
  `div.figure-thumb > img`（懒加载）、`div.table-wrap-foot`（Note）
- ❌ **原有的表格图逻辑认 `data-img-name="Table N."`，而这篇所有 `<img>` 该属性都是
  `None`** —— 于是既没进下载表，正文遍历也**静默跳过**：那个 `<section>` 有
  `figure-thumb` 但没有 `fig-ada`，落进插图分支后拿不到 `data-img-name` 就什么都不输出。
  判据改成**认容器**（`div.table-wrap-ada` / `div.table-wrap`）
- ⚠️ **Note 必须一起取**（`div.table-wrap-foot`）：它是符号定义
  （"H i and L i represent the high-n layer and low-n layer…"），丢了表里的数字就读不懂
- ⚠️ **编号接在插图后面**（`tab_8/9/10`），不按 id 的 `tab_1/2/3`：主流程按 key 末尾
  数字给下载文件编号，`tab_1` 会和 `fig_1` 抢同一个槽位。这是沿用原有约定
- ✅ 实测该篇：图片 7 → **10**，三张表格图落盘（51,347 / 11,458 / 6,262 字节，
  `magic` 均 `image/png`），md 里远程链接残留 **0**、Note 2 条
- 📌 **同一个修复在另一篇上多找出一张表**：`10.1017/hpl.2019.36` 从 43 图 / 1,683 行
  变成 44 图 / 1,687 行 —— 多出来的正是它一直没被抓到的
  `**Table 1.** LaserNet US facility capabilities.`（26,589 字节 gif）。
  ⚠️ 行数变了**不等于回归**，要看清多出来的是什么

### Cambridge (`10.1017`, cambridge.org)

- ⚠️ **同一个 URL 会答出两份不同的文档，而请求里看不出区别**。实测
  `10.1017/hpl.2019.36`，同样代码、同样 profile、相隔几分钟：
  **813,314 字符（无 `<div class="body">`，0 张图，md 913 行）** 与
  **2,259,335 字符（43 张图，md 1,683 行）**。逐项 diff 两次请求，只差 `referer`、
  `cache-control` 和 session cookie —— **URL、方法、`Accept-Language` 全都一样**
- ❌ **"切到 Français 才有正文"是个假线索**。那个切换按钮是
  `<span role="button" lang="fr">`，点它会**对同一 URL 再发一次 GET**，拿到的
  就是完整那份；实测四种 `Accept-Language`（en/fr/zh/不发）**全部返回完整正文**，
  切法语再切回英文，`div.body` 始终在、正文文本始终是 235,190 字符。
  所以真正起作用的是"**再要一次**"，不是语言
- 📌 **真正起作用的是 `cache-control: max-age=0`**，这也决定了 `_refetch_if_shell()`
  两层的顺序。真机实测同一篇：

  | 做法 | 请求头 `cache-control` |
  |---|---|
  | `goto(同一 URL)` | **无** —— 可能直接吃缓存 |
  | `reload()` | `max-age=0` |
  | 点 Français | `max-age=0` |

  所以 ① 先 `reload()`（与点击**同样的缓存语义**，但不依赖按钮存在）×2，
  ② 再点语言切换（用户在真机上验证过有效的那一个）。都失败就返回空，
  绝不拿壳冒充正文。拿到带 `<div class="body">` 的那份就替换 `_raw_server_html`，
  `page_raw.html` 随之被覆盖成实际用于提取的那一份
- ⚠️ **点击后只等 `networkidle` 会抓到 0 字节**。点击触发的导航是异步的，
  networkidle 可以被"当前这个页面"满足，于是监听器在新文档到达前就被摘掉了。
  必须用 `expect_navigation` 包住点击再补一段固定等待 —— 实测 0 字节 → 2,259,527 字节。
  ⚠️ 同步 emit 的假 page 永远测不出这条，它只在真机上暴露
- 📌 点击后那份文档是 `<html lang="fr">`，但**正文仍是英文原文**（实测作者名
  Danson×49、petawatt×272）。Cambridge 只翻译界面外壳，不翻译论文，
  所以拿它当 raw 不会产出一篇法语 md
- ⚠️ 判据是 `<div class="body">`（完整页恰好 1 个），正是
  `extract_article_text_from_html` 找不到它就返回空正文的那个容器。用词边界匹配，
  `class="bodycopy"` 不算
- **两层的产物都落盘**：壳存 `page_shell.html`（响应头那时已经没了，但壳与完整页的
  diff 仍值得留着，否则它会被好的那份直接覆盖掉），点击那层收到的文档存
  `page_fr.html`（**不论有没有正文都存** —— 它是"这一层到底返回了什么"的唯一证据）。
  实测落盘 2,262,839 字节、`Danson`×49，可与 `page_raw.html` 直接比对
- 📌 有用户在 Windows 上报告英文视图**确定性地**看不到正文、切法语就有 ——
  本机复现不出来（见上），怀疑是**所在节点缓存了那份壳**（法语那次点击带
  `cache-control: max-age=0`，等于强制回源）。要坐实得看那个文档请求的
  `cf-cache-status` / `age` / `x-cache` / `vary`

### IEEE (`10.1109`, ieeexplore.ieee.org)

- 页面是 Angular 客户端渲染，**正文不在 DOM 里**。全部内容走 REST 接口，键是数字
  **articleId**（不是 DOI），从最终 URL `/document/{articleId}/` 或页面里的
  `"articleId":"..."` 取
- 接口（都用页面内 `fetch()` 发，out-of-page 请求会被当作未授权）：
  - `/rest/document/{aid}/?logAccess=true` — 正文 XHTML，公式是
    `<tex-math notation="LaTeX">` 原文，无需 MathJax 还原
  - `/rest/document/{aid}/references`、`/multimedia`、`/footnotes`
- 书目元数据来自 landing page 里的 `xplGlobal.document.metadata` JSON
  （authors + affiliation、abstract、keywords、pdfUrl / pdfPath、supplementGroup）
- 响应会缓存到 `html/`：`rest.html`、`references.json`、`multimedia.json`、
  `footnotes.json`，可离线重跑渲染
- **PDF 走通用取数阶梯，和别的出版商一样**：`get_pdf_url()` 返回
  `https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={aid}` ——
  查看器自己调的那个接口，直接回 PDF 字节，所以导航到它就能下载
- ⚠️ **`pdfPath` / `pdfUrl` 只能兜底，绝不能排在前面**：`pdfPath` 的 `/iel7/...pdf`
  会重定向到 `stamp.jsp`，而 stamp 页只是个查看器，要人手点「open」才触发下载 ——
  浏览器 download 事件永远不会触发，整个重试预算白烧。IEEE 早先之所以需要一条自己的
  PDF 下载路径（`download_pdf_via_page()` 页面内 fetch），根源就是这个返回顺序；
  现在那条路径已删除，把顺序改回去就会把它招回来
- ⚠️ **注意与 entitlement 规则的相互作用**：IEEE 把权限绑在**渲染文章的那个会话**上，
  而有头阶梯的第一层是**一次性 Chrome，完全没有正文页会话**。订阅内容多半要落到第二层
  `tab`（在已打开文章的浏览器里开新标签页，会话相同）才拿得到。整批跑 IEEE 可以直接
  `DP_FETCH_PDF=tab` 跳过第一层，省掉每篇一次的空跑
- `xplGlobal` 里的 `title` / `abstract` / `keywords` **是 HTML 片段不是纯文本**，
  摘要里常有 `<inline-formula><tex-math>`，必须走 `field_md()`（同一套公式管道）
- `supplementGroup` 是**仓库分组的列表**，条目带的是外部 DOI（IEEE DataPort）而不是
  文件路径 —— 对应页面上的 "Code & Datasets"。这些只列进 md 的 `## Code & Datasets`
  段，**不能**塞进 `supplemental_urls`，否则下载器会把 landing page 当数据集存下来。
  带 `filePath` 的条目才当真正的补充材料下载。
  ⚠️ 那个字段虽然叫 `doi`，**但不一定是 DOI** —— 它是托管仓库自己的标识符，别的文章的
  data 链接也可能长得完全不像 DOI。所以 `_supplement_link()` 只在值真的匹配 DOI 格式时
  才拼 `doi.org`，本身就是 URL 的直接用，其余情况不给链接（把标识符原样列出）。
  这段**只输出链接**，不单独打印 DOI 行 —— Crossref 核验不了这类标识符
- 陷阱：正文里 `<p>` **可以嵌套整个 `<ul>`**（见 `_render_paragraph`）；
  `\$` 在公式内部是字面美元符号，只能剥最外层定界符；references 的文本是
  UTF-8 被当 cp1252 的乱码，需 `_fix_mojibake`

### ACM (`10.1145`, dl.acm.org)

- **仅抓 abstract**。ACM 全文对未登录用户 gated，正文/图片/补充材料抓不到
- 输出的 `paper.md` 保证有 `## Abstract` 段，其余章节尽力而为（Index Terms、References 等在 landing page 上能看到的会被 h2 walker 顺手带出来，但不保证完整）
- PDF 链接固定构造为 `https://dl.acm.org/doi/pdf/{doi}`（下载可能仍 401，走标准 retry/skip）
- **必须有头** — ACM 对 headless Chromium 有 Cloudflare 硬拦截。不要把 `'acm'` 加进 `HEADLESS_ACCESSIBLE_PUBLISHERS`
- 图片 / 补充材料 handler 里保留接口 stub，将来想抓时不用改提取契约

## 参考

详细文档见 `docs/CLAUDE.md`，工作流说明见 `docs/README.md`，IOP 提取逻辑见 `publisher/iop.md`。

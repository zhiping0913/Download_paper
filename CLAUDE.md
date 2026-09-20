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
python batch_process.py --file dois.txt                     # 批量
```

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

### 一份 HTML 只落一次

- `page_raw.html` = 原始 HTTP 响应；`page.html` = 渲染后 DOM，**只在与原始响应不同时才写**
- ⚠️ handler 大多把原始响应原样交回当 `fulltext_data`，于是两个名字装同样的字节 ——
  实测存档 **57 篇里 30 篇** `page.html` 与 `page_raw.html` 逐字节相同。现在"有 page.html"
  才真的意味着存在第二个视图
- ❌ `headless_initial.html` **已取消**：它写的是 `raw or rendered`，也就是上面两个之中
  的一个，实测 **5/5** 与 `page_raw.html` 逐字节相同
- ⚠️ 正文页的 HTML **只由主流程落盘**。handler 侧剩下的写入都不是正文页：
  Wiley / Optica 的 `source.html` 仅在 view-source 救援触发时写（写了就说明预载捕获落空，
  是个要查的信号）；Cambridge 的 `page_shell.html` / `page_fr.html` 是重取阶梯的证据；
  IOP / APS 写的是**补充材料页**

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

### 谁还需要 `block_mathjax`（`RAW_HTML_PUBLISHERS`）

`block_mathjax` 用 `page.route` + `route.abort()` 掐掉 MathJax 脚本请求，是**页面内干预**：
被中止的子资源请求在页面里看得见。读原始响应的 handler 不需要它——它们的公式源从来
就没进过 DOM。目标是逐步摘干净，而不是全局开关一刀切。

- 判据只有一处：`core/utilities.RAW_HTML_PUBLISHERS` + `should_block_mathjax(publisher)`。
  **未知/空一律照旧拦截** —— 错误的跳过会静默丢掉 LaTeX 源，错误的拦截只多一次被中止的
  请求，两种代价不对称
- 当前成员：`iop`、`sciencedirect`、`aps`、`optica`、`cambridge`、`acs`、`wiley`、`ieee`。
  **每一个都是 A/B 实测进来的**（同一篇跑两遍、diff `paper.md`），不是推理出来的：

  | | 样本 | 结果 |
  |---|---|---|
  | sciencedirect | `10.1016/j.rinp.2021.104097` | 逐字节相同（41,616 B）|
  | aps | `10.1103/PhysRevA.98.043407` | 逐字节相同 |
  | optica | `10.1364/OE.444043` | 逐字节相同 |
  | cambridge | `10.1017/hpl.2018.33`（latex 藏在 svg 后面）| 逐字节相同，两边都是 46 行公式 |
  | acs | `10.1021/acs.nanolett.8b05070` | 逐字节相同，17 行公式 |
  | wiley | `10.1002/lpor.202401986` | 逐字节相同，36 行公式 |
  | ieee | `10.1109/TPS.2010.2064310` | 逐字节相同，41 行公式 |
  | spie | `10.1117/12.2038680`(2014会议) / `10.1117/1.oe.62.8.086102`(2023) / `10.1117/1.OE.64.11.115106`(2025) | 三篇全部逐字节相同，0/27/57 行公式 |

- ⚠️ **测 SPIE 每次访问之间隔 5 分钟**：它是本仓最严的一家，连打是它的 bot manager
  最会扣分的形状。另外 2014 与 2023 那两篇还与 **captured_data 里的旧存档逐字节相同**
  （123 行 / 165 行），说明今天这一串改动对产出零影响
- 📌 **"页面自己 fetch 正文"不是新版页面才有的**：2014 年的会议论文集同样命中，
  五次访问全部复用捕获、零次主动 POST

- ⚠️ **ACS 和 Wiley 必须先换数据来源才够格**，顺序反了就是直接丢公式：
  - **ACS** 原来只读 `page.content()`，公式靠 `mjx-assistive-mml` 捞 —— 那是 **MathJax 自己
    的产物**。先摘拦截的话，41 个 `<math>` 会一起消失。现在读 `get_page_html()`；实测
    原始响应与渲染后 DOM 对所有提取器结果**完全一致**（4 图、33 参考文献、正文 24,755 字符、
    摘要 1,054 字符）
  - **Wiley** 原来每篇发一次页面内 view-source `fetch()`。实测 `10.1002/lpor.202401986`：
    预载捕获与那次 fetch **公式源都是 121、x-tex 注解都是 121**，逐行 diff 164 行**全是
    每次请求都不同的 id**，提取器结果一致（4 图、68 参考文献、正文 39,388 字符）。
    现在 view-source 降为救援，日志里那一行出现 **0 次**
  - ⚠️ 救援不触发时**不再写 `source.html`** —— 它和主流程落的 `page_raw.html` 是同一份
    字节，两个名字装一样的内容。`source.html` 现在只有一个含义：**这次运行不得不自己
    重取源码**
- ⚠️ **给 handler 补 `PUBLISHER` 常量时别插进 docstring 同一行**：
  `PUBLISHER = 'acs'    """doc"""` **能编译**（相邻字符串隐式拼接），结果是
  `PUBLISHER = 'acsFull-text handler for ACS Publications.'`、`__doc__ = None`，
  而 `should_block_mathjax()` 对这个垃圾值返回 True —— 表现为"**什么都没变**"，
  六个文件全中招且 `py_compile` 全绿

  `DP_RAW_HTML_PUBLISHERS=<token>` 只为这个 A/B 存在；**过了的要写进常量，不是靠环境变量长期配置**
- ⚠️ **A/B 只走了"原始响应拿到了"那条路**，对回落路径什么都没证明 —— 而拦截真正保护的正是
  回落。能接受这个残差，是因为加入这个集合是**一换一**：失去拦截，换来 `get_page_html()`
  在捕获落空时先走 view-source 重取、并把降级**打印出来**
- ⚠️ **Cambridge 是先把回落改掉才加进来的**。它原本是 `fulltext_html = raw_html or rendered_html`，
  而渲染后的 DOM **一定**有 `<div class="body">`，所以重取阶梯看一眼就说"有正文"、
  一次都不会触发 —— 最该重取的那种情况恰恰是唯一不重取的。现在"没捕到原始响应"和
  "拿到的是壳"走同一条阶梯，渲染后 DOM 只作**声明过的**最后手段
- ⚠️ **测法本身也踩过坑**：用 `ls -dt captured_data/*/ | head -1` 取"最新输出目录"是错的 ——
  目录 mtime 只在新建/删除文件时更新，覆盖写已有文件不会动它，于是旧目录反而显得更新。
  实测四个 A/B 产物 md5 全同（都是另一篇的 md），差点据此报出"通过"。要从日志里
  `📝 Markdown 文件:` 那行取程序自己打印的路径
- token 来自两处，但决策点仍是同一个：主流程用它已导入的
  `orchestrator.detect_publisher_from_url`（**不是** `core.utilities` 里那个同名的旧副本
  —— 后者只认 7 家，大多数会答 `unknown`）；`wildcard` 不能 import orchestrator
  （handler 反过来 import 它，会成环），所以改由 handler 自报 `PublisherHandler.PUBLISHER`
- ⚠️ **摘掉拦截会连带削弱一条回落路径**：原始响应没捕到时 `get_page_html()` 会退到
  `page.content()`，而那份 DOM 现在**没有保护**。所以 `RAW_HTML_PUBLISHERS` 的 handler
  在回落前先走 `fetch_view_source_html()` 重取，并把这次降级**打印出来**——
  静默劣化比失败更难查
- 📌 **预载阶段现在自己捕获响应，所以这条回落基本不再触发**。见下面「预载期间的
  响应捕获」。它仍然留着，作为捕获落空时的保险

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

### 反检测补丁 `_stealth_js`（`DP_STEALTH_JS`，默认开）

注入 headed context 每个页面。**实测下来它基本无效，而真正生效的部分可能适得其反** ——
下面每一条都是在真实 Chrome 二进制上量出来的，不是推断：

| 分支 | 实测 |
|---|---|
| 伪造 `plugins` | **永不执行** —— 条件是 `.length === 0`，真实 Chrome 报 5 个 |
| 伪造 `languages` | **永不执行** —— 同上，真实 Chrome 报 2 种（`en-US,en`） |
| `delete window.cdc_…` | **空操作** —— `cdc_` 是 ChromeDriver 的痕迹，Playwright/CDP 不注入 |
| 改写 `navigator.webdriver` | 生效，但造出真人**不可能**的状态 |
| 改写 `permissions.query` | 生效，但同样留下痕迹 |

- ⚠️ **它把「可疑」换成了「不可能」**。未注入时 `navigator.webdriver === true` —— 诚实、
  常见的一个信号；注入后变成 `undefined`，而真实浏览器**只会是 `false`**。前者说明
  「这是自动化」，后者说明「这是**在撒谎的**自动化」，后者罕见得多，因而更好认
- ⚠️ **补丁本身比它掩盖的破绽更显眼**：真属性是 `Navigator.prototype` 上的**数据属性**，
  补丁却在 `navigator` **实例**上新建了 **getter** —— 于是原型上有、实例上也有，
  且实例那个是访问器，真人那里根本不存在这种组合。一行
  `Object.getOwnPropertyDescriptor(navigator, 'webdriver')` 即可看出
- ⚠️ `permissions.query.toString()` 从 `[native code]` 变成箭头函数源码，且从原型挪到
  实例自有属性 —— 这是最经典的一条检测
- **关掉不损失任何能力**：本程序不读 `navigator.webdriver` / `plugins` /
  `permissions.query`。唯一可能沾边的「靠 PDF 插件决定内嵌还是下载」，我们是用
  profile 的 `always_open_pdf_externally` 强制下载，不依赖插件存在
- 默认仍为**开**（维持现状），`DP_STEALTH_JS=0` 关闭。**它是否真的影响拦截率尚无证据**，
  开关就是为了做这个 A/B —— 有结论前不要改默认

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

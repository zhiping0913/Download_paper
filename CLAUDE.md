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
python complete_paper_extraction.py "<DOI>"
python complete_paper_extraction.py "<DOI>" --force-headed  # 有头模式
python complete_paper_extraction.py "<DOI>" --pdf-only      # 只下 PDF，不生成 md
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
- **ScienceDirect**：读 `div.content-meta-access-label` 的文本；`Abstract only`、
  `No access`、**或该元素不存在**都判无权限
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

- landing page 只有元数据（`citation_*` + `ld+json`），正文要 **POST**
  `/api/{family}/article/fulltexthtml`，body `{"urlId": "<doi>"}`，
  用页面内 `fetch()` 发（DOI 不分大小写）。响应存 `fulltexthtml.json`
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

- **有头**：先用一次性 Chrome（`chrome_session.open_url_in_fresh_chrome`），
  失败再回退 Playwright 导航 —— 共享浏览器自打开正文页起就被 Playwright 接管，
  带自动化指纹；而且正文域名过的 Cloudflare 对 PDF 域名（如
  `pdf.sciencedirectassets.com`）不算数
- **无头**：反过来，先用手头这个无头浏览器下，失败了才起一次性 Chrome ——
  能无头访问到的出版商本来就没在拦我们，每篇都弹一个窗口就失去无头的意义了
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
| `tab` | 在已打开论文的浏览器里开新标签页 |
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
- 而调用方给的 `default` 是**完整顺序**：有头 PDF 默认 `('fresh','tab','referer')`，
  无头默认 `('tab','fresh','referer')`。有头之所以把 `fresh` 排在 `tab` 前面是有实测
  支撑的（见上面「PDF 下载顺序」），而截断表达不了这种**重排**，所以 `fetch_ladder()`
  才要把「截断」和「默认顺序」分成两种语义
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

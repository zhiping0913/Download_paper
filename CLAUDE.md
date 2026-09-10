# Download_paper 项目指南

本文档自动加载。详细版本见 `docs/CLAUDE.md`。

## 核心原则

**始终通过浏览器访问论文页面**，使用 `complete_paper_extraction.py`，不使用 `curl`/`wget`/`requests` 直接 HTTP 请求期刊网站。

**所有元素使用同一套公式转换管道** — 正文段落、图注、表格单元格等所有包含潜在 LaTeX 公式的元素，都必须通过 `_convert_iop_paragraph_to_md()` (IOP) 或对应的公式转换函数处理，不能直接使用 `get_text()` 提取纯文本。

## 快速命令

```bash
source /home/zhiping/research-env/bin/activate
cd /home/zhiping/Projects/Download_paper
python complete_paper_extraction.py "<DOI>"
python complete_paper_extraction.py "<DOI>" --force-headed  # 有头模式
python batch_process.py --file dois.txt                     # 批量
```

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
  `/doi/pdfdirect/{doi}?download=true`
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
- 补充材料没有独立浏览器路径，一直用传进去的 page/context，所以无头时本来就是无头下载
- `DP_PDF_FRESH_CHROME=0` 两种模式下都彻底禁用一次性 Chrome
### profile 生命周期（`chrome_session.prepare_profile_dir`）

- **抓取 profile 永不复用**。每次开浏览器都是「先删再建」，两个实例（正文页的共享实例、
  PDF 的一次性实例）走的是同一个函数。每篇论文结束后
  `retire_headed_browser()` 会关掉浏览器，下一篇重来一遍 ——
  Chrome 对 user-data-dir 是独占锁，不关就替换不掉
- 重建时填什么，只看两个条件：

  | 条件 | 结果 |
  |---|---|
  | `FRESH_PROFILE=0` 且 `CHROME_PROFILE_SOURCE_DIR` 有效 | 从真实 profile 播种（Cookies + Local State + Preferences…） |
  | `FRESH_PROFILE=1`，或源目录无效 | 空 profile（零登录态） |

  「有效」= 目录存在**且**含 `CHROME_PROFILE` 那个子目录；路径写错当作没有，
  不会静默产出一个没 cookie 的 profile
- ⚠️ **安全护栏**：目标目录解析成真实 Chrome profile（日常 profile / 播种来源 /
  平台默认路径）时 `prepare_profile_dir` 直接抛 `ValueError`，`launch_chrome` 退回临时目录。
  没有这条，`CHROME_USER_DATA_DIR` 忘了设就会 `rm -rf` 掉用户自己的 Chrome 数据
- ⚠️ **端口上有残留 Chrome 时不能复用** —— 上次运行崩了/被杀，Chrome 还占着调试端口，
  新运行接管它就等于继承了那个被污染的 profile（现象：设了 `FRESH_PROFILE=1`
  却还是被 SPIE 拦）。所以先 `kill_chrome()` 再起
- 启动时 `sweep_stale_profiles()` 扫掉 `/tmp/chrome_fresh_*`、`/tmp/chrome_pdf_*`：
  只删**没有活进程持有**的（读 `/proc/*/cmdline` 的 `--user-data-dir=` 判断），
  并行跑的另一个批次不受影响
- SPIE 走 Imperva Incapsula（不是 Cloudflare，那套 Turnstile 点击逻辑对它无效），
  播种进来的 cookie 反而扣分，用 `FRESH_PROFILE=1`；代价是零登录态，
  靠机构订阅的文章别开
- **两个 Chrome 实例、两个端口 + 两个 profile 目录**，都能用环境变量改：

  | | 端口 | profile 目录 |
  |---|---|---|
  | 主实例（正文页） | `CHROME_DEBUG_PORT`（默认 9222） | `$CHROME_PROFILE_ROOT/main_dir` |
  | 一次性实例（PDF） | `CHROME_PDF_DEBUG_PORT`（默认 9333，被占用自动顺延） | `$CHROME_PROFILE_ROOT/pdf_dir` |

  只有 `CHROME_PROFILE_ROOT` 一个旋钮，两个目录名固定、都是一次性的。
  默认 `/tmp/dp_profiles_xxxxxx` —— 后缀按启动时间做种随机生成，每个进程一个，
  所以并发跑多个批次天然隔离。⚠️ 显式指定 root 时，并发的批次要各给各的，
  否则两个 Chrome 抢同一个 profile 锁

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
- **PDF 不能靠导航下载**：`pdfPath` 的 `/iel7/...pdf` 会重定向到 `stamp.jsp`，
  而 stamp 页只是个查看器，要人手点「open」才触发下载 —— 浏览器 download 事件
  永远不会触发。handler 用 `download_pdf_via_page()` 页面内 `fetch()` 直接取字节，
  首选 `https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={aid}`
  （查看器自己调的那个接口，直接回 PDF），失败再回退 metadata 里的两个链接并跟随
  查看器页里的 `<iframe src>`
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

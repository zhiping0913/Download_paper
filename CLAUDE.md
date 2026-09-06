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
  带 `filePath` 的条目才当真正的补充材料下载。这段**只输出链接**，不单独打印 DOI ——
  这类是 DataCite 的数据集 DOI，Crossref 核验不了，而且 DOI 本身已经在链接里了
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

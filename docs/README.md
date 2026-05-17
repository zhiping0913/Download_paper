# Download_paper 工作流说明

本文档说明 `complete_paper_extraction.py` 的主工作流、各个 publisher 的接口契约，以及新增 publisher 时需要遵守的边界。

## 主工作流

入口函数是：

```python
complete_extraction_workflow(doi, output_file=None, force_headed=False)
```

主流程只负责统一调度，不直接处理具体出版商的网页结构。它的职责是：

1. 准备输出目录。
   默认输出到项目下的 `captured_data`。每篇论文会先建立 DOI 缓存目录：
   `output_dir / doi.replace("/", "_")`。

2. 构造 DOI 跳转 URL。

   ```text
   https://doi.org/{doi}
   ```

3. 根据 `force_headed` 决定浏览器路径。
   - `force_headed=True`：跳过无头预检，直接使用有头 Chrome。
   - `force_headed=False`：先使用无头 Chromium 访问 DOI，根据最终跳转 URL 判断 publisher。

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

## Publisher 判断

当前 `detect_publisher_from_url()` 的主要规则：

- `10.1038`、`nature.com`、`springer.com`、`s41...` -> `nature`
- `10.1103`、`journals.aps.org`、`prl/pre/pra` -> `aps`
- `10.1063`、`pubs.aip.org`、`aip.scitation.org` -> `aip`
- `10.1088`、`iopscience.iop.org` -> `iop`
- `10.1017`、`cambridge.org` -> `cambridge`
- `sciencedirect.com`、`10.1016` -> `nature` (Elsevier 回退)
- `epj-conferences.org`、`10.1051` -> `nature` (EDP Sciences 回退)
- `arxiv.org` -> `arxiv`
- 其他 -> `unknown`

handler 创建由 `get_publisher_handler()` 负责：

- `nature` -> `NatureHandler`
- `aps` -> `APSHandler`
- `aip` -> `AIPHandler`
- `iop` -> `IOPHandler`
- `cambridge` -> `CambridgeHandler`
- `arxiv` -> 带 `journal_prefix="arxiv"` 的 `APSHandler`
- `unknown` -> 默认 `APSHandler`

## 浏览器路径

当前主流程有三条路径。

### 1. 无头直连路径

如果 `force_headed=False`，主流程会先启动无头 Chromium 访问 DOI，并保存：

```text
captured_data/{doi}/headless_initial.html
captured_data/{doi}/page.html
```

Phase 0 会先访问 DOI resolver URL。如果 DOI 可以直接识别为 Nature，且 DOI resolver 访问失败，会继续尝试 Nature 文章直连 URL：

```text
https://www.nature.com/articles/{doi_suffix}
```

如果最终 publisher 在：

```python
HEADLESS_ACCESSIBLE_PUBLISHERS = ["nature", "aip", "cambridge"]
```

中，主流程直接把这个无头 `page` 传给对应 handler，然后进入统一处理阶段。

无头预检可以使用持久化登录态文件：

```text
.auth/headless_storage_state.json
```

正常远程运行时，Phase 0 只读取这个文件，不会自动连接 `127.0.0.1:9222`。如果需要从真实 Chrome 刷新该文件，可以在方便使用本机 Chrome/CDP 时显式运行：

```bash
python complete_paper_extraction.py --doi <doi> --refresh-headless-auth
```

`.auth/` 不应提交到 git。

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
2. 如果没有，通过 `chrome_launcher.py` 启动。
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

| 路径 | 提取阶段 | 下载阶段 | 说明 |
|------|---------|---------|------|
| `force_headed=True` | headed CDP | headed（复用 context） | 用户显式要求或有头 publisher |
| publisher 在 `HEADLESS_ACCESSIBLE_PUBLISHERS` 内 | headless（共用 precheck page） | headless（新建） | 如 Nature、AIP、Cambridge |
| publisher **不在** `HEADLESS_ACCESSIBLE_PUBLISHERS` 内 | headed CDP | headed（复用 context，同一 `force_headed`） | 如 APS、IOP |

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
}
```

主流程不关心 `fulltext_data` 是 HTML 还是 JSON。APS 当前返回 JSON，Nature 当前返回 HTML。具体转换逻辑由各自的 `convert_to_markdown()` 实现。

## 统一处理阶段

无论 publisher 是 APS 还是 Nature，只要进入 `process_with_handler()`，后续流程一致：

1. 调用 `handler.extract_all(captured=captured_data)`。
2. 取出 `metadata`、`links`、`fulltext_data`。
3. 使用 Semantic Scholar 数据补全缺失的 `year/title`。
4. 创建最终论文目录：

   ```text
   {year}--{title}/
   ```

5. 调用 `_download_all_resources()` 下载资源。
6. 调用 `handler.convert_to_markdown()` 生成 Markdown。
7. 保存 `.md`。
8. 调用 `save_metadata_json()` 保存元数据 JSON。
9. 打印统计信息。

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
- Markdown 正文转换由 `json_to_md_converter.convert_json_data_to_markdown()` 完成。

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

## wildcard.py — 共享提取模块

`publisher/wildcard.py` 提供跨 publisher 共享的提取函数，被 NatureHandler、IOPHandler、AIPHandler、CambridgeHandler 共同引用：

| 函数 | 用途 |
|------|------|
| `find_generic_article_body(soup)` | CSS 选择器级联查找文章正文容器 |
| `extract_abstract_with_fallbacks(soup)` | 多策略摘要提取 |
| `generate_bibtex_key(authors, year, title)` | 生成 BibTeX 引用 key |
| `format_as_bibtex(parts)` | 解析后的引用 dict → `@article{key, ...}` 字符串 |
| `parse_citation_reference_string(ref_str)` | 解析分号分隔的 `citation_reference` meta 标签 |
| `prepare_mathjax_html_fragment(html)` | MathJax CHTML → placeholder 折叠 |
| `convert_html_fragment_to_markdown(html)` | HTML → Markdown + 公式还原 |
| `convert_mathml(mathml_str)` | MathML → LaTeX（通过 pandoc） |

新增 publisher 时应优先复用 wildcard 中的函数，减少重复代码。

## 顶层 API 文件

### download_paper.py

同步接口，可被其他 Python 程序 import 调用：

```python
from download_paper import download_paper
md_path = download_paper("10.1103/PhysRevLett.125.015001")
```

也支持 CLI：`python download_paper.py "10.1103/PhysRevLett.125.015001" --output ~/papers`

### batch_process.py

批量 DOI 处理器，支持从文件或命令行读取多个 DOI：

```bash
python batch_process.py --file dois.txt
python batch_process.py --dois "10.1103/..." "10.1063/..."
```

内置随机睡眠防拉黑机制（通过 `config.py` 的 `BATCH_SLEEP_*` 配置），可用 `--no-sleep` 临时禁用。

## 反爬虫检测

`is_bot_challenge_page(url, html)` 在 Phase 0 无头预检阶段检测页面是否为反爬虫拦截页面（Radware Bot Manager、Cloudflare Turnstile、Distil Networks 等）。检测到拦截后自动回退到有头 Chrome 路径，并设置 `headless_blocked` 标志防止无头 handler 路径被错误触发。

## 参考文献格式化

`core/utilities.py` 中的 `format_references_as_bibtex(references)` 将参考文献列表转为 BibTeX 代码块：

1. 对每条参考文献尝试提取 DOI
2. 通过 `fetch_semanticscholar()` 查询 Semantic Scholar 获取结构化元数据
3. 构建 `@article{key, author={...}, title={...}, ...}` 条目
4. Fallback 到 `wildcard.parse_citation_reference_string()` 解析
5. 所有条目包裹在 ` ```bibtex ` 代码块中

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

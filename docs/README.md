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
- `arxiv.org` -> `arxiv`
- 其他 -> `unknown`

handler 创建由 `get_publisher_handler()` 负责：

- `nature` -> `NatureHandler`
- `aps` -> `APSHandler`
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
HEADLESS_ACCESSIBLE_PUBLISHERS = ["nature"]
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

APS 当前只能通过这条有头路径访问。

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

## 新增 Publisher 的接入方式

新增 publisher 时按这个顺序做：

1. 新建 `publisher/{name}.py`，继承 `PublisherHandler`。
2. 实现 `extract_all()` 和 `convert_to_markdown()`。
3. 如果需要缓存网页或 API 响应，实现 `setup_network_capture()`。
4. 在 `publisher/orchestrator.py` 的 `detect_publisher_from_url()` 中增加 DOI/URL 识别规则。
5. 在 `get_publisher_handler()` 中返回新的 handler。
6. 如果该 publisher 可以无头完整访问，把名字加入 `HEADLESS_ACCESSIBLE_PUBLISHERS`。
7. 保持 `extract_all()` 返回统一结构，避免修改主流程。

核心原则：`complete_paper_extraction.py` 保持 publisher 不敏感；具体网页结构、API 响应、HTML 转 Markdown 的逻辑都放在各自的 `PublisherHandler` 中。

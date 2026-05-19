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

| DOI 前缀 | Handler | 浏览器 |
|---|---|---|
| `10.1038` | NatureHandler | 无头 |
| `10.1103` | APSHandler | 有头 |
| `10.1063` | AIPHandler | 无头 |
| `10.1088` | IOPHandler | 有头 |
| `10.1017` | CambridgeHandler | 无头 |

## 参考

详细文档见 `docs/CLAUDE.md`，工作流说明见 `docs/README.md`，IOP 提取逻辑见 `publisher/iop.md`。

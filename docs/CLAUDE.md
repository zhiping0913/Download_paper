# Download_paper 项目指南

**工作目录**: `/home/zhiping/Projects/Download_paper`  
**用户角色**: 科研工作者 (激光等离子体相互作用研究)  
**日期**: 2026-05-18

---

## 项目目标

通过有头/无头浏览器直接访问论文网页，提取格式正确的论文全文并转为 AI 友好的 Markdown 文件，同时下载 PDF、高清原图和补充材料。

核心原则：**始终通过浏览器访问**，不使用 `curl`/`wget` 等直接 HTTP 请求，以免被期刊网站的反爬机制限制访问。

---

## 快速使用

### 单篇论文

```bash
source /home/zhiping/research-env/bin/activate
cd /home/zhiping/Projects/Download_paper
python complete_paper_extraction.py "10.1103/PhysRevLett.109.245005"
```

需要强制有头浏览器（某些 publisher 需要登录态才能获取补充材料）：
```bash
python complete_paper_extraction.py "10.1088/1361-6463/ae36b8" --force-headed
```

### 批量处理

```bash
python batch_process.py --file dois.txt
python batch_process.py --dois "10.1103/..." "10.1063/..."
```

---

## 项目结构

### 入口文件

| 文件 | 用途 |
|---|---|
| `complete_paper_extraction.py` | 主入口，编排完整提取流程（浏览器→提取→下载→保存） |
| `batch_process.py` | 批量 DOI 处理器 |

### 配置与基础设施

| 文件 | 用途 |
|---|---|
| `config.py` | Chrome 路径、输出目录、批处理延迟、`SAVE_WITHOUT_REFERENCES` 等全局配置 |
| `chrome_launcher.py` | 跨平台 Chrome 启动/关闭，从 `config.py` 读取路径 |
| `html_to_md_converter.py` | HTML → Markdown 转换工具函数（pandoc、LaTeX清理、MathML→LaTeX），可被任意 publisher handler 独立调用 |

### `html_to_md_converter.py` 可复用的关键函数

这些函数不依赖任何 publisher 上下文，可独立调用：

| 函数 | 用途 | 调用方式示例 |
|---|---|---|
| `convert_html_to_markdown(html_content)` | HTML → Markdown（通过 pandoc），自动处理 MathJax 公式和引用编号 | 被所有 publisher handler 间接使用 |
| `cleanup_markdown(md_content)` | 清理 Markdown 中的不兼容 LaTeX 命令（`\mspace`、`\ensuremath`、`\slash`）、HTML 实体、转义括号 | `from html_to_md_converter import cleanup_markdown` |
| `remove_newlines_in_paragraph(text)` | 清除段落内换行但保留数学环境（`$$...$$`）结构 | 用于段落合并 |
| `mathml_to_latex_pandoc(mathml_html)` | MathML → LaTeX 转换（通过 pandoc），返回 LaTeX 字符串 | `from html_to_md_converter import mathml_to_latex_pandoc` |

### Publisher 处理器

| 文件 | 处理范围 | 浏览器模式 |
|---|---|---|
| `publisher/nature.py` | Nature / Springer / Elsevier / EDP Sciences | 无头 |
| `publisher/aps.py` | APS (PRL/PRA/PRX 等) | 有头 |
| `publisher/aip.py` | AIP Publishing | 无头 |
| `publisher/iop.py` | IOP Science | 有头 |
| `publisher/cambridge.py` | Cambridge University Press | 无头 |
| `publisher/oup.py` | Oxford University Press | 无头 |

各 handler 继承 `publisher/base.py` 中的 `PublisherHandler` 抽象基类，实现统一的 `extract_all()` 和 `convert_to_markdown()` 接口。

### 共享工具

| 文件 | 用途 |
|---|---|
| `publisher/orchestrator.py` | DOI/URL → publisher 类型 → handler 工厂 |
| `publisher/wildcard.py` | 跨 publisher 共享函数：正文查找、摘要提取、公式转换、BibTeX 格式化 |
| `publisher/__init__.py` | 模块导出 |
| `core/utilities.py` | Semantic Scholar 查询、BibTeX 格式化 |

---

## 工作流（complete_paper_extraction.py）

1. **准备输出目录** — `captured_data/{doi_safe}/`
2. **启动浏览器** — 根据 publisher 选择无头或有头 Chrome，使用 `config.py` 中配置的持久化用户数据目录（复用已登录态）
3. **访问 DOI** — `https://doi.org/{doi}`
4. **识别 publisher** — 根据最终 URL / DOI 前缀确定 handler 类型
5. **提取元数据** — 从 `<meta>` 标签和 DOM 中提取标题、作者、期刊、摘要等
6. **提取正文** — 遍历文章 HTML，通过公式转换管道保留 LaTeX 数学公式
7. **提取参考文献** — 格式化为 BibTeX 代码块
8. **提取脚注** — 在参考文献前展示（IOP 支持）
9. **提取图片链接** — 优先高分辨率版本，传给主流程下载
10. **提取补充材料** — 从文章页面或专用 `/data` 端点发现下载链接
11. **下载 PDF** — 从 `citation_pdf_url` 或 handler 提供的链接下载
12. **生成 Markdown** — 调用 handler 的 `convert_to_markdown()`
13. **保存** — Markdown、PDF、图片、补充材料、元数据 JSON

---

## 输出目录结构

```
captured_data/
└─ {YYYY}--{Title}/
   ├─ paper.md                    # AI 友好的 Markdown 全文
   ├─ paper.pdf                   # 原始 PDF
   ├─ metadata.json               # 完整元数据记录
   ├─ figure_1.jpg                # 高清图片（可有多张）
   ├─ figure_2.jpg
   ├─ supplemental--Data.zip      # 补充材料（可选）
   └─ page.html                   # 原始页面 HTML（调试用）
```

---

## 输出文件内容

`paper.md` 包含以下部分：

1. **标题** — `# Title`
2. **作者** — 含机构信息
3. **DOI**
4. **Publication** — 期刊、卷期、页码、出版日期
5. **Abstract** — 摘要（含公式）
6. **Article Text** — 正文（含公式、图表引用、表格）
7. **Supplemental Material** — 补充材料链接
8. **Footnotes** — 脚注（IOP 支持）
9. **References** — 编号引用 + BibTeX 代码块

---

## 关键设计原则

### 浏览器优先，避免直接 HTTP 请求

所有期刊页面都通过 Playwright（有头或无头 Chromium）访问。这确保了：
- ✅ Cloudflare 等反爬验证可以正常通过（利用持久化登录态）
- ✅ JavaScript 动态加载的内容可以完整获取
- ✅ 需要登录/订阅的页面可以正常访问（复用 Chrome 用户数据目录）
- ❌ 不使用 `curl`、`requests`、`wget` 等直接 HTTP 工具访问期刊页面

### 公式完整保留

不同 publisher 使用不同的公式格式，各 handler 针对处理：
- **IOP**: `<script type="math/tex">` — 正则提取，预处理后走 pandoc 转换
- **APS**: MathML — 通过 pandoc `--mathjax` 转 LaTeX
- **AIP/Cambridge/Nature**: MathJax CHTML — `wildcard.py` 中的 `prepare_mathjax_html_fragment()` 折叠为占位符后转换
- **OUP**: MathJax CHTML with `<mjx-assistive-mml>` MathML twin — `OupHandler._mathml_to_latex()` 把 MathML 走 pandoc 转为 LaTeX，display 公式可附 `\tag{...}` 编号

### publisher 无关的主流程

`complete_paper_extraction.py` 不直接处理任何 publisher 的网页结构。所有 publisher 特定的逻辑封装在各 `PublisherHandler` 中，通过统一的 `extract_all()` / `convert_to_markdown()` 接口调用。

---

## 配置说明 (`config.py`)

```python
OUTPUT_DIR_DEFAULT = "captured_data"        # 输出目录
BATCH_SLEEP_ENABLED = True                  # 批量处理时随机睡眠
BATCH_SLEEP_MIN = 60                        # 最小睡眠秒数
BATCH_SLEEP_MAX = 300                       # 最大睡眠秒数
SAVE_WITHOUT_REFERENCES = False             # 参考文献为空时仍保存
CHROME_PATH = "/usr/bin/google-chrome"       # Chrome 可执行文件路径
CHROME_USER_DATA_DIR = "~/.config/google-chrome"  # 用户数据目录
```

---

## 支持的 publisher

| DOI / URL 模式 | Handler | 浏览器 | 公式格式 |
|---|---|---|---|
| `10.1038` / `nature.com` | NatureHandler | 无头 | MathJax |
| `10.1103` / `journals.aps.org` | APSHandler | 有头 | MathML |
| `10.1063` / `pubs.aip.org` | AIPHandler | 无头 | MathJax |
| `10.1088` / `iopscience.iop.org` | IOPHandler | 有头 | `<script type="math/tex">` |
| `10.1017` / `cambridge.org` | CambridgeHandler | 无头 | MathJax |
| `10.1093` / `academic.oup.com` | OupHandler | 无头 | MathJax + MathML twin |
| `10.3390` / `mdpi.com` | MDPIHandler | 无头 | MathJax |
| `10.1145` / `dl.acm.org` | ACMHandler | **有头** | — (**abstract-only**) |
| `10.1016` / `sciencedirect.com` | NatureHandler (回退) | 无头 | MathJax |
| `10.1051` / `epj-conferences.org` | NatureHandler (回退) | 无头 | MathJax |
| `arxiv.org` | APSHandler (回退) | 有头 | 取决于源格式 |

**ACM 的覆盖范围说明**: `ACMHandler` 只保证 `paper.md` 里有 `## Abstract` 段。正文 / 图片 / 补充材料在 ACM 上通常需要登录才能拿到，handler 里全部作为 stub 保留（`figure_urls={}`、`supplemental_urls=[]`），将来若要抓时不用改契约。PDF 链接固定构造为 `https://dl.acm.org/doi/pdf/{doi}`；ACM 对 headless Chromium 有 Cloudflare 硬拦截，因此不能加入 `HEADLESS_ACCESSIBLE_PUBLISHERS`。

---

## 故障排查

| 问题 | 可能原因 | 解决 |
|---|---|---|
| 图片未在 Markdown 中显示 | 正则未匹配到 `**Fig. N:**` 格式 | 检查 `convert_to_markdown()` 中的正则 |
| 公式缺失或格式错误 | pandoc 或 MathML 转换失败 | 检查 `_preprocess_iop_html()` 或 `convert_mathml()` |
| 补充材料未下载 | 需要登录态 | 使用 `--force-headed` 复用已登录 Chrome |
| 反爬检测 | 无头浏览器被识别 | 回退到有头模式 |
| "database is locked" | SQLite 多进程写入 | 已启用 WAL 模式，重试即可 |

---

**最后更新**: 2026-05-18  
**版本**: v3.0 (publisher 多处理器架构)  
**维护者**: Claude AI Assistant

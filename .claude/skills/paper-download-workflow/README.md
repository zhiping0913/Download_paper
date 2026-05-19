# 论文自动提取 Skill — Download_paper

自动化从 DOI 提取论文全文、元数据、图片和补充材料的工作流。

## 快速使用

```bash
/paper-download-workflow <DOI>
```

### 示例

```bash
/paper-download-workflow 10.1103/PhysRevLett.109.245005
/paper-download-workflow 10.1038/s41586-026-10400-2
/paper-download-workflow 10.1088/1361-6463/ae36b8
```

## 完整工作流

1. **激活研究环境** — `source /home/zhiping/research-env/bin/activate`
2. **检测 DOI** — 识别 publisher 类型（Nature、APS、AIP、IOP、Cambridge、arXiv）
3. **启动浏览器** — 根据 publisher 选择无头或有头 Chrome
4. **访问 DOI 页面** — `https://doi.org/{DOI}`
5. **提取元数据** — 标题、作者、期刊、摘要、年份、卷期页码
6. **提取正文** — 遍历文章内容，保留公式（LaTeX/MathML）和章节结构
7. **提取参考文献** — 格式化为 BibTeX 代码块
8. **提取脚注** — 在参考文献前展示
9. **下载图片** — 优先获取高分辨率版本
10. **提取补充材料** — 从 `/data` 端点或 figshare widget 获取下载链接
11. **下载 PDF** — 从 `citation_pdf_url` meta 标签获取链接
12. **生成 Markdown** — 全文章 + 图引用 + 脚注 + 参考文献
13. **保存输出** — 按 `{year}--{title}/` 目录组织

## 输出结构

```
captured_data/
└─ {YYYY}--{Title}/
   ├─ paper.md                    # AI友好的Markdown全文
   ├─ paper.pdf                   # 原始PDF
   ├─ metadata.json               # 完整元数据记录
   ├─ figure_1.jpg                # 高清图片（可有多张）
   ├─ supplemental--Data.zip      # 补充材料（可选）
   └─ page.html                   # 原始页面HTML（调试用）
```

## 支持的 publisher

| DOI / URL 模式 | Publisher | 浏览器模式 |
|---|---|---|
| `10.1038` / `nature.com` | Nature | 无头 |
| `10.1103` / `journals.aps.org` | APS (PRL/PRA/PRX 等) | 有头 |
| `10.1063` / `pubs.aip.org` | AIP Publishing | 无头 |
| `10.1088` / `iopscience.iop.org` | IOP Science | 有头 |
| `10.1017` / `cambridge.org` | Cambridge University Press | 无头 |
| `10.1016` / `sciencedirect.com` | Elsevier (回退 Nature handler) | 无头 |
| `10.1051` / `epj-conferences.org` | EDP Sciences (回退) | 无头 |
| `arxiv.org` | arXiv | 有头 |

## 关键文件

| 文件 | 用途 |
|---|---|
| `complete_paper_extraction.py` | 主入口，编排完整提取流程 |
| `complete_paper_extraction.py` | 主入口，异步工作流编排 |
| `batch_process.py` | 批量 DOI 处理器（支持文件/命令行输入） |
| `config.py` | Chrome 路径、输出目录、批处理延迟等全局配置 |
| `chrome_launcher.py` | 跨平台 Chrome 启动/关闭 |
| `publisher/orchestrator.py` | DOI/URL → publisher 类型 → handler 工厂 |
| `publisher/base.py` | `PublisherHandler` 抽象基类 |
| `publisher/nature.py` | Nature/Springer/Elsevier/EDP 处理器 |
| `publisher/aps.py` | APS 处理器（依赖网络请求捕获） |
| `publisher/aip.py` | AIP 处理器 |
| `publisher/iop.py` | IOP Science 处理器（含脚注、表格公式） |
| `publisher/cambridge.py` | Cambridge University Press 处理器 |
| `publisher/wildcard.py` | 共享工具：正文查找、公式转换、BibTeX 格式化 |
| `core/utilities.py` | Semantic Scholar 查询、BibTeX 格式化 |
| `html_to_md_converter.py` | HTML → Markdown 转换工具函数 |

## 配置

编辑 `config.py` 可调整：

- `OUTPUT_DIR_DEFAULT` — 输出目录（默认 `captured_data/`）
- `BATCH_SLEEP_ENABLED` / `BATCH_SLEEP_MIN` / `BATCH_SLEEP_MAX` — 批量处理时随机睡眠防拉黑
- `SAVE_WITHOUT_REFERENCES` — 参考文献为空时仍保存 Markdown
- `CHROME_PATH` / `CHROME_USER_DATA_DIR` — Chrome 浏览器路径与用户数据目录

## 网络要求

需要在有相关期刊访问权限的网络内使用（**校园网**或机构 VPN）。

## 版本

**版本**: 3.0 (publisher 多处理器架构)  
**最后更新**: 2026-05-18

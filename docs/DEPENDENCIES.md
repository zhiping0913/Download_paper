# Download_paper 依赖需求文档

本文档列举 `complete_paper_extraction.py` 及其 publisher handler 所需的全部依赖，
按类别分为系统工具、Python 包、浏览器、网络资源四个部分。

## 1. 系统工具

| 工具 | 最低版本 | 用途 | 安装方式 |
|------|----------|------|----------|
| `pandoc` | 2.0 | MathML → LaTeX 转换；HTML → Markdown 转换（通过 pypandoc 调用） | `sudo apt install pandoc` |
| `libmagic` | — | 文件头 MIME 类型检测（通过 python-magic 调用） | `sudo apt install libmagic1` |

## 2. Python 包

| 包名 | 最低版本 | 用途 | pip 安装 |
|------|----------|------|----------|
| `playwright` | 1.40 | 浏览器自动化（有头/无头 Chromium） | `pip install playwright` |
| `beautifulsoup4` | 4.12 | HTML 解析与提取 | `pip install beautifulsoup4` |
| `pypandoc` | 1.11 | pandoc 的 Python 绑定 | `pip install pypandoc` |
| `python-magic` | 0.4 | libmagic 的 Python 绑定，文件类型检测 | `pip install python-magic` |
| `requests` | 2.31 | HTTP 请求（Semantic Scholar API、DOI resolver） | `pip install requests` |

以上包均已在 `/home/zhiping/research-env/` 虚拟环境中安装。

### 2.1 安装命令（一次性）

```bash
source /home/zhiping/research-env/bin/activate
pip install playwright beautifulsoup4 pypandoc python-magic requests
playwright install chromium
```

## 3. 浏览器

| 组件 | 用途 | 获取方式 |
|------|------|----------|
| Chromium (Playwright) | 无头模式：DOI 跳转、页面访问、PDF/图片下载 | `playwright install chromium` |
| Google Chrome | 有头模式：远程调试（CDP port 9222），复用已登录会话 | 系统安装 `/opt/google/chrome/chrome` |

### 3.1 Chrome 配置（有头模式）

有头模式需要：
- 可执行文件 `/opt/google/chrome/chrome`（或路径覆盖）
- 用户数据目录 `~/.config/google-chrome`（持有登录态 cookies）
- CDP 端口 `localhost:9222` 可用
- 登录态文件 `.auth/headless_storage_state.json`（可选，由 `--refresh-headless-auth` 刷新）

## 4. 网络资源

| 资源 | URL | 用途 |
|------|-----|------|
| DOI Resolver | `https://doi.org/{doi}` | 将 DOI 重定向到出版商页面 |
| Semantic Scholar API | `https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}` | 补全缺失的标题/年份 |
| APS journals | `https://journals.aps.org` | APS 论文正文、图片、PDF |
| Nature | `https://www.nature.com` | Nature 论文正文、图片、PDF |
| AIP Publishing | `https://pubs.aip.org` | AIP 论文正文、图片、PDF、补充材料 (figshare) |
| Cambridge Core | `https://www.cambridge.org` | Cambridge 论文正文、图片、PDF、补充材料 |
| arXiv | `https://arxiv.org` | 预印本论文 |

## 5. 文件系统约定

| 路径 | 说明 |
|------|------|
| `captured_data/{doi}/` | 每个 DOI 的缓存目录（headless HTML、元数据） |
| `.auth/headless_storage_state.json` | 无头 Chromium 登录态（不在 git 中） |
| `output_dir/{year}--{title}/` | 最终输出目录（MD、PDF、图片、补充材料） |

## 6. 环境变量

无需特殊环境变量。所有配置在 `config.py` 中管理。

## 7. Python 版本

- 最低：Python 3.10+
- 当前：Python 3.12.3（虚拟环境 `/home/zhiping/research-env/`）

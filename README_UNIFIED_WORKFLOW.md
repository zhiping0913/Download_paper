# 🔬 论文完整提取系统 - Download_paper Project

**目的**: 从学术论文DOI自动提取完整信息 → Markdown转换（含LaTeX公式和图片）

**技术**: Playwright + Chrome CDP + 网络监听 + MathML转换

**状态**: ✅ 完整版本 (2026-05-10)

---

## 🎯 核心功能

| 功能 | 说明 | 状态 |
|------|------|------|
| **元数据提取** | 从HTML meta标签提取作者、单位、摘要等 | ✅ |
| **网络监听** | 捕获原始JSON API响应（含MathML公式） | ✅ |
| **公式转换** | MathML → LaTeX自动转换 | ✅ |
| **图片下载** | 高分辨率图片自动下载 | ✅ |
| **Markdown生成** | 完整格式化输出 | ✅ |
| **Chrome集成** | 使用已登录Chrome，绕过认证 | ✅ |

---

## 📋 项目结构

```
Download_paper/
├── 📜 脚本文件
│   ├── complete_paper_extraction.py      ⭐ 主脚本（统一工作流）
│   ├── run_complete_workflow.sh          🚀 快速启动脚本
│   ├── monitor_with_metadata.py          📡 元数据+网络监听
│   ├── convert_with_figures.py           🖼️  图片下载+转换
│   └── [其他辅助脚本]
│
├── 📚 文档
│   ├── COMPLETE_WORKFLOW_GUIDE.md        📖 完整使用指南（你在这里）
│   ├── CLAUDE.md                         ⚙️  项目配置说明
│   └── README.md                         📋 (待补充)
│
├── ⚙️  配置
│   └── config.py                         🔧 全局配置文件
│
└── 📂 数据目录
    ├── captured_data/                    📥 网络监听数据
    ├── markdown_output/                  📤 生成的Markdown
    └── __pycache__/
```

---

## 🚀 极速开始（选择一种方式）

### 方式A：使用启动脚本（推荐）

```bash
# 最简单的方式
cd ~/Projects/Download_paper
./run_complete_workflow.sh 10.1103/PhysRevLett.109.245005
```

### 方式B：使用Python直接运行

```bash
cd ~/Projects/Download_paper
python3 complete_paper_extraction.py 10.1103/PhysRevLett.109.245005
```

### 方式C：指定输出路径

```bash
python3 complete_paper_extraction.py 10.1103/PhysRevLett.109.245005 ~/my_papers/paper.md
```

---

## ⚙️ 环境准备（一次性）

### 1. 检查Chrome运行状态

```bash
# 验证Chrome是否在监听
curl http://localhost:9222/json/version

# 如果没有，启动Chrome：
/opt/google/chrome/chrome --remote-debugging-port=9222 \
  --user-data-dir=~/.config/google-chrome &

# 确认启动
curl http://localhost:9222/json/version
```

### 2. 激活Python环境

```bash
source ~/research-env/bin/activate
python3 --version  # 验证激活
```

### 3. 安装依赖包

```bash
pip install -q playwright pypandoc

# 可选：验证安装
python3 -c "import playwright, pypandoc; print('✓ 依赖已安装')"
```

---

## 📊 工作流详解

### 流程图

```
DOI 输入
   ↓
Step 1: 元数据提取
   ├─ 访问 https://doi.org/{DOI}
   ├─ JavaScript提取meta标签
   └─ 获取：标题、作者、单位、摘要等
   ↓
Step 2: 网络监听
   ├─ 拦截网络请求
   ├─ 捕获JSON API响应
   └─ 保存至 captured_data/api_response_000.json
   ↓
Step 3: 内容提取和转换
   ├─ 解析原始JSON结构
   ├─ MathML → LaTeX转换
   └─ 下载高分辨率图片
   ↓
最终输出
   ├─ {标题}.md （完整Markdown）
   ├─ figure_1.png, figure_2.png, ... （图片）
   └─ captured_data/ （原始数据）
```

### 每一步的详细说明

#### 📍 Step 1: 元数据提取

**目的**: 获取论文的基本信息

**过程**:
```python
# 在Chrome中执行JavaScript
document.querySelectorAll('meta').forEach(meta => {
    const name = meta.getAttribute('name') || meta.getAttribute('property');
    const content = meta.getAttribute('content');
    // 收集所有meta标签
});
```

**提取的字段**:
- `citation_title` → 标题
- `citation_author` → 作者列表
- `citation_author_institution` → 作者单位
- `citation_abstract` → 摘要
- `citation_journal_title` → 期刊
- `citation_publication_date` → 发表日期
- `citation_doi` → DOI
- `citation_volume` → 卷号
- `citation_issue` → 期号
- `citation_firstpage/lastpage` → 页码

**输出示例**:
```json
{
  "title": "Coherent Focusing of High Harmonics...",
  "authors": ["J. M. Mikhailova", "M. V. Fedorov", ...],
  "author_institutions": {
    "J. M. Mikhailova": "Max-Planck-Institut für Quantenoptik",
    ...
  },
  "abstract": "We present a novel method...",
  "journal": "Physical Review Letters",
  "publication_date": "2012-12-15",
  "year": "2012"
}
```

#### 📡 Step 2: 网络监听

**目的**: 捕获原始JSON格式的论文数据（含MathML公式）

**过程**:
- 监听所有网络请求
- 过滤JSON/API响应
- 检测是否包含"abstract", "article", "fulltext"等关键词
- 自动保存到文件

**捕获的信息**:
```json
{
  "front": {
    "components": [
      {
        "type": "p",
        "klass": "introduction",
        "body": "<p>This paper presents...</p>"
      }
    ]
  },
  "back": {
    "components": [
      {
        "type": "reference",
        "body": "<reference>..."
      }
    ]
  }
}
```

#### 🖼️ Step 3: 内容处理

**子步骤3a**: 提取文本和公式
```
遍历JSON组件树
  → 找到 type='p' → 提取段落文本
  → 找到 type='sec' → 提取章节标题
  → 找到 klass='disp-eq' → 提取MathML公式
  → 找到 klass='figure' → 提取图片编号和图注
```

**子步骤3b**: MathML → LaTeX转换
```
输入: <math xmlns="..."><mi>E</mi><mo>=</mo><mi>mc</mi><mo>^</mo><mn>2</mn></math>
        ↓
使用Pandoc转换
        ↓
输出: E = mc^2
```

**子步骤3c**: 下载图片
```
对每个图片编号N：
  1. 构造URL: https://journals.aps.org/prl/article/{DOI}/figures/{N}/large
  2. 在已认证的浏览器会话中访问
  3. 提取图片src属性
  4. 下载图片二进制数据
  5. 保存为 figure_N.png
```

---

## 📁 输出文件说明

### Markdown文件结构

```markdown
# [论文完整标题]

## Authors
- **[作者1]** — [作者1单位]
- **[作者2]** — [作者2单位]
...

## Publication
**Journal:** [期刊名]
**Date:** [日期]
**Year:** [年]
**Volume:** [卷], Issue [期]
**Pages:** [页码]
**DOI:** [DOI]

## Abstract
[完整摘要文本]

---

## Introduction
[介绍内容，公式用$$...$$ LaTeX格式]

## Methodology
[方法部分]

## Figure 1
![Figure 1](figure_1.png)
*[图注]*

## Results
[结果部分]

## References
[参考文献列表]
```

### 生成的文件列表

在指定的输出目录（默认 `~/Downloads/`）中：

```
~/Downloads/
├── [论文标题].md              ← 完整Markdown文档
├── figure_1.png               ← 图片1
├── figure_2.png               ← 图片2
├── figure_3.png               ← 图片3
└── ...

captured_data/
├── api_response_000.json      ← 原始JSON（保存用于调试）
├── capture_report_*.json      ← 网络监听报告
└── [其他中间文件]
```

---

## 🔧 配置和自定义

### 全局配置文件: `config.py`

```python
# Chrome配置
CHROME_USER_DATA_DIR = "/home/zhiping/.config/google-chrome"  # 已登录状态
CHROME_DEBUG_PORT = 9222                                      # 远程调试端口

# 输出目录
CAPTURED_DATA_DIR = "captured_data"                           # 网络数据
MARKDOWN_OUTPUT_DIR = "markdown_output"                       # Markdown输出

# 页面加载
PAGE_LOAD_TIMEOUT = 60000                                     # 60秒超时
WAIT_FOR_REQUESTS = 5                                         # 等待额外请求

# 日志
VERBOSE = True                                                # 详细输出
SAVE_RAW_HTML = True                                          # 保存原始HTML
SAVE_RENDERED_HTML = True                                     # 保存渲染HTML
```

### 修改配置示例

```python
# 修改输出目录
MARKDOWN_OUTPUT_DIR = "/mnt/data/papers"

# 修改超时时间（对于大型论文）
PAGE_LOAD_TIMEOUT = 120000  # 2分钟

# 关闭详细日志
VERBOSE = False
```

---

## 🐛 常见问题和解决方案

### ❌ "无法连接到Chrome port 9222"

**诊断**:
```bash
# 检查Chrome进程
ps aux | grep chrome

# 检查端口监听
netstat -tuln | grep 9222
```

**解决**:
```bash
# 启动Chrome
/opt/google/chrome/chrome --remote-debugging-port=9222 \
  --user-data-dir=~/.config/google-chrome &

# 等待2秒
sleep 2

# 验证
curl http://localhost:9222/json/version
```

### ❌ "没有捕获到JSON响应"

**原因**: 可能是网页结构不同或API不同

**调试**:
1. 检查 `captured_data/` 目录是否有文件
2. 查看 `capture_report_*.json` 了解捕获了什么
3. 检查网络连接

### ❌ "图片下载失败"

**原因**: 网络问题或权限问题

**处理**: 脚本会继续运行，只是跳过未下载的图片

**重试**:
```bash
# 手动检查图片URL是否可访问
curl -I "https://journals.aps.org/prl/article/10.1103/PhysRevLett.109.245005/figures/1/large"
```

### ❌ "pypandoc导入错误"

**解决**:
```bash
# 重新安装
pip install --upgrade pypandoc

# 验证Pandoc安装
pandoc --version

# 如果Pandoc没装
sudo apt-get install pandoc
```

### ⚠️ "超时错误"

**调整超时时间** (`config.py`):
```python
PAGE_LOAD_TIMEOUT = 120000  # 增加到120秒
WAIT_FOR_REQUESTS = 10       # 增加等待时间
```

---

## 💡 使用技巧

### 1️⃣ 批量处理多篇论文

创建 `batch_process.py`:
```python
import asyncio
from complete_paper_extraction import complete_extraction_workflow

DOI_LIST = [
    "10.1103/PhysRevLett.109.245005",
    "10.1103/PhysRevLett.125.015001",
    "10.1103/PhysRevLett.130.123001",
]

async def main():
    for doi in DOI_LIST:
        print(f"\n处理: {doi}")
        await complete_extraction_workflow(doi)
        print(f"✓ 完成: {doi}\n")

asyncio.run(main())
```

运行: `python3 batch_process.py`

### 2️⃣ 在特定目录组织论文

```bash
# 创建按年份组织的目录
mkdir -p ~/Papers/2024 ~/Papers/2023 ~/Papers/2022

# 运行脚本并指定输出
python3 complete_paper_extraction.py 10.1103/PhysRevLett.125.015001 \
  ~/Papers/2024/my_paper.md
```

### 3️⃣ 提取后的处理

```bash
# 转换Markdown为PDF（需要pandoc）
pandoc -f markdown -t pdf output.md -o output.pdf

# 转换为DOCX
pandoc -f markdown -t docx output.md -o output.docx

# 查看文本
cat output.md | less

# 计数统计
wc -l output.md
grep -c "^##" output.md  # 章节数
grep -c '![' output.md    # 图片数
```

---

## 📈 性能指标

| 操作 | 耗时 | 备注 |
|------|------|------|
| 元数据提取 | 3-5秒 | 快速 |
| 网络监听 | 5-15秒 | 取决于页面大小 |
| 公式转换 | <1秒/个 | 非常快 |
| 图片下载 | 2-10秒/个 | 网络相关 |
| **总耗时** | **30-90秒** | 取决于图片数量 |

---

## 🔐 安全和隐私

- ✅ 使用本地Chrome实例（无远程连接）
- ✅ 所有数据保存在本地磁盘
- ✅ 不上传任何信息到外部服务
- ✅ 自动清理临时缓存
- ✅ 使用已登录会话（无需密码）

---

## 📚 相关脚本说明

### monitor_with_metadata.py
**用途**: 单独运行元数据提取和网络监听
```bash
python3 monitor_with_metadata.py https://doi.org/10.1103/PhysRevLett.109.245005
```

### convert_with_figures.py
**用途**: 单独进行图片下载和转换
```python
# Python代码中调用
from convert_with_figures import json_to_markdown_with_figures
```

---

## 🎓 学习资源

- **Playwright文档**: https://playwright.dev/python/
- **Chrome DevTools Protocol**: https://chromedevtools.github.io/devtools-protocol/
- **MathML规范**: https://www.w3.org/Math/
- **Pandoc使用**: https://pandoc.org/

---

## 📞 调试和支持

### 启用详细日志

```python
# 在脚本开头添加
import logging
logging.basicConfig(level=logging.DEBUG)
```

### 获取诊断信息

```bash
# Chrome连接测试
curl -v http://localhost:9222/json/version

# 查看捕获的数据
cat captured_data/api_response_000.json | jq .

# 检查生成的Markdown
head -100 ~/Downloads/*.md
```

### 报告问题

包含以下信息：
1. DOI标识符
2. 错误信息完整文本
3. `captured_data/` 目录中的文件
4. Python和Playwright版本

---

## 📝 更新历史

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.0 | 2026-05-10 | 首个完整版本 - 统一工作流 |
| 0.9 | 2026-05-09 | 元数据提取完成 |
| 0.8 | 2026-05-08 | 图片下载功能 |
| 0.7 | 2026-05-07 | MathML转换 |

---

## 📋 检查清单

启动前检查：

- [ ] Chrome已启动且监听port 9222
- [ ] Python环境已激活 (`source ~/research-env/bin/activate`)
- [ ] Playwright和pypandoc已安装
- [ ] 网络连接正常
- [ ] 输出目录有写权限

---

## 💻 快速命令参考

```bash
# 启动Chrome
/opt/google/chrome/chrome --remote-debugging-port=9222 \
  --user-data-dir=~/.config/google-chrome &

# 激活环境
source ~/research-env/bin/activate

# 运行工作流
cd ~/Projects/Download_paper
./run_complete_workflow.sh 10.1103/PhysRevLett.109.245005

# 查看输出
ls -lh ~/Downloads/*.md
cat ~/Downloads/*.md

# 查看捕获的数据
ls -lh captured_data/
cat captured_data/api_response_000.json | jq .

# 停止Chrome
pkill chrome
```

---

## 🎯 下一步计划

- [ ] 支持arXiv论文
- [ ] 支持其他期刊（Nature, Science等）
- [ ] 并行下载多篇论文
- [ ] Web UI界面
- [ ] 数据库存储
- [ ] 自动生成文献列表（BibTeX）

---

**项目维护**: Claude AI Assistant  
**最后更新**: 2026-05-10  
**版本**: 1.0 (稳定版)

---

## 📖 获取更多帮助

- 查看 `COMPLETE_WORKFLOW_GUIDE.md` 获取详细说明
- 查看 `CLAUDE.md` 获取项目配置
- 查看源代码中的注释了解实现细节

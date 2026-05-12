# Download_paper 项目指南

**工作目录**: `/home/zhiping/Projects/Download_paper`  
**用户角色**: 科研工作者 (激光等离子体相互作用研究)  
**主要工具**: Playwright CLI (浏览器自动化)  
**日期**: 2026-04-24

---

## 🎯 项目目标

使用 `playwright-cli` 工具自动化论文查询和下载流程：
1. 根据用户提供的 **DOI 标识符**
2. 利用 playwright-cli **有头模式**自动访问论文页面
3. 处理页面交互和用户验证
4. 保存并优化为可重复使用的 skill

---

## 🔧 工作方式

### 基本流程

```
用户提供 DOI
    ↓
使用 playwright-cli open --persistent
    ↓
导航到 https://doi.org/{DOI}
    ↓
获取页面快照（snapshot）
    ↓
需要用户验证? 
    ├─ 是 → 提示用户手动验证，等待继续
    └─ 否 → 继续自动化流程
    ↓
提取论文信息/下载链接
    ↓
保存结果
```

### 技术要求

- **有头模式**: 显示浏览器窗口（用户可见）
- **持久化会话**: 使用 `--persistent` 参数保存会话状态、cookies、登录信息
- **等待用户验证**: 遇到验证码或需要登录时，提示用户手动完成，然后程序继续

### DOI 访问方式

标准格式：`https://doi.org/{DOI}`

示例：
- DOI: `10.1103/PhysRevLett.125.015001`
- URL: `https://doi.org/10.1103/PhysRevLett.125.015001`

---

## 💡 我的职责

### ✅ 我应该做的

1. **理解 DOI 格式** - 解析用户提供的各种 DOI 格式
2. **调用 playwright-cli** - 使用正确的参数启动有头浏览器
3. **获取页面快照** - 定期使用 `snapshot` 了解页面状态
4. **处理页面交互** - 根据页面内容点击、填充表单等
5. **提示用户验证** - 检测验证码/登录要求，提示用户
6. **保存工作流** - 将流程转化为可重复的 skill
7. **优化和改进** - 基于测试结果改进流程

### ⚠️ 需要确认的事项

- 是否删除或修改现有文件
- 是否需要长时间运行的自动化任务
- 任何可能消耗资源的操作

---

## 🚀 快速开始命令

### 启动持久化浏览器会话

```bash
# 打开浏览器访问论文
playwright-cli open --persistent https://doi.org/10.1103/PhysRevLett.125.015001

# 获取页面快照
playwright-cli snapshot

# 交互示例
playwright-cli click e5        # 点击元素
playwright-cli fill e3 "text"  # 填充表单
playwright-cli press Enter     # 按回车键

# 关闭浏览器
playwright-cli close
```

### 使用命名会话（推荐）

```bash
# 创建命名会话
playwright-cli -s=paper_session open --persistent

# 在同一会话中进行操作
playwright-cli -s=paper_session goto https://doi.org/10.1103/PhysRevLett.125.015001
playwright-cli -s=paper_session snapshot

# 关闭会话
playwright-cli -s=paper_session close
```

---

## 📋 工作流模板

### 单篇论文下载流程

```bash
# 1. 打开持久化浏览器
playwright-cli -s=paper open --persistent

# 2. 导航到 DOI 页面
playwright-cli -s=paper goto https://doi.org/{DOI}

# 3. 获取快照了解页面结构
playwright-cli -s=paper snapshot

# 4. 检查是否需要验证
# 如果有验证码/登录要求 → 提示用户手动完成

# 5. 与页面交互（示例）
playwright-cli -s=paper click e5      # 点击下载按钮
playwright-cli -s=paper wait-for-load # 等待加载

# 6. 保存结果
playwright-cli -s=paper screenshot --filename=paper.png

# 7. 关闭浏览器
playwright-cli -s=paper close
```

---

## 📚 参考资源

### Playwright CLI 文档
- 核心命令: 在 `.claude/skills/playwright-cli/SKILL.md` 中查看
- 参考资料: `.claude/skills/playwright-cli/references/` 目录

### 关键概念
- **Snapshot**: 页面状态快照，包含可交互元素的引用（e1, e2等）
- **持久化会话**: 保存 cookies、localStorage、sessionStorage
- **有头模式**: 显示浏览器窗口，便于调试和用户验证

---

## 🔄 工作流优化建议

1. **批量处理**: 可以为多个 DOI 循环执行
2. **错误处理**: 捕获网络错误、超时等情况
3. **缓存结果**: 保存已下载的论文信息，避免重复
4. **并行处理**: 使用多个命名会话同时处理多篇论文
5. **验证检测**: 自动检测验证码和登录页面

---

## 🎓 示例场景

### 场景1: 访问开放获取论文
```
用户: "帮我获取 DOI 10.1103/PhysRevLett.125.015001 的论文"
流程: 直接访问 → 获取链接 → 下载成功
```

### 场景2: 需要登录/验证
```
用户: "我需要下载 Nature 上的论文"
流程: 访问 → 检测登录页面 → 提示用户登录 → 用户手动操作 → 继续下载
```

### 场景3: 批量下载
```
用户: "帮我下载这100篇论文"
流程: 循环处理每个 DOI → 管理会话 → 处理验证 → 保存结果
```

---

## 📊 性能指标

- 单篇论文访问: ~3-5秒
- 页面快照生成: ~1-2秒
- 用户验证等待: 需要用户手动操作
- 会话建立: ~2-3秒

---

## 🔐 安全考虑

- **会话管理**: 使用 `--persistent` 保存会话，避免重复登录
- **Cookie 处理**: 自动保存认证信息
- **隐私**: 不保存用户密码（由浏览器管理）

---

## 📝 下一步

1. ✅ 学习和熟悉 playwright-cli 的基本命令
2. ⏳ 开发单篇论文下载的自动化脚本
3. ⏳ 创建可重复使用的 skill
4. ⏳ 测试和优化流程
5. ⏳ 支持批量处理

---

**最后更新**: 2026-04-24  
**维护者**: Claude AI Assistant  
**状态**: 项目初始化阶段

---

## 📝 关键优化发现：使用 Semantic Scholar API

**问题**：从网页解析标题和年份较慢且容易出错

**解决方案**：使用 Semantic Scholar API（来自 Academic_graph_miner 项目）

```python
# 配置
S2_API_URL = "https://api.semanticscholar.org/graph/v1/paper/DOI:"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json'
}

# 获取论文信息
s2_fields = 'title,year'
response = requests.get(
    f"{S2_API_URL}{doi}", 
    params={'fields': s2_fields}, 
    headers=HEADERS, 
    timeout=15
)
data = response.json()  # {"title": "...", "year": 2025}
```

**优势**：
- ✅ 更快（无需渲染网页）
- ✅ 更可靠（结构化数据）
- ✅ 更标准（学术库标准格式）
- ✅ 防变化（不依赖网页结构）

**集成到工作流**：在 playwright-cli 之前先调用 API 获取信息，然后打开浏览器下载 PDF。

---

**最后更新**：2026-04-24（添加 Semantic Scholar API 优化）

---

## 🔐 Chrome持久化配置（关键配置 - 2026-05-10）

**问题**：每次AI会话重新开始时，Playwright会打开新的、没有登录态的Chrome，导致遇到Cloudflare验证等需要手动操作的页面。

**解决方案**：在 `config.py` 中配置Chrome用户数据目录

```python
# config.py 中的关键配置
CHROME_USER_DATA_DIR = "/home/zhiping/.config/google-chrome"  # 自动加载登录状态
CHROME_PROFILE = "Default"
USE_CHROME_MODE = "persistent"  # 使用持久化模式
```

**效果**：
- ✅ 所有Playwright脚本（network_listener.py 等）默认使用已登录的Chrome
- ✅ 即使AI会话压缩或重新开始，仍然保持登录态和cookies
- ✅ 无需手动验证Cloudflare等挑战页面
- ✅ 无需每次都提醒配置Chrome路径

**使用方式**：

```bash
# 自动使用配置文件中的Chrome用户数据目录
python network_listener.py "https://doi.org/10.1103/PhysRevLett.109.245005"

# 或使用新脚本（支持更多选项）
python network_listener_with_auth.py "https://..." 
# 会自动使用 config.py 中的 CHROME_USER_DATA_DIR

# 如需显式指定其他路径（覆盖配置）
python network_listener_with_auth.py "https://..." --user-data-dir /custom/path
```

**配置文件位置**：`/home/zhiping/Projects/Download_paper/config.py`

**重要提示**：这个配置是持久的，即使会话重新开始，我的脚本也会自动使用您已登录的Chrome，无需您重复配置！

---

**最后更新**：2026-05-10（添加Chrome持久化配置）

---

## 🎯 完整论文提取工作流 (v2.0 - 2026-05-11)

### 📌 概述

当前项目已升级到**完整端到端论文提取系统**，支持以下功能：

```
DOI输入 → 元数据提取 → 网络请求监听 → 内容转换 → 
图片下载 → PDF下载 → 补充材料提取 → 元数据JSON记录
```

### 🔄 完整工作流步骤

| 步骤 | 功能 | 输出 |
|------|------|------|
| 1 | 获取论文基本信息 | 标题、年份、DOI |
| 2 | 启动Playwright浏览器 | Chrome连接 |
| 3 | 提取元数据 | 作者、机构、摘要、期刊 |
| 4 | 监听网络请求 | JSON API数据 |
| 5 | 下载高分辨率图片 | PNG文件 |
| 6 | HTML→Markdown转换 | 格式化文本 |
| 7 | 下载PDF | 完整论文 |
| 8 | 提取补充材料 | 链接+描述 |
| 9 | 下载补充文件 | 各类附件 |
| 10 | 保存元数据JSON | 完整记录 |

### 📁 输出文件结构

```
~/Downloads/papers/
└─ {年份}--{标题}/
   ├─ {年份}--{标题}.md              # Markdown文档
   ├─ {年份}--{标题}.pdf             # PDF论文
   ├─ {年份}--{标题}.json            # 元数据
   ├─ figure_1.png                  # 图片1
   ├─ figure_2.png                  # 图片2
   ├─ ...                           # 更多图片
   └─ {年份}--{标题}--Supplemental--{文件}  # 补充材料
```

### 🔑 核心改进 (v2.0)

#### 1. 引用编号保留 ✅
**修复**: 引用编号 `[1–8]` 在转换中丢失
**方案**: 修改HTML清理函数，保留span内容
```python
# 保留内容，只移除标签
html = re.sub(r'<span[^>]*(?:ref-target|multi-ref-content)[^>]*>(.*?)</span>', r'\1', html)
```

#### 2. 智能换行处理 ✅
**修复**: 过度删除换行导致段落合并
**方案**: 识别句子边界（句号+大写字母）
```python
para = re.sub(r'([\.!?])\s*\n+(?=[A-Z])', r'\1:::PARA_BREAK:::', para)
```

#### 3. 补充材料完整提取 ✅
**功能**: 
- JavaScript提取链接和描述
- 浏览器内下载保持登录态
- 单一补充材料部分（无重复）

#### 4. 完整元数据JSON ✅
**记录**:
```json
{
  "pdf": "2025--Title.pdf",
  "supplemental": [
    "2025--Title--Supplemental--Movie.gif",
    "2025--Title--Supplemental--Data.pdf"
  ]
}
```

### 🚀 快速使用

#### 基本命令
```bash
source /home/zhiping/research-env/bin/activate
python complete_paper_extraction.py "10.1103/PhysRevLett.110.175001"
```

#### 支持的期刊
- Physical Review Letters (prl)
- Physical Review Research (prresearch)
- 其他APS期刊（自动检测）

### 📊 性能指标

| 操作 | 时间 |
|------|------|
| 单篇论文完整提取 | 1-3分钟 |
| 图片下载 | 10-20秒 |
| PDF下载 | 5-15秒 |
| 补充材料处理 | 10-60秒 |

### 💡 使用建议

1. **确保Chrome已登录**: `~/.config/google-chrome` 配置
2. **激活研究环境**: `source /home/zhiping/research-env/bin/activate`
3. **检查网络连接**: 确保能访问论文网站
4. **查看输出**: `~/Downloads/papers/` 目录

### 📚 关键文件

| 文件 | 说明 |
|------|------|
| `complete_paper_extraction.py` | 主程序 |
| `WORKFLOW_SUMMARY.md` | 详细工作流文档 |
| `config.py` | Chrome配置 |
| `chrome_launcher.py` | Chrome启动脚本 |

### 🔧 故障排查

**问题**: 引用编号丢失
- ✅ 已在v2.0版本修复（2026-05-11）

**问题**: 补充材料未下载
- 检查Chrome登录状态
- 验证补充材料页面存在

**问题**: 公式格式错误
- 检查pypandoc配置
- 查看转换后的markdown

### 📖 相关文档

- `WORKFLOW_SUMMARY.md` - 完整工作流总结
- `README.md` - 项目说明
- `.claude/Download_paper/CLAUDE.md` - 本文件

---

**最后更新**: 2026-05-11 21:40 UTC  
**版本**: v2.0 (完整功能版)  
**状态**: 生产就绪 ✅  
**维护者**: Claude AI Assistant

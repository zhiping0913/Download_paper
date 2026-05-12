# Download_paper 项目结构

**清理时间**: 2026-05-11  
**状态**: 项目已整理，只保留核心工作流文件

---

## 📁 保留的文件说明

### 🔧 核心文件

#### 1. **complete_paper_extraction.py** (48KB)
**作用**: 统一的论文完整提取工作流主脚本

**功能**:
- ✅ 连接到已登录Chrome (CDP)
- ✅ 从meta标签提取元数据（作者、单位、摘要等）
- ✅ 监听网络请求捕获JSON数据和HTML响应
- ✅ 提取References并转换为Markdown（带DOI链接）
- ✅ 下载高分辨率图片（使用API响应中的URL）
- ✅ 转换MathML公式为LaTeX（**带方程编号**）
- ✅ **从HTML提取PDF下载链接**（支持任何APS期刊）
- ✅ **Semantic Scholar API集成**获取标准化元数据
- ✅ **组织化输出**：`{year}--{title}/` 目录结构
- ✅ **保存JSON元数据文件**
- ✅ 生成结构完整的Markdown文件

**使用方式**:
```bash
source /home/zhiping/research-env/bin/activate
python3 complete_paper_extraction.py <DOI> [输出文件路径]

# 示例
python3 complete_paper_extraction.py 10.1103/PhysRevLett.109.245005 ~/Downloads/paper.md
```

**输出**:
- `paper.md` - Markdown格式的论文（含所有内容）
- `figure_1.png`, `figure_2.png`, `figure_3.png` - 高分辨率图片
- 支持手动下载 `paper.pdf`

---

#### 2. **config.py** (2.1KB)
**作用**: 全局配置文件

**包含**:
- Chrome用户数据目录配置（持久化登录态）
- 数据库路径
- API配置

**关键配置**:
```python
CHROME_USER_DATA_DIR = "/home/zhiping/.config/google-chrome"
```

---

#### 3. **CLAUDE.md** (7.9KB)
**作用**: 项目工作指南

**内容**:
- 项目目标和工作流程
- 技术要求说明
- 我的职责和工作方式
- Chrome持久化配置说明

---

### 📚 文档文件

#### 4. **README_UNIFIED_WORKFLOW.md** (14KB)
**作用**: 统一工作流完整说明文档

**包含**:
- 工作流详细步骤
- 技术架构说明
- 常见问题和解决方案

---

#### 5. **PROJECT_SUMMARY.md** (12KB)
**作用**: 项目总结和设计文档

**包含**:
- 项目背景
- 模块设计
- 性能指标
- 已知问题

---

## 🚀 快速启动

### 前置条件
1. Chrome已启动remote debugging端口
2. 已激活research-env虚拟环境
3. 已登录Chrome（论文访问权限）

### 执行工作流
```bash
# 1. 激活环境
source /home/zhiping/research-env/bin/activate

# 2. 运行提取脚本
cd /home/zhiping/Projects/Download_paper
python3 complete_paper_extraction.py 10.1103/PhysRevLett.109.245005 ~/Downloads/paper.md

# 3. 检查输出
ls -lh ~/Downloads/
# paper.md - 完整的Markdown文件
# figure_*.png - 3张图片
```

---

## 📊 完整工作流流程

```
用户提供DOI
    ↓
访问论文页面 (Playwright + Chrome CDP)
    ↓
Step 1: 提取元数据
    • 从meta标签获取作者、单位、摘要
    • 处理作者-机构关系
    • 提取通讯作者邮箱
    ↓
Step 2: 监听网络请求
    • 捕获fulltext JSON (含文本和Acknowledgements)
    • 捕获abstract HTML (用于References)
    • 保存所有API响应
    ↓
Step 3: 转换并下载
    • 从abstract HTML提取31条References（含DOI链接）
    • MathML → LaTeX公式转换
    • 下载3张高分辨率图片
    • 生成结构完整的Markdown
    ↓
输出文件
    • paper.md (完整正文、公式、图片链接)
    • figure_*.png (3张图片)
    • References带DOI链接
    • Acknowledgements正确位置
```

---

## ✨ 核心功能特性

### 元数据提取 ✅
- 作者名字和所有从属机构
- 正确的作者-机构关系（处理上标编号）
- 通讯作者邮箱（从contrib-notes提取）

### 内容提取 ✅
- 完整正文（front matter + body）
- MathML → LaTeX转换（3个display equations）
- Inline公式正确间距处理

### 图片处理 ✅
- 3张高分辨率图片 `/large` 版本
- 完整的图片编号和说明文本

### References处理 ✅
- 31条References完整提取
- 每条都包含DOI链接（Markdown格式）
- 按正确顺序排列

### Markdown结构 ✅
```
# 标题
## Authors (作者及所有从属机构)
## Publication (期刊、卷号、页码、DOI)
## Abstract
## Article Text (正文)
## Acknowledgements
## Figure 1, 2, 3 (带高分辨率图片)
## Supplemental Material
## References (31条，带DOI链接)
```

---

## 🔍 已知限制

1. **PDF下载**: 
   - APS服务器在浏览器中打开PDF查看器而非直接下载
   - 需要用户手动在浏览器中 Ctrl+S 保存

2. **补充材料**: 
   - 仅提供链接，不下载具体内容

3. **图片位置**: 
   - 图片显示在文章后面，与原文布局不同

---

## 📝 版本历史

- **v1.0 (2026-05-11)**: 项目清理完成，保留核心工作流
  - 删除所有临时/过时脚本
  - 保留3个核心脚本（complete_paper_extraction.py, config.py, CLAUDE.md）
  - 保留2个核心文档（README_UNIFIED_WORKFLOW.md, PROJECT_SUMMARY.md）

---

**最后更新**: 2026-05-11  
**维护者**: Claude AI Assistant  
**状态**: 生产就绪

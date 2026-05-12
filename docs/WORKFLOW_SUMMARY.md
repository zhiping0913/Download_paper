# 完整论文提取工作流总结

**最后更新**: 2026-05-11  
**版本**: 2.0 (完整功能版)  
**状态**: 生产就绪 ✅

---

## 🎯 工作流概述

完整的学术论文自动化提取系统，从DOI到多格式输出的端到端解决方案。

```
输入: DOI
  ↓
[Step 1] 获取论文基本信息 (Semantic Scholar API)
  ↓
[Step 2] 启动Playwright浏览器，连接到已登录Chrome
  ↓
[Step 3] 提取元数据 (作者、机构、摘要、期刊信息)
  ↓
[Step 4] 监听网络请求，捕获JSON API数据 (包含MathML公式)
  ↓
[Step 5] 下载高分辨率图片 (PNG格式)
  ↓
[Step 6] HTML → Markdown转换
        - MathML → LaTeX (pypandoc)
        - 公式编号 (1), (2)...
        - 智能换行处理 (保留句子边界)
        - 引用编号保留 [1–8]
  ↓
[Step 7] 下载论文PDF
  ↓
[Step 8] 获取补充材料
        - 提取链接和描述 (JavaScript)
        - 下载补充文件 (Playwright)
  ↓
[Step 9] 添加补充材料部分到Markdown
  ↓
[Step 10] 保存元数据JSON
         - DOI, 标题, 年份, 作者
         - PDF文件名
         - 补充材料文件列表
  ↓
输出: 完整论文包
      ├─ Markdown文件 (年份--标题.md)
      ├─ PDF文件 (年份--标题.pdf)
      ├─ 元数据JSON (年份--标题.json)
      ├─ 图片文件 (figure_1.png, figure_2.png, ...)
      └─ 补充材料 (年份--标题--Supplemental--文件名)
```

---

## 📋 输入与输出

### 输入
- **必需**: DOI标识符 (e.g., `10.1103/PhysRevLett.110.175001`)
- **可选**: 输出文件路径

### 输出文件结构
```
~/Downloads/papers/
└─ 2025--Isolated attosecond pulses.../
   ├─ 2025--Isolated attosecond pulses.md
   ├─ 2025--Isolated attosecond pulses.pdf
   ├─ 2025--Isolated attosecond pulses.json
   ├─ figure_1.png
   ├─ figure_2.png
   ├─ figure_3.png
   └─ 2025--Isolated attosecond pulses.--Supplemental--Movie.gif
```

### 元数据JSON格式
```json
{
  "doi": "10.1103/PhysRevLett.110.175001",
  "title": "Paper Title",
  "year": 2013,
  "authors": ["Author1", "Author2"],
  "abstract": "...",
  "journal": "Physical Review Letters",
  "volume": "110",
  "issue": "17",
  "pages": "175001",
  "corresponding_author_email": "email@example.com",
  "extracted_at": "2026-05-11T21:12:30.868160",
  "pdf": "2013--Title.pdf",
  "supplemental": [
    "2013--Title--Supplemental--Movie.gif",
    "2013--Title--Supplemental--Data.pdf"
  ]
}
```

---

## 🔧 核心功能模块

### 1. **网络与数据捕获**
- **Playwright CDP连接**: 连接到已登录的Chrome浏览器
- **网络请求监听**: 捕获API JSON响应
- **元数据提取**: 从HTML meta标签解析
- **会话持久化**: 保存cookies和登录态

### 2. **内容转换**
- **HTML清理** (`clean_html_before_conversion`):
  - 移除不必要的span/div标签
  - 保留引用编号 `[1–8]`
  - 转换内联公式格式
  
- **MathML → LaTeX**: 使用pypandoc和MathJax
  
- **智能换行处理** (`clean_excessive_newlines`):
  - 保留句子边界 (句号+大写字母→新段落)
  - 删除段落内不必要的换行
  - 合并错误分割的连接词

- **公式编号** (`add_equation_numbers`):
  - 为display equations自动编号 (1), (2)...
  - 修复pypandoc的格式问题

### 3. **文件管理**
- **图片下载**: 高分辨率PNG
- **PDF下载**: 完整版本
- **补充材料**:
  - JavaScript提取链接和描述
  - 浏览器内下载 (保持登录态)
  - 智能文件命名

### 4. **元数据管理**
- 初始保存 (Step 3)
- 最终更新 (Step 10) - 包含PDF和补充材料列表
- 完整的论文信息记录

---

## 💡 关键改进 (v2.0)

### ✅ 引用编号保留
**问题**: 原始代码删除了引用编号 `[1–8]`
**解决**: 修改HTML清理函数，保留span内容只移除标签
```python
# Before: 删除整个 <span class="ref-target">...</span>
# After: 提取内容 → <span>...</span> → ...
html = re.sub(r'<span[^>]*(?:ref-target|multi-ref-content)[^>]*>(.*?)</span>', r'\1', html)
```

### ✅ 智能换行处理
**问题**: 过度删除换行导致段落合并错误
**解决**: 识别句子边界（句号+大写字母）
```python
para = re.sub(r'([\.!?])\s*\n+(?=[A-Z])', r'\1:::PARA_BREAK:::', para)
```

### ✅ 补充材料部分合并
**问题**: 生成两个"Supplemental Materials"部分
**解决**: 删除`json_to_markdown_complete`中的基本部分，只保留完整版

### ✅ 完整元数据JSON
**功能**: 
- 记录PDF文件名
- 列出所有补充材料文件
- 空列表表示无补充材料 `"supplemental": []`

---

## 🚀 使用方法

### 基本命令
```bash
source /home/zhiping/research-env/bin/activate
python complete_paper_extraction.py "10.1103/PhysRevLett.110.175001"
```

### 指定输出路径
```bash
python complete_paper_extraction.py "10.1103/PhysRevLett.110.175001" ~/papers/output.md
```

### 支持的期刊
- Physical Review Letters (prl)
- Physical Review Research (prresearch)
- 其他APS期刊 (自动检测)

---

## 📊 性能指标

| 操作 | 耗时 |
|------|------|
| 单篇论文完整提取 | 1-3分钟 |
| 图片下载 (4张) | 10-20秒 |
| PDF下载 (1-3MB) | 5-15秒 |
| 补充材料下载 (0-4个) | 10-60秒 |
| Markdown转换 | 2-5秒 |

---

## ⚙️ 系统要求

- **Python**: 3.12+
- **Chrome**: 已登录且持久化配置 (`~/.config/google-chrome`)
- **虚拟环境**: `/home/zhiping/research-env/`
- **依赖**: 
  - Playwright (browser automation)
  - pypandoc (HTML/MathML转换)
  - requests (API调用)
  - Beautiful Soup 4 (HTML解析)

---

## 🔐 安全特性

- ✅ 使用已登录Chrome避免验证码
- ✅ 会话持久化保存cookies
- ✅ 无密码存储 (由浏览器管理)
- ✅ 本地文件存储，无云上传

---

## 📝 近期改进 (2026-05-11)

| 日期 | 改进 |
|------|------|
| 2026-05-11 | 修复引用编号保留 + 完整元数据JSON |
| 2026-05-10 | Chrome持久化配置 + 补充材料合并 |
| 2026-05-09 | 智能换行处理改进 |
| 2026-05-08 | 补充材料描述提取 |
| 2026-05-07 | 补充材料浏览器下载 |

---

## 🔄 故障排查

### 问题: 补充材料未下载
- 检查补充材料页面是否存在
- 验证Chrome登录状态
- 查看网络请求日志

### 问题: 公式格式错误
- 验证MathML转LaTeX是否成功
- 检查pypandoc配置
- 查看generated markdown中的公式格式

### 问题: 引用编号丢失
- 已在v2.0版本修复 ✅
- 确保使用最新代码

---

## 📚 文件位置

| 文件 | 位置 |
|------|------|
| 主程序 | `/home/zhiping/Projects/Download_paper/complete_paper_extraction.py` |
| 配置文件 | `/home/zhiping/Projects/Download_paper/config.py` |
| Chrome启动器 | `/home/zhiping/Projects/Download_paper/chrome_launcher.py` |
| 输出目录 | `~/Downloads/papers/` |

---

## ✨ 下一步改进方向

1. **批量处理**: 支持DOI列表批量提取
2. **并行下载**: 使用multiprocessing加速
3. **缓存优化**: 本地缓存已提取论文
4. **OCR支持**: 处理扫描版论文
5. **多语言**: 支持非英文论文

---

**维护者**: Claude AI Assistant  
**最后更新**: 2026-05-11 21:35 UTC

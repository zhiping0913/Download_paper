# 论文提取工具 - 快速使用指南

**版本**: 2.0 | **最后更新**: 2026-05-11

---

## ⚡ 30秒快速开始

```bash
# 1. 激活环境
source /home/zhiping/research-env/bin/activate

# 2. 提取论文
python complete_paper_extraction.py "10.1103/PhysRevLett.110.175001"

# 3. 查看结果
cd ~/Downloads/papers/
ls -la "2013--Direct observation"*/
```

---

## 📋 完整命令参考

### 基本用法
```bash
# 默认输出到 ~/Downloads/papers/
python complete_paper_extraction.py <DOI>

# 指定输出路径
python complete_paper_extraction.py <DOI> ~/path/to/output.md
```

### 支持的DOI格式
```bash
# 标准格式
python complete_paper_extraction.py 10.1103/PhysRevLett.110.175001

# 包含 / 符号也可以
python complete_paper_extraction.py 10.1103/PhysRevLett.124.185004
```

---

## 📦 输出文件说明

| 文件 | 说明 |
|------|------|
| `*.md` | Markdown格式的完整论文（包含图片引用、公式、引用编号） |
| `*.pdf` | 原始PDF论文 |
| `*.json` | 元数据JSON（包含作者、期刊、PDF和补充材料列表） |
| `figure_*.png` | 论文中的图片（高分辨率） |
| `*--Supplemental--*` | 补充材料（PDFs、数据文件、电影等） |

---

## 📊 工作流时间表

```
0s   → 启动程序
5s   → 连接Chrome浏览器
10s  → 获取元数据
20s  → 监听网络请求
30s  → 下载图片
45s  → 转换为Markdown
60s  → 下载PDF
90s  → 处理补充材料
120s → 保存所有文件

总耗时: 1-3分钟 (取决于补充材料数量和文件大小)
```

---

## ✨ 核心特性

### ✅ 自动处理
- 元数据自动提取（作者、机构、摘要）
- 高分辨率图片自动下载
- MathML公式自动转为LaTeX
- 公式自动编号
- 补充材料描述自动提取

### ✅ 格式处理
- 引用编号保留 `[1–8]`
- 智能换行处理（保留段落结构）
- 括号配对正确
- 数学公式正确显示

### ✅ 安全性
- 使用已登录Chrome（无验证码困扰）
- 会话持久化（自动保存cookies）
- 本地存储（无云上传）

---

## 🔍 查看结果

### 打开Markdown文件
```bash
# 用你喜欢的编辑器打开
code ~/Downloads/papers/2025--Title/2025--Title.md

# 或用less预览
less ~/Downloads/papers/2025--Title/2025--Title.md
```

### 查看元数据JSON
```bash
# 格式化输出JSON
cat ~/Downloads/papers/2025--Title/2025--Title.json | python -m json.tool

# 查看包含的文件
cat ~/Downloads/papers/2025--Title/2025--Title.json | grep -E '"pdf"|"supplemental"'
```

### 列出所有文件
```bash
# 查看提取的所有文件
ls -lh ~/Downloads/papers/2025--Title/

# 统计数据
du -sh ~/Downloads/papers/2025--Title/
```

---

## 🐛 常见问题

### Q: 为什么需要Chrome已登录？
A: 某些论文网站需要验证。使用已登录的Chrome避免验证码和登录提示。

### Q: 如何配置Chrome登录状态？
A: 已自动配置。脚本使用 `~/.config/google-chrome` 目录的现有登录状态。

### Q: 能否处理多篇论文？
A: 当前支持单篇。可手动循环执行或创建batch脚本。

### Q: 补充材料为什么有时下载失败？
A: 网络问题或文件过大。脚本会在错误处继续，不会中断。

### Q: 公式显示不正确？
A: 确保你的Markdown查看器支持LaTeX（VSCode + markdown-preview-enhanced）。

---

## 🎯 最佳实践

1. **检查Chrome登录状态**
   ```bash
   # 手动打开Chrome验证
   google-chrome --profile-directory=Default
   ```

2. **激活虚拟环境**
   ```bash
   source /home/zhiping/research-env/bin/activate
   ```

3. **处理大型论文**
   - 补充材料很大时，可能需要3-5分钟
   - 不要中断，让程序完成

4. **查看日志**
   - 程序输出会显示每个步骤的进度
   - 遇到错误时，日志会帮助诊断

---

## 📁 文件位置

| 组件 | 位置 |
|------|------|
| 主程序 | `/home/zhiping/Projects/Download_paper/complete_paper_extraction.py` |
| 输出 | `~/Downloads/papers/` |
| 配置 | `/home/zhiping/Projects/Download_paper/config.py` |
| 虚拟环境 | `/home/zhiping/research-env/` |

---

## 📚 了解更多

- **完整工作流**: 查看 `WORKFLOW_SUMMARY.md`
- **项目指南**: 查看 `CLAUDE.md`
- **代码注释**: 查看 `complete_paper_extraction.py`

---

## 🚀 示例工作流

### 场景1: 提取单篇论文
```bash
source /home/zhiping/research-env/bin/activate
python complete_paper_extraction.py "10.1103/PhysRevLett.110.175001"
# 结果在 ~/Downloads/papers/2013--Direct observation.../
```

### 场景2: 批量提取多篇论文
```bash
# 创建文件 dois.txt，每行一个DOI
# 然后运行:
while IFS= read -r doi; do
    python complete_paper_extraction.py "$doi"
done < dois.txt
```

### 场景3: 整理提取的论文
```bash
# 移动到研究目录
mv ~/Downloads/papers/* ~/Research/Literature/

# 查看统计
find ~/Research/Literature -name "*.md" | wc -l  # 论文数量
du -sh ~/Research/Literature  # 总大小
```

---

## ✅ 验证安装

```bash
# 检查Python环境
python --version  # 应该显示 3.12+

# 检查依赖
python -c "import playwright; print('✓ Playwright')"
python -c "import pypandoc; print('✓ pypandoc')"

# 检查Chrome
which google-chrome  # 应该显示Chrome路径

# 检查虚拟环境
which pip  # 应该指向 /home/zhiping/research-env/bin/pip
```

---

## 📞 获取帮助

遇到问题时，检查以下内容：

1. ✅ Chrome已安装且登录
2. ✅ 虚拟环境已激活
3. ✅ 网络连接正常
4. ✅ DOI格式正确
5. ✅ 磁盘空间充足

如果问题仍未解决，查看完整工作流文档或代码注释。

---

**Happy researching! 🎓**

版本: 2.0 | 更新: 2026-05-11 | 维护: Claude AI Assistant

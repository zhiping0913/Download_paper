# ✅ 作者和机构信息提取 - 验证指南

**目标**: 确保从网页的meta标签中正确提取作者和机构信息

**日期**: 2026-05-10

---

## 🎯 核心问题

**需求**：从HTML meta标签中直接获取作者信息和对应的机构

**关键标签**：
- `citation_author` - 作者名单
- `citation_author_institution` - 对应的机构信息

**示例**：
```html
<meta name="citation_author" content="J. M. Mikhailova"/>
<meta name="citation_author" content="M. V. Fedorov"/>
<meta name="citation_author_institution" content="Max-Planck-Institut für Quantenoptik, ..."/>
<meta name="citation_author_institution" content="A. M. Prokhorov General Physics Institute, ..."/>
```

---

## 🚀 快速验证

### 1️⃣ 准备环境

```bash
# 启动Chrome
/opt/google/chrome/chrome --remote-debugging-port=9222 &

# 进入项目目录
cd ~/Projects/Download_paper

# 激活环境
source ~/research-env/bin/activate
```

### 2️⃣ 运行验证脚本

```bash
# 验证作者-机构信息
python3 verify_authors_metadata.py 10.1103/PhysRevLett.109.245005
```

### 3️⃣ 查看验证结果

```
📋 验证总结
================================================================================
✓ 作者信息: ✓ 通过
✓ 机构信息: ✓ 通过
✓ 作者-机构配对: ✓ 通过
```

---

## 📋 验证脚本输出说明

验证脚本会输出以下信息：

### Step 1️⃣ - 提取作者信息

```
✓ 从 citation_author 提取到 7 位作者:

  1. J. M. Mikhailova
  2. M. V. Fedorov
  3. N. Karpowicz
  4. P. Gibbon
  5. V. T. Platonenko
  6. A. M. Zheltikov
  7. F. Krausz
```

### Step 2️⃣ - 提取机构信息

```
✓ 从 citation_author_institution 提取到 7 个机构:

  1. Max-Planck-Institut für Quantenoptik, Hans-Kopfermann-Strasse 1, ...
  2. A. M. Prokhorov General Physics Institute, Russian Academy of Sciences, ...
  3. Russian Quantum Center, Novaya Street 100, ...
  ...
```

### Step 3️⃣ - 作者-机构对应关系

```
作者数: 7
机构数: 7

✓ 作者数和机构数相同，可以正确配对:

  J. M. Mikhailova
  → Max-Planck-Institut für Quantenoptik, Hans-Kopfermann-Strasse 1, ...

  M. V. Fedorov
  → A. M. Prokhorov General Physics Institute, Russian Academy of Sciences, ...

  ...
```

---

## ✨ 更新的Markdown输出

使用更新后的 `complete_paper_extraction.py`，生成的Markdown中的作者部分现在会是：

```markdown
## Authors

- **J. M. Mikhailova**  
  Max-Planck-Institut für Quantenoptik, Hans-Kopfermann-Strasse 1, 85748 Garching, Germany

- **M. V. Fedorov**  
  A. M. Prokhorov General Physics Institute, Russian Academy of Sciences, Vavilova Street 38, 119991 Moscow, Russia

- **N. Karpowicz**  
  Max-Planck-Institut für Quantenoptik, Hans-Kopfermann-Strasse 1, 85748 Garching, Germany

...
```

---

## 🔍 调试信息

### 查看原始meta数据

验证脚本会保存完整的meta数据：

```bash
# 查看保存的meta数据
cat captured_data/meta_tags_verification.json | jq .

# 查看只有作者和机构的信息
cat captured_data/meta_tags_verification.json | jq '.authors, .institutions'
```

### 手动验证（浏览器开发者工具）

1. 打开网页：https://doi.org/10.1103/PhysRevLett.109.245005
2. 打开开发者工具：F12
3. 打开Console标签
4. 运行以下代码：

```javascript
// 提取所有meta标签
const metas = {};
document.querySelectorAll('meta').forEach(meta => {
    const name = meta.getAttribute('name') || meta.getAttribute('property');
    const content = meta.getAttribute('content');
    if (name && content) {
        if (!metas[name]) metas[name] = [];
        metas[name].push(content);
    }
});

// 查看作者和机构
console.log("作者:", metas['citation_author']);
console.log("机构:", metas['citation_author_institution']);
```

---

## 📊 完整工作流（包括作者-机构验证）

```
1. 启动Chrome
   ↓
2. 运行验证脚本
   python3 verify_authors_metadata.py 10.1103/PhysRevLett.109.245005
   ↓
3. 查看验证结果
   ✓ 作者信息已正确提取
   ✓ 机构信息已正确提取
   ✓ 作者-机构对应关系正确
   ↓
4. 运行完整提取
   ./run_complete_workflow.sh 10.1103/PhysRevLett.109.245005
   ↓
5. 检查生成的Markdown
   ✓ 作者部分显示每位作者和对应机构
```

---

## 🐛 常见问题

### Q: 提取到的作者或机构信息为空？

**A**: 原因可能是：
1. Chrome未正确连接
2. 网页未完全加载
3. meta标签不存在或标签名不同

**解决**：
```bash
# 1. 检查Chrome连接
curl http://localhost:9222/json/version

# 2. 等待更长时间
# 修改 config.py
PAGE_LOAD_TIMEOUT = 120000

# 3. 检查实际的meta标签名
# 使用浏览器开发者工具查看
```

### Q: 作者数和机构数不相同？

**A**: 这可能是网站的meta标签设置不一致

**解决**：
- 验证脚本会显示具体的作者-机构对应关系
- 脚本会处理这种情况，只配对存在的关系

### Q: Markdown中作者信息没有显示机构？

**A**: 可能的原因：
1. 机构信息提取失败
2. 脚本版本不是最新的

**解决**：
```bash
# 更新脚本到最新版本
git pull  # 或重新下载

# 运行验证脚本确认信息被正确提取
python3 verify_authors_metadata.py 10.1103/PhysRevLett.109.245005
```

---

## ✅ 验证清单

首次使用前：

- [ ] Chrome已启动且监听port 9222
- [ ] 运行验证脚本看是否能提取作者信息
- [ ] 检查作者数和机构数是否相同
- [ ] 查看生成的Markdown中作者部分是否包含机构信息
- [ ] 对比期望的格式和实际输出

---

## 📚 相关文件

| 文件 | 说明 |
|------|------|
| `verify_authors_metadata.py` | 验证脚本 |
| `complete_paper_extraction.py` | 更新的主脚本 |
| `config.py` | 配置文件 |
| `captured_data/meta_tags_verification.json` | 验证结果 |

---

## 🎯 下一步

1. **验证环境**：运行验证脚本
2. **确认结果**：查看作者-机构信息
3. **运行提取**：使用完整工作流
4. **检查输出**：验证Markdown中的作者部分

---

**最后更新**: 2026-05-10  
**版本**: 1.0  
**状态**: 完成验证流程

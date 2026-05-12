# 论文自动下载 Skill

自动化从DOI下载研究论文、提取元数据、组织存档的工作流。

## 快速使用

```bash
/paper-download-workflow <DOI>
```

### 示例

```bash
/paper-download-workflow 10.1103/PhysRevLett.109.245005
/paper-download-workflow 10.1103/PhysRevLett.94.103903
```

## 完整工作流

1. **启动Chrome** - 用已有的登录态启动浏览器
2. **获取元数据** - 调用Semantic Scholar API获取标题和年份
3. **打开DOI页面并保存网页** - 在Chrome中访问 `https://doi.org/{DOI}`，滚动加载所有内容，保存完整HTML
4. **提取补充材料链接** - 从保存的HTML中搜索supplemental链接
5. **打开PDF链接** - 在Chrome新建标签页直接访问PDF链接
6. **保存PDF** - 用Ctrl+S保存到Downloads文件夹
7. **检查补充材料关键字** - 用pdfplumber扫描PDF中的补充材料关键字（可选）
8. **验证下载文件** - 查找并验证下载的PDF
9. **整理文件** - 清理文件名，按`{year}/{year}--{title}.pdf`格式组织

## 新特性：完整网页保存 + 智能缓存

现在工作流在下载PDF之前会**保存完整的DOI页面HTML**，包含：
- ✅ 异步加载的论文内容
- ✅ PDF下载链接
- ✅ 补充材料链接
- ✅ 图片和嵌入资源

**智能缓存机制** (v2.0新增):
- 检查本地`~/Downloads/`中是否有**15分钟内**保存的HTML
- 如果有，直接使用本地缓存，**避免Chrome的"replace"提示**
- 如果没有，才重新保存新的HTML
- 效果：连续下载多篇论文时工作流完全自动化 ✅

## 输出

保存位置示例：
```
/home/zhiping/Research/Papers/2012/2012--Isolated attosecond pulses from laser-driven synchrotron radiation..pdf
/home/zhiping/Research/Papers/2005/2005--Coherent focusing of high harmonics: a new way towards the extreme intensities..pdf
```

## 工作原理

- **Chrome持久化**: 复用已登录的浏览器会话
- **Semantic Scholar API**: 快速获取标准化元数据
- **自动验证**: 先访问DOI页面通过Cloudflare验证，再直接下载PDF
- **智能重命名**: 自动去除文件名中的特殊字符

## 时间预期

约30-40秒完成一篇论文，主要取决于：
- Cloudflare验证速度（5-10秒）
- PDF文件大小（5-15秒）
- 网络延迟

## 支持的期刊

- **Physical Review Letters** (PRL) - ✅ 完全支持
- 其他APS期刊 - 应该可以工作（URL格式相同）
- 其他期刊 - 可能需要调整PDF链接格式

## 依赖项

- Chrome浏览器
- xdotool
- curl
- Python 3

## 故障排除

### Q: Chrome窗口没有出现新标签页？
A: 检查Chrome是否正在运行。脚本会在已打开的Chrome实例中创建新标签页。

### Q: PDF没有保存？
A: 检查验证步骤是否完成。可能需要在浏览器中手动点击PDF链接。

### Q: 文件名很长且包含特殊字符？
A: 脚本会自动清理，去除 `/ : * ? " < > |` 等字符。

## 配置

编辑 `paper-download.sh` 中的以下内容：
- `DOI` - 论文标识符
- `/home/zhiping/Research/Papers/` - 存档目录
- `https://journals.aps.org/prl/pdf/` - PDF链接格式

---

**版本**: 2.0 (HTML缓存优化)  
**最后更新**: 2026-05-09

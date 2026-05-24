我们尝试来让程序支持从sciencedirect出版商处获取论文，这个出版商的页面要求必须使用有头浏览器来进行访问,以10.1016/j.jcp.2019.109131为例，缓存下来的html已经位于/home/zhiping/Projects/Download_paper/captured_data/10.1016_j.jcp.2019.109131/page.html。
请参照/home/zhiping/Projects/Download_paper/publisher/iop.py和/home/zhiping/Projects/Download_paper/publisher/optica.py里提取信息的方法，一个针对sciencedirect的Handler。
走/home/zhiping/Projects/Download_paper/complete_paper_extraction.py里全流程，直到生成的md里包含我们需要的正文全部内容，pdf能成功下载，图片完全。
我们发现：
1：
window.__PRELOADED_STATE__ 的json里有很多我们需要的元数据，提取这个json。
2：
<div class="body-area" lang="en">下面，有Abstract，有Highlights，keywords，（或者别的条目），把它们都写进md的abstract部分。metadata.json中的abstract使用window.__PRELOADED_STATE__里的abstract。
3：
pdf链接为https://www.sciencedirect.com/science/article/pii/S0021999119308368/pdfft?md5=a1f388af625b6dda59ba10f684221af2&pid=1-s2.0-S0021999119308368-main.pdf，在html里有2处：
window.__PRELOADED_STATE__ 的json里有"pdfDownload"字段，这里有下载链接（首选）
</button>aria-label="View PDF. Opens in a new window.这里也有链接（备选）
请找到正确的链接。
注意，html里会把reference文章的pdf链接也给出来，请不要被这些reference里的链接误导了，程序也不要识别到那些reference的pdf链接
4：
正文开始于<div class="body u-font-serif" id="body">，以<div></div>分隔段落，注意要包含里面的所有的h2,h3,h4等等层级的子标题如<h4 id="st0070" class="u-margin-m-top u-margin-xs-bottom">2.1.1. Mass conservation equation</h4>。inline以及大display公式都是mathjax，可以直接转换。注意要保留公式编号<span class="display"><spanid="fm0010" class="formula"><spanclass="label">(1)</span>
5：
图片在<figure class="figure text-xs" id="fg0010">，要保留high-res image，
<a class="anchor download-link u-font-sans anchor-primary" href="https://ars.els-cdn.com/content/image/1-s2.0-S0021999119308368-gr001_lrg.jpg" target="_blank" download="" title="Download high-res image (69KB)">，可以同时提取Download high-res image和Download full-size image，优先选择Download high-res image。图片下面有图注，<span class="captions text-s">，按照有公式的段落进行转换。
6：
这篇文章有table，在<div class="tables frame-topbot rowsep-0 colsep-0" id="tbl0010">，对table中的每个各自都当做有公式的段落进行转换。不要忘了table的<span class="captions text-s">，这里也是有公式的。
7：
reference在<div data-testid="references">，下面用<li>分隔每条reference。在md中输出原文给出的reference样式，以及从crossref获取并转为bibtex格式的。
8：
目前我还没找到有supplementary的sciencedirect的文章，等遇到了再增加提取supplementary的部分。


我们尝试来让程序支持从science出版商处获取论文，这个出版商的页面要求必须使用有头浏览器来进行访问,以10.1126/sciadv.aar3761为例，缓存下来的html已经位于/home/zhiping/Projects/Download_paper/captured_data/10.1126_sciadv.aar3761/page_000__www.science.org_doi_10.1126_sciadv.aar3761__efdee4c058.html。
请参照/home/zhiping/Projects/Download_paper/publisher/iop.py和/home/zhiping/Projects/Download_paper/publisher/sciencedirect.py里提取信息的方法，一个针对science的Handler。
走/home/zhiping/Projects/Download_paper/complete_paper_extraction.py里全流程，直到生成的md里包含我们需要的正文全部内容，pdf能成功下载，图片完全。
我们发现：
1:
abstracts在<div id="abstracts" data-extent="frontmatter">，提取它，按照段落处理。
2：
正文在
<section id="bodymatter" data-extent="bodymatter" property="articleBody" typeof="Text">，下面会有<h2><h3>等小标题，正文段落由<div role="paragraph">分隔。
3：
图片在<div class="figure-wrap">，里面有图片链接，<figcaption>，<div class="caption">，<div class="notes">都要当做段落转换。
4：
display-formula在<div id="E1" class="display-formula">，直接转。
5：
Supplementary在<section id="backmatter" data-extent="backmatter">里的<section id="supplementary-materials" class="core-supplementary-materials">，如果这里也有<div role="paragraph">的话，那也要提取。补充文件下载链接可以通过搜索<h2>Supplementary Material</h2>里的链接找到。
6：
reference也在<section id="backmatter" data-extent="backmatter">里的<section id="bibliography" role="doc-bibliography">。直接将<div class="citation-content">输出到md里，附上<div class="core-xlink-crossref">，以及从crossref获取到的引用转为bibtex
7：
pdf的链接为https://www.science.org/doi/pdf/{doi}?download=true，注意不要忘了这个download=true。



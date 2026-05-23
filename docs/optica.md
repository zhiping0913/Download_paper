# Optica Publishing Handler

## Overview

The Optica handler extracts metadata, article body, figures, tables, references, and supplemental materials from Optica Publishing Group articles (opg.optica.org, journals.aps.org for Optics Express and others).

**Publisher Detection**: DOI prefix `10.1364` or URL domain `opg.optica.org`

**Browser Requirement**: Must use **headed browser** (浏览器可见) for JavaScript rendering

## HTML Structure

### Article Marker
- Articles start after `<!-- Article Body -->` comment
- Main content is in `<div class="main-content col-md-9">`

### Sections (H2 Headings)
All article sections use `<h2 class="article-heading" id="...">` pattern:
```html
<h2 class="article-heading" id="Abstract">Abstract</h2>
<h2 class="article-heading" id="sec1">1. Introduction</h2>
<h2 class="article-heading" id="sec2">2. Methods</h2>
<h2 class="article-heading" id="References">References</h2>
<h2 class="article-heading" id="Supplemental document">Supplemental document</h2>
```

### Paragraph Content
- Paragraphs use standard `<p>` tags
- Can contain:
  - Plain text
  - Inline formulas: `<span class="inline-formula">$...$</span>`
  - Reference links: `<a class="ref" href="#ref1">[1]</a>`
  - Formatted text: `<b>`, `<i>`, `<sup>`, `<sub>` tags

### Formulas
Optica stores formulas directly in LaTeX format:

**Inline math**: Already in `$...$` delimiters
```html
<span class="inline-formula">$I(\omega )\propto {\omega ^{ - 8/3}}$</span>
```

**Display math**: Inside `<div class="article-math-block">` with `$$...$$` delimiters
```html
<div class="article-math-block">
  <a class="math-controls" data-target="#mathJaxHelp">
    <span class="icon-options"/>(1)</a>
  $$f({\omega ,P} )={-} \frac{i}{4}\mathop {\oint }\limits_C ...$$
</div>
```

No preprocessing needed—formulas are already in LaTeX, not MathML or GIFs.

### Figures
Structure:
```html
<div class="figure-image" id="g001">
  <img class="figure" src="/getimagev2.cfm?img=..." data-src="..." alt="Fig. 1. ..."/>
  <p>
    <span class="figure-title"><strong>Fig. 1.</strong></span>
    <span class="figure-caption">SHHG simulation using the EPOCH code. (a) ...</span>
  </p>
  <p class="small">
    <a href="/viewmedia.cfm?uri=oe-30-1-389&figure=oe-30-1-389-g001&imagetype=full" target="_blank">
      Download Full Size
    </a> | 
    <a href="/viewmedia.cfm?...&imagetype=pdf">PDF</a>
  </p>
</div>
```

**Figure extraction process**:
1. Find all `<div class="figure-image" id="...">` elements
2. Extract `<strong>Fig. N.</strong>` from `<span class="figure-title">`
3. Extract caption from `<span class="figure-caption">` (process as paragraph with formula support)
4. Get image URL from "Download Full Size" link's `href`
5. Convert relative URLs (e.g., `/viewmedia.cfm?...`) to absolute

### References
Structure:
```html
<h2 class="article-heading" id="References">References</h2>
<p id="ref1" class="reference-body">
  <strong class="number">1. </strong>
  E. Goulielmakis, Z. H. Loh, ... "Real-time observation...", 
  Nature <b>466</b>(7307), 739–743 (2010).
  <br/>
  <a target="_blank" href="http://dx.doi.org/10.1038/nature09212">Crossref</a>
</p>
...
```

**Reference extraction process**:
1. Find `<h2 id="References">`
2. Extract all `<p id="refN" class="reference-body">` elements
3. For each reference:
   - Extract DOI from `<a href="http://dx.doi.org/...">` link
   - Fetch reference metadata from Crossref API using the DOI
   - Convert to BibTeX format
   - If no Crossref data available, store as raw text

### Supplemental Materials
**Link location**:
```html
<h2 class="article-heading" id="Supplemental document">Supplemental document</h2>
<p>See <a target="_blank" href="https://doi.org/10.6084/m9.figshare.17207330">Supplement 1</a> for supporting content.</p>
```

Or in supplementary materials section:
```html
<h3 class="article-heading">Supplementary Material (1)</h3>
<table>
  <tr>
    <td>
      <i class="fal fa-angle-double-right"></i> 
      <a class="view_media" href="viewmedia.cfm?uri=oe-30-1-389&seq=s001">
        Supplement 1
      </a>
    </td>
    <td>Supplemental document: Numerical methods and error analysis</td>
  </tr>
</table>
```

**Supplemental extraction process**:
1. Find `<h2>` containing "Supplemental" (case-insensitive)
2. Extract all `<a href="...">` links pointing to:
   - Figshare: `https://doi.org/10.6084/...` or `figshare.com/...`
   - Local: `viewmedia.cfm?...` (convert to absolute URL)
3. For figshare links, optionally follow to figshare page and extract download button URL

## Extraction Algorithm

### Metadata
```python
soup = BeautifulSoup(html_content, 'html.parser')

# Extract from <meta> tags
for tag in soup.find_all('meta'):
    name = tag.get('name', '')
    content = tag.get('content', '')
    
    if name == 'citation_author':      authors.append(content)
    if name == 'citation_title':       meta['title'] = content
    if name == 'citation_doi':         meta['doi'] = content
    if name == 'citation_journal_title': meta['journal'] = content
    if name == 'citation_volume':      meta['volume'] = content
    if name == 'citation_issue':       meta['issue'] = content
    if name == 'citation_firstpage':   meta['first_page'] = content
    if name == 'citation_publication_date': meta['publication_date'] = content
```

### Article Body
```python
# 1. Find article body marker and main content div
article_body_marker = soup.find(string=re.compile(r'Article Body'))
main_content = article_body_marker.find_next('div', class_='main-content')

# 2. Traverse all <h2 class="article-heading"> sections
for h2 in main_content.find_all('h2', class_='article-heading'):
    h2_id = h2.get('id', '')
    
    if 'abstract' in h2_id.lower():
        # Extract abstract in separate call
        continue
    elif 'supplemental' in h2_id.lower():
        # Skip supplemental document section
        continue
    elif 'references' in h2_id.lower():
        # Skip references (extracted separately)
        continue
    else:
        # Regular section: add heading
        h2_text = h2.get_text(' ', strip=True)
        body_parts.append(f"## {h2_text}")
        
        # 3. Process all content between this h2 and next h2
        current = h2.find_next_sibling()
        while current and current.name != 'h2':
            if current.name == 'p':
                # Convert paragraph to markdown with formula support
                p_md = convert_optica_paragraph_to_md(str(current))
                body_parts.append(p_md)
            elif current.name == 'h3':
                # Subsection
                h3_text = current.get_text(' ', strip=True)
                body_parts.append(f"### {h3_text}")
            elif current.name == 'div' and 'article-math-block' in (current.get('class') or []):
                # Display equation
                math_content = current.get_text(strip=True)
                body_parts.append(f"$$\n{math_content}\n$$")
            
            current = current.find_next_sibling()
```

### Paragraph to Markdown Conversion
```python
def convert_optica_paragraph_to_md(html_fragment: str) -> str:
    # 1. Protect math delimiters during HTML→MD conversion
    math_placeholders = {}
    
    # Extract display math $$...$$ (non-greedy)
    html_fragment = re.sub(
        r'\$\$[^$]*\$\$',
        lambda m: placeholder_for_math(m, math_placeholders),
        html_fragment,
        flags=re.DOTALL
    )
    
    # Extract inline math $...$ (non-greedy)
    html_fragment = re.sub(
        r'\$[^$]+?\$',
        lambda m: placeholder_for_math(m, math_placeholders),
        html_fragment,
    )
    
    # 2. Convert remaining HTML tags → Markdown
    md = convert_html_fragment_to_markdown(html_fragment)
    
    # 3. Restore math delimiters
    for placeholder, math in math_placeholders.items():
        md = md.replace(placeholder, math)
    
    return md
```

### Figures
```python
figures = {}
for fig_div in soup.find_all('div', class_='figure-image'):
    # Extract title
    title_span = fig_div.find('span', class_='figure-title')
    title = title_span.get_text().strip() if title_span else ''
    
    # Extract caption
    caption_span = fig_div.find('span', class_='figure-caption')
    caption = convert_optica_paragraph_to_md(str(caption_span)) if caption_span else ''
    
    # Extract download link
    download_link = fig_div.find('a', string=re.compile(r'Download Full Size'))
    if download_link:
        img_url = download_link.get('href', '')
        if not img_url.startswith('http'):
            img_url = 'https://opg.optica.org' + img_url
    
    figures[f'fig_{len(figures)+1}'] = {
        'url': img_url,
        'caption': caption,
    }
```

### References
```python
references = []
for ref_p in soup.find_all('p', class_='reference-body'):
    ref_text = ref_p.get_text(' ', strip=True)
    
    # Try to find DOI
    doi_link = ref_p.find('a', href=re.compile(r'doi.org'))
    if doi_link:
        doi = extract_doi_from_url(doi_link.get('href'))
        crossref_data = fetch_crossref(doi)
        bibtex = format_as_bibtex(crossref_data)
        references.append(bibtex)
    else:
        # No DOI, store as raw text
        references.append({
            'raw': ref_text,
            'type': 'misc',
        })
```

### Supplemental Materials
```python
supp_links = []

# Find Supplemental document section
supp_h2 = soup.find('h2', class_='article-heading', string=re.compile(r'Supplemental'))
if supp_h2:
    # Look for figshare links in following paragraphs
    for p in supp_h2.find_next_siblings('p'):
        a = p.find('a', href=re.compile(r'figshare|doi.org/10.6084'))
        if a:
            href = a.get('href')
            if href and 'http' in href:
                supp_links.append(href)

# If figshare link found, optionally navigate with browser to get actual download URL
# This requires headed browser and separate page navigation
```

## Key Implementation Details

### 1. Formula Handling
- **No preprocessing needed**: Formulas are already in LaTeX
- **Protect during HTML→MD**: Extract `$...$` and `$$...$$` before HTML conversion
- **Restore after**: Replace placeholders with original LaTeX

### 2. Paragraph Processing
- **All paragraphs must go through conversion**: Including figure captions and references
- **Preserve HTML structure**: Bold, italic, subscript/superscript
- **Handle reference links**: `<a class="ref" href="#ref1">[1]</a>` → `[1]`

### 3. Figure URLs
- **Relative URLs**: Convert `/viewmedia.cfm?...` to `https://opg.optica.org/viewmedia.cfm?...`
- **Prefer high-quality**: Use `/imagetype=full` for full resolution

### 4. References with Crossref
- **Extract DOI from HTML**: From `<a href="http://dx.doi.org/10.1038/...">Crossref</a>`
- **Fetch from Crossref API**: Get complete metadata (authors, title, journal, etc.)
- **Format as BibTeX**: Use standardized key format

### 5. Supplemental Materials
- **Figshare links**: Store as-is or follow with browser to get download URL
- **Local links**: Convert `viewmedia.cfm?uri=...&seq=s001` to absolute URLs
- **Fallback**: Store URLs as plain links if full parsing fails

## Testing

Example paper: **10.1364/OE.444043**
- Title: "Proposal for complete characterization of attosecond pulses from relativistic plasmas"
- Contains: Abstract, Methods, Results, References (41), Supplemental materials (Figshare)
- Expected: Full HTML extraction with formulas, figures, and supplemental link

```bash
python complete_paper_extraction.py "10.1364/OE.444043" --force-headed
```

Expected output:
- `10.1364_OE.444043.md` - Full article in Markdown with LaTeX formulas
- `10.1364_OE.444043_figures/` - Extracted figures
- References formatted with DOI links
- Supplemental material link preserved

## Notes

- **Browser requirement**: Optica uses JavaScript rendering for some content; headed browser necessary
- **No MathML**: All formulas are in LaTeX, making extraction straightforward
- **Figshare links**: For supplemental materials, may need separate page navigation with `page.goto()`
- **Crossref API**: References may be incomplete if DOI not in Crossref database
- **Rate limiting**: Observe Crossref API rate limits (1.2s between requests)

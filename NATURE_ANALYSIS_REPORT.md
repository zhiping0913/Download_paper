# Nature Publisher vs APS: Implementation Guide

**Analysis Date**: 2026-05-12  
**Paper Analyzed**: 10.1038/s41586-026-10400-2  
**Title**: "Efficiency-optimized relativistic plasma harmonics for extreme fields"

---

## 1. REDIRECT CHAIN

### Nature (Springer Link)
```
https://doi.org/10.1038/s41586-026-10400-2
  ↓ (302)
https://www.nature.com/articles/s41586-026-10400-2
  ↓ (303)
https://idp.nature.com/authorize?response_type=cookie&client_id=grover&redirect_uri=...
  ↓ (302)
https://idp.nature.com/transit?redirect_uri=...&code=...
  ↓ (302)
https://www.nature.com/articles/s41586-026-10400-2
```

**Key difference from APS**: 
- Nature uses OAuth-like authentication flow (idp.nature.com)
- Multiple redirects before reaching final article page
- Authentication happens via identity provider

---

## 2. METADATA EXTRACTION LOCATIONS

### Nature Meta Tags (Primary Source)

| Metadata Type | Meta Tag Name | Value |
|--------------|--------------|-------|
| **Title** | `dc.title` | "Efficiency-optimized relativistic plasma harmonics for extreme fields" |
| **Title** | `citation_title` | Same as above |
| **Author** | `citation_author` | "Norreys, Peter" (only first author in meta tag) |
| **Author Institution** | `citation_author_institution` | "John Adams Institute..." |
| **DOI** | `citation_doi` | "10.1038/s41586-026-10400-2" |
| **DOI** | `prism.doi` | "doi:10.1038/s41586-026-10400-2" |
| **DOI** | `DOI` | "10.1038/s41586-026-10400-2" |
| **Journal** | `citation_journal_title` | "Nature" |
| **Publication Date** | `citation_online_date` | "2026/04/22" |
| **Publication Date** | `prism.publicationDate` | "2026-04-22" |
| **Abstract** | `dc.description` | (truncated in meta tag, full in JSON-LD) |

### JSON-LD Structured Data

```javascript
{
  "@type": "WebPage",
  "mainEntity": {
    "@type": "ScholarlyArticle",  // likely
    "headline": "Efficiency-optimized relativistic plasma harmonics for extreme fields",
    "description": "Bright harmonic radiation from relativistically oscillating laser plasmas...",
    // Contains full abstract and more details
  }
}
```

### HTML DOM Content

| Element Type | CSS/HTML Selector | Content |
|--------------|------------------|---------|
| **Title** | `h1`, `[class*="title"]` | Article title in header |
| **Authors** | `[class*="author"]` | Multiple author elements with ORCID |
| **Figures** | `figure`, `[class*="figure"]` | DIV or FIGURE elements |
| **Figure Caption** | `figcaption`, `[class*="caption"]` | "Fig. 1: Fine-tuning of laser..." |
| **References** | `[class*="reference"] li` | Reference list items |
| **Formulas** | `math`, `[class*="math"]`, `.katex` | 38 formula elements found (SPAN type) |

---

## 3. NETWORK REQUESTS & APIs

### Nature-Specific API Endpoints

| Endpoint | Method | Purpose | Response |
|----------|--------|---------|----------|
| `/platform/contextual?doi=10.1038/...` | GET (XHR) | Contextual/metadata endpoint | JSON (593 bytes) |
| `/exposed-details` (idp.nature.com) | GET (XHR) | User/auth details | JSON (198 bytes) |

### Key Difference from APS
- **APS**: `/fulltext/{doi}` returns structured JSON with components, figures as objects
- **Nature**: Meta tags + HTML DOM + minimal API calls
- **Nature**: No dedicated fulltext JSON API found in initial page load

### Figure URLs

Nature figure URLs follow this pattern:
```
https://media.springernature.com/lw685/springer-static/image/art%3A10.1038%2Fs41586-026-10400-2/MediaObjects/41586_2026_10400_Fig1_HTML.png?as=webp
```

Size variants: `w215h120`, `lw685`, etc. (similar to APS asset.variants)

---

## 4. ARTICLE CONTENT STRUCTURE

### What Works Differently

#### APS Approach (Current Implementation)
```
complete_paper_extraction.py
  → Navigate to DOI
  → Listen for /fulltext/{doi} API response
  → Parse fulltext JSON with components array
  → Extract figures from asset.variants
  → Convert JSON to markdown
```

#### Nature Approach (Needed for Publisher Expansion)
```
complete_paper_extraction.py (with NatureHandler)
  → Navigate to DOI
  → Wait for page load (content in HTML/DOM)
  → Extract meta tags for title/authors/abstract
  → Parse JSON-LD for structured data
  → Query DOM for article content sections
  → Extract figures from HTML img tags
  → Render HTML to markdown (pypandoc)
  → Extract references from HTML list
```

### Nature Metadata Found

| Field | Source | Example Value |
|-------|--------|----------------|
| **Title** | meta dc.title | "Efficiency-optimized relativistic..." |
| **Authors** | HTML DOM `[class*="author"]` | 5+ authors with ORCIDs |
| **Abstract** | JSON-LD mainEntity.description | Full abstract text |
| **Journal** | meta citation_journal_title | "Nature" |
| **DOI** | meta citation_doi | "10.1038/s41586-026-10400-2" |
| **Date** | meta prism.publicationDate | "2026-04-22" |
| **Figures** | HTML `<figure>` elements | 3+ figures with captions |
| **References** | HTML `[class*="reference"]` | 50+ references |
| **Formulas** | HTML `<span class="math">` | 38 formula elements |

---

## 5. AUTHENTICATION & ACCESS

### Nature Authentication Flow

1. DOI resolver → Nature URL (302)
2. Nature URL → Identity provider (303)  
3. Identity provider → OAuth flow with code
4. Redirect back to article with authentication cookie
5. Content becomes accessible

### Implications for Handler

- **What's different**: No separate fulltext endpoint access needed
- **What's the same**: Like APS, need to handle redirects properly
- **Implementation**: Playwright automatically handles cookies, so no manual OAuth needed

---

## 6. DESIGN RECOMMENDATIONS FOR NatureHandler

### Class Structure

```python
class NatureHandler(PublisherHandler):
    """Handler for Nature/Springer Nature journals"""
    
    def __init__(self, journal_name: str = 'nature'):
        self.journal_name = journal_name
        self.base_url = "https://www.nature.com"
    
    async def extract_metadata(self, page) -> dict:
        """Extract from meta tags and JSON-LD"""
        # Query meta tags
        # Parse JSON-LD
        # Extract from HTML
        
    async def get_pdf_url(self, page) -> str:
        """Find PDF download button/link in page"""
        
    async def get_figures(self, page) -> dict:
        """Extract figure URLs from HTML img tags"""
        
    async def extract_references(self, page) -> list:
        """Parse reference list from HTML"""
```

### Key Implementation Differences

| Component | APS | Nature |
|-----------|-----|--------|
| **Metadata** | API response JSON | Meta tags + JSON-LD |
| **Content** | Structured JSON components | HTML DOM |
| **Figures** | asset.variants URLs | HTML img src attributes |
| **Conversion** | JSON traversal | HTML to markdown (pypandoc) |
| **PDF URL** | Constructed from DOI | Find download button |
| **Supplementary** | JSON supplemental endpoint | HTML links |

---

## 7. IMPLEMENTATION CHECKLIST

### Phase 1: Basic Handler
- [ ] Create `publisher/nature.py` with NatureHandler class
- [ ] Implement `extract_metadata()` using meta tag queries
- [ ] Implement `get_pdf_url()` by finding download link
- [ ] Implement `get_figures()` by querying HTML img tags

### Phase 2: Content Conversion  
- [ ] Implement `extract_references()` from HTML list
- [ ] Implement `convert_to_markdown()` using pypandoc on article HTML
- [ ] Handle formula elements (MathJax SPAN → LaTeX)
- [ ] Extract abstract from JSON-LD

### Phase 3: Testing
- [ ] Test with 10.1038/s41586-026-10400-2 (this paper)
- [ ] Test with additional Nature papers
- [ ] Verify metadata extraction accuracy
- [ ] Compare markdown quality vs APS

### Phase 4: Integration
- [ ] Register NatureHandler in publisher factory
- [ ] Update `complete_paper_extraction.py` to detect Nature URLs
- [ ] Update CLI to support `--publisher nature` flag

---

## 8. API ENDPOINTS FOR FUTURE EXPANSION

### Known Working Endpoints
- `/articles/{article_id}` - Main article page (HTML)
- `/platform/contextual?doi={doi}` - Metadata endpoint (JSON)
- `idp.nature.com/authorize` - Authentication
- `media.springernature.com` - Figure CDN

### PDF Access Strategy
Need to identify:
- [ ] PDF download URL pattern (may require clicking button in browser)
- [ ] Whether PDF available to unauthenticated users
- [ ] Supplementary materials download URLs

---

## 9. COMPARISON: APS vs Nature

```
ARCHITECTURE COMPARISON

APS (Physics Journals)
├── RESTful API for fulltext
├── Structured JSON responses  
├── asset.variants for figures
├── Direct PDF URL from pattern
└── No authentication needed

Nature (Springer)
├── Traditional web page (HTML)
├── Meta tags + JSON-LD for metadata
├── Figures as img elements
├── PDF via download button/link
└── OAuth-based authentication required
```

---

## 10. MIGRATION PATH

### From APS-Only to Multi-Publisher

1. **Current state**: `complete_paper_extraction.py` assumes APS
2. **Step 1**: Publisher detection in `detect_publisher_from_url()`
3. **Step 2**: Conditional handler instantiation
4. **Step 3**: Add NatureHandler alongside APSHandler
5. **Step 4**: Test with both publishers
6. **Future**: Add more publishers (Elsevier, Science, etc.)

### Code Pattern

```python
async def complete_extraction_workflow(doi: str):
    # Detect publisher from URL
    publisher = await detect_publisher(doi)  # Returns 'aps', 'nature', etc.
    
    # Get appropriate handler
    if publisher == 'aps':
        handler = APSHandler(journal_prefix)
    elif publisher == 'nature':
        handler = NatureHandler()
    else:
        raise ValueError(f"Unsupported publisher: {publisher}")
    
    # Use handler polymorphically
    metadata = await handler.extract_metadata(page)
    figures = await handler.get_figures(page)
    ...
```

---

## 11. OBSERVATIONS

### Metadata Completeness
- ✅ **Meta tags**: Complete title, DOI, journal, date, first author
- ✅ **JSON-LD**: Contains full abstract and description  
- ✅ **HTML DOM**: Authors with ORCID, figures with captions, references
- ⚠️ **Multiple Authors**: Only first author in meta tag, need HTML extraction
- ⚠️ **Author Emails**: Not found in accessible areas, may be restricted

### Formula Handling
- Nature uses MathJax for rendering formulas
- 38 formula elements detected
- Need to convert SPAN math elements to LaTeX for markdown

### Accessibility
- ✅ Public article page accessible without login
- ✅ Metadata available via meta tags
- ⚠️ Full text may have access restrictions
- ⚠️ PDF download button may require interaction

---

## Next Steps

1. **Immediate**: Start implementing `publisher/nature.py`
2. **Short-term**: Test with multiple Nature papers
3. **Medium-term**: Add other Springer journals
4. **Long-term**: Generalize to other publishers (Cell, Science, etc.)

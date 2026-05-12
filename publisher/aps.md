# APS Journal API Documentation

## Overview

This document describes the API structure and endpoints used by American Physical Society (APS) journals for paper extraction.

## Journal Codes

- `prl` - Physical Review Letters
- `pre` - Physical Review E  
- `pra` - Physical Review A
- `prb` - Physical Review B
- `prc` - Physical Review C
- `prd` - Physical Review D
- `prresearch` - Physical Review Research
- `revmodphys` - Reviews of Modern Physics

## API Endpoints

### 1. Abstract Page
**URL**: `https://journals.aps.org/{journal}/abstract/{doi}`

**Response Type**: HTML with embedded JSON

**Contains**:
- Paper title
- Author list  
- Author affiliations
- Abstract text
- Reference list (partial)
- Link to fulltext API
- Link to supplemental materials (if available)

**Extraction Points**:
- `<meta name="citation_title">` - Paper title
- `<meta name="citation_author">` - Authors (multiple)
- `<meta name="citation_publication_date">` - Publication date
- `<meta name="citation_journal_title">` - Journal name
- `<meta name="citation_volume">` - Volume
- `<meta name="citation_issue">` - Issue
- `<meta name="citation_firstpage">`, `citation_lastpage` - Pages
- `<meta name="description">` or `<meta property="og:description">` - Abstract
- `<ol class="references">` - Reference list HTML

---

### 2. Fulltext API
**URL**: `https://journals.aps.org/{journal}/fulltext/{doi}`

**Response Type**: JSON (via XHR/Fetch)

**Contains**:
- Article text in structured JSON (nested components)
- MathML formulas
- Figure captions
- Section headers (Introduction, Methods, Results, etc.)

**Key JSON Structure**:
```json
{
  "body": "<html content>",
  "components": [
    {
      "type": "p",
      "klass": "article-fulltext-paragraph",
      "body": "Paragraph HTML with MathML"
    },
    {
      "type": "fig",
      "id": "fig1",
      "body": "Figure HTML",
      "components": [
        {
          "type": "fig-caption",
          "body": "Caption text with MathML"
        }
      ]
    }
  ]
}
```

**Component Types**:
- `p` - Paragraph
- `h1`, `h2`, `h3` - Headers
- `fig` - Figure
- `fig-caption` - Figure caption
- `front` - Front matter
- `back` - Back matter

---

### 3. Supplemental Materials Page
**URL**: `https://journals.aps.org/{journal}/supplemental/{doi}`

**Response Type**: HTML

**Contains**:
- List of supplemental files
- File descriptions
- Download links

**Extraction Points**:
- Supplemental file links and descriptions
- Material type (movie, data, code, etc.)

---

### 4. Figure Images
**URL**: `https://journals.aps.org/{journal}/article/{doi}/figures/{n}/large`

**Response Type**: Image (PNG/JPG)

**Parameters**:
- `{n}` - Figure number (1, 2, 3, ...)
- `large` - Resolution variant (can be `medium`, `small`, `large`)

**Note**: Default resolution is medium. Use `large` for high-resolution figures.

---

### 5. PDF Download
**URL**: `https://journals.aps.org/{journal}/pdf/{doi}`

**Response Type**: PDF binary

**Features**:
- Complete paper PDF
- Usually includes all figures and appendices
- Retains formatting and equations

---

## Extraction Workflow

```
1. Navigate to abstract page: /abstract/{doi}
   ↓
   Extract metadata from HTML meta tags
   Extract reference list from <ol class="references">
   Capture fulltext API URL from network requests
   
2. Monitor network requests while page loads
   ↓
   Intercept XHR/Fetch to /fulltext/{doi}
   Extract JSON response containing article structure
   
3. Parse JSON recursively
   ↓
   Extract article text from component bodies
   Convert MathML to LaTeX
   Extract figure captions and references
   
4. Download resources
   ↓
   Get high-res figures from /figures/{n}/large
   Download PDF from /pdf/{doi}
   Download supplemental materials from /supplemental/{doi}
```

## Key Implementation Notes

### Citation Meta Tags
All APS papers include standard citation meta tags in the abstract page HTML:
```html
<meta name="citation_title" content="...">
<meta name="citation_author" content="...">
<meta name="citation_journal_title" content="...">
<meta name="citation_volume" content="...">
<meta name="citation_issue" content="...">
<meta name="citation_publication_date" content="...">
<meta name="citation_abstract" content="...">
<meta name="citation_doi" content="...">
```

### MathML Formula Handling
- Fulltext API returns formulas as MathML
- Convert using pandoc: `mathml → LaTeX`
- Clean up unsupported commands like `\mspace`

### Figure Resolution
- Default figures in fulltext JSON: medium resolution
- For higher quality: append `/large` to figure URL
- Maintains aspect ratio across resolutions

### Network Listening Strategy
1. Start listening before navigating to abstract page
2. The fulltext API call happens automatically as page renders
3. Capture the JSON response containing complete article text
4. This is more efficient than parsing HTML directly

---

## Error Handling

| Error | Cause | Solution |
|-------|-------|----------|
| No fulltext JSON | Paper might be preview-only | Try PDF download |
| Missing figures | Figure resolution issue | Try alternate `/medium` or `/small` |
| Supplemental not available | Not all papers have supplemental | Check if link exists first |
| Citation meta tags missing | Non-standard page format | Fall back to HTML parsing |

---

## Performance Metrics

- Abstract page load: ~2-3 seconds
- Fulltext API call: ~1-2 seconds  
- Figure download (large): ~0.5-1 second each
- PDF download: ~3-10 seconds (depends on file size)
- Total per paper: ~1-3 minutes (including waits)

---

## Browser Requirements

- JavaScript enabled (for fulltext API call)
- Capable of handling MathML
- Support for Fetch/XMLHttpRequest (network capture)
- Session/cookie management for authenticated content

---

## References

- [APS Journals Home](https://journals.aps.org)
- [APS Journal List](https://journals.aps.org/browse/)
- [DOI System](https://www.doi.org/)


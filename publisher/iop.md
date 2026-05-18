# IOP Publishing Handler (`iop.py`) — Extraction Logic

## Overview

`IOPHandler` extracts article content from IOP Science pages (`iopscience.iop.org`). The key challenge is that IOP stores LaTeX directly in `<script type="math/tex">` tags (not MathML), requiring a dedicated preprocessing pass before the standard HTML→Markdown pipeline.

## 1. Metadata Extraction

**Method**: `_extract_metadata_from_html_meta(html_content) → dict`

Iterates all `<meta>` tags and maps `name` → `content`:

| Meta tag | Metadata key |
|---|---|
| `citation_title` | `title` |
| `citation_doi` | `doi` |
| `citation_journal_title` | `journal` |
| `citation_volume` | `volume` |
| `citation_issue` / `citation_number` | `issue` |
| `citation_firstpage` | `pages` |
| `citation_publication_date` | `publication_date`, `year` |
| `citation_pdf_url` | `pdf_url` |
| `citation_abstract` | `abstract` |
| `citation_keywords` | `keywords` |
| `citation_author` (multiple) | `authors[]` |

`extract_metadata()` wraps this, adding abstract fallback from DOM if no `<meta>` abstract is available.

## 2. HTML Preprocessing (Math + GIF)

**Method**: `_preprocess_iop_html(html_fragment) → (processed_html, display_eqns_dict)`

Runs *before* any HTML→Markdown conversion. Four steps:

1. **Display math extraction** — `<script type="math/tex; mode=display">` content is extracted into `<<<IOP_DISPLAY_MATH_N>>>` placeholders. `\tag{...}` equation numbers are preserved.

2. **Inline math** — `<script type="math/tex">` is converted to `$...$` inline.

3. **MathJax cruft removal** — Hidden `<span class="texImage">` (base64 PNG wrappers) are stripped. Outer `<span class="inline-eqn"><span class="tex">$...$</span></span>` is unwrapped to just `$...$`.

4. **GIF epsilon → LaTeX** — The GIF entity URL `cdn.images.iop.org/Entities/epsi.gif` is replaced with `\epsilon`. A compound `<em><img ... epsi.gif .../><sub>r</sub></em>` pattern becomes `\epsilon_{\text{r}}`.

## 3. Body Text Extraction

**Entry point**: `extract_article_text_from_html(html_content) → (abstract_md, body_md)`

1. Abstract is extracted via `extract_abstract_with_fallbacks()` from `wildcard.py`.
2. The article body `<div>` is located via `find_generic_article_body()` from `wildcard.py`.
3. Direct children of the body div are iterated (`h2`–`h4`, `p`, `div`, `figure`, `table`).

For each child element:

- **Headings (`h2`–`h4`)**: converted to `##`–`####` markdown.
- **Paragraphs (`<p>`)**: run through `_convert_iop_paragraph_to_md()`.
- **Figures (`<figure class="boxout">`)**: caption is extracted and added as `**Figure N.** caption` inline placeholder.
- **Display equations (`<div class="display-eqn">`)**: content from `<script type="math/tex; mode=display">` is extracted and wrapped in `$$...$$`.
- **Nested `div.article-text`**: delegated to `_walk_iop_body()`.
- **Tables (`<table data-toolbar-type="table">`)**: delegated to `_table_element_to_md()`.

### Nested Container Walker: `_walk_iop_body(container, body_parts)`

IOP sometimes nests content inside multiple `div.article-text` layers. This recursive method handles arbitrary nesting depth, processing `p`, `h3`–`h4`, `div`, `figure`, and `table` elements. For `div.boxout` (which wraps tables with descriptions), it finds the table and its description text, then calls `_table_element_to_md(table, description=desc)`.

### Paragraph Conversion Pipeline: `_convert_iop_paragraph_to_md(html_fragment) → str`

1. Run `_preprocess_iop_html()` → extracts math into placeholders, removes cruft.
2. Run `convert_html_fragment_to_markdown()` (from `wildcard.py`) → pandoc-based HTML→MD.
3. Restore display equations from placeholders back into `$$...$$`.
4. Collapse excess blank lines.

## 4. Figure Extraction

**Method**: `extract_figures_from_html(html_content) → dict`

Finds `<figure data-toolbar-type="figure">` elements. For each:

1. **Image URL**: prefers `a.fig-dwnld-hi-img` (high-res), falls back to `a.fig-dwnld-std-img`, falls back to `<img>` data-src.
2. **Caption**: extracted from `div.figure-caption > p`.
3. Returns `{ "fig_N": {"url": "...", "caption": "..."} }`.

Also has a generic img-src fallback for `dae_*_hr.*` URL patterns.

## 5. Table Extraction

**Methods**: `_process_table_cell(cell_html) → str`, `_table_element_to_md(table_element, description) → str`, `extract_tables_from_html(html_content) → list`

### Table Cell Processing

`_process_table_cell()` treats each cell as a mini-HTML fragment, running it through `_preprocess_iop_html()` + `convert_html_fragment_to_markdown()`. This ensures `<script type="math/tex">`, GIF epsilons, `<sub>`, `<sup>`, `<i>` are all preserved in table cells — the same pipeline as body paragraphs.

### Table to Markdown

`_table_element_to_md()`:

1. **Title**: from `data-toolbar-title` attribute (or fallback to preceding `<strong>`).
2. **Header row**: `<thead>` → `<th>` cells → Markdown pipe-row + separator row.
3. **Body rows**: `<tbody>` → `<tr>` → `<td>`/`<th>` cells. Uses `_process_table_cell()` for each.
4. Returns `**Title — description**\n| ... |` or empty string if no data rows.

### Table Description from Boxout

When inside `<div class="boxout">`, the leading `<p>` is parsed: the `<b>` label text is stripped, and the remainder becomes the table description (appended after `—` in the heading).

## 6. Reference Extraction

**Methods**: `extract_references_from_html(html_content) → list`, `_extract_raw_citation_references(html_content) → list`

IOP stores references as `<meta name="citation_reference" content="key=value; key=value; ...">` tags. Each tag is parsed by `parse_citation_reference_string()` from `wildcard.py`, which splits on `;` and builds a BibTeX entry via `format_as_bibtex()`. The raw strings are also kept in `_refs_raw` for numbered citation formatting in the markdown output.

## 7. Footnote Extraction

**Method**: `extract_footnotes_from_html(html_content) → list`

Finds `<h2 id="footnotes">`, then the following `div > ul.clear-list.wd-content-footnotes > li.indices-list`. For each:

1. `.indices-id` → footnote number.
2. `.indices-content > p` → run through `_convert_iop_paragraph_to_md()` to preserve formulas.
3. Returns `["N. footnote text...", ...]`

## 8. Supplemental Material Extraction

**Methods**: `_extract_supplemental_links_from_html(html_content) → list`, `_extract_supplementary_from_data_page(page, doi) → (urls, descriptions)`

Two strategies:

1. **From article page**: scans `<a href>` for links containing `/data`.
2. **From `/data` endpoint**: navigates to `https://iopscience.iop.org/article/{doi}/data`, then runs a JS snippet: `document.querySelector('#supplementarydata').querySelectorAll('a.link--decoration-none')`. Returns resolved URLs and link text.

## 9. Markdown Assembly

**Method**: `convert_to_markdown(metadata, article_text, **kwargs) → str`

Assembles sections in order:

1. **Title** (`# Title`)
2. **Authors** (with optional affiliations)
3. **DOI**
4. **Publication** info (journal, volume, issue, pages, date)
5. **Abstract**
6. **Article Text** (body markdown, with figure image placeholders inserted after captions if `add_figure_refs` and `figure_filenames` kwargs are provided)
7. **Supplemental Material** (download links)
8. **Footnotes** (numbered list with formulas)
9. **References** (numbered text + BibTeX code blocks)

## 10. Orchestration

**Method**: `extract_all(page, doi, captured) → dict`

The main entry point called by the publisher orchestrator:

1. If no page is provided, launches its own headless Chromium session.
2. Extracts metadata via `extract_metadata()`.
3. Captures full page HTML.
4. Extracts references, raw references, and footnotes from HTML.
5. Extracts figures and tables from HTML.
6. Navigates to `/data` endpoint for supplementary material links.
7. Returns unified dict `{metadata, links, fulltext_data, journal_name}`.

"""
Generic utility functions for paper extraction
These functions are publisher-agnostic and can be reused across different publishers
"""

import asyncio
import json
import os
import re
import requests
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

# ============================================================================
# Semantic Scholar API Configuration
# ============================================================================
S2_API_URL = "https://api.semanticscholar.org/graph/v1/paper/DOI:"

# ============================================================================
# Crossref API Configuration
# ============================================================================
CROSSREF_API_URL = "https://api.crossref.org/works"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json'
}


# ============================================================================
# Bounded waits for the two Playwright calls that have no timeout
# ============================================================================
# ``page.evaluate()`` and ``response.body()`` take no ``timeout=`` argument and
# are NOT governed by ``set_default_timeout``. They wait forever. Every other
# wait in this tree is bounded, so these two are the only places a blip can
# wedge the whole run -- and they sit on the hottest path there is: every
# figure, every in-page API fetch.
#
# Why that hangs instead of failing: a stalled TCP connection produces no
# error, so the in-page ``fetch()`` promise never settles, so ``evaluate``
# never returns. ``retry_download`` only retries on *exceptions*, which means
# a hang skips the retry ladder, the fallback-URL rung and the ``fresh`` rung
# alike. Nothing downstream ever runs. That is the difference between "this
# figure failed" (fine, we retry) and "the batch stopped at 3am" (not fine).
#
# Both layers below are needed, because each covers the other's blind spot:
#   in-page AbortController  aborts the request while the page still runs,
#                            and stops a dangling transfer from keeping the
#                            next goto()'s 'networkidle' from ever arriving
#   asyncio.wait_for         the real backstop -- it still fires when the
#                            renderer itself is wedged, which is exactly when
#                            no in-page timer can run

def env_seconds(name: str, default: float) -> float:
    """Read a positive float number of seconds from environment.

    Returns ``default`` if the var is unset, empty, or unparseable.
    """
    raw = os.environ.get(name, '').strip()
    if not raw:
        return float(default)
    try:
        val = float(raw)
        if val <= 0:
            return float(default)
        return val
    except ValueError:
        return float(default)


# Hard cap on any single "pull bytes out of the browser" call -- an in-page
# fetch or a response-body read. Generous on purpose: this is a deadlock
# breaker, not a performance knob. It should only ever fire on a connection
# that has genuinely stopped moving, never on one that is merely slow.
DP_INPAGE_FETCH_TIMEOUT = env_seconds('DP_INPAGE_FETCH_TIMEOUT', 90)


async def evaluate_with_timeout(page, expression, arg=None, *,
                                timeout_s: float = None, what: str = 'in-page fetch'):
    """``page.evaluate`` that cannot hang. Raises on timeout.

    The raise is deliberate and is what makes this a one-line change at every
    call site: each one already wraps its ``evaluate`` in ``except Exception``
    and degrades to a fallback, and ``retry_download`` already retries on
    exceptions. Returning a sentinel instead would mean teaching all of them a
    new failure shape.
    """
    budget = float(timeout_s if timeout_s else DP_INPAGE_FETCH_TIMEOUT)
    try:
        return await asyncio.wait_for(page.evaluate(expression, arg), timeout=budget)
    except asyncio.TimeoutError:
        print(f"    ⏱️  {what} 超过 {budget:g}s 未返回，判定为卡死并放弃")
        raise


async def read_body_with_timeout(response, *, timeout_s: float = None,
                                 what: str = '响应体') -> bytes:
    """``response.body()`` that cannot hang. Returns b'' instead of raising.

    Unlike :func:`evaluate_with_timeout`, every caller of this one already
    treats an empty body as "didn't work, move on", so b'' needs no new
    handling anywhere and keeps the timeout from aborting a loop that still
    has other files to fetch.
    """
    budget = float(timeout_s if timeout_s else DP_INPAGE_FETCH_TIMEOUT)
    try:
        return await asyncio.wait_for(response.body(), timeout=budget)
    except asyncio.TimeoutError:
        print(f"    ⏱️  读取{what}超过 {budget:g}s 未完成，判定为卡死并放弃")
        return b''
    except Exception as exc:
        print(f"    ⚠️  读取{what}失败（{type(exc).__name__}: {str(exc)[:80]}）")
        return b''


# The in-page half of the pair: hand ``signal: __dpAbort(ms)`` to fetch() and
# the browser aborts the request itself, which rejects the promise and lands in
# the snippet's own catch.
#
# ⚠️ This is a *statement*, so it must be spliced INSIDE the function body --
#    """async (u) => {""" + INPAGE_ABORT_JS + """ ...rest... }"""
#    Putting it in front of the arrow function instead makes the expression two
#    statements, and Playwright evaluates a non-function expression as an
#    expression: instant SyntaxError, on every call, for every publisher.
# ⚠️ Substitute the millisecond value with ``.replace('__MS__', ...)``, not
#    ``%``: these snippets are full of JS braces and a stray ``%`` in a future
#    one would turn into a formatting error nobody expects.
INPAGE_ABORT_JS = """
    const __dpAbort = (ms) => {
        const c = new AbortController();
        setTimeout(() => c.abort(), ms);
        return c.signal;
    };
"""


def inpage_abort_ms() -> str:
    """The millisecond budget to splice into an in-page ``__dpAbort`` call."""
    return str(int(DP_INPAGE_FETCH_TIMEOUT * 1000))


# ============================================================================
# The fetch ladder
# ============================================================================
# Three rungs, one order for every kind of fetch -- API pages, the PDF,
# figures, supplemental files:
#
#   request  plain HTTP carrying the article session's cookies
#   tab      a new tab in the browser already holding the article
#   fresh    a throwaway Chrome seeded from the real profile
#
# Each kind names the rung it *starts* at; a failure falls through to the ones
# below. This lives here rather than in complete_paper_extraction because
# publisher handlers need it too: the dependency runs one way (main module ->
# publisher), and a handler importing the main module would be the first cycle
# in the tree.

# 'referer' is the last rung and only applies to downloads: a throwaway Chrome
# opens the referring page (the article), then reaches the file by a trusted
# click from it. That is the only way to send a Referer *and* keep
# Sec-Fetch-User: ?1 -- setting the header via CDP produces a request that
# claims a referrer with no user activation behind it, which is a worse tell
# than sending none at all. Measured, not assumed; see
# chrome_session._download_via_referer_click.
FETCH_TIERS = ('request', 'tab', 'fresh', 'referer')
FETCH_KINDS = ('api', 'pdf', 'figure', 'supplement')

DP_HTTP_USER_AGENT = os.environ.get(
    'DP_HTTP_USER_AGENT',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36',
)
# DP_HTTP_FIRST=0 skips the plain request and goes straight to the browser.
DP_HTTP_FIRST = os.environ.get('DP_HTTP_FIRST', '1').strip().lower() not in (
    '0', 'false', 'no', 'off')


def http_asset_headers(referer: str = None) -> dict:
    headers = {
        'User-Agent': DP_HTTP_USER_AGENT,
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    if referer and referer.startswith('http'):
        headers['Referer'] = referer
    return headers


def fresh_chrome_enabled() -> bool:
    """Whether the throwaway-Chrome rung may be used at all."""
    if os.environ.get('DP_PDF_FRESH_CHROME', '1').strip().lower() in (
            '0', 'false', 'no', 'off'):
        return False
    try:
        from chrome_session import open_url_in_fresh_chrome  # noqa: F401
    except ImportError:
        return False
    return True


def fetch_ladder(kind: str, default: tuple = ('tab', 'fresh')) -> tuple:
    """Rungs to try for *kind*, in order.

    ``DP_FETCH_<KIND>`` overrides ``DP_FETCH_ORDER`` overrides *default*. An
    unrecognised value is ignored rather than fatal -- a typo in a launch
    script should not stop a download.

    A configured value truncates: ``DP_FETCH_PDF=fresh`` means *only* the
    throwaway Chrome, which is what someone naming a rung explicitly wants.
    *default* is a full order instead, so a caller can express a preference
    that is not a prefix of request/tab/fresh -- a headed PDF wants
    ('fresh', 'tab', 'referer'), and truncation could not say that.
    """
    start = (os.environ.get(f'DP_FETCH_{kind.upper()}')
             or os.environ.get('DP_FETCH_ORDER')
             or '').strip().lower()
    if start in FETCH_TIERS:
        # DP_HTTP_FIRST=0 predates this and says exactly "skip the plain
        # request".
        if start == 'request' and not DP_HTTP_FIRST:
            start = 'tab'
        tiers = FETCH_TIERS[FETCH_TIERS.index(start):]
    else:
        tiers = tuple(default)
    if not fresh_chrome_enabled():
        tiers = tuple(t for t in tiers if t != 'fresh')
    return tiers or ('tab',)


async def cookies_for_requests(url: str, context=None, page=None) -> dict:
    """Cookies from the live browser session, scoped to *url*'s host.

    This is what makes the 'request' rung worth attempting: without them a
    publisher that gated the article behind a login serves an asset request a
    login page instead, which the content check then rejects -- a wasted round
    trip every time.

    ⚠️ Cookies are not sufficient against a host that is actively challenging.
    A Cloudflare clearance cookie is bound to the user agent, IP and TLS
    fingerprint of the browser that earned it, and ``requests`` matches none of
    those, so such hosts still fall through to the browser rungs. That is the
    ladder working as intended, not a bug to chase.
    """
    ctx = context
    if ctx is None and page is not None:
        ctx = getattr(page, 'context', None)
    if ctx is None:
        return {}
    try:
        raw = await ctx.cookies()
    except Exception:
        return {}

    host = (urlparse(url).hostname or '').lower()
    jar = {}
    for cookie in raw or []:
        name = cookie.get('name')
        value = cookie.get('value')
        if not name or value is None:
            continue
        # Send only what this host is entitled to; a flat dump of the jar
        # would leak one publisher's session to another's CDN.
        domain = (cookie.get('domain') or '').lstrip('.').lower()
        if domain and not (host == domain or host.endswith('.' + domain)):
            continue
        jar[name] = value
    return jar


def _html_is_acceptable(html: str, expect) -> bool:
    """Whether *html* is the page we asked for rather than something else.

    The check is the whole reason the ladder can use the browser rungs safely.
    A challenge page, a login wall and a 404 are all perfectly valid HTML, so
    "non-empty" proves nothing -- without *expect*, a Cloudflare interstitial
    would be parsed for links, yield none, and be reported as "0 files found"
    instead of falling through to the next rung.
    """
    if not html or len(html) < 200:
        return False
    if expect is None:
        return True
    if callable(expect):
        try:
            return bool(expect(html))
        except Exception:
            return False
    return str(expect) in html


async def fetch_html_via_ladder(url: str, *, kind: str = 'api', page=None,
                                context=None, referer: str = None,
                                expect=None, timeout_s: float = 30.0,
                                restore_url: str = None,
                                headless: bool = True) -> str:
    """Fetch *url* as HTML, walking the ladder until something usable comes back.

    *expect* is a substring or a predicate identifying the page we wanted; see
    :func:`_html_is_acceptable` for why it matters. Returns '' when every rung
    failed, so the caller can report honestly rather than parse a challenge
    page.

    *restore_url* is navigated back to after the 'tab' rung, because that rung
    drives the caller's own page: leaving the shared article tab parked on a
    supplemental listing is how metadata ends up recording the wrong URL.

    ⚠️ *headless* must be given the mode the run is actually in, which comes
    from the state of the page the handler was started on --
    ``headless=not handler.is_headed_run()``. The whole point of the bottom
    rung is to present a browser that has never been automated; launching it
    headless during a headed run throws that away and walks straight into a
    bot check (observed on IOP's /data page, which answered with a Radware
    captcha).

    The default is True so that forgetting to pass it cannot pop a window
    during a headless batch -- the quieter of the two wrong answers. It is
    still a wrong answer on a headed run, so pass it explicitly.
    """
    tiers = fetch_ladder(kind)

    for tier in tiers:
        if tier == 'request':
            jar = await cookies_for_requests(url, context=context, page=page)
            try:
                resp = await asyncio.to_thread(
                    requests.get, url,
                    headers=http_asset_headers(referer),
                    cookies=jar or None,
                    timeout=(15, timeout_s),
                    allow_redirects=True,
                )
                if resp.status_code < 400 and _html_is_acceptable(resp.text,
                                                                 expect):
                    print(f"    ✓ 取得页面 [直接请求] {len(resp.text):,} 字符")
                    return resp.text
                print(f"    ↪ 直接请求不可用 (HTTP {resp.status_code})，下一层")
            except Exception as exc:
                print(f"    ↪ 直接请求失败（{type(exc).__name__}），下一层")

        elif tier == 'tab':
            if page is None:
                continue
            back_to = restore_url or getattr(page, 'url', '') or ''
            try:
                try:
                    await page.goto(url, wait_until='networkidle',
                                    timeout=int(timeout_s * 1000))
                except Exception:
                    await page.goto(url, wait_until='domcontentloaded',
                                    timeout=int(timeout_s * 1000))
                html = await page.content()
                if _html_is_acceptable(html, expect):
                    print(f"    ✓ 取得页面 [浏览器标签页] {len(html):,} 字符")
                    return html
                print("    ↪ 标签页拿到的不是目标页面，下一层")
            except Exception as exc:
                print(f"    ↪ 标签页访问失败（{type(exc).__name__}），下一层")
            finally:
                if back_to:
                    try:
                        await page.goto(back_to,
                                        wait_until='domcontentloaded',
                                        timeout=15000)
                    except Exception:
                        pass

        elif tier == 'fresh':
            session = None
            try:
                from chrome_session import open_url_in_fresh_chrome
                session = await open_url_in_fresh_chrome(
                    url, timeout_s=int(timeout_s), headless=headless,
                    want_html=True)
                html = (session.result or {}).get('html') or ''
                if _html_is_acceptable(html, expect):
                    print(f"    ✓ 取得页面 [一次性 Chrome] {len(html):,} 字符")
                    return html
                print("    ↪ 一次性 Chrome 未取得目标页面")
            except Exception as exc:
                print(f"    ↪ 一次性 Chrome 失败（{type(exc).__name__}）")
            finally:
                if session is not None:
                    try:
                        await session.close()
                    except Exception:
                        pass

    return ''


# ============================================================================
# API Functions
# ============================================================================

def fetch_semanticscholar(doi: str) -> dict:
    """Fetch paper metadata from Semantic Scholar API"""
    s2_fields = 'title,year,venue,authors'
    try:
        s2_res = requests.get(
            f"{S2_API_URL}{doi}",
            params={'fields': s2_fields},
            headers=HEADERS,
            timeout=15
        )
        if s2_res.status_code == 200:
            data = s2_res.json() or {}
            if data:
                print(f"  ✓ Semantic Scholar: {data.get('title', 'N/A')[:50]}... ({data.get('year', 'N/A')})")
            return data
    except Exception as e:
        print(f"  ⚠️  Semantic Scholar exception {doi}: {e}")
    return {}


def fetch_crossref(doi: str) -> dict:
    """Fetch paper metadata from Crossref API

    Extracts: publisher, publication date (year/month/day), authors, ISBN, ISSN, references

    Returns dict with keys:
        - title
        - authors (list of dicts with 'name', 'given', 'family')
        - publisher
        - year (publication year)
        - date_parts ([year, month, day])
        - isbn (list)
        - issn (list)
        - references (list of reference objects)
        - volume, issue, pages
        - journal (container-title)
    """
    try:
        url = f"{CROSSREF_API_URL}/{doi}"
        response = requests.get(url, headers=HEADERS, timeout=15)

        if response.status_code == 200:
            data = response.json()
            work = data.get('message', {})

            if not work:
                return {}

            # Extract key information
            result = {
                'title': work.get('title', [''])[0] if work.get('title') else '',
                'type': work.get('type', ''),
                'publisher': work.get('publisher', ''),
                'authors': [],
                'year': None,
                'date_parts': None,
                'isbn': work.get('ISBN', []),
                'issn': work.get('ISSN', []),
                'references': work.get('reference', []),
                'volume': work.get('volume'),
                'issue': work.get('issue'),
                'pages': work.get('page'),
                'journal': work.get('container-title', [''])[0] if work.get('container-title') else '',
                'doi': work.get('DOI', doi),
            }

            # Extract authors
            if work.get('author'):
                for author in work['author']:
                    result['authors'].append({
                        'name': f"{author.get('given', '')} {author.get('family', '')}".strip(),
                        'given': author.get('given', ''),
                        'family': author.get('family', ''),
                    })

            # Extract publication date
            if work.get('published-online'):
                date_parts = work['published-online'].get('date-parts', [])
                if date_parts and date_parts[0]:
                    result['date_parts'] = date_parts[0]
                    result['year'] = date_parts[0][0] if date_parts[0] else None

            # Fallback to issued date if published-online not available
            if not result['year'] and work.get('issued'):
                date_parts = work['issued'].get('date-parts', [])
                if date_parts and date_parts[0]:
                    result['date_parts'] = date_parts[0]
                    result['year'] = date_parts[0][0] if date_parts[0] else None

            # Keep the untouched API response alongside the parsed subset.
            # save_crossref_json() writes it out once the paper directory
            # exists; everything above is a lossy projection of it, and the
            # fields this project does not read today are exactly the ones a
            # later question tends to need.
            result['_raw_response'] = data

            if result['title']:
                print(f"  ✓ Crossref: {result['title'][:50]}... ({result['year'] or 'N/A'})")

            return result
        else:
            print(f"  ⚠️  Crossref API error {response.status_code} for DOI {doi}")
            return {}

    except Exception as e:
        print(f"  ⚠️  Crossref exception {doi}: {e}")
        return {}


# ============================================================================
# File Organization Functions
# ============================================================================

def _clean_title_for_directory(title: str) -> str:
    """Clean title for use as directory name, handling formulas and HTML tags."""
    if not title:
        return 'paper'

    # Remove HTML/XML tags (including MathML and other markup)
    # Add space before removing tag to preserve word boundaries
    title = re.sub(r'<[^>]+>', ' ', title)

    # Replace common mathematical symbols with text representations
    replacements = {
        '−': '-',           # minus sign (U+2212) -> ASCII hyphen-minus
        '±': 'pm',          # plus-minus
        '×': 'x',           # multiplication
        '÷': 'div',         # division
        '≈': 'approx',      # approximately equal
        '≠': 'ne',          # not equal
        '≤': 'le',          # less than or equal
        '≥': 'ge',          # greater than or equal
        '→': 'to',          # arrow
        '←': 'from',        # left arrow
        '↔': 'iff',         # bidirectional arrow
        'α': 'alpha',
        'β': 'beta',
        'γ': 'gamma',
        'δ': 'delta',
        'ε': 'epsilon',
        'ζ': 'zeta',
        'η': 'eta',
        'θ': 'theta',
        'λ': 'lambda',
        'μ': 'mu',
        'ν': 'nu',
        'π': 'pi',
        'ρ': 'rho',
        'σ': 'sigma',
        'τ': 'tau',
        'φ': 'phi',
        'χ': 'chi',
        'ψ': 'psi',
        'ω': 'omega',
        'Ω': 'Omega',
        'Σ': 'Sigma',
        '∫': 'integral',
        '∂': 'partial',
        '∇': 'nabla',
        '∞': 'infinity',
    }

    for symbol, name in replacements.items():
        title = title.replace(symbol, f' {name} ')

    # Remove remaining problematic characters for filenames
    # Keep only alphanumeric, spaces, hyphens, underscores, and parentheses
    title = re.sub(r'[/\\:*?"<>|]', '', title)

    # Collapse multiple spaces and trim
    title = re.sub(r'\s+', ' ', title).strip()

    # Limit length but keep it readable
    title = title[:150].strip()

    # If title becomes empty after cleaning, use placeholder
    if not title:
        title = 'paper'

    return title



def organize_paper_output(output_dir: Path, metadata: dict, s2_data: dict) -> Path:
    """
    Create organized paper directory structure
    Format: {year}--{title}/
    Returns the new output directory

    Priority: metadata (from handler) > s2_data (from Semantic Scholar)
    This ensures that for books/chapters, we use the correct extracted title, not cached S2 data
    Handles mathematical formulas and HTML tags in titles gracefully.
    """
    try:
        # Prioritize metadata from handler over s2_data
        year = metadata.get('year') or s2_data.get('year') or '0000'
        title = metadata.get('title') or s2_data.get('title') or 'paper'

        # Safety check: ensure year and title are strings
        if not isinstance(year, str):
            year = str(year) if year else '0000'
        if not isinstance(title, str):
            title = str(title) if title else 'paper'

        # Clean title: remove HTML tags and convert math symbols
        title_clean = _clean_title_for_directory(title)

        # Create directory: {year}--{title}
        dir_name = f"{year}--{title_clean}"
        paper_dir = output_dir / dir_name
        paper_dir.mkdir(parents=True, exist_ok=True)

        print(f"  📁 Created paper directory: {dir_name}/")
        return paper_dir
    except Exception as e:
        print(f"  ⚠️  Failed to create directory: {e}")
        import traceback
        traceback.print_exc()
        return output_dir


def save_crossref_json(paper_dir: Path, crossref_data: dict) -> Path:
    """Write the Crossref response to ``<paper_dir>/crossref.json``.

    Saves the raw API payload when fetch_crossref() carried it through
    (``_raw_response``); falls back to the parsed dict for callers that built
    the data some other way. Returns the path written, or None when there was
    nothing to write.
    """
    if not crossref_data:
        return None
    try:
        payload = crossref_data.get('_raw_response') or {
            k: v for k, v in crossref_data.items() if k != '_raw_response'
        }
        if not payload:
            return None
        paper_dir.mkdir(parents=True, exist_ok=True)
        json_file = paper_dir / 'crossref.json'
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"  ✓ Crossref response saved: {json_file.name}")
        return json_file
    except Exception as e:
        print(f"  ⚠️  保存 crossref.json 失败: {e}")
        return None


def save_metadata_json(paper_dir: Path, metadata: dict, s2_data: dict, doi: str,
                      pdf_filename: str = None, supplemental_files: list = None,
                      link: str = None, pdf_link: str = None):
    """Save paper metadata as JSON file

    Args:
        link: The final URL the browser landed on after resolving the DOI
              (or the explicit ``link`` from the --json input). Useful to
              cache the direct publisher URL so future runs can bypass
              doi.org (see --json mode).
        pdf_link: The URL the PDF was downloaded from. Recorded alongside the
              local filename so a paper whose PDF failed can be retried, and
              so the source is traceable without re-running extraction.
    """
    try:
        year = s2_data.get('year') or metadata.get('year') or '0000'
        title = s2_data.get('title') or metadata.get('title') or 'paper'

        # Prefer explicit link, fall back to whatever the handler stashed on
        # metadata['_landing_url'] during extraction (see process_with_handler).
        resolved_link = (link
                         or metadata.get('_landing_url')
                         or metadata.get('fulltext_url')
                         or '')

        metadata_json = {
            'doi': doi,
            'link': resolved_link,
            'title': title,
            'year': year,
            # Crossref's work type ("journal-article", "proceedings-article",
            # "book-chapter", …). It is what tells a conference paper apart
            # from a journal one after the fact, which the URL and the journal
            # name often do not.
            'type': s2_data.get('type') or metadata.get('type') or '',
            'authors': [item['author'] for item in metadata.get('author_with_affiliations', [])] or metadata.get('authors', []),
            'abstract': metadata.get('abstract', ''),
            'journal': metadata.get('journal', ''),
            'volume': metadata.get('volume'),
            'issue': metadata.get('issue'),
            'pages': metadata.get('pages'),
            'corresponding_author_emails': metadata.get('corresponding_author_emails', []),
            'extracted_at': datetime.now().isoformat(),
            'pdf': pdf_filename,
            'pdf_link': pdf_link or metadata.get('pdf_url') or '',
            'supplemental': supplemental_files if supplemental_files else []
        }

        # Add additional_doi field for books with multiple chapters
        if metadata.get('additional_doi'):
            metadata_json['additional_doi'] = metadata['additional_doi']

        # Add ISBN field if present
        if metadata.get('ISBN'):
            metadata_json['ISBN'] = metadata['ISBN']

        # Add ISSN field if present
        if metadata.get('ISSN'):
            metadata_json['ISSN'] = metadata['ISSN']

        # Save as metadata.json (canonical filename)
        json_file = paper_dir / "metadata.json"
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(metadata_json, f, ensure_ascii=False, indent=2)

        print(f"  ✓ Metadata saved: {json_file.name}")
        return json_file
    except Exception as e:
        print(f"  ⚠️  Failed to save metadata: {e}")
        return None


# ============================================================================
# Publisher Detection
# ============================================================================

# ============================================================================
# Playwright Helpers
# ============================================================================

async def block_mathjax(page) -> None:
    """Route-intercept MathJax script requests so they never execute.

    Many publishers (Cambridge, AIP, IOP, Optica, …) serve their full-text
    pages with original <math> MathML or \\(...\\) / \\[...\\] TeX delimiters.
    When MathJax runs in the browser it rewrites those into SVG/CHTML, which
    destroys the LaTeX source we want for the extracted markdown. Aborting
    the script requests up-front leaves the DOM untouched, so page.content()
    and the raw response listener both see the original math markup.

    Call this on every page **before** the first page.goto() — once a script
    has loaded, route handlers don't apply retroactively.
    """
    def _is_mathjax_url(url: str) -> bool:
        u = url.lower()
        return (
            'mathjax' in u                 # generic — covers cdn.mathjax.org, self-hosted MathJax-*.js
            or 'mml-chtml' in u            # MathJax 3 CHTML build
            or 'mml-svg' in u              # MathJax 3 SVG build
            or 'tex-mml' in u              # MathJax 3 TeX + MML build
            or 'polyfill.io' in u          # MathJax 3 ships with a polyfill.io bootstrap
        )

    async def _abort(route):
        try:
            await route.abort()
        except Exception:
            # Page may have been closed by the time the request arrives.
            pass

    try:
        await page.route(_is_mathjax_url, _abort)
    except Exception as e:
        # Don't let a broken interceptor block extraction — log and continue.
        print(f"  ⚠️  无法注册 MathJax 拦截器: {e}")


async def fetch_view_source_html(page, url: str = None, timeout_ms: int = 30000) -> str:
    """Re-fetch the current document and return its *unrendered* source.

    This is the programmatic equivalent of opening ``view-source:<url>``: the
    response body exactly as the server sent it, before MathJax (or any other
    script) rewrites the DOM. Publishers such as Optica ship display math as
    ``$$...$$`` TeX in the HTML source, but MathJax 4 replaces it with SVG
    whose only textual content is the accessibility *speech* string — which is
    how "P sub 0 equals E sub 0 divided by tau sub eff" ends up in the
    markdown instead of ``{P_0} = {E_0}/{\tau _{\rm{eff}}}``.

    Route-blocking MathJax (:func:`block_mathjax`) is the first line of
    defence, but it only helps when the interceptor is registered before the
    script loads — on CDP-attached headed pages, and after an in-handler
    navigation, that is not guaranteed. Re-fetching the source is
    unconditional and cannot be defeated by rendering.

    The fetch runs *inside the page* (``page.evaluate`` + ``fetch``) rather
    than through ``context.request``: same-origin credentials, and the real
    browser TLS/JS fingerprint, so publishers that 403 an out-of-page request
    (ScienceDirect, APS) serve it normally.

    Returns the HTML string, or ``''`` when the fetch fails or the response is
    implausibly small (the caller should keep whatever it already had).
    """
    target = url or getattr(page, 'url', '') or ''
    if not target:
        return ''
    try:
        html = await evaluate_with_timeout(
            page,
            ("""async (u) => {""" + INPAGE_ABORT_JS + """
                const r = await fetch(u, {
                    credentials: 'include',
                    signal: __dpAbort(__MS__),
                    headers: {'Accept': 'text/html,application/xhtml+xml'},
                });
                if (!r.ok) return '';
                return await r.text();
            }""").replace('__MS__', inpage_abort_ms()),
            target,
            what='view-source 抓取',
        )
    except Exception as e:
        print(f"  ⚠️  view-source 抓取失败: {e}")
        return ''

    if not html or len(html) < 2000:
        return ''
    return html


def detect_publisher_from_url(url: str) -> str:
    """
    Detect publisher from URL domain
    Returns: 'aps', 'nature', 'elsevier', 'iop', 'cambridge', etc.
    """
    url_lower = url.lower()

    if 'pubs.aip.org' in url_lower:
        return 'aip'
    elif 'aip.scitation.org' in url_lower:
        return 'aip'
    elif 'physicstoday.aip.org' in url_lower:
        return 'aip'
    elif '10.1063' in url_lower:
        return 'aip'
    elif 'journals.aps.org' in url_lower:
        return 'aps'
    elif 'iopscience.iop.org' in url_lower:
        return 'iop'
    elif '10.1088' in url_lower:
        return 'iop'
    elif 'cambridge.org' in url_lower:
        return 'cambridge'
    elif '10.1017' in url_lower:
        return 'cambridge'
    elif 'nature.com' in url_lower:
        return 'nature'
    elif 'sciencedirect.com' in url_lower:
        return 'elsevier'
    elif 'arxiv.org' in url_lower:
        return 'arxiv'
    else:
        return 'unknown'

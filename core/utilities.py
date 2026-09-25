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

from config import IS_WINDOWS
from datetime import datetime
from urllib.parse import urlparse, urlsplit

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

# Hard ceiling on ONE plain-HTTP file transfer (the `request` rung).
#
# This is NOT the same thing as the read timeout requests already takes. That
# one only fires when the socket goes *idle*, so a server dribbling bytes never
# trips it -- measured locally against a handler emitting one byte every 0.4 s:
# with timeout=(15, 2) it streamed for 24 s straight and would have continued
# indefinitely. Supplemental videos are where this bites, because they are the
# only assets big enough for a degraded link to stay "almost working" for
# hours.
#
# Generous on purpose (same rule as DP_INPAGE_FETCH_TIMEOUT): it exists to end
# a transfer that is never going to finish, not to police slow ones. A genuine
# 500 MB video on a slow link must still complete.
DP_HTTP_TOTAL_TIMEOUT = env_seconds('DP_HTTP_TOTAL_TIMEOUT', 600)


async def download_save_as_with_timeout(download, dest, *, timeout_s: float,
                                        what: str = '下载') -> bool:
    """``download.save_as()`` that cannot hang. True only when *dest* landed.

    ⚠️ Prefer this over ``path()`` + ``shutil.copy`` whenever the destination
    is known. ``path()`` hands back a file inside Playwright's own artifacts
    directory, and that file is deleted when the page or context closes -- so
    every line between resolving the path and copying it is a window in which
    a *successful* download can vanish. Measured on IOP: the tab rung had the
    PDF, the page was closed while the handler was still between the two
    calls, and the copy died with

        [Errno 2] No such file or directory: /tmp/playwright-artifacts-.../...

    which the caller then reported as a network failure and retried. ``save_as``
    copies while the Download is still live, so the window does not exist.

    Like ``path()``, it has no timeout of its own and only returns once the
    transfer finishes, hence the wrapper.
    """
    try:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"    ⚠️  无法创建{what}的目标目录（{type(exc).__name__}: {str(exc)[:80]}）")
        return False
    try:
        await asyncio.wait_for(download.save_as(str(dest)), timeout=float(timeout_s))
    except asyncio.TimeoutError:
        print(f"    ⏱️  {what}未在 {float(timeout_s):g}s 内完成落盘，判定为卡死并放弃")
        return False
    except Exception as exc:
        print(f"    ⚠️  保存{what}失败（{type(exc).__name__}: {str(exc)[:80]}）")
        return False
    # save_as returns None; the only honest success check is the file itself.
    try:
        return Path(dest).is_file() and Path(dest).stat().st_size > 0
    except OSError:
        return False


async def content_with_timeout(page, *, timeout_s: float = 20.0,
                               what: str = 'page.content()') -> str:
    """``page.content()`` that cannot hang. Returns '' instead of blocking.

    ⚠️ Another Playwright call with no ``timeout=`` of its own, and one that
    was missing from this module's list. It does not fail when the renderer
    dies -- it waits for a process that is never going to answer. Measured on
    ScienceDirect 10.1016/j.jcpx.2019.100006: the preload finished, landed
    page_raw.html and captured 10 API responses, then the tab crashed
    ("Something went wrong while displaying this page. Error code: 9") and the
    run stopped forever at the next content() call, with everything it needed
    already on disk.

    Returns '' rather than raising, because every caller here already treats
    empty HTML as "this page is no good" and moves on.
    """
    try:
        return await asyncio.wait_for(page.content(), timeout=float(timeout_s))
    except asyncio.TimeoutError:
        print(f"    ⏱️  {what} 超过 {timeout_s:g}s 未返回（渲染进程可能已崩溃），放弃该页面")
        return ''
    except Exception:
        return ''


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
# Bot-interstitial URLs
# ============================================================================
# A bot manager answers by *redirecting* rather than by an error status, so the
# landed URL alone identifies the interstitial -- no HTML needed. That matters
# where only the URL is available: over CDP, where reading the body costs a
# round-trip, and inside chrome_session, which cannot import the main module
# without creating this tree's first import cycle.
#
# Lives here so the one list serves both sides. complete_paper_extraction's
# is_bot_challenge_page() adds HTML-marker checks on top of it.
# Unambiguous interstitial hosts: seeing one of these IS the answer.
BOT_CHALLENGE_HOSTS = (
    'validate.perfdrive.com',      # Radware Bot Manager (IOP)
    'distilnetworks.com',
    'distilidentify.com',
)

# Weak words. A bot manager may sit at captcha.example.com or example.com/blocked,
# but these words also turn up in ordinary article URLs.
BOT_CHALLENGE_HINTS = ('captcha', 'challenge', 'accessdenied', 'blocked')


#: URL substrings whose XHR/Fetch bodies are worth keeping alongside the
#: documents. These are the endpoints a publisher's own page fetches during
#: load and that a handler would otherwise request a second time.
#:
#: ⚠️ The APS entries carry the DOI's '10.' on purpose. A bare '/fulltext/'
#: would match other publishers' reading views and cost a body fetch per
#: article for bytes nothing reads.
#:
#: Lives here rather than in chrome_session because both capture paths need
#: it -- the CDP preload (headed) and the Playwright response listener
#: (either mode) -- and two copies would drift the first time a publisher is
#: added to one of them.
DEFAULT_API_HARVEST = (
    '/sdfe/arp/',            # ScienceDirect: body, references, metadata
    '/rest/document/',       # IEEE: the REST article endpoints
    '/fulltext/10.',         # APS: the reading view's own JSON
    '/supplemental/10.',     # APS: the supplemental listing
    '/article/fulltexthtml', # SPIE: the body the landing page fetches itself
    '/article/supplemental', # SPIE: its supplemental listing, same idea
)


def api_harvest_patterns() -> list:
    """URL substrings whose XHR/Fetch bodies are worth keeping.

    On by default. ``DP_HARVEST_API`` overrides the list; setting it to ``0``,
    ``off`` or ``none`` disables API harvesting entirely and leaves only
    documents, which is the pre-2026-09 behaviour.
    """
    raw = (os.environ.get('DP_HARVEST_API') or '').strip()
    if not raw:
        return list(DEFAULT_API_HARVEST)
    if raw.lower() in ('0', 'off', 'none', 'false', 'no'):
        return []
    return [p.strip().lower() for p in raw.split(',') if p.strip()]


def url_wants_api_harvest(url: str) -> bool:
    """True when *url* matches one of :func:`api_harvest_patterns`."""
    pats = api_harvest_patterns()
    low = (url or '').lower()
    return bool(pats) and any(p in low for p in pats)


def captured_api_entry(captured: dict, path_suffix: str) -> tuple:
    """``(body, url)`` for a captured response whose URL path ends with *path_suffix*.

    The URL is worth having on its own: SPIE's fulltext endpoint spells the
    content family in it (/api/journals|proceedings|ebooks/article/fulltexthtml),
    which the handler otherwise has to infer from a meta tag and, when that is
    wrong, discovers only as an ``hasAccess=False`` empty shell that looks
    like a permissions problem.

    Returns ``('', '')`` on a miss. See captured_api_body for the matching
    rules.
    """
    if not captured or not path_suffix:
        return '', ''
    want = path_suffix.rstrip('/').lower()
    best, best_url = '', ''
    for entry in captured.values():
        if not isinstance(entry, dict):
            continue
        body = entry.get('body')
        if not body:
            continue
        url = (entry.get('url') or '')
        if not url:
            continue
        path = urlsplit(url).path.rstrip('/').lower()
        if path == want or path.endswith(want):
            # Prefer the largest when a page requests the same endpoint more
            # than once -- a short answer is usually the pre-entitlement one.
            if len(body) > len(best):
                best, best_url = body, url
    return best, best_url


def captured_api_body(captured: dict, path_suffix: str) -> str:
    """Return a captured response body whose URL *path* ends with *path_suffix*.

    ⚠️ Matches on the path only, never the full URL. A publisher signs these
    endpoints per session: ScienceDirect's body call arrives as
    ``/sdfe/arp/pii/<PII>/body?entitledToken=EF8C3B04…`` while the handler
    would build the same path with a token it resolved separately. Comparing
    full URLs would miss every time, and comparing hosts is not enough either.

    *path_suffix* is matched against the URL path with any query string
    dropped, e.g. ``/sdfe/arp/pii/S2352214326001231/body`` or
    ``/rest/document/9084126/references``.

    Returns '' when nothing matches or the entry has no body -- the caller
    then requests it as before. A miss must stay cheap and silent: capture is
    an optimisation, not a dependency.
    """
    return captured_api_entry(captured, path_suffix)[0]


def pick_raw_article_html(candidates, doi: str = '') -> str:
    """Choose the document response that is actually the article.

    The response listeners append EVERY ok HTML document they see: the
    doi.org redirect hop, a Cloudflare interstitial, an iframe document, and
    finally the article. Taking ``[-1]`` assumes the article came last, which
    is not guaranteed -- and when it is wrong the caller silently parses a
    challenge page and reports "0 figures" rather than failing.

    Among the candidates carrying article markers (the DOI itself, or a
    ``citation_*`` meta tag -- the same test APS's capture callback uses), the
    one with the **most content** wins; with no marked candidate, the longest
    overall does.

    ⚠️ Size, not position, and not "first marked wins". A protected publisher
    answers the *same* URL twice: once before the bot check and once after.
    Measured on ScienceDirect, an article URL (``…/pii/S221137972100245X?via=ihub``)
    produces two document responses, and only the second carries the metadata --
    yet both can carry ``citation_*``, so picking by marker alone can return the
    pre-challenge shell and report an article with no authors and no figures.
    The shell is short and the real page is not, which is what makes length the
    usable signal.

    This replaces an earlier "last marked candidate, else ``[-1]``" rule that
    promised never to do worse than ``[-1]``; that promise is gone on purpose,
    since ``[-1]`` is exactly what loses the ScienceDirect case when the shell
    happens to arrive last.
    """
    items = [c for c in (candidates or []) if c]
    if not items:
        return ''
    doi_l = (doi or '').lower()

    def _is_article(html: str) -> bool:
        low = html.lower()
        return ('citation_doi' in low
                or 'citation_title' in low
                or (bool(doi_l) and doi_l in low))

    marked = [h for h in items if _is_article(h)]
    return max(marked or items, key=len)


def url_looks_like_bot_challenge(url: str) -> bool:
    """True when *url* is a bot-manager interstitial rather than the real page.

    ⚠️ Matches the host and whole path SEGMENTS only -- never a bare substring
    of the URL. Substring matching on the weak words looks equivalent and is
    not: it flags ``/article/10.1088/…/challenges-in-tokamak-control`` and
    ``10.1002/challenge.20250101`` as interstitials. Measured on four otherwise
    ordinary article URLs, a substring version misjudged all four.

    That matters because the cost of a false positive is not symmetric between
    the two callers. In the headless preflight it only escalates to headed; in
    the referer rung it *refuses a referring page that was fine*, reloads it
    pointlessly and abandons a path that would have worked -- a worse failure
    than the one this check exists to prevent.
    """
    if not url:
        return False
    parts = urlsplit(url.lower())          # urlsplit, not urlparse: the latter
    host = parts.netloc                    # eats everything after ';' into
    if not host and not parts.path:        # .params, and SICI DOIs contain ';'
        return False
    if any(h in host for h in BOT_CHALLENGE_HOSTS):
        return True
    if any(w in host for w in BOT_CHALLENGE_HINTS):
        return True
    segments = [s for s in parts.path.split('/') if s]
    return any(s in BOT_CHALLENGE_HINTS for s in segments)


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


def sniffed_mime(data: bytes) -> str:
    """The media type of *data* according to its own bytes, or ''.

    "Servers mislabel constantly; trust the bytes" -- the rule
    ``_http_download_to`` already applies to every direct download. Shared so
    the browser rungs judge a payload the same way instead of growing their
    own, weaker test (a substring check for a leading ``<`` misses a PDF-
    looking challenge page and anything served with a BOM).
    """
    if not data:
        return ''
    try:
        import magic
        return magic.from_buffer(data[:4096], mime=True) or ''
    except Exception:
        return ''


def looks_like_html_bytes(data: bytes) -> bool:
    """True when *data* is a web page rather than the file we asked for.

    ⚠️ Used where the target is known to be a binary (a PDF, an image, a
    supplement). A challenge page answers 200 at the file's own URL, so the
    status code proves nothing -- measured: an Optics Journal PDF came back
    as 15,999 bytes of "Verification" interstitial.
    """
    mime = sniffed_mime(data)
    if mime:
        return mime in ('text/html', 'application/xhtml+xml', 'text/xml',
                        'application/xml')
    return data.lstrip()[:1] == b'<'


#: Per-component byte limit for a filename we invent ourselves. ext4 allows
#: 255 bytes; 200 leaves room for the suffixes a download picks up on the way
#: (``.crdownload``, a ``_1`` de-duplication tail).
#:
#: ⚠️ This is NOT the cap that governs the output directory. Those are
#: MAX_STEM_BYTES (supplemental), the figure name cap, and
#: WINDOWS_CHILD_RESERVE, and they are a matched set -- see CLAUDE.md. This
#: one only bounds names derived from a URL inside a temporary download dir.
SAFE_NAME_MAX_BYTES = 200


def complete_downloads_in(download_dir: str) -> list:
    """``[(path, size)]`` for the finished files in *download_dir*, largest first.

    The single definition of "this download is done", shared by the two places
    that watch a Chrome download directory:
    ``chrome_session._await_download`` (the challenge loop) and
    ``complete_paper_extraction._finalize_downloaded_pdf`` (the hand-off that
    copies the file into the paper directory).

    ⚠️ Deriving the final name from the ``.crdownload`` path does not work and
    must not be attempted: Chrome renames the partial file to the name the
    *server* gave it, so stripping the suffix off
    ``Unconfirmed 821706.crdownload`` yields ``Unconfirmed 821706``, a name
    Chrome never uses. The directory has to be looked at. It is created empty
    for each attempt, so anything complete in it belongs to this download.

    Callers decide how long to wait and whether to require the size to settle;
    what counts as "complete" lives here so the two cannot drift apart.
    """
    if not download_dir or not os.path.isdir(download_dir):
        return []
    found = []
    try:
        for name in os.listdir(download_dir):
            if name.endswith(('.crdownload', '.tmp')):
                continue
            path = os.path.join(download_dir, name)
            try:
                if not os.path.isfile(path):
                    continue
                size = os.path.getsize(path)
            except OSError:
                continue
            if size > 0:
                found.append((path, size))
    except OSError:
        return []
    found.sort(key=lambda item: item[1], reverse=True)
    return found


def safe_download_name(url: str, fallback: str = 'download.bin',
                       max_bytes: int = SAFE_NAME_MAX_BYTES) -> str:
    """A filename for *url* that the filesystem will actually accept.

    ⚠️ A URL path segment can be arbitrarily long, and some publishers put a
    whole title in it. Measured: a PDF whose basename began
    ``div-class-title-51-5-w-monol…`` made the browser-stream download die
    with ``OSError: [Errno 36] File name too long`` -- after the bytes had
    already been fetched, which is the worst moment to lose them.

    The extension is preserved: it is what ``_detect_and_rename`` and the
    media-type checks downstream look at.
    """
    from urllib.parse import unquote, urlparse
    import os as _os

    raw = _os.path.basename(urlparse(url or '').path)
    raw = unquote(raw).strip().strip('.') or fallback
    raw = raw.replace('/', '_').replace('\\', '_')

    stem, ext = _os.path.splitext(raw)
    ext_bytes = ext.encode('utf-8')
    if len(ext_bytes) > 24:          # not an extension, just a long tail
        stem, ext, ext_bytes = raw, '', b''

    room = max_bytes - len(ext_bytes)
    stem_bytes = stem.encode('utf-8')
    if room <= 0:
        return fallback
    if len(stem_bytes) > room:
        stem_bytes = stem_bytes[:room]
        # Never split a multi-byte UTF-8 character.
        while stem_bytes and (stem_bytes[-1] & 0xC0) == 0x80:
            stem_bytes = stem_bytes[:-1]
        stem = stem_bytes.decode('utf-8', errors='ignore')
    return (stem + ext) or fallback


def env_off(name: str, default: str = '1') -> bool:
    """True when *name* is set to one of the usual "no" spellings."""
    return os.environ.get(name, default).strip().lower() in (
        '0', 'false', 'no', 'off')


#: Whether to download supplemental material at all (``--supplemental=False``
#: or ``DP_SUPPLEMENTAL=0``). Off is a real use case, not a debugging knob:
#: an OUP book lists **every one of its chapters** as a supplemental PDF --
#: 19 files, ~60 MB and most of the run's wall clock for
#: ``10.1093/acprof:oso/9780198562641.001.0001`` -- when all that was wanted
#: was the book's own text. The links still go into the Markdown; only the
#: fetching is skipped, so nothing about the article is lost from the record.
DP_SUPPLEMENTAL = not env_off('DP_SUPPLEMENTAL')

#: Whether the run said anything at all. A handler may default to skipping
#: (books do); an explicit flag or variable outranks that, and "not set" has
#: to be distinguishable from "set to True" for that to work.
DP_SUPPLEMENTAL_SET = 'DP_SUPPLEMENTAL' in os.environ


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
        # DP_HTTP_FIRST=0 means "never the plain request", and it has to hold
        # for a default that names that rung too -- the guard above only sees
        # explicitly configured values.
        if not DP_HTTP_FIRST:
            tiers = tuple(t for t in tiers if t != 'request')
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


def capture_document_html(page) -> list:
    """Record main-document response bodies on *page* as they arrive.

    Returns the list the listener appends to, so the caller reads it after
    navigating. Register this **before** ``goto()`` -- a listener attached
    afterwards misses the document that is already on the wire.

    What comes back is the body exactly as the server sent it, before any
    script rewrites the DOM. That is the same passive mechanism the article
    page uses (``_raw_server_html``): no extra request, nothing executed
    inside the page, so it adds no automation surface of its own.
    """
    bodies: list = []

    async def _on_response(response):
        try:
            if (response.request.resource_type == 'document'
                    and response.ok
                    and 'text/html' in response.headers.get('content-type', '')):
                bodies.append(await response.text())
        except Exception:
            pass

    page.on('response', _on_response)
    return bodies


async def _fetch_html_in_new_tab(ctx, url: str, *, expect, timeout_s: float) -> str:
    """Fetch *url* in a throwaway tab. Returns '' when it isn't the page we want.

    A separate tab rather than the caller's own: driving the article tab to
    another URL and back is something a person never does, and on a publisher
    that scores behaviour (IOP's Radware profiler) it happens at exactly the
    moment the article has just been read. The download rungs already work
    this way -- see the ``context.new_page()`` in the PDF, figure and
    supplement paths -- so this only brings the HTML rung in line with them.

    No Referer header is set on the tab. A CDP-injected Referer arrives
    without ``Sec-Fetch-User``, a combination a real click never produces;
    that is the same reason the download ladder's referer rung clicks instead
    of setting headers.
    """
    tab = None
    try:
        tab = await ctx.new_page()
    except Exception as exc:
        print(f"    ↪ 无法新建标签页（{type(exc).__name__}），下一层")
        return ''
    try:
        # Registered before goto(): the raw response is the whole point, and a
        # listener attached after navigation would only ever see nothing.
        raw_docs = capture_document_html(tab)
        try:
            await tab.goto(url, wait_until='networkidle',
                           timeout=int(timeout_s * 1000))
        except Exception:
            await tab.goto(url, wait_until='domcontentloaded',
                           timeout=int(timeout_s * 1000))

        # Prefer the raw body, newest first (the listener also records
        # redirect hops), but fall back to the rendered DOM: a client-rendered
        # listing would be empty in the response and complete only after JS.
        for html in reversed(raw_docs):
            if _html_is_acceptable(html, expect):
                print(f"    ✓ 取得页面 [新标签页·原始响应] {len(html):,} 字符")
                return html
        rendered = await content_with_timeout(tab, what='阶梯 tab 层读取 DOM')
        if _html_is_acceptable(rendered, expect):
            print(f"    ✓ 取得页面 [新标签页·渲染后] {len(rendered):,} 字符")
            return rendered
        print("    ↪ 标签页拿到的不是目标页面，下一层")
    except Exception as exc:
        print(f"    ↪ 标签页访问失败（{type(exc).__name__}），下一层")
    finally:
        try:
            await tab.close()
        except Exception:
            pass
    return ''


async def _fetch_html_in_place(page, url: str, *, expect, timeout_s: float,
                               restore_url: str) -> str:
    """Degenerate fallback: drive the caller's own page, then put it back.

    Only reached when there is no context to open a tab in. It is the older,
    more conspicuous shape -- the article tab visibly leaves and returns --
    and *restore_url* matters here and only here: leaving the shared tab
    parked on a supplemental listing is how metadata ends up recording the
    wrong URL.
    """
    back_to = restore_url or getattr(page, 'url', '') or ''
    try:
        try:
            await page.goto(url, wait_until='networkidle',
                            timeout=int(timeout_s * 1000))
        except Exception:
            await page.goto(url, wait_until='domcontentloaded',
                            timeout=int(timeout_s * 1000))
        html = await content_with_timeout(page, what='阶梯 tab 层读取 DOM')
        if _html_is_acceptable(html, expect):
            print(f"    ✓ 取得页面 [浏览器标签页] {len(html):,} 字符")
            return html
        print("    ↪ 标签页拿到的不是目标页面，下一层")
    except Exception as exc:
        print(f"    ↪ 标签页访问失败（{type(exc).__name__}），下一层")
    finally:
        if back_to:
            try:
                await page.goto(back_to, wait_until='domcontentloaded',
                                timeout=15000)
            except Exception:
                pass
    return ''


async def fetch_html_via_ladder(url: str, *, kind: str = 'api', page=None,
                                context=None, referer: str = None,
                                expect=None, timeout_s: float = 30.0,
                                restore_url: str = None,
                                headless: bool = True,
                                default: tuple = ('tab', 'fresh')) -> str:
    """Fetch *url* as HTML, walking the ladder until something usable comes back.

    *expect* is a substring or a predicate identifying the page we wanted; see
    :func:`_html_is_acceptable` for why it matters. Returns '' when every rung
    failed, so the caller can report honestly rather than parse a challenge
    page.

    *restore_url* applies to the degenerate in-place fallback only. The 'tab'
    rung normally opens a throwaway tab, so the caller's page never moves and
    there is nothing to restore; only when no context is available does that
    rung drive the caller's own page and navigate back here -- leaving the
    shared article tab parked on a supplemental listing is how metadata ends
    up recording the wrong URL.

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

    *default* is the full rung order for callers whose target is a plain file
    on an undefended host -- an XML or JSON API answers a bare request, and
    the 'tab' rung would hand back Chrome's *viewer* DOM instead of the
    publisher's bytes. ``DP_FETCH_*`` still overrides it.
    """
    tiers = fetch_ladder(kind, default)

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
            # A tab of its own whenever there is a context to open one in --
            # which is every real call site, since a page carries its context.
            ctx = context or getattr(page, 'context', None)
            if ctx is not None:
                html = await _fetch_html_in_new_tab(
                    ctx, url, expect=expect, timeout_s=timeout_s)
                if html:
                    return html
            elif page is not None:
                html = await _fetch_html_in_place(
                    page, url, expect=expect, timeout_s=timeout_s,
                    restore_url=restore_url)
                if html:
                    return html

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
                # Crossref's own full-text links. The first one's host is the
                # most reliable pre-navigation hint about *where* the article
                # actually lives -- see _crossref_headless_host in the main
                # flow for why the publisher name is not.
                'link': [entry.get('URL') for entry in (work.get('link') or [])
                         if entry.get('URL')],
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

# Windows refuses any path over MAX_PATH, and the limit counts the terminating
# NUL, so the usable string length is 259.
WINDOWS_MAX_PATH = 260

# Room to leave under the paper directory for the deepest thing that goes in it,
# which is always a supplemental file. Add the parts up rather than guessing --
# the first version of this reserve was 96 against a worst case of 100, i.e. it
# would have put the very overflow it exists to prevent back on the table:
#
#     \supplemental\                     14
#     stem cap (MAX_STEM_BYTES, Windows) 80   ← already includes "supplemental--"
#     extension (.docx is the longest)    5
#     dedup suffix (_1 … _100)            4
#                                       ----
#                                       103
#
# 104 leaves the arithmetic exact with a character to spare. Everything else in
# the paper directory (html\page_raw.html, paper.pdf, key_image.png, figures) is
# shorter, so budgeting for this tail covers them all.
#
# ⚠️ MAX_STEM_BYTES in complete_paper_extraction.py is the other half of this
# pair. Change one and you must redo this sum.
WINDOWS_CHILD_RESERVE = 104

# Cap on the directory name itself. On Linux this is the only limit that
# applies; on Windows it is an upper bound that the path budget may lower.
DIR_NAME_MAX = 150


def _clean_title_for_directory(title: str, max_len: int = DIR_NAME_MAX) -> str:
    """Clean title for use as directory name, handling formulas and HTML tags.

    *max_len* caps the cleaned title. The caller lowers it on Windows so the
    deepest file that will live inside still fits under MAX_PATH -- see
    :func:`organize_paper_output`.
    """
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
    title = title[:max(1, int(max_len))].strip()

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
        # ``_dir_title`` lets a handler name the folder differently from the
        # metadata title. J-STAGE needs it: metadata['title'] carries the
        # Japanese and English titles together so a search on either hits, but
        # a directory called "日本語 English" twice as long serves nobody --
        # the folder uses the Japanese title alone.
        title = (metadata.get('_dir_title') or metadata.get('title')
                 or s2_data.get('title') or 'paper')

        # Safety check: ensure year and title are strings
        if not isinstance(year, str):
            year = str(year) if year else '0000'
        if not isinstance(title, str):
            title = str(title) if title else 'paper'

        # ⚠️ Budget the directory name against how deep the output root already
        # is. Windows rejects any path over MAX_PATH with ENOENT -- "No such
        # file or directory" for a directory that plainly exists, which is a
        # thoroughly misleading way to be told the name is too long. Measured
        # failure: a 156-char title under C:\Users\...\captured_data produced a
        # 272-char supplemental path, 12 over the limit, so paper.pdf and the
        # figures landed but every supplemental file failed.
        #
        # The per-component caps already in this tree (150 chars here, 200
        # bytes on supplemental stems) do not help: they bound one component,
        # and Windows bounds the whole path.
        title_budget = DIR_NAME_MAX
        if IS_WINDOWS:
            room = (WINDOWS_MAX_PATH - 1          # MAX_PATH counts the NUL
                    - len(str(Path(output_dir)))  # the root the user chose
                    - 1                           # separator before our name
                    - WINDOWS_CHILD_RESERVE)      # deepest child under it
            title_budget = max(20, min(DIR_NAME_MAX, room - len(f"{year}--")))
            if room - len(f"{year}--") < 20:
                print(f"  ⚠️  输出根目录过深（{len(str(Path(output_dir)))} 字符），"
                      f"文章目录名已压到下限；请把 --output 指向更短的路径")

        # Clean title: remove HTML tags and convert math symbols
        title_clean = _clean_title_for_directory(title, title_budget)

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
        # ⚠️ Handler metadata first, Crossref second -- the same order
        # organize_paper_output uses. They used to disagree: the folder was
        # named from the handler's title and metadata.json from Crossref's, so
        # the two could describe the same paper differently. Measured on
        # J-STAGE 10.2184/lsj.51.5_337, where the handler builds
        # "<日本語> <English>" on purpose so a search over metadata.json hits
        # either language, and Crossref's English-only title silently replaced
        # it.
        year = metadata.get('year') or s2_data.get('year') or '0000'
        title = metadata.get('title') or s2_data.get('title') or 'paper'

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
            # ❌ No 'references' key. It was added here for J-STAGE and taken
            # back out: the reference list already lives in paper.md (the
            # publisher's own wording) and in crossref.json (Crossref's), so a
            # third copy inside metadata.json only makes that file large and
            # harder to scan.

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

#: Publishers whose handler parses the raw server response rather than the
#: rendered DOM -- which, since the last conversion, is all of them.
#:
#: ❌ This used to also mean "skip the MathJax route interception".
#: :func:`block_mathjax` is gone: every handler now reads the captured
#: response, and for a publisher we have no handler for the only thing that
#: happens is that ``page_raw.html`` is landed for someone to write one from
#: later -- and that file is the response, which MathJax never touched. So
#: the interception protected nothing while remaining an intervention the
#: page can see (an aborted subresource request).
#:
#: What membership still buys: :meth:`PublisherHandler.get_page_html`
#: re-fetches the source with view-source when no raw body was captured, and
#: announces that downgrade instead of silently handing back a rendered DOM.
#: ⚠️ The token is the handler's own ``PUBLISHER``, so a handler that does
#: not declare one never gets the rescue however it is spelled here.
RAW_HTML_PUBLISHERS = frozenset({
    'iop', 'sciencedirect', 'aps', 'optica', 'cambridge',
    'acs', 'wiley', 'ieee', 'spie', 'aip', 'nature', 'mdpi', 'acm', 'oup', 'science', 'researching',
    'opticsjournal', 'jstage', 'rcsi',
})


async def fetch_view_source_html(page, url: str = None, timeout_ms: int = 30000) -> str:
    """Re-fetch the current document and return its *unrendered* source.

    This is the programmatic equivalent of opening ``view-source:<url>``: the
    response body exactly as the server sent it, before MathJax (or any other
    script) rewrites the DOM. Publishers such as Optica ship display math as
    ``$$...$$`` TeX in the HTML source, but MathJax 4 replaces it with SVG
    whose only textual content is the accessibility *speech* string — which is
    how "P sub 0 equals E sub 0 divided by tau sub eff" ends up in the
    markdown instead of ``{P_0} = {E_0}/{\tau _{\rm{eff}}}``.

    This is the rescue path, and now the only one: the MathJax route
    interception it used to back up is gone (see RAW_HTML_PUBLISHERS).
    Re-fetching the source cannot be defeated by rendering.

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

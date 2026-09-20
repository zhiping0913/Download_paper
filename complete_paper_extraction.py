#!/usr/bin/env python3
"""
完整论文提取脚本 - 统一工作流
从DOI到完整Markdown的端到端解决方案

功能：
1. 连接到已登录Chrome（通过CDP）
2. 提取元数据（作者、单位、摘要等）
3. 监听网络请求捕获原始JSON（包含MathML）
4. 下载高分辨率图片
5. 转换为完整Markdown（公式转为LaTeX）
"""

import json
import asyncio
import magic
import os
import random
import re
import requests
import shutil
import socket
from typing import Optional
import tempfile
import threading
import time
import sys
import signal
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse
from playwright.async_api import async_playwright
from chrome_session import (
    chrome_password_store_args,
    HEADLESS_PROFILE_NAME,
    prepare_profile_dir,
    scraping_profile_dir,
    PROFILE_SEED_FILES,
    PROFILE_SEED_ROOT_FILES,
    cleanup_profile_root,
    kill_chrome,
    launch_chrome,
    seed_profile,
    sweep_stale_profiles,
)
try:
    from chrome_session import bypass_cloudflare_cdp, has_cf_clearance_cdp
    _CF_BYPASS_AVAILABLE = True
except ImportError:
    _CF_BYPASS_AVAILABLE = False

# 导入核心模块 (Phase 2 refactoring)
from core import (
    fetch_crossref,
    fetch_semanticscholar,
    organize_paper_output,
    save_metadata_json,
    save_crossref_json,
    block_mathjax,
)
# The fetch ladder lives in core.utilities because publisher handlers need it
# too, and the dependency runs one way: a handler importing this module would
# be the first cycle in the tree. Imported straight from the submodule, the
# way wiley/optica/iop do, rather than widening core/__init__'s public surface
# with what is internal plumbing.
from core.utilities import (
    cookies_for_requests,
    download_save_as_with_timeout,
    env_seconds,
    evaluate_with_timeout,
    fetch_ladder,
    fresh_chrome_enabled,
    http_asset_headers,
    inpage_abort_ms,
    read_body_with_timeout,
    pick_raw_article_html,
    should_block_mathjax,
    url_looks_like_bot_challenge,
    url_wants_api_harvest,
    DP_HTTP_TOTAL_TIMEOUT,
    INPAGE_ABORT_JS,
)

# Private aliases so the call sites here read as they always did.
_fetch_ladder = fetch_ladder
_cookies_for_requests = cookies_for_requests
_fresh_chrome_enabled = fresh_chrome_enabled
_http_asset_headers = http_asset_headers
from publisher.orchestrator import (
    detect_publisher_from_url,
    get_publisher_handler,
    extract_metadata_multi_publisher
)

from config import (
    BATCH_SLEEP_ENABLED,
    BATCH_SLEEP_MAX,
    BATCH_SLEEP_MIN,
    CHROME_DEBUG_PORT,
    CHROME_PROFILE,
    CHROME_PROFILE_SOURCE_DIR,
    FRESH_PROFILE,
    IS_WINDOWS,
    OUTPUT_DIR_DEFAULT,
    SAVE_WITHOUT_REFERENCES,
)

OUTPUT_DIR = OUTPUT_DIR_DEFAULT


# ============================================================================
# Timeout / wait knobs — configurable via environment variables
# ============================================================================
# Each knob controls a family of related waits. Values are in SECONDS.
# All timeout= arguments passed to Playwright below are computed as
# ``<knob> * 1000`` internally (Playwright expects milliseconds).
#
#   DP_PAGE_LOAD_TIMEOUT       page.goto / wait_for_load_state / API GET
#                              (both headed and headless preflight)
#   DP_CLOUDFLARE_TIMEOUT      Cloudflare Turnstile auto-solve budget
#                              (initial-poll fraction fixed at ~13% below)
#   DP_PDF_FASTPATH_WAIT       how long the throwaway PDF browser waits for
#                              the file to land before attaching CDP to click
#                              the challenge (short: a challenged PDF never
#                              lands, so this only delays the click)
#   DP_SUPPLEMENTAL_TIMEOUT    supplemental download navigation +
#                              download-event wait
#   DP_FIGURE_TIMEOUT          figure navigation (both primary and
#                              fallback img re-fetch)
#   DP_INPAGE_FETCH_TIMEOUT    hard cap on one in-page fetch() or one
#                              response-body read -- the only two Playwright
#                              calls that take no timeout of their own and so
#                              wait forever. Defined in core.utilities because
#                              publisher handlers need it too. Raise it for a
#                              slow link; it is a deadlock breaker, not a
#                              throughput knob.
#
# Missing / unparseable env vars fall through to the hardcoded defaults
# that were in place before this refactor.

# The parser itself lives in core.utilities, because the knobs defined there
# (DP_INPAGE_FETCH_TIMEOUT) have to read the environment the same way these do
# and the dependency only runs one way.
_env_seconds = env_seconds


# Page-load family — covers the initial article navigation (headed + headless),
# every intermediate wait_for_load_state('networkidle'), and the direct
# APIRequestContext GET used for asset fetches. Default: 120 s.
DP_PAGE_LOAD_TIMEOUT = _env_seconds('DP_PAGE_LOAD_TIMEOUT', 120)

# Cloudflare Turnstile family — total budget once a widget is seen.
#
# The initial-poll window (how long to wait for a widget to APPEAR) is a fixed
# fraction of this, which makes the default matter far more than it looks: it
# is what a page with NO challenge pays before it can conclude there is none,
# and most pages have no challenge. At 600 that was 80 s of dead waiting on
# every one of them. 60 puts the derived window at 8 s.
DP_CLOUDFLARE_TIMEOUT = _env_seconds('DP_CLOUDFLARE_TIMEOUT', 60)
DP_CLOUDFLARE_INITIAL_POLL = max(2.0, DP_CLOUDFLARE_TIMEOUT / 7.5)  # 8 s at default

# Throwaway-PDF-browser fast path — how long to wait for Chrome's own startup
# navigation to resolve before attaching CDP. Default: 5 s.
#
# The window no longer costs a challenged publisher anything: it races the
# finished download against a real page target appearing, and a challenge
# produces the page almost immediately, which ends the wait. Only the
# unchallenged case spends the full budget, and that case is the one where
# spending it means never attaching at all -- a ~1 MB PDF does not finish in
# the 3 s this used to allow, which is why IOP kept falling through to the
# CDP path and getting a Radware captcha there.
DP_PDF_FASTPATH_WAIT = _env_seconds('DP_PDF_FASTPATH_WAIT', 5)

# PDF download hard cap — 判据为「下载事件」的等待上限。
# 分享 Chrome 被 Playwright(accept_downloads=True) 接管后，文件落入 playwright-artifacts
# 临时目录而非 /root/Downloads，故不能再靠“目录新文件”判通过；
# 改为监听 Playwright download 事件。部分网站下载慢，故单独可配。默认 30 s。
# 超时则如实判定 PDF 下载失败（不误判、不无限等待、不硬点挑战框）。
DP_PDF_DOWNLOAD_TIMEOUT = _env_seconds('DP_PDF_DOWNLOAD_TIMEOUT', 30)

# PDF 下载「完成」等待 — 慢网速专用。
# 与 DP_PDF_DOWNLOAD_TIMEOUT（判“是否开始了下载”）分离：
# 一旦 download 事件已触发（下载真实开始），就只等它完成，不因慢而重开页面/retry。
# 默认 60 s；慢网或超大 PDF 可用 DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT 调大。
DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT = _env_seconds('DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT', 60)

# Supplemental download family — both the initial page.goto(url) and the
# download-event wait for each supplemental link. Also covers inline-audio
# body-fetch waits. Default: 60 s.
DP_SUPPLEMENTAL_TIMEOUT = _env_seconds('DP_SUPPLEMENTAL_TIMEOUT', 60)

# Supplemental download completion wait — after the download event fires
# (file transfer in progress), how long to let download.save_as() finish
# writing before giving up. Default: 120 s. A large DOCX/MP4 on a slow link
# can need 10+ minutes; that is what raising
# DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT is for, rather than making every
# run wait that long by default.
DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT = _env_seconds('DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT', 120)

# Figure download family — the CDN goto for each figure image (and the
# fallback img_src re-fetch if the first response wasn't image/*).
# Default: 60 s.
DP_FIGURE_TIMEOUT = _env_seconds('DP_FIGURE_TIMEOUT', 60)

# Retry knobs — configurable via environment variables
DP_MAX_RETRIES = int(_env_seconds('DP_MAX_RETRIES', 5))          # generic downloads
# Flat pause between download attempts -- no growth, the same wait every time.
#
# 90 s because this is the ONLY pacing inside one paper (BATCH_SLEEP separates
# papers, not attempts) and because IOP's bot manager appears to hold a block
# for minutes, not attempts. Two runs, both ending in success the moment the
# block lifted rather than on any particular retry:
#
#   no pacing at all   success on attempt 4, ~4-5 min in
#   60 s pacing        success on attempt 5, ~6 min in -- and that attempt was
#                      visibly clean: no Cloudflare challenge at all, and the
#                      throwaway Chrome's startup navigation went straight to
#                      the PDF instead of to validate.perfdrive.com
#
# That sharp transition is what a penalty expiring looks like; probabilistic
# sampling would not produce it. 90 s x 4 waits gives ~6 min of pure waiting,
# i.e. margin over the observed window. n=2, so this is a calibrated guess.
DP_RETRY_DELAY = _env_seconds('DP_RETRY_DELAY', 90)              # seconds between retries
# Figures get their own, much smaller pause. They share retry_download but not
# the problem: a figure that 404s is simply absent, and a paper has dozens of
# them, so the PDF's anti-bot pacing would be paid over and over for nothing.
DP_IMG_RETRY_DELAY = _env_seconds('DP_IMG_RETRY_DELAY', 10)      # seconds between figure retries

# Whether to inject the anti-detection patches into headed pages.
#
# Default on = current behaviour; DP_STEALTH_JS=0 turns it off so the two can be
# compared. Measured against the real Chrome binary, the script is mostly inert
# and the rest is arguably counterproductive:
#
#   plugins / languages branches   never run -- both are guarded on
#                                  `.length === 0`, and Chrome reports 5 plugins
#                                  and 2 languages, so they are dead code here
#   delete window.cdc_…            no-op -- cdc_ is a ChromeDriver artifact and
#                                  Playwright/CDP never injects it
#   navigator.webdriver = undefined  a value no real browser ever reports
#                                  (real Chrome says false), created as an
#                                  instance-level GETTER while the genuine
#                                  property is a data property on the prototype
#   permissions.query override     toString() stops saying [native code], and
#                                  the function moves onto the instance
#
# Nothing in this program reads any of those, so switching it off costs us no
# capability. Whether it helps against a bot manager is unproven -- that is what
# the switch is for.
DP_STEALTH_JS = os.environ.get('DP_STEALTH_JS', '1').strip().lower() not in (
    '0', 'false', 'no', 'off')
DP_IMG_MAX_RETRIES = int(_env_seconds('DP_IMG_MAX_RETRIES', 3))  # figure/image downloads
DP_SUPP_MAX_RETRIES = int(_env_seconds('DP_SUPP_MAX_RETRIES', 5)) # supplemental downloads



# Publisher IDs that can be entered from the Phase 0 headless page.
# Phase 0 substring matches against the Crossref `publisher` field, so each
# entry must appear *inside* the publisher's display name. Crossref returns
# OUP papers under either "Oxford University Press (OUP)" or just
# "Oxford University Press", so we keep both 'oup' and 'oxford' here to
# catch both forms. The URL/DOI detector still returns the canonical
# 'oup' handler name.
HEADLESS_ACCESSIBLE_PUBLISHERS = [
    'nature', 
    'aip', 
    'cambridge', 
    'springer', 
    'springer_book', 
    'oup', 
    'oup_book', 
    'oxford', 
    'pleiades',
    'acs'
    ]


def pin_capture_on_handler(handler, raw_html: str = '', api_capture: dict = None,
                           label: str = '') -> None:
    """Hand a handler everything the page-loading phase captured.

    ⚠️ Both branches of the workflow must call this. There are two of them --
    the headless-direct path returns into process_with_handler without ever
    reaching the headed block -- and every capability added to one of them
    alone has silently existed in one browser mode only. That is how
    ``_captured_api`` came to be headed-only: handlers' "reuse what the page
    already fetched" path was dead code in headless, and the symptom was not
    an error but a paper quietly missing its supplemental files.

    Headed and headless are two modes of the same Chrome; the only thing that
    may legitimately branch on the mode is how a browser is launched (see
    PublisherHandler.is_headed_run).
    """
    if raw_html:
        handler._raw_server_html = raw_html
    if api_capture:
        handler._captured_api = api_capture
        # An empty label means the capture already announced itself where it
        # happened (the preload prints its own tally); saying it twice would
        # read like two different captures.
        if label:
            biggest = max(len(v.get('body') or '') for v in api_capture.values())
            print(f"  ✓ {label} API 响应 {len(api_capture)} 条"
                  f"（最大 {biggest:,} 字符）")


class PageCapture:
    """Everything one article-page load produced, and where it lands.

    One object for what used to be four parallel piles of locals -- the
    headless precheck's raw-HTML/API locals and the headed path's three.
    They drifted:
    API bodies were kept on one path and thrown away on the other, so every
    handler's "reuse what the page already fetched" branch was dead code in
    headless, and the symptom was a paper quietly missing files rather than an
    error. Headed and headless are two modes of the same Chrome; only how a
    browser is launched may branch on the mode.

    Sources, all merged into the same two piles:

    * ``attach(page)``   -- Playwright's response listener (either mode)
    * ``absorb_cdp(...)``-- the headed preload's CDP sink, which is the only
      thing that sees the article document on a headed run (Playwright is not
      connected yet when the preload navigates)
    """

    def __init__(self, doi: str = ''):
        self.doi = doi or ''
        self.documents: list = []
        self.api: dict = {}
        self._page = None
        self._listener = None

    # -- collection ---------------------------------------------------
    def attach(self, page):
        """Start recording *page*'s responses. Registers before any goto()."""
        async def _on_response(response):
            try:
                if (response.request.resource_type == 'document'
                        and response.ok
                        and 'text/html' in response.headers.get('content-type', '')):
                    self.documents.append(await response.text())
                    return
                # The endpoints the page fetches for itself. Keyed by URL plus
                # a counter so a publisher that answers the same URL twice
                # (before and after its bot check) keeps both, the way the CDP
                # sink keys by requestId.
                if response.ok and url_wants_api_harvest(response.url):
                    body = await response.text()
                    if body:
                        self.api[f"{response.url}#{len(self.api)}"] = {
                            'url': response.url,
                            'type': response.request.resource_type,
                            'status': response.status,
                            'body': body,
                        }
            except Exception:
                pass

        self._page = page
        self._listener = _on_response
        page.on('response', _on_response)
        return self

    def absorb_cdp(self, responses: dict) -> None:
        """Merge a CDP preload's capture (``result["responses"]``)."""
        for entry in (responses or {}).values():
            body = entry.get('body')
            if not body:
                continue
            if 'html' in (entry.get('mimeType') or '').lower():
                self.documents.append(body)
            elif (entry.get('type') or '') != 'Document':
                self.api[f"cdp:{len(self.api)}"] = entry

    # -- results ------------------------------------------------------
    def raw_html(self) -> str:
        """The document that is actually the article (see pick_raw_article_html)."""
        return pick_raw_article_html(self.documents, self.doi) or ''

    def announce(self, where: str) -> None:
        if self.api:
            print(f"  ✓ {where}捕获 API 响应 {len(self.api)} 条"
                  f"（最大 {max(len(v.get('body') or '') for v in self.api.values()):,} 字符）")
        if self.documents:
            print(f"  ✓ {where}捕获文档响应 {len(self.documents)} 份"
                  f"（最大 {max(len(h) for h in self.documents):,} 字符）")
        else:
            print(f"  ⚠️  {where}未捕到文档响应")

    def land(self, captured_data_dir, rendered_html: str = '') -> str:
        """Write page_raw.html (and page.html when a rendered DOM is given).

        Landing happens here, as soon as the bytes exist, rather than after
        extract_all: waiting meant page_raw.html appeared only once the whole
        extraction had run, and not at all if anything in between raised.
        """
        raw = self.raw_html()
        if not captured_data_dir:
            return raw
        try:
            captured_data_dir.mkdir(parents=True, exist_ok=True)
            if raw:
                save_html_snapshot(captured_data_dir / "page_raw.html", raw, "原始HTML")
            if rendered_html:
                save_html_snapshot(captured_data_dir / "page.html", rendered_html, "HTML")
        except Exception as e:
            print(f"  ⚠️  HTML 落盘失败: {e}")
        return raw

    def pin(self, handler, label: str = '') -> str:
        """Hand the capture to *handler* and return the raw article HTML."""
        raw = self.raw_html()
        pin_capture_on_handler(handler, raw, self.api, label)
        return raw


async def navigate_with_capture(page, urls, *, capture: PageCapture,
                                publisher_token: str = '',
                                extra_headers: dict = None,
                                timeout_s: float = None,
                                solve_challenge: bool = True):
    """Load the first of *urls* that works, recording everything it fetches.

    The single entry point both modes use: MathJax interception, request
    headers, the response listener, the navigation itself and the Cloudflare
    checkbox all happen here, in this order, so neither mode can end up with
    one of them and not the others.

    ⚠️ The listener has to exist before the first goto(), and block_mathjax
    before the first script request -- that is why this owns the navigation
    rather than being something a caller runs afterwards.

    Returns ``(final_url, rendered_html, error)``; *error* is the last
    navigation exception when every URL failed.
    """
    timeout_ms = int((timeout_s or DP_PAGE_LOAD_TIMEOUT) * 1000)

    if should_block_mathjax(publisher_token):
        await block_mathjax(page)
    elif publisher_token:
        print(f"  ⏭  {publisher_token.upper()} 读原始响应，不拦 MathJax")

    if extra_headers:
        try:
            await page.set_extra_http_headers(
                {str(k): str(v) for k, v in extra_headers.items()})
            print(f"  ↪ 附加 header(s): {list(extra_headers.keys())}")
        except Exception as e:
            print(f"  ⚠️  set_extra_http_headers 失败: {e}")

    capture.attach(page)

    last_error = None
    # urls=None means the page is already loaded (the headed preload opened it
    # over raw CDP before Playwright connected). Everything above still has to
    # run -- the listener for any navigation this flow makes later, the header
    # and MathJax settings for the same reason -- and the challenge check below
    # still applies to whatever is on screen.
    for candidate in ([] if urls is None else
                      ([urls] if isinstance(urls, str) else list(urls))):
        print(f"  ↪ 访问: {candidate}")
        try:
            await page.goto(candidate, wait_until='domcontentloaded', timeout=timeout_ms)
            try:
                await page.wait_for_load_state('networkidle', timeout=timeout_ms)
            except Exception:
                print("  ℹ️  页面主文档已加载，后台资源未完全静默，继续")
            last_error = None
            break
        except Exception as e:
            last_error = e
            print(f"  ⚠️  访问失败: {type(e).__name__}: {str(e)[:100]}")

    rendered = ''
    try:
        rendered = await page.content()
    except Exception:
        pass

    if solve_challenge and last_error is None:
        rendered = await _clear_challenge_if_present(page, rendered, timeout_ms)

    return (page.url if page else ''), rendered, last_error


async def _clear_challenge_if_present(page, rendered_html: str, timeout_ms: int) -> str:
    """Click through a Cloudflare checkbox if this page is one. Returns the HTML.

    ⚠️ Gated on detection rather than run unconditionally. auto_solve_bot_challenge
    waits ``DP_CLOUDFLARE_INITIAL_POLL`` seconds for a widget to appear before
    concluding there is none, and the headed path used to pay that on every
    single article -- including the overwhelming majority where the preload had
    already cleared the challenge, or where there never was one. The detector
    is the same one the headless branch already trusts to decide "blocked, fall
    back to headed", and it fires on a real interstitial on three independent
    signals (title, _cf_chl_opt, the challenge-platform script with a short
    body).
    """
    try:
        if not is_bot_challenge_page(page.url, rendered_html):
            return rendered_html
    except Exception:
        return rendered_html

    print("  🤖 检测到 Cloudflare 挑战页，尝试自动点击...")
    try:
        solved = await auto_solve_bot_challenge(
            page, timeout_s=DP_CLOUDFLARE_TIMEOUT,
            initial_poll_s=DP_CLOUDFLARE_INITIAL_POLL)
    except Exception as e:
        print(f"  ⚠️  auto_solve_bot_challenge 抛异常: {e}")
        return rendered_html
    if not solved:
        return rendered_html

    # A JS-only challenge answers the same URL 403 then 200 once the cookie is
    # set, so the listener never saw the article. Reload to capture it.
    print("  🔄 挑战已通过，重新加载页面以获取论文内容...")
    try:
        await page.goto(page.url, wait_until='networkidle', timeout=timeout_ms)
    except Exception as e:
        print(f"     ⚠️  重新加载异常: {e}")
    try:
        return await page.content()
    except Exception:
        return rendered_html


def save_html_snapshot(path, content: str, label: str = "HTML") -> bool:
    """Write *content* to *path*, unless the file already holds exactly that.

    Several places land the same snapshot: the preload writes page_raw.html as
    soon as it has the body, the headless precheck writes page.html and
    page_raw.html, and process_with_handler writes both again once extraction
    finishes. The later write is not simply redundant -- the response listener
    keeps appending during the handler's own navigations, so the final pick can
    legitimately differ -- but when it does not, rewriting identical bytes only
    produces a second "已保存" line that reads like two different snapshots.

    ⚠️ Compare bytes, never ``read_text()``. Text mode applies universal
    newlines, so a page served with CRLF comes back \n-normalised and never
    equals the string in hand -- measured on IEEE 10.1109/ACCESS.2020.2991812,
    where 12 CRLFs made a byte-identical snapshot look like a 64,131 → 64,143
    change and the skip never fired.

    Returns True when the file was actually written.
    """
    if not content:
        return False
    path = Path(path)
    payload = content.encode('utf-8')
    try:
        if path.exists() and path.read_bytes() == payload:
            print(f"  ↪ {label} 未变，跳过重写: {path.name} ({len(content):,} 字符)")
            return False
    except Exception:
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    except Exception as e:
        print(f"  ⚠️  {label} 保存失败: {type(e).__name__}: {str(e)[:80]}")
        return False
    print(f"  ✓ {label}已保存: {path.name} ({len(content):,} 字符)")
    return True


def _crossref_headless_publisher(crossref_data: dict):
    """Return the HEADLESS_ACCESSIBLE_PUBLISHERS entry Crossref's publisher matches.

    Word-boundary matching, so a short key like 'oup' does not match inside an
    unrelated word like 'group' (e.g. "Optica Publishing Group").

    None means unknown or not in the list -- i.e. this publisher needs a headed
    browser. Both the Phase 0 decision and the pdf_link direct download consult
    this, so the two cannot drift apart.
    """
    crossref_publisher = (crossref_data.get('publisher') or '').lower()
    if not crossref_publisher:
        return None
    for publisher_name in HEADLESS_ACCESSIBLE_PUBLISHERS:
        pattern = r'\b' + re.escape(publisher_name.lower()) + r'\b'
        if re.search(pattern, crossref_publisher):
            return publisher_name
    return None


# Crossref `type` values that indicate the DOI belongs to a book or one of
# its chapters. When we see one of these on an OUP DOI, route to the book
# handler so the whole book gets aggregated rather than just one chapter.
_CROSSREF_BOOK_TYPES = {'book', 'monograph', 'book-chapter', 'reference-book',
                        'edited-book', 'book-section', 'book-part'}


def apply_crossref_type_override(publisher: str, crossref_data: dict) -> str:
    """Promote generic publishers to a book-specific handler when Crossref says
    the DOI is a book.

    - `oup`     -> `oup_book` for any book-typed Crossref entry.
    - `nature`  -> `springer_book` when the Crossref publisher is Springer-family
      (covers reference works whose redirect URL is
      `link.springer.com/referencework/...` rather than `/book/...`).
    """
    if not crossref_data:
        return publisher
    crossref_type = (crossref_data.get('type') or '').strip().lower()
    if crossref_type not in _CROSSREF_BOOK_TYPES:
        return publisher
    if publisher == 'oup':
        return 'oup_book'
    if publisher == 'nature':
        crossref_publisher = (crossref_data.get('publisher') or '').strip().lower()
        if 'springer' in crossref_publisher:
            return 'springer_book'
    return publisher
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.tif', '.tiff', '.svg'}

MIME_TO_EXT = {
    'application/pdf': '.pdf',
    'application/zip': '.zip',
    'application/gzip': '.gz',
    'application/x-gzip': '.gz',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'application/msword': '.doc',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
    'application/vnd.ms-excel': '.xls',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
    'application/vnd.ms-powerpoint': '.ppt',
    'text/csv': '.csv',
    'text/plain': '.txt',
    'video/mp4': '.mp4',
    'video/mpeg': '.mpeg',
    'video/quicktime': '.mov',
    'video/x-msvideo': '.avi',
    'video/x-matroska': '.mkv',
    'video/webm': '.webm',
    'audio/mpeg': '.mp3',
    'audio/mp4': '.m4a',
    'audio/wav': '.wav',
    'audio/ogg': '.ogg',
}


def _detect_and_rename(filepath: Path) -> Path:
    """Detect file type from header bytes and rename with correct extension."""
    mime = magic.from_file(str(filepath), mime=True)
    ext = MIME_TO_EXT.get(mime, '')
    if not ext or filepath.suffix == ext:
        return filepath
    new_path = filepath.with_suffix(filepath.suffix + ext)
    filepath.rename(new_path)
    return new_path


def is_bot_challenge_page(url: str, html: str = None) -> bool:
    """Detect whether the current page is an anti-bot challenge rather than a real article.

    Checks URL patterns and page content for common bot-detection / CAPTCHA indicators.
    """
    # The URL half lives in core.utilities: chrome_session needs the same test
    # and cannot import this module (cycle), and two copies of a list like this
    # drift the moment one publisher's interstitial gets added to one of them.
    if url_looks_like_bot_challenge(url):
        return True

    if html:
        html_lower = html.lower()
        # High-confidence markers — these only appear on actual challenge pages.
        # NB: bare 'cloudflare' and 'cdn-cgi/challenge-platform' substrings also
        # appear in Cloudflare's harmless JSD tracking script (cdn-cgi/challenge-platform/scripts/jsd/main.js)
        # which is injected on REAL article pages too. Use stricter markers
        # to avoid false positives on Cloudflare-protected sites like
        # cambridge.org and journals.aps.org.
        challenge_markers = [
            'bot manager',
            'request unsuccessful',
            'are you a bot',
            'verify you are human',
            'please verify',
            'security check',
            'ddos protection',
            'incident id',
            'radware',
            'perfdrive',
            # Cloudflare-challenge-specific markers (NOT the generic JSD tracker)
            'cf-browser-verification',
            'cf-chl-bypass',
            'cf-error-details',
            '_cf_chl_opt',
            'just a moment...',
            'checking your browser before',
            'cdn-cgi/challenge-platform/h/',  # the challenge HTML path, not /scripts/jsd/
            'turnstile',
            '正在进行安全验证',
            'security verification',
            'enable javascript and cookies to continue',
        ]
        marker_count = sum(1 for m in challenge_markers if m in html_lower)
        # Large pages with full article content shouldn't be challenge pages
        # regardless of incidental keyword matches in scripts/analytics.
        html_size = len(html)
        if html_size > 100_000:
            # A real article page is typically 100KB+; only treat as challenge
            # if multiple high-confidence markers and the page looks short on content.
            if marker_count >= 3:
                return True
        else:
            if marker_count >= 2:
                return True
        if html_size < 5000 and any(
            m in html_lower for m in ['verify', 'challenge', 'captcha', 'robot', 'bot']
        ):
            return True

    return False


# ------------------------------------------------------------------------
# Cloudflare Turnstile auto-clicker
# ------------------------------------------------------------------------
# The "Verify you are human" checkbox on ScienceDirect / Cambridge / etc.
# is a Cloudflare Turnstile widget delivered in a cross-origin iframe from
# challenges.cloudflare.com. We can't touch the iframe DOM (same-origin
# policy), but a synthetic mouse click at the checkbox screen coordinates
# is often enough for the "managed" and "invisible" Turnstile variants.
# Interactive "challenge" variants (image puzzle) will still fall through
# and require a human — but the simple checkbox case succeeds most of the
# time in headed mode with a persistent Chrome profile.

_TURNSTILE_IFRAME_SELECTORS = (
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="cf-chl-widget"]',
    'iframe[src*="cdn-cgi/challenge-platform"]',  # publisher self-hosted CDN
    'iframe[src*="turnstile"]',
    'iframe[title*="widget containing a Cloudflare" i]',
    'iframe[title*="Cloudflare security challenge" i]',
    'iframe[title*="Verify you are human" i]',
    'iframe[title*="human" i]',
    'iframe[title*="challenge" i]',
)

# URL substrings that identify a Cloudflare challenge frame regardless of
# how it's embedded (top-level iframe, nested iframe, cross-origin).
_TURNSTILE_URL_MARKERS = (
    'challenges.cloudflare.com',
    'cf-chl-widget',
    'cdn-cgi/challenge-platform',
    'turnstile',
)


def _looks_like_challenge_frame(frame) -> bool:
    """True if a Playwright Frame's URL looks like a Cloudflare challenge."""
    url = (frame.url or '').lower()
    if not url or url == 'about:blank':
        return False
    return any(marker in url for marker in _TURNSTILE_URL_MARKERS)


async def _find_turnstile_iframe(page):
    """Return the first visible Cloudflare Turnstile iframe element and its
    Frame object, or (None, None). We check the top DOM first (fast path),
    then fall back to enumerating every frame in the page tree — that
    catches nested / same-origin-wrapped challenge widgets that
    ``page.query_selector('iframe[src*=...]')`` misses because their URL
    lives on the Frame, not on the <iframe> src attribute.
    """
    # Fast path: match by <iframe> src attribute or title text.
    for selector in _TURNSTILE_IFRAME_SELECTORS:
        try:
            el = await page.query_selector(selector)
        except Exception:
            el = None
        if el:
            try:
                if not await el.is_visible():
                    continue
            except Exception:
                pass
            return el, None

    # Fallback: walk the frame tree. Cloudflare's checkbox lives in a
    # cross-origin frame, and Playwright exposes that as an entry in
    # page.frames even when we can't find a matching <iframe> element in
    # the main-frame DOM.
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        if _looks_like_challenge_frame(frame):
            try:
                element = await frame.frame_element()
                if element:
                    try:
                        if not await element.is_visible():
                            continue
                    except Exception:
                        pass
                    return element, frame
            except Exception:
                continue
    return None, None


async def auto_solve_bot_challenge(
    page,
    timeout_s: float = 30.0,
    initial_poll_s: float = 4.0,
) -> bool:
    """Best-effort auto-click a Cloudflare Turnstile checkbox.

    Polls for a Turnstile iframe. If one is found, clicks at
    (30, height/2) inside its bounding box — that's where the checkbox
    sits in every widget size Cloudflare currently ships. Waits for the
    iframe to disappear or for URL navigation, either of which signals
    the challenge cleared.

    Two timeouts:
      * ``initial_poll_s`` — how long to wait for a widget to APPEAR at
        all. If nothing shows in this window we assume there's no
        challenge on this page and return False immediately. This keeps
        the happy path (no challenge) from burning the full ``timeout_s``
        on every download-page navigation.
      * ``timeout_s`` — total budget once a widget IS seen (covers the
        click + validation + navigation-back-to-real-page). Only used
        after a widget is found in the initial window.

    Returns True if a challenge was found AND appears resolved, False
    otherwise (including "no challenge present" — that's the happy path
    for pages that don't need a click).
    """
    import asyncio

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    initial_deadline = loop.time() + initial_poll_s
    click_attempts = 0
    saw_widget = False

    # Give the page a moment to render any deferred Cloudflare widget.
    await asyncio.sleep(1)

    while loop.time() < deadline:
        iframe, frame = await _find_turnstile_iframe(page)
        if iframe is None:
            if saw_widget:
                # Widget was there earlier and is now gone → success.
                print("  ✓ Cloudflare Turnstile 已通过")
                return True
            # No widget yet. Give up quickly if we've been in the initial
            # window for long enough with no widget — most pages have no
            # challenge and we don't want to hang each PDF/figure download
            # for 30 s just to prove there's nothing to click.
            if loop.time() >= initial_deadline:
                # No Turnstile widget in initial window — break out and
                # let the JS-challenge detection block below take over.
                break
            await asyncio.sleep(1)
            continue

        saw_widget = True
        try:
            box = await iframe.bounding_box()
        except Exception:
            box = None

        if not box or box['width'] < 1 or box['height'] < 1:
            await asyncio.sleep(1)
            continue

        # Click position — the checkbox sits on the left side of the widget
        # at x≈30 in every Turnstile size (compact/normal/managed). Y is
        # simply the vertical centre of the iframe.
        x = box['x'] + 30
        y = box['y'] + box['height'] / 2
        click_attempts += 1
        frame_url = (frame.url if frame else '(inline)')[:80]
        print(f"  🤖 检测到 Cloudflare 挑战 iframe ({box['width']:.0f}x{box['height']:.0f}) "
              f"[{frame_url}]，点击 @ ({x:.0f}, {y:.0f})  [第 {click_attempts} 次]")
        try:
            await page.mouse.move(x, y)
            await asyncio.sleep(0.1)
            await page.mouse.click(x, y, delay=60)
        except Exception as e:
            print(f"  ⚠️  Turnstile 点击失败: {e}")

        # Give Cloudflare a few seconds to validate the click. Success
        # manifests as either (a) the iframe disappearing, or (b) the
        # page navigating away (e.g. to the real article).
        for _ in range(6):
            await asyncio.sleep(1)
            gone, _ = await _find_turnstile_iframe(page)
            if gone is None:
                print("  ✓ Cloudflare Turnstile 已通过")
                # Give the real article page a moment to settle so the
                # caller can read page.content()/page.url reliably.
                try:
                    await page.wait_for_load_state('networkidle', timeout=int(DP_PAGE_LOAD_TIMEOUT * 1000))
                except Exception:
                    pass
                return True

        # Cap total attempts so we don't burn the whole timeout in a loop
        # on a stuck widget.
        if click_attempts >= 3:
            break

    if saw_widget:
        print(f"  ⚠️  Cloudflare 挑战未在 {timeout_s:.0f}s 内自动通过 — "
              "可能需要人工点击 (headed 模式下手动完成即可继续)")
        return False

    # No Turnstile checkbox found — check if this is a newer-style
    # Cloudflare JS challenge ("Just a moment..." page without a
    # clickable widget). These resolve automatically when the browser
    # passes the JS fingerprinting check, indicated by a cf_clearance
    # cookie appearing and/or the page navigating away.
    try:
        page_url = (page.url or '').lower()
        page_title = (await page.title()).lower()
        # Wait a moment for the challenge page JS to execute and render the title.
        # Cloudflare's JS challenge often starts with an empty/blank page
        # and the title updates after the orchestrator script runs.
        for _ in range(15):
            await asyncio.sleep(1)
            try:
                page_title = (await page.title()).lower()
                if any(w in page_title for w in ['just a moment', 'challenge', 'verify you are human', 'security']):
                    break
            except Exception:
                pass
            page_url = (page.url or '').lower()
            if any(w in page_url for w in ['cdn-cgi', '__cf_chl', 'challenge']):
                break
        is_challenge_page = (
            'cdn-cgi' in page_url
            or 'challenge' in page_url
            or 'just a moment' in page_title
            or 'verify you are human' in page_title
            or '__cf_chl' in page_url
            or 'security verification' in page_title
        )
        if not is_challenge_page:
            print(f"DEBUG: is_challenge_page=False, url={page_url[:80]!r}, title={page_title!r}")
            return False

        print(f"  🤖 检测到 Cloudflare JS 挑战页 (title={page_title!r})，等待自动通过 (最长 {timeout_s:.0f}s)...")
        cf_deadline = loop.time() + timeout_s
        check_interval = max(5.0, min(30.0, timeout_s / 40))
        print(f"  ⏱️   轮询间隔: {check_interval:.1f}s")
        while loop.time() < cf_deadline:
            await asyncio.sleep(check_interval)
            # Most reliable signal: page title is no longer a challenge title.
            # URL-based checks fail for sites like AIP that serve the challenge
            # directly on the article URL (same URL, 403 + challenge body).
            try:
                current_title = (await page.title()).lower()
            except Exception:
                current_title = ''
            challenge_keywords = ['just a moment', 'verify you are human',
                                  'security verification', 'attention required']
            still_challenge = any(kw in current_title for kw in challenge_keywords)

            # Also require cf_clearance cookie (proves CF JS actually ran)
            cookies = await page.context.cookies()
            has_cf_clearance = any(
                c.get('name', '') == 'cf_clearance' and c.get('value')
                for c in cookies
            )

            # Check body text for 'Verification successful' signal.
            # Cloudflare's challenge page sometimes shows this text when
            # the JS challenge has passed but the page hasn't auto-
            # redirected yet (e.g. when challenge iframe's postMessage
            # fails due to origin issues).
            try:
                body_text = await page.evaluate('document.body?.innerText || ""')
            except Exception:
                body_text = ''
            verification_successful = 'verification successful' in body_text.lower()

            if has_cf_clearance and (not still_challenge or verification_successful):
                # Real page loaded — wait for network to settle
                try:
                    await page.wait_for_load_state('networkidle', timeout=int(DP_PAGE_LOAD_TIMEOUT * 1000))
                except Exception:
                    pass
                reason = "title changed" if not still_challenge else "verification successful"
                print(f"  ✓ Cloudflare JS 挑战已通过 ({reason})")
                return True

        # Timed out — still on challenge page
        current_title = ''
        try:
            current_title = (await page.title()).lower()
        except Exception:
            pass
        print(f"  ⚠️  Cloudflare JS 挑战未在 {timeout_s}s 内自动通过 "
              f"(title={current_title!r})")
        print(f"  🔎 frame tree at timeout:")
        for i, fr in enumerate(page.frames):
            print(f"       [{i}] {(fr.url or '(no url)')[:120]}")
        return False
    except Exception as e:
        print(f"  ⚠️  Cloudflare JS 挑战检测异常: {e}")
        return False



HEADLESS_AUTH_STATE_FILE = Path(
    os.environ.get(
        "DOWNLOAD_PAPER_HEADLESS_AUTH_STATE",
        Path(__file__).resolve().parent / ".auth" / "headless_storage_state.json",
    )
).expanduser()

# 全局变量仅用于兼容信号处理；实际生命周期由 SharedBrowserSession 管理。
_active_browser_session = None

def _cleanup_chrome_launcher():
    """同步兜底：只清理本批次拥有的 Chrome，不误杀其他并发任务。"""
    if _active_browser_session is not None:
        _active_browser_session.cleanup_owned_chrome_sync()
    # The browsers are down, so the profiles they held can go. Runs on the
    # normal exit, on an exception, and from the SIGINT/SIGTERM handler, so a
    # killed batch does not leave its profile root behind either.
    try:
        cleanup_profile_root()
    except Exception as exc:
        print(f"  ⚠️  清理 profile 目录失败: {exc}")

def _signal_handler(signum, frame):
    """SIGINT信号处理器 - 清理子进程然后退出"""
    print("\n\n⚠️  收到中断信号，正在清理子进程...")
    _cleanup_chrome_launcher()
    sys.exit(130)  # 标准SIGINT退出码


class SharedBrowserSession:
    """One headed Chrome and one headless context shared by a DOI batch."""

    def __init__(self, playwright):
        self.playwright = playwright
        self.headless_browser = None
        self.headless_context = None
        self.headed_process = None
        self.headed_profile_dir = None
        self.owns_headed_profile = False
        self.headed_browser = None
        self.headed_context = None
        self.latest_headed_state = None

    @staticmethod
    def _chrome_ready() -> bool:
        import socket
        try:
            with socket.create_connection(("127.0.0.1", CHROME_DEBUG_PORT), timeout=2):
                return True
        except OSError:
            return False

    def _check_cdp_port(self) -> bool:
        import socket
        try:
            with socket.create_connection(("127.0.0.1", CHROME_DEBUG_PORT), timeout=2):
                return True
        except OSError:
            return False

    async def launch_headed_chrome(self) -> bool:
        """只启动独立 Chrome，不连接 Playwright。
        目的：chrome_session 过 Cloudflare 之前，避免 Playwright 注入自动化指纹。"""
        if self._check_cdp_port():
            if not FRESH_PROFILE:
                print("  ✓ Chrome 已在运行 (CDP 端口就绪)")
                return True
            # FRESH_PROFILE 下复用是错的：端口上那个实例是上次运行留下的，
            # 带着它累积的自动化指纹，接管它等于把"全新 profile"悄悄作废。
            print("  ⚠️  端口上有上次残留的 Chrome，FRESH_PROFILE=1 下不复用，先关掉")
            kill_chrome()
            await asyncio.sleep(2)

        sweep_stale_profiles()

        print("  启动独立 Chrome...")
        try:
            proc, profile_dir, owns = launch_chrome(return_details=True)
            self.headed_process = proc
            # 记下来才删得掉 —— 以前这里丢掉了返回值，下面那段 rmtree
            # 因此是死代码，临时 profile 只增不减。
            self.headed_profile_dir = profile_dir
            self.owns_headed_profile = owns
            for _ in range(30):
                await asyncio.sleep(1)
                if self._check_cdp_port():
                    break
            if self._check_cdp_port():
                print("✓ Chrome 已启动 (CDP 端口就绪)")
                return True
            print("⚠️  Chrome 启动但 CDP 端口未响应")
            return False
        except Exception as exc:
            print(f"⚠️  启动Chrome失败: {exc}")
            return False

    async def connect_headed_browser(self) -> bool:
        """将 Playwright connect 到已启动的 Chrome。
        注意：必须在 chrome_session 过挑战之后调用，否则 Playwright 指纹会导致 Cloudflare 403。"""
        if self.headed_browser is not None and self.headed_browser.is_connected():
            return True
        try:
            self.headed_browser = await self.playwright.chromium.connect_over_cdp(
                f"http://localhost:{CHROME_DEBUG_PORT}"
            )
            print("✓ Playwright 已连接到 Chrome (CDP)")
            return True
        except Exception as exc:
            print(f"⚠️  Playwright 连接Chrome失败: {exc}")
            return False

    async def ensure_headed_chrome(self) -> bool:
        # 使用独立启动的 Chrome + CDP 连接。
        # 原因：Playwright 自带的 chromium 过不了 Cloudflare（用户确认），
        # 且 Playwright 连接 CDP 会留下自动化指纹。
        # 解决方案：先用 chrome_session.launch_chrome 启动独立 Chrome，
        # 再用纯 CDP WebSocket 过 Cloudflare 挑战（chrome_session），
        # 最后 Playwright 才 connect_over_cdp 接棒抓论文。
        if self.headed_browser is not None and self.headed_browser.is_connected():
            return True
        if not await self.launch_headed_chrome():
            return False
        return await self.connect_headed_browser()

    async def ensure_headless_context(self, storage_state=None):
        if self.headless_context is not None:
            return self.headless_context

        # A persistent context rather than launch(): the profile directory is
        # what carries always_open_pdf_externally. Without it real Chrome shows
        # a PDF in its built-in viewer instead of downloading it, so the
        # download event never fires and every paper burned a 30s timeout
        # before falling back to the throwaway browser. Playwright's bundled
        # Chromium has no PDF viewer, which is why this only appeared once
        # CHROME_PATH started being honoured here.
        user_data_dir = scraping_profile_dir(HEADLESS_PROFILE_NAME)
        prepare_profile_dir(user_data_dir, quiet=True)

        launch_kwargs = {"headless": True, "accept_downloads": True,
                         "args": chrome_password_store_args()}
        chrome_path = os.environ.get("CHROME_PATH", "").strip()
        if chrome_path:
            launch_kwargs["executable_path"] = chrome_path
        # A persistent context owns the browser; there is no separate object.
        self.headless_browser = None
        self.headless_context = await self.playwright.chromium.launch_persistent_context(
            str(user_data_dir), **launch_kwargs)

        # storage_state is a launch()-only option; a persistent context takes
        # its cookies afterwards.
        state = self.latest_headed_state or storage_state
        cookies = (state or {}).get("cookies") or []
        if cookies:
            try:
                await self.headless_context.add_cookies(cookies)
            except Exception as exc:
                print(f"  ⚠️  无头context载入cookies失败: {str(exc)[:80]}")
        print(f"  ↔ 共享无头context已创建，载入 {len(cookies)} 个cookies")
        return self.headless_context

    async def ensure_headed_context(self):
        # 快速路径：browser 已连接 + context 非空 + context 仍然有效
        if (self.headed_browser is not None
                and self.headed_browser.is_connected()
                and self.headed_context is not None):
            # 校验 context 是否真的还活着（关闭最后一个 tab 后 Chrome 会销毁 context）
            try:
                _ = self.headed_context.pages
                return self.headed_browser, self.headed_context
            except Exception:
                # context 已失效，清空缓存重走创建流程
                self.headed_context = None
        if not await self.ensure_headed_chrome():
            return None
        _stealth_js = """
            // Hide webdriver flag
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });
            // Restore missing plugins (headless/automated Chrome has 0)
            if (navigator.plugins && navigator.plugins.length === 0) {
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
                        { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
                    ],
                });
            }
            if (navigator.languages && navigator.languages.length === 0) {
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                });
            }
            // Fix permissions query
            const originalQuery = window.navigator.permissions.query;
            if (originalQuery) {
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : originalQuery(parameters)
                );
            }
            // Hide CDP-specific global
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_;
        """

        if self.headed_browser.contexts:
            self.headed_context = self.headed_browser.contexts[0]
            # Inject stealth into the default context too
            if DP_STEALTH_JS:
                await self.headed_context.add_init_script(_stealth_js)
            else:
                print("  🫥 DP_STEALTH_JS=0 — 不注入反检测补丁")
        else:
            self.headed_context = await self.headed_browser.new_context(
                accept_downloads=True,
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )
            if DP_STEALTH_JS:
                await self.headed_context.add_init_script(_stealth_js)
            else:
                print("  🫥 DP_STEALTH_JS=0 — 不注入反检测补丁")

        return self.headed_browser, self.headed_context

    async def sync_headed_to_headless(self, headed_context):
        self.latest_headed_state = await headed_context.storage_state()
        cookies = self.latest_headed_state.get("cookies", [])
        if self.headless_context is not None:
            await self.headless_context.add_cookies(cookies)
        print(f"  ↔ 有头→无头 cookie同步: {len(cookies)}")

    async def sync_headless_to_headed(self, headed_context):
        if self.headless_context is not None:
            cookies = await self.headless_context.cookies()
            await headed_context.add_cookies(cookies)
            print(f"  ↔ 无头→有头 cookie同步: {len(cookies)}")

    def cleanup_owned_chrome_sync(self):
        proc = self.headed_process
        self.headed_process = None
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=10)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass
        if self.owns_headed_profile and self.headed_profile_dir:
            shutil.rmtree(self.headed_profile_dir, ignore_errors=True)
        self.headed_profile_dir = None
        self.owns_headed_profile = False
        self.headed_context = None
        self.headed_browser = None

    # ------------------------------------------------------------------
    # Scraping-profile hygiene
    # ------------------------------------------------------------------
    # Files worth carrying over from the clean profile. Cookies bring the
    # publisher sessions and Cloudflare clearance; "Local State" holds the key
    # those cookies are encrypted with (copy one without the other and every
    # cookie decodes to garbage); Preferences carries the PDF-download
    # settings ensure_chrome_preferences() would otherwise have to rebuild.
    # Deliberately NOT copied: History, Cache, extensions, Sessions — bulky,
    # and the point of the reset is to shed accumulated state.
    # Seeding lives in chrome_session so the shared browser and the
    # throwaway PDF browser cannot drift apart on which files they copy.
    _PROFILE_SEED_FILES = PROFILE_SEED_FILES
    _PROFILE_SEED_ROOT_FILES = PROFILE_SEED_ROOT_FILES

    def retire_headed_browser(self) -> bool:
        """Shut the shared Chrome down so the next paper opens a new session.

        A profile driven over CDP wears its automation traces as it goes, so
        it is never carried into the next paper: closing the browser here is
        what makes the next ensure_headed_chrome() rebuild the profile from
        scratch (see chrome_session.prepare_profile_dir). Chrome holds an
        exclusive lock on its user-data-dir, so it has to exit before that
        directory can be replaced.

        Returns True when a browser was actually running and got closed.
        """
        if self.headed_process is None and not self._check_cdp_port():
            return False

        print("  🔄 本篇结束，关闭抓取浏览器（下一篇将重建 profile）")
        self.headed_context = None
        self.headed_browser = None
        self.cleanup_owned_chrome_sync()
        time.sleep(2)
        return True

    async def close(self):
        if self.headless_context is not None:
            try:
                await self.headless_context.close()
            except Exception:
                pass
            self.headless_context = None
        if self.headless_browser is not None:
            try:
                await self.headless_browser.close()
            except Exception:
                pass
            self.headless_browser = None
        self.headed_context = None
        self.headed_browser = None
        self.cleanup_owned_chrome_sync()

# ============================================================================
# Publisher Detection and Handler Factory
# ============================================================================

def detect_publisher(url: str) -> str:
    """
    Detect which publisher based on URL domain or DOI

    Returns: 'aps' | 'nature' | 'aip' | 'unknown'

    Note: This is a wrapper around orchestrator.detect_publisher_from_url
    for backward compatibility. New code should use the orchestrator module.
    """
    return detect_publisher_from_url(url)


def _publisher_for_page(url: str = '', doi: str = '') -> str:
    """Best guess at the publisher *before* the page has been navigated to.

    Used only to decide whether an interception still has to be installed, so
    it runs ahead of the handler existing. The URL is tried first (a --json
    ``link`` names the publisher outright); the DOI is the fallback, which is
    what the plain flow has at that point, since ``https://doi.org/{doi}``
    carries the prefix the detector matches on.

    Uses the orchestrator's detector, not ``core.utilities``' same-named but
    much older copy -- that one knows seven publishers and would answer
    'unknown' for most of the tree.
    """
    pub = detect_publisher_from_url(url or '')
    if pub in ('', 'unknown'):
        pub = detect_publisher_from_url(doi or '')
    return pub


def get_publisher_handler_factory(publisher: str, **kwargs):
    """
    Factory function to get appropriate publisher handler

    Note: This is a wrapper around orchestrator.get_publisher_handler
    for backward compatibility. New code should use the orchestrator module.
    """
    return get_publisher_handler(publisher, **kwargs)


def fetch_metadata_with_priority(doi: str) -> dict:
    """Fetch paper metadata with priority: Crossref → Semantic Scholar

    Attempts to fetch metadata from Crossref first (richer data: publisher, ISBN, references),
    then falls back to Semantic Scholar if Crossref fails.

    Args:
        doi: Digital Object Identifier (without 'https://doi.org/' prefix)

    Returns:
        dict with metadata from whichever source succeeds
    """
    print("\n🔍 获取论文元数据...")
    print("=" * 80)

    # Try Crossref first (primary source - has publisher, date, ISBN, references)
    print("  → 尝试 Crossref (优先)...")
    crossref_data = fetch_crossref(doi)

    if crossref_data and crossref_data.get('title'):
        print("  ✓ 使用 Crossref 数据\n")
        return crossref_data

    # Fallback to Semantic Scholar
    print("  → Crossref 未获取到数据，尝试 Semantic Scholar (备用)...")
    s2_data = fetch_semanticscholar(doi)

    if s2_data and s2_data.get('title'):
        print("  ✓ 使用 Semantic Scholar 数据\n")
        return s2_data

    print("  ⚠️  两个来源都未获取到元数据\n")
    return {}


# ============================================================================
# Semantic Scholar API 配置
# ============================================================================
S2_API_URL = "https://api.semanticscholar.org/graph/v1/paper/DOI:"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json'
}



def normalize_image_url(image_url: str, base_url: str = None) -> str:
    """Normalize protocol-relative and relative image URLs."""
    if not image_url:
        return image_url

    image_url = image_url.strip()
    if image_url.startswith('//'):
        return f"https:{image_url}"
    if image_url.startswith('https://www.nature.com//'):
        return image_url.replace('https://www.nature.com//', 'https://', 1)
    if base_url and not image_url.startswith(('http://', 'https://', 'data:')):
        return urljoin(base_url, image_url)
    return image_url


def original_image_filename(image_url: str, fig_num: int, default_ext: str = '.png') -> str:
    """Return a safe local filename based on the source image URL basename."""
    if image_url:
        parsed = urlparse(image_url)
        basename = Path(unquote(parsed.path or '')).name
        basename = re.sub(r'[/\\:*?"<>|\x00-\x1f]', '-', basename).strip().strip('.')

        # ⚠️ Two limits again. 180 is just "stop a pathological URL"; on Windows
        # the whole path is bounded, and figures sit DIRECTLY in the paper
        # directory, so a long figure name is the longest tail there is --
        # longer than the supplemental one, which merely sits deeper.
        # organize_paper_output reserves WINDOWS_CHILD_RESERVE (104) for the
        # deepest child, so cap at 80 here to stay inside it. Real publisher
        # names are ~19 chars (ppcfadd59df1_hr.jpg), so this never bites.
        name_cap = 80 if IS_WINDOWS else 180
        if basename and Path(basename).suffix.lower() in IMAGE_EXTENSIONS:
            if len(basename) > name_cap:
                suffix = Path(basename).suffix
                basename = f"{Path(basename).stem[:name_cap - len(suffix)]}{suffix}"
            return basename

    return f"figure_{fig_num}{default_ext}"


# ============================================================================
# Phase 4: Unified Download Manager with Retry Logic
# ============================================================================

async def _looks_like_article_page(pw_page, url: str, doi: str) -> bool:
    """True if *pw_page* actually holds the article, not a helper document."""
    try:
        html = await pw_page.content()
    except Exception:
        return False
    if not html or len(html) < 2000:
        return False
    lowered = html.lower()
    if 'citation_doi' in lowered or 'citation_title' in lowered:
        return True
    if doi and doi.lower() in lowered:
        return True
    # Last resort: the right host and a body of real size.
    try:
        from urllib.parse import urlparse
        return (urlparse(pw_page.url).netloc
                and urlparse(pw_page.url).netloc == urlparse(url).netloc
                and len(html) > 20000)
    except Exception:
        return False


async def _pick_article_page(browser, candidate, url: str, doi: str):
    """Return the page holding the article, preferring *candidate*.

    Falls back to scanning every open page, because the CDP target the
    preload reports can be an out-of-process iframe rather than the tab the
    article rendered in.
    """
    if candidate is not None and await _looks_like_article_page(candidate, url, doi):
        return candidate

    if candidate is not None:
        print("  ⚠️  预载目标不是文章页面（可能是广告/同步 iframe），继续查找...")

    for ctx in browser.contexts:
        for pg in ctx.pages:
            if pg is candidate:
                continue
            if await _looks_like_article_page(pg, url, doi):
                try:
                    print(f"  ✓ 改用文章页面: {pg.url[:90]}")
                except Exception:
                    pass
                return pg
    return candidate


async def _find_pw_page_by_cdp_target(browser, target_id):
    """在 Playwright 连接的 browser 中按 CDP targetId 精确定位 page。
    targetId 是 tab 的唯一标识，页面重定向（doi.org -> 文章页）后不变，
    与 URL 内容无关 —— 适用于任何 publisher（含 SD 这类 URL 里不带 doi 的）。"""
    if not target_id or browser is None:
        return None
    for _ct in browser.contexts:
        for _pg in _ct.pages:
            try:
                _sess = await _ct.new_cdp_session(_pg)
                _info = await _sess.send("Target.getTargetInfo")
                _tid = _info.get("targetInfo", {}).get("targetId", "")
                if _tid == target_id:
                    return _pg
            except Exception:
                continue
    return None


async def retry_download(download_func, *args, max_retries=DP_MAX_RETRIES, retry_delay=DP_RETRY_DELAY, **kwargs):
    """Retry a download function, pausing between attempts.

    ⚠️ The pause used to sit inside the ``except`` branch only, so a function
    that reports failure by **returning None** -- which is how every rung of
    the fetch ladder reports it -- was retried with no gap whatsoever. Nothing
    else paces a single paper either: BATCH_SLEEP separates papers, not
    attempts. Measured on IOP, five attempts fired back to back within a couple
    of minutes, and against a bot manager that scores by IP that is the worst
    possible shape -- the one run that ever succeeded did so only after several
    minutes had passed.

    The pause is *retry_delay* flat -- the same wait before every attempt, no
    growth. See DP_RETRY_DELAY (90 s) for why it is that long. Figures pass
    DP_IMG_RETRY_DELAY (10 s) instead: they are not what the pacing is for, and
    a paper has dozens of them.
    """
    for attempt in range(max_retries):
        reason = ''
        try:
            result = await download_func(*args, **kwargs)
            if result is not None:
                if attempt > 0:
                    print(f"    ✓ 重试成功 (第 {attempt + 1} 次尝试)")
                return result
        except Exception as e:
            reason = f": {str(e)[:100]}"
        if attempt < max_retries - 1:
            print(f"    ⚠️  本次未拿到文件{reason}，{retry_delay:g}s 后重试 "
                  f"(尝试 {attempt + 1}/{max_retries})")
            await asyncio.sleep(retry_delay)
        else:
            print(f"    ❌ 已达最大重试次数 ({max_retries}){reason}")
    return None


async def _download_all_resources(
    page,
    links: dict,
    output_dir: Path,
    context,
    metadata: dict,
    doi: str = None,
    force_headed: bool = False,
    reuse_context: bool = False,
    pdf_only: bool = False,
    referer_url: str = '',
) -> dict:
    """Unified download manager for all resources (PDF, figures, supplemental)

    Args:
        page: Playwright page instance
        links: dict with 'pdf_url', 'figure_urls', 'supplemental_urls'
        output_dir: Output directory for downloads
        context: Playwright browser context (for new tabs)
        metadata: Paper metadata (for supplemental material naming)
        doi: DOI for constructing PDF URL if pdf_url not provided
        pdf_only: stop once the PDF is in. Figures, the key image and the
            supplemental files exist to be referenced from the markdown, and
            a pdf-only run writes no markdown

    Returns:
        dict with 'pdf', 'figures', 'supplemental' keys
    """
    downloads = {
        'pdf': None,
        'figures': {},
        'supplemental': [],
        'key_image': None,
    }

    download_playwright = None
    download_browser = None
    download_context = context
    download_page = page

    if not force_headed and not reuse_context:
        download_playwright = await async_playwright().start()
        download_browser = await download_playwright.chromium.launch(headless=True)
        download_context = await download_browser.new_context(accept_downloads=True)
        download_page = await download_context.new_page()
        print("  ✓ 使用无头浏览器执行资源下载")

    try:
        # Download PDF
        pdf_url = links.get('pdf_url')
        if pdf_url:
            print("Step 4️⃣  下载论文PDF...")
            print("=" * 80)
            try:
                pdf_filename = "paper.pdf"

                # Every publisher goes through the same ladder now. IEEE used
                # to need its own in-page fetch here because get_pdf_url handed
                # back a viewer URL that no navigation could download; it
                # returns /stampPDF/getPDF.jsp instead, which serves the bytes.
                pdf_result = await retry_download(
                    download_pdf,
                    download_page, pdf_url, output_dir, pdf_filename,
                    download_context, force_headed,
                    # Keyword, not positional: retry_download forwards
                    # *args straight through, so an extra positional here
                    # would land on the wrong parameter.
                    referer_url=referer_url,
                    max_retries=DP_MAX_RETRIES, retry_delay=DP_RETRY_DELAY,
                )
                downloads['pdf'] = pdf_result
            except Exception as e:
                print(f"⚠️  PDF下载失败: {e}")

        # pdf-only 到此为止：图片、key image、补充材料都是给 markdown 引用的，
        # 而这个模式不产出 markdown。下面的 finally 照样会关掉自建的无头浏览器。
        if pdf_only:
            return downloads

        # Download figures
        figure_urls = links.get('figure_urls', {})
        if figure_urls:
            print("\n🖼️  下载图片...")
            print(f"  📊 找到 {len(figure_urls)} 个图片")
            for fig_id, fig_info in figure_urls.items():
                try:
                    if isinstance(fig_info, dict):
                        fig_url = fig_info.get('url')
                        fallback_url = fig_info.get('original_url')
                    else:
                        fig_url = fig_info
                        fallback_url = None
                    fig_match = re.search(r'(\d+)$', str(fig_id))
                    fig_num = fig_match.group(1) if fig_match else str(fig_id)

                    img_filename = await retry_download(
                        download_figure,
                        download_page, fig_url, int(fig_num), output_dir, download_context, force_headed,
                        max_retries=DP_MAX_RETRIES, retry_delay=DP_IMG_RETRY_DELAY
                    )
                    # Fall back to the original (lower-res) URL if the high-res fetch failed
                    if not img_filename and fallback_url and fallback_url != fig_url:
                        print(f"  ↪️  Figure {fig_num}: 高清链接失败，回退到原始链接")
                        img_filename = await retry_download(
                            download_figure,
                            download_page, fallback_url, int(fig_num), output_dir, download_context, force_headed,
                            max_retries=DP_IMG_MAX_RETRIES, retry_delay=DP_IMG_RETRY_DELAY
                        )
                    if img_filename:
                        downloads['figures'][fig_num] = img_filename
                except Exception as e:
                    print(f"⚠️  Figure {fig_id} 下载失败: {e}")
        else:
            print("\n⚠️  未找到图片链接")

        # Download key image (Popular Summary cover image)
        key_image_url = metadata.get('key_image_url')
        if key_image_url:
            print("\n🔑 下载Key Image...")
            try:
                img_filename = await download_figure(
                    download_page,
                    key_image_url,
                    0,  # Use 0 as fig_num for key image
                    output_dir,
                    download_context,
                    force_headed,
                )
                if img_filename:
                    # Rename to key_image.png
                    key_image_path = output_dir / "key_image.png"
                    (output_dir / img_filename).rename(key_image_path)
                    # Record it so convert_to_markdown can link the local file
                    # instead of the (signed, expiring) CDN URL.
                    downloads['key_image'] = key_image_path.name
                    print(f"  ✓ Key image已保存: key_image.png")
            except Exception as e:
                print(f"  ⚠️  Key image下载失败: {e}")

        # Download supplemental materials
        supp_urls = links.get('supplemental_urls', [])
        if supp_urls:
            print("\nStep 5️⃣  下载补充材料...")
            print("=" * 80)
            supp_descriptions = links.get('supplemental_descriptions', {})

            # Use the current page URL (after DOI redirect) as Referer so
            # publishers like Science.org don't 403-reject direct asset requests.
            try:
                _article_url = page.url if page is not None else None
            except Exception:
                _article_url = None

            # Put supplemental files under <paper_dir>/supplemental/ so they
            # don't clutter the paper root alongside paper.md / figures.
            supp_output_dir = output_dir / "supplemental"
            count, descriptions = await download_supplemental_materials(
                supp_urls,
                supp_output_dir,
                download_context,
                supp_descriptions,
                download_page,
                force_headed,
                article_url=_article_url,
            )
            downloads['supplemental'] = list(descriptions.keys())

    finally:
        if download_page is not page:
            try:
                await download_page.close()
            except:
                pass
        if download_browser is not None:
            try:
                await download_browser.close()
            except:
                pass
        if download_playwright is not None:
            try:
                await download_playwright.stop()
            except:
                pass

    return downloads


# ============================================================================
# Phase 4-5: Simplified Workflow
# ============================================================================


async def _try_fresh_chrome_download(url: str, output_dir: Path,
                                     filename: str,
                                     headless: bool = False,
                                     referer_url: str = '') -> Optional[str]:
    """Download *url* with a throwaway Chrome seeded from the real profile.

    The bottom rung of the fetch ladder, for any kind of file: it watches a
    download directory, so the article PDF, a supplemental PDF or ZIP, and a
    figure all arrive the same way. Nothing here is PDF-specific, and the
    messages must not claim otherwise -- a supplement reported as "下载 PDF"
    sends you looking in the wrong place.

    Used when the ordinary browser cannot get the file: the shared instance
    has been driven by Playwright since the article loaded and carries that
    fingerprint, and a clearance won on the article host does not transfer to
    the separate host several publishers serve files from.

    Returns the saved filename, or None so the caller can fall back.
    """
    if not url or not _fresh_chrome_enabled():
        return None

    from chrome_session import open_url_in_fresh_chrome

    session = None
    # A dedicated download directory per attempt, so "which file appeared" is
    # unambiguous.
    download_dir = tempfile.mkdtemp(prefix='dp_dl_')
    try:
        if referer_url:
            print(f"  🛡️  用独立 Chrome 下载，先开 referer 页再点击跳转...")
        else:
            print("  🛡️  用独立 Chrome 下载（避开共享浏览器的自动化指纹）...")
        session = await open_url_in_fresh_chrome(
            url,
            pdf_mode=True,
            download_dir=download_dir,
            timeout_s=int(DP_CLOUDFLARE_TIMEOUT),
            headless=headless,
            referer_url=referer_url,
            fast_path_wait_s=DP_PDF_FASTPATH_WAIT,
        )
        result = session.result or {}
        landed = result.get('downloaded_file')
        if not (landed and os.path.isfile(landed)):
            if result.get('success'):
                print("  ✓ 挑战已通过，但未检测到下载文件")
            else:
                print("  ⚠️  独立 Chrome 未拿到文件")
            return None
        print(f"  ✓ 独立 Chrome 已触发下载: {landed}")
        return _finalize_downloaded_pdf(landed, output_dir, filename)
    except Exception as exc:
        print(f"  ⚠️  独立 Chrome 下载异常: {exc}")
        return None
    finally:
        await _close_fresh_pdf_session(session, download_dir)


def _finalize_downloaded_pdf(src: str, output_dir: Path,
                             filename: str) -> Optional[str]:
    """Wait for a .crdownload to settle, then copy the file into place."""
    final_pdf = Path(output_dir) / filename
    waited = 0
    base, _ = os.path.splitext(src)
    if src.endswith('.crdownload'):
        print("  ⏳ 等待下载完成（源文件仍为 .crdownload）...")
        while src.endswith('.crdownload') and waited < DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT:
            time.sleep(2)
            waited += 2
            if os.path.isfile(base):
                src = base
                break
            if not os.path.isfile(src):
                break
        if src.endswith('.crdownload'):
            print(f"    ⏰  下载在 {DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT}s 内未完成")
            return None
    if not (os.path.isfile(src) and not src.endswith('.crdownload')):
        print("    ⚠️  下载文件异常")
        return None
    shutil.copy(str(src), str(final_pdf))
    size_mb = final_pdf.stat().st_size / (1024 * 1024)
    print(f"    ✓ 保存: {filename} ({size_mb:.2f} MB) [独立 Chrome 下载]")
    return filename


async def _close_fresh_pdf_session(session, download_dir) -> None:
    """Shut down the throwaway PDF browser and remove its download directory."""
    if session is not None:
        try:
            await session.close()
        except Exception as exc:
            print(f"  ⚠️  关闭独立 Chrome 失败: {type(exc).__name__}: {exc}")
    if download_dir:
        try:
            shutil.rmtree(download_dir, ignore_errors=True)
        except OSError:
            pass


async def download_pdf(
    page,
    pdf_url: str,
    output_dir: Path,
    filename: str = "paper.pdf",
    context=None,
    force_headed: bool = False,
    referer_url: str = '',
) -> str:
    """下载论文PDF

    Args:
        page: Playwright page instance
        pdf_url: 完整的PDF URL
        output_dir: 输出目录
        filename: 保存的文件名 (默认: paper.pdf)

    Returns:
        保存的文件名，或None如果下载失败
    """
    if not pdf_url:
        print("❌ PDF URL为空")
        return None

    try:
        print(f"  📥 下载 PDF...")
        print(f"     链接: {pdf_url}")

        # ── 下载顺序取决于当前是有头还是无头 ──
        #
        # Headed runs try the throwaway Chrome first: the shared instance has
        # been driven by Playwright since the article page loaded, so it
        # carries an automation fingerprint, and a Cloudflare clearance won on
        # the article host does not transfer to the separate host publishers
        # like ScienceDirect serve PDFs from.
        #
        # Headless runs are the opposite. Reaching a publisher headless at all
        # means it is not challenging us, so the browser already in hand can
        # fetch the PDF -- and launching a windowed Chrome for every paper
        # would defeat the point of running headless. The throwaway browser
        # stays available as the fallback for when that fails.
        # ⚠️ 上面那两段是**改前**的理由，现已被实测推翻，保留是为了说明为什么
        # 不再那样排。`tab` 现在对有头/无头都排第一。
        #
        # The throwaway Chrome carries no automation fingerprint at all --
        # Chrome's own startup navigation fetches the page and _stealth_js is
        # only ever injected into the Playwright context -- and it is still the
        # rung that gets refused. Measured end to end on EPL
        # 10.1209/0295-5075/122/14004: every one of the four attempts saw the
        # fresh rung land on validate.perfdrive.com, while the *shared*
        # Playwright browser -- the most automated one in the run -- took the
        # PDF with no challenge at all, because it was the browser that had
        # just read the article. What the bot manager gates here is session
        # continuity, not fingerprint: a brand-new browser asking for
        # /article/{doi}/pdf with no history and no referer is the shape it
        # refuses. Putting `fresh` first cost three failed attempts and ~4.5
        # minutes of retry pacing before `tab` was even reached.
        #
        # 'referer' sits last: it costs an extra page load, so it is only
        # worth reaching for once the cheaper rungs have failed. It is also
        # the only rung that needs something the others do not -- a page to
        # click from -- so it stays inert when no referer is known.
        ladder = _fetch_ladder('pdf', default=('tab', 'fresh', 'referer'))

        try:
            pdf_referer = page.url if page is not None else None
        except Exception:
            pdf_referer = None

        if ladder and ladder[0] == 'request':
            cookies = await _cookies_for_requests(pdf_url, context=context,
                                                  page=page)
            if await asyncio.to_thread(
                    _http_download_to, pdf_url, output_dir / filename,
                    pdf_referer, DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT, False,
                    cookies):
                print(f"    ✓ 保存: {filename} [直接下载]")
                return filename

        if ladder and ladder[0] == 'fresh':
            saved = await _try_fresh_chrome_download(
                pdf_url, output_dir, filename, headless=not force_headed)
            if saved:
                return saved
            # 只有下面真的还有一层时才说「回退」。阶梯被截断成只剩 fresh 时
            # （DP_FETCH_PDF=fresh），先说要回退、下一行又说没有这一层，
            # 自相矛盾。
            if 'tab' in ladder:
                print("  ↪ 回退 Playwright 导航")

        if 'tab' not in ladder:
            # Still give 'referer' its turn: with the ladder truncated to
            # ('referer',) this is the only place it could ever run.
            if 'referer' in ladder and referer_url:
                saved = await _try_fresh_chrome_download(
                    pdf_url, output_dir, filename,
                    headless=not force_headed, referer_url=referer_url)
                if saved:
                    return saved
            print("    ⚠️  阶梯里没有浏览器标签页这一层，不再尝试")
            return None

        # pdf_link 直连模式下没有浏览器可以回退 —— 整条流程只起那一个一次性
        # Chrome。如实说明失败原因，而不是拿 None 去取 .context 崩一个
        # AttributeError。
        if page is None and context is None:
            print("    ⚠️  无可用浏览器上下文，无法回退导航下载")
            return None

        pdf_downloaded = False
        # ── 单次导航 + context级 download 事件 作为「真实拿到 PDF」的实体判据 ──
        # 说明：共享 Chrome 被 Playwright(accept_downloads=True) 接管后，无论哪个 tab 触发
        # 下载都会落入 playwright-artifacts 临时目录。因此：
        #   1) 不再 cd4 预热二次导航 PDF（正文阶段已拿齐 cookie；重复导航会新增 tab、
        #      可能再次触发 Radware 校验、并叠加一次多余下载）。
        #   2) download 事件挂在 context 层，捕获任意 tab 的下载回调，避免"事件派发到
        #      非监听 page"导致判空。
        #   3) 等待上限 DP_PDF_DOWNLOAD_TIMEOUT（默认30s），超时如实判定失败。
        # 判据一：「下载已开始」→ 用 DP_PDF_DOWNLOAD_TIMEOUT 判定（默认30s）
        # 判据二：「下载已完成」→ 用 DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT 判定（默认180s，慢网速专用）
        # 拆成两个信号：download 事件一旦触发（浏览器已开始接收响应）立即视为“已开始”；
        # 之后无论下载多慢，都只等它完成，绝不因为慢而重开页面 / 触发 retry 叠加。
        # 只有「根本没触发下载事件」（页面未落到下载）才算失败，交给 retry_download 重试。
        _dl_started = asyncio.Event()   # 下载事件已触发（真实开始）
        _dl_done = asyncio.Event()      # 文件已完整落盘到 output_dir
        _dl_failed = asyncio.Event()    # 下载中途失败（网络波动、cancel等）
        _ctx = context if context is not None else (page.context if page is not None else None)
        # 同一个 Download 会同时派发给下面注册的 page 级和 context 级监听 —— 实测
        # 两次回调拿到的是同一个对象，且都在任一方 await 完 path() 之前就进了函数。
        # 所以去重必须在第一个 await 之前同步完成，不能看 _dl_done（那个要等
        # path() 回来才置位，两次都会漏过去）。用 `is` 逐个比：既不假设 Download
        # 可哈希，也不怕 id() 在对象回收后被复用。
        _seen_downloads = []

        async def _ctx_handle_download(download):
            """context 层下载事件回调 —— 事件一触发即视为下载已开始"""
            if any(d is download for d in _seen_downloads):
                return  # 同一个下载的第二次派发，已经处理过了
            _seen_downloads.append(download)
            _dl_started.set()  # 第一时间标记已开始，不等 path()（path 可能因慢网速阻塞）
            try:
                # ⚠️ save_as, NOT path() + copy. path() returns a file inside
                # Playwright's artifacts directory, which is deleted when the
                # page closes -- and this handler is fire-and-forget, so the
                # outer flow can close download_page (a new tab under
                # force_headed) while we sit between the two calls. Measured on
                # IOP: the download had already succeeded and was lost to
                # "[Errno 2] ... /tmp/playwright-artifacts-.../...", then
                # reported as a network failure and retried. save_as copies
                # while the Download is still live, so there is no window.
                # The budget matches the outer _dl_done wait, so the handler
                # cannot still be running after the outer flow gave up.
                final_path = output_dir / filename
                if not await download_save_as_with_timeout(
                        download, final_path,
                        timeout_s=max(DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT, 120),
                        what='PDF'):
                    _dl_failed.set()  # 明确置失败，让 retry_download 重试
                    return
                pdf_size_mb = final_path.stat().st_size / (1024 * 1024)
                print(f"    ✓ 保存: {filename} ({pdf_size_mb:.2f} MB) [context下载事件]")
                _dl_done.set()
            except Exception as _e:
                print(f"    ⚠️  context下载事件处理异常: {_e}")
                _dl_failed.set()  # 标记失败，让 retry_download 重试

        # 注册 context 级监听（能捕获 context 下任意 tab 的下载，含 CDP 导航触发的）
        if _ctx is not None:
            _ctx.on("download", _ctx_handle_download)

        # 复用当前页或新建页，单次导航到 PDF
        download_page = await context.new_page() if force_headed and context is not None else page
        # 双保险：page 级和 context 级都注册。实测两者都会收到同一个 Download
        # （回调自己按对象去重，见上面的 _seen_downloads）；两个注册都留着，是因为
        # CDP 导航触发的下载不一定落在我们手里这个 page 上。
        try:
            download_page.on("download", _ctx_handle_download)
        except Exception as _e:
            print(f"    ⚠️  page 级下载监听注册失败: {_e}")
        try:
            await download_page.goto(pdf_url, timeout=int(DP_PAGE_LOAD_TIMEOUT * 1000), wait_until='commit')
        except:
            # 下载开始时页面加载会中断，这是正常的
            pass

        # 反爬挑战与「等待下载开始」并行：下载事件一旦触发（真实开始）就立即确认，
        # 不再被 auto_solve 空转阻塞；仅当下载未触发时，才用挑战处理兜底去解验证。
        solve_task = None
        try:
            solve_task = asyncio.ensure_future(
                auto_solve_bot_challenge(download_page, timeout_s=DP_CLOUDFLARE_TIMEOUT, initial_poll_s=DP_CLOUDFLARE_INITIAL_POLL)
            )
        except Exception as e:
            print(f"    ⚠️  auto_solve_bot_challenge (PDF): {e}")
            solve_task = None

        # 阶段A：与挑战处理并行，等待「下载已开始」。超时说明没触发下载 → 交给 retry。
        started = False
        try:
            await asyncio.wait_for(_dl_started.wait(), timeout=float(DP_PDF_DOWNLOAD_TIMEOUT))
            started = True
            # 下载已真实开始：取消仍在空转的挑战处理，避免拖住保存逻辑
            if solve_task is not None and not solve_task.done():
                solve_task.cancel()
        except asyncio.TimeoutError:
            print(f"    ⏰  未触发下载事件（>{DP_PDF_DOWNLOAD_TIMEOUT}s），判定未开始下载，交由挑战处理兜底")
            # 给 auto_solve 更充分时间去解挑战（它可能正是触发下载的关键一步）
            if solve_task is not None:
                try:
                    await asyncio.wait_for(solve_task, timeout=float(DP_CLOUDFLARE_TIMEOUT))
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

        # 阶段B：已开始下载 → 等完成或失败，慢网速只等不重开。
        done = False
        if started:
            _done_t = asyncio.ensure_future(_dl_done.wait())
            _fail_t = asyncio.ensure_future(_dl_failed.wait())
            try:
                await asyncio.wait_for(
                    asyncio.wait([_done_t, _fail_t], return_when=asyncio.FIRST_COMPLETED),
                    timeout=float(DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT)
                )
                if _dl_done.is_set():
                    # 验证文件真实存在且非空
                    _fp = output_dir / filename
                    if _fp.exists() and _fp.stat().st_size > 0:
                        done = True
                    else:
                        print(f"    ⚠️  下载回调标记完成但文件不存在或为空，判定失败")
                elif _dl_failed.is_set():
                    print(f"    ⚠️  下载中途失败（网络波动），交由 retry 重试")
                else:
                    print(f"    ⏰  下载已开始但 {DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT}s 内未完成")
            except asyncio.TimeoutError:
                print(f"    ⏰  下载已开始但 {DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT}s 内未完成（继续等待完成）")
                # 下载已真实开始，慢就多给缓冲，不重开页面
                try:
                    await asyncio.wait_for(_dl_done.wait(), timeout=float(max(DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT, 120)))
                    _fp = output_dir / filename
                    if _fp.exists() and _fp.stat().st_size > 0:
                        done = True
                    else:
                        print(f"    ⚠️  二次等待后文件仍不存在或为空")
                except asyncio.TimeoutError:
                    print(f"    ⏰  二次等待仍超时，放弃本次（不重开，避免叠加）")
            finally:
                if not _done_t.done():
                    _done_t.cancel()
                if not _fail_t.done():
                    _fail_t.cancel()

        # 移除监听并关闭下载页（page 级 + context 级都移除）
        try:
            download_page.remove_listener("download", _ctx_handle_download)
        except Exception:
            pass
        if _ctx is not None:
            _ctx.remove_listener("download", _ctx_handle_download)
        if download_page is not page:
            try:
                await download_page.close()
            except Exception:
                pass

        if done:
            return filename

        print(f"    ⚠️  未成功下载PDF")
        # The tab could not get it (a challenge, or a viewer that never fires a
        # download event). When 'fresh' sits below 'tab' in the ladder, the
        # throwaway Chrome is now worth the launch.
        if 'fresh' in ladder and ladder.index('fresh') > ladder.index('tab'):
            saved = await _try_fresh_chrome_download(
                pdf_url, output_dir, filename, headless=not force_headed)
            if saved:
                return saved

        # Last rung: open the referring page in the throwaway Chrome and click
        # through to the file, so the request carries a Referer and still
        # looks user-initiated. Needs a referer to click from, so a run that
        # has none simply stops here.
        if 'referer' in ladder and referer_url:
            saved = await _try_fresh_chrome_download(
                pdf_url, output_dir, filename,
                headless=not force_headed, referer_url=referer_url)
            if saved:
                return saved
        return None

    except Exception as e:
        print(f"    ❌ 下载失败: {e}")
        return None


async def download_supplemental_materials(
    supplemental_links: list,
    output_dir: Path,
    context,
    descriptions: dict = None,
    page=None,
    force_headed: bool = False,
    article_url: str = None,
) -> tuple:
    """在浏览器中打开新标签页下载补充材料文件（保持登录态）

    Args:
        supplemental_links: 补充材料链接列表
        output_dir: 输出目录
        context: Playwright browser context（已enable downloads）
        descriptions: 补充材料的描述字典 {chapter_title: description}

    Returns:
        tuple: (成功下载的文件数量, 下载文件的描述字典 {filename: description})
    """
    if not supplemental_links:
        return 0, {}

    import urllib.parse
    import shutil

    # Emit saved filenames RELATIVE to the paper output directory so markdown
    # links resolve correctly whether files live at the paper root (legacy
    # layout) or inside a supplemental/ subdirectory (current layout). For a
    # nested subdir like <paper_dir>/supplemental/, this yields
    # "supplemental/foo.pdf" — for the legacy flat layout it yields "foo.pdf".
    _rel_base = output_dir.parent if output_dir.name == 'supplemental' else output_dir

    def _rel_saved_name(p: Path) -> str:
        try:
            return p.relative_to(_rel_base).as_posix()
        except ValueError:
            return p.name

    _MEDIA_EXTENSIONS = {
        '.mp4', '.avi', '.mov', '.wmv', '.mkv', '.webm',
        '.mp3', '.wav', '.ogg', '.flac',
        '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff', '.tif', '.webp',
        '.zip', '.gz', '.tar', '.7z', '.rar', '.pdf',
    }

    def _is_direct_download_url(u: str) -> bool:
        ext = Path(urllib.parse.urlparse(u).path).suffix.lower()
        return ext in _MEDIA_EXTENSIONS

    if descriptions is None:
        descriptions = {}

    downloaded_count = 0
    downloaded_descriptions = {}

    for i, link in enumerate(supplemental_links, 1):
        # 为每个补充材料添加重试逻辑
        max_retries_supp = DP_SUPP_MAX_RETRIES
        retry_delay_supp = DP_RETRY_DELAY

        for retry_attempt in range(max_retries_supp):
            success = False
            # The tab rung below marks this attempt successful even when it
            # captured nothing, so "did this link actually produce a file?"
            # has to be answered by the counter, not by a failure branch.
            count_before = downloaded_count
            try:
                url = link if isinstance(link, str) else link.get('url', link.get('href', ''))
                if not url:
                    continue

                # 优先使用descriptions中的标题作为文件名
                chapter_title = None
                desc_value = None

                if descriptions:
                    # 1) URL-keyed lookup (IOP, Optica, …).
                    chapter_title = descriptions.get(url) or descriptions.get(url.split('?')[0])
                    if chapter_title:
                        desc_value = chapter_title
                        chapter_title = None  # set from URL basename below; desc_value carries text
                    else:
                        # 2) Filename-keyed lookup. Some publishers (APS) key
                        #    descriptions by the file's display name and expose
                        #    that name on the link object as ``text``.  Use it
                        #    before falling back to a fragile positional match.
                        link_text = (link.get('text', '')
                                     if isinstance(link, dict) else '').strip()
                        if link_text and link_text in descriptions:
                            desc_value = descriptions[link_text]
                            # Don't set chapter_title here — let the URL basename
                            # supply the filename so we save with the publisher's
                            # actual filename (e.g. input1D.deck) rather than
                            # the long description text.
                        else:
                            # 3) Positional fallback (Springer books where the
                            #    key itself IS the chapter title).
                            desc_items = list(descriptions.items())
                            if i - 1 < len(desc_items):
                                key, val = desc_items[i - 1]
                                if key and not key.startswith('http'):
                                    chapter_title = key
                                    desc_value = val

                # 如果没有找到chapter标题，从URL中提取文件名
                if not chapter_title:
                    parsed_url = urllib.parse.urlparse(url)
                    # 处理URL以斜杠结尾的情况
                    path_parts = [p for p in parsed_url.path.split('/') if p]
                    filename = urllib.parse.unquote(path_parts[-1]) if path_parts else ''
                    if not filename:
                        filename = f"supplemental_{i}"
                    chapter_title = filename

                # 清理文件名中的非法字符（保留基本的文件名安全字符）
                safe_title = re.sub(r'[<>:"/\\|?*]', '_', chapter_title)
                safe_title = re.sub(r'_+', '_', safe_title).strip('_')  # 去重下划线并去除边界

                # Always use safe_title (URL basename or short chapter title) for
                # the filename. Long prose descriptions live in metadata, never
                # in filenames — APS descriptions can run to 300+ bytes and
                # commonly contain '--' or '/' (e.g. "reflection/transmission"),
                # which used to blow past the 255-byte filename limit and break
                # Path.stem-based truncation.
                output_filename = f"supplemental--{safe_title}"

                # Truncate to fit ext4's 255-byte per-component limit. Use UTF-8
                # byte length (not char count) since non-ASCII titles take 2–4
                # bytes per char. Preserve the extension so the file type stays
                # recognisable.
                suffix = Path(output_filename).suffix
                stem = output_filename[:-len(suffix)] if suffix else output_filename
                # ⚠️ Two different limits. 200 bytes is ext4's per-component
                # headroom; Windows instead bounds the WHOLE path at MAX_PATH,
                # and this file sits one directory deeper than everything else
                # (<paper>\supplemental\supplemental--…), so it is the first to
                # blow it. organize_paper_output reserves
                # WINDOWS_CHILD_RESERVE for this tail -- keep the two in step.
                MAX_STEM_BYTES = 80 if IS_WINDOWS else 200
                stem_bytes = stem.encode('utf-8')
                if len(stem_bytes) > MAX_STEM_BYTES:
                    truncated = stem_bytes[:MAX_STEM_BYTES]
                    # Drop trailing bytes that would split a multi-byte UTF-8 char
                    while truncated and (truncated[-1] & 0xC0) == 0x80:
                        truncated = truncated[:-1]
                    stem = truncated.decode('utf-8', errors='ignore')
                output_filename = stem + suffix
                output_path = output_dir / output_filename

                # 确保输出目录存在
                output_dir.mkdir(parents=True, exist_ok=True)

                print(f"  📥 下载补充材料 ({i}/{len(supplemental_links)}): {chapter_title}")
                print(f"     URL: {url}")

                # For media/binary files, first try APIRequestContext to fetch bytes directly.
                # This shares cookies with the browser context but skips the renderer,
                # so the browser won't open a video player or image viewer.
                # If the response is non-OK or HTML (e.g. a Cloudflare interstitial),
                # OR the body is too large for the CDP transport, fall through to
                # the browser-tab download path which streams to disk natively.
                #
                # Why the size cap? APIRequestContext buffers the entire response
                # body and ships it over the CDP WebSocket base64-encoded. For a
                # few-MB asset that's fast; for a 200+ MB video (seen on
                # 10.1103/PhysRevLett.127.114801) the encode+round-trip stalls
                # the websocket so badly that ``await api_response.body()``
                # appears to hang for far longer than the actual download would
                # take.  The browser's native download manager streams straight
                # to disk and has no such limit.
                DIRECT_FETCH_MAX_BYTES = 80 * 1024 * 1024  # 80 MB

                # First rung: a plain request, no browser at all, carrying the
                # article session's cookies. Most publishers serve supplements
                # from a CDN that needs nothing more than a browser-like
                # User-Agent and the article as Referer.
                supp_ladder = _fetch_ladder('supplement')
                if 'request' in supp_ladder:
                    supp_cookies = await _cookies_for_requests(
                        url, context=context, page=page)
                    if await asyncio.to_thread(
                            _http_download_to, url, output_path, article_url,
                            DP_SUPPLEMENTAL_TIMEOUT, False, supp_cookies):
                        output_path = _detect_and_rename(output_path)
                        file_size_mb = output_path.stat().st_size / (1024 * 1024)
                        print(f"    ✓ 已保存: {output_path.name} ({file_size_mb:.2f} MB) [直接下载]")
                        downloaded_count += 1
                        saved_name = _rel_saved_name(output_path)
                        downloaded_descriptions[saved_name] = desc_value if desc_value else chapter_title
                        success = True
                        break

                if _is_direct_download_url(url):
                    direct_ok = False
                    try:
                        extra_headers = {}
                        if article_url:
                            extra_headers['Referer'] = article_url
                        api_response = await context.request.get(url, timeout=int(DP_PAGE_LOAD_TIMEOUT * 1000), headers=extra_headers)
                        content_type = (api_response.headers.get('content-type') or '').lower()
                        is_html_challenge = 'text/html' in content_type

                        # Inspect Content-Length BEFORE reading the body.  If the
                        # asset is large (or the server didn't report a size at
                        # all on a media URL), prefer the browser-tab path.
                        cl_raw = api_response.headers.get('content-length') or ''
                        try:
                            content_length = int(cl_raw) if cl_raw else -1
                        except ValueError:
                            content_length = -1
                        is_too_large = content_length > DIRECT_FETCH_MAX_BYTES

                        if api_response.ok and not is_html_challenge and not is_too_large:
                            # The size cap above keeps this from being the
                            # 200 MB case; the timeout covers the transfer
                            # simply stopping partway.
                            body = await read_body_with_timeout(
                                api_response,
                                timeout_s=DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT,
                                what='补充材料')
                            if body:
                                output_path.write_bytes(body)
                                output_path = _detect_and_rename(output_path)
                                file_size_mb = output_path.stat().st_size / (1024 * 1024)
                                print(f"    ✓ 已保存: {output_path.name} ({file_size_mb:.2f} MB)")
                                downloaded_count += 1
                                saved_name = _rel_saved_name(output_path)
                                downloaded_descriptions[saved_name] = desc_value if desc_value else chapter_title
                                direct_ok = True
                            else:
                                print(f"    ⚠️  响应体为空: {chapter_title}")
                        elif is_html_challenge:
                            print(f"    ↪ 直接请求被反爬虫拦截 (Cloudflare等)，回退到浏览器标签页下载")
                        elif is_too_large:
                            size_mb = content_length / (1024 * 1024) if content_length > 0 else 0
                            print(
                                f"    ↪ 响应体过大 ({size_mb:.1f} MB > "
                                f"{DIRECT_FETCH_MAX_BYTES // (1024 * 1024)} MB 直接下载上限)，回退到浏览器标签页下载"
                            )
                        else:
                            print(f"    ↪ 请求失败 (status={api_response.status})，回退到浏览器标签页下载")
                    except Exception as e:
                        print(f"    ↪ 直接下载失败: {str(e)[:100]}，回退到浏览器标签页下载")
                    if direct_ok:
                        success = True
                        break
                    # else: fall through to the browser-tab path below.

                # force-headed mode avoids navigating the article tab.
                download_page = await context.new_page() if force_headed or page is None else page

                if article_url:
                    await download_page.set_extra_http_headers({"Referer": article_url})

                # For audio types that Chrome plays inline (no download event),
                # register a response listener BEFORE goto() so we capture the
                # bytes from Playwright's network layer regardless of whether
                # goto() returns None (which it does when Chrome intercepts the
                # navigation to render an inline audio player).
                _INLINE_AUDIO_EXTS = {'.mp3', '.wav', '.ogg', '.flac', '.m4a', '.aac', '.opus'}
                _url_ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
                _is_inline_audio = _url_ext in _INLINE_AUDIO_EXTS
                _audio_body: bytes | None = None
                _audio_done = asyncio.Event()

                if _is_inline_audio:
                    _capture_url = url

                    async def _on_audio_response(resp):
                        nonlocal _audio_body
                        if _audio_done.is_set():
                            return
                        ct = (resp.headers.get('content-type') or '').lower()
                        is_ours = (
                            resp.url == _capture_url
                            or resp.url.split('?')[0] == _capture_url.split('?')[0]
                            or ('audio/' in ct and resp.ok)
                        )
                        if is_ours:
                            if resp.ok and 'audio/' in ct:
                                body = await read_body_with_timeout(
                                    resp, timeout_s=DP_SUPPLEMENTAL_TIMEOUT,
                                    what='内嵌音频')
                                if body:
                                    _audio_body = body
                            _audio_done.set()

                    download_page.on('response', _on_audio_response)

                # 设置下载事件处理
                downloaded_file = None

                async def on_download(download):
                    nonlocal downloaded_file
                    # ⚠️ 直接 save_as 到最终位置，不用 path() 再复制。
                    # path() 给的是 Playwright 自己 artifacts 目录里的文件，而那个
                    # 文件在页面/上下文关闭时就被删掉 —— 从取到路径到复制之间的每一行，
                    # 都是一个「已经下载成功的文件可能凭空消失」的窗口。这条路的窗口
                    # 尤其宽：取路径在此处，真正复制在一百行之后，中间还夹着一次
                    # download_page.close()。save_as 在 Download 仍存活时落盘，窗口不存在。
                    # save_as 同样没有自己的超时，所以照样要包一层。
                    if await download_save_as_with_timeout(
                            download, output_path,
                            timeout_s=DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT,
                            what='补充材料'):
                        downloaded_file = str(output_path)

                download_page.on("download", on_download)

                # 导航到链接（会自动触发下载）
                response = None
                try:
                    response = await download_page.goto(url, timeout=int(DP_SUPPLEMENTAL_TIMEOUT * 1000), wait_until='commit')
                except:
                    # 下载开始时页面加载会中断，这是正常的
                    pass

                # Same Cloudflare-Turnstile guard as for the main article
                # and PDF paths — some publishers wall supplemental
                # downloads behind the same "verify you are human" checkbox.
                try:
                    await auto_solve_bot_challenge(download_page, timeout_s=DP_CLOUDFLARE_TIMEOUT, initial_poll_s=DP_CLOUDFLARE_INITIAL_POLL)
                except Exception as e:
                    print(f"    ⚠️  auto_solve_bot_challenge (supp): {e}")

                # For inline audio: wait for the response listener to finish
                # reading the body (up to 60 s for large files).  Then save
                # directly and skip the download-event path entirely.
                if _is_inline_audio and not downloaded_file:
                    try:
                        await asyncio.wait_for(_audio_done.wait(), timeout=DP_SUPPLEMENTAL_TIMEOUT)
                    except asyncio.TimeoutError:
                        pass
                    try:
                        download_page.remove_listener('response', _on_audio_response)
                    except Exception:
                        pass
                    if _audio_body:
                        try:
                            output_path.write_bytes(_audio_body)
                            output_path = _detect_and_rename(output_path)
                            file_size_mb = output_path.stat().st_size / (1024 * 1024)
                            print(f"    ✓ 已保存: {output_path.name} ({file_size_mb:.2f} MB)")
                            downloaded_count += 1
                            saved_name = _rel_saved_name(output_path)
                            downloaded_descriptions[saved_name] = desc_value if desc_value else chapter_title
                        except Exception as e:
                            print(f"    ⚠️  音频保存失败: {str(e)[:100]}")
                        if download_page is not page:
                            await download_page.close()
                        success = True
                        break

                # 等待下载事件或超时。
                # 对于 Cloudflare 等反爬挑战页面，需要给 JS 几秒钟时间通过 challenge
                # 后才会触发实际的下载，所以等待时间放宽到 ~20 秒。
                try:
                    if not downloaded_file:
                        # 期望下载事件在 DP_SUPPLEMENTAL_TIMEOUT 内触发；
                        # 外层 asyncio.wait_for 额外多 2 s 让 Playwright 有余量正常抛超时。
                        _dl_ms = int(DP_SUPPLEMENTAL_TIMEOUT * 1000)
                        download_event = await asyncio.wait_for(
                            asyncio.create_task(download_page.wait_for_event("download", timeout=_dl_ms)),
                            timeout=DP_SUPPLEMENTAL_TIMEOUT + 2
                        )
                        if download_event:
                            # 同上：save_as 直接落到最终位置，不经 Playwright 的
                            # artifacts 目录，免得文件在复制前随页面关闭而消失。
                            if await download_save_as_with_timeout(
                                    download_event, output_path,
                                    timeout_s=DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT,
                                    what='补充材料'):
                                downloaded_file = str(output_path)
                except asyncio.TimeoutError:
                    # 如果等待超时，继续使用response方法
                    pass
                except Exception:
                    pass

                await asyncio.sleep(1)

                # 如果捕获到下载，文件此时已由 save_as 落在 output_path 上。
                # ⚠️ 不能再 shutil.copy 一次 —— 源和目标是同一个文件，会抛
                # SameFileError。保留 copy 分支只为兼容「downloaded_file 来自别处」
                # 的情况（目前没有，但这段历史上换过几次来源）。
                if downloaded_file and Path(downloaded_file).exists():
                    try:
                        file_size = Path(downloaded_file).stat().st_size
                        if file_size > 0:
                            if Path(downloaded_file) != Path(output_path):
                                shutil.copy(str(downloaded_file), str(output_path))
                            output_path = _detect_and_rename(output_path)
                            file_size_mb = output_path.stat().st_size / (1024 * 1024)
                            print(f"    ✓ 已保存: {output_path.name} ({file_size_mb:.2f} MB)")
                            downloaded_count += 1

                            # 记录该文件的描述
                            saved_name = _rel_saved_name(output_path)
                            if desc_value:
                                downloaded_descriptions[saved_name] = desc_value
                            else:
                                downloaded_descriptions[saved_name] = chapter_title
                        else:
                            print(f"    ⚠️  下载文件为空: {chapter_title}")
                    except Exception as e:
                        print(f"    ⚠️  复制文件失败: {str(e)[:100]}")
                elif response:
                    try:
                        status = response.status if response else 'unknown'
                        content_type = response.headers.get('content-type', '').lower() if response else ''

                        if response.ok and 'text/html' not in content_type and content_type:
                            body = await read_body_with_timeout(
                                response,
                                timeout_s=DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT,
                                what='补充材料')
                            if len(body) > 0:
                                output_path.write_bytes(body)
                                output_path = _detect_and_rename(output_path)
                                file_size_mb = output_path.stat().st_size / (1024 * 1024)
                                print(f"    ✓ 已保存: {output_path.name} ({file_size_mb:.2f} MB)")
                                downloaded_count += 1

                                saved_name = _rel_saved_name(output_path)
                                if desc_value:
                                    downloaded_descriptions[saved_name] = desc_value
                                else:
                                    downloaded_descriptions[saved_name] = chapter_title
                            else:
                                print(f"    ⚠️  响应体为空 (status={status}, type={content_type}): {chapter_title}")
                        else:
                            print(f"    ⚠️  响应不是文件内容 (status={status}, type={content_type}): {chapter_title}")
                    except Exception as e:
                        print(f"    ⚠️  直接保存响应失败: {str(e)[:100]}")
                else:
                    print(f"    ⚠️  未捕获到下载事件: {chapter_title}")

                if download_page is not page:
                    await download_page.close()

                # Last rung: a Chrome that has never been automated. Science
                # gates its supplemental host behind the same Cloudflare check
                # as the article, and by the time we get here the shared
                # browser has been driven by Playwright long enough to be
                # refused. Keyed off the counter because the tab path above
                # reports success either way.
                if downloaded_count == count_before and 'fresh' in supp_ladder:
                    fresh_saved = await _try_fresh_chrome_download(
                        url, output_path.parent, output_path.name,
                        headless=not force_headed)
                    if fresh_saved:
                        output_path = _detect_and_rename(
                            output_path.parent / fresh_saved)
                        file_size_mb = output_path.stat().st_size / (1024 * 1024)
                        print(f"    ✓ 已保存: {output_path.name} "
                              f"({file_size_mb:.2f} MB) [一次性 Chrome]")
                        downloaded_count += 1
                        saved_name = _rel_saved_name(output_path)
                        downloaded_descriptions[saved_name] = (
                            desc_value if desc_value else chapter_title)

                success = True  # 标记成功
                break  # 跳出重试循环

            except Exception as e:
                if retry_attempt < max_retries_supp - 1:
                    print(f"    ⚠️  下载失败，{retry_delay_supp}秒后重试... (尝试 {retry_attempt + 1}/{max_retries_supp})")
                    if download_page is not page:
                        try:
                            await download_page.close()
                        except:
                            pass
                    await asyncio.sleep(retry_delay_supp)
                else:
                    print(f"    ❌ 已达最大重试次数 ({max_retries_supp}): {str(e)[:100]}")
                    if download_page is not page:
                        try:
                            await download_page.close()
                        except:
                            pass

    if downloaded_count > 0:
        print(f"\n  ✓ 成功下载 {downloaded_count} 个补充材料")

    return downloaded_count, downloaded_descriptions



async def _fetch_image_as_bytes(page, url: str) -> bytes:
    """Fetch a URL as raw bytes using browser-side fetch + base64.

    Avoids the CDP binary-as-string corruption bug where 0xFF bytes
    get replaced with U+FFFD (efbfbd) when Playwright returns non-base64
    encoded binary responses.
    """
    import base64
    # Swallows the timeout rather than propagating it: the caller's very next
    # move is ``response.body()``, a genuinely different transport (Playwright's
    # own, not an in-page fetch) that may well work when this one stalled.
    # Raising here would skip it.
    try:
        b64 = await evaluate_with_timeout(
            page,
            ("""async (url) => {""" + INPAGE_ABORT_JS + """
                const resp = await fetch(url, {
                    credentials: 'include',
                    signal: __dpAbort(__MS__),
                });
                if (!resp.ok) return null;
                const buf = await resp.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let binary = '';
                for (let i = 0; i < bytes.byteLength; i++) {
                    binary += String.fromCharCode(bytes[i]);
                }
                return btoa(binary);
            }""").replace('__MS__', inpage_abort_ms()),
            url,
            what='图片 in-page fetch',
        )
    except Exception:
        return None
    if b64 is None:
        return None
    return base64.b64decode(b64)


# ----------------------------------------------------------------------------
# Plain-HTTP first attempt for figures and supplementary files
# ----------------------------------------------------------------------------
# Unlike the PDF, figures and supplementary files are almost always served
# from a CDN with no bot check and no entitlement gate -- even ScienceDirect's
# ars.els-cdn.com hands them to a bare GET. Going through the browser for each
# one costs a tab, a navigation and often a Cloudflare round-trip, so a plain
# request with a browser-like User-Agent and the article as Referer is tried
# first; only when that yields no usable file does the browser path run.
#
# "Usable" is checked on the bytes, not just the status: a 200 carrying an
# HTML challenge or login page is the common way this fails, and saving it
# would leave a .jpg that is really a Cloudflare interstitial.
#
# The rungs themselves (and the cookie jar they carry) live in core.utilities;
# see the aliases near the top of this module.


def _http_download_to(url: str, dest: Path, referer: str = None,
                      timeout: float = 60, want_image: bool = False,
                      cookies: dict = None, total_timeout: float = None) -> bool:
    """GET *url* straight to *dest*. True only when a real file landed there.

    Streams to a ``.part`` file and renames on success, so a large video
    never sits in memory and an aborted transfer never looks finished.
    Rejects: non-2xx, an HTML/text response (challenge or login page), an
    empty body, and -- with *want_image* -- anything whose bytes are not an
    image.

    Two different clocks, and they catch different failures:

    *timeout* is requests' read timeout, which fires only when the socket goes
    **idle**. *total_timeout* caps the whole transfer, which is the only thing
    that can end a server dribbling bytes slowly enough to never look idle --
    the failure that leaves a video "downloading" until someone kills the run.
    """
    part = dest.with_name(dest.name + '.part')
    budget = float(total_timeout if total_timeout else DP_HTTP_TOTAL_TIMEOUT)
    # Written by the watchdog thread, read after the transfer unwinds -- the
    # stall surfaces as a socket error, so the report has to survive the except.
    state = {'stalled': False, 'got': 0}

    def _report_stall() -> bool:
        if not state['stalled']:
            return False
        print(f"    ↪ 直接请求超过 {budget:g}s 仍未传完"
              f"（已收 {state['got'] / (1024 * 1024):.2f} MB），回退到浏览器")
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        return True

    try:
        with requests.get(url, headers=_http_asset_headers(referer),
                          cookies=cookies or None,
                          timeout=(15, timeout), stream=True,
                          allow_redirects=True) as r:
            if r.status_code >= 400:
                print(f"    ↪ 直接请求 HTTP {r.status_code}，回退到浏览器")
                return False
            ctype = (r.headers.get('content-type') or '').lower()
            if ctype.startswith('text/html') or ctype.startswith('text/xml'):
                print(f"    ↪ 直接请求拿到的是网页（{ctype.split(';')[0]}），回退到浏览器")
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)

            # ⚠️ Checking the clock inside the loop does NOT work here, and the
            # obvious version of this fix is dead code. iter_content blocks
            # *inside* urllib3 until it has a full chunk_size buffer, so a
            # server trickling bytes never lets the loop body run at all --
            # measured: 60 s of one-byte-per-0.3 s writes produced zero
            # iterations, and the only thing that ever ended it was the read
            # timeout firing 15 s after the server finally stopped.
            # Closing the socket is the one thing that can interrupt that read,
            # so a watchdog thread does it.
            def _watchdog():
                state['stalled'] = True
                # ⚠️ Closing is not cancelling. Only shutting the socket down
                # wakes a thread already blocked in recv. Measured against a
                # trickling server: r.raw.close(), r.close() and
                # r.raw._fp.close() each left the read blocked until the server
                # itself stopped (8.81 s -- i.e. they did nothing), while
                # sock.shutdown(SHUT_RDWR) returned in 1.50 s.
                try:
                    sock = r.raw._connection.sock
                except Exception:
                    sock = None
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                # Still close afterwards, to release the pooled connection.
                for closer in (getattr(r, 'raw', None), r):
                    try:
                        if closer is not None:
                            closer.close()
                    except Exception:
                        pass

            timer = threading.Timer(budget, _watchdog)
            timer.daemon = True
            timer.start()
            deadline = time.monotonic() + budget
            try:
                with open(part, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)
                            state['got'] += len(chunk)
                        # Belt and braces for the other shape of the same
                        # problem: chunks arriving steadily but endlessly. This
                        # one exits cleanly instead of via a socket error.
                        if time.monotonic() > deadline:
                            state['stalled'] = True
                            break
            finally:
                timer.cancel()
        if _report_stall():
            return False

        # requests does NOT verify Content-Length: a server that dies mid-body
        # ends the iteration without raising, so a truncated transfer arrives
        # here looking exactly like a complete one. For a video that means a
        # corrupt file reported as a success -- and the browser rungs, which
        # would have got it right, never run. Only comparable when the body
        # wasn't decoded on the way in (gzip changes the length).
        declared = (r.headers.get('content-length') or '').strip()
        encoded = (r.headers.get('content-encoding') or '').strip().lower()
        if declared.isdigit() and encoded in ('', 'identity'):
            if state['got'] < int(declared):
                print(f"    ↪ 直接请求被截断（只收到 {state['got']}/{declared} 字节），"
                      f"回退到浏览器")
                part.unlink(missing_ok=True)
                return False

        if not part.exists() or part.stat().st_size == 0:
            print("    ↪ 直接请求响应为空，回退到浏览器")
            part.unlink(missing_ok=True)
            return False

        # Servers mislabel constantly; trust the bytes.
        sniffed = magic.from_file(str(part), mime=True) or ''
        if sniffed in ('text/html', 'application/xhtml+xml') or \
                (want_image and not sniffed.startswith('image/')
                 and sniffed not in ('application/postscript', 'application/pdf')):
            print(f"    ↪ 直接请求内容不对（{sniffed}），回退到浏览器")
            part.unlink(missing_ok=True)
            return False
        part.replace(dest)
        return True
    except Exception as exc:
        # A watchdog close surfaces here as a connection error. Report it as the
        # stall it actually was, not as a mysterious network failure.
        if _report_stall():
            return False
        print(f"    ↪ 直接请求失败（{type(exc).__name__}: {str(exc)[:80]}），回退到浏览器")
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        return False


async def download_figure(page, fig_url: str, fig_num: int, output_dir: Path, context=None, force_headed: bool = False) -> str:
    """下载高分辨率图片：按取数阶梯 request → tab → fresh 依次尝试。"""
    # Resolved before the try: the last rung runs after the except/finally, so
    # an exception raised before this point must not leave it undefined.
    fig_ladder = _fetch_ladder('figure')
    try:
        if not fig_url:
            return None

        fig_url = normalize_image_url(fig_url)

        print(f"  📥 下载 Figure {fig_num}: {fig_url}")

        try:
            referer = page.url if page is not None else None
        except Exception:
            referer = None

        if 'request' in fig_ladder:
            img_filename = original_image_filename(fig_url, fig_num)
            cookies = await _cookies_for_requests(fig_url, context=context,
                                                  page=page)
            # Tighter overall cap than a supplement gets: an image still
            # trickling in after this long is not going to arrive, and there
            # are dozens of them per paper, so the ceiling is paid repeatedly.
            ok = await asyncio.to_thread(
                _http_download_to, fig_url, output_dir / img_filename,
                referer, DP_FIGURE_TIMEOUT, True, cookies,
                DP_FIGURE_TIMEOUT * 3)
            if ok:
                print(f"    ✓ 保存: {img_filename} [直接下载]")
                return img_filename

        download_page = await context.new_page() if force_headed and context is not None else page

        # Navigate to the figure URL so auth cookies are active on this origin
        response = await download_page.goto(fig_url, wait_until='networkidle', timeout=int(DP_FIGURE_TIMEOUT * 1000))
        content_type = response.headers.get('content-type', '') if response else ''

        # If the CDN routes the image through a Cloudflare-protected host
        # (some ScienceDirect / Wiley figures do), the page shows a
        # "verify you are human" checkbox before the binary is served.
        # We only need to trigger the check when the response is HTML
        # (i.e. NOT an image) — content-type "image/*" means we're
        # already looking at the file itself.
        if not content_type.startswith('image/'):
            try:
                solved = await auto_solve_bot_challenge(
                    download_page, timeout_s=DP_CLOUDFLARE_TIMEOUT, initial_poll_s=DP_CLOUDFLARE_INITIAL_POLL
                )
                if solved:
                    # Re-fetch the same URL: the Cloudflare cookie set by
                    # the solved challenge lets this second goto through
                    # and returns the actual image bytes.
                    try:
                        response = await download_page.goto(
                            fig_url, wait_until='networkidle', timeout=int(DP_FIGURE_TIMEOUT * 1000)
                        )
                        content_type = (response.headers.get('content-type', '')
                                        if response else '')
                    except Exception:
                        pass
            except Exception as e:
                print(f"    ⚠️  auto_solve_bot_challenge (figure): {e}")

        if response and content_type.startswith('image/'):
            # Use browser-side fetch to avoid CDP binary corruption
            image_data = await _fetch_image_as_bytes(download_page, fig_url)
            if not image_data:
                image_data = await read_body_with_timeout(response, what='图片')
            # An empty body must NOT be reported as a save. Returning a filename
            # tells retry_download this succeeded, so the retries, the
            # fallback (lower-res) URL and the `fresh` rung would all be skipped
            # -- leaving a 0-byte .jpg and no way to tell it apart from a real
            # one later.
            if image_data:
                img_filename = original_image_filename(fig_url, fig_num)
                img_path = output_dir / img_filename
                img_path.write_bytes(image_data)
                print(f"    ✓ 保存: {img_filename}")
                return img_filename

        img_elements = await download_page.query_selector_all('img')

        if img_elements:
            img_src = await img_elements[0].get_attribute('src')
            if img_src:
                img_src = normalize_image_url(img_src, download_page.url)
                image_data = await _fetch_image_as_bytes(download_page, img_src)
                if not image_data:
                    response = await download_page.goto(img_src, wait_until='networkidle', timeout=int(DP_FIGURE_TIMEOUT * 1000))
                    # Guard the fallback goto too.
                    try:
                        await auto_solve_bot_challenge(download_page, timeout_s=DP_CLOUDFLARE_TIMEOUT, initial_poll_s=DP_CLOUDFLARE_INITIAL_POLL)
                    except Exception:
                        pass
                    image_data = await read_body_with_timeout(response, what='图片')
                if image_data:
                    img_filename = original_image_filename(img_src, fig_num)
                    img_path = output_dir / img_filename
                    img_path.write_bytes(image_data)
                    print(f"    ✓ 保存: {img_filename}")
                    if download_page is not page:
                        await download_page.close()
                    return img_filename

    except Exception as e:
        print(f"    ❌ 下载失败: {e}")
    finally:
        try:
            if 'download_page' in locals() and download_page is not page:
                await download_page.close()
        except:
            pass

    # Last rung: a Chrome that has never been automated. Some publishers put
    # the figure host behind the same challenge as the article, and by now the
    # shared browser has been driven by Playwright long enough to be refused.
    if 'fresh' in fig_ladder and fig_url:
        fresh_name = original_image_filename(fig_url, fig_num)
        saved = await _try_fresh_chrome_download(
            fig_url, output_dir, fresh_name, headless=not force_headed)
        if saved:
            print(f"    ✓ 保存: {saved} [一次性 Chrome]")
            return saved

    return None


# ============================================================================
# 第5部分：主工作流
# ============================================================================

async def _pdf_link_direct_download(
    doi: str,
    pdf_link: str,
    crossref_data: dict,
    output_path: Path,
    captured_data_dir: Path,
    force_headed: bool = False,
    referer: str = '',
) -> Optional[str]:
    """Fetch a PDF whose URL was handed to us, without opening the article page.

    The aggressive half of pdf-only mode: ``--json`` supplied "pdf_link", so
    there is nothing to extract and nothing to preflight. Everything the
    output needs -- the title and year that name the directory, the authors,
    the journal -- comes from the Crossref response fetched in Step 0, and a
    single browser is started, to fetch the file.

    Headed or headless is decided by the same Crossref publisher gate the main
    flow uses for Phase 0 (``_crossref_headless_publisher``): a publisher that
    is not on the headless list serves its PDF from a host that usually wants a
    challenge clicked, so it gets the throwaway headed Chrome -- still a single
    browser. An explicit ``force_headed`` from the command line wins outright.
    """
    print("\n⚡ pdf_link 直连模式：跳过预检与 doi.org，直接下载 PDF")
    print("=" * 80)
    print(f"  📎 {pdf_link}")

    if not crossref_data.get('title'):
        print("  ⚠️  Crossref 未返回标题，目录名将退化为 0000--paper")

    # Crossref 的 authors 是 dict（name/given/family），metadata.json 要的是名字
    metadata = {
        'doi': doi,
        'title': crossref_data.get('title', ''),
        'year': crossref_data.get('year'),
        'type': crossref_data.get('type', ''),
        'journal': crossref_data.get('journal', ''),
        'authors': [a.get('name', '') for a in crossref_data.get('authors', [])
                    if isinstance(a, dict) and a.get('name')],
        'volume': crossref_data.get('volume'),
        'issue': crossref_data.get('issue'),
        'pages': crossref_data.get('pages'),
        'pdf_url': pdf_link,
    }

    output_path.mkdir(parents=True, exist_ok=True)
    paper_output_dir = organize_paper_output(output_path, metadata, crossref_data)

    # 同一个 Crossref 出版商闸门：主流程用它决定要不要 Phase 0，这里用它决定有头
    # 还是无头 —— 两处共用 _crossref_headless_publisher()，不会各自走偏。不在无头
    # 直连表里的（ScienceDirect、SPIE、IOP、APS…）PDF 域名多半要点验证框，跟正常
    # 流程一样直接上有头。判定用的 Crossref 响应 Step 0 已经拿到，不额外发请求；
    # 有头这条走一次性 Chrome，依然只起一个浏览器。
    if not force_headed:
        matched_publisher = _crossref_headless_publisher(crossref_data)
        publisher_label = crossref_data.get('publisher') or 'N/A'
        if matched_publisher:
            print(f"  ✓ Crossref publisher '{publisher_label}' 属于 "
                  f"{matched_publisher.upper()} → 无头下载")
        else:
            force_headed = True
            print(f"  ⊘ Crossref publisher '{publisher_label}' 不在无头直连列表中 "
                  f"→ 改用有头一次性 Chrome")

    if referer:
        print(f"  ↪ 备用来路（最后一层用它点击跳转）: {referer[:80]}")

    downloads = await _download_all_resources(
        None,                     # 没有论文页面，这条路径也不需要
        {'pdf_url': pdf_link},
        paper_output_dir,
        None,
        metadata,
        doi,
        force_headed,
        reuse_context=False,      # 让它按需自起浏览器（无头时唯一的那次启动）
        pdf_only=True,
        referer_url=referer or '',
    )

    # Step 0 之前建的 DOI 缓存目录在这条路径上始终是空的，收掉
    try:
        if captured_data_dir.exists() and not any(captured_data_dir.iterdir()):
            captured_data_dir.rmdir()
    except OSError:
        pass

    save_metadata_json(paper_output_dir, metadata, crossref_data, doi,
                       downloads['pdf'], [], pdf_link=pdf_link)
    save_crossref_json(paper_output_dir, crossref_data)

    print("\n" + "=" * 80)
    print("📊 完成统计 (pdf_link 直连)")
    print("=" * 80)
    if downloads['pdf']:
        print(f"  📕 PDF: {downloads['pdf']}")
    else:
        print("  ⚠️  PDF 未下载成功")
    print(f"  💾 输出目录: {paper_output_dir}")
    print()

    return str(paper_output_dir) if downloads['pdf'] else None


async def complete_extraction_workflow(
    doi: str,
    output_file: str = None,
    force_headed: bool = False,
    refresh_headless_auth: bool = False,
    browser_session: SharedBrowserSession = None,
    link: str = None,
    extra_headers: dict = None,
    pdf_only: bool = False,
    pdf_link: str = None,
    referer: str = None,
):
    """完整提取工作流 - Phase 4/5 重构版本

    Args:
        doi: 论文的DOI标识符
        output_file: 输出目录路径 (可选，和命令行 --output 含义一致)
        force_headed: 是否强制使用有头浏览器，跳过无头预检 (默认: False)
                       - True: 跳过Phase 0，直接使用有头Chrome
                       - False: 先用无头浏览器预检，根据结果决定是否需要有头
        refresh_headless_auth: 是否通过CDP从真实Chrome刷新无头浏览器登录态
        link: 可选。若提供，会绕过 https://doi.org/{doi} 重定向，直接访问该 URL
              (headless 预检和有头访问都以此为主 URL)。对于绕过 doi.org
              redirect 时才会弹的反 bot 校验很有用。
        extra_headers: 可选。附加到每次导航请求上的 HTTP header dict
              (例如 {"referer": "https://pubs.aip.org/aip/pop/issue/24/12"})。
              会 merge 进 headless / headed 两条路径的 context extra headers；
              cookies 由 SharedBrowserSession 自己维护，不受影响。
        pdf_only: 只要 PDF。照常访问论文页面、解析出 PDF 链接并下载，但跳过
              图片/补充材料下载和 Markdown 生成；metadata.json 和 crossref.json
              照常写。老文章和会议短文的网页往往根本没有正文，这就够用了。
        pdf_link: 可选。直接给定 PDF 地址。给了就完全不碰出版商的论文页面：
              跳过 Phase 0 预检和 doi.org 跳转，元数据（含目录名要的标题/年份）
              全部取自 Step 0 那一次 Crossref 响应，只为取文件起一次浏览器。
              隐含 pdf_only=True。

    New architecture:
    1. Phase 0 (可选): 使用无头浏览器快速预检 (除非force_headed=True)
    2. If publisher supports headless extraction, process directly from the headless page
    3. Otherwise connect to headed Chrome and navigate to DOI
    4. Detect publisher
    5. Use handler's extract_all() to get all metadata and links in one go
    6. Download all resources using unified _download_all_resources
    7. Save everything
    """

    doi = doi.strip()
    output_path = Path(output_file or OUTPUT_DIR).expanduser().resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    captured_data_dir = output_path / doi.replace('/', '_')
    captured_data_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 80)
    print("🔍 论文完整提取工作流 (Phase 4-5)")
    print("=" * 80)
    print(f"🕒 {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"📌 DOI: {doi}\n")

    # 构建URL
    # If the caller supplied an explicit `link`, use it as the primary URL
    # (skips the doi.org redirect entirely — helpful for publishers whose
    # doi.org landing triggers a bot check that the direct URL avoids).
    doi_url = f"https://doi.org/{doi}"
    url = (link or '').strip() or doi_url
    if link:
        print(f"  ↪ 使用 JSON 中提供的 link，跳过 https://doi.org/{doi} 重定向")
        print(f"    → {url}")

    def build_headless_precheck_urls() -> list:
        """Return candidate URLs for Phase 0, avoiding a hard dependency on doi.org."""
        candidates = [url]
        # doi.org 作为最后 fallback（若 link 就是 doi.org 就不重复）
        if doi_url not in candidates:
            candidates.append(doi_url)
        publisher_hint = detect_publisher_from_url(url)

        if publisher_hint == 'nature' and '/' in doi:
            nature_article_id = doi.split('/', 1)[1].strip()
            if nature_article_id:
                nature_url = f"https://www.nature.com/articles/{nature_article_id}"
                if nature_url not in candidates:
                    candidates.append(nature_url)

        return candidates

    async def process_with_handler(page, context, handler, publisher, captured_data, force_headed_downloads):
        """Run publisher extraction and shared output/download steps."""
        print(f"Step 2️⃣  使用{publisher.upper()}Handler完整提取...")
        print("=" * 80)

        # Whether this run is headed. A handler cannot see it, but the bottom
        # rung of the fetch ladder must: a throwaway Chrome launched headless
        # during a headed run is the most detectable thing we could present,
        # and IOP's Radware check refuses exactly that. Set unconditionally,
        # before the landing-url block below, which is skipped when there is
        # no page at all.
        handler._force_headed = bool(force_headed_downloads)

        # Pin the article URL BEFORE extract_all runs. Handlers navigate the
        # page during extraction — the APS one visits /supplemental/{doi} to
        # enumerate attachments — so reading page.url afterwards records
        # whatever page the handler happened to finish on, not the article.
        # (That is how metadata.json ended up with a supplemental link.)
        # Handlers that want it can also read handler._landing_url, e.g. to
        # derive a journal code from the URL.
        landing_url = ''
        try:
            candidate = (page.url or '') if page is not None else ''
            if candidate and not candidate.startswith('about:'):
                landing_url = candidate
                handler._landing_url = landing_url
        except Exception:
            pass

        try:
            extraction_result = await handler.extract_all(captured=captured_data)
        except Exception as e:
            print(f"  ⚠️  extract_all 失败: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            raise

        metadata = extraction_result['metadata']
        links = extraction_result['links']
        fulltext_data = extraction_result['fulltext_data']
        # Handlers that can tell whether the article is readable say so here.
        # Default True: a handler with no opinion must not cause a skip.
        access = extraction_result.get('access', True)

        # Record the article URL pinned above — after the doi.org redirect
        # this is the direct publisher URL. save_metadata_json surfaces it as
        # "link" so future runs can bypass doi.org via the --json input.
        # Fall back to the handler's own value (it may have refined it) and
        # only then to the live page, which by now may have navigated away.
        if not landing_url:
            landing_url = getattr(handler, '_landing_url', '') or ''
        if not landing_url:
            try:
                candidate = (page.url or '') if page is not None else ''
                if candidate and not candidate.startswith('about:'):
                    landing_url = candidate
            except Exception:
                pass
        if landing_url:
            metadata['_landing_url'] = landing_url

        # Save HTML to the per-DOI capture directory.
        # page.html     = post-JS rendered DOM (fulltext_data from handler)
        # page_raw.html = raw server HTTP response (pre-JS, captured by interceptor)
        if isinstance(fulltext_data, str) and fulltext_data:
            save_html_snapshot(captured_data_dir / "page.html", fulltext_data, "HTML")

        raw_server_html = getattr(handler, '_raw_server_html', None)
        if raw_server_html:
            save_html_snapshot(captured_data_dir / "page_raw.html",
                               raw_server_html, "原始HTML")

        # Merge with Crossref data (fill in missing fields)
        if crossref_data:
            if crossref_data.get('year') and not metadata.get('year'):
                metadata['year'] = str(crossref_data['year'])
            if crossref_data.get('title') and not metadata.get('title'):
                metadata['title'] = crossref_data['title']
            if crossref_data.get('publisher') and not metadata.get('publisher'):
                metadata['publisher'] = crossref_data['publisher']
            if crossref_data.get('type') and not metadata.get('type'):
                metadata['type'] = crossref_data['type']
            # Store Crossref reference data for unified BibTeX generation
            # Note: fetch_crossref returns 'references' (plural), not 'reference'
            if crossref_data.get('references'):
                metadata['_crossref_references'] = crossref_data['references']
                print(f"  ✓ 从Crossref获取{len(crossref_data['references'])}条参考文献")
            else:
                print(f"  ⚠️  Crossref中没有参考文献数据")

        # Ensure DOI is set in metadata for markdown generation
        if not metadata.get('doi') and doi:
            metadata['doi'] = doi

        print(f"  ✓ 标题: {metadata.get('title', 'N/A')[:60]}...")
        print(f"  ✓ 作者: {len(metadata.get('authors', []))} 位")
        print(f"  ✓ 期刊: {metadata.get('journal', 'N/A')}")
        print(f"  ✓ 图片: {len(links.get('figure_urls', {}))} 个")
        print(f"  ✓ 补充材料: {len(links.get('supplemental_urls', []))} 个")
        print()

        # Prepare output directory
        base_output_dir = output_path
        base_output_dir.mkdir(parents=True, exist_ok=True)
        paper_output_dir = organize_paper_output(base_output_dir, metadata, crossref_data)

        # Move the DOI-named capture dir (raw HTML + API JSON dumps collected
        # during Phase 0 preflight + handler extract_all) into
        # <paper_dir>/html/ so every output for this paper lives under one
        # tree. The capture dir has to exist during extraction under a
        # DOI-based name because the paper title isn't known yet.
        import shutil as _shutil
        html_dir = paper_output_dir / 'html'
        if (captured_data_dir.exists()
                and captured_data_dir.resolve() != paper_output_dir.resolve()):
            html_dir.mkdir(parents=True, exist_ok=True)
            for src in list(captured_data_dir.iterdir()):
                dest = html_dir / src.name
                if dest.exists():
                    if dest.is_dir():
                        _shutil.rmtree(dest)
                    else:
                        dest.unlink()
                _shutil.move(str(src), str(dest))
            try:
                captured_data_dir.rmdir()
            except OSError:
                pass  # not empty (shouldn't happen) — leave it alone

        # The publisher said outright that this article is not readable, so
        # everything below is wasted effort: the PDF 404s or redirects back to
        # the landing page, the figures and supplements are gated the same
        # way, and there is no body text to turn into markdown. Keep the
        # landing page itself (html/ shows how the call was made) and
        # crossref.json, and stop.
        if not access:
            save_crossref_json(paper_output_dir, crossref_data)
            print("\n" + "=" * 80)
            print("🔒 无访问权限 — 跳过 PDF / 图片 / 补充材料 / Markdown")
            print("=" * 80)
            print(f"  📄 标题: {(metadata.get('title') or 'N/A')[:60]}")
            print(f"  💾 输出目录: {paper_output_dir}")
            print(f"     已保留: html/ 与 crossref.json")
            print()
            return str(paper_output_dir)

        markdown_filename = "paper.md"
        markdown_file = paper_output_dir / markdown_filename

        # Step 3: Download all resources
        downloads = await _download_all_resources(
            page,
            links,
            paper_output_dir,
            context,
            metadata,
            doi,
            force_headed_downloads,
            reuse_context=browser_session is not None,
            pdf_only=pdf_only,
            # The article URL, pinned before extract_all ran. It is what the
            # last rung clicks through from, so a normal run gets that rung
            # for free -- no JSON, no configuration.
            referer_url=(metadata.get('_landing_url') or ''),
        )

        # Step 3.5: Check if paper has meaningful content before saving
        # Skip save only if there's no title (truly empty paper)
        # References are optional - some publishers don't provide them
        title = (metadata.get('title') or '').strip()
        abstract = (metadata.get('abstract') or '').strip()
        has_content = bool(title) or bool(abstract)

        if not has_content:
            print(f"\n⚠️  论文缺少标题和摘要，跳过保存")
            print(f"  DOI: {doi}")
            return None

        refs = metadata.get('references', [])
        if not refs and not SAVE_WITHOUT_REFERENCES:
            print(f"\n⚠️  未找到参考文献（某些出版商可能不提供）")
            if SAVE_WITHOUT_REFERENCES:
                print(f"  提示：将继续保存，因为 SAVE_WITHOUT_REFERENCES=True")
            else:
                print(f"  提示：可在 config.py 中设置 SAVE_WITHOUT_REFERENCES=True 强制保存")

        # pdf-only：PDF 就是全部交付物，不生成 Markdown。元数据照常落盘 ——
        # metadata.json 里记着 pdf_link，PDF 没下来时可以据此重试。
        if pdf_only:
            save_metadata_json(paper_output_dir, metadata, crossref_data, doi,
                               downloads['pdf'], downloads['supplemental'],
                               pdf_link=links.get('pdf_url') or '')
            save_crossref_json(paper_output_dir, crossref_data)

            print("\n" + "=" * 80)
            print("📊 完成统计 (pdf-only)")
            print("=" * 80)
            if downloads['pdf']:
                print(f"  📕 PDF: {downloads['pdf']}")
            else:
                print("  ⚠️  PDF 未下载成功")
            print(f"  💾 输出目录: {paper_output_dir}")
            print()

            return str(paper_output_dir) if downloads['pdf'] else None

        # Step 3.5: Generate markdown with figures
        print("\nStep 3.5️⃣  生成Markdown...")
        print("=" * 80)
        try:
            md = handler.convert_to_markdown(
                metadata,
                fulltext_data,
                add_figure_refs=bool(downloads['figures']),
                figure_filenames=downloads['figures'],
                figure_urls=links.get('figure_urls', {}),
                supplemental_urls=links.get('supplemental_urls', []),
                supplemental_descriptions=links.get('supplemental_descriptions', {}),
                supplemental_downloads=downloads.get('supplemental', []),
                key_image_filename=downloads.get('key_image'),
                table_data=links.get('table_data', {}),
            )
        except Exception as e:
            print(f"  ⚠️  convert_to_markdown 失败: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            raise
        with open(markdown_file, 'w', encoding='utf-8') as f:
            f.write(md)
        print(f"  ✓ Markdown已保存: {markdown_filename}")

        # Save metadata
        save_metadata_json(paper_output_dir, metadata, crossref_data, doi,
                         downloads['pdf'], downloads['supplemental'],
                         pdf_link=links.get('pdf_url') or '')

        # The Crossref response, verbatim, next to metadata.json.
        save_crossref_json(paper_output_dir, crossref_data)

        # Statistics
        print("\n" + "=" * 80)
        print("📊 完成统计")
        print("=" * 80)
        lines = md.split('\n')
        display_eqs = len(re.findall(r'\$\$', md)) // 2
        print(f"  📄 Markdown 行数: {len(lines)}")
        print(f"  🖼️  图片: {len(downloads['figures'])} 个")
        print(f"  📐 Display equations: {display_eqs} 个")
        if downloads['pdf']:
            print(f"  📕 PDF: {downloads['pdf']}")
        if downloads['supplemental']:
            print(f"  📎 补充材料: {len(downloads['supplemental'])} 个")
        print(f"  💾 输出目录: {paper_output_dir}")
        print(f"  📝 Markdown 文件: {markdown_file}")
        print()

        return str(markdown_file)

    async def cleanup_context_pages(context):
        """Close all pages in a browser context."""
        print("\n🧹 清理标签页...")
        print("=" * 80)
        for context_page in context.pages:
            try:
                await context_page.close()
            except:
                pass
        print("  ✓ 标签页已清理")
        print()

    def _cleanup_via_cdp(debug_port: int, current_doi_url: str = ""):
        """通过纯 CDP 协议关闭论文页面标签页，保留至少一个空白页。
        不依赖 Playwright 的 page/context 对象，避免状态不一致导致挂死。
        同步函数，使用 requests 直接调用 CDP HTTP endpoint。"""
        import json
        import urllib.request
        try:
            # 获取所有 target
            resp = urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json", timeout=5)
            targets = json.loads(resp.read().decode())
            # 筛选页面类型的 target（排除 background_page、service_worker 等）
            # 只算真实网页 tab，排除 Chrome 内部页面
            # （Omnibox Popup、settings 等不算，不能阻止 Chrome 退出）
            internal_url_prefixes = ("chrome://omnibox", "chrome://settings", 
                                      "chrome://history", "chrome://bookmarks",
                                      "chrome://extensions", "chrome://flags")
            page_targets = [
                t for t in targets
                if t.get("type") == "page"
                and not any(t.get("url", "").startswith(p) for p in internal_url_prefixes)
            ]
            # 关闭所有非系统页（保留 chrome://newtab / about:blank）
            # 不按出版社区分，避免 ScienceDirect 等其他域名的 tab 泄漏
            to_close = []
            for t in page_targets:
                t_url = t.get("url", "")
                # 系统白名单：这些页面保留
                if t_url in ("chrome://newtab/", "about:blank", "chrome://newtab"):
                    continue
                # 其他全部关掉（AIP、ScienceDirect、DOI 跳转页等）
                to_close.append(t["id"])
            # 如果关完之后就没页面了，就少关一个（保留最后一个）
            if len(to_close) >= len(page_targets) and len(to_close) > 0:
                to_close = to_close[:-1]
            closed = 0
            for tid in to_close:
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{debug_port}/json/close/{tid}",
                        method="GET"
                    )
                    urllib.request.urlopen(req, timeout=3)
                    closed += 1
                except Exception:
                    pass
            # 如果现在没有 page target 了，新建一个
            resp2 = urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json", timeout=5)
            targets2 = json.loads(resp2.read().decode())
            # 只算真实网页 tab，排除 Chrome 内部页面
            page_targets2 = [
                t for t in targets2
                if t.get("type") == "page"
                and not any(t.get("url", "").startswith(p) for p in internal_url_prefixes)
            ]
            if not page_targets2:
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{debug_port}/json/new",
                        method="PUT"
                    )
                    urllib.request.urlopen(req, timeout=3)
                except Exception:
                    pass
        except Exception as e:
            try:
                print(f"  ⚠️  CDP 清理标签页异常: {e}")
            except:
                pass


    def check_chrome_ready():
        """Check whether the headed Chrome CDP endpoint is available."""
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex(('127.0.0.1', CHROME_DEBUG_PORT))
            sock.close()
            return result == 0
        except:
            return False

    async def ensure_headed_chrome_ready(connect_playwright: bool = True) -> bool:
        """Start the real headed Chrome profile if the CDP endpoint is not ready.
        connect_playwright=False 时只启动 Chrome 不连接 Playwright，
        避免 Playwright 注入自动化指纹影响 Cloudflare 挑战。"""
        if check_chrome_ready():
            if connect_playwright and browser_session is not None:
                await browser_session.connect_headed_browser()
            return True

        if browser_session is not None:
            print("⚠️  Chrome 未运行，正在启动批次共享实例...")
            # 先只启动 Chrome（不过早连接 Playwright，避免自动化指纹）
            ready = await browser_session.launch_headed_chrome()
            if ready and connect_playwright:
                await browser_session.connect_headed_browser()
            if ready:
                print("✓ 批次共享 Chrome 已就绪\n")
            return ready

        print("⚠️  Chrome 未运行，正在启动...")
        chrome_launcher = Path(__file__).parent / "chrome_session.py"
        if not chrome_launcher.exists():
            print("⚠️  chrome_session.py 未找到\n")
            return False

        try:
            subprocess.Popen([sys.executable, str(chrome_launcher)])
        except Exception as e:
            print(f"⚠️  启动Chrome失败: {e}\n")
            return False

        for _ in range(30):
            await asyncio.sleep(1)
            if check_chrome_ready():
                print("✓ Chrome 已就绪\n")
                return True

        print("⚠️  Chrome 启动超时，无法读取真实浏览器登录态\n")
        return False

    def summarize_storage_state(storage_state: dict) -> str:
        cookie_count = len(storage_state.get('cookies', []))
        origin_count = len(storage_state.get('origins', []))
        return f"{cookie_count} cookies, {origin_count} origins"

    def load_saved_headless_storage_state():
        """Load persisted Playwright storage_state for the headless precheck."""
        if not HEADLESS_AUTH_STATE_FILE.exists():
            print(f"  ℹ️  未找到无头登录态文件: {HEADLESS_AUTH_STATE_FILE}")
            return None

        try:
            with open(HEADLESS_AUTH_STATE_FILE, 'r', encoding='utf-8') as f:
                storage_state = json.load(f)
            print(f"  ✓ 已加载无头登录态: {summarize_storage_state(storage_state)}")
            return storage_state
        except Exception as e:
            print(f"  ⚠️  读取无头登录态失败: {type(e).__name__}: {str(e)[:100]}")
            return None

    def save_headless_storage_state(storage_state: dict):
        """Persist Playwright storage_state for future remote headless runs."""
        try:
            HEADLESS_AUTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(HEADLESS_AUTH_STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(storage_state, f, ensure_ascii=False, indent=2)
            print(f"  ✓ 无头登录态已保存: {HEADLESS_AUTH_STATE_FILE}")
        except Exception as e:
            print(f"  ⚠️  保存无头登录态失败: {type(e).__name__}: {str(e)[:100]}")

    @asynccontextmanager
    async def playwright_scope():
        """Reuse the batch Playwright driver when one was supplied."""
        if browser_session is not None:
            yield browser_session.playwright
        else:
            async with async_playwright() as playwright:
                yield playwright

    @asynccontextmanager
    async def headed_connection_scope():
        """Yield the batch CDP connection, or a temporary one for legacy callers."""
        if browser_session is not None:
            try:
                connection = await browser_session.ensure_headed_context()
            except Exception as exc:
                print(f"❌ 无法连接到共享Chrome port {CHROME_DEBUG_PORT}: {exc}")
                connection = None
            yield connection
            return

        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.connect_over_cdp(
                    f"http://localhost:{CHROME_DEBUG_PORT}"
                )
                if browser.contexts:
                    context = browser.contexts[0]
                else:
                    context = await browser.new_context(accept_downloads=True)
                connection = (browser, context)
            except Exception as exc:
                print(f"❌ 无法连接到Chrome port {CHROME_DEBUG_PORT}: {exc}")
                connection = None
            yield connection

    async def export_headed_chrome_storage_state(playwright):
        """Export cookies/localStorage from the headed Chrome profile for headless use."""
        if not await ensure_headed_chrome_ready():
            return None

        try:
            headed_browser = await playwright.chromium.connect_over_cdp(
                f"http://localhost:{CHROME_DEBUG_PORT}"
            )
            if not headed_browser.contexts:
                print("  ⚠️  有头Chrome没有可用context，Phase 0将使用干净无头context")
                return None

            headed_context = headed_browser.contexts[0]
            storage_state = await headed_context.storage_state()
            print(f"  ✓ 已从真实Chrome导出登录态: {summarize_storage_state(storage_state)}")
            save_headless_storage_state(storage_state)
            return storage_state
        except Exception as e:
            print(f"  ⚠️  读取真实Chrome登录态失败: {type(e).__name__}: {str(e)[:100]}")
            return None

    async def load_headless_storage_state(playwright):
        """Resolve the storage_state used by Phase 0 without requiring CDP by default."""
        if refresh_headless_auth:
            print("  🔄 正在从真实Chrome刷新无头登录态...")
            storage_state = await export_headed_chrome_storage_state(playwright)
            if storage_state:
                return storage_state
            print("  → 刷新失败，将尝试读取已有无头登录态文件")

        storage_state = load_saved_headless_storage_state()
        if storage_state:
            return storage_state

        print("  → Phase 0将使用干净无头context继续预检")
        return None

    # ========== 预获取Crossref元数据（Phase 0之前）==========
    print("\nStep 0️⃣ (Pre)  获取Crossref元数据...")
    print("=" * 80)
    crossref_data = fetch_crossref(doi)
    if crossref_data.get('title'):
        print(f"  ✓ 标题: {crossref_data['title'][:60]}...")
        print(f"  ✓ 出版商: {crossref_data.get('publisher', 'N/A')}")
        print(f"  ✓ 类型: {crossref_data.get('type', 'N/A')}")
        print(f"  ✓ 年份: {crossref_data.get('year', 'N/A')}")
        print(f"  ✓ 参考文献: {len(crossref_data.get('references', []))} 条")
    else:
        print("  ⚠️  Crossref未返回数据")
    print()

    # ========== pdf_link 直连：跳过预检与 doi.org，只取 PDF ==========
    # JSON 里已经给了 PDF 地址，就没有任何理由再去访问出版商的论文页面。
    # 目录名要的 title/year 就在刚拿到的 Crossref 响应里。
    if pdf_link:
        return await _pdf_link_direct_download(
            doi=doi,
            pdf_link=pdf_link,
            crossref_data=crossref_data,
            output_path=output_path,
            captured_data_dir=captured_data_dir,
            force_headed=force_headed,
            referer=referer or '',
        )

    # ========== 第1步判断：根据Crossref publisher决定是否需要Phase 0 ==========
    should_use_headless_phase0 = False
    if not force_headed:
        crossref_publisher = crossref_data.get('publisher', '').lower()
        matched_publisher = _crossref_headless_publisher(crossref_data)
        if matched_publisher:
            should_use_headless_phase0 = True
            print(f"✓ 根据Crossref publisher '{crossref_publisher}' 判断出版商为 {matched_publisher.upper()}")
            print(f"  → 将使用Phase 0进行无头浏览器预检\n")
        else:
            print(f"⊘ Crossref publisher '{crossref_publisher}' 不在无头直连列表中")
            print(f"  → 跳过Phase 0，直接使用有头浏览器\n")

    # ========== 阶段0（可选）：使用无头浏览器快速预检 ==========
    # 如果 force_headed=True，跳过此阶段直接使用有头浏览器
    # 典型使用场景：已知目标期刊必须使用有头浏览器访问

    headless_success = False
    headless_blocked = False
    headless_publisher = None
    headless_html = None

    if force_headed:
        print("\n🔧 强制有头模式 - 跳过无头浏览器预检")
        print("=" * 80)
        print("  将直接使用有头Chrome访问\n")
    elif not should_use_headless_phase0:
        print("\n⊘ 出版商不支持无头浏览器 - 跳过Phase 0")
        print("=" * 80)
        print("  将直接使用有头浏览器完整提取\n")
    else:
        print("\n📋 Phase 0️⃣  使用无头浏览器快速预检页面...")
        print("=" * 80)

        try:
            async with playwright_scope() as p:
                storage_state = await load_headless_storage_state(p)
                headless_owned_context = None
                if browser_session is not None:
                    headless_browser = None
                    headless_context = await browser_session.ensure_headless_context(storage_state)
                else:
                    # Same reason as ensure_headless_context: the profile is
                    # what makes Chrome download a PDF instead of displaying it.
                    _hl_dir = scraping_profile_dir(HEADLESS_PROFILE_NAME)
                    prepare_profile_dir(_hl_dir, quiet=True)
                    _hl_kwargs = {'headless': True, 'accept_downloads': True,
                                  'args': chrome_password_store_args()}
                    _chrome_path = os.environ.get('CHROME_PATH', '').strip()
                    if _chrome_path:
                        _hl_kwargs['executable_path'] = _chrome_path
                    headless_browser = None
                    headless_context = await p.chromium.launch_persistent_context(
                        str(_hl_dir), **_hl_kwargs)
                    headless_owned_context = headless_context
                    if storage_state and storage_state.get('cookies'):
                        try:
                            await headless_context.add_cookies(storage_state['cookies'])
                        except Exception:
                            pass
                headless_page = await headless_context.new_page()

                # One entry point for both modes: MathJax interception,
                # headers, the response listener, the navigation and the
                # Cloudflare checkbox. See navigate_with_capture.
                _capture = PageCapture(doi)
                _final_url, headless_rendered_html, last_precheck_error = \
                    await navigate_with_capture(
                        headless_page,
                        build_headless_precheck_urls(),
                        capture=_capture,
                        publisher_token=_publisher_for_page(doi=doi),
                        extra_headers=extra_headers,
                    )

                try:
                    if last_precheck_error is not None:
                        raise last_precheck_error

                    # 保存无头浏览器访问结果
                    # page_raw.html = 原始HTTP响应（JS运行前）
                    # page.html     = 渲染后DOM
                    # headless_initial.html = 本阶段所见的那一份，便于溯源
                    headless_html = headless_rendered_html  # used for bot-detection below
                    headless_raw_html = _capture.land(
                        captured_data_dir, headless_rendered_html) or None
                    save_html_snapshot(captured_data_dir / "headless_initial.html",
                                       headless_raw_html or headless_rendered_html,
                                       "原始HTML" if headless_raw_html else "页面")

                    # 检测最终URL
                    final_headless_url = headless_page.url
                    print(f"  ✓ 最终URL: {final_headless_url}")

                    # 检测出版商
                    headless_publisher = detect_publisher_from_url(final_headless_url)
                    # Promote OUP → OUP_BOOK when Crossref says this DOI is a
                    # book or book-chapter (then OupBookHandler walks the TOC).
                    headless_publisher = apply_crossref_type_override(headless_publisher, crossref_data)
                    print(f"  ✓ 检测出版商: {headless_publisher.upper()}")

                    # 检测是否被反爬虫拦截
                    if is_bot_challenge_page(final_headless_url, headless_html):
                        print(f"  ⚠️  检测到反爬虫拦截页面 (validate.perfdrive.com 等)")
                        print(f"  → 无头浏览器被拦截，将回退到有头Chrome")
                        headless_success = False  # Force fallback to headed browser
                        headless_blocked = True
                        headless_publisher = None  # Prevent headless-only handler path
                    else:
                        headless_success = True

                    if headless_publisher in HEADLESS_ACCESSIBLE_PUBLISHERS:
                        print()
                        print("🟢 无头直连路径：当前出版商支持无头完整提取")
                        print("=" * 80)
                        print(f"  出版商类型: {headless_publisher.upper()}")
                        print("  → 跳过有头Chrome连接，直接使用无头页面进入Handler流程")

                        handler = get_publisher_handler(
                            headless_publisher,
                            page=headless_page,
                            captured_data_dir=captured_data_dir,
                            doi=doi,
                        )
                        handler.crossref_data = crossref_data
                        _capture.pin(handler, '预检捕获')

                        captured_data = None
                        if hasattr(handler, 'setup_network_capture'):
                            captured_data = handler.setup_network_capture(headless_page, doi)
                            print("✓ 网络监听已启动\n")

                        result = await process_with_handler(
                            headless_page,
                            headless_context,
                            handler,
                            headless_publisher,
                            captured_data,
                            force_headed,
                        )
                        await headless_page.close()
                        if headless_browser is not None:
                            await headless_browser.close()
                        if headless_owned_context is not None:
                            await headless_owned_context.close()
                        return result

                except Exception as e:
                    print(f"  ⚠️  无头浏览器访问失败: {type(e).__name__}: {str(e)[:100]}")
                    print(f"  → 这对某些需要认证或完整JavaScript渲染的出版商是正常的")
                    import traceback
                    traceback.print_exc()
                finally:
                    try:
                        await headless_page.close()
                    except:
                        pass
                    if headless_browser is not None:
                        try:
                            await headless_browser.close()
                        except:
                            pass
                    if headless_owned_context is not None:
                        try:
                            await headless_owned_context.close()
                        except:
                            pass
        except Exception as e:
            print(f"  ⚠️  无头浏览器启动失败: {e}")

        print()

        # ========== Phase 0分析：判断是否需要有头浏览器 ==========

        print("📊 Phase 0分析：评估是否需要有头浏览器...")
        print("=" * 80)

        if headless_success and headless_publisher:
            print(f"  出版商类型: {headless_publisher.upper()}")

            if headless_publisher in HEADLESS_ACCESSIBLE_PUBLISHERS:
                print(f"  ℹ️  {headless_publisher.upper()} 支持无头直连")
                print(f"  ⚠️  无头直连未完成，将尝试Handler自主管理无头访问")
            else:
                print(f"  ℹ️  {headless_publisher.upper()} 未配置无头直连")
                print(f"  💡 将使用有头浏览器进行完整提取")
        else:
            print(f"  ⚠️  无头浏览器预检失败")
            if detect_publisher_from_url(url) in HEADLESS_ACCESSIBLE_PUBLISHERS:
                print(f"  💡 DOI可识别为无头可访问出版商，将尝试Handler自主管理无头访问")
            else:
                print(f"  💡 将使用有头浏览器进行完整提取")

        print()

        fallback_publisher = headless_publisher or detect_publisher_from_url(url)
        fallback_publisher = apply_crossref_type_override(fallback_publisher, crossref_data)
        if fallback_publisher in HEADLESS_ACCESSIBLE_PUBLISHERS and not headless_blocked:
            print("🟢 无头Handler自主管理路径：当前出版商支持无头完整提取")
            print("=" * 80)
            print(f"  出版商类型: {fallback_publisher.upper()}")
            print("  → 不连接有头Chrome，交给PublisherHandler自行创建无头页面")

            if browser_session is not None:
                context = await browser_session.ensure_headless_context()
                page = await context.new_page()
                try:
                    handler = get_publisher_handler(
                        fallback_publisher,
                        page=page,
                        captured_data_dir=captured_data_dir,
                        doi=doi,
                    )
                    handler.crossref_data = crossref_data
                    return await process_with_handler(
                        page, context, handler, fallback_publisher, None, False
                    )
                finally:
                    try:
                        await page.close()
                    except Exception:
                        pass
            else:
                handler = get_publisher_handler(
                    fallback_publisher,
                    captured_data_dir=captured_data_dir,
                    doi=doi,
                )
                handler.crossref_data = crossref_data

                return await process_with_handler(
                    None,
                    None,
                    handler,
                    fallback_publisher,
                    None,
                    False,
                )

        print("  🔵 标准路径：使用有头浏览器完整提取")

        print()

    # 检查Chrome是否就绪（先只启动不连 Playwright，避免指纹影响 cf_bypass）
    if not await ensure_headed_chrome_ready(connect_playwright=False):
        return None

    # ── 预加载：先用纯 CDP 过 Cloudflare + 加载页面（Playwright 还没连，无指纹） ──
    _cf_preloaded = False
    _cf_pre_url = url
    if _CF_BYPASS_AVAILABLE:
        print("🛡️  预载：纯CDP过 Cloudflare 并加载页面（Playwright未连接，无自动化指纹）...")
        try:
            # Same "open + clear Cloudflare over raw CDP" helper the PDF
            # download uses, just pointed at the batch's shared browser
            # instead of a throwaway one.
            from chrome_session import open_url_via_cdp
            _cf_pre_result = await open_url_via_cdp(
                url,
                CHROME_DEBUG_PORT,
                expected_doi=doi,
                timeout_s=int(DP_CLOUDFLARE_TIMEOUT),
            )
            if _cf_pre_result["success"]:
                print(f"  ✅ 预载成功：挑战通过，页面已加载")
                _cf_preloaded = True
            else:
                print(f"  ⚠️  预载失败（挑战未通过），将走 Playwright 路径重试")
        except Exception as _e:
            print(f"  ⚠️  预载异常: {_e}")

    # 现在才让 Playwright 连接（如果挑战已通过，即使有指纹也不影响了）
    if browser_session is not None:
        await browser_session.connect_headed_browser()

    async with headed_connection_scope() as connection:
        if connection is None:
            print("   请运行: python chrome_session.py\n")
            return None
        browser, context = connection
        print("✓ 已连接到批次共享Chrome\n" if browser_session else "✓ 已连接到Chrome\n")
        try:
            print("✓ 使用批次共享context\n" if browser_session else "✓ 使用现有context\n")

            if browser_session is not None:
                await browser_session.sync_headless_to_headed(context)

            # ⚠️ The page is NOT created here. When the preload succeeded it
            # already has the article open in its own tab, and creating one
            # now only to close it moments later is a tab that visibly flashes
            # open and shut on the user's screen -- plus a wasted about:blank.
            # Decide first, create only if nothing usable came back, and hang
            # the headers / MathJax route / response listener on whichever
            # page we end up with. All of that still happens before the
            # page.goto() further down, which is the ordering those three
            # actually require.
            page = None

            # Intercept the main-document HTTP response to capture the raw server
            # HTML *before* JavaScript (e.g. MathJax) rewrites the DOM.
            _capture = PageCapture(doi)
            _preload_had_api = False

            # ⚠️ The listener below cannot see the article on a headed run.
            # The preload fetches the page over raw CDP *before* Playwright is
            # attached, so by the time this handler exists the main document
            # has already been and gone. That is why IOP fell back to an
            # in-page view-source fetch on every single run, why APS silently
            # lost abstract_html (see the comment in aps.py), and why
            # page_raw.html stopped being written. The preload now records
            # what it sees, so feed its documents into the same list and let
            # pick_raw_article_html decide which one is the article -- it
            # takes the largest marked candidate, which is exactly how the two
            # responses a protected publisher sends for one URL get resolved.
            if _cf_preloaded:
                try:
                    _pre = (_cf_pre_result.get('responses') or {})
                    # The preload is the only thing that sees the article
                    # document on a headed run: Playwright is not connected
                    # when it navigates. Merge it into the same capture the
                    # listener fills, then land it immediately -- waiting for
                    # extract_all meant page_raw.html appeared only once the
                    # whole extraction had run, and not at all if anything in
                    # between raised.
                    _capture.absorb_cdp(_pre)
                    _preload_had_api = bool(_capture.api)
                    _capture.announce('预载')
                    _capture.land(captured_data_dir)

                    # Opt-in diagnostic: what did the page fetch on its own?
                    #
                    # The sink records every response's metadata but only
                    # Document bodies are harvested, and none of the metadata
                    # is persisted -- so an ordinary run cannot answer "does
                    # this publisher's page issue its own API calls?". That
                    # question decides whether a handler can read a response
                    # the page already made instead of running fetch() inside
                    # it. Off by default; it prints nothing and costs nothing.
                    if (os.environ.get('DP_DUMP_RESPONSES') or '').strip().lower() \
                            in ('1', 'true', 'yes', 'on'):
                        _by_type: dict = {}
                        for _e in _pre.values():
                            _by_type[_e.get('type') or '?'] = \
                                _by_type.get(_e.get('type') or '?', 0) + 1
                        print(f"  🔍 预载响应 {len(_pre)} 条，按类型: "
                              + ", ".join(f"{k}={v}" for k, v in
                                          sorted(_by_type.items())))

                        # Targeted count over the WHOLE sink, not just the
                        # lines printed below.
                        #
                        # ⚠️ The listing is capped, so "I did not see it" is
                        # not evidence of absence -- on an IEEE probe the cap
                        # hid 6 of the 9 dynamic requests, which is more than
                        # enough room for the four REST calls the question was
                        # about. Counting every entry is what makes a negative
                        # result mean something.
                        _pat = (os.environ.get('DP_DUMP_RESPONSES_MATCH')
                                or '/rest/,/api/,/sdfe/,/ajax/')
                        _needles = [p.strip().lower()
                                    for p in _pat.split(',') if p.strip()]
                        _hits = [_e for _e in _pre.values()
                                 if any(nd in (_e.get('url') or '').lower()
                                        for nd in _needles)]
                        _with_body = [_e for _e in _hits if _e.get('body')]
                        print(f"  🔍 匹配 {_pat!r} 的响应: {len(_hits)} 条"
                              + ("（页面自己发起的 API 调用）" if _hits
                                 else "（页面加载期间没有自己发起这些调用）"))
                        # The count that decides whether capture is usable:
                        # recording a URL proves the page asked for it, but
                        # only a body proves we can skip asking again. XHR
                        # bodies complete earlier than the document, so
                        # eviction is the thing to watch.
                        print(f"  🔍 其中已取到 body 的: {len(_with_body)} 条"
                              + (f"（最大 {max(len(_e['body']) for _e in _with_body):,} 字符）"
                                 if _with_body else "（body 全部为空 —— 要么未开"
                                 " DP_HARVEST_API，要么已被驱逐）"))
                        for _e in _hits:
                            print(f"     ★ [{_e.get('type') or '?':9s}] "
                                  f"{_e.get('status')} "
                                  f"{(_e.get('url') or '')[:120]}")
                        # Documents are never elided: their count is how you
                        # tell one page load from two, and a capped list hid
                        # exactly that once already.
                        _docs = [_e for _e in _pre.values()
                                 if (_e.get('type') or '') == 'Document']
                        print(f"  🔍 Document 响应 {len(_docs)} 条（全部列出）:")
                        for _e in _docs:
                            print(f"     ▣ {_e.get('status')} "
                                  f"{(_e.get('url') or '')[:110]}")
                        _shown = 0
                        for _e in _pre.values():
                            if (_e.get('type') or '') in ('Image', 'Stylesheet',
                                                          'Font', 'Media',
                                                          'Document'):
                                continue
                            if _shown >= 60:
                                print("  🔍 …（其余从略）")
                                break
                            print(f"     [{_e.get('type') or '?':9s}] "
                                  f"{_e.get('status')} "
                                  f"{(_e.get('mimeType') or '')[:28]:28s} "
                                  f"{(_e.get('url') or '')[:110]}")
                            _shown += 1
                except Exception as _e:
                    print(f"  ⚠️  预载响应合并失败: {_e}")

            # ── 纯 CDP 过 Cloudflare 挑战 + 预加载页面 ──
            # 如果预载阶段（Playwright 连接前）已经成功过了挑战，直接复用页面。
            # 否则用 Playwright 连接后的 CDP 再试一次（作为 fallback）。
            _cf_loaded = False  # 纯CDP是否已成功加载页面
            # (_cf_raw_html removed: the CDP-bypass paths no longer push
            # page.content() into the capture, so nothing holds it.)

            if _cf_preloaded:
                # 预载已成功：在 Playwright pages 中找到对应页面复用
                print("🛡️  复用预载页面（Playwright连接前已通过 Cloudflare）...")
                _cf_pre_target_id = _cf_pre_result.get("target_id")
                _cf_page_obj = await _find_pw_page_by_cdp_target(browser, _cf_pre_target_id)
                if _cf_page_obj is None:
                    for _ctx in browser.contexts:
                        for _pg in _ctx.pages:
                            try:
                                _pg_url = _pg.url
                            except Exception:
                                _pg_url = ''
                            if _pg_url and (url in _pg_url or _pg_url == url):
                                _cf_page_obj = _pg
                                break
                        if _cf_page_obj:
                            break
                # The preloaded target is not always the article. Publisher
                # pages spawn out-of-process iframes (Wiley's ad "User-Sync"
                # document is one) that Chrome exposes as separate CDP page
                # targets, and picking one of those yields a 235-byte stub:
                # extraction then finds no authors, no figures and no body,
                # and an in-page fetch from it fails CORS. Verify the page
                # actually holds the article before committing to it.
                _cf_page_obj = await _pick_article_page(browser, _cf_page_obj,
                                                        url, doi)
                # When no open page holds the article, do not settle for the
                # stub: leaving _cf_loaded False makes the flow fall through
                # to an ordinary Playwright navigation, which works because
                # the Cloudflare clearance is already in the profile.
                if _cf_page_obj is not None and not await _looks_like_article_page(
                        _cf_page_obj, url, doi):
                    print("  ⚠️  预载页面不可用，改为 Playwright 直接导航")
                    _cf_page_obj = None
                if _cf_page_obj is not None:
                    print(f"  ✓ 找到预载页面，直接复用")
                    # Nothing to close: the page is created below only when
                    # this branch does not supply one.
                    page = _cf_page_obj
                    # ⚠️ Deliberately NOT appending page.content() into
                    # the capture. Its documents are consumed as
                    # _raw_server_html, which handlers treat as the pre-JS
                    # server body -- Optica and Cambridge read it precisely to
                    # get TeX that MathJax would have destroyed. Feeding it the
                    # rendered DOM makes the two indistinguishable and defeats
                    # get_page_html()'s own fallback, which already calls
                    # page.content() when no raw body was captured. Leaving it
                    # empty loses nothing and keeps the provenance honest.
                    _cf_loaded = True
                else:
                    print(f"  ⚠️  未找到预载页面，将重新尝试")
            
            if not _cf_loaded and _CF_BYPASS_AVAILABLE:
                # Fallback：Playwright 已连接后再用纯 CDP 试一次
                print("🛡️  Fallback：纯CDP模式过 Cloudflare 并预加载页面...")
                try:
                    _cf_result = await bypass_cloudflare_cdp(
                        url=url,
                        debug_port=CHROME_DEBUG_PORT,
                        timeout_s=DP_CLOUDFLARE_TIMEOUT,
                        wait_for_content=True,
                        expected_doi=doi,
                    )
                    if _cf_result["success"]:
                        print(f"  ✅ 纯CDP挑战通过")
                        # 在 Playwright 中找到这个 page 并复用（优先按 CDP targetId，与 URL 无关）
                        _cf_target_id = _cf_result.get("target_id")
                        _cf_page_obj = await _find_pw_page_by_cdp_target(browser, _cf_target_id)
                        if _cf_page_obj is None:
                            for _ctx in browser.contexts:
                                for _pg in _ctx.pages:
                                    try:
                                        _pg_url = _pg.url
                                    except Exception:
                                        _pg_url = ''
                                    if url in _pg_url or _pg_url == url:
                                        _cf_page_obj = _pg
                                        break
                                if _cf_page_obj:
                                    break
                        
                        if _cf_page_obj is not None:
                            print(f"  ✓ 找到对应 Playwright page，将直接复用")
                            # 同上：page 尚未创建，没有要关的东西
                            page = _cf_page_obj
                            # Same as above: page.content() is the rendered DOM
                            # and must not masquerade as the raw server body in
                            # the capture. get_page_html() falls back to
                            # page.content() on its own when nothing was
                            # captured, so nothing is lost by not faking it.
                            _cf_loaded = True
                        else:
                            print(f"  ⚠️  未找到对应 page，将用 Playwright 重新导航")
                    else:
                        print(f"  ⚠️  纯CDP挑战未通过，仍将尝试Playwright路径")
                except Exception as _e:
                    print(f"  ⚠️  纯CDP挑战模块异常: {_e}")
            else:
                print("  ℹ️  chrome_session 模块不可用，跳过纯CDP预检查")

            # Neither CDP path produced a usable page, so make one now and let
            # the ordinary Playwright navigation below drive it.
            if page is None:
                page = await context.new_page()

            # Now that the final page is known, attach everything that has to
            # be in place before it navigates.
            #
            # On the preload path the page has already loaded, so these three
            # do nothing for *that* load -- the raw body comes from the
            # preload's own capture instead. They still matter whenever this
            # flow navigates itself, which is exactly the page just created.
            # Headers, MathJax interception, the response listener, the
            # navigation and the Cloudflare checkbox all happen in
            # navigate_with_capture below -- the same call the headless branch
            # makes. Nothing about them is mode-specific.
            _pub_pre = _publisher_for_page(url=url, doi=doi)

            # Step 1: Navigate and detect publisher
            print("Step 1️⃣  导航到DOI并检测出版商...")
            print("=" * 80)
            publisher = detect_publisher_from_url(url)
            publisher = apply_crossref_type_override(publisher, crossref_data)
            handler = get_publisher_handler(
                publisher,
                page=page,
                captured_data_dir=captured_data_dir,
                doi=doi,
            )
            handler.crossref_data = crossref_data
            captured_data = None
            if hasattr(handler, 'setup_network_capture'):
                captured_data = handler.setup_network_capture()
                print("✓ 网络监听已启动\n")

            # urls=None when the preload already opened the page: the
            # listener and settings still have to be installed (this flow may
            # navigate later), and the challenge check still applies to
            # whatever is on screen.
            if _cf_loaded:
                print("  ✓ 页面已由纯CDP预加载，跳过 goto")
            await navigate_with_capture(
                page,
                None if _cf_loaded else url,
                capture=_capture,
                publisher_token=_pub_pre,
                extra_headers=extra_headers,
            )

            # Store the raw server HTML on the handler so it can use it instead
            # of page.content() (which returns the post-JS-rendered DOM).
            # Pick the document that is actually the article, not merely the
            # last one seen -- the listener also records the doi.org redirect
            # hop and any interstitial. See pick_raw_article_html.
            _headed_raw = _capture.raw_html() or None

            final_url = page.url
            print(f"✓ 最终 URL: {final_url}")

            final_publisher = detect_publisher_from_url(final_url)
            final_publisher = apply_crossref_type_override(final_publisher, crossref_data)
            if final_publisher != publisher:
                publisher = final_publisher
                handler = get_publisher_handler(
                    publisher,
                    page=page,
                    captured_data_dir=captured_data_dir,
                    doi=doi,
                )
                handler.crossref_data = crossref_data
                captured_data = None
                if hasattr(handler, 'setup_network_capture'):
                    captured_data = handler.setup_network_capture()
                    print("✓ 网络监听已启动\n")
            else:
                handler.configure(page=page, captured_data_dir=captured_data_dir, doi=doi)

            # The preload's capture wins when there is one -- it sees the
            # page's own load, which is when these calls happen. The
            # listener's is the fallback (and, on the headless branch above,
            # the only capture there is).
            # Silent when the preload already printed its tally; the
            # listener's own capture announces itself.
            _capture.pin(handler, '' if _preload_had_api else '监听捕获')

            print(f"✓ 检测出版商: {publisher.upper()}\n")

            # Step 2: Use handler's extract_all for complete extraction
            #
            # handler.extract_all() 返回一个统一的字典结构（所有出版商通用）：
            # {
            #     'metadata': {
            #         'title': str,                    # 论文标题
            #         'authors': [str],               # 作者列表
            #         'author_with_affiliations': [   # 带机构的作者信息
            #             {'author': str, 'affiliations': [str]}
            #         ],
            #         'abstract': str,                # 摘要
            #         'journal': str,                 # 期刊名称
            #         'year': str,                    # 发表年份
            #         'volume': str,                  # 卷号
            #         'issue': str,                   # 期号
            #         'pages': str,                   # 页码
            #         'doi': str,                     # DOI
            #         'publication_date': str,        # 发表日期
            #         'corresponding_author_emails': [str],  # 通讯作者邮箱
            #         'references': [str],            # 参考文献列表
            #     },
            #     'links': {
            #         'pdf_url': str,                 # PDF下载链接 (如 https://journals.aps.org/prl/pdf/...)
            #         'figure_urls': {
            #             'fig_1': {                  # 图片ID
            #                 'url': str,             # 图片完整URL
            #                 'caption': str,         # 图片标题
            #             },
            #             ...
            #         },
            #         'supplemental_urls': [str],     # 补充材料链接列表
            #         'supplemental_descriptions': {  # 补充材料描述 (可选)
            #             'filename': 'description',
            #             ...
            #         },
            #     },
            #     'fulltext_data': str|dict,          # 文章内容，格式由PublisherHandler决定
            #     'journal_prefix' / 'journal_name': str, # 可选的出版商扩展字段
            # }
            if callable(getattr(handler, 'extract_all', None)):
                result = await process_with_handler(page, context, handler, publisher, captured_data, True)
            else:
                # Other publishers - use Crossref metadata only
                print("Step 2️⃣  使用Crossref元数据...")
                print("=" * 80)

                metadata = crossref_data or {
                    'title': 'Unknown Paper',
                    'authors': [],
                    'journal': 'Unknown Journal',
                    'year': None
                }

                print(f"  ✓ 标题: {metadata.get('title', 'N/A')[:60]}...")
                print(f"  ✓ 作者: {len(metadata.get('authors', []))} 位")
                print(f"  ✓ 期刊: {metadata.get('journal', 'N/A')}")
                print(f"  ✓ DOI: {doi}")
                print()
                result = None

            if browser_session is not None:
                await browser_session.sync_headed_to_headless(context)

            # Clean up only this DOI's page when using a batch context. Closing
            # every page makes desktop Chrome exit and loses batch cookies.
            print("\n🧹 清理标签页...")
            print("=" * 80)
            if browser_session is not None:
                # 批次共享模式：用 CDP 协议直接关 tab，不走 Playwright
                # 原因：Playwright connect_over_cdp 的 context/page 状态可能与 Chrome 不一致，
                # 导致 page.close() 挂死或报 TargetClosedError
                _cleanup_via_cdp(CHROME_DEBUG_PORT, current_doi_url=url)
                # 重置 Playwright 端的 context 缓存，下一篇重新 connect 获取最新状态
                browser_session.headed_context = None
                print("  ✓ 当前DOI标签页已关闭（CDP方式），Playwright context已重置")
            else:
                pages_to_close = list(context.pages)
                for p in pages_to_close:
                    try:
                        await p.close()
                    except:
                        pass
                try:
                    blank_page = await context.new_page()
                    await blank_page.goto("about:blank")
                    print("  ✓ 标签页已清理")
                except:
                    pass

            print()
            return result

        except Exception as e:
            print(f"❌ 错误: {e}")
            import traceback
            traceback.print_exc()

            try:
                if browser_session is not None:
                    # 批次共享模式：CDP 方式关 tab，避免 Playwright 状态不一致挂死
                    _cleanup_via_cdp(CHROME_DEBUG_PORT, current_doi_url=url)
                    browser_session.headed_context = None
                else:
                    pages_to_close = list(context.pages)
                    for p in pages_to_close:
                        try:
                            await p.close()
                        except:
                            pass
            except Exception:
                pass

            return None


# ============================================================================
# 入口点
# ============================================================================

async def main():
    """Entry point with argparse support

    Examples:
        # 单个DOI
        python complete_paper_extraction.py --doi 10.1103/PhysRevLett.109.245005

        # 从文件列表
        python complete_paper_extraction.py --file doi_list.txt

        # 指定输出目录
        python complete_paper_extraction.py --doi 10.1103/PhysRevLett.109.245005 --output ~/Downloads

        # 强制使用有头浏览器（跳过无头预检）
        python complete_paper_extraction.py --doi 10.1103/PhysRevLett.109.245005 --force-headed

        # DOI列表 + 强制有头
        python complete_paper_extraction.py --file doi_list.txt --force-headed
    """
    try:
        import argparse

        parser = argparse.ArgumentParser(
            description="完整论文提取工作流 - 从DOI到完整Markdown的端到端解决方案",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
示例：
  单个DOI:
    python %(prog)s --doi 10.1103/PhysRevLett.109.245005

  从文件列表:
    python %(prog)s --file doi_list.txt

  从 JSON 列表 (每项可选 link + header 字典):
    python %(prog)s --json examples/examples.json

  指定输出目录:
    python %(prog)s --doi 10.1103/PhysRevLett.109.245005 --output ~/Downloads

  强制使用有头浏览器（跳过无头预检）:
    python %(prog)s --doi 10.1103/PhysRevLett.109.245005 --force-headed

  只要 PDF，不生成 Markdown（老文章 / 会议短文的网页往往没有正文）:
    python %(prog)s --file doi_list.txt --pdf-only

  已知 PDF 地址，连论文页面都不访问（JSON 里给 pdf_link，自动进入 pdf-only）:
    python %(prog)s --json examples/examples.json

JSON 格式:
  {
    "article": [
      {
        "doi": "10.1063/1.4994562",                                    # 必填
        "link": "https://pubs.aip.org/aip/pop/article/24/12/...",       # 可选：绕过 doi.org 重定向
        "pdf_link": "https://.../paper.pdf",                            # 可选：直接给 PDF 地址；
                                                                        #   跳过预检和 doi.org，
                                                                        #   元数据取自 Crossref
        "referer": "https://www.nature.com/articles/xxx",               # 可选：来路页。前几层都
                                                                        #   失败时，一次性 Chrome 先
                                                                        #   打开它，再点击跳到 pdf_link
                                                                        #   （缺省则读 header.referer；
                                                                        #    都没有就不走这一层）
        "header": {"referer": "https://pubs.aip.org/aip/pop/issue/24/12"} # 可选：附加 HTTP header
      }
    ]
  }
        """
        )

        # 创建互斥组用于--doi、--file、--json
        input_group = parser.add_mutually_exclusive_group(required=True)
        input_group.add_argument(
            '--doi',
            type=str,
            help='单个DOI (例如: 10.1103/PhysRevLett.109.245005)'
        )
        input_group.add_argument(
            '--file',
            type=str,
            metavar='FILE',
            help='包含DOI列表的文件 (每行一个DOI)'
        )
        input_group.add_argument(
            '--json',
            type=str,
            metavar='FILE',
            help='含 article 列表的 JSON 文件 (每篇必须有 "doi"，可选 "link"、'
                 '"pdf_link"、"referer" 和 "header" 字典)。给了 "pdf_link" 就跳过'
                 '预检和 doi.org 直接下载该 PDF；"referer" 供取数阶梯最后一层'
                 '点击跳转使用（缺省读 header.referer）。'
                 '见 examples/examples.json 的格式。'
        )

        parser.add_argument(
            '--output',
            type=str,
            default=OUTPUT_DIR,
            help=f'输出目录路径 (默认: {OUTPUT_DIR})'
        )

        parser.add_argument(
            '--force-headed',
            action='store_true',
            default=False,
            help='强制使用有头浏览器，跳过无头预检阶段 (默认: False，使用智能检测)'
        )

        parser.add_argument(
            '--pdf-only',
            action='store_true',
            default=False,
            help='只要 PDF：照常访问论文页面取 PDF 链接并下载，但跳过图片/补充材料'
                 '下载和 Markdown 生成 (metadata.json / crossref.json 照常写)'
        )

        parser.add_argument(
            '--refresh-headless-auth',
            action='store_true',
            default=False,
            help='通过本机Chrome CDP导出登录态到 .auth/headless_storage_state.json，供后续无头预检使用'
        )

        args = parser.parse_args()

        # 构建 article 列表 —— 每项是 dict: {"doi", "link"?, "header"?}
        articles = []
        if args.doi:
            articles = [{"doi": args.doi}]
            print(f"📌 单个DOI: {args.doi}\n")
        elif args.file:
            try:
                with open(args.file, 'r', encoding='utf-8') as f:
                    dois = [line.strip() for line in f
                            if line.strip() and not line.strip().startswith('#')]
                articles = [{"doi": d} for d in dois]
                print(f"📌 从文件读取 {len(articles)} 个DOI: {args.file}\n")
            except FileNotFoundError:
                print(f"❌ 文件不存在: {args.file}")
                sys.exit(1)
            except Exception as e:
                print(f"❌ 读取文件时出错: {e}")
                sys.exit(1)
        elif args.json:
            import json as _json
            try:
                with open(args.json, 'r', encoding='utf-8') as f:
                    payload = _json.load(f)
                raw_articles = payload.get('article') or payload.get('articles') or []
                if not isinstance(raw_articles, list):
                    print(f"❌ JSON 顶层需含 'article' 数组: {args.json}")
                    sys.exit(1)
                for idx, item in enumerate(raw_articles, 1):
                    if not isinstance(item, dict):
                        print(f"  ⚠️  第 {idx} 项不是 dict，跳过")
                        continue
                    doi_val = (item.get('doi') or '').strip()
                    if not doi_val:
                        print(f"  ⚠️  第 {idx} 项缺 'doi'，跳过")
                        continue
                    entry = {'doi': doi_val}
                    if item.get('link'):
                        entry['link'] = str(item['link']).strip()
                    if item.get('pdf_link'):
                        entry['pdf_link'] = str(item['pdf_link']).strip()
                    header = item.get('header')
                    if isinstance(header, dict) and header:
                        entry['header'] = header
                    # 顶层 "referer" 优先，其次 header 里的 referer（键名大小写
                    # 不敏感）。两者都没有就不设：没有可点的来路页，最后那一层
                    # 本来就无从谈起，不去猜一个（比如拿 "link" 顶替 —— pdf-only
                    # 模式下 link 不一定给，给了也不一定是本文的文章页）。
                    referer_val = str(item.get('referer') or '').strip()
                    if not referer_val and isinstance(header, dict):
                        for _k, _v in header.items():
                            if str(_k).lower() == 'referer' and _v:
                                referer_val = str(_v).strip()
                                break
                    if referer_val:
                        entry['referer'] = referer_val
                    articles.append(entry)
                print(f"📌 从 JSON 读取 {len(articles)} 个 article: {args.json}\n")
            except FileNotFoundError:
                print(f"❌ 文件不存在: {args.json}")
                sys.exit(1)
            except _json.JSONDecodeError as e:
                print(f"❌ JSON 解析失败: {e}")
                sys.exit(1)
            except Exception as e:
                print(f"❌ 读取 JSON 时出错: {e}")
                sys.exit(1)

        if not articles:
            print("❌ 没有有效的 article")
            sys.exit(1)
        # 兼容后续打印/统计仍以 dois 变量命名
        dois = [a['doi'] for a in articles]

        # 处理force_headed参数
        force_headed_mode = args.force_headed
        if force_headed_mode:
            print(f"🔧 强制有头浏览器模式启用 - 将跳过无头浏览器预检\n")
        if args.refresh_headless_auth:
            print(f"🔄 将刷新无头浏览器登录态缓存: {HEADLESS_AUTH_STATE_FILE}\n")

        # 处理输出路径
        output_dir = str(Path(args.output).expanduser().resolve())
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        print(f"📁 输出目录: {output_path}\n")

        # 处理多个DOI。一个批次共享同一有头 Chrome、无头 context 和 cookies。
        success_count = 0
        fail_count = 0
        global _active_browser_session
        async with async_playwright() as batch_playwright:
            browser_session = SharedBrowserSession(batch_playwright)
            _active_browser_session = browser_session
            try:
                for i, article in enumerate(articles, 1):
                    doi = article['doi']
                    print(f"\n{'='*80}")
                    print(f"处理论文 {i}/{len(articles)}: {doi}")
                    if article.get('link'):
                        print(f"  ↪ 使用 link: {article['link']}")
                    if article.get('pdf_link'):
                        print(f"  ↪ 使用 pdf_link（跳过预检与 doi.org）: "
                              f"{article['pdf_link']}")
                    if article.get('header'):
                        print(f"  ↪ 附加 header keys: {list(article['header'].keys())}")
                    print(f"{'='*80}\n")

                    try:
                        md_path = await complete_extraction_workflow(
                            doi,
                            output_file=output_dir,
                            force_headed=force_headed_mode,
                            refresh_headless_auth=args.refresh_headless_auth,
                            browser_session=browser_session,
                            link=article.get('link'),
                            extra_headers=article.get('header'),
                            # 给了 pdf_link 就没有 markdown 可生成，隐含 pdf-only
                            pdf_only=args.pdf_only or bool(article.get('pdf_link')),
                            pdf_link=article.get('pdf_link'),
                            referer=article.get('referer'),
                        )
                        if md_path:
                            success_count += 1
                            print(f"✅ 成功: {md_path}")
                        else:
                            fail_count += 1
                    except Exception as e:
                        print(f"❌ 处理失败: {e}")
                        import traceback
                        traceback.print_exc()
                        fail_count += 1

                    # Retire the browser after every paper. A profile driven
                    # over CDP picks up automation fingerprints as it goes,
                    # until Cloudflare stops letting it through; the next
                    # paper opens a session on a profile rebuilt from scratch.
                    try:
                        browser_session.retire_headed_browser()
                    except Exception as exc:
                        print(f"  ⚠️  关闭抓取浏览器失败: {exc}")

                    # 批量处理防拉黑：随机睡眠 (最后一条不需要)
                    if BATCH_SLEEP_ENABLED and i < len(dois):
                        sleep_seconds = random.randint(BATCH_SLEEP_MIN, BATCH_SLEEP_MAX)
                        sleep_minutes = sleep_seconds / 60
                        print(f"\n😴 防拉黑休眠 {sleep_seconds}s ({sleep_minutes:.1f} min)...")
                        await asyncio.sleep(sleep_seconds)
                        print("🚀 继续下一篇文章...\n")
            finally:
                await browser_session.close()
                _active_browser_session = None

        # 显示统计信息
        if len(dois) > 1:
            print(f"\n{'='*80}")
            print(f"📊 处理完成")
            print(f"{'='*80}")
            print(f"✓ 成功: {success_count}/{len(dois)}")
            print(f"✗ 失败: {fail_count}/{len(dois)}")

        sys.exit(0 if fail_count == 0 else 1)

    except KeyboardInterrupt:
        print("\n\n⚠️  收到中断信号 (Ctrl+C)，正在清理...")
        _cleanup_chrome_launcher()
        sys.exit(130)
    finally:
        # 确保在任何情况下都清理子进程
        _cleanup_chrome_launcher()



if __name__ == "__main__":
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  收到中断信号 (Ctrl+C)，正在清理...")
        _cleanup_chrome_launcher()
        sys.exit(130)

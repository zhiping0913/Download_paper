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
import sys
import signal
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse
from playwright.async_api import async_playwright
from chrome_launcher import launch_chrome
try:
    from cf_bypass_cdp import bypass_cloudflare_cdp, has_cf_clearance_cdp
    _CF_BYPASS_AVAILABLE = True
except ImportError:
    _CF_BYPASS_AVAILABLE = False

# 导入核心模块 (Phase 2 refactoring)
from core import (
    fetch_crossref,
    fetch_semanticscholar,
    organize_paper_output,
    save_metadata_json,
    block_mathjax,
)
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
#   DP_PDF_WAIT                sleep after PDF navigation (browser tab
#                              needs time to trigger the download event)
#   DP_SUPPLEMENTAL_TIMEOUT    supplemental download navigation +
#                              download-event wait
#   DP_FIGURE_TIMEOUT          figure navigation (both primary and
#                              fallback img re-fetch)
#
# Missing / unparseable env vars fall through to the hardcoded defaults
# that were in place before this refactor.

def _env_seconds(name: str, default: float) -> float:
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


# Page-load family — covers the initial article navigation (headed + headless),
# every intermediate wait_for_load_state('networkidle'), and the direct
# APIRequestContext GET used for asset fetches. Default: 60 s.
DP_PAGE_LOAD_TIMEOUT = _env_seconds('DP_PAGE_LOAD_TIMEOUT', 120)

# Cloudflare Turnstile family — total budget once a widget is seen.
# The initial-poll window (how long to wait for a widget to APPEAR) is
# a fixed fraction of this so no-challenge pages exit fast.
DP_CLOUDFLARE_TIMEOUT = _env_seconds('DP_CLOUDFLARE_TIMEOUT', 600)
DP_CLOUDFLARE_INITIAL_POLL = max(2.0, DP_CLOUDFLARE_TIMEOUT / 7.5)  # ~4 s at default

# PDF post-navigation wait — the browser tab needs some time after
# goto(pdf_url) to fire the download event. Default: 10 s.
DP_PDF_WAIT = _env_seconds('DP_PDF_WAIT', 10)

# PDF download hard cap — 判据为「下载事件」的等待上限。
# 分享 Chrome 被 Playwright(accept_downloads=True) 接管后，文件落入 playwright-artifacts
# 临时目录而非 /root/Downloads，故不能再靠“目录新文件”判通过；
# 改为监听 Playwright download 事件。部分网站下载慢，故单独可配。默认 30 s。
# 超时则如实判定 PDF 下载失败（不误判、不无限等待、不硬点挑战框）。
DP_PDF_DOWNLOAD_TIMEOUT = _env_seconds('DP_PDF_DOWNLOAD_TIMEOUT', 30)

# PDF 下载「完成」等待 — 慢网速专用。
# 与 DP_PDF_DOWNLOAD_TIMEOUT（判“是否开始了下载”）分离：
# 一旦 download 事件已触发（下载真实开始），就只等它完成，不因慢而重开页面/retry。
# 默认 180 s，可通过 DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT 覆盖。
DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT = _env_seconds('DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT', 180)

# Supplemental download family — both the initial page.goto(url) and the
# download-event wait for each supplemental link. Also covers inline-audio
# body-fetch waits. Default: 60 s.
DP_SUPPLEMENTAL_TIMEOUT = _env_seconds('DP_SUPPLEMENTAL_TIMEOUT', 60)

# Supplemental download completion wait — after the download event fires
# (file transfer in progress), how long to wait for download.path() to
# resolve before giving up. Slow networks may need 10+ minutes for large
# DOCX/MP4 files. Default: 600 s (10 min).
DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT = _env_seconds('DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT', 600)

# Figure download family — the CDN goto for each figure image (and the
# fallback img_src re-fetch if the first response wasn't image/*).
# Default: 60 s.
DP_FIGURE_TIMEOUT = _env_seconds('DP_FIGURE_TIMEOUT', 60)



# Publisher IDs that can be entered from the Phase 0 headless page.
# Phase 0 substring matches against the Crossref `publisher` field, so each
# entry must appear *inside* the publisher's display name. Crossref returns
# OUP papers under either "Oxford University Press (OUP)" or just
# "Oxford University Press", so we keep both 'oup' and 'oxford' here to
# catch both forms. The URL/DOI detector still returns the canonical
# 'oup' handler name.
HEADLESS_ACCESSIBLE_PUBLISHERS = ['nature', 'aip', 'cambridge', 'springer', 'springer_book', 'oup', 'oup_book', 'oxford', 'pleiades']

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
    if url:
        url_lower = url.lower()
        challenge_domains = [
            'validate.perfdrive.com',
            'distilnetworks.com',
            'distilidentify.com',
            'captcha',
            'challenge',
            'accessdenied',
            'blocked',
        ]
        for pattern in challenge_domains:
            if pattern in url_lower:
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
        目的：cf_bypass_cdp 过 Cloudflare 之前，避免 Playwright 注入自动化指纹。"""
        if self._check_cdp_port():
            print("  ✓ Chrome 已在运行 (CDP 端口就绪)")
            return True
        print("  启动独立 Chrome...")
        try:
            proc = launch_chrome(use_user_config=True)
            self.headed_process = proc
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
        注意：必须在 cf_bypass_cdp 之后调用，否则 Playwright 指纹会导致 Cloudflare 403。"""
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
        # 解决方案：先用 chrome_launcher 启动独立 Chrome，
        # 再用纯 CDP WebSocket 过 Cloudflare 挑战（cf_bypass_cdp），
        # 最后 Playwright 才 connect_over_cdp 接棒抓论文。
        if self.headed_browser is not None and self.headed_browser.is_connected():
            return True
        if not await self.launch_headed_chrome():
            return False
        return await self.connect_headed_browser()

    async def ensure_headless_context(self, storage_state=None):
        if self.headless_context is not None:
            return self.headless_context
        self.headless_browser = await self.playwright.chromium.launch(headless=True)
        kwargs = {"accept_downloads": True}
        state = self.latest_headed_state or storage_state
        if state:
            kwargs["storage_state"] = state
        self.headless_context = await self.headless_browser.new_context(**kwargs)
        cookie_count = len((state or {}).get("cookies", []))
        print(f"  ↔ 共享无头context已创建，载入 {cookie_count} 个cookies")
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
            await self.headed_context.add_init_script(_stealth_js)
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
            await self.headed_context.add_init_script(_stealth_js)
        # Apply undetected-playwright stealth patches for Cloudflare evasion
        try:
            from undetected_playwright import stealth_async
            self.headed_context = await stealth_async(self.headed_context)
            print("  🥷 undetected-playwright stealth 已应用")
        except ImportError:
            print("  ⚠️  undetected-playwright 未安装，使用基础 stealth")
        except Exception as e:
            print(f"  ⚠️  undetected-playwright 应用失败: {e}")

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

        if basename and Path(basename).suffix.lower() in IMAGE_EXTENSIONS:
            if len(basename) > 180:
                suffix = Path(basename).suffix
                basename = f"{Path(basename).stem[:180 - len(suffix)]}{suffix}"
            return basename

    return f"figure_{fig_num}{default_ext}"


# ============================================================================
# Phase 4: Unified Download Manager with Retry Logic
# ============================================================================

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


async def retry_download(download_func, *args, max_retries=5, retry_delay=1.0, **kwargs):
    """Retry a download function with automatic retries on network failures."""
    for attempt in range(max_retries):
        try:
            result = await download_func(*args, **kwargs)
            if result is not None:
                if attempt > 0:
                    print(f"    ✓ 重试成功 (第 {attempt + 1} 次尝试)")
                return result
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"    ⚠️  下载失败，{retry_delay}秒后重试... (尝试 {attempt + 1}/{max_retries})")
                await asyncio.sleep(retry_delay)
            else:
                print(f"    ❌ 已达最大重试次数 ({max_retries}): {str(e)[:100]}")
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
) -> dict:
    """Unified download manager for all resources (PDF, figures, supplemental)

    Args:
        page: Playwright page instance
        links: dict with 'pdf_url', 'figure_urls', 'supplemental_urls'
        output_dir: Output directory for downloads
        context: Playwright browser context (for new tabs)
        metadata: Paper metadata (for supplemental material naming)
        doi: DOI for constructing PDF URL if pdf_url not provided

    Returns:
        dict with 'pdf', 'figures', 'supplemental' keys
    """
    downloads = {
        'pdf': None,
        'figures': {},
        'supplemental': []
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

                pdf_result = await retry_download(
                    download_pdf,
                    download_page, pdf_url, output_dir, pdf_filename, download_context, force_headed,
                    max_retries=5, retry_delay=1.0
                )
                downloads['pdf'] = pdf_result
            except Exception as e:
                print(f"⚠️  PDF下载失败: {e}")

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
                        max_retries=5, retry_delay=1.0
                    )
                    # Fall back to the original (lower-res) URL if the high-res fetch failed
                    if not img_filename and fallback_url and fallback_url != fig_url:
                        print(f"  ↪️  Figure {fig_num}: 高清链接失败，回退到原始链接")
                        img_filename = await retry_download(
                            download_figure,
                            download_page, fallback_url, int(fig_num), output_dir, download_context, force_headed,
                            max_retries=3, retry_delay=1.0
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


async def download_pdf(
    page,
    pdf_url: str,
    output_dir: Path,
    filename: str = "paper.pdf",
    context=None,
    force_headed: bool = False,
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

        # ── AIP/Cloudflare PDF：先用纯 CDP 预载+下载，避免 Playwright 指纹触发高难度挑战 ──
        # CDP 模式下浏览器自己下载到默认目录；若成功可直接落盘，Playwright 只需兜底。
        cdp_downloaded_path: str = None
        if pdf_url and ('pubs.aip.org' in pdf_url or 'aip.org' in pdf_url):
            try:
                from cf_bypass_cdp import bypass_cloudflare_cdp
                _chrome_download_dir = '/root/Downloads'
                print(f"  🛡️  AIP PDF 先用纯 CDP 过 Cloudflare（监控 {_chrome_download_dir}）...")
                _cf_result = await bypass_cloudflare_cdp(
                    url=pdf_url,
                    debug_port=9222,
                    timeout_s=int(DP_CLOUDFLARE_TIMEOUT),
                    pdf_mode=True,
                    download_dir=_chrome_download_dir,
                )
                if _cf_result.get('success'):
                    _dl_file = _cf_result.get('downloaded_file')
                    if _dl_file and os.path.isfile(_dl_file):
                        cdp_downloaded_path = _dl_file
                        print(f"  ✓ CDP 已触发下载: {cdp_downloaded_path}")
                    else:
                        print(f"  ✓ CDP Cloudflare 已通过，但未检测到下载文件，回退 Playwright 导航")
                else:
                    print(f"  ⚠️  CDP 模式未通过，回退 Playwright 导航")
            except Exception as _e:
                print(f"  ⚠️  CDP 绕过 PDF 异常: {_e}，回退 Playwright 导航")

        # 若 CDP 已直接拿到下载文件（可能是 .crdownload），等其落盘后复制到目标目录
        if cdp_downloaded_path:
            _final_pdf = output_dir / filename
            _waited = 0
            _src = cdp_downloaded_path
            _base, _ext = os.path.splitext(_src)
            # 若 Chrome 还没下载完，文件名以 .crdownload 结尾；循环等它完成
            if _src.endswith('.crdownload'):
                print(f"  ⏳ 等待 CDP 下载完成（源文件仍为 .crdownload）...")
                while _src.endswith('.crdownload') and _waited < DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT:
                    await asyncio.sleep(2)
                    _waited += 2
                    # Chrome 完成后会把 .crdownload 重命名为原扩展名
                    if os.path.isfile(_base):
                        _src = _base
                        break
                    if not os.path.isfile(_src):
                        # 文件消失且没出现目标文件：下载失败
                        break
                if _src.endswith('.crdownload'):
                    print(f"    ⏰  CDP 下载文件在 {DP_PDF_DOWNLOAD_COMPLETE_TIMEOUT}s 内未完成")
            if os.path.isfile(_src) and not _src.endswith('.crdownload'):
                shutil.copy(str(_src), str(_final_pdf))
                _size_mb = _final_pdf.stat().st_size / (1024 * 1024)
                print(f"    ✓ 保存: {filename} ({_size_mb:.2f} MB) [CDP 直接下载]")
                if _src.startswith('/root/Downloads') or _src.startswith('/tmp/'):
                    try:
                        os.remove(_src)
                    except Exception:
                        pass
                return filename
            else:
                print(f"    ⚠️  CDP 下载文件异常，回退 Playwright 导航")

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

        async def _ctx_handle_download(download):
            """context 层下载事件回调 —— 事件一触发即视为下载已开始"""
            _dl_started.set()  # 第一时间标记已开始，不等 path()（path 可能因慢网速阻塞）
            try:
                pdf_path_temp = await download.path()
                if not pdf_path_temp:
                    print(f"    ⚠️  download.path() 为空（下载可能仍在进行）")
                    return
                final_path = output_dir / filename
                import shutil
                shutil.copy(str(pdf_path_temp), str(final_path))
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
        # 实证：download 事件派发到「触发下载的那个 page」层（而非 context/browser 层），
        # 故在本次将导航的 page 上也注册监听（双保险），确保能捕获下载开始信号。
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
        else:
            print(f"    ⚠️  未成功下载PDF")
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
        max_retries_supp = 5
        retry_delay_supp = 1.0

        for retry_attempt in range(max_retries_supp):
            success = False
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
                MAX_STEM_BYTES = 200  # leave headroom for suffix + dedup suffix
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
                            body = await api_response.body()
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
                                try:
                                    body = await resp.body()
                                    if body:
                                        _audio_body = body
                                except Exception:
                                    pass
                            _audio_done.set()

                    download_page.on('response', _on_audio_response)

                # 设置下载事件处理
                downloaded_file = None

                async def on_download(download):
                    nonlocal downloaded_file
                    # 获取下载路径（默认是临时目录）
                    downloaded_file = await download.path()

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
                            try:
                                downloaded_file = await asyncio.wait_for(
                                    download_event.path(),
                                    timeout=float(DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT)
                                )
                            except asyncio.TimeoutError:
                                print(f"    ⏰  补充材料下载未在 {DP_SUPPLEMENTAL_DOWNLOAD_COMPLETE_TIMEOUT}s 内完成")
                except asyncio.TimeoutError:
                    # 如果等待超时，继续使用response方法
                    pass
                except Exception:
                    pass

                await asyncio.sleep(1)

                # 如果捕获到下载，复制文件
                if downloaded_file and Path(downloaded_file).exists():
                    try:
                        file_size = Path(downloaded_file).stat().st_size
                        if file_size > 0:
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
                            body = await response.body()
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
    b64 = await page.evaluate("""
        async (url) => {
            const resp = await fetch(url, {credentials: 'include'});
            if (!resp.ok) return null;
            const buf = await resp.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let binary = '';
            for (let i = 0; i < bytes.byteLength; i++) {
                binary += String.fromCharCode(bytes[i]);
            }
            return btoa(binary);
        }
    """, url)
    if b64 is None:
        return None
    return base64.b64decode(b64)


async def download_figure(page, fig_url: str, fig_num: int, output_dir: Path, context=None, force_headed: bool = False) -> str:
    """下载高分辨率图片 - 使用API响应中的URL"""
    try:
        if not fig_url:
            return None

        fig_url = normalize_image_url(fig_url)

        print(f"  📥 下载 Figure {fig_num}: {fig_url}")

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
                image_data = await response.body()
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
                    image_data = await response.body()
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

    return None


# ============================================================================
# 第5部分：主工作流
# ============================================================================

async def complete_extraction_workflow(
    doi: str,
    output_file: str = None,
    force_headed: bool = False,
    refresh_headless_auth: bool = False,
    browser_session: SharedBrowserSession = None,
    link: str = None,
    extra_headers: dict = None,
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

        # Record the browser's landed URL — after doi.org redirect this is
        # the direct publisher URL. save_metadata_json will surface it as
        # "link" so future runs can bypass doi.org via the --json input.
        try:
            landing_url = (page.url or '') if page is not None else ''
        except Exception:
            landing_url = ''
        if landing_url and not landing_url.startswith('about:'):
            metadata['_landing_url'] = landing_url

        # Save HTML to the per-DOI capture directory.
        # page.html     = post-JS rendered DOM (fulltext_data from handler)
        # page_raw.html = raw server HTTP response (pre-JS, captured by interceptor)
        if isinstance(fulltext_data, str) and fulltext_data:
            html_file = captured_data_dir / "page.html"
            with open(html_file, 'w', encoding='utf-8') as f:
                f.write(fulltext_data)
            print(f"  ✓ HTML已保存: {html_file}")

        raw_server_html = getattr(handler, '_raw_server_html', None)
        if raw_server_html:
            raw_html_file = captured_data_dir / "page_raw.html"
            with open(raw_html_file, 'w', encoding='utf-8') as f:
                f.write(raw_server_html)
            print(f"  ✓ 原始HTML已保存: {raw_html_file}")

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
                         downloads['pdf'], downloads['supplemental'])

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
        chrome_launcher = Path(__file__).parent / "chrome_launcher.py"
        if not chrome_launcher.exists():
            print("⚠️  chrome_launcher.py 未找到\n")
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

    # ========== 第1步判断：根据Crossref publisher决定是否需要Phase 0 ==========
    should_use_headless_phase0 = False
    if not force_headed:
        crossref_publisher = crossref_data.get('publisher', '').lower()
        if crossref_publisher:
            # Check if Crossref publisher contains any HEADLESS_ACCESSIBLE_PUBLISHERS.
            # Use word-boundary matching so short keys like 'oup' don't match
            # inside unrelated words like 'group' (e.g. "Optica Publishing Group").
            for publisher_name in HEADLESS_ACCESSIBLE_PUBLISHERS:
                pattern = r'\b' + re.escape(publisher_name.lower()) + r'\b'
                if re.search(pattern, crossref_publisher):
                    should_use_headless_phase0 = True
                    print(f"✓ 根据Crossref publisher '{crossref_publisher}' 判断出版商为 {publisher_name.upper()}")
                    print(f"  → 将使用Phase 0进行无头浏览器预检\n")
                    break

        if not should_use_headless_phase0:
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
                if browser_session is not None:
                    headless_browser = None
                    headless_context = await browser_session.ensure_headless_context(storage_state)
                else:
                    context_kwargs = {'accept_downloads': True}
                    if storage_state:
                        context_kwargs['storage_state'] = storage_state
                    headless_browser = await p.chromium.launch(headless=True)
                    headless_context = await headless_browser.new_context(**context_kwargs)
                headless_page = await headless_context.new_page()

                # Stop MathJax from running so we keep original \(...\) / <math>
                # markup in the DOM. Must be registered before the first goto().
                await block_mathjax(headless_page)

                # Attach any JSON-supplied headers (e.g. Referer). Cookies
                # continue to be carried by the shared context.
                if extra_headers:
                    try:
                        await headless_page.set_extra_http_headers(
                            {str(k): str(v) for k, v in extra_headers.items()}
                        )
                        print(f"  ↪ 附加 header(s): {list(extra_headers.keys())}")
                    except Exception as e:
                        print(f"  ⚠️  set_extra_http_headers 失败: {e}")

                # Capture raw server HTML (pre-JavaScript) via response interception.
                _headless_raw_html: list = []

                async def _headless_on_response(response):
                    try:
                        if (response.request.resource_type == 'document'
                                and response.ok
                                and 'text/html' in response.headers.get('content-type', '')):
                            _headless_raw_html.append(await response.text())
                    except Exception:
                        pass

                headless_page.on('response', _headless_on_response)

                try:
                    last_precheck_error = None
                    for precheck_url in build_headless_precheck_urls():
                        print(f"  ↪ 预检访问: {precheck_url}")
                        try:
                            await headless_page.goto(precheck_url, wait_until='domcontentloaded', timeout=int(DP_PAGE_LOAD_TIMEOUT * 1000))
                            try:
                                await headless_page.wait_for_load_state('networkidle', timeout=int(DP_PAGE_LOAD_TIMEOUT * 1000))
                            except:
                                print("  ℹ️  页面主文档已加载，后台资源未完全静默，继续预检")
                            last_precheck_error = None
                            break
                        except Exception as e:
                            last_precheck_error = e
                            print(f"  ⚠️  预检访问失败: {type(e).__name__}: {str(e)[:100]}")

                    if last_precheck_error is not None:
                        raise last_precheck_error

                    # 保存无头浏览器访问结果
                    # page_raw.html / headless_initial.html = 原始HTTP响应（JS运行前）
                    # page.html = 渲染后DOM（handler稍后通过process_with_handler覆盖写入）
                    headless_raw_html = _headless_raw_html[-1] if _headless_raw_html else None
                    headless_rendered_html = await headless_page.content()
                    headless_html = headless_rendered_html  # used for bot-detection below

                    headless_html_file = captured_data_dir / "headless_initial.html"
                    page_html_file = captured_data_dir / "page.html"
                    # Always write the rendered DOM to page.html (consistent with headed path)
                    with open(page_html_file, 'w', encoding='utf-8') as f:
                        f.write(headless_rendered_html)
                    # Write raw server response separately when available
                    if headless_raw_html:
                        with open(headless_html_file, 'w', encoding='utf-8') as f:
                            f.write(headless_raw_html)
                        raw_html_file = captured_data_dir / "page_raw.html"
                        with open(raw_html_file, 'w', encoding='utf-8') as f:
                            f.write(headless_raw_html)
                        print(f"  ✓ 原始HTML已保存: {headless_html_file.name} ({len(headless_raw_html)} 字节)")
                    else:
                        with open(headless_html_file, 'w', encoding='utf-8') as f:
                            f.write(headless_rendered_html)
                        print(f"  ✓ 页面已保存: {headless_html_file.name} ({len(headless_rendered_html)} 字节)")

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
                        if headless_raw_html:
                            handler._raw_server_html = headless_raw_html

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
            _cf_pre_result = await bypass_cloudflare_cdp(
                url=url,
                debug_port=CHROME_DEBUG_PORT,
                timeout_s=DP_CLOUDFLARE_TIMEOUT,
                wait_for_content=True,
                expected_doi=doi,
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
            print("   请运行: python chrome_launcher.py\n")
            return None
        browser, context = connection
        print("✓ 已连接到批次共享Chrome\n" if browser_session else "✓ 已连接到Chrome\n")
        try:
            print("✓ 使用批次共享context\n" if browser_session else "✓ 使用现有context\n")

            if browser_session is not None:
                await browser_session.sync_headless_to_headed(context)

            page = await context.new_page()

            # Attach any JSON-supplied headers (e.g. Referer) to the headed
            # page BEFORE we register the response listener + navigate.
            # Only applied to this page — the shared context's own headers
            # / cookies are untouched.
            if extra_headers:
                try:
                    await page.set_extra_http_headers(
                        {str(k): str(v) for k, v in extra_headers.items()}
                    )
                    print(f"  ↪ 附加 header(s): {list(extra_headers.keys())}")
                except Exception as e:
                    print(f"  ⚠️  set_extra_http_headers 失败: {e}")

            # Stop MathJax from running so the rendered DOM (page.content())
            # also keeps original \(...\) / <math> markup. Must be registered
            # before the first goto().
            await block_mathjax(page)

            # Intercept the main-document HTTP response to capture the raw server
            # HTML *before* JavaScript (e.g. MathJax) rewrites the DOM.
            _headed_raw_html: list = []

            async def _headed_on_response(response):
                try:
                    if (response.request.resource_type == 'document'
                            and response.ok
                            and 'text/html' in response.headers.get('content-type', '')):
                        _headed_raw_html.append(await response.text())
                except Exception:
                    pass

            page.on('response', _headed_on_response)

            # ── 纯 CDP 过 Cloudflare 挑战 + 预加载页面 ──
            # 如果预载阶段（Playwright 连接前）已经成功过了挑战，直接复用页面。
            # 否则用 Playwright 连接后的 CDP 再试一次（作为 fallback）。
            _cf_loaded = False  # 纯CDP是否已成功加载页面
            _cf_raw_html = None  # 纯CDP获取的原始HTML

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
                if _cf_page_obj is not None:
                    print(f"  ✓ 找到预载页面，直接复用")
                    try:
                        await page.close()
                    except Exception:
                        pass
                    page = _cf_page_obj
                    try:
                        _cf_raw_html = await page.content()
                        _headed_raw_html.append(_cf_raw_html)
                        print(f"  ✓ 已捕获原始 HTML ({len(_cf_raw_html)} bytes)")
                    except Exception as _e2:
                        print(f"  ⚠️  获取 raw HTML 失败: {_e2}")
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
                            # 关闭原来的 page（纯CDP开了新tab，原page没用了）
                            try:
                                await page.close()
                            except Exception:
                                pass
                            page = _cf_page_obj
                            # 用 CDP 获取原始 HTML（作为 _headed_raw_html 的替代）
                            try:
                                _cf_raw_html = await page.content()
                                _headed_raw_html.append(_cf_raw_html)
                                print(f"  ✓ 已捕获原始 HTML ({len(_cf_raw_html)} bytes)")
                            except Exception as _e2:
                                print(f"  ⚠️  获取 raw HTML 失败: {_e2}")
                            _cf_loaded = True
                        else:
                            print(f"  ⚠️  未找到对应 page，将用 Playwright 重新导航")
                    else:
                        print(f"  ⚠️  纯CDP挑战未通过，仍将尝试Playwright路径")
                except Exception as _e:
                    print(f"  ⚠️  纯CDP挑战模块异常: {_e}")
            else:
                print("  ℹ️  cf_bypass_cdp 模块不可用，跳过纯CDP预检查")

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

            if _cf_loaded:
                # 纯 CDP 已加载页面，跳过 goto 和 Cloudflare 处理
                print("  ✓ 页面已由纯CDP预加载，跳过 goto")
            else:
                print("DEBUG: about to page.goto")
                try:
                    resp = await page.goto(url, wait_until='networkidle', timeout=int(DP_PAGE_LOAD_TIMEOUT * 1000))
                    print(f"DEBUG: page.goto done, status={resp.status if resp else None}, url={page.url[:80]}")
                except Exception as e:
                    print(f"DEBUG: page.goto exception: {e}")

                # If the landing page is a Cloudflare Turnstile "verify you are
                # human" checkbox, try to click through it automatically.
                try:
                    cf_solved = await auto_solve_bot_challenge(page, timeout_s=DP_CLOUDFLARE_TIMEOUT, initial_poll_s=DP_CLOUDFLARE_INITIAL_POLL)
                    # If Cloudflare was solved and the page navigated to the real
                    # content, the on-response listener already captured the new
                    # document HTML — but if the challenge was JS-only (same URL
                    # returning 403 then 200 after cookie is set), we need to
                    # reload to get the real content into _headed_raw_html.
                    if cf_solved:
                        # Always reload after challenge resolution to ensure we
                        # capture the real article HTML.
                        print("  🔄 挑战已通过，重新加载页面以获取论文内容...")
                        try:
                            await page.goto(page.url, wait_until='networkidle', timeout=int(DP_PAGE_LOAD_TIMEOUT * 1000))
                            print(f"     ✓ 重新加载完成, status check: {len(_headed_raw_html)} document(s) captured")
                        except Exception as e:
                            print(f"     ⚠️  重新加载异常: {e}")
                except Exception as e:
                    print(f"  ⚠️  auto_solve_bot_challenge 抛异常: {e}")

            # Store the raw server HTML on the handler so it can use it instead
            # of page.content() (which returns the post-JS-rendered DOM).
            _headed_raw = _headed_raw_html[-1] if _headed_raw_html else None

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

            if _headed_raw:
                handler._raw_server_html = _headed_raw

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

JSON 格式:
  {
    "article": [
      {
        "doi": "10.1063/1.4994562",                                    # 必填
        "link": "https://pubs.aip.org/aip/pop/article/24/12/...",       # 可选：绕过 doi.org 重定向
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
            help='含 article 列表的 JSON 文件 (每篇必须有 "doi"，可选 "link" 和 '
                 '"header" 字典)。见 examples/examples.json 的格式。'
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
                    header = item.get('header')
                    if isinstance(header, dict) and header:
                        entry['header'] = header
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

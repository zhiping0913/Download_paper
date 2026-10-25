#!/usr/bin/env python3
"""Sci-Hub as one more place a PDF can come from (pdf-only mode).

Sci-Hub has no article page worth extracting -- it serves somebody else's PDF
inside a one-element viewer -- so this path only ever produces a PDF, and the
bibliographic record comes from Crossref. That is why ``--scihub`` implies
``--pdf-only``: there is nothing for the Markdown pipeline to read.

Two steps, and only the first one lives here:

1. ask a mirror for ``{mirror}/{doi}`` and read the PDF address out of the
   viewer it answers with (that is all this module does), and
2. hand that address to ``_pdf_link_direct_download()``, the same function
   ``--json``'s ``pdf_link`` already uses.

⚠️ The page is fetched **through a browser** (the ladder's throwaway-Chrome
rung), like every other publisher page in this repo: the mirrors sit behind
Cloudflare, and a bare request mostly earns an interstitial.
"""

import os
import re
from typing import Optional, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

import asyncio

import websockets

# ``_send`` is the one place in chrome_session that reads the CDP socket, so
# driving a tab means going through it rather than writing a second reader.
from chrome_session import _send as _cdp_send
from core.utilities import html_looks_like_bot_challenge

# Mirrors come and go; this is a starting order, not a promise. Override the
# whole list with DP_SCIHUB_MIRRORS (comma separated, scheme optional).
# .st goes first because .se is unreachable from here (user report, measured):
# an unreachable mirror still costs a Chrome launch plus the challenge budget
# before it can report nothing.
DEFAULT_SCIHUB_MIRRORS = (
    'https://sci-hub.st',
    'https://sci-hub.se',
    'https://sci-hub.ru',
)


def scihub_mirrors() -> tuple:
    raw = (os.environ.get('DP_SCIHUB_MIRRORS') or '').strip()
    if not raw:
        return DEFAULT_SCIHUB_MIRRORS
    out = []
    for item in raw.split(','):
        item = item.strip().rstrip('/')
        if not item:
            continue
        if '://' not in item:
            item = 'https://' + item
        out.append(item)
    return tuple(out) or DEFAULT_SCIHUB_MIRRORS


def _absolutise(src: str, base: str) -> str:
    """Make *src* fetchable: Sci-Hub writes it three different ways."""
    src = (src or '').strip()
    if not src:
        return ''
    # The viewer appends PDF.js view options; they are not part of the address.
    src = src.split('#', 1)[0]
    if src.startswith('//'):
        # Protocol-relative -- inherit the mirror's scheme, not the page's base
        return (urlsplit(base).scheme or 'https') + ':' + src
    # ⚠️ Plain urljoin, and nothing hand-rolled around it. The first version
    # lstrip()ed the leading slash and joined against ``base + '/'``, which
    # turns the root-relative ``/storage/…/einstein1935.pdf`` into
    # ``sci-hub.st/10.1103/PhysRev.47.777/storage/…`` -- a URL that exists
    # nowhere. Root-relative and path-relative are two different things and
    # urljoin already knows the difference.
    return urljoin(base, src)


def _looks_like_pdf_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith('.pdf') or '/pdf' in path or 'download=true' in url.lower()


def parse_scihub_page(html: str, base_url: str) -> Tuple[str, str]:
    """Pull (pdf_url, citation) out of a Sci-Hub article page.

    Pure text in, nothing fetched -- so this is the part that can be tested
    offline against a saved page.

    Where the address shows up, strongest evidence first:
    ``<meta name="citation_pdf_url">`` and the download icon's anchor (both
    present in the current layout, both server-rendered), then the viewer
    element older mirrors use (``embed#pdf`` / ``iframe#pdf``), then any
    ``location.href='…pdf'`` in an ``onclick``, then a plain ``.pdf`` anchor.

    ⚠️ A ``?download=true`` URL **wins outright** when one exists: the server
    answers it with ``Content-Disposition: attachment``, and that is the
    difference between a browser downloading the file and rendering it in its
    PDF viewer (where no download event ever fires -- the same trap this repo
    hit with inline images and videos). The current sci-hub.st layout has no
    such URL, so normally the meta tag wins.
    """
    soup = BeautifulSoup(html, 'html.parser')

    candidates = []

    # ✅ Strongest of all, and it is in the **server's own response**: the
    # current layout declares <meta name="citation_pdf_url"
    # content="/storage/…/einstein1935.pdf">. No script has to have run, so
    # this is the one source that works even on a half-loaded page.
    meta = soup.find('meta', attrs={'name': 'citation_pdf_url'})
    if meta and meta.get('content'):
        candidates.append(_absolutise(meta['content'], base_url))

    # The download icon: <div class="download"><a href="/storage/…"></a></div>
    # -- an anchor with no text at all, which is why it is matched by class
    # and not by what it says.
    for el in soup.select('.download a[href], a.download[href]'):
        candidates.append(_absolutise(el.get('href'), base_url))

    for sel in ('embed#pdf', 'iframe#pdf', 'embed[type="application/pdf"]',
                'embed[src]', 'iframe[src]'):
        for el in soup.select(sel):
            candidates.append(_absolutise(el.get('src'), base_url))

    # <button onclick="location.href='//zero.sci-hub.se/…/x.pdf?download=true'">
    download_urls = []
    for el in soup.find_all(attrs={'onclick': True}):
        for m in re.finditer(r"""location\.href\s*=\s*['"]([^'"]+)['"]""",
                             el.get('onclick') or ''):
            download_urls.append(_absolutise(m.group(1), base_url))

    for a in soup.find_all('a', href=True):
        href = a['href']
        if '.pdf' in href.lower():
            candidates.append(_absolutise(href, base_url))

    pdf_url = next((u for u in download_urls if _looks_like_pdf_url(u)), '') or \
        next((u for u in candidates if _looks_like_pdf_url(u)), '')

    # Only ever used to fill a gap Crossref left (see complete_scihub_title).
    # ⚠️ Two layouts: the current one declares <meta name="citation_title">
    # (and has no #citation element at all), older ones print the publisher's
    # citation string. The meta tag is a title already, so it is preferred --
    # the other has to be cut out of a sentence.
    citation = ''
    meta_title = soup.find('meta', attrs={'name': 'citation_title'})
    if meta_title and (meta_title.get('content') or '').strip():
        citation = meta_title['content'].strip()
    else:
        cit = soup.find(id='citation')
        if cit:
            citation = re.sub(r'\s+', ' ', cit.get_text(' ', strip=True)).strip()

    return pdf_url, citation


# The interstitial: <div class="ask">Are you a robot?</div> plus a single
# clickable circle <div class="answer" onclick="check()">No</div>. Clicking it
# starts an **Altcha** proof-of-work widget (``/captcha/challenge/<id>``),
# which computes a SHA-256 preimage in the page, POSTs it to
# ``/captcha/solution/<id>`` and reloads on success. No human is needed for
# the work itself -- only for the click, and a click is scriptable.
_ROBOT_PAGE_MARKERS = ('are you are robot', 'are you a robot',
                       'altcha-widget', 'en:isrobot')

_CLICK_NO_JS = """
(() => {
  const el = document.querySelector('.question .answer')
          || document.querySelector('.answer')
          || Array.from(document.querySelectorAll('[onclick]'))
                  .find(e => /check\\s*\\(/.test(e.getAttribute('onclick') || ''));
  if (!el) return 'no-target';
  el.click();
  return 'clicked';
})()
"""


def looks_like_robot_question(html: str) -> bool:
    """Whether this is Sci-Hub's own "Are you a robot?" page.

    ⚠️ Distinct from :func:`html_looks_like_bot_challenge`, which knows
    Cloudflare. This one is Sci-Hub's, it is served by DDoS-Guard-fronted
    mirrors after a few requests, and **it has no Turnstile box** -- so the
    repo's challenge-clicking machinery finds nothing to click and waits out
    its whole budget reporting a challenge that never passes.
    """
    low = (html or '').lower()
    if 'citation_pdf_url' in low:
        return False          # the real article page, whatever else is on it
    return sum(1 for m in _ROBOT_PAGE_MARKERS if m in low) >= 2


async def _answer_robot_question(ws_url: str, *, timeout_s: float = 60.0) -> str:
    """Click "No", wait for the proof of work, and return the reloaded page.

    Returns '' when there was nothing to click or the page never came back --
    the caller then treats this mirror as unusable and tries the next one,
    which is the honest answer: an unsolved interstitial is not an article
    page, and parsing it would report "this paper is not on Sci-Hub".
    """
    if not ws_url:
        print("    ⚠️  拿不到这个 tab 的 CDP 地址，无法点 'No'")
        return ''
    try:
        async with websockets.connect(ws_url, max_size=None,
                                      open_timeout=10) as ws:
            res = await _cdp_send(ws, 'Runtime.evaluate', {
                'expression': _CLICK_NO_JS, 'returnByValue': True})
            verdict = (res.get('result') or {}).get('value')
            if verdict != 'clicked':
                print("    ⚠️  机器人问句页上找不到 'No' 可点（页面改版了？）")
                return ''
            print("    🖱️  点了 'No' —— 等 Altcha 算完工作量证明并自动重载")

            deadline = asyncio.get_event_loop().time() + timeout_s
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(2)
                res = await _cdp_send(ws, 'Runtime.evaluate', {
                    'expression': 'document.documentElement.outerHTML',
                    'returnByValue': True})
                html = (res.get('result') or {}).get('value') or ''
                if html and not looks_like_robot_question(html):
                    print(f"    ✓ 已通过机器人问句（{len(html):,} 字符）")
                    return html
            print(f"    ⏰ 点了 'No' 但 {timeout_s:.0f}s 内没回到文章页")
    except Exception as exc:
        print(f"    ⚠️  点 'No' 失败（{type(exc).__name__}）")
    return ''


# ⚠️ A mirror may hand the article to a **different site**: sci-hub.st
# redirects some DOIs to ``sci-net.xyz/{doi}`` (measured on 10.1086/112164),
# whose page never says "Sci-Hub" anywhere -- it says "Sci-Net". Requiring that
# string made a perfectly good article page be reported as "this mirror has no
# usable page", which looks exactly like "this paper is not on Sci-Hub".
_MIRROR_PAGE_MARKERS = ('sci-hub', 'scihub', 'sci-net', 'scinet')


def _is_scihub_page(html: str) -> bool:
    """A real mirror page (found or not found), not a challenge or a 404."""
    if html_looks_like_bot_challenge(html):
        return False
    low = html.lower()
    return any(m in low for m in _MIRROR_PAGE_MARKERS)


async def _tab_url(ws_url: str, *, port: int = None) -> str:
    """Where the tab actually ended up.

    ⚠️ Load-bearing, not diagnostics: the PDF address on these pages is
    **root-relative** (``/storage/…/x.pdf``), so resolving it against the URL
    we asked for instead of the one we landed on produces
    ``sci-hub.st/storage/…`` for a file that lives on ``sci-net.xyz`` -- a URL
    that exists nowhere, and the download layer can only report "no file".
    """
    # ⚠️ ``ws_url`` is only filled in on the paths the session considers
    # successful, and a Sci-Hub page never passes its test (the DOI is not in
    # the response and the body is ~100 characters) -- so the timeout path,
    # which is the normal one here, hands back ws_url=None while the tab sits
    # there fully loaded. Ask the debug port for the tab instead, exactly as
    # ``fetch_page_html_via_cdp`` does.
    if not ws_url and port:
        from chrome_session import _get_page_ws_url
        try:
            ws_url = await _get_page_ws_url(port) or ''
        except Exception:
            ws_url = ''
    if not ws_url:
        return ''
    try:
        async with websockets.connect(ws_url, max_size=None,
                                      open_timeout=10) as ws:
            res = await _cdp_send(ws, 'Runtime.evaluate', {
                'expression': 'location.href', 'returnByValue': True})
            return (res.get('result') or {}).get('value') or ''
    except Exception:
        return ''


async def _fetch_scihub_html(page_url: str, *, headless: bool,
                             timeout_s: float) -> Tuple[str, str]:
    """Open *page_url* in a throwaway Chrome and return its HTML.

    ⚠️ **Not `fetch_html_via_ladder`**, although that is this repo's normal way
    to fetch a page: the mirrors answer with their own "Are you a robot?"
    interstitial, and getting past it needs a *click* on that page -- the
    ladder only ever hands back markup. So this drives the throwaway Chrome
    (the ladder's own bottom rung) directly and keeps the tab long enough to
    answer the question.

    ``timeout_s`` is deliberately short. The session's own pass test looks for
    the DOI or 5,000+ characters of body text and declares a Cloudflare
    challenge otherwise -- a Sci-Hub page satisfies neither, so waiting the
    full default budget only delays reading a page that has been sitting there
    for seconds.

    Returns ``(html, landed_url)``; the landed URL is what relative addresses
    on the page have to be resolved against (see :func:`_tab_url`).
    """
    from chrome_session import open_url_in_fresh_chrome

    session = None
    try:
        session = await open_url_in_fresh_chrome(
            page_url, timeout_s=int(timeout_s), headless=headless,
            want_html=True)
        result = session.result or {}
        html = result.get('html') or ''
        ws_url = result.get('ws_url') or ''
        if looks_like_robot_question(html):
            print("    🤖 镜像先问「Are you a robot?」—— 需要点那个 'No'")
            html = await _answer_robot_question(ws_url)
        landed = await _tab_url(ws_url, port=getattr(session, 'port', None))
        if landed and landed.split('#', 1)[0] != page_url:
            print(f"    ↪ 落在 {landed[:90]}")
        return html, (landed or page_url)
    except Exception as exc:
        print(f"    ⚠️  打开镜像页失败（{type(exc).__name__}）")
        return '', page_url
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass


async def resolve_scihub_pdf_url(doi: str, *, headless: bool = False,
                                 timeout_s: float = 25.0) -> Tuple[str, str, str]:
    """Ask the mirrors, in order, for *doi*'s PDF address.

    Returns ``(pdf_url, article_page_url, citation)``; ``pdf_url`` is '' when
    no mirror had the paper. The article page URL is returned so the caller can
    use it as the Referer for the download -- the PDF may live on a different
    host than the viewer.

    ⚠️ "The mirror answered", "the mirror wants the robot question answered"
    and "the mirror has no such paper" are three different answers and are
    printed as such. Only the last one is about the paper; the other two are
    about this request, and the next mirror may well not ask.
    """
    mirrors = scihub_mirrors()
    print(f"\n🔓 Sci-Hub 取 PDF（{len(mirrors)} 个镜像）")
    print("=" * 80)

    for mirror in mirrors:
        page_url = f"{mirror.rstrip('/')}/{doi}"
        print(f"  ↪ {page_url}")
        html, landed_url = await _fetch_scihub_html(
            page_url, headless=headless, timeout_s=timeout_s)
        if not html or not _is_scihub_page(html):
            print("    ⊘ 这个镜像没答出可用页面（挡板页/不可达），试下一个")
            continue

        # ⚠️ Resolve against where we landed, not where we asked.
        pdf_url, citation = parse_scihub_page(html, landed_url)
        if not pdf_url:
            # The mirror answered and said no. Keep going anyway -- the mirrors
            # do not share one corpus -- but say which kind of miss this was.
            print("    ⊘ 镜像答复里没有 PDF（这个镜像上没有这篇），试下一个")
            continue

        print(f"    ✓ PDF: {pdf_url}")
        if citation:
            print(f"    ℹ️  Sci-Hub 引用串: {citation[:90]}")
        # The Referer has to be the page the link was on, which after a
        # redirect is the landed URL rather than the mirror URL we asked for.
        return pdf_url, landed_url, citation

    print("  ❌ 所有镜像都没拿到 PDF 地址")
    return '', '', ''


def complete_scihub_title(crossref_data: dict, citation: str) -> Optional[str]:
    """Fill in a title Crossref did not give, from Sci-Hub's citation string.

    Crossref is the record; this is only a gap filler, because the directory
    name is built from the title and a missing one degrades it to
    ``0000--paper``. Returns None when Crossref already had a title (the
    normal case) or when the citation gives nothing usable.

    Sci-Hub's citation reads like
    ``Author, A. (2012). Some title. Journal, 1(2), 3-4. doi:10.…`` -- the
    sentence after the year in parentheses is the title.
    """
    if (crossref_data.get('title') or '').strip():
        return None
    if not citation:
        return None
    m = re.search(r'\(\d{4}[a-z]?\)\.?\s*(.+?)\s*\.\s', citation)
    title = (m.group(1) if m else citation).strip(' .')
    # Everything after the DOI is boilerplate, and a citation with no sentence
    # structure is not a title worth writing into the directory name.
    title = re.split(r'\bdoi:', title, flags=re.I)[0].strip(' .')
    if len(title) < 8:
        return None
    return title

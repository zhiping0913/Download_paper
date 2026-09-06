#!/usr/bin/env python3
"""
Cloudflare 挑战通过 —— 纯 CDP WebSocket 版本
==============================================

Playwright 连接 CDP 后会注入自动化指纹，导致 Cloudflare 挑战进入
最高难度模式。本模块直接用 websocket + CDP 协议打开页面并等待挑战通过。

用法：
    from cf_bypass_cdp import bypass_cloudflare_cdp, has_cf_clearance_cdp

    result = await bypass_cloudflare_cdp(
        url="https://pubs.aip.org/...",
        debug_port=9222,
        timeout_s=600,
    )
    # result["success"] == True 表示挑战已通过
    # result["target_id"] / result["ws_url"] 可用于定位页面
"""

import asyncio
import os
import glob
import json
import urllib.request
from typing import Optional

import websockets


# Cloudflare localises its interstitial by Accept-Language, so the title is
# whatever language the browser asked for. A Chinese-locale Chrome shows
# "请稍候…" where an English one shows "Just a moment…" -- matching only the
# English strings meant the challenge went undetected on this machine, the
# Turnstile auto-click never ran, and the preload just span until timeout.
_CHALLENGE_TITLE_KEYWORDS = (
    # English
    'just a moment', 'verify you are human', 'security verification',
    'attention required', 'performing security verification', 'checking your browser',
    # Chinese (Simplified / Traditional)
    '请稍候', '请稍等', '稍候片刻', '請稍候', '請稍等',
    '正在验证', '正在驗證', '需要注意', '安全验证', '安全驗證',
    # Japanese / Korean
    'お待ちください', '少々お待ち', '잠시만 기다',
    # European
    'einen moment', 'un instant', 'un momento', 'um momento',
    'even geduld', 'подождите', 'один момент',
)


def _is_challenge_title(title_lower: str) -> bool:
    """True if *title_lower* is a Cloudflare interstitial in any locale."""
    if not title_lower:
        return False
    return any(kw in title_lower for kw in _CHALLENGE_TITLE_KEYWORDS)


# Language-independent fallback: the challenge page's own DOM. Any of these
# means Cloudflare is holding the request, whatever the title says.
_CHALLENGE_DOM_JS = r"""(function () {
    try {
        if (document.querySelector(
                '#challenge-form, #challenge-running, #challenge-stage, ' +
                '#cf-challenge-running, [id^="cf-chl"], ' +
                'script[src*="cdn-cgi/challenge-platform"]')) {
            return true;
        }
        return /cdn-cgi\/challenge-platform/.test(document.documentElement.innerHTML)
               && document.body && document.body.innerText.length < 2000;
    } catch (e) {
        return false;
    }
})()"""


async def _send(ws, method: str, params: dict = None) -> dict:
    """发送 CDP 命令并等待结果。跳过事件消息（无 id）。"""
    msg_id = id(object()) % 1000000
    msg = {"id": msg_id, "method": method}
    if params:
        msg["params"] = params
    await ws.send(json.dumps(msg))
    while True:
        raw = await ws.recv()
        try:
            resp = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if "id" not in resp:
            continue  # 事件消息，跳过
        if resp["id"] == msg_id:
            if "error" in resp:
                raise RuntimeError(f"CDP error: {resp['error']}")
            return resp.get("result", {})


async def _get_page_ws_url(debug_port: int = 9222) -> Optional[str]:
    """获取第一个 page target 的 WebSocket 调试 URL"""
    url = f"http://localhost:{debug_port}/json"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            targets = json.loads(resp.read().decode())
        for t in targets:
            if t.get("type") == "page":
                return t.get("webSocketDebuggerUrl")
    except Exception as e:
        print(f"  ⚠️  无法获取 CDP target 列表: {e}")
    return None


async def _find_page_ws_url(debug_port: int = 9222, url_match: str = "") -> Optional[str]:
    """找到 URL 匹配的 page target；找不到就返回第一个 page"""
    url = f"http://localhost:{debug_port}/json"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            targets = json.loads(resp.read().decode())
        for t in targets:
            if t.get("type") == "page" and url_match and url_match in t.get("url", ""):
                return t.get("webSocketDebuggerUrl")
        for t in targets:
            if t.get("type") == "page":
                return t.get("webSocketDebuggerUrl")
    except Exception as e:
        print(f"  ⚠️  无法获取 CDP target 列表: {e}")
    return None


async def _create_new_tab(debug_port: int = 9222, url: str = "about:blank") -> Optional[str]:
    """新建一个 tab，返回其 WebSocket 调试 URL"""
    create_url = f"http://localhost:{debug_port}/json/new?{urllib.request.quote(url)}"
    try:
        req = urllib.request.Request(create_url, method="PUT")
        with urllib.request.urlopen(req, timeout=10) as resp:
            tab = json.loads(resp.read().decode())
        return tab.get("webSocketDebuggerUrl")
    except Exception as e:
        print(f"  ⚠️  新建 tab 失败: {e}")
        return None


_TURNSTILE_IFRAME_SELECTORS = [
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="cloudflare.com/cdn-cgi/challenge-platform"]',
    'iframe[data-sitekey]',
    'iframe[title="Widget containing a Cloudflare security challenge"]',
    'iframe[title="Cloudflare"]',
    'iframe[title*="challenge"]',
    'iframe[title*="security"]',
]


async def _find_turnstile_iframe_cdp(ws) -> dict:
    """用 CDP 在页面中查找 Turnstile challenge iframe。
    返回 {found, selector, index, rect, src}
    """
    js = """
    (() => {
        const selectors = [
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="cloudflare.com/cdn-cgi/challenge-platform"]',
            'iframe[data-sitekey]',
            'iframe[title="Widget containing a Cloudflare security challenge"]',
            'iframe[title="Cloudflare"]',
            'iframe[title*="challenge"]',
            'iframe[title*="security"]',
            'iframe[title*="verification"]',
        ];
        for (const sel of selectors) {
            const els = document.querySelectorAll(sel);
            for (let i = 0; i < els.length; i++) {
                const el = els[i];
                const rect = el.getBoundingClientRect();
                if (rect.width > 1 && rect.height > 1 &&
                    rect.bottom > 0 && rect.right > 0 &&
                    rect.top < window.innerHeight + 200) {
                    return {
                        found: true, selector: sel, index: i,
                        rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height,
                                top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right },
                        src: el.src || '',
                    };
                }
            }
        }
        const allIframes = document.querySelectorAll('iframe');
        for (let i = 0; i < allIframes.length; i++) {
            const el = allIframes[i];
            const src = el.src || '';
            if (src.includes('challenge') || src.includes('cloudflare') || src.includes('turnstile')) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 1 && rect.height > 1) {
                    return { found: true, selector: 'iframe', index: i,
                             rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
                             src: src };
                }
            }
        }
        return { found: false, selector: null, index: -1, rect: null, src: null };
    })()
    """
    result = await _send(ws, "Runtime.evaluate", {
        "expression": js, "returnByValue": True,
    })
    return result.get("result", {}).get("value", {"found": False})


async def _click_at_cdp(ws, x: float, y: float, delay_ms: int = 60):
    """用 CDP Input.dispatchMouseEvent 在指定视口坐标点击。"""
    await _send(ws, "Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": x, "y": y, "button": "none",
    })
    await asyncio.sleep(0.05)
    await _send(ws, "Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1,
    })
    await asyncio.sleep(delay_ms / 1000.0)
    await _send(ws, "Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1,
    })


async def _auto_click_turnstile_cdp(ws, timeout_s: float = 90.0) -> bool:
    """自动点击 Turnstile 验证按钮。找到 iframe 后在 (left+30, center_y) 处点击。
    返回 True 表示通过（iframe 消失或页面跳转）。"""
    deadline = asyncio.get_event_loop().time() + timeout_s
    click_attempts = 0
    saw_widget = False

    await asyncio.sleep(1)

    while asyncio.get_event_loop().time() < deadline:
        info = await _find_turnstile_iframe_cdp(ws)

        if not info.get("found"):
            if saw_widget:
                print("  ✓ Turnstile 验证已通过（iframe 消失）")
                return True
            await asyncio.sleep(1)
            continue

        saw_widget = True
        rect = info.get("rect", {})
        if not rect or rect.get("width", 0) < 1 or rect.get("height", 0) < 1:
            await asyncio.sleep(1)
            continue

        click_x = rect["left"] + 30
        click_y = rect["top"] + rect["height"] / 2
        click_attempts += 1

        src_preview = (info.get("src") or "")[:60]
        print(
            f"  🤖 检测到 Turnstile iframe "
            f"({rect['width']:.0f}x{rect['height']:.0f}) "
            f"点击 @ ({click_x:.0f}, {click_y:.0f}) "
            f"[第 {click_attempts} 次] [{src_preview}]"
        )

        try:
            await _click_at_cdp(ws, click_x, click_y, delay_ms=60)
        except Exception as e:
            print(f"  ⚠️  Turnstile 点击失败: {e}")

        # 等验证结果
        for _ in range(8):
            await asyncio.sleep(1)
            check = await _find_turnstile_iframe_cdp(ws)
            if not check.get("found"):
                print("  ✓ Turnstile 验证已通过（iframe 消失）")
                await asyncio.sleep(3)
                return True

        if click_attempts >= 3:
            print(f"  ⚠️  Turnstile 点击达到 3 次上限")
            break

    if saw_widget:
        print(f"  ⚠️  Turnstile 未在 {timeout_s:.0f}s 内自动通过")
    return False


async def has_cf_clearance_cdp(url: str, debug_port: int = 9222) -> bool:
    """检查浏览器 profile 中是否已有 cf_clearance cookie"""
    ws_url = await _get_page_ws_url(debug_port)
    if not ws_url:
        return False
    try:
        async with websockets.connect(ws_url, max_size=10 * 1024 * 1024, open_timeout=10) as ws:
            result = await _send(ws, "Network.getAllCookies")
            cookies = result.get("cookies", [])
            return any(
                c.get("name") == "cf_clearance" and c.get("value")
                for c in cookies
            )
    except Exception:
        return False


async def bypass_cloudflare_cdp(
    url: str,
    debug_port: int = 9222,
    timeout_s: int = 600,
    check_interval: float = 5.0,
    wait_for_content: bool = True,
    expected_doi: str = "",
    pdf_mode: bool = False,
    download_dir: str = "",
) -> dict:
    """
    用纯 CDP WebSocket 打开 URL 并等待 Cloudflare 挑战通过。

    策略：复用现有 tab，用 JS location.href 导航（而不是 Page.navigate 新开 tab）。
    原因：Page.navigate 新开 tab 会被 Cloudflare 检测为高风险，导致 iframe 不渲染、
    挑战无法正常通过。复用 tab + JS 导航能正常触发挑战并自动通过。

    Returns:
        dict: {
            "success": bool,
            "target_id": str|None,
            "ws_url": str|None,
        }
    """
    result = {"success": False, "target_id": None, "ws_url": None}

    print(f"\n  🛡️  [纯CDP模式] 打开 {url[:80]}...")

    # 找一个可复用的 tab（优先 about:blank，其次任意 page）
    ws_url = None
    try:
        targets_url = f"http://localhost:{debug_port}/json"
        with urllib.request.urlopen(targets_url, timeout=5) as resp:
            targets = json.loads(resp.read().decode())

        # 优先选 New Tab（chrome://newtab）—— 起始环境最自然，
        # Cloudflare 挑战会正常渲染 iframe 并自动通过
        for t in targets:
            if t.get("type") == "page" and "chrome://newtab" in t.get("url", "").lower():
                ws_url = t.get("webSocketDebuggerUrl")
                print(f"  📄 复用 New Tab (chrome://newtab)")
                break
        
        # 找不到 New Tab 就新建一个
        # 注意：绝不复用 about:blank tab——它很可能是 Playwright 创建的，
        # 带有自动化指纹，会导致 Cloudflare 直接 403
        if not ws_url:
            print(f"  🔧  新建 New Tab（跳过 about:blank，避免 Playwright 指纹）")
            ws_url = await _create_new_tab(debug_port, "chrome://newtab")
            if ws_url:
                print(f"  📄 已新建 New Tab")
                # 等一下让 New Tab 加载完成
                import asyncio as _aio
                _aio.sleep(1)
        
        # 最后 fallback：任意 page（排除 about:blank）
        if not ws_url:
            for t in targets:
                if t.get("type") == "page" and t.get("url") != "about:blank":
                    ws_url = t.get("webSocketDebuggerUrl")
                    print(f"  📄 复用现有 tab: {t.get('title', '')[:40]}")
                    break
    except Exception as e:
        print(f"  ⚠️  获取 target 列表失败: {e}")

    # 实在没有就新开
    if not ws_url:
        ws_url = await _create_new_tab(debug_port, "about:blank")
        if not ws_url:
            print("  ❌ 无法连接到 Chrome CDP")
            return result

    try:
        async with websockets.connect(ws_url, max_size=10 * 1024 * 1024, open_timeout=10) as ws:
            _current_ws_url = ws_url

            # 启用必要的域
            await _send(ws, "Page.enable")
            await _send(ws, "Network.enable")
            await _send(ws, "Runtime.enable")

            # 用 JS location.href 导航（比 Page.navigate 指纹更自然）
            print(f"  🚀  导航到目标页面 (JS location.href)...")
            await _send(ws, "Runtime.evaluate", {
                "expression": f"location.href = {json.dumps(url)}"
            })

            # 等待页面开始加载
            await asyncio.sleep(3)

            deadline = asyncio.get_event_loop().time() + timeout_s
            challenge_detected = False
            challenge_rounds = 0  # 挑战页已经过了多少轮（用于判断 iframe 是否延迟加载）
            turnstile_tried = False
            last_status = ""

            # ── PDF 模式：监控下载目录新文件作为「挑战真正通过」的实体判据 ──
            # Chrome 已配置 always_open_pdf_externally=True + prompt_for_download=False，
            # 只有真正绕过挑战拿到 PDF 才会自动落盘新文件（.pdf / .crdownload ）。
            # 相比 cf_clearance cookie（会被正文页提前写入同一 profile 而污染），
            # 下载文件落盘是「实体证据」，绝不误判。调用方传入 download_dir 时启用。
            _dl_baseline = set()
            if pdf_mode and download_dir:
                try:
                    if os.path.isdir(download_dir):
                        _dl_baseline = set(os.listdir(download_dir))
                        print(f"  📁 监控下载目录: {download_dir} (基线 {len(_dl_baseline)} 文件)")
                except Exception as _e:
                    print(f"  ⚠️  下载目录初始化异常: {_e}")

            while asyncio.get_event_loop().time() < deadline:
                try:
                    # 获取标题
                    title_r = await _send(ws, "Runtime.evaluate", {"expression": "document.title || ''"})
                    title = title_r.get("result", {}).get("value", "") or ""
                    title_lower = title.lower()

                    # 获取 body 文本
                    body_r = await _send(ws, "Runtime.evaluate", {"expression": "document.body?.innerText || ''"})
                    body_text = body_r.get("result", {}).get("value", "") or ""
                    body_lower = body_text.lower()

                    # 检查是否是挑战页
                    is_challenge = _is_challenge_title(title_lower)
                    if not is_challenge:
                        # Title match is language-dependent; the DOM markers
                        # are not. See _CHALLENGE_DOM_JS.
                        try:
                            marker_r = await _send(ws, "Runtime.evaluate", {
                                "expression": _CHALLENGE_DOM_JS,
                                "returnByValue": True,
                            })
                            is_challenge = bool(
                                marker_r.get("result", {}).get("value")
                            )
                        except Exception:
                            pass
                    if is_challenge:
                        challenge_detected = True
                        challenge_rounds += 1

                    # 检查 cf_clearance
                    cookies_r = await _send(ws, "Network.getAllCookies")
                    cookies = cookies_r.get("cookies", [])
                    has_cf = any(
                        c.get("name") == "cf_clearance" and c.get("value")
                        for c in cookies
                    )

                    # 检查 verification successful
                    verification_ok = 'verification successful' in body_lower

                    # 检查 iframe 数（调试用）
                    iframe_r = await _send(ws, "Runtime.evaluate", {
                        "expression": "document.querySelectorAll('iframe').length"
                    })
                    iframe_count = iframe_r.get("result", {}).get("value", 0)

                    # 状态打印
                    status = (
                        f"title={title[:40]!r} "
                        f"cf={'✓' if has_cf else '✗'} "
                        f"iframes={iframe_count} "
                        f"body={len(body_text)}"
                    )
                    if status != last_status:
                        print(f"  📊  {status}")
                        last_status = status

                    # ── Turnstile 自动点击 ──
                    # 检测到挑战页就尝试找 Turnstile iframe 并点击
                    if is_challenge:
                        turnstile_info = await _find_turnstile_iframe_cdp(ws)
                        if turnstile_info.get("found") and not turnstile_tried:
                            print(f"  🎯  发现 Turnstile widget，尝试自动点击...")
                            turnstile_tried = True
                            await _auto_click_turnstile_cdp(ws, timeout_s=90.0)
                        elif not turnstile_info.get("found") and iframe_count == 0:
                            # interactive 模式：widget 延迟渲染（先转圈圈，再出框框）
                            # 每隔几秒用 CDP 真实鼠标点击一次 captcha-box 区域，触发 render
                            # 每 4 轮（8 秒）点一次，避免频繁点击
                            if challenge_rounds % 4 == 2:  # 第2、6、10...轮点
                                try:
                                    box_r = await _send(ws, "Runtime.evaluate", {
                                        "expression": (
                                            "(function(){"
                                            "var b=document.querySelector('#captcha-box, .cf-turnstile');"
                                            "if(!b)return {found:false};"
                                            "b.scrollIntoView({behavior:'instant',block:'center'});"
                                            "var r=b.getBoundingClientRect();"
                                            "return {found:true,x:r.x,y:r.y,w:r.width,h:r.height,cx:r.x+32,cy:r.y+r.height/2};"
                                            "})()"
                                        ),
                                        "returnByValue": True,
                                    })
                                    box_info = box_r.get("result", {}).get("value", {})
                                    # 只在 captcha-box 可见（高度>0）时才点击
                                    # interactive模式下widget先"转圈圈"再出框，高度为0说明还没渲染完
                                    if box_info.get("found") and box_info.get("h", 0) > 10:
                                        cx = box_info["cx"]
                                        cy = box_info["cy"]
                                        print(f"  🖱️  点击 Turnstile 区域 ({cx:.0f}, {cy:.0f})...")
                                        # 移动 + 按下 + 弹起
                                        await _send(ws, "Input.dispatchMouseEvent", {
                                            "type": "mouseMoved", "x": cx, "y": cy, "button": "none"
                                        })
                                        await asyncio.sleep(0.1)
                                        await _send(ws, "Input.dispatchMouseEvent", {
                                            "type": "mousePressed", "x": cx, "y": cy,
                                            "button": "left", "clickCount": 1, "buttons": 1
                                        })
                                        await asyncio.sleep(0.12)
                                        await _send(ws, "Input.dispatchMouseEvent", {
                                            "type": "mouseReleased", "x": cx, "y": cy,
                                            "button": "left", "clickCount": 1, "buttons": 0
                                        })
                                except Exception as e:
                                    print(f"     ⚠️  点击异常: {e}")

                    # ── 通过判定 ──
                    # 【最高优先级】DOI 判定：页面正文里出现期望的 DOI 就一定是论文页
                    # Cloudflare 挑战页绝对不会出现具体 DOI，这是最可靠的判据
                    # 全部转小写比较，避免大小写不一致
                    doi_passed = False
                    if expected_doi:
                        try:
                            doi_lower = expected_doi.lower()
                            doi_r = await _send(ws, "Runtime.evaluate", {
                                "expression": f"(document.body?.innerText || '').toLowerCase().includes({json.dumps(doi_lower)})"
                            })
                            doi_passed = doi_r.get("result", {}).get("value", False)
                        except Exception:
                            doi_passed = False

                    if doi_passed:
                        print(f"  ✅ DOI [{expected_doi}] 已出现在页面，挑战通过（{len(body_text)} 字）")
                        # DOI 出现说明正文已加载，等内容稳定
                        if wait_for_content:
                            print(f"  ⏳ 等待页面内容渲染完成（body 稳定检测，最多 60s）...")
                            stable_count = 0
                            last_body_len = len(body_text)
                            max_wait = 60
                            waited = 0
                            while waited < max_wait and stable_count < 3:
                                await asyncio.sleep(2)
                                waited += 2
                                try:
                                    b_r2 = await _send(ws, "Runtime.evaluate", {
                                        "expression": "(document.body?.innerText || '').length"
                                    })
                                    cur_len = b_r2.get("result", {}).get("value", 0)
                                    if cur_len == last_body_len and cur_len > 200:
                                        stable_count += 1
                                    else:
                                        stable_count = 0
                                        last_body_len = cur_len
                                except Exception:
                                    stable_count = 0
                            print(f"  ✓ 内容渲染完成（{last_body_len} 字，等待 {waited}s）")
                        target_id = _current_ws_url.rstrip("/").split("/")[-1]
                        result["success"] = True
                        result["target_id"] = target_id
                        result["ws_url"] = _current_ws_url
                        return result

                    # ── PDF 模式通过判定 ──
                    # PDF 页面 body 没有 DOI、也没有长正文（<5000字）。
                    # 首选判据：监控下载目录是否出现新文件（实体证据）。
                    #   Chrome 配了 always_open_pdf_externally=True + prompt_for_download=False，
                    #   只有真正绕过挑战拿到 PDF 才会落盘新文件（.pdf/.crdownload/.download/无扩展名）。
                    #   它不像 cf_clearance cookie 会被正文页提前写入同一 profile 而误判。
                    # 退路判据（未传 download_dir 时）：cf_clearance + 非挑战页。
                    if pdf_mode and not doi_passed:
                        _new_dl = None
                        if download_dir:
                            try:
                                _cur = set(os.listdir(download_dir)) if os.path.isdir(download_dir) else set()
                                _cands = _cur - _dl_baseline
                                # 过滤掉临时/无关文件，只认看起来像下载产物的
                                _cands = {f for f in _cands if not f.endswith('.tmp') and not f.endswith('.partial')}
                                # 去掉浏览器未完成下载标记，但仍视为"下载事件已触发"
                                _has_pdfish = any(
                                    f.lower().endswith(('.pdf', '.crdownload', '.download'))
                                    or ('.' not in f and len(f) < 64)  # 无扩展名的新文件也可能是
                                    for f in _cands
                                )
                                if _cands:
                                    # 只要基线之后多了文件，就说明下载已经真实开始/完成
                                    _new_dl = sorted(_cands)[0]
                                    _dl_baseline = _cur
                            except Exception as _e:
                                print(f"    ⚠️  下载目录检测异常: {_e}")
                        if _new_dl is not None:
                            print(f"  ✅ [PDF模式] 检测到下载事件（新文件: {_new_dl}），挑战真实通过")
                            target_id = _current_ws_url.rstrip("/").split("/")[-1]
                            result["success"] = True
                            result["target_id"] = target_id
                            result["ws_url"] = _current_ws_url
                            result["downloaded_file"] = os.path.join(download_dir, _new_dl) if download_dir else _new_dl
                            return result
                        # 未检测到下载文件：即使 cookie 在也不判成功（避免误判），继续等
                        if not has_cf and not is_challenge:
                            # 无下载监控且 cookie 也没有，保持等待
                            pass
                        # 退路：无 download_dir 时沿用 cookie 判据（向后兼容）
                        if has_cf and not is_challenge and not download_dir:
                            print(f"  ✅ [PDF模式] cf_clearance 已获取，挑战通过")
                            target_id = _current_ws_url.rstrip("/").split("/")[-1]
                            result["success"] = True
                            result["target_id"] = target_id
                            result["ws_url"] = _current_ws_url
                            return result

                    # 回退判定：body > 5000 字且标题无挑战关键词
                    # 只有长论文才会触发这个兜底；短论文必须等 DOI 出现才算通过
                    # （"ScienceDirect" 这类 600 字占位页绝对不会误判）
                    body_fallback_passed = (
                        not is_challenge
                        and len(title) > 0
                        and len(body_text) > 5000
                    )
                    if body_fallback_passed:
                        if challenge_detected:
                            print(f"  ✅ 挑战已通过，页面已加载（{len(body_text)} 字）")
                        else:
                            print(f"  ✅ 未触发挑战，直接访问成功")
                        # wait_for_content：等待 JS 动态渲染完成
                        # 策略：body 长度连续稳定 6 秒（采样间隔 2s，连续 3 次不变）才算加载完成
                        # AIP 等期刊是 JS 渲染的，标题先出来，正文和图片后加载
                        if wait_for_content:
                            print(f"  ⏳ 等待页面内容渲染完成（body 稳定检测，最多 60s）...")
                            stable_count = 0
                            last_body_len = len(body_text)
                            max_wait = 60
                            waited = 0
                            while waited < max_wait and stable_count < 3:
                                await asyncio.sleep(2)
                                waited += 2
                                try:
                                    b_r2 = await _send(ws, "Runtime.evaluate", {
                                        "expression": "(document.body?.innerText || '').length"
                                    })
                                    cur_len = b_r2.get("result", {}).get("value", 0)
                                    # 稳定判定：连续不变 + body>200（过滤几乎空页）
                                    # 不再用 5000 字硬门槛，避免短论文永远不稳定
                                    if cur_len == last_body_len and cur_len > 200:
                                        stable_count += 1
                                    else:
                                        stable_count = 0
                                        last_body_len = cur_len
                                except Exception:
                                    stable_count = 0
                            print(f"  ✓ 内容渲染完成（{last_body_len} 字，等待 {waited}s）")
                        else:
                            # 不需要 wait_for_content 时，也等一次 body 稳定（简单版）
                            # 避免页面还在加载就返回
                            print(f"  ⏳ 等待页面初始稳定（最多 10s）...")
                            stable_count = 0
                            last_body_len = len(body_text)
                            waited = 0
                            while waited < 10 and stable_count < 3:
                                await asyncio.sleep(1)
                                waited += 1
                                try:
                                    b_r2 = await _send(ws, "Runtime.evaluate", {
                                        "expression": "(document.body?.innerText || '').length"
                                    })
                                    cur_len = b_r2.get("result", {}).get("value", 0)
                                    if cur_len == last_body_len and cur_len > 200:
                                        stable_count += 1
                                    else:
                                        stable_count = 0
                                        last_body_len = cur_len
                                except Exception:
                                    stable_count = 0
                            print(f"  ✓ 页面初始稳定（{last_body_len} 字，等待 {waited}s）")
                        target_id = _current_ws_url.rstrip("/").split("/")[-1]
                        result["success"] = True
                        result["target_id"] = target_id
                        result["ws_url"] = _current_ws_url
                        return result

                    # 条件2：cf_clearance + verification successful 但标题还是挑战页
                    # （iframe 未渲染导致 postMessage 失败，页面没自动跳转）
                    if has_cf and verification_ok:
                        print(f"  ✅ 验证通过但页面未跳转，强制 reload...")
                        await _send(ws, "Runtime.evaluate", {
                            "expression": "location.reload()"
                        })
                        # 等 reload 完成
                        for _ in range(30):
                            await asyncio.sleep(2)
                            try:
                                t_r = await _send(ws, "Runtime.evaluate", {"expression": "document.title || ''"})
                                new_title = t_r.get("result", {}).get("value", "") or ""
                                still_chal = any(kw in new_title.lower() for kw in challenge_keywords)
                                b_r = await _send(ws, "Runtime.evaluate", {
                                    "expression": "(document.body?.innerText || '').length"
                                })
                                body_len = b_r.get("result", {}).get("value", 0)
                                # 优先判 DOI，其次 body>5000 兜底
                                reload_doi_pass = False
                                if expected_doi:
                                    try:
                                        doi_lower_r = expected_doi.lower()
                                        doi_chk = await _send(ws, "Runtime.evaluate", {
                                            "expression": f"(document.body?.innerText || '').toLowerCase().includes({json.dumps(doi_lower_r)})"
                                        })
                                        reload_doi_pass = doi_chk.get("result", {}).get("value", False)
                                    except Exception:
                                        reload_doi_pass = False
                                # 兜底：body>5000 字且标题正常（长论文才会命中）
                                reload_body_pass = (
                                    not still_chal
                                    and len(new_title) > 0
                                    and body_len > 5000
                                )
                                # DOI通过 或 长论文兜底 都算成功
                                if reload_doi_pass or reload_body_pass:
                                    print(f"  ✅ reload 成功: {new_title[:60]}")
                                    break
                            except Exception:
                                pass  # 加载中可能报错
                        if wait_for_content:
                            print(f"  ⏳ 等待页面内容渲染完成（body 稳定检测，最多 60s）...")
                            stable_count = 0
                            try:
                                ib = await _send(ws, "Runtime.evaluate", {
                                    "expression": "(document.body?.innerText || '').length"
                                })
                                last_body_len = ib.get("result", {}).get("value", 0)
                            except Exception:
                                last_body_len = 0
                            max_wait = 60
                            waited = 0
                            while waited < max_wait and stable_count < 3:
                                await asyncio.sleep(2)
                                waited += 2
                                try:
                                    b_r2 = await _send(ws, "Runtime.evaluate", {
                                        "expression": "(document.body?.innerText || '').length"
                                    })
                                    cur_len = b_r2.get("result", {}).get("value", 0)
                                    # 稳定判定：连续不变 + body>200（过滤几乎空页）
                                    if cur_len == last_body_len and cur_len > 200:
                                        stable_count += 1
                                    else:
                                        stable_count = 0
                                        last_body_len = cur_len
                                except Exception:
                                    stable_count = 0
                            print(f"  ✓ 内容渲染完成（{last_body_len} 字，等待 {waited}s）")
                        target_id = _current_ws_url.rstrip("/").split("/")[-1]
                        result["success"] = True
                        result["target_id"] = target_id
                        result["ws_url"] = _current_ws_url
                        return result

                except Exception as e:
                    print(f"  ⚠️  轮询出错: {e}")

                await asyncio.sleep(check_interval)

            # 超时
            print(f"  ⏰  Cloudflare 挑战未在 {timeout_s}s 内通过")
            return result

    except Exception as e:
        print(f"  ❌ CDP 连接异常: {e}")
        return result


if __name__ == "__main__":
    import sys
    test_url = (
        sys.argv[1] if len(sys.argv) > 1
        else "https://pubs.aip.org/aip/pop/article/30/10/100601/2915124/Electrode-durability-and-sheared-flow-stabilized-Z"
    )
    result = asyncio.run(bypass_cloudflare_cdp(test_url, timeout_s=120))
    print(f"\n结果: {'成功' if result['success'] else '失败'}")
    if result['success']:
        print(f"  target_id: {result['target_id']}")

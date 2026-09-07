#!/usr/bin/env python3
"""Open a URL in a throwaway Chrome that carries no automation fingerprint.

Why this exists
---------------
The headed flow is: pure CDP opens the article page, Playwright attaches,
then the same browser opens the PDF. By the time the PDF page loads, that
browser has been driven by Playwright and carries its fingerprint —
``navigator.webdriver``, the CDP-injected bindings, the mutated page
lifecycle. Cloudflare sees it and refuses.

Clearing the challenge on the article page does not help, either. Several
publishers serve the article and the PDF from **different hosts**
(ScienceDirect: ``www.sciencedirect.com`` vs ``pdf.sciencedirectassets.com``),
and a Cloudflare clearance cookie is bound to the host that issued it. The
PDF host is a fresh challenge on a browser that now looks automated.

So the PDF gets its own browser: a new Chrome on its own debugging port,
seeded from the user's real profile, never touched by Playwright. It opens
the URL over raw CDP, clears the challenge the same way the article preload
does, and is torn down afterwards.

Usage::

    session = await open_url_in_fresh_chrome(pdf_url, pdf_mode=True,
                                             download_dir=str(tmp))
    try:
        if session.result.get('success'):
            saved = session.result.get('downloaded_file')
    finally:
        await session.close()

Environment
-----------
``CHROME_PDF_DEBUG_PORT``   port for the throwaway instance (default 9333);
                            must differ from ``CHROME_DEBUG_PORT``
``CHROME_PROFILE_SOURCE_DIR``  real profile to copy from (per-platform default)
``CHROME_PROFILE``          profile name inside it (default ``Default``)
``CHROME_PDF_PROFILE_ROOT`` where to build the throwaway profile
                            (default: a temp directory, removed on close)
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

try:
    from config import (
        CHROME_PATH,
        CHROME_PROFILE,
        CHROME_PROFILE_SOURCE_DIR,
        IS_WINDOWS,
    )
except ImportError:                                  # standalone use
    IS_WINDOWS = sys.platform == 'win32'
    CHROME_PATH = 'chrome.exe' if IS_WINDOWS else 'google-chrome'
    CHROME_PROFILE = os.environ.get('CHROME_PROFILE', 'Default')
    CHROME_PROFILE_SOURCE_DIR = str(Path.home() / '.config' / 'google-chrome')


# The minimal working set that makes a copied profile look like the user's:
# cookies (so existing clearances and any institutional login carry over),
# the preference files Chrome validates on startup, and saved logins.
#
# Local State MUST come along with Cookies -- it holds the encryption key, so
# copying one without the other yields cookies that cannot be decrypted.
#
# History / Cache / Sessions / extensions are deliberately left behind: this
# profile is disposable, and copying a multi-gigabyte cache for one PDF is
# both slow and pointless.
_PROFILE_FILES = (
    'Cookies',
    'Cookies-journal',
    'Login Data',
    'Preferences',
    'Secure Preferences',
    'Web Data',
)
_PROFILE_ROOT_FILES = ('Local State',)


def _default_pdf_port() -> int:
    raw = (os.environ.get('CHROME_PDF_DEBUG_PORT') or '').strip()
    try:
        port = int(raw)
        if 1 <= port <= 65535:
            return port
    except ValueError:
        pass
    return 9333


def _port_is_free(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return False        # something is already listening
    except OSError:
        return True


def _pick_free_port(preferred: int) -> int:
    """Return *preferred* if nothing is listening, else the next free port.

    Two extractions running concurrently would otherwise attach to each
    other's throwaway browser.
    """
    port = preferred
    for _ in range(20):
        if _port_is_free(port):
            return port
        port += 1
    return preferred


def seed_profile(target: Path, source: Path, profile_name: str) -> bool:
    """Copy the minimal working set from the real profile into *target*.

    Returns True when at least one file was copied.
    """
    src_inner = source / profile_name
    if not src_inner.is_dir():
        print(f"  ⚠️  源 profile 不存在: {src_inner}")
        return False

    dst_inner = target / profile_name
    dst_inner.mkdir(parents=True, exist_ok=True)

    copied = []
    for name in _PROFILE_FILES:
        src_file = src_inner / name
        if not src_file.exists():
            continue
        try:
            shutil.copy2(src_file, dst_inner / name)
            copied.append(name)
        except OSError:
            pass
    for name in _PROFILE_ROOT_FILES:
        src_file = source / name
        if not src_file.exists():
            continue
        try:
            shutil.copy2(src_file, target / name)
            copied.append(name)
        except OSError:
            pass

    if not copied:
        print(f"  ⚠️  未能从 {source} 复制任何文件")
        return False
    print(f"  ✓ 已从真实 profile 播种: {', '.join(copied)}")
    return True


def _write_download_prefs(profile_dir: Path, profile_name: str,
                          download_dir: str) -> None:
    """Make the throwaway profile save PDFs instead of previewing them."""
    import json

    prefs_path = profile_dir / profile_name / 'Preferences'
    try:
        prefs = json.loads(prefs_path.read_text(encoding='utf-8')) \
            if prefs_path.exists() else {}
    except (OSError, ValueError):
        prefs = {}

    prefs.setdefault('plugins', {})['always_open_pdf_externally'] = True
    download = prefs.setdefault('download', {})
    download['prompt_for_download'] = False
    download['directory_upgrade'] = True
    if download_dir:
        download['default_directory'] = download_dir

    try:
        prefs_path.parent.mkdir(parents=True, exist_ok=True)
        prefs_path.write_text(json.dumps(prefs), encoding='utf-8')
    except OSError as exc:
        print(f"  ⚠️  写入下载偏好失败: {exc}")


async def open_url_via_cdp(url: str, port: int, *, expected_doi: str = '',
                          pdf_mode: bool = False, download_dir: str = '',
                          timeout_s: int = 60) -> dict:
    """Open *url* over raw CDP on an already-running Chrome at *port*.

    The shared "navigate and clear Cloudflare without Playwright attached"
    step, used both by the article-page preload (which targets the batch's
    shared browser) and by :meth:`FreshChromeSession.open_url` (which targets
    the throwaway one). Keeping it in one place means the challenge handling
    cannot drift between the two.
    """
    from cf_bypass_cdp import bypass_cloudflare_cdp

    return await bypass_cloudflare_cdp(
        url=url,
        debug_port=port,
        timeout_s=timeout_s,
        wait_for_content=not pdf_mode,
        expected_doi=expected_doi,
        pdf_mode=pdf_mode,
        download_dir=download_dir,
    )


class FreshChromeSession:
    """A disposable Chrome instance with a copy of the real profile.

    Nothing here attaches Playwright. The whole point is that the browser
    stays fingerprint-clean, so the caller drives it over raw CDP (via
    :mod:`cf_bypass_cdp`) or simply reads whatever the page downloaded.
    """

    def __init__(self, port: Optional[int] = None,
                 download_dir: str = '',
                 keep_profile: bool = False):
        self.port = _pick_free_port(port or _default_pdf_port())
        self.download_dir = download_dir
        self.keep_profile = keep_profile
        self.process: Optional[subprocess.Popen] = None
        self.profile_dir: Optional[Path] = None
        self._owns_profile = False
        self.result: dict = {}

    @property
    def cdp_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------------

    def _make_profile(self) -> Path:
        root = (os.environ.get('CHROME_PDF_PROFILE_ROOT') or '').strip()
        if root:
            Path(root).expanduser().mkdir(parents=True, exist_ok=True)
            target = Path(tempfile.mkdtemp(prefix='chrome_pdf_',
                                           dir=str(Path(root).expanduser())))
        else:
            target = Path(tempfile.mkdtemp(prefix='chrome_pdf_'))
        self._owns_profile = True

        source = Path(
            (os.environ.get('CHROME_PROFILE_SOURCE_DIR') or '').strip()
            or CHROME_PROFILE_SOURCE_DIR
        ).expanduser()
        profile_name = os.environ.get('CHROME_PROFILE', CHROME_PROFILE) or 'Default'
        seed_profile(target, source, profile_name)
        _write_download_prefs(target, profile_name, self.download_dir)
        return target

    async def start(self) -> bool:
        """Launch the browser and wait for its CDP port."""
        self.profile_dir = self._make_profile()

        args = [
            CHROME_PATH,
            f'--remote-debugging-port={self.port}',
            f'--user-data-dir={self.profile_dir}',
            '--no-first-run',
            '--no-default-browser-check',
            '--no-sandbox',
            '--disable-dev-shm-usage',
            # No anti-detection flags on purpose: they make the fingerprint
            # *less* like a real browser, which is what we are trying to be.
        ]
        if self.download_dir:
            Path(self.download_dir).mkdir(parents=True, exist_ok=True)

        extra = {}
        if not IS_WINDOWS:
            extra['preexec_fn'] = os.setsid
        else:
            extra['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP

        print(f"  🌐 启动独立 Chrome (端口 {self.port}, 真实 profile 副本)...")
        try:
            self.process = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **extra)
        except OSError as exc:
            print(f"  ⚠️  启动 Chrome 失败: {exc}")
            return False

        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                print(f"  ⚠️  Chrome 已退出 (exit={self.process.returncode})")
                return False
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=1):
                    print(f"  ✓ CDP 端口 {self.port} 就绪")
                    return True
            except OSError:
                await asyncio.sleep(0.5)

        print(f"  ⚠️  CDP 端口 {self.port} 20s 内未响应")
        return False

    async def open_url(self, url: str, *, expected_doi: str = '',
                       pdf_mode: bool = False, timeout_s: int = 60) -> dict:
        """Open *url* over raw CDP, clearing any Cloudflare challenge."""
        self.result = await open_url_via_cdp(
            url, self.port,
            expected_doi=expected_doi,
            pdf_mode=pdf_mode,
            download_dir=self.download_dir,
            timeout_s=timeout_s,
        )
        return self.result

    async def _browser_close_via_cdp(self) -> bool:
        """Ask the browser to quit through CDP. Returns True if it obeyed."""
        import json as _json
        import urllib.request

        try:
            with urllib.request.urlopen(
                    f'{self.cdp_endpoint}/json/version', timeout=3) as resp:
                ws_url = _json.loads(resp.read().decode()).get('webSocketDebuggerUrl')
        except Exception:
            return False
        if not ws_url:
            return False

        try:
            import websockets
        except ImportError:
            return False
        try:
            async with websockets.connect(ws_url, max_size=None) as ws:
                await ws.send(_json.dumps({'id': 1, 'method': 'Browser.close'}))
                try:
                    await asyncio.wait_for(ws.recv(), timeout=3)
                except Exception:
                    pass
            return True
        except Exception:
            return False

    def _kill_by_profile(self) -> None:
        """Kill any Chrome still holding this session's throwaway profile.

        Chrome's launched process often forks and exits, leaving the real
        browser detached — ``self.process.poll()`` then reports "already
        gone" and the process-group kill is skipped, so the browser survives
        and keeps its debugging port bound. Matching on the profile path is
        exact (the directory name is unique to this session) so it cannot
        touch the user's own Chrome.
        """
        if not self.profile_dir or IS_WINDOWS:
            return
        marker = f'--user-data-dir={self.profile_dir}'
        try:
            out = subprocess.run(['pgrep', '-f', marker],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return
        for line in (out.stdout or '').split():
            try:
                os.kill(int(line), 9)
            except (ValueError, OSError):
                pass

    async def close(self) -> None:
        """Kill the browser and delete the throwaway profile."""
        await self._browser_close_via_cdp()

        if self.process is not None:
            try:
                if not IS_WINDOWS:
                    os.killpg(os.getpgid(self.process.pid), 15)
                else:
                    self.process.terminate()
            except OSError:
                pass
            for _ in range(12):
                if self.process.poll() is not None:
                    break
                await asyncio.sleep(0.25)
            if self.process.poll() is None:
                try:
                    self.process.kill()
                except OSError:
                    pass

        # Belt and braces: the launcher process exiting does not mean the
        # browser did.
        self._kill_by_profile()
        await asyncio.sleep(0.3)
        self.process = None

        if (self.profile_dir and self._owns_profile and not self.keep_profile
                and self.profile_dir.exists()):
            try:
                shutil.rmtree(self.profile_dir, ignore_errors=True)
            except OSError:
                pass
        self.profile_dir = None


async def open_url_in_fresh_chrome(url: str, *, expected_doi: str = '',
                                   pdf_mode: bool = False,
                                   download_dir: str = '',
                                   timeout_s: int = 60,
                                   port: Optional[int] = None
                                   ) -> FreshChromeSession:
    """Launch a clean Chrome, open *url* in it, and hand back the session.

    The session is returned **started and still running** so the caller can
    read ``session.result`` (and, in ``pdf_mode``, the downloaded file) before
    calling ``await session.close()``. Always close it — that is what removes
    the throwaway profile.

    On launch failure the session comes back with an empty ``result``; the
    caller should fall back to its normal path rather than assume success.
    """
    session = FreshChromeSession(port=port, download_dir=download_dir)
    if not await session.start():
        await session.close()
        return session
    try:
        await session.open_url(url, expected_doi=expected_doi,
                               pdf_mode=pdf_mode, timeout_s=timeout_s)
    except Exception as exc:
        print(f"  ⚠️  独立 Chrome 打开页面失败: {type(exc).__name__}: {exc}")
    return session

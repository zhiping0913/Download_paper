#!/usr/bin/env python3
"""Chrome sessions that can get through Cloudflare.

One module for everything that opens a browser and clears a challenge —
previously split across ``chrome_launcher.py`` (launch/kill/preferences),
``cf_bypass_cdp.py`` (the pure-CDP challenge solver) and ``fresh_chrome.py``
(the throwaway instance used for PDFs). They shared a job and three copies of
the same helpers: writing download preferences, polling the CDP port, and
building Chrome's argv.

Layout
------
1. Chrome process        argv, spawn, CDP-port wait, kill, preferences, profile seeding
2. CDP primitives        websocket send, tab lookup, synthetic mouse clicks
3. Challenge handling    locale-independent detection, Turnstile auto-click
4. Public entry points   bypass_cloudflare_cdp, has_cf_clearance_cdp,
                         open_url_via_cdp, launch_chrome,
                         FreshChromeSession, open_url_in_fresh_chrome

Why pure CDP at all: attaching Playwright injects an automation fingerprint
that pushes Cloudflare into its hardest mode, so the challenge is cleared over
raw CDP *before* Playwright connects.

Why a throwaway instance for PDFs: by then the shared browser has been driven
by Playwright and carries that fingerprint. Clearing the challenge on the
article host does not carry over either — publishers such as ScienceDirect
serve the PDF from a different host (``pdf.sciencedirectassets.com``) and a
clearance cookie is bound to the host that issued it.

Usage::

    # clear a challenge on an already-running Chrome
    result = await open_url_via_cdp(url, port=9222, expected_doi=doi)

    # or in a browser that has never seen Playwright
    session = await open_url_in_fresh_chrome(pdf_url, pdf_mode=True,
                                             download_dir=str(tmp))
    try:
        saved = session.result.get('downloaded_file')
    finally:
        await session.close()

Environment
-----------
``CHROME_PATH``                 executable (else auto-detected by config.py)
``CHROME_DEBUG_PORT``           shared instance's port (default 9222)
``CHROME_PROFILE``              profile name inside it (default ``Default``)
``CHROME_PROFILE_ROOT``         holds both scraping profiles: ``main_dir``
                                and ``aux_dir`` (default: a per-run
                                <tmp>/dp_profiles_xxxxxx)
``CHROME_AUX_DEBUG_PORT``       throwaway instance's port (default 9333).
                                ``CHROME_PDF_DEBUG_PORT`` is still read as a
                                fallback: the instance outgrew being PDF-only
                                (it also fetches supplemental pages, figures
                                and API responses), which is the rename
``CHROME_PROFILE_SOURCE_DIR``   real profile that gets copied
``CHROME_DOWNLOAD_DIR``         default download directory
``FRESH_PROFILE``               1 = every instance gets a brand-new empty
                                profile, with no seeding from the real one
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import glob
import json
import os
import random
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import websockets

# Safe at module level despite core.utilities also reaching for this module:
# its two references to chrome_session are lazy, inside functions, precisely so
# the dependency runs one way. Verified by import, not by reasoning.
from core.utilities import url_looks_like_bot_challenge

try:
    from config import (
        CHROME_DEBUG_PORT,
        CHROME_PATH,
        CHROME_PROFILE,
        CHROME_PROFILE_SOURCE_DIR,
        CHROME_USER_DATA_DIR,
        FRESH_PROFILE,
        IS_WINDOWS,
    )
except ImportError:                                  # standalone use
    IS_WINDOWS = sys.platform == 'win32'
    CHROME_PATH = 'chrome.exe' if IS_WINDOWS else 'google-chrome'
    CHROME_PROFILE = os.environ.get('CHROME_PROFILE', 'Default')
    CHROME_DEBUG_PORT = int(os.environ.get('CHROME_DEBUG_PORT', 9222))
    CHROME_USER_DATA_DIR = (
        str(Path.home() / 'AppData' / 'Local' / 'Google' / 'Chrome' / 'User Data')
        if IS_WINDOWS else str(Path.home() / '.config' / 'google-chrome')
    )
    CHROME_PROFILE_SOURCE_DIR = CHROME_USER_DATA_DIR
    FRESH_PROFILE = os.environ.get('FRESH_PROFILE', '').strip().lower() in (
        '1', 'true', 'yes', 'on')


# ==========================================================================
# 1. Chrome process
# ==========================================================================

# The minimal working set that makes a copied profile look like the user's:
# cookies (so existing clearances and any institutional login carry over),
# the preference files Chrome validates at startup, and saved logins.
#
# Local State MUST accompany Cookies -- it holds the encryption key, so
# copying one without the other yields cookies that cannot be decrypted.
#
# History / Cache / Sessions / extensions are deliberately left behind: a
# scraping profile is disposable, and copying a multi-gigabyte cache is both
# slow and exactly the accumulated state we want to shed.
PROFILE_SEED_FILES = (
    'Cookies',
    'Cookies-journal',
    'Login Data',
    'Preferences',
    'Secure Preferences',
    'Web Data',
)
PROFILE_SEED_ROOT_FILES = ('Local State',)

# Bot-manager cookies to strip from the seeded copy.
#
# Seeding the real profile is what buys institutional entitlement, but the same
# Cookies file also carries the bot manager's own reputation state -- so every
# "fresh" throwaway Chrome went out wearing the identity that had just been
# flagged. Measured: FRESH_PROFILE=1 (no cookies at all) downloads IOP and
# ScienceDirect PDFs straight away, where the seeded profile got perfdrive.
# Dropping just these rows is the attempt to keep the entitlement and lose the
# record.
#
# ⚠️ GLOB, not LIKE. In SQL LIKE, '_' is a single-character wildcard, so
# "name LIKE '__ss%'" also matches session, sessionid, passport_csrf_token …
# -- on this machine that pattern swept in 15 unrelated cookies including the
# user's own claude.ai session keys. GLOB treats '_' literally.
_BOT_COOKIE_NAME_GLOBS = ('__uzm*', '__ss*', 'uzmx', 'uzmxj')
_BOT_COOKIE_HOST_GLOBS = ('*perfdrive.com', '*distilnetworks*', '*distilidentify*')


def _strip_bot_cookies(cookies_db: Path) -> int:
    """Delete bot-manager rows from a *copied* cookie DB. Returns rows removed.

    Only ever touches the throwaway profile's copy -- the real profile is
    read-only to this program and must stay that way.

    Failure is not fatal: a seeded profile that still carries the bot cookies
    is exactly what we had before, so any error degrades to the old behaviour
    rather than breaking the run.
    """
    if os.environ.get('DP_SEED_DROP_BOT_COOKIES', '1').strip().lower() in (
            '0', 'false', 'no', 'off'):
        return 0
    import sqlite3          # stdlib, imported here because this is its only use
    where = ' OR '.join(
        ["name GLOB ?"] * len(_BOT_COOKIE_NAME_GLOBS)
        + ["host_key GLOB ?"] * len(_BOT_COOKIE_HOST_GLOBS))
    params = list(_BOT_COOKIE_NAME_GLOBS) + list(_BOT_COOKIE_HOST_GLOBS)
    try:
        with sqlite3.connect(str(cookies_db)) as con:
            removed = con.execute(
                f"DELETE FROM cookies WHERE {where}", params).rowcount
            con.commit()
        # A stale journal could roll the deletes back. It is normally empty,
        # but "normally" is not a guarantee worth relying on here.
        journal = cookies_db.with_name(cookies_db.name + '-journal')
        if journal.exists():
            journal.unlink()
        return max(0, removed)
    except Exception as exc:
        print(f"  ⚠️  清理反爬 cookie 失败（{type(exc).__name__}: {str(exc)[:60]}），"
              f"按原样播种")
        return 0


def seed_profile(target: Path, source: Path, profile_name: str = '') -> bool:
    """Copy the minimal working set from a real profile into *target*.

    Returns True when at least one file was copied.
    """
    profile_name = profile_name or CHROME_PROFILE or 'Default'
    src_inner = Path(source) / profile_name
    if not src_inner.is_dir():
        print(f"  ⚠️  源 profile 不存在: {src_inner}")
        return False

    dst_inner = Path(target) / profile_name
    dst_inner.mkdir(parents=True, exist_ok=True)

    copied = []
    for name in PROFILE_SEED_FILES:
        src_file = src_inner / name
        if src_file.exists():
            try:
                shutil.copy2(src_file, dst_inner / name)
                copied.append(name)
            except OSError:
                pass
    for name in PROFILE_SEED_ROOT_FILES:
        src_file = Path(source) / name
        if src_file.exists():
            try:
                shutil.copy2(src_file, Path(target) / name)
                copied.append(name)
            except OSError:
                pass

    if not copied:
        print(f"  ⚠️  未能从 {source} 复制任何文件")
        return False
    print(f"  ✓ 已从真实 profile 播种: {', '.join(copied)}")

    # Strip the bot manager's own state from the copy we just made, so the
    # throwaway browser keeps the login cookies without inheriting whatever
    # reputation the real profile accumulated. DP_SEED_DROP_BOT_COOKIES=0
    # restores the previous all-or-nothing behaviour for A/B testing.
    if 'Cookies' in copied:
        dropped = _strip_bot_cookies(dst_inner / 'Cookies')
        if dropped:
            print(f"  ✂️  已剔除 {dropped} 条反爬 cookie（__uzm* / __ss* / perfdrive）")
    return True


def write_chrome_preferences(user_data_dir, profile_name: str = '',
                             download_dir: str = '', quiet: bool = False) -> None:
    """Write the preferences a scraping profile needs, preserving the rest.

    The one that matters is ``always_open_pdf_externally``: without it Chrome
    renders the PDF in its viewer and never fires a download event, so a PDF
    fetch silently produces nothing.
    """
    profile_name = profile_name or CHROME_PROFILE or 'Default'
    prefs_path = Path(user_data_dir) / profile_name / 'Preferences'
    try:
        prefs = json.loads(prefs_path.read_text(encoding='utf-8')) \
            if prefs_path.exists() else {}
    except (OSError, ValueError):
        prefs = {}

    target_dir = download_dir or os.environ.get('CHROME_DOWNLOAD_DIR', '') \
        or str(Path.home() / 'Downloads')

    prefs.setdefault('plugins', {})['always_open_pdf_externally'] = True
    download = prefs.setdefault('download', {})
    download['default_directory'] = target_dir
    download['prompt_for_download'] = False
    download['directory_upgrade'] = True
    prefs.setdefault('browser', {})['check_default_browser'] = False
    # Keep the window alive when the last tab closes, so the session survives
    # a tab teardown mid-batch.
    prefs.setdefault('profile', {})['exit_type'] = 'Normal'

    try:
        prefs_path.parent.mkdir(parents=True, exist_ok=True)
        prefs_path.write_text(json.dumps(prefs, indent=2), encoding='utf-8')
    except OSError as exc:
        print(f"  ⚠️  写入 Chrome 偏好失败: {exc}")
        return

    if not quiet:
        print("✓ Chrome 设置已配置:")
        print("  - PDF 处理: 默认下载（always_open_pdf_externally=True）")
        print(f"  - 下载目录: {target_dir}")
        print("  - 下载提示: 关闭")


def chrome_password_store_args() -> list:
    """Chrome's flag for reaching the OS keyring, when that is possible.

    Cookies copied from the real profile are encrypted with a key kept in the
    keyring -- on Linux ``Local State`` carries no ``os_crypt.encrypted_key``
    -- so a Chrome that cannot reach it falls back to the "basic" store,
    fails to decrypt, and drops them silently. Measured on this machine: 1712
    seeded cookies become 22 visible, and every one of the 247 publisher
    cookies is lost. Naming the store brings back 1304, 184 of them
    publishers'.

    Linux only, and only with a session bus to talk to: without one Chrome can
    block waiting on a keyring that will never unlock. The right value is
    machine-dependent (kwallet5 yielded nothing here), so
    ``CHROME_PASSWORD_STORE`` overrides it; set it empty to drop the flag.
    """
    override = os.environ.get('CHROME_PASSWORD_STORE')
    if override is not None:
        override = override.strip()
        return [f'--password-store={override}'] if override else []
    if IS_WINDOWS or sys.platform == 'darwin':
        return []
    if not (os.environ.get('DBUS_SESSION_BUS_ADDRESS') or '').strip():
        return []
    return ['--password-store=gnome-libsecret']


def chrome_argv(user_data_dir, port: int, headless: bool = False,
                start_url: str = 'about:blank') -> list:
    """Chrome's command line for a scraping instance.

    Deliberately free of "anti-detection" flags (``--disable-extensions``,
    ``--disable-blink-features=AutomationControlled`` and friends): they make
    the fingerprint *less* like a real browser, which is the opposite of what
    gets a challenge cleared.
    """
    args = [
        CHROME_PATH,
        f'--remote-debugging-port={port}',
        f'--user-data-dir={user_data_dir}',
        '--no-first-run',
        '--no-default-browser-check',
        '--no-sandbox',
        '--disable-dev-shm-usage',
    ]
    args.extend(chrome_password_store_args())
    if headless:
        args.append('--headless=new')
    # The starting URL goes on the command line. For the throwaway browser
    # that is the article/PDF URL itself, so the page is fetched by Chrome's
    # own startup navigation -- before any CDP command has touched the tab.
    if start_url:
        args.append(start_url)
    return args


def spawn_chrome(user_data_dir, port: int, headless: bool = False,
                 start_url: str = 'about:blank') -> Optional[subprocess.Popen]:
    """Start Chrome detached, in its own process group. None on failure."""
    extra = {}
    if not IS_WINDOWS:
        extra['preexec_fn'] = os.setsid
    else:
        extra['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(
            chrome_argv(user_data_dir, port, headless, start_url),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **extra)
    except OSError as exc:
        print(f"  ⚠️  启动 Chrome 失败: {exc}")
        return None


def cdp_port_open(port: int, timeout: float = 1.0) -> bool:
    """True if something is listening on *port*."""
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=timeout):
            return True
    except OSError:
        return False


async def wait_for_cdp_port(port: int, timeout_s: float = 20.0,
                            process: Optional[subprocess.Popen] = None) -> bool:
    """Poll until Chrome's debugging port answers.

    Polling beats a fixed sleep in both directions: a warm profile is ready in
    a couple of seconds, a cold one can take fifteen. When *process* is given,
    its death is detected immediately — Chrome exits at once if another
    instance already holds the profile lock (it forwards the URL and quits),
    and the port would never open.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            print(f"  ⚠️  Chrome 进程已退出 (exit={process.returncode})。"
                  "常见原因：--user-data-dir 指向的 profile 正被其它 Chrome 实例占用；"
                  "换一个专用 profile 或先关掉现有 Chrome。")
            return False
        if cdp_port_open(port):
            print(f"  ✓ CDP 端口 {port} 就绪")
            return True
        await asyncio.sleep(0.5)
    print(f"  ⚠️  CDP 端口 {port} 在 {timeout_s:.0f}s 内未响应")
    return False


def _scraping_chrome_pids(user_data_dir=None) -> list:
    """PIDs of Chrome processes this tool started.

    Identified by ``--user-data-dir``: ours always sit in a temp directory
    named with one of :data:`TEMP_PROFILE_PREFIXES`, either as the directory
    itself or as its parent (``/tmp/dp_profiles_ab12cd/main_dir``). A browser
    the user opened themselves never carries such a path, which is the whole
    point of looking.

    Pass *user_data_dir* to narrow it to one profile.
    """
    wanted = str(user_data_dir).rstrip(os.sep) if user_data_dir else ''
    pids = []
    try:
        listing = subprocess.run(['ps', '-eo', 'pid=,args='],
                                 capture_output=True, text=True,
                                 timeout=10).stdout
    except Exception:
        return pids

    for line in listing.splitlines():
        line = line.strip()
        if not line or 'chrome' not in line.lower():
            continue
        pid_str, _, args = line.partition(' ')
        profile = ''
        for token in args.split():
            if token.startswith('--user-data-dir='):
                profile = token.split('=', 1)[1].rstrip(os.sep)
                break
        if not profile:
            continue

        if wanted:
            if profile != wanted and not profile.startswith(wanted + os.sep):
                continue
        else:
            base = os.path.basename(profile)
            parent = os.path.basename(os.path.dirname(profile))
            if not any(name.startswith(p)
                       for name in (base, parent)
                       for p in TEMP_PROFILE_PREFIXES):
                continue
        try:
            pids.append(int(pid_str))
        except ValueError:
            continue
    return pids


def kill_chrome(user_data_dir=None) -> None:
    """Close the scraping Chromes this tool started. Never a blanket kill.

    This used to be ``pkill -9 chrome``, which took out the user's own browser
    along with every other extraction running on the machine -- a batch job
    that had been going for hours would lose its browser because an unrelated
    run decided the debug port looked stale.

    ⚠️ It still cannot tell a crashed leftover from another *live* extraction:
    both have a profile under the same temp prefixes, and both have been
    reparented to init, so there is nothing left in the process table to
    separate them. Running two extractions at once therefore needs
    CHROME_DEBUG_PORT / CHROME_AUX_DEBUG_PORT set apart, so neither ever looks
    at the other's port and calls this in the first place.
    """
    import signal

    pids = _scraping_chrome_pids(user_data_dir)
    if not pids:
        print("✓ 没有需要关闭的抓取 Chrome")
        return

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(2)

    survivors = 0
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            survivors += 1
        except OSError:
            pass
    print(f"✓ 已关闭 {len(pids)} 个抓取 Chrome 进程"
          + (f"（其中 {survivors} 个需强制结束）" if survivors else ""))


# Temporary-profile name prefixes owned by this module. Anything matching
# these in the temp directory was created by a previous run.
# 'chrome_pdf_' stays alongside 'chrome_aux_': a directory left behind by a
# run from before the rename is still ours to sweep.
TEMP_PROFILE_PREFIXES = ('dp_profiles_', 'chrome_fresh_', 'chrome_aux_',
                         'chrome_pdf_', 'chrome_fallback_', 'chrome_9',
                         'chrome_')


def _profile_dirs_in_use() -> set:
    """user-data-dir paths held by Chrome processes that are still alive."""
    in_use = set()
    for proc_dir in glob.glob('/proc/[0-9]*'):
        try:
            cmdline = Path(proc_dir, 'cmdline').read_bytes().decode(
                'utf-8', 'replace')
        except OSError:
            continue                             # process exited mid-scan
        for arg in cmdline.split('\x00'):
            if arg.startswith('--user-data-dir='):
                in_use.add(arg.split('=', 1)[1])
    return in_use


def sweep_stale_profiles(quiet: bool = False) -> int:
    """Delete temp profiles left behind by earlier runs. Returns the count.

    A profile that outlives its run is not merely wasted disk: a leftover
    Chrome still holding the debug port makes the next run *reuse* it, so a
    run that asked for a clean profile silently inherits the previous one's
    accumulated automation fingerprint. Sweeping at startup keeps that from
    happening.

    ⚠️ **This does NOT reliably spare a concurrent run.** The docstring used to
    claim "only directories no live Chrome has open are removed, so a
    concurrent run is never touched". That is false, and believing it nearly
    destroyed a 41-hour batch.

    ``_profile_dirs_in_use()`` collects the ``--user-data-dir`` values Chrome
    was launched with, and those are **child** paths
    (``/tmp/dp_profiles_ab12cd/main_dir``). The glob below yields the **root**
    (``/tmp/dp_profiles_ab12cd``). ``path in in_use`` compares a root against a
    set of children, so it never matches and the root is removed -- even while
    a Chrome is holding a directory inside it. The prefix list includes
    ``dp_profiles_``, so every run's profile root is a target.

    Isolated debug ports do not help: the decision is made on directory names,
    not ports. To run alongside a live batch, point ``TMPDIR`` at a short
    private directory first -- the glob is rooted at ``tempfile.gettempdir()``,
    so the other run's ``/tmp/dp_profiles_*`` falls outside its view entirely.
    Short matters: Chrome builds ``$TMPDIR/com.google.Chrome.XXXXXX/
    SingletonSocket`` and aborts when that path passes ~108 bytes.
    """
    in_use = _profile_dirs_in_use()
    removed = 0
    for prefix in TEMP_PROFILE_PREFIXES:
        for path in glob.glob(str(Path(tempfile.gettempdir()) / (prefix + '*'))):
            if path in in_use or not Path(path).is_dir():
                continue
            try:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
            except OSError:
                pass
    if removed and not quiet:
        print(f"  🧹 清理了 {removed} 个上次运行残留的临时 profile")
    return removed


# Both scraping profiles live under one root, named for the instance that
# opens them.
MAIN_PROFILE_NAME = 'main_dir'
# The second instance is no longer PDF-only -- it also fetches supplemental
# pages, figures and API responses whenever the shared session cannot -- so
# the directory is named for the role rather than the file type. The old name
# is still swept on cleanup so an upgrade leaves nothing behind.
AUX_PROFILE_NAME = 'aux_dir'
LEGACY_AUX_PROFILE_NAME = 'pdf_dir'
# The headless Playwright browser needs a profile of its own for the same
# reason the others do: always_open_pdf_externally lives in Preferences, and
# without it Chrome renders PDFs instead of downloading them.
HEADLESS_PROFILE_NAME = 'headless_dir'

def _default_profile_root() -> Path:
    """``<tmp>/dp_profiles_xxxxxx``, one per run.

    The suffix comes from a generator seeded with the start time, so two runs
    launched together land in different directories and cannot fight over
    Chrome's profile lock. The pid goes into the seed as well: two processes
    started inside the same clock tick would otherwise draw the same suffix.

    gettempdir() rather than a literal "/tmp" so Windows and macOS get their
    own temp location; on Linux this is exactly /tmp/dp_profiles_xxxxxx.
    """
    rng = random.Random(time.time_ns() ^ (os.getpid() << 16))
    suffix = ''.join(rng.choices(string.ascii_lowercase + string.digits, k=6))
    return Path(tempfile.gettempdir()) / f'dp_profiles_{suffix}'


# Resolved once, so every call in this process agrees on where the profiles are.
DEFAULT_PROFILE_ROOT = _default_profile_root()


def profile_root() -> Path:
    """The directory holding ``main_dir``, ``aux_dir`` and ``headless_dir``."""
    raw = (os.environ.get('CHROME_PROFILE_ROOT') or '').strip()
    root = Path(raw).expanduser() if raw else DEFAULT_PROFILE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root


def scraping_profile_dir(name: str) -> Path:
    """Where one instance keeps its profile. Disposable, rebuilt every launch.

    The two names are fixed, so concurrent runs must not share a root -- the
    default root is per-run precisely so they do not. Setting
    ``CHROME_PROFILE_ROOT`` to the same path in two runs brings the clash back.
    """
    return profile_root() / name


def profile_source_dir() -> Optional[Path]:
    """The profile to seed from, or None when there isn't a usable one.

    "Usable" means the directory exists *and* holds the named profile
    subdirectory -- an empty or wrong path is treated as absent rather than
    silently producing a profile with no cookies in it.
    """
    raw = (os.environ.get('CHROME_PROFILE_SOURCE_DIR') or '').strip() \
        or CHROME_PROFILE_SOURCE_DIR
    if not raw:
        return None
    source = Path(raw).expanduser()
    profile_name = os.environ.get('CHROME_PROFILE', CHROME_PROFILE) or 'Default'
    return source if (source / profile_name).is_dir() else None


def _protected_profile_dirs() -> set:
    """Directories prepare_profile_dir() must never delete.

    Two kinds: the profile seeding copies *from*, and the user's own Chrome
    data wherever the platform puts it. Scraping profiles are never in here --
    they live under CHROME_PROFILE_ROOT, which no environment variable can
    point at a real profile without this refusing to wipe it.
    """
    protected = set()

    for raw in (CHROME_USER_DATA_DIR, CHROME_PROFILE_SOURCE_DIR,
                os.environ.get('CHROME_PROFILE_SOURCE_DIR') or ''):
        if raw:
            try:
                protected.add(Path(raw).expanduser().resolve())
            except OSError:
                pass

    if IS_WINDOWS:
        local = os.environ.get('LOCALAPPDATA', '')
        if local:
            candidates = [Path(local, 'Google', 'Chrome', 'User Data')]
        else:
            candidates = []
    else:
        candidates = [Path.home() / c for c in (
            '.config/google-chrome', '.config/chromium',
            'Library/Application Support/Google/Chrome')]
    for candidate in candidates:
        try:
            protected.add(candidate.resolve())
        except OSError:
            protected.add(candidate)
    return protected


def cleanup_profile_root(quiet: bool = False) -> bool:
    """Remove this run's scraping profiles once the browsers are gone.

    The two profile directories are always cleared. The root itself goes too
    when it is the auto-generated ``dp_profiles_xxxxxx`` -- that name belongs
    to this process and nothing else will ever look in it. A root the user
    named through ``CHROME_PROFILE_ROOT`` is left in place: it may be somewhere
    they keep other things, and deleting a path someone chose is not ours to
    do.

    A directory a live Chrome still holds is skipped rather than yanked out
    from under it, which also keeps a concurrent run that shares the root safe.

    Returns True when the root itself was removed.
    """
    raw = (os.environ.get('CHROME_PROFILE_ROOT') or '').strip()
    root = Path(raw).expanduser() if raw else DEFAULT_PROFILE_ROOT
    if not root.exists():
        return False

    in_use = _profile_dirs_in_use()

    def _held(path: Path) -> bool:
        target = str(path)
        return any(u == target or u.startswith(target + os.sep) for u in in_use)

    for name in (MAIN_PROFILE_NAME, AUX_PROFILE_NAME, LEGACY_AUX_PROFILE_NAME,
                 HEADLESS_PROFILE_NAME):
        d = root / name
        if d.exists() and not _held(d):
            shutil.rmtree(d, ignore_errors=True)

    if raw:                                  # user-chosen root: leave it alone
        return False
    if _held(root):
        return False
    try:
        shutil.rmtree(root, ignore_errors=True)
    except OSError:
        return False
    removed = not root.exists()
    if removed and not quiet:
        print(f"  🧹 已清理本次运行的 profile 目录: {root}")
    return removed


def prepare_profile_dir(target: Path, download_dir: str = '',
                        quiet: bool = False) -> bool:
    """Put *target* into a known-clean state, ready for Chrome to open.

    One rule, both instances (the shared browser and the throwaway PDF one):

        wipe the directory, then either seed it from the real profile or
        leave it empty -- seeded when FRESH_PROFILE is off and a usable
        CHROME_PROFILE_SOURCE_DIR exists, empty otherwise.

    Wiping first is the point. A profile Chrome has driven under CDP carries
    the automation traces of that session, and reusing it is what makes a run
    that asked for a clean start get refused anyway; recreating it every time
    means a session can never inherit the previous one's state.

    Returns True when the profile was seeded, False when it starts empty.
    """
    target = Path(target).expanduser()
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target

    if resolved in _protected_profile_dirs():
        # Never wipe the user's own Chrome data. Callers resolve their target
        # before getting here, so this is a guard against a misconfiguration
        # (CHROME_USER_DATA_DIR left unset, say), not an expected path.
        raise ValueError(
            f"拒绝清空 {resolved}：这是真实 Chrome profile，不是抓取用的目录"
        )

    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)

    profile_name = os.environ.get('CHROME_PROFILE', CHROME_PROFILE) or 'Default'
    source = None if FRESH_PROFILE else profile_source_dir()

    seeded = False
    if source is not None:
        seeded = seed_profile(target, source, profile_name)
        if not seeded and not quiet:
            print(f"  ⚠️  播种失败，使用空 profile: {target}")
    elif not quiet:
        reason = 'FRESH_PROFILE=1' if FRESH_PROFILE else '无可用的 CHROME_PROFILE_SOURCE_DIR'
        print(f"  🆕 空 profile（{reason}）: {target}")

    write_chrome_preferences(target, profile_name=profile_name,
                             download_dir=download_dir, quiet=quiet)
    return seeded


def launch_chrome(headless: bool = False, return_details: bool = False):
    """Start the shared scraping Chrome and wait for its debugging port.

    The profile is always rebuilt first -- see :func:`prepare_profile_dir` --
    so a session never opens on a directory a previous run left behind.
    It always lives at ``<CHROME_PROFILE_ROOT>/main_dir``. There is no option
    to open the user's real profile: this function wipes what it is about to
    open, and :func:`prepare_profile_dir` refuses outright if the target
    resolves to the user's own Chrome data.
    """
    user_data_dir = scraping_profile_dir(MAIN_PROFILE_NAME)

    try:
        prepare_profile_dir(user_data_dir)
    except ValueError as exc:
        # The configured directory is the real profile. Fall back to a
        # throwaway one rather than refusing to run -- or wiping it.
        print(f"  ⚠️  {exc}")
        user_data_dir = Path(tempfile.mkdtemp(prefix='chrome_fallback_'))
        prepare_profile_dir(user_data_dir)
    user_data_dir = str(user_data_dir)

    print(f"正在启动 Chrome ({CHROME_PATH})...")
    proc = spawn_chrome(user_data_dir, CHROME_DEBUG_PORT, headless)
    if proc is None:
        return (None, user_data_dir, True) if return_details else None

    print(f"✓ Chrome 已启动 (PID: {proc.pid})")
    print(f"✓ 远程调试端口: {CHROME_DEBUG_PORT}")

    # launch_chrome() is called from sync code, so the async waiter is driven
    # here rather than awaited.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(wait_for_cdp_port(CHROME_DEBUG_PORT, process=proc))
    else:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and not cdp_port_open(CHROME_DEBUG_PORT):
            if proc.poll() is not None:
                break
            time.sleep(0.5)

    return (proc, user_data_dir, True) if return_details else proc


def _default_aux_port() -> int:
    """Port for the second Chrome instance.

    ``CHROME_AUX_DEBUG_PORT`` first, then the older ``CHROME_PDF_DEBUG_PORT``
    so a launch script written before the rename keeps working.
    """
    for var in ('CHROME_AUX_DEBUG_PORT', 'CHROME_PDF_DEBUG_PORT'):
        raw = (os.environ.get(var) or '').strip()
        if not raw:
            continue
        try:
            port = int(raw)
        except ValueError:
            continue
        if 1 <= port <= 65535:
            return port
    return 9333


def _pick_free_port(preferred: int) -> int:
    """*preferred*, or the next port nothing is listening on.

    Two extractions running at once would otherwise attach to each other's
    throwaway browser.
    """
    port = preferred
    for _ in range(20):
        if not cdp_port_open(port):
            return port
        port += 1
    return preferred


# ==========================================================================
# 2-3. CDP primitives and Cloudflare challenge handling
# ==========================================================================

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
#
# ⚠️ script[src*="cdn-cgi/challenge-platform"] does NOT belong in the
# unguarded list. That is Cloudflare's bot-management precursor script, and it
# is served on every protected page, including fully loaded articles -- the
# science.org page for 10.1126/science.252.5004.384 carries it at 590KB with
# 6786 characters of body text. Having it here made is_challenge permanently
# true on that whole publisher, which silently defeats any check written as
# "not is_challenge". The second branch below already covers the same script
# and is guarded on body length, which is what tells a challenge page (short)
# apart from an article that merely ships the script (long).
#
# The id selectors stay unguarded on purpose: they matched zero times on that
# same loaded article page, so they really do only appear on a challenge.
_CHALLENGE_DOM_JS = r"""(function () {
    try {
        if (document.querySelector(
                '#challenge-form, #challenge-running, #challenge-stage, ' +
                '#cf-challenge-running, [id^="cf-chl"]')) {
            return true;
        }
        return /cdn-cgi\/challenge-platform/.test(document.documentElement.innerHTML)
               && document.body && document.body.innerText.length < 2000;
    } catch (e) {
        return false;
    }
})()"""


#: Where :func:`_send` files the CDP events it would otherwise discard.
#:
#: A ContextVar rather than a module global on purpose: the article preload
#: and a PDF download both run through :func:`bypass_cloudflare_cdp`, and under
#: asyncio they can be separate tasks in flight at once. A global would mix one
#: page's responses into the other's dict, which is worse than capturing
#: nothing because the mixture still looks like a valid capture.
_CDP_EVENT_SINK: contextvars.ContextVar = contextvars.ContextVar(
    '_cdp_event_sink', default=None)


def _record_cdp_event(sink: dict, msg: dict) -> None:
    """Note one ``Network.responseReceived`` event, keyed by requestId.

    Only the metadata is available here -- the body has to be asked for
    separately, which is what :func:`_harvest_document_bodies` does once the
    page has settled. Keying by requestId is what keeps the *two* responses a
    protected publisher sends for the same URL (one before its bot check, one
    after) as two distinct entries instead of one overwriting the other.
    """
    if (msg.get("method") or "") != "Network.responseReceived":
        return
    params = msg.get("params") or {}
    rid = params.get("requestId")
    if not rid:
        return
    resp = params.get("response") or {}
    sink[rid] = {
        "url": resp.get("url") or "",
        "type": params.get("type") or "",
        "status": resp.get("status"),
        "mimeType": resp.get("mimeType") or "",
        "body": None,
    }


#: URL substrings whose XHR/Fetch bodies are harvested alongside the
#: documents. Harvested by default. These are the endpoints a publisher's
#: own page fetches during load and that a handler would otherwise request a
#: second time: ScienceDirect's body/references/metadata calls, IEEE's REST document
#: calls. Measured: skipping the duplicate request is the whole point, and
#: both were observed being issued by the page itself.
#:
#: ⚠️ SPIE is deliberately absent. Its fulltext POST is issued by *us* from
#: inside the page, not by the landing page, so there is nothing to capture --
#: adding /api/ here would harvest unrelated traffic for no benefit.
_DEFAULT_API_HARVEST = ('/sdfe/arp/', '/rest/document/')


def _api_harvest_patterns() -> list:
    """URL substrings whose XHR/Fetch bodies are worth keeping.

    On by default. ``DP_HARVEST_API`` overrides the list; setting it to ``0``,
    ``off`` or ``none`` disables API harvesting entirely and leaves only
    documents, which is the pre-2026-09 behaviour.
    """
    raw = (os.environ.get('DP_HARVEST_API') or '').strip()
    if not raw:
        return list(_DEFAULT_API_HARVEST)
    if raw.lower() in ('0', 'off', 'none', 'false', 'no'):
        return []
    return [p.strip().lower() for p in raw.split(',') if p.strip()]


async def _harvest_document_bodies(ws, sink: dict, limit: int = 48) -> int:
    """Fill in the bodies of the responses worth keeping from *sink*.

    ⚠️ Must run while the socket is still open and soon after the page
    settles: Chrome keeps response bodies in a per-renderer buffer and evicts
    them, so a body asked for after the ``async with`` has closed is simply
    gone. That is why every exit of :func:`bypass_cloudflare_cdp` calls this
    before returning rather than leaving it to the caller.

    ``Document`` responses are always fetched. XHR/Fetch bodies are fetched
    only when their URL matches :func:`_api_harvest_patterns`, which is empty
    unless ``DP_HARVEST_API`` is set -- asking for every image, stylesheet and
    font would add dozens of round trips per paper for bytes nothing reads.

    ⚠️ The cap is 48, not the 12 that sufficed for documents alone: measured on
    ScienceDirect 10.1016/j.cocom.2026.e01326, one article issues 20 matching
    API calls (each endpoint twice, with different entitledTokens). A cap sized
    for documents would have silently dropped the tail.
    """
    fetched = 0
    for rid, entry in list(sink.items()):
        if entry.get("body") is not None:
            continue
        _type_ok = entry.get("type") == "Document"
        if not _type_ok:
            _pats = _api_harvest_patterns()
            _url = (entry.get("url") or "").lower()
            _type_ok = bool(_pats) and any(p in _url for p in _pats)
        if not _type_ok:
            continue
        if fetched >= limit:
            break
        try:
            got = await _send(ws, "Network.getResponseBody", {"requestId": rid})
        except Exception:
            continue  # evicted, or the request never completed
        body = got.get("body")
        if body is None:
            continue
        if got.get("base64Encoded"):
            try:
                body = base64.b64decode(body).decode("utf-8", "replace")
            except Exception:
                continue
        entry["body"] = body
        fetched += 1
    return fetched


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
            # 事件消息。此前直接丢弃 —— 而这里正是整个模块唯一能看到 CDP 事件
            # 的地方（_send 就是消息泵，没有另一个读 socket 的地方），所以
            # 「预载期间记录响应」只能挂在这一行上。
            _sink = _CDP_EVENT_SINK.get()
            if _sink is not None:
                try:
                    _record_cdp_event(_sink, resp)
                except Exception:
                    pass
            continue
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
    already_open: bool = False,
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
    # "responses" is seeded here, at the one place the dict is built, so that
    # every one of this function's exits -- the early "no ws_url" return, the
    # five success paths, the timeout and the outer except -- hands back a dict
    # that has the key. Adding it only on the happy path would leave callers
    # doing .get("responses") silently empty on the others, which is the
    # failure shape this repo keeps getting caught by.
    result = {"success": False, "target_id": None, "ws_url": None,
              "responses": {}}

    print(f"\n  🛡️  [纯CDP模式] 打开 {url[:80]}...")

    # 找一个可复用的 tab（优先 about:blank，其次任意 page）
    ws_url = None
    created_at_target = already_open
    # True only for an about:blank tab this function created itself.
    # 📌 Measured: assigning location.href on such a tab does nothing --
    # instrumenting the wait showed 10.0s of polling with zero Document
    # responses, and Page.navigate did the whole job afterwards. Trying JS
    # first there bought nothing and cost ~10s an article.
    blank_tab_we_opened = False
    try:
        if already_open:
            # Chrome was launched with this URL on its command line, so the
            # page is already loading in its own tab. Attach to that tab and
            # navigate nothing: the fetch has happened without a single CDP
            # command touching it, which is the whole point of using a
            # throwaway browser.
            target_host = urllib.parse.urlparse(url).netloc.lower()
            for _ in range(20):
                try:
                    with urllib.request.urlopen(
                            f"http://localhost:{debug_port}/json", timeout=5) as resp:
                        for t in json.loads(resp.read().decode()):
                            if t.get("type") != "page":
                                continue
                            host = urllib.parse.urlparse(
                                t.get("url", "")).netloc.lower()
                            if host and host == target_host:
                                ws_url = t.get("webSocketDebuggerUrl")
                                break
                except Exception:
                    pass
                if ws_url:
                    break
                await asyncio.sleep(0.5)
            if ws_url:
                print(f"  📄 附着到启动时打开的 tab（未经 CDP 导航）")
            else:
                print(f"  ⚠️  未找到启动时打开的 tab，回退到常规流程")
                created_at_target = False
    except Exception:
        created_at_target = already_open and bool(ws_url)

    try:
        targets_url = f"http://localhost:{debug_port}/json"
        with urllib.request.urlopen(targets_url, timeout=5) as resp:
            targets = json.loads(resp.read().decode())

        # 优先选 New Tab（chrome://newtab）—— 起始环境最自然，
        # Cloudflare 挑战会正常渲染 iframe 并自动通过
        for t in targets:
            if ws_url:
                break
            if t.get("type") == "page" and "chrome://newtab" in t.get("url", "").lower():
                ws_url = t.get("webSocketDebuggerUrl")
                print(f"  📄 复用 New Tab (chrome://newtab)")
                break
        
        # 找不到 New Tab 就新建一个
        # 注意：绝不复用 about:blank tab——它很可能是 Playwright 创建的，
        # 带有自动化指纹，会导致 Cloudflare 直接 403
        if not ws_url:
            # Create the tab at about:blank and navigate *after* attaching.
            #
            # ⚠️ This reverses a deliberate earlier choice, so the reasoning
            # matters. The tab used to be created at the target URL, because
            # "letting Chrome open the URL itself is an ordinary browser
            # navigation, not an automated one". True -- but it also means
            # Chrome starts fetching the main document before this function
            # has a WebSocket, let alone Network.enable, and the document's
            # Network.responseReceived can fire before we can hear it. Then
            # result["responses"] has every subresource and no document.
            # Measured on ScienceDirect 10.1016/j.rinp.2021.104097: 110
            # response events, zero documents, so page_raw.html was not
            # written. EPL happened to win the race; that is all it proved.
            #
            # Navigating after enabling costs a JS-initiated navigation
            # instead of a browser-initiated one. The else-branch below
            # already prefers ``location.href`` over Page.navigate for
            # exactly that reason, and already verifies arrival and falls
            # back, so this reuses a hardened path rather than adding one.
            #
            # ⚠️ Do NOT "fix" this by creating at chrome://newtab: a WebUI
            # target refuses JS navigation to the open web *silently*, and the
            # poll loop then spins on title='New Tab' body=0 until timeout.
            # about:blank is not a WebUI page. Avoiding about:blank applies to
            # *reusing* someone else's blank tab (usually Playwright's, which
            # carries an automation fingerprint) -- not to one we open.
            print(f"  🔧  新建空白 tab（附着并启用 Network 后再导航）")
            ws_url = await _create_new_tab(debug_port, "about:blank")
            if ws_url:
                created_at_target = False
                blank_tab_we_opened = True
                print(f"  📄 已新建空白 tab")
                await asyncio.sleep(0.5)
        
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

            # 启用必要的域。
            #
            # ⚠️ Runtime.enable 被刻意省掉：它是业界公认的 CDP 检测点（开启后
            # DevTools 会序列化控制台对象，触发 Error.stack 的 getter，Radware /
            # DataDome 都靠这个认出附着的调试会话）。而我们**根本用不到它**——
            # Runtime.evaluate 是命令，不需要 enable；enable 只负责推送事件。
            #
            # 同理 Page.enable / Network.enable 也是多余的（_send 直接丢弃所有
            # 无 id 的事件消息，本模块从不消费 CDP 事件），暂按原样保留，
            # 要不要一并清掉另行决定。
            await _send(ws, "Page.enable")
            await _send(ws, "Network.enable")

            # ⚠️ Network.enable is no longer optional. The comment above used to
            # call it redundant and float removing it; it is now what makes the
            # response capture below possible, so deleting it would silently
            # empty every capture.
            #
            # The sink IS result["responses"], so the events recorded here and
            # the bodies filled in later land in one dict with no syncing step.
            _CDP_EVENT_SINK.set(result["responses"])

            if created_at_target:
                # The tab is already on (or loading) the target URL: either
                # Chrome opened it at startup, or the tab was created there.
                print(f"  🚀  tab 已在目标页面，无需导航")
                await asyncio.sleep(2)
            elif blank_tab_we_opened:
                # Straight to the command that works; see blank_tab_we_opened.
                print(f"  🚀  导航到目标页面 (Page.navigate，空白 tab)...")
                await _send(ws, "Page.navigate", {"url": url})
                await asyncio.sleep(3)
                created_at_target = True   # navigation issued; skip the retry
            else:
                # 用 JS location.href 导航（比 Page.navigate 指纹更自然）
                print(f"  🚀  导航到目标页面 (JS location.href)...")
                await _send(ws, "Runtime.evaluate", {
                    "expression": f"location.href = {json.dumps(url)}"
                })

                # 等待页面开始加载
                await asyncio.sleep(3)

            # Verify the renderer actually left, and force it if not.
            #
            # A chrome:// page refuses JS navigation to the open web and does
            # so *silently*: the CDP target list reports the new URL while the
            # renderer stays on chrome://new-tab-page. The tab this function
            # prefers is exactly such a page, so on a browser whose only tab
            # is the new-tab page the poll loop below would spin on
            # title='New Tab' body=0 until it timed out.
            #
            # The check is "did we reach the target host", not "are we still
            # on chrome://": Runtime.evaluate against a WebUI target can come
            # back empty or as an error, and an unreadable location is just as
            # much a reason to force the navigation as a chrome:// one.
            if not created_at_target:
                _nav_t0 = time.monotonic()
                # This path is for a *reused* tab. Tabs we open ourselves at
                # about:blank skip it entirely -- see blank_tab_we_opened.
                #
                # 📌 On such a blank tab the JS assignment produced no Document
                # response for a full 10s of polling, yet it was not inert: it
                # stayed queued and ran anyway, so Page.navigate made the page
                # load a *second* time. Measured on
                # 10.1016/j.cocom.2026.e01326 -- skipping the JS attempt took
                # Document responses from 4 to 2 and the page's own API calls
                # from 20 to 10, one round per endpoint, matching what DevTools
                # shows for a human visit.
                #
                # The poll waits on a signal it can observe: a committed
                # Document from any host other than the starting one proves the
                # renderer left. Do not test against urlparse(url).netloc --
                # url is a doi.org link that redirects away, so that comparison
                # is false by construction.
                start_host = urllib.parse.urlparse(url).netloc.lower()
                arrived = False
                current = ""
                for _ in range(20):          # up to ~10s, 0.5s apart
                    for _e in result["responses"].values():
                        if (_e.get("type") or "") != "Document":
                            continue
                        _u = (_e.get("url") or "")
                        if not _u.startswith(("http://", "https://")):
                            continue
                        _h = urllib.parse.urlparse(_u).netloc.lower()
                        if _h and _h != start_host:
                            arrived = True
                            current = _u
                            break
                    if arrived:
                        break
                    await asyncio.sleep(0.5)
                # ⚠️ Timestamps, because three fixes in a row were argued from
                # a log with none. Adjacent lines were read as "instant" when
                # one of them sat behind a 3s sleep, which sent the diagnosis
                # after the wrong cause twice.
                print(f"  ⏱  导航判定耗时 {time.monotonic() - _nav_t0:.1f}s，"
                      f"已捕获 Document {sum(1 for _e in result['responses'].values() if (_e.get('type') or '') == 'Document')} 条")

                if arrived:
                    print(f"  ✓ JS 导航已到达目标页面（{current[:60]}）")
                else:
                    print(f"  ↪ JS 导航未生效 (当前 {current or '未知'})，"
                          "改用 Page.navigate")
                    await _send(ws, "Page.navigate", {"url": url})
                    await asyncio.sleep(3)

            deadline = asyncio.get_event_loop().time() + timeout_s
            challenge_detected = False
            challenge_rounds = 0  # 挑战页已经过了多少轮（用于判断 iframe 是否延迟加载）
            turnstile_tried = False
            last_status = ""

            # 下面这四个都在循环体内赋值，超时那行要读它们来如实说明失败原因。
            # 先给初值：首轮轮询若在赋值前抛异常、或 deadline 已过导致循环一次
            # 都没跑，超时分支就会 NameError —— 一条「说清楚为什么失败」的改动
            # 反而把程序弄崩。
            title = ""
            body_text = ""
            is_challenge = False
            has_cf = False

            # ── PDF 模式：监控下载目录新文件作为「挑战真正通过」的实体判据 ──
            # Chrome 已配置 always_open_pdf_externally=True + prompt_for_download=False，
            # 只有真正绕过挑战拿到 PDF 才会自动落盘新文件（.pdf / .crdownload ）。
            # 相比 cf_clearance cookie（会被正文页提前写入同一 profile 而污染），
            # 下载文件落盘是「实体证据」，绝不误判。调用方传入 download_dir 时启用。
            _dl_baseline = set()
            if pdf_mode and download_dir:
                try:
                    if os.path.isdir(download_dir):
                        # Deliberately NOT seeded with what is already
                        # there. The directory is created empty for each
                        # attempt, so a file present now is this very
                        # download -- it landed while we were attaching.
                        # Counting it as "pre-existing" made the loop wait
                        # for a new file that could never appear, burning
                        # the entire timeout (600s under launch.sh) on a
                        # PDF that was already on disk. Optica, which
                        # renders the file in a few seconds, hit this
                        # every time.
                        _dl_baseline = set()
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
                        await _harvest_document_bodies(ws, result["responses"])
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
                            print(f"  ✅ [下载模式] 检测到下载事件（新文件: {_new_dl}），挑战真实通过")
                            target_id = _current_ws_url.rstrip("/").split("/")[-1]
                            result["success"] = True
                            result["target_id"] = target_id
                            result["ws_url"] = _current_ws_url
                            result["downloaded_file"] = os.path.join(download_dir, _new_dl) if download_dir else _new_dl
                            await _harvest_document_bodies(ws, result["responses"])
                            return result
                        # 未检测到下载文件：即使 cookie 在也不判成功（避免误判），继续等
                        if not has_cf and not is_challenge:
                            # 无下载监控且 cookie 也没有，保持等待
                            pass
                        # 退路：无 download_dir 时沿用 cookie 判据（向后兼容）
                        if has_cf and not is_challenge and not download_dir:
                            print(f"  ✅ [下载模式] cf_clearance 已获取，挑战通过")
                            target_id = _current_ws_url.rstrip("/").split("/")[-1]
                            result["success"] = True
                            result["target_id"] = target_id
                            result["ws_url"] = _current_ws_url
                            await _harvest_document_bodies(ws, result["responses"])
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
                        await _harvest_document_bodies(ws, result["responses"])
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
                        await _harvest_document_bodies(ws, result["responses"])
                        return result

                except Exception as e:
                    print(f"  ⚠️  轮询出错: {e}")

                await asyncio.sleep(check_interval)

            # 超时。两种截然不同的失败，此前都被报成「挑战未通过」：
            #   1) 确实卡在挑战页
            #   2) 页面其实早就正常打开了，只是始终没有文件落盘
            # pdf 模式下第 2 种最常见的原因是**没有访问权限**（出版商把
            # /doi/pdf/... 重定向回落地页），跟 Cloudflare 毫无关系。照旧报成
            # 挑战未通过会把人引去查反爬，而该查的是订阅权限。
            # ⚠️ 判据**不能**用 is_challenge。实测 science.org 的每一个页面——
            # 包括完整加载的文章页（590KB、正文 6786 字）——都挂着
            # <script src="/cdn-cgi/challenge-platform/scripts/precursor/main.js">，
            # 而 _CHALLENGE_DOM_JS 第一分支的 querySelector 没有正文长度护栏，
            # 于是 is_challenge 恒为 True，这个分支永远进不来。改用正向信号：
            # 标题不是挑战页、正文够长，就说明页面本身是好的。
            page_looks_real = (
                title
                and not _is_challenge_title(title.lower())
                and len(body_text) > 2000
            )
            if pdf_mode and download_dir and page_looks_real:
                print(f"  ⏰  {timeout_s}s 内没有文件落盘，但页面是打开的"
                      f"（title={title[:40]!r}, body={len(body_text)} 字, "
                      f"cf={'✓' if has_cf else '✗'}）")
                print(f"      → 不是挑战未通过；通常是该文章没有访问权限/需订阅")
            else:
                print(f"  ⏰  Cloudflare 挑战未在 {timeout_s}s 内通过")
            # Harvest even on timeout: a page that never satisfied the pass
            # test can still have delivered a perfectly good document, and the
            # caller may prefer it to nothing.
            await _harvest_document_bodies(ws, result["responses"])
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


# ==========================================================================
# 4. Throwaway instance
# ==========================================================================


async def open_url_via_cdp(url: str, port: int, *, expected_doi: str = '',
                           pdf_mode: bool = False, download_dir: str = '',
                           timeout_s: int = 60, already_open: bool = False) -> dict:
    """Open *url* over raw CDP on an already-running Chrome at *port*.

    The shared "navigate and clear Cloudflare without Playwright attached"
    step, used both by the article-page preload (which targets the batch's
    shared browser) and by :meth:`FreshChromeSession.open_url` (which targets
    the throwaway one), so the challenge handling cannot drift between them.
    """
    return await bypass_cloudflare_cdp(
        url=url,
        debug_port=port,
        timeout_s=timeout_s,
        wait_for_content=not pdf_mode,
        expected_doi=expected_doi,
        pdf_mode=pdf_mode,
        download_dir=download_dir,
        already_open=already_open,
    )


async def fetch_page_html_via_cdp(ws_url: str = '', *, port: int = None,
                                  timeout_s: float = 15.0) -> str:
    """Read a tab's rendered HTML over raw CDP.

    Keeping this out of ``bypass_cloudflare_cdp`` means none of its five
    success paths grows an extra step, and a caller that only wants a
    downloaded file pays nothing for it.

    ``ws_url`` is optional on purpose. That function only fills it in on the
    paths it considers successful; when it times out it hands back the dict it
    started with, ``ws_url=None`` -- and a timeout is exactly when the page may
    still be perfectly loaded and worth reading (its pass test wants a matching
    DOI or 5000+ characters of body, which a short supplemental listing never
    has). So fall back to asking the debug port for the tab directly.

    Returns '' on any failure, so the caller can fall back a tier.

    ⚠️ The HTML of a challenge page is still HTML: the caller must check that
    what came back is what it asked for, not merely that it is non-empty.
    """
    if not ws_url and port:
        try:
            ws_url = await _get_page_ws_url(port) or ''
        except Exception:
            ws_url = ''
    if not ws_url:
        return ''
    try:
        # max_size=None: article pages run well past the default 1 MB frame
        # cap (a SPIE full text came back at 926 KB of JSON alone).
        async with websockets.connect(ws_url, max_size=None,
                                      open_timeout=10) as ws:
            result = await asyncio.wait_for(
                _send(ws, "Runtime.evaluate", {
                    "expression": "document.documentElement.outerHTML",
                    "returnByValue": True,
                }),
                timeout=timeout_s,
            )
        html = (result.get("result") or {}).get("value") or ''
        return html if isinstance(html, str) else ''
    except Exception as exc:
        print(f"  ⚠️  CDP 取页面 HTML 失败: {type(exc).__name__}: {str(exc)[:80]}")
        return ''


class FreshChromeSession:
    """A disposable Chrome instance with a copy of the real profile.

    Nothing here attaches Playwright. The whole point is that the browser
    stays fingerprint-clean, so the caller drives it over raw CDP (via
    the helpers above) or simply reads whatever the page downloaded.
    """

    def __init__(self, port: Optional[int] = None,
                 download_dir: str = '',
                 keep_profile: bool = False,
                 headless: bool = False):
        self.port = _pick_free_port(port or _default_aux_port())
        self.download_dir = download_dir
        self.keep_profile = keep_profile
        # A headless run must not pop a window for every paper. Headed runs
        # keep the visible browser: that is the whole reason this throwaway
        # instance exists for them.
        self.headless = headless
        self.process: Optional[subprocess.Popen] = None
        self.profile_dir: Optional[Path] = None
        self._owns_profile = False
        self._started_empty = True
        self.result: dict = {}

    @property
    def cdp_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------------

    def _make_profile(self) -> Path:
        """``<CHROME_PROFILE_ROOT>/aux_dir`` -- rebuilt on every launch."""
        target = scraping_profile_dir(AUX_PROFILE_NAME)
        self._owns_profile = True
        try:
            self._started_empty = not prepare_profile_dir(
                target, download_dir=self.download_dir, quiet=True)
        except ValueError as exc:
            print(f"  ⚠️  {exc}")
            target = Path(tempfile.mkdtemp(prefix='chrome_aux_'))
            self._started_empty = not prepare_profile_dir(
                target, download_dir=self.download_dir, quiet=True)
        return target

    async def start(self, start_url: str = 'about:blank') -> bool:
        """Launch the browser, optionally straight at *start_url*.

        Opening the URL from the command line means Chrome performs the
        navigation itself, exactly as it would for a user clicking a link:
        no CDP command participates in the page load.
        """
        self.profile_dir = self._make_profile()
        if self.download_dir:
            Path(self.download_dir).mkdir(parents=True, exist_ok=True)

        flavour = '全新空 profile' if self._started_empty else '真实 profile 副本'
        mode = '无头' if self.headless else '有头'
        print(f"  🌐 启动独立 Chrome (端口 {self.port}, {mode}, {flavour})...")
        self.process = spawn_chrome(self.profile_dir, self.port,
                                    headless=self.headless,
                                    start_url=start_url)
        if self.process is None:
            return False
        return await wait_for_cdp_port(self.port, process=self.process)

    async def open_url(self, url: str, *, expected_doi: str = '',
                       pdf_mode: bool = False, timeout_s: int = 60,
                       already_open: bool = False) -> dict:
        """Open *url*, clearing any Cloudflare challenge.

        With *already_open* the browser was launched on this URL, so this
        only attaches and watches the challenge through.
        """
        self.result = await open_url_via_cdp(
            url, self.port,
            expected_doi=expected_doi,
            pdf_mode=pdf_mode,
            download_dir=self.download_dir,
            timeout_s=timeout_s,
            already_open=already_open,
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


# How many times to reload the referring page when a bot manager has taken it
# over. Three, because the interstitial is transient: in the observed run the
# attempt that finally worked had its referring page load on the first try.
# Retrying here costs one navigation; leaving it to the outer retry costs two
# Chrome launches and two captcha waits before this rung is reached again.
def _referer_page_retries() -> int:
    raw = (os.environ.get('DP_REFERER_PAGE_RETRIES') or '').strip()
    try:
        return max(1, int(raw)) if raw else 1
    except ValueError:
        return 1


# ⚠️ Default 1, i.e. check the premise and decline to click -- do NOT reload.
#
# Reloading was this constant's original purpose, on the reasoning that the
# interstitial is transient. That reasoning was WRONG, and the data says so:
# across five attempts the referring page was reloaded ten times and landed
# back on the bot manager every single time. The reloads bought nothing and
# added request volume, which is the one thing most likely to make a
# rate-scored block worse. The premise check itself is still right and free.
_REFERER_PAGE_RETRIES = _referer_page_retries()


async def _wait_for_committed_url(ws, timeout_s: float) -> str:
    """The tab's URL once a real document has committed, or '' on timeout.

    ⚠️ ``/json`` reports a *loading* tab's pending URL, so a target that looks
    like the referring page there can still be the pre-commit ``about:blank``
    document. A click handler armed on that document dies the instant the real
    page commits, and the click then lands on a page with no handler -- which
    is exactly how this rung once reported "no file" while quietly doing
    nothing. ``readyState`` is checked too, so the caller gets a document that
    has finished parsing.
    """
    for _ in range(int(max(3.0, min(20.0, timeout_s)))):
        probe = await _send(ws, "Runtime.evaluate", {
            "expression": "location.href + '|' + document.readyState",
            "returnByValue": True})
        value = (probe.get('result') or {}).get('value') or ''
        href, _, ready = value.partition('|')
        if (href and not href.startswith(('about:', 'chrome://'))
                and ready in ('interactive', 'complete')):
            return href
        await asyncio.sleep(1)
    return ''


_REFERER_CLICK_JS = """(() => {
    document.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopImmediatePropagation();
        location.href = %s;
    }, {once: true, capture: true});
    return JSON.stringify({
        x: Math.max(4, Math.floor(window.innerWidth / 2)),
        y: Math.max(4, Math.floor(window.innerHeight / 2))
    });
})()"""


async def _download_via_referer_click(session, referer_url: str, target_url: str,
                                      download_dir: str,
                                      timeout_s: float) -> dict:
    """Reach *target_url* by clicking through from *referer_url*.

    Chrome is already open on the referring page, loaded by its own startup
    navigation. This attaches once, arms a capture-phase one-shot click
    handler, and dispatches a trusted click. The handler cancels whatever the
    page would have done and navigates to the target instead.

    Measured against a local server, the resulting request is byte-identical
    to a human clicking a link: Referer set to the referring page,
    Sec-Fetch-Site: same-origin, and -- the part that matters --
    ``Sec-Fetch-User: ?1``. Launching straight at the target URL sends no
    Referer and Sec-Fetch-Site: none, which is what a bot manager sees as a
    request that came from nowhere.

    Two details are load-bearing. ``capture: true`` puts the handler ahead of
    the page's own, and ``preventDefault`` stops it, so the click may land on
    any element -- including a link to a PDF *viewer*, which is exactly what
    publishers like Wiley put there -- without going where the page wanted.
    And ``preventDefault`` does not consume the user activation, so the
    navigation still counts as user-initiated; that was verified, not assumed.
    """
    result = {'success': False, 'target_id': None, 'ws_url': None}

    ws_url = ''
    deadline = time.monotonic() + min(20.0, timeout_s)
    while time.monotonic() < deadline and not ws_url:
        try:
            with urllib.request.urlopen(
                    f"http://localhost:{session.port}/json", timeout=5) as resp:
                for target in json.loads(resp.read().decode()):
                    if target.get('type') != 'page':
                        continue
                    page_url = (target.get('url') or '')
                    if page_url.startswith(('about:', 'chrome://')):
                        continue
                    ws_url = target.get('webSocketDebuggerUrl') or ''
                    break
        except Exception:
            pass
        if not ws_url:
            await asyncio.sleep(0.5)

    if not ws_url:
        print("  ⚠️  referer 页面未就绪，放弃点击跳转")
        return result

    try:
        async with websockets.connect(ws_url, max_size=10 * 1024 * 1024,
                                      open_timeout=10) as ws:
            # No Page.enable / Network.enable here. Those exist only to start
            # event pushes, and _send discards every message without an id --
            # this module consumes no CDP events at all. On the rung whose
            # point is to touch the page as little as possible, two commands
            # that buy nothing are two commands worth not sending.
            # Runtime.evaluate and Input.dispatchMouseEvent are commands and
            # need no domain enabled.
            # (The about:blank pre-commit race this used to describe now lives
            # with the code that handles it -- see _wait_for_committed_url.)
            #
            # The premise of this rung is that the click departs from the
            # *referring page*. When that page is itself challenged, clicking
            # from the captcha sends a request whose Referer is the captcha --
            # worse than sending none, and it silently burns the attempt.
            #
            # ⚠️ This is not an edge case. Measured on IOP 10.1088/1361-6587/
            # aaa57d: three consecutive outer attempts died exactly here, each
            # after paying for two Chrome launches and two 20 s captcha waits,
            # and the fourth succeeded for no reason other than that its
            # referring page happened to load. Detecting it earlier is not what
            # fixes that -- reloading the referring page *inside this rung* is,
            # because the alternative is restarting the whole ladder.
            before_url = ''
            for attempt in range(_REFERER_PAGE_RETRIES):
                landed = await _wait_for_committed_url(ws, timeout_s)
                if not landed:
                    print("  ⚠️  来路页未在预期时间内加载完成，放弃点击跳转")
                    return result
                if not url_looks_like_bot_challenge(landed):
                    before_url = landed
                    break
                will_reload = attempt + 1 < _REFERER_PAGE_RETRIES
                # Say what is about to happen, not what the loop is called:
                # printing "重新加载" on the last iteration -- the only one at
                # the default of 1 -- claimed a reload that never came.
                print(f"  🚧 来路页被拦截器接管（{landed[:64]}）"
                      + (f"，重新加载来路页 "
                         f"[{attempt + 1}/{_REFERER_PAGE_RETRIES}]"
                         if will_reload else ""))
                if not will_reload:
                    break
                try:
                    await _send(ws, "Page.navigate", {"url": referer_url})
                except Exception:
                    break
                await asyncio.sleep(2)

            if not before_url:
                print("  ⚠️  来路页始终停在拦截页 —— 本层前提不成立，不做无谓点击")
                return result

            print(f"  📄 来路页实际停在: {before_url[:90]}")

            armed = await _send(ws, "Runtime.evaluate", {
                "expression": _REFERER_CLICK_JS % json.dumps(target_url),
                "returnByValue": True,
            })
            box = json.loads((armed.get('result') or {}).get('value') or '{}')
            print(f"  🖱️  自 referer 页点击跳转 → {target_url[:70]}")
            await _click_at_cdp(ws, box.get('x', 8), box.get('y', 8))

            # Did the click actually navigate, and to where? Without this the
            # two failure modes are indistinguishable: a request that was made
            # and refused (lands on the bot-manager domain) versus a click
            # that never navigated at all (still on the referring page).
            for _ in range(8):
                await asyncio.sleep(1)
                try:
                    now = await _send(ws, "Runtime.evaluate", {
                        "expression": "location.href + ' | ' + (document.title||'')",
                        "returnByValue": True})
                    seen = (now.get('result') or {}).get('value') or ''
                except Exception:
                    seen = '(tab 已关闭 —— 通常意味着导航变成了下载)'
                    print(f"  📄 点击后: {seen}")
                    break
                if seen.split(' | ')[0] != before_url:
                    print(f"  📄 点击后跳到: {seen[:110]}")
                    break
            else:
                # Staying put is not failure. A target that answers with
                # Content-Disposition: attachment is handled as a download,
                # and a download does not navigate the tab -- which is the
                # successful case for this rung, not a broken one. Measured on
                # IOP: the tab stayed on the article page while the PDF landed.
                # Whether a file actually arrived is decided below; this line
                # only states what the tab did.
                print("  📄 点击后标签页仍停在来路页"
                      "（目标若以下载响应，本就不会导航）")
    except Exception as exc:
        print(f"  ⚠️  点击跳转失败: {type(exc).__name__}: {str(exc)[:80]}")
        return result

    landed = await _await_download(download_dir, timeout_s=timeout_s)
    if landed:
        result = {'success': True, 'downloaded_file': landed,
                  'target_id': None, 'ws_url': None}
    return result


async def open_url_in_fresh_chrome(url: str, *, expected_doi: str = '',
                                   pdf_mode: bool = False,
                                   download_dir: str = '',
                                   timeout_s: int = 60,
                                   port: Optional[int] = None,
                                   headless: bool = False,
                                   want_html: bool = False,
                                   referer_url: str = '',
                                   fast_path_wait_s: float = 3.0
                                   ) -> FreshChromeSession:
    """Launch a clean Chrome, open *url* in it, and hand back the session.

    The session is returned **started and still running** so the caller can
    read ``session.result`` (and, in ``pdf_mode``, the downloaded file) before
    calling ``await session.close()``. Always close it — that is what removes
    the throwaway profile.

    On launch failure the session comes back with an empty ``result``; the
    caller should fall back to its normal path rather than assume success.
    """
    session = FreshChromeSession(port=port, download_dir=download_dir,
                                 headless=headless)
    # Launch straight at the URL: Chrome's own startup navigation fetches the
    # page, so nothing automated participates in the load. Attaching happens
    # afterwards, only to watch the challenge and click it through.
    #
    # With referer_url the browser starts on the referring page instead, and
    # reaches the target by a click from there -- see
    # _download_via_referer_click for why that changes what the server sees.
    if not await session.start(start_url=referer_url or url):
        await session.close()
        return session

    if referer_url:
        session.result = await _download_via_referer_click(
            session, referer_url, url, download_dir, timeout_s)
        return session

    # Fast path: a PDF that is served without a challenge is already on disk
    # moments after startup. Returning here means no CDP command ever ran
    # against the page at all.
    #
    # This has to come first for correctness: the download starts before we
    # could attach, so the challenge watcher would take its "baseline" of the
    # directory *after* the file landed, see nothing new, and report failure
    # for a download that had already succeeded.
    #
    # The window no longer has to be short. It races the finished download
    # against a real page target appearing, and a publisher that challenges the
    # PDF produces that page within a second or so, which ends the wait -- the
    # Turnstile box is looked for just as promptly as before. Only the
    # unchallenged case spends the full budget, and there spending it is the
    # entire point: the file lands and no CDP command ever touches the tab.
    #
    # Three seconds was too little for that to happen. A ~1 MB PDF does not
    # finish in the window, so IOP fell through to the CDP path every time and
    # met a Radware captcha there -- on a link that downloads perfectly well
    # when left alone.
    if pdf_mode and download_dir:
        kind, value = await _await_download_or_tab(
            download_dir, session.port,
            timeout_s=min(fast_path_wait_s, timeout_s))
        if kind == 'file':
            print(f"  ✓ 文件已下载（未经 CDP 交互）: {value}")
            session.result = {'success': True, 'downloaded_file': value,
                              'target_id': None, 'ws_url': None}
            return session
        if kind == 'page':
            # A real page instead of a download: a challenge, a paywall, or a
            # viewer. Stop waiting -- the rest of the budget would be spent on
            # a file that is never coming. The URL is printed because what the
            # startup navigation actually turns into on a PDF link has never
            # been observed directly; this is the log line that will say.
            print(f"  ↪ 启动导航停在页面而非下载: {value[:90]}")

            # ⚠️ An interstitial ends this rung. Going on to the CDP flow
            # cannot help and actively hurts: that flow finds the startup tab
            # by matching HOST against the target's, the interstitial's host is
            # not the target's, so the match fails ("未找到启动时打开的 tab")
            # and it falls through to opening a NEW tab straight at the target
            # -- a second, Referer-less request from a browser the bot manager
            # has just flagged. Measured on IOP: three consecutive attempts
            # each paid a Chrome launch plus a 20 s captcha wait here, and not
            # one of them could have succeeded.
            #
            # Returning with session.result left empty is the documented
            # "this rung produced nothing" shape; the caller's finally closes
            # the browser, so nothing leaks by not closing it here.
            if url_looks_like_bot_challenge(value):
                print("  ⛔ 落在拦截器页面 —— 放弃本层"
                      "（继续只会从已被标记的浏览器再发一次无 Referer 的请求）")
                return session

            print("  ↪ 转 CDP 流程")

    try:
        await session.open_url(url, expected_doi=expected_doi,
                               pdf_mode=pdf_mode, timeout_s=timeout_s,
                               already_open=True)
    except Exception as exc:
        print(f"  ⚠️  独立 Chrome 打开页面失败: {type(exc).__name__}: {exc}")

    # Pages (a publisher's supplemental listing, an API response) are wanted as
    # markup, not as a file on disk. The tab is still open and already past any
    # challenge, so one CDP round-trip is all it takes.
    if want_html and not (session.result or {}).get('html'):
        html = await fetch_page_html_via_cdp(
            (session.result or {}).get('ws_url') or '', port=session.port)
        if html:
            session.result = dict(session.result or {}, html=html)

    # The challenge flow reports the file it saw appear; when it did not (it
    # only watches for *new* files), fall back to whatever is in the
    # directory. It is created empty per attempt, so anything there is ours.
    if pdf_mode and download_dir and not (session.result or {}).get('downloaded_file'):
        landed = await _await_download(download_dir, timeout_s=5)
        if landed:
            session.result = dict(session.result or {},
                                  success=True, downloaded_file=landed)
    return session


async def _await_download_or_tab(download_dir: str, debug_port: int,
                                 timeout_s: float = 5.0,
                                 settle_s: float = 1.0) -> tuple:
    """Race Chrome's startup navigation to its two possible outcomes.

    Returns ``('file', path)`` when the download finished, ``('page', url)``
    when a real page target appeared instead, or ``('', '')`` on timeout.

    Both halves matter. Waiting only for the file means a challenged publisher
    burns the whole window before anyone looks for the checkbox. Waiting only
    for a tab means an unchallenged PDF -- which produces no page at all --
    falls through to the CDP path and gets challenged *there*, which is
    precisely how IOP ended up showing a Radware captcha on a link that
    downloads fine when left alone.

    The page half deliberately does **not** match on host. The host check in
    ``bypass_cloudflare_cdp``'s ``already_open`` branch never once matched on a
    PDF link in practice, so anything but ``about:blank`` / ``chrome://newtab``
    counts here, and the caller logs what it found.
    """
    ignored = ('about:blank', 'chrome://newtab', 'chrome://new-tab-page')
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        landed = await _await_download(download_dir, timeout_s=0.01,
                                       settle_s=settle_s)
        if landed:
            return 'file', landed

        try:
            with urllib.request.urlopen(
                    f"http://localhost:{debug_port}/json", timeout=5) as resp:
                targets = json.loads(resp.read().decode())
        except Exception:
            targets = []
        for target in targets:
            if target.get('type') != 'page':
                continue
            page_url = (target.get('url') or '').strip()
            if not page_url or page_url.startswith(ignored):
                continue
            return 'page', page_url

        await asyncio.sleep(0.5)
    return '', ''


async def _await_download(download_dir: str, timeout_s: float = 20.0,
                          settle_s: float = 1.0) -> str:
    """Wait for a finished download to appear in *download_dir*.

    The directory is created empty for each attempt, so any completed file in
    it belongs to this download -- no before/after comparison is needed, which
    is what makes this robust when the download beats us to the directory.

    ``.crdownload`` files are Chrome's in-progress markers and are skipped;
    the file is considered done when it has stopped growing.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            names = [n for n in os.listdir(download_dir)
                     if not n.endswith('.crdownload')]
        except OSError:
            names = []
        for name in names:
            path = os.path.join(download_dir, name)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size <= 0:
                continue
            await asyncio.sleep(settle_s)
            try:
                if os.path.getsize(path) == size:
                    return path
            except OSError:
                continue
        await asyncio.sleep(0.5)
    return ''


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='启动配置好的 Chrome')
    parser.add_argument('--headless', action='store_true', help='无头模式')
    parser.add_argument('--kill', action='store_true', help='关闭所有 Chrome 进程')
    args = parser.parse_args()

    if args.kill:
        kill_chrome()
        raise SystemExit(0)

    launch_chrome(headless=args.headless)

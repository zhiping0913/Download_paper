#!/usr/bin/env python3
"""
Pre-flight dependency checker for complete_paper_extraction.py.

Run before the main program to verify all system tools, Python packages,
browsers, and network endpoints are available.

Usage:
    python check_dependencies.py          # check all
    python check_dependencies.py --quiet  # only print failures
    python check_dependencies.py --json   # machine-readable output
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Terminal colours
# ---------------------------------------------------------------------------
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

_ok   = f"{GREEN}✓{RESET}"
_fail = f"{RED}✗{RESET}"
_warn = f"{YELLOW}⚠{RESET}"


def _status(condition: bool) -> str:
    return _ok if condition else _fail


# ===================================================================
# Check categories
# ===================================================================

class Checker:
    def __init__(self, quiet: bool = False, json_out: bool = False):
        self.quiet = quiet
        self.json_out = json_out
        self.results: list[dict] = []
        self.warnings: list[str] = []

    def log_ok(self, msg: str):
        self.results.append({"status": "ok", "message": msg})
        if not self.quiet:
            print(f"  {_ok}  {msg}")

    def log_fail(self, msg: str):
        self.results.append({"status": "fail", "message": msg})
        if not self.quiet:
            print(f"  {_fail}  {msg}")

    def log_warn(self, msg: str):
        self.results.append({"status": "warn", "message": msg})
        self.warnings.append(msg)
        if not self.quiet:
            print(f"  {_warn}  {msg}")

    def section(self, title: str):
        if not self.quiet:
            print(f"\n{BOLD}{title}{RESET}")

    @property
    def all_ok(self) -> bool:
        return all(r["status"] != "fail" for r in self.results)


def check_system_tools(c: Checker):
    c.section("1. System tools")

    # pandoc
    pandoc_path = shutil.which("pandoc")
    if pandoc_path:
        try:
            out = subprocess.check_output(
                ["pandoc", "--version"], stderr=subprocess.STDOUT, timeout=10
            )
            ver = out.decode().splitlines()[0]
            c.log_ok(f"pandoc — {ver} ({pandoc_path})")
        except Exception:
            c.log_warn(f"pandoc found but failed to run ({pandoc_path})")
    else:
        c.log_fail("pandoc — not found in PATH (install: sudo apt install pandoc)")

    # libmagic
    try:
        import ctypes.util
        libmagic = ctypes.util.find_library("magic")
        if libmagic:
            c.log_ok(f"libmagic — {libmagic}")
        else:
            # python-magic may bundle its own; verify via import test below
            c.log_ok("libmagic — (checked via python-magic)")
    except Exception:
        c.log_warn("libmagic — could not probe; python-magic import will decide")


def check_python_packages(c: Checker):
    c.section("2. Python packages")

    packages = {
        "playwright": "playwright",
        "bs4": "beautifulsoup4",
        "pypandoc": "pypandoc",
        "magic": "python-magic",
        "requests": "requests",
    }

    for import_name, pkg_name in packages.items():
        try:
            mod = __import__(import_name)
            ver = getattr(mod, "__version__", "?")
            c.log_ok(f"{pkg_name} — {ver}")
        except ImportError:
            c.log_fail(f"{pkg_name} — not installed (pip install {pkg_name})")


def check_browsers(c: Checker):
    c.section("3. Browsers")

    # Playwright Chromium
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
            c.log_ok("playwright chromium — launchable")
    except Exception as e:
        msg = str(e).splitlines()[0] if str(e) else "unknown error"
        if "Executable doesn't exist" in str(e) or "not found" in str(e):
            c.log_fail(f"playwright chromium — not installed (run: playwright install chromium)")
        else:
            c.log_fail(f"playwright chromium — {msg}")

    # Headed Chrome (for --force-headed path, optional)
    chrome_candidates = [
        "/opt/google/chrome/chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]
    chrome_path = None
    for path in chrome_candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            chrome_path = path
            break

    if chrome_path and chrome_path == "/opt/google/chrome/chrome":
        c.log_ok(f"chrome — {chrome_path}")
    elif chrome_path:
        c.log_warn(f"chrome — found at {chrome_path} (expected /opt/google/chrome/chrome)")
    else:
        c.log_warn("chrome — not found (headed mode will not work; headless-only)")


def check_network(c: Checker):
    c.section("4. Network endpoints")

    try:
        import requests
    except ImportError:
        c.log_fail("requests not available, skipping network checks")
        return

    endpoints = {
        "DOI resolver": "https://doi.org/10.1103/PhysRevLett.124.185004",
        "Semantic Scholar": "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1103/PhysRevLett.124.185004?fields=title",
    }

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })

    for name, url in endpoints.items():
        try:
            resp = session.get(url, timeout=15, allow_redirects=True)
            # DOI resolver often returns 403 to non-browser user-agents; it
            # still resolves correctly inside Playwright's browser context.
            if resp.status_code < 500 or (name == "DOI resolver" and resp.status_code == 403):
                c.log_ok(f"{name} — reachable (HTTP {resp.status_code})")
            else:
                c.log_warn(f"{name} — HTTP {resp.status_code}")
        except requests.ConnectionError:
            c.log_fail(f"{name} — connection refused")
        except requests.Timeout:
            c.log_fail(f"{name} — timeout (15s)")
        except Exception as e:
            c.log_fail(f"{name} — {e}")


def check_filesystem(c: Checker):
    c.section("5. Filesystem")

    project_root = Path(__file__).resolve().parent
    needed_dirs = ["captured_data", "publisher"]
    for d in needed_dirs:
        p = project_root / d
        if p.is_dir():
            c.log_ok(f"{d}/ — exists")
        else:
            c.log_warn(f"{d}/ — missing (will be created on first run)")

    # config.py
    config_py = project_root / "config.py"
    if config_py.is_file():
        c.log_ok("config.py — exists")
    else:
        c.log_fail("config.py — missing")

    # Headless auth state (optional, warn if missing but not fatal)
    auth_file = project_root / ".auth" / "headless_storage_state.json"
    if auth_file.is_file():
        c.log_ok(".auth/headless_storage_state.json — exists")
    else:
        c.log_warn(".auth/headless_storage_state.json — missing (headless may trigger login)")


# ===================================================================
# Main
# ===================================================================

def run_checks(quiet: bool = False, json_out: bool = False) -> bool:
    """Run all dependency checks.  Returns True when no failures found."""
    if not quiet:
        print(f"{BOLD}Download_paper — Dependency Check{RESET}")
        print(f"Python: {sys.version}")

    c = Checker(quiet=quiet, json_out=json_out)

    check_system_tools(c)
    check_python_packages(c)
    check_browsers(c)
    check_network(c)
    check_filesystem(c)

    if json_out:
        import json as _json
        print(_json.dumps(c.results, indent=2))
        return c.all_ok

    if not quiet:
        print()
        fail_count = sum(1 for r in c.results if r["status"] == "fail")
        warn_count = len(c.warnings)

        if fail_count == 0 and warn_count == 0:
            print(f"{GREEN}{BOLD}All dependency checks passed.{RESET}")
        elif fail_count == 0:
            print(f"{YELLOW}{BOLD}All required checks passed ({warn_count} optional warning(s)).{RESET}")
        else:
            print(f"{RED}{BOLD}{fail_count} check(s) FAILED. Fix above before running complete_paper_extraction.py.{RESET}")

    return c.all_ok


def main():
    quiet = "--quiet" in sys.argv
    json_out = "--json" in sys.argv
    ok = run_checks(quiet=quiet, json_out=json_out)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

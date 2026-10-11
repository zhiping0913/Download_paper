"""Portable MIME sniffing with a python-magic-compatible surface.

Prefer python-magic when its native library is genuinely usable.  Some
systems (notably stock macOS) provide the ``file`` utility and magic database
without a separately loadable libmagic, so use ``file`` as a fallback.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Optional


class MagicBackendUnavailable(RuntimeError):
    """Raised when neither python-magic nor a usable `file` command exists."""


class _FileCommandMagic:
    def __init__(self, executable: str):
        self.executable = executable

    def from_file(self, path: str, mime: bool = False) -> str:
        argv = [self.executable, "-b"]
        if mime:
            argv.append("--mime-type")
        argv.extend(["--", os.fspath(path)])
        return subprocess.check_output(
            argv, text=True, stderr=subprocess.STDOUT, timeout=15
        ).strip()

    def from_buffer(self, data: bytes, mime: bool = False) -> str:
        fd, path = tempfile.mkstemp(prefix="dp_magic_")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            return self.from_file(path, mime=mime)
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


class _UnavailableMagic:
    def __init__(self, reason: str):
        self.reason = reason

    def _raise(self) -> None:
        raise MagicBackendUnavailable(self.reason)

    def from_file(self, path: str, mime: bool = False) -> str:
        self._raise()

    def from_buffer(self, data: bytes, mime: bool = False) -> str:
        self._raise()


def _load_backend():
    python_magic_error: Optional[BaseException] = None
    try:
        import magic as python_magic

        # Some python-magic builds import successfully but load libmagic only
        # on first use. Probe the real call now rather than failing mid-download.
        python_magic.from_buffer(b"%PDF-1.4\n", mime=True)
        return python_magic, "python-magic"
    except Exception as exc:
        python_magic_error = exc

    file_executable = shutil.which("file")
    if file_executable:
        backend = _FileCommandMagic(file_executable)
        try:
            backend.from_buffer(b"%PDF-1.4\n", mime=True)
            return backend, f"file:{file_executable}"
        except (OSError, subprocess.SubprocessError) as exc:
            file_error = exc
    else:
        file_error = FileNotFoundError("`file` executable not found in PATH")

    reason = (
        "No MIME detection backend is usable. Install python-magic plus "
        "libmagic (or python-magic-bin on Windows), or install the `file` "
        f"utility. python-magic: {python_magic_error}; file: {file_error}"
    )
    return _UnavailableMagic(reason), "unavailable"


magic, BACKEND_NAME = _load_backend()

"""Atomic replacement for artifacts that other processes read.

``Path.write_text`` truncates the destination before writing, so a concurrent
reader (or a crash mid-write) can observe an empty or partial file.  This
helper writes a sibling temp file, fsyncs it and ``os.replace``s it over the
destination, so readers see either the old or the new content.
"""

from __future__ import annotations

import io
import os
import secrets
import stat
from pathlib import Path


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    encoding: str | None = None,
    newline: str | None = None,
) -> None:
    """Replace ``path`` with ``text`` atomically.

    ``encoding`` and ``newline`` mean what they mean for ``Path.write_text``,
    so a call site switching over produces identical bytes.  A symlink
    destination is followed, and an existing file keeps its permission bits;
    a new file gets the umask-derived mode ``Path.write_text`` would give it.
    """

    destination = Path(os.path.realpath(Path(path)))
    encoding = io.text_encoding(encoding)
    try:
        mode = stat.S_IMODE(destination.stat().st_mode)
    except FileNotFoundError:
        mode = None
    temp_path, fd = _create_sibling_temp(destination)
    try:
        if mode is not None:
            os.chmod(fd, mode)
        with open(fd, "w", encoding=encoding, newline=newline) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _create_sibling_temp(destination: Path) -> tuple[Path, int]:
    # os.open with 0o666 lets the umask apply exactly as it does for
    # Path.write_text; tempfile.mkstemp would force 0o600 instead.
    for _ in range(100):
        candidate = destination.with_name(f".{destination.name}.{secrets.token_hex(6)}.tmp")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            continue
        return candidate, fd
    raise FileExistsError(f"could not create a temp file next to {destination}")

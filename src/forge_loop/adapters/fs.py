"""Filesystem adapter — read, write, exists, glob, chmod.

Wraps ``pathlib.Path`` + a tiny bit of stdlib so tests can substitute
an in-memory file system without ``monkeypatch`` per call site.

The Protocol covers the file operations production code actually does:
read/write text, check existence, glob a pattern, chmod for the
worker-planted ``.claude/`` directory. Anything fancier (atomic write,
file locking) stays in the impl — the Protocol is the contract, not
the kitchen sink.
"""

from __future__ import annotations

import glob as _glob
import os as _os
from pathlib import Path
from typing import Protocol


class FileSystem(Protocol):
    """Filesystem operations injected so tests can swap a fake."""

    def exists(self, path: Path) -> bool:
        ...

    def read_text(self, path: Path) -> str:
        ...

    def write_text(self, path: Path, content: str) -> None:
        ...

    def glob(self, pattern: str) -> list[str]:
        """``glob.glob(pattern)`` — returns paths as strings, like stdlib."""
        ...

    def chmod(self, path: Path, mode: int) -> None:
        ...

    def mkdir(self, path: Path, parents: bool = False, exist_ok: bool = False) -> None:
        ...


class OsFileSystem:
    """Real filesystem — delegates to pathlib + stdlib glob."""

    def exists(self, path: Path) -> bool:
        return path.exists()

    def read_text(self, path: Path) -> str:
        return path.read_text()

    def write_text(self, path: Path, content: str) -> None:
        path.write_text(content)

    def glob(self, pattern: str) -> list[str]:
        return _glob.glob(pattern)

    def chmod(self, path: Path, mode: int) -> None:
        _os.chmod(path, mode)

    def mkdir(self, path: Path, parents: bool = False, exist_ok: bool = False) -> None:
        path.mkdir(parents=parents, exist_ok=exist_ok)


class FakeFileSystem:
    """In-memory filesystem for tests.

    Stores file contents in a dict keyed by absolute path. Directories
    are tracked separately so ``mkdir`` + ``exists`` work without
    materialising on disk.

    Limitations (intentional — keep the fake simple):
    - No permission bits / ownership / inode tracking.
    - ``chmod`` is a no-op (no production test exercises mode-dependent
      behaviour via this Protocol; the worker-planted ``.claude/``
      lock-down is asserted at integration level).
    - ``glob`` supports only literal-prefix + ``*`` patterns (e.g.
      ``/tmp/wt-loop-*``); full fnmatch is overkill for the test
      patterns this fake serves.
    """

    def __init__(self) -> None:
        self._files: dict[str, str] = {}
        self._dirs: set[str] = set()

    def exists(self, path: Path) -> bool:
        k = str(path)
        return k in self._files or k in self._dirs

    def read_text(self, path: Path) -> str:
        try:
            return self._files[str(path)]
        except KeyError:
            raise FileNotFoundError(f"FakeFileSystem: {path}") from None

    def write_text(self, path: Path, content: str) -> None:
        # Auto-create parent dirs to match the convenience of
        # ``Path.write_text`` when used after ``mkdir(parents=True)``.
        parent = str(path.parent)
        self._dirs.add(parent)
        self._files[str(path)] = content

    def glob(self, pattern: str) -> list[str]:
        if "*" not in pattern:
            return [pattern] if pattern in self._files or pattern in self._dirs else []
        prefix, _, suffix = pattern.partition("*")
        out: list[str] = []
        for key in (*self._files, *self._dirs):
            if key.startswith(prefix) and key.endswith(suffix):
                out.append(key)
        return sorted(out)

    def chmod(self, path: Path, mode: int) -> None:
        # No-op — see class docstring.
        pass

    def mkdir(self, path: Path, parents: bool = False, exist_ok: bool = False) -> None:
        k = str(path)
        if k in self._dirs and not exist_ok:
            raise FileExistsError(f"FakeFileSystem: {path}")
        if parents:
            parts: list[str] = []
            for part in path.parts:
                parts.append(part)
                self._dirs.add(str(Path(*parts)))
        else:
            if str(path.parent) not in self._dirs and str(path.parent) != ".":
                raise FileNotFoundError(f"FakeFileSystem: parent of {path} not created")
            self._dirs.add(k)


__all__ = ["FakeFileSystem", "FileSystem", "OsFileSystem"]

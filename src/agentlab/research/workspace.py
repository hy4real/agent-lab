"""Scoped filesystem workspace with fail-closed path resolution.

Every filesystem tool must go through :meth:`Workspace.resolve`. The resolver
walks the path one component at a time on an open directory descriptor and
refuses to follow a symlink at any level, so a path that looks relative cannot
escape the root and a pre-planted link cannot redirect a write.

The tool layer declares *which* roots a tool may touch via ``PathScope``; this
module enforces *how* a path inside those roots is resolved.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

PATH_MODES = frozenset({"allow", "deny", "interrupt"})

MAX_PATH_BYTES = 1024
MAX_WRITE_BYTES = 256 * 1024


class WorkspaceError(RuntimeError):
    """A path failed resolution. Always fail-closed."""


@dataclass(frozen=True)
class PathScope:
    """Declared filesystem scope of one tool.

    ``allow``     the tool may touch paths under ``roots``.
    ``deny``      the tool is registered but every path call is refused.
    ``interrupt`` the tool never runs unattended: a call returns
                  ``approval_required`` and the caller must decide.

    ``deny_globs`` are checked against the *resolved relative* path, so they
    apply after the scope check and cannot be bypassed by ``./x/../y``.
    """

    mode: str
    roots: tuple[str, ...] = ()
    deny_globs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.mode not in PATH_MODES:
            raise ValueError("path_scope.mode 必须是 allow / deny / interrupt")
        if self.mode == "allow" and not self.roots:
            raise ValueError("allow 模式必须至少声明一个 root")
        for root in self.roots:
            if not root or root.startswith("/") or ".." in PurePosixPath(root).parts:
                raise ValueError("root 必须是相对路径且不含 ..")


def _reject(reason: str) -> "WorkspaceError":
    return WorkspaceError(reason)


def _relative_parts(relative: str) -> tuple[str, ...]:
    """Split a caller-supplied path into vetted components, or raise."""
    if not isinstance(relative, str) or not relative:
        raise _reject("path_invalid")
    if "\x00" in relative:
        raise _reject("path_invalid")
    if len(relative.encode("utf-8")) > MAX_PATH_BYTES:
        raise _reject("path_too_long")
    if relative.startswith("/") or relative.startswith("\\"):
        raise _reject("path_outside_scope")
    if ":" in relative.split("/")[0] and os.name == "nt":
        raise _reject("path_outside_scope")
    raw_parts = relative.split("/")
    parts: list[str] = []
    for part in raw_parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise _reject("path_outside_scope")
        parts.append(part)
    if not parts:
        raise _reject("path_invalid")
    return tuple(parts)


class Workspace:
    """One root directory. All reads and writes are confined to it."""

    def __init__(self, root: Path) -> None:
        root = Path(root)
        if root.is_symlink():
            raise _reject("root_symlink")
        if not root.is_dir():
            raise _reject("root_not_directory")
        self._root = root
        self._root_real = root.resolve(strict=True)

    @property
    def root(self) -> Path:
        return self._root

    def _open_root(self) -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            return os.open(self._root, flags)
        except OSError as exc:  # pragma: no cover - root re-checked in __init__
            raise _reject("root_unreadable") from exc

    def _stat_at(self, dir_fd: int, name: str) -> os.stat_result:
        try:
            return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise _reject("path_missing") from exc
        except OSError as exc:
            raise _reject("path_unreadable") from exc

    def resolve(self, relative: str, *, must_exist: bool = True) -> Path:
        """Resolve a relative path inside the root without following symlinks."""
        parts = _relative_parts(relative)
        fd = self._open_root()
        try:
            for part in parts[:-1]:
                info = self._stat_at(fd, part)
                if stat.S_ISLNK(info.st_mode):
                    raise _reject("path_symlink")
                if not stat.S_ISDIR(info.st_mode):
                    raise _reject("path_not_directory")
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                try:
                    next_fd = os.open(part, flags, dir_fd=fd)
                except OSError as exc:
                    raise _reject("path_unreadable") from exc
                os.close(fd)
                fd = next_fd

            last = parts[-1]
            last_info: os.stat_result | None
            try:
                last_info = os.stat(last, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                if must_exist:
                    raise _reject("path_missing") from None
                last_info = None
            if last_info is not None:
                if stat.S_ISLNK(last_info.st_mode):
                    raise _reject("path_symlink")
                if not stat.S_ISREG(last_info.st_mode):
                    raise _reject("path_not_regular_file")
        finally:
            os.close(fd)

        candidate = self._root.joinpath(*parts)
        resolved = candidate.parent.resolve(strict=True).joinpath(parts[-1])
        if not resolved.is_relative_to(self._root_real):
            raise _reject("path_outside_scope")
        return resolved

    def relative_of(self, path: Path) -> str:
        return path.relative_to(self._root_real).as_posix()

    def read_text(self, relative: str, *, max_bytes: int = MAX_WRITE_BYTES) -> tuple[str, int]:
        path = self.resolve(relative)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise _reject("path_unreadable") from exc
        if size > max_bytes:
            raise _reject("file_too_large")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise _reject("path_unreadable") from exc
        try:
            return data.decode("utf-8"), len(data)
        except UnicodeDecodeError as exc:
            raise _reject("file_not_utf8") from exc

    def write_text(self, relative: str, text: str, *, overwrite: bool = False) -> int:
        if not isinstance(text, str):
            raise _reject("write_payload_invalid")
        payload = text.encode("utf-8")
        if len(payload) > MAX_WRITE_BYTES:
            raise _reject("file_too_large")
        path = self.resolve(relative, must_exist=False)
        flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        flags |= os.O_TRUNC if overwrite else os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise _reject("file_exists") from exc
        except OSError as exc:
            raise _reject("write_failed") from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise _reject("write_failed") from exc
        return len(payload)


def scope_permits(scope: PathScope, relative: str) -> bool:
    """Containment check against a declared scope.

    Raises ``WorkspaceError`` for a malformed path (so a caller cannot smuggle
    an absolute path past the check), returns False for a well-formed path that
    falls outside the roots or matches a deny glob.
    """
    if scope.mode != "allow":
        return False
    parts = _relative_parts(relative)
    joined = "/".join(parts)
    for pattern in scope.deny_globs:
        if PurePosixPath(joined).match(pattern):
            return False
    return any(joined == root or joined.startswith(root.rstrip("/") + "/") for root in scope.roots)

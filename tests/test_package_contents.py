"""Release archive allowlist tests."""

from __future__ import annotations

import io
import importlib.util
import tarfile
from pathlib import Path


_SCRIPT = Path(__file__).parents[1] / "scripts" / "check_package_contents.py"
_SPEC = importlib.util.spec_from_file_location("check_package_contents", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
audit = _MODULE.audit


def _sdist(tmp_path: Path, members: dict[str, bytes]) -> Path:
    path = tmp_path / "agent_lab-1.1.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(f"agent_lab-1.1.0/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return path


def test_audit_rejects_unlisted_private_member(tmp_path: Path) -> None:
    failures = audit(_sdist(tmp_path, {"docs/private-corpus.md": b"synthetic"}))
    assert "unexpected archive member: docs/private-corpus.md" in failures


def test_audit_rejects_posix_home_path(tmp_path: Path) -> None:
    private_path = b"/" + b"home/example/secrets/private.txt"
    failures = audit(_sdist(tmp_path, {"README.md": private_path}))
    assert "sensitive content marker in: README.md" in failures


def test_audit_rejects_duplicate_and_symlink_members(tmp_path: Path) -> None:
    path = tmp_path / "agent_lab-1.1.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for content in (b"first", b"second"):
            info = tarfile.TarInfo("agent_lab-1.1.0/README.md")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        link = tarfile.TarInfo("agent_lab-1.1.0/docs/private-link.md")
        link.type = tarfile.SYMTYPE
        link.linkname = "../README.md"
        archive.addfile(link)
    failures = audit(path)
    assert "duplicate archive member: README.md" in failures
    assert "non-regular archive member: docs/private-link.md" in failures

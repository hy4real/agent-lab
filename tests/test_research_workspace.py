"""Attack tests for the scoped workspace and the declared path scope.

Each test states one thing an attacker or a careless tool could try. The point
is not that the code is long, it is that every escape route named in
``docs/isolation-attack-matrix.md`` has a case here that fails when the defence
is removed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentlab.reasoning import ToolError
from agentlab.research.tools import build_workspace_tools
from agentlab.research.workspace import (
    PathScope,
    Workspace,
    WorkspaceError,
    scope_permits,
)


@pytest.fixture()
def ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "root"
    root.mkdir()
    (root / "notes").mkdir()
    (root / "notes" / "a.md").write_text("alpha", encoding="utf-8")
    (root / "b.md").write_text("beta", encoding="utf-8")
    return Workspace(root)


# --- path resolution -------------------------------------------------------


@pytest.mark.parametrize(
    "relative",
    ["/etc/passwd", "\\\\server\\share", "../outside.md", "notes/../../outside.md", "", ".", "notes/"],
)
def test_rejects_paths_that_do_not_name_a_file_inside_the_root(
    ws: Workspace, relative: str
) -> None:
    with pytest.raises(WorkspaceError):
        ws.resolve(relative)


def test_rejects_nul_byte_and_overlong_path(ws: Workspace) -> None:
    with pytest.raises(WorkspaceError):
        ws.resolve("a\x00b.md")
    with pytest.raises(WorkspaceError):
        ws.resolve("x" * 2000)


def test_rejects_symlinked_file(ws: Workspace, tmp_path: Path) -> None:
    outside = tmp_path / "secret.md"
    outside.write_text("s3cret", encoding="utf-8")
    (ws.root / "link.md").symlink_to(outside)
    with pytest.raises(WorkspaceError, match="path_symlink"):
        ws.resolve("link.md")
    with pytest.raises(WorkspaceError, match="path_symlink"):
        ws.read_text("link.md")


def test_rejects_symlinked_directory_component(ws: Workspace, tmp_path: Path) -> None:
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "x.md").write_text("outside", encoding="utf-8")
    (ws.root / "escape").symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="path_symlink"):
        ws.resolve("escape/x.md")


def test_rejects_symlinked_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="root_symlink"):
        Workspace(link)


def test_rejects_non_regular_file(ws: Workspace) -> None:
    fifo = ws.root / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(WorkspaceError, match="path_not_regular_file"):
        ws.resolve("pipe")


def test_resolves_a_plain_nested_path(ws: Workspace) -> None:
    text, size = ws.read_text("notes/a.md")
    assert (text, size) == ("alpha", 5)


def test_read_rejects_non_utf8_and_oversized(ws: Workspace) -> None:
    (ws.root / "bad.md").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(WorkspaceError, match="file_not_utf8"):
        ws.read_text("bad.md")
    (ws.root / "big.md").write_text("x" * 100, encoding="utf-8")
    with pytest.raises(WorkspaceError, match="file_too_large"):
        ws.read_text("big.md", max_bytes=10)


# --- writes ----------------------------------------------------------------


def test_write_is_exclusive_by_default(ws: Workspace) -> None:
    assert ws.write_text("new.md", "hello") == 5
    assert (ws.root / "new.md").read_text(encoding="utf-8") == "hello"
    with pytest.raises(WorkspaceError, match="file_exists"):
        ws.write_text("new.md", "again")
    assert (ws.root / "new.md").read_text(encoding="utf-8") == "hello"


def test_write_needs_an_explicit_overwrite_flag(ws: Workspace) -> None:
    ws.write_text("n.md", "one")
    ws.write_text("n.md", "two", overwrite=True)
    assert (ws.root / "n.md").read_text(encoding="utf-8") == "two"


def test_write_is_owner_only(ws: Workspace) -> None:
    ws.write_text("n.md", "one")
    assert (ws.root / "n.md").stat().st_mode & 0o777 == 0o600


def test_write_cannot_follow_a_planted_symlink(ws: Workspace, tmp_path: Path) -> None:
    target = tmp_path / "victim.md"
    target.write_text("unchanged", encoding="utf-8")
    (ws.root / "planted.md").symlink_to(target)
    with pytest.raises(WorkspaceError, match="path_symlink"):
        ws.write_text("planted.md", "pwned", overwrite=True)
    assert target.read_text(encoding="utf-8") == "unchanged"


# --- declared scope --------------------------------------------------------


def test_scope_rejects_outside_roots_and_deny_globs() -> None:
    scope = PathScope(mode="allow", roots=("notes",), deny_globs=("**/*.key",))
    assert scope_permits(scope, "notes/a.md") is True
    assert scope_permits(scope, "notes/deep/a.md") is True
    assert scope_permits(scope, "b.md") is False
    assert scope_permits(scope, "notes/secret.key") is False


def test_scope_rejects_absolute_and_traversal_paths() -> None:
    scope = PathScope(mode="allow", roots=("notes",))
    for bad in ("/etc/passwd", "../notes/a.md", "notes/../../x"):
        with pytest.raises(WorkspaceError):
            scope_permits(scope, bad)


@pytest.mark.parametrize(("mode", "roots"), [("allow", ()), ("allow", ("/abs",)), ("allow", ("../up",)), ("bogus", ("x",))])
def test_invalid_path_scope_declarations_are_rejected(mode: str, roots: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        PathScope(mode=mode, roots=roots)


@pytest.mark.parametrize("mode", ["deny", "interrupt"])
def test_refusing_scopes_do_not_need_roots(mode: str) -> None:
    """A deny or interrupt scope refuses everything, so it needs no roots."""
    scope = PathScope(mode=mode)
    assert scope.roots == ()
    assert scope_permits(scope, "anything.md") is False


def test_declared_scope_is_required_to_match_side_effect() -> None:
    from agentlab.research.tools import ToolSpec

    with pytest.raises(ValueError, match="read 或 write"):
        ToolSpec(
            required={},
            optional={},
            handler=lambda _a: {},
            side_effect="none",
            max_output_bytes=1024,
            path_scope=PathScope(mode="allow", roots=("notes",)),
        )


# --- registry enforcement --------------------------------------------------


def test_registry_refuses_a_path_outside_the_scope(ws: Workspace) -> None:
    registry = build_workspace_tools(ws, scope=PathScope(mode="allow", roots=("notes",)))
    assert registry.call("read_file", '{"path":"notes/a.md"}').startswith("{")
    with pytest.raises(ToolError, match="path_outside_scope"):
        registry.call("read_file", '{"path":"b.md"}')
    with pytest.raises(ToolError, match="path_outside_scope"):
        registry.call("write_file", '{"path":"b.md","text":"x"}')


def test_registry_never_auto_approves_an_interrupt_scope(ws: Workspace) -> None:
    registry = build_workspace_tools(ws, scope=PathScope(mode="interrupt", roots=("notes",)))
    with pytest.raises(ToolError, match="approval_required"):
        registry.call("write_file", '{"path":"notes/a.md","text":"x"}')
    assert (ws.root / "notes" / "a.md").read_text(encoding="utf-8") == "alpha"


def test_registry_refuses_every_path_call_in_a_deny_scope(ws: Workspace) -> None:
    registry = build_workspace_tools(ws, scope=PathScope(mode="deny"))
    with pytest.raises(ToolError, match="path_denied"):
        registry.call("read_file", '{"path":"notes/a.md"}')


def test_registry_counts_write_calls_for_the_suite_assertions(ws: Workspace) -> None:
    registry = build_workspace_tools(ws, scope=PathScope(mode="allow", roots=("notes",)))
    registry.call("write_file", '{"path":"notes/new.md","text":"ok"}')
    assert registry.write_calls() == 1
    with pytest.raises(ToolError):
        registry.call("write_file", '{"path":"b.md","text":"no"}')
    assert registry.write_calls() == 1, "被拒绝的调用不得计为副作用"


def test_traversal_reaches_the_registry_as_a_tool_error(ws: Workspace) -> None:
    registry = build_workspace_tools(ws, scope=PathScope(mode="allow", roots=("notes",)))
    for argument in (
        '{"path":"../../etc/passwd"}',
        '{"path":"/etc/passwd"}',
        '{"path":"notes/../../../etc/passwd"}',
    ):
        with pytest.raises(ToolError, match="path_outside_scope"):
            registry.call("read_file", argument)


def test_escape_fallback_rejects_a_path_whose_parent_resolves_outside(ws: Workspace) -> None:
    """Directly exercise the post-walk containment fallback.

    The ``dir_fd`` walk already refuses to follow a symlink, so in normal
    operation this branch cannot be reached; it is the second line of defence
    against a component swapped between the walk and the final path build.
    Fault injection cannot detect its removal (behaviour is unchanged), so it is
    tested by forcing the branch instead of pretending a suite case covers it.
    """
    object.__setattr__(ws, "_root_real", Path("/nonexistent-root"))
    with pytest.raises(WorkspaceError, match="path_outside_scope"):
        ws.resolve("notes/a.md")

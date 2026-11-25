"""SSH file writes use the executor's observed path, bytes, and permissions."""

import io
import json
import sys
from unittest.mock import MagicMock

import pytest

from tools import file_tools
from tools.environments.ssh import SSHEnvironment
from tools.file_operations import ShellFileOperations


@pytest.mark.platforms("posix")
def test_ssh_whole_write_checks_versions_and_policy(tmp_path, monkeypatch, capsys):
    executor_home = tmp_path / "home"
    executor_home.mkdir()
    monkeypatch.setenv("HOME", str(executor_home))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HERMES_WRITE_SAFE_ROOT", str(project))
    host_sibling = tmp_path / "sibling.txt"
    host_sibling.write_text("sibling")
    target = project / "notes.txt"
    target.write_bytes(b"\xef\xbb\xbffirst\r\nsecond\r\n")
    target.chmod(0o755)
    alias = project / "alias"
    alias.symlink_to(target)
    blocked = project / "blocked"
    blocked.symlink_to(host_sibling)
    instruction = project / "AGENTS.md"
    approval = MagicMock(return_value="approval required")
    monkeypatch.setattr("tools.file_tools_write_guards._request_protected_instruction_approval", approval)

    def execute(snippet, *, stdin_data=None):
        with monkeypatch.context() as patch:
            source = io.TextIOWrapper(io.BytesIO((stdin_data or "").encode("ascii")))
            patch.setattr(sys, "stdin", source)
            exec(compile(snippet, "<ssh executor>", "exec"), {})
        return capsys.readouterr().out

    backend = MagicMock(spec=SSHEnvironment, host="executor", port=2222, user="runner", _sync_manager=None)
    operations = ShellFileOperations(backend)
    monkeypatch.setattr(operations, "_framed_ssh_file_output", execute)
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda _: operations)
    monkeypatch.setattr(file_tools, "uses_ssh_for_task", lambda _: True)

    def write(path, data="updated\n"):
        return json.loads(file_tools.write_file_tool(str(path), data, task_id="guarded-ssh-writes"))

    def read(path, **kwargs):
        return json.loads(file_tools.read_file_tool(str(path), task_id="guarded-ssh-writes", **kwargs))

    def patch(path, old, new):
        return json.loads(file_tools.patch_tool(path=str(path), old_string=old, new_string=new,
                                                task_id="guarded-ssh-writes"))

    assert write(target)["stale_write_blocked"]
    assert read(alias, limit=1)["content"].rstrip("\r") == "1|first"
    assert write(target)["stale_write_blocked"]
    assert read(alias, offset=2, limit=1)["content"].rstrip("\r") == "2|second"
    assert write(alias)["verified"]
    assert target.read_bytes() == b"\xef\xbb\xbfupdated\r\n"
    assert target.stat().st_mode & 0o111 == 0o111

    target.write_text("external\n")
    assert write(alias)["stale_write_blocked"]
    assert target.read_text() == "external\n"
    assert "outside the allowed SSH write roots" in write(blocked)["error"]
    assert host_sibling.read_text() == "sibling"
    assert write(instruction)["error"] == "approval required"
    approval.assert_called_once_with(["AGENTS.md"], "guarded-ssh-writes")
    assert not instruction.exists()
    ordinary = project / "instructions-target"
    ordinary.mkdir()
    (project / ".hermes").symlink_to(ordinary, target_is_directory=True)
    relative_instruction = f"{project}/.hermes/./defaults.txt"
    assert write(relative_instruction)["error"] == "approval required"
    approval.assert_called_with([f"{project}/.hermes/defaults.txt"], "guarded-ssh-writes")
    assert not (ordinary / "defaults.txt").exists()
    assert write(project / "new.txt")["verified"]
    assert (project / "new.txt").read_text() == "updated\n"
    assert patch(target, "external", "patched")["success"]
    assert write(target)["stale_write_blocked"]
    assert target.read_text() == "patched\n"
    assert read(target)["content"] == "1|patched"
    assert patch(target, "patched", "checked patch")["success"]
    assert write(target, "after checked patch\n")["verified"]
    assert target.read_text() == "after checked patch\n"

    def apply_v4a(*operations):
        patch_text = "\n".join(("*** Begin Patch", *operations, "*** End Patch"))
        return json.loads(file_tools.patch_tool(mode="patch", patch=patch_text, task_id="guarded-ssh-writes"))

    added, relocated = project / "added.txt", project / "relocated.txt"
    multi = apply_v4a(
        f"*** Add File: {added}\n+first",
        f"*** Update File: {added}\n@@\n-first\n+second",
        f"*** Move File: {added} -> {relocated}",
        f"*** Delete File: {alias}",
        f"*** Update File: {target}\n@@\n-after checked patch\n+target survives",
    )
    assert multi["success"], multi
    assert not added.exists() and not alias.is_symlink()
    assert relocated.read_text() == "second"
    assert target.read_text() == "target survives\n"

    guarded = apply_v4a(
        f"*** Update File: {target}\n@@\n-target survives\n+no edit",
        f"*** Move File: {relocated} -> {host_sibling}",
    )
    assert "outside the allowed SSH write roots" in guarded["error"]
    assert target.read_text() == "target survives\n"
    assert relocated.read_text() == "second"
    assert host_sibling.read_text() == "sibling"

    outside_link = tmp_path / "outside-link"
    outside_link.symlink_to(target)
    assert "outside the allowed SSH write roots" in apply_v4a(f"*** Delete File: {outside_link}")["error"]
    assert outside_link.is_symlink()
    assert target.read_text() == "target survives\n"

    relative_link = project / "relative-link"
    relative_link.symlink_to(target.name)
    moved_link = project / "moved-link"
    through_link = apply_v4a(
        f"*** Move File: {relative_link} -> {moved_link}",
        f"*** Update File: {moved_link}\n@@\n-target survives\n+updated through moved link",
        f"*** Update File: {target}\n@@\n-updated through moved link\n+updated referent next",
    )
    assert through_link["success"], through_link
    assert through_link["files_landed"] == [str(relative_link), str(moved_link), str(target)]
    assert moved_link.is_symlink() and target.read_text() == "updated referent next\n"

    not_moved = project / "not-moved"
    old_spelling = f"{project}/./{moved_link.name}"
    unavailable = apply_v4a(
        f"*** Move File: {moved_link} -> {not_moved}",
        f"*** Update File: {old_spelling}\n@@\n-updated referent next\n+invalid target",
    )
    assert not unavailable["success"] and unavailable["files_landed"] == []
    assert moved_link.is_symlink() and not not_moved.is_symlink()

    deeper = project / "deeper"
    deeper.mkdir()
    retarget = apply_v4a(
        f"*** Move File: {moved_link} -> {not_moved}",
        f"*** Move File: {not_moved} -> {deeper / 'invalid-link'}",
    )
    assert "change a relative symlink target" in retarget["error"]
    assert moved_link.is_symlink() and not not_moved.is_symlink()

    (deeper / target.name).symlink_to(target)
    cross_directory = apply_v4a(f"*** Move File: {moved_link} -> {deeper / 'alternate-chain'}")
    assert "change a relative symlink target" in cross_directory["error"]
    assert not cross_directory.get("files_landed")
    assert moved_link.is_symlink() and not (deeper / "alternate-chain").exists()

    indirect = project / "indirect-link"
    indirect.symlink_to(moved_link.name)
    bridge_move = apply_v4a(
        f"*** Move File: {moved_link} -> {not_moved}",
        f"*** Update File: {indirect}\n@@\n-updated referent next\n+unreachable",
    )
    assert "intermediate symlink" in bridge_move["error"]
    assert moved_link.is_symlink() and not not_moved.is_symlink()

    secret = project / ".bashrc"
    secret.write_text('password: "shortsecret"\nmarker=before\n')
    edited = patch(secret, "marker=before", "marker=after")
    assert edited["success"] and "diff" not in edited
    assert "shortsecret" not in json.dumps(edited)
    assert "marker=after" in secret.read_text()

    target_hint = project / "key-excerpt.txt"
    anchor_line = "marker setting is enabled"
    body = "EXAMPLEMODEonlybaseSixtyFourKEYMATERIAL"
    target_hint.write_text(f"{anchor_line}\n-----BEGIN PRIVATE KEY-----\n{body}\n")
    old = "marker setting is missing"
    replace_miss = patch(target_hint, old, "replacement")
    v4a_miss = apply_v4a(f"*** Update File: {target_hint}\n@@\n-{old}\n+replacement")
    for miss in (replace_miss, v4a_miss):
        assert not miss["success"]
        assert body not in miss["error"]
    target_hint.write_text(f"{anchor_line}\n")
    assert anchor_line in patch(target_hint, old, "replacement")["error"]
    assert anchor_line in apply_v4a(f"*** Update File: {target_hint}\n@@\n-{old}\n+replacement")["error"]

    armor = "-----BEGIN PRIVATE KEY-----"
    keys = f"{armor}\n{body}\n{body}\n"
    target_hint.write_text(keys)
    ambiguous = patch(target_hint, "MATERIAL", "UNIQUE")
    assert not ambiguous["success"] and body not in ambiguous["error"]
    early_remove = apply_v4a(f"*** Update File: {target_hint}\n@@\n-{armor}\n+sample\n"
                             "@@\n-MATERIAL\n+UNIQUE")
    assert not early_remove["success"] and body not in early_remove["error"]
    edited = patch(target_hint, armor, "sample")
    assert edited["success"] and "diff" not in edited
    assert body not in json.dumps(edited) and target_hint.read_text().startswith("sample\n")

    target_hint.write_text(keys)
    edited = apply_v4a(
        f"*** Update File: {target_hint}\n@@\n-{armor}\n+sample",
        f"*** Update File: {target_hint}\n@@\n-sample\n+changed sample",
    )
    assert edited["success"] and "diff" not in edited
    assert body not in json.dumps(edited) and target_hint.read_text().startswith("changed sample\n")

    def lint_unavailable(*args, **kwargs):
        raise OSError("fixture SSH connection failed after publication")

    with monkeypatch.context() as settings:
        settings.setattr(operations, "_check_lint_delta", lint_unavailable)
        lint_target = project / "lint-target.py"
        saved = write(lint_target, "first = 1\n")
        assert saved["verified"] and saved["files_landed"] == [str(lint_target)]
        assert saved["lint"]["status"] == "skipped"
        edited = patch(lint_target, "first", "second")
        assert edited["success"] and edited["lint"]["status"] == "skipped"
        renamed = project / "renamed.py"
        batch = apply_v4a(
            f"*** Update File: {lint_target}\n@@\n-second = 1\n+third = 1",
            f"*** Move File: {lint_target} -> {renamed}",
        )
        assert batch["success"], batch
        assert batch["files_landed"] == [str(lint_target), str(renamed)]
        assert batch["lint"][str(lint_target)]["status"] == "skipped"
        assert renamed.read_text() == "third = 1\n"
        unlinted = write(project / "path-lint.js", "let value = 1;\n")
        assert unlinted["verified"] and "edited bytes" in unlinted["lint"]["message"]

    json_bom = project / "with-bom.json"
    json_bom.write_bytes(b'\xef\xbb\xbf{"value": 1}\r\n')
    edited_json = patch(json_bom, '"value": 1', '"value": 2')
    assert edited_json["success"] and edited_json["lint"]["status"] == "ok"
    assert json_bom.read_bytes() == b'\xef\xbb\xbf{"value": 2}\r\n'

    python_bom = project / "with-bom.py"
    python_bom.write_bytes(b"\xef\xbb\xbf# coding: utf-8\nvalue = 1\n")
    conflict = patch(python_bom, "coding: utf-8", "coding: latin-1")
    assert conflict["success"] and conflict["lint"]["status"] == "error"
    assert "encoding problem" in conflict["lint"]["output"]
    assert python_bom.read_bytes().startswith(b"\xef\xbb\xbf# coding: latin-1")

    configured = project / ".configured"
    sessions = configured / "profiles" / "sibling" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(configured))
    history = sessions / "saved.json"
    history.write_text('{"fixture": "saved state"}')
    assert "protected Hermes" in apply_v4a(f"*** Delete File: {history}")["error"]
    assert history.exists()

    ssh_home = executor_home / ".ssh"
    ssh_home.mkdir()
    keys = ssh_home / "authorized_keys"
    keys.write_text("fixture key")
    (ssh_home / "config").symlink_to(keys)
    monkeypatch.setenv("HERMES_WRITE_SAFE_ROOT", str(tmp_path))
    assert "credential file" in patch(ssh_home / "config", "fixture key", "different")["error"]
    assert keys.read_text() == "fixture key"
    relocated_key = project / "relocated-key"
    relocated_key.write_text("fixture key bytes")
    (ssh_home / "id_ed25519").symlink_to(relocated_key)
    assert "credential file" in patch(relocated_key, "fixture key bytes", "changed")["error"]
    ssh_home.rename(executor_home / "saved-ssh")
    aws = executor_home / ".aws"
    aws.mkdir()
    (aws / "config").write_text("fixture credential configuration")
    ssh_home.symlink_to(aws, target_is_directory=True)
    assert "credential file" in patch(ssh_home / "config", "fixture credential configuration", "changed")["error"]

    physical_entry = aws / "outgoing"
    physical_entry.symlink_to(target)
    alias_home = project / "credentials-alias"
    alias_home.symlink_to(aws, target_is_directory=True)
    outbound = alias_home / "outgoing"
    assert "credential file" in patch(outbound, "updated referent next", "invalid target")["error"]
    assert target.read_text() == "updated referent next\n"

    move_source = project / "move-source"
    move_source.write_text("before validate\n")
    move_target = project / "move-target"
    landed_file = project / "landed-file"

    def edit_before_move(snippet, *, stdin_data=None):
        if "\nprint(json.dumps(mutate_entry(" in snippet:
            move_source.write_text("external editor\n")
        return execute(snippet, stdin_data=stdin_data)

    monkeypatch.setattr(operations, "_framed_ssh_file_output", edit_before_move)
    partial = apply_v4a(f"*** Add File: {landed_file}\n+landed",
                        f"*** Move File: {move_source} -> {move_target}")
    assert not partial["success"] and "changed before the edit" in partial["error"]
    assert partial["files_landed"] == [str(landed_file)]
    assert move_source.read_text() == "external editor\n" and not move_target.exists()

    to_delete = project / "delete-source"
    to_delete.write_text("unchanged at validation\n")
    inspections = 0

    def edit_before_delete(snippet, *, stdin_data=None):
        nonlocal inspections
        expression = f"\nprint(json.dumps(inspect_write({str(to_delete)!r}"
        if expression in snippet and "entry=False" in snippet.rsplit(expression, 1)[1]:
            inspections += 1
            if inspections == 2:
                to_delete.write_text("replacement at apply\n")
        return execute(snippet, stdin_data=stdin_data)

    monkeypatch.setattr(operations, "_framed_ssh_file_output", edit_before_delete)
    changed_delete = apply_v4a(f"*** Delete File: {to_delete}")
    assert "changed before the edit" in changed_delete["error"]
    assert changed_delete["files_landed"] == []
    assert to_delete.read_text() == "replacement at apply\n"

    incoming = project / "incoming"
    incoming.write_text("prior move text\n")
    moved = project / "moved"
    alternative = f"{project}/./{moved.name}"
    swapped = False

    def replace_after_move(snippet, *, stdin_data=None):
        nonlocal swapped
        if not swapped and moved.is_file() and f"\nprint(json.dumps(inspect_write({alternative!r}" in snippet:
            replacement = project / "external-replacement"
            replacement.write_text("prior move text\n")
            replacement.replace(moved)
            swapped = True
        return execute(snippet, stdin_data=stdin_data)

    monkeypatch.setattr(operations, "_framed_ssh_file_output", replace_after_move)
    partial = apply_v4a(
        f"*** Move File: {incoming} -> {moved}",
        f"*** Update File: {alternative}\n@@\n-prior move text\n+unapproved replacement",
    )
    assert swapped and partial["files_landed"] == [str(incoming), str(moved)]
    assert "changed before the edit" in partial["error"]
    assert moved.read_text() == "prior move text\n"

    appeared = project / "unrelated-new-file"
    later = project / "later-target"

    def race_create(snippet, *, stdin_data=None):
        if f"\nprint(json.dumps(publish({str(appeared)!r}" in snippet and not appeared.exists():
            appeared.write_text("external content")
        return execute(snippet, stdin_data=stdin_data)

    monkeypatch.setattr(operations, "_framed_ssh_file_output", race_create)
    partial = apply_v4a(
        f"*** Add File: {appeared}\n+external content",
        f"*** Update File: {appeared}\n@@\n-external content\n+changed by patch",
        f"*** Add File: {later}\n+should not land after failure",
    )
    assert not partial["success"] and partial["files_landed"] == []
    assert appeared.read_text() == "external content" and not later.exists()

    link_source = project / "link-source"
    link_destination = project / "link-destination"
    linked_file = project / "linked-file"
    linked_file.write_text("same bytes after replacement\n")
    link_source.symlink_to(linked_file.name)
    swapped = False

    def replace_moved_referent(snippet, *, stdin_data=None):
        nonlocal swapped
        if not swapped and link_destination.is_symlink():
            replacement = project / "replaced-referent"
            replacement.write_text(linked_file.read_text())
            replacement.replace(linked_file)
            swapped = True
        return execute(snippet, stdin_data=stdin_data)

    monkeypatch.setattr(operations, "_framed_ssh_file_output", replace_moved_referent)
    partial = apply_v4a(
        f"*** Move File: {link_source} -> {link_destination}",
        f"*** Update File: {link_destination}\n@@\n-same bytes after replacement\n+unexpected overwrite",
    )
    assert swapped and not partial["success"] and "referent changed" in partial["error"]
    assert partial["files_landed"] == [str(link_source), str(link_destination)]
    assert linked_file.read_text() == "same bytes after replacement\n"

    replaced_link = project / "replaced-link"
    replaced_link.symlink_to(linked_file.name)

    def swap_before_publish(snippet, *, stdin_data=None):
        if f"\nprint(json.dumps(publish({str(replaced_link)!r}" in snippet:
            replacement = project / "replacement-link"
            replacement.symlink_to(linked_file.name)
            replacement.replace(replaced_link)
        return execute(snippet, stdin_data=stdin_data)

    monkeypatch.setattr(operations, "_framed_ssh_file_output", swap_before_publish)
    swapped_link = patch(replaced_link, "same bytes after replacement", "stale link write")
    assert not swapped_link.get("success") and "path changed" in swapped_link["error"]
    assert linked_file.read_text() == "same bytes after replacement\n"

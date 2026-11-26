"""Gateway replies keep executor artifacts separate from profile-owned files."""

import base64
from pathlib import Path
import posixpath

from gateway.platforms import base
from gateway.ssh_media import fetch_managed_media
from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError
from tools.file_operations import ReadResult, SSHPathState


def test_managed_reply_never_probes_a_same_named_hermes_file(tmp_path, monkeypatch):
    from gateway import ssh_media

    artifact = "/workspace/out.txt"
    cache = str(tmp_path / "profile" / "generated.ogg")
    fetched = []
    local_reads = []

    def resolve(path, **_):
        fetched.append(path)
        return str(tmp_path / "staged-executor.txt") if path == artifact else None

    def validate(path, **_):
        local_reads.append(path)
        return path if path == cache else None

    monkeypatch.setattr(base, "_managed_media_delivery", lambda: True)
    monkeypatch.setattr(ssh_media, "fetch_managed_media", resolve)
    monkeypatch.setattr(base, "validate_media_delivery_path", validate)

    def unexpected(_):
        raise AssertionError("A managed reply tried to probe Hermes")

    monkeypatch.setattr(base, "_existing_regular_file", unexpected)
    response = f"Report\nMEDIA:{artifact}\nMEDIA:~/not-there.txt\nMEDIA:{cache}"
    media, text = base.BasePlatformAdapter.extract_media(response)
    assert fetched == local_reads == []
    assert text == "Report"
    delivered = base.BasePlatformAdapter.filter_media_delivery_paths(media, approved=(cache,))
    assert delivered == [(str(tmp_path / "staged-executor.txt"), False), (cache, False)]
    assert fetched == [artifact, "~/not-there.txt"]
    assert local_reads == [cache]


def test_guarded_delivery_uses_the_executor_without_an_existing_terminal(tmp_path, monkeypatch):
    cache = tmp_path / "documents"
    cache.mkdir()
    collision = "/workspace/out.txt"
    seen = []
    remote = {"available": True}

    class Workspace:
        _env = object()

        def normalize(self, path):
            return "/home/runner" if path == "~" else posixpath.normpath(path)

    class Operations:
        def __init__(self, environment):
            assert environment is Workspace._env

        def inspect_ssh_path(self, path, *, protect_read):
            assert protect_read
            seen.append(path)
            if not remote["available"]:
                raise SshWorkspaceFsError("ECONN")
            return SSHPathState(path, "regular", (1, 2, 8, 3, 4))

        def read_inspected_ssh_bytes(self, state, size_limit):
            assert state.path == collision and size_limit >= 8
            return ReadResult(base64_content=base64.b64encode(b"executor").decode(), file_size=8)

    monkeypatch.setattr("hermes_cli.ssh_project_probe.managed_project_access", lambda _: (("/workspace",), {}))
    monkeypatch.setattr("hermes_cli.ssh_project_probe.managed_project_connection", lambda *_: Workspace())
    monkeypatch.setattr("tools.terminal_scope.get_terminal_scope", lambda: None)
    monkeypatch.setattr("tools.file_operations.ShellFileOperations", Operations)
    monkeypatch.setattr(base, "get_document_cache_dir", lambda: cache)
    monkeypatch.setattr(base, "validate_media_delivery_path", lambda path: path)

    staged = fetch_managed_media(collision)
    assert Path(staged).read_bytes() == b"executor"
    assert seen == [collision]
    assert fetch_managed_media("out.txt", cwd="/retired/project") is None
    assert seen == [collision]
    remote["available"] = False
    assert fetch_managed_media(collision) is None
    assert list(cache.iterdir()) == [Path(staged)]

import base64
from pathlib import Path

from agent.context_references import preprocess_context_references
from agent.ssh_context_references import ReferenceWorkspace
from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError


def test_managed_chat_references_use_executor_bytes(tmp_path, monkeypatch):
    decoy = tmp_path / "note.txt"
    decoy.write_text("hermes decoy")
    requests = []

    class Backend:
        def reference_path(self, path, roots, **options):
            requests.append((path, roots, options))
            if options["kind"] == "directory":
                return path, 0, [["remote-file", 11]]
            if path.endswith("note.txt"):
                data = base64.b64encode(b"executor one\nexecutor two\n").decode()
                sliced = base64.b64encode(b"executor two\n").decode()
                return path, 26, [data, sliced]
            raise SshWorkspaceFsError("ENOENT", "Executor entry unavailable")

    monkeypatch.setattr(
        "agent.ssh_context_references.reference_workspace",
        lambda _: ReferenceWorkspace(str(tmp_path), Backend()),
    )
    result = preprocess_context_references(
        "Read @file:note.txt:2 @folder:. @file:missing", cwd=tmp_path, context_length=2000,
    )
    assert "executor two" in result.message
    assert "executor one" not in result.message
    assert "hermes decoy" not in result.message
    assert "remote-file" in result.message
    assert "Executor entry unavailable" in result.message
    assert any(options.get("start") == 2 for _, _, options in requests)
    assert all(roots == (str(tmp_path),) for _, roots, _ in requests)


def test_failed_ssh_reference_does_not_probe_the_same_host_path(tmp_path, monkeypatch):
    source = tmp_path / "note.txt"
    source.write_text("hermes decoy")
    resolve = Path.resolve

    def fail_backend(_):
        raise SshWorkspaceFsError("ECONN", "SSH executor unavailable")

    def check_resolve(path, *args, **kwargs):
        assert path not in (tmp_path, source), "The Hermes checkout was probed"
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr("agent.ssh_context_references.reference_workspace", fail_backend)
    monkeypatch.setattr(Path, "resolve", check_resolve)
    result = preprocess_context_references("Read @file:note.txt @diff", cwd=tmp_path, context_length=2000)
    assert "SSH executor unavailable" in result.message
    assert "hermes decoy" not in result.message


def test_executor_prompt_and_context_manifest_share_git_root_to_cwd_order(tmp_path, monkeypatch):
    from agent.context_file_sources import list_context_file_sources
    from agent.prompt_builder import build_context_files_prompt
    from agent.ssh_project_context import executor_coding_workspace

    root = tmp_path / "project"
    cwd = root / "nested"
    cwd.mkdir(parents=True)
    (root / "AGENTS.md").write_text("Hermes root decoy")
    (cwd / ".hermes.md").write_text("Hermes preferred decoy")
    files = {
        str(root / "AGENTS.md"): b"Executor project instructions",
        str(cwd / "AGENTS.override.md"): b"Executor nested override",
        str(cwd / "AGENTS.md"): b"Executor shadowed nested instructions",
        str(cwd / "CLAUDE.md"): b"Executor lower-priority instructions",
    }

    class Backend:
        def inspect_projects(self, paths, grants, **kwargs):
            return {str(cwd): {"cwd": str(cwd), "worktree_root": str(root), "branch": "executor-branch"}}

        def reference_path(self, path, roots, **options):
            if path == str(root) and options["kind"] == "directory":
                return path, 0, [["pyproject.toml", 10]]
            if path not in files:
                raise SshWorkspaceFsError("ENOENT")
            raw = files[path]
            encoded = base64.b64encode(raw).decode()
            return path, len(raw), [encoded, encoded]

    monkeypatch.setattr("hermes_cli.main_resume_args.uses_managed_executor", lambda: True)
    monkeypatch.setattr(
        "agent.ssh_project_context._selected_workspace",
        lambda: ReferenceWorkspace(str(cwd), Backend(), (str(root),)),
    )
    prompt = build_context_files_prompt(cwd=str(cwd), skip_soul=True, context_length=20000)
    assert prompt.index("Executor project instructions") < prompt.index("Executor nested override")
    assert "Hermes" not in prompt and "lower-priority" not in prompt and "shadowed nested" not in prompt
    sources = list_context_file_sources(cwd=str(cwd), skip_soul=True, context_length=20000)
    assert {entry["label"]: entry["status"] for entry in sources} == {
        "../AGENTS.md": "loaded",
        "AGENTS.override.md": "loaded",
        "CLAUDE.md": "shadowed",
    }
    selected, coding = executor_coding_workspace()
    assert selected == str(cwd)
    assert "executor-branch" in coding and "pyproject.toml" in coding


def test_executor_git_references_keep_staged_and_unstaged_changes_separate(tmp_path):
    import subprocess
    import sys

    from hermes_cli.ssh_project_git import git_reference

    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    file = tmp_path / "note.txt"
    file.write_text("first commit\n")
    git("add", "note.txt")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "first commit")
    file.write_text("staged change\n")
    git("add", "note.txt")
    file.write_text("unstaged change\n")

    class Transport:
        def _framed_ssh_file_output(self, script, *, stdin_data):
            return subprocess.run([sys.executable, "-I", "-c", script], input=stdin_data,
                                  capture_output=True, text=True, check=True).stdout

    backend = Transport()
    cwd = str(tmp_path)
    unstaged = git_reference(backend, cwd, (cwd,), ["diff"], 8192)
    staged = git_reference(backend, cwd, (cwd,), ["diff", "--staged"], 8192)
    log = git_reference(backend, cwd, (cwd,), ["log", "-1", "-p"], 8192)
    assert "+unstaged change" in unstaged
    assert "+staged change" in staged
    assert "+first commit" in log


def test_only_issued_staged_handles_read_the_owning_sessions_hermes_bytes(tmp_path, monkeypatch):
    from agent.ssh_staged_references import attachment_directory, attachment_reference

    home = tmp_path / "hermes"
    directory = attachment_directory(home, "owner")
    directory.mkdir(parents=True)
    staged = directory / ("a" * 32 + "-report.txt")
    staged.write_text("Owner staged bytes")
    secret = home / "cache" / "spillover" / "draft.txt"
    secret.parent.mkdir(parents=True)
    secret.write_text("Hermes private cache decoy")
    monkeypatch.setenv("HERMES_HOME", str(home))

    def offline(_):
        raise SshWorkspaceFsError("ECONN", "SSH executor unavailable")

    monkeypatch.setattr("agent.ssh_context_references.reference_workspace", offline)
    ref = attachment_reference(staged)
    message = f"Read @file:{ref} @file:{secret} @folder:{secret.parent} @file:{staged}"
    result = preprocess_context_references(message, cwd=tmp_path, context_length=2000, attachment_session_id="owner")
    assert result.message.count("Owner staged bytes") == 1
    assert "Hermes private cache decoy" not in result.message
    foreign = preprocess_context_references(message, cwd=tmp_path, context_length=2000, attachment_session_id="other")
    assert "Owner staged bytes" not in foreign.message
    assert "staged attachment is unavailable" in foreign.message

"""Tests for managed SSH skill inline-shell placement and project skill boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent import skill_preprocessing
from agent.ssh_skill_shell import managed_skill_workspace
from hermes_cli.ssh_workspace_fs import SshWorkspaceFsError
from tools import skills_tool
from tools.environments.file_sync import FileSyncManager


def test_managed_inline_shell_runs_over_ssh_and_maps_skill_dir(monkeypatch, tmp_path):
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "ops" / "demo"
    skill_dir.mkdir(parents=True)
    calls = []

    class Backend:
        def normalize(self, path):
            assert path == "~"
            return "/home/runner"

        def sync_skill_files(self):
            calls.append(("sync",))

        def run_skill_shell(self, command, cwd, timeout):
            calls.append(("run", command, cwd, timeout))
            return {"returncode": 0, "output": "runner-ok\n"}

    monkeypatch.setattr("hermes_cli.main_resume_args.uses_managed_executor", lambda: True)
    monkeypatch.setattr("agent.ssh_context_references.reference_workspace",
                        lambda _cwd=None: SimpleNamespace(backend=Backend(), grants=("/workspace",)))
    monkeypatch.setattr("tools.credential_files._skill_dir_roots",
                        lambda base: [(skills_root, f"{base}/skills")])
    monkeypatch.setattr(skill_preprocessing, "run_inline_shell",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("local shell used")))

    rendered = skill_preprocessing.preprocess_skill_content(
        "dir=${HERMES_SKILL_DIR} out=!`./helper.sh`",
        skill_dir,
        skills_cfg={"template_vars": True, "inline_shell": True, "inline_shell_timeout": 15},
    )

    assert rendered == "dir=/home/runner/.hermes/skills/ops/demo out=runner-ok"
    assert calls == [
        ("sync",),
        ("run", "./helper.sh", "/home/runner/.hermes/skills/ops/demo", 15),
    ]


def test_managed_inline_shell_refuses_outage_or_unmappable_dir_without_local_fallback(monkeypatch, tmp_path):
    skill_dir = tmp_path / "unmapped"
    skill_dir.mkdir()
    monkeypatch.setattr("hermes_cli.main_resume_args.uses_managed_executor", lambda: True)
    monkeypatch.setattr("agent.ssh_context_references.reference_workspace", lambda _cwd=None: None)
    monkeypatch.setattr(skill_preprocessing, "run_inline_shell",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("local shell used")))

    rendered = skill_preprocessing.preprocess_skill_content(
        "value=!`id -u`",
        skill_dir,
        skills_cfg={"template_vars": True, "inline_shell": True},
    )

    assert rendered.startswith("value=[inline-shell refused:")
    assert "unavailable" in rendered


def test_managed_skill_workspace_rejects_missing_executor_project_dir(monkeypatch):
    class Backend:
        def inspect_projects(self, paths, allowed):
            assert paths == ["/mnt/proj/.hermes/skills/demo"]
            assert allowed == ("/workspace", "/mnt/proj")
            return {paths[0]: {"cwd": None}}

    monkeypatch.setattr("agent.ssh_context_references.reference_workspace",
                        lambda _cwd=None: SimpleNamespace(backend=Backend(), grants=("/workspace", "/mnt/proj")))

    try:
        managed_skill_workspace(Path("/mnt/proj/.hermes/skills/demo"), project=True)
    except SshWorkspaceFsError as exc:
        assert exc.code == "ENOENT"
    else:
        raise AssertionError("expected ENOENT for unavailable project skill dir")


def test_managed_project_skill_view_blocks_profile_credentials_and_host_deps(monkeypatch):
    skill_md = (
        "---\n"
        "name: repo-tool\n"
        "description: Project skill\n"
        "deps:\n"
        "  - some-pkg\n"
        "required_environment_variables:\n"
        "  - name: SECRET_TOKEN\n"
        "---\n"
        "Use ./run.sh from ${HERMES_SKILL_DIR}.\n"
    )
    monkeypatch.setattr("hermes_cli.main_resume_args.uses_managed_executor", lambda: True)
    monkeypatch.setattr("agent.ssh_project_skills.load_project_skill", lambda name, file_path: {
        "directory": Path("/mnt/proj/.hermes/skills/repo-tool"),
        "content": skill_md,
        "load_path": "/mnt/proj/.hermes/skills/repo-tool/SKILL.md",
        "linked": ["run.sh"],
        "support": None,
    })
    monkeypatch.setattr(skills_tool, "_skill_readiness",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("readiness called")))

    payload = json.loads(skills_tool.skill_view("repo-tool", preprocess=False))

    assert payload["success"] is True
    assert payload["executor_project"] is True
    assert payload["skill_dir"] == "/mnt/proj/.hermes/skills/repo-tool"
    assert payload["deps_note"] == "Project skill dependencies must be available in the SSH executor."
    assert payload["setup_note"] == "Project skills cannot request credentials from the Hermes profile."


def test_file_sync_manager_skips_sync_back_for_upload_only_skill_roots(tmp_path):
    host_skill = tmp_path / "host_skill.md"
    host_skill.write_text("original", encoding="utf-8")
    staged_skill = tmp_path / "staged_skill.md"
    staged_skill.write_text("tampered-in-executor", encoding="utf-8")
    host_data = tmp_path / "host_data.txt"
    host_data.write_text("before", encoding="utf-8")
    staged_data = tmp_path / "staged_data.txt"
    staged_data.write_text("after", encoding="utf-8")

    mgr = FileSyncManager(
        get_files_fn=list,
        upload_fn=lambda _s, _d: None,
        delete_fn=lambda _d: None,
        upload_only_remote_roots=("/home/runner/.hermes/skills",),
    )

    mapping = [
        (str(host_skill), "/home/runner/.hermes/skills/demo/SKILL.md"),
        (str(host_data), "/home/runner/.hermes/notes/data.txt"),
    ]
    skipped = mgr._apply_staged_file(str(staged_skill), "/home/runner/.hermes/skills/demo/SKILL.md", mapping, set())
    applied = mgr._apply_staged_file(str(staged_data), "/home/runner/.hermes/notes/data.txt", mapping, set())

    assert skipped == 0
    assert host_skill.read_text(encoding="utf-8") == "original"
    assert applied == 1
    assert host_data.read_text(encoding="utf-8") == "after"

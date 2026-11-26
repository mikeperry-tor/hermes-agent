"""Write tool policy and version checks for SSH files."""

import hashlib
import json
import logging
import os
import posixpath

from agent.file_safety import (
    HOME_CREDENTIAL_DIRS,
    HOME_CREDENTIAL_FILES,
    SHELL_RC_FILES,
    SSH_KEY_FILES,
    WRITE_PROTECTED_SYSTEM_FILES,
    get_read_block_error,
    ssh_read_protected_entries,
)
from agent.redact import _is_secret_file_arg, redact_sensitive_text
from hermes_cli.home_data_layout import HERMES_PROTECTED_SUBPATHS
from tools import file_state
from tools.file_operations import (
    _RAW_READ_MAX_BYTES,
    _refuse_introduced_json_constant,
)
from tools.file_operations_common import LintResult, _detect_line_ending, _normalize_line_endings, _strip_bom
from tools.file_operations_lint import LINTERS, LINTERS_INPROC
from tools.file_operations_ssh_write import inspect_ssh_write, publish_ssh_file, read_ssh_write_source
from tools.file_tools_paths import managed_ssh_workspace_roots
from tools.file_tools_read_tracking import SSHFileKey, record_ssh_write, ssh_write_baseline
from tools.file_tools_write_guards import (
    _SENSITIVE_EXACT_PATHS,
    _SENSITIVE_PATH_PREFIXES,
    _check_binary_document_write,
    _check_ssh_instruction_write,
    _is_internal_file_tool_content,
    _request_write_approvals,
    _stale_write_refusal,
)
from tools.registry import tool_error

logger = logging.getLogger(__name__)


def executor_write_policy(roots: tuple[str, ...], *, managed: bool) -> tuple:
    return (ssh_read_protected_entries(), (*HOME_CREDENTIAL_FILES, *SSH_KEY_FILES),
            HOME_CREDENTIAL_DIRS, SHELL_RC_FILES, HERMES_PROTECTED_SUBPATHS, _SENSITIVE_PATH_PREFIXES,
            tuple(sorted(_SENSITIVE_EXACT_PATHS | set(WRITE_PROTECTED_SYSTEM_FILES))), roots, managed)


def ssh_write_policy() -> tuple:
    managed = managed_ssh_workspace_roots()
    if managed is not None:
        return executor_write_policy(managed, managed=True)
    roots = tuple(filter(None, os.getenv("HERMES_WRITE_SAFE_ROOT", "").split(os.pathsep)))
    return executor_write_policy(roots, managed=False)


def ssh_content_denial(inspected, task_id: str) -> str | None:
    state = inspected.destination
    known = "exists" if state.kind == "regular" else "absent"
    for path in (inspected.requested, state.path, inspected.entry):
        error = (get_read_block_error(path, ssh_path=True)
                 or _check_binary_document_write(path, task_id, inspected=known))
        if error:
            return error
    return None


def approve_ssh_write(inspected, task_id: str) -> str | None:
    paths = list(dict.fromkeys((inspected.requested, inspected.destination.path, inspected.entry)))
    return (ssh_content_denial(inspected, task_id)
            or _check_ssh_instruction_write(paths, task_id)
            or _request_write_approvals({key: [inspected.requested] for key in inspected.approvals}))


def ssh_edit_text(text: str, paths: list[str]) -> str:
    secret = any(_is_secret_file_arg(path, ssh_path=True) for path in paths)
    return redact_sensitive_text(text, file_read=True, secret_file=secret)


def ssh_edit_report(response: dict, paths: list[str]) -> str:
    def redact_value(value):
        if isinstance(value, str):
            return ssh_edit_text(value, paths)
        if isinstance(value, list):
            return [redact_value(item) for item in value]
        if isinstance(value, dict):
            return {key: redact_value(item) for key, item in value.items()}
        return value

    for key in ("error", "note", "warning", "lint", "lsp_diagnostics"):
        if key in response:
            response[key] = redact_value(response[key])
    return json.dumps(response)


def _publish_whole_file(ops, requested, inspected, content, expected, policy, source=None,
                        *, hide_diagnostics=False):
    state = inspected.destination
    path = state.path
    paths = [inspected.requested, path, inspected.entry]
    ext = posixpath.splitext(path)[1].lower()
    want_pre = ext in LINTERS_INPROC
    maximum = _RAW_READ_MAX_BYTES if want_pre else 4096
    if source is None:
        source = read_ssh_write_source(ops, state, maximum)
    original = source.decode("utf-8", "surrogateescape")
    pre_content, bom = _strip_bom(original)
    hide_diagnostics = (hide_diagnostics or ssh_edit_text(pre_content, paths) != pre_content
                        or ssh_edit_text(content, paths) != content)
    if state.metadata and state.metadata[2] != len(source):
        pre_content = None
        hide_diagnostics |= want_pre
    refused = ops._reject_unencodable(path, content) or ops._fail_closed_syntax_error(path, ext, content)
    if not refused and ext == ".json":
        refused = _refuse_introduced_json_constant(path, content, pre_content)
    if refused:
        if hide_diagnostics:
            refused.error = "The SSH write failed text or syntax validation."
        return refused
    if _detect_line_ending(original) == "\r\n":
        content = _normalize_line_endings(content, "\r\n")
    if bom and not content.startswith("\ufeff"):
        content = "\ufeff" + content
    result = publish_ssh_file(ops, requested, inspected, expected, content.encode("utf-8", "surrogateescape"), policy)
    if result.error:
        return result
    if hide_diagnostics:
        lint = LintResult(skipped=True, message="SSH lint diagnostics are hidden for this edit.")
    elif ext not in LINTERS_INPROC:
        message = ("SSH lint requires a check that can use the edited bytes."
                   if ext in LINTERS else f"No linter for {ext} files")
        lint = LintResult(skipped=True, message=message)
    else:
        try:
            before = original if ext == ".py" and pre_content is not None else pre_content
            after = content if ext == ".py" else _strip_bom(content)[0]
            lint = ops._check_lint_delta(path, pre_content=before, post_content=after)
        except (OSError, ValueError, RuntimeError) as exc:
            # The executor has already confirmed publication. Keep the receipt even if lint fails.
            logger.warning("Post-write SSH lint failed (%s)", type(exc).__name__)
            lint = LintResult(skipped=True, message="The SSH file was saved, but lint could not run.")
    result.lint = lint.to_dict()
    return result


def write_ssh_file(ops, requested: str, content: str, task_id: str) -> str:
    """Write an absent file or replace the version read by this task."""
    if _is_internal_file_tool_content(content):
        return tool_error("Strip read_file display prefixes before writing file content.")
    policy = ssh_write_policy()
    inspected = inspect_ssh_write(ops, requested, policy)
    state = inspected.destination
    error = approve_ssh_write(inspected, task_id)
    if error:
        return tool_error(error)
    key = SSHFileKey.for_backend(ops.env, state.path)
    with file_state.lock_path(repr(key)):
        expected = None
        if state.kind == "regular":
            expected = ssh_write_baseline(task_id, key)
            if expected is None or expected[:5] != state.metadata:
                reason = "Read every page of the current SSH file before replacing it, or use a targeted patch."
                return json.dumps(_stale_write_refusal(requested, reason, state.path))
        result = _publish_whole_file(ops, requested, inspected, content, expected, policy)
        response = result.to_dict()
        response["resolved_path"] = state.path
        if result._entry_landed:
            response["files_landed"] = [state.path]
        if not result.error:
            if result._snapshot is not None:
                record_ssh_write(task_id, key, result._snapshot)
            response["files_modified"] = [state.path]
        return ssh_edit_report(response, [inspected.requested, state.path, inspected.entry])


def patch_ssh_replace(ops, requested: str, old: str, new: str, replace_all: bool, task_id: str) -> str:
    policy = ssh_write_policy()
    inspected = inspect_ssh_write(ops, requested, policy)
    if error := approve_ssh_write(inspected, task_id):
        return tool_error(error)
    state = inspected.destination
    if state.kind != "regular":
        return tool_error(f"The SSH file {state.path!r} does not exist.")
    key = SSHFileKey.for_backend(ops.env, state.path)
    paths = [inspected.requested, state.path, inspected.entry]
    with file_state.lock_path(repr(key)):
        source = read_ssh_write_source(ops, state, state.metadata[2])
        version = (*state.metadata, hashlib.sha256(source).digest())
        original = source.decode("utf-8", "surrogateescape")
        replacement, result = ops._prepare_patch_replace(
            state.path, original, old, new, replace_all, hint_text=lambda text: ssh_edit_text(text, paths))
        if replacement is None:
            return ssh_edit_report({**result.to_dict(), "files_landed": []}, paths)
        written = _publish_whole_file(ops, requested, inspected, replacement, version, policy, source)
        if written.error:
            if written._entry_landed:
                return ssh_edit_report({"error": written.error, "files_landed": [state.path]}, paths)
            return ssh_edit_report({"error": f"Failed to write changes: {written.error}"}, paths)
        if written._snapshot is not None and ssh_write_baseline(task_id, key) == version:
            record_ssh_write(task_id, key, written._snapshot)
        before = _strip_bom(original)[0]
        if ssh_edit_text(before, paths) == before and ssh_edit_text(replacement, paths) == replacement:
            result.diff = ops._unified_diff(before, replacement, state.path)
        else:
            result.note = "The SSH patch diff is hidden because the edit includes redacted text."
        result.lint = written.lint
        response = result.to_dict()
        response["resolved_path"] = state.path
        return ssh_edit_report(response, paths)

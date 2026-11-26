"""Apply parsed V4A operations with SSH path and file-version checks."""

import hashlib
import json
from contextlib import ExitStack
from dataclasses import replace
from typing import NamedTuple

from tools import file_state
from tools.file_operations import _RAW_READ_MAX_BYTES
from tools.file_operations_common import ReadResult, WriteResult, _strip_bom
from tools.file_operations_ssh_write import (
    SSHWritePath,
    check_ssh_link_move,
    inspect_ssh_write,
    mutate_ssh_entry,
    read_ssh_write_source,
)
from tools.file_tools_paths import _anchor_ssh_path, _hosted_tilde
from tools.file_tools_read_tracking import SSHFileKey, record_ssh_write, ssh_write_baseline
from tools.file_tools_ssh_writes import (
    _publish_whole_file,
    ssh_content_denial,
    ssh_edit_report,
    ssh_edit_text,
    ssh_write_policy,
)
from tools.file_tools_write_guards import _check_ssh_instruction_write, _request_write_approvals
from tools.patch_parser import OperationType, apply_v4a_operations, parse_v4a_patch
from tools.path_security import has_traversal_component
from tools.registry import tool_error
from agent.file_safety import get_nt_namespace_error, get_read_block_error


class AppliedEntry(NamedTuple):
    path: str
    entry_metadata: tuple | None
    referent_metadata: tuple | None
    approvals: tuple[str, ...]


class SSHV4AFileOperations:
    """Bind the patch parser's reads and writes to approved executor paths."""

    def __init__(self, ops, task_id, policy, approved):
        self.ops = ops
        self.env = ops.env
        self.task_id = task_id
        self.policy = policy
        self.approved = approved
        self.last_read = None
        self.hidden_source = False
        self.entries = {path: state for (path, entry), state in approved.items() if entry}
        self.edited: dict[str, AppliedEntry | None] = {}
        self.paths = list(dict.fromkeys(path for inspected in approved.values()
                                       for path in (inspected.requested, inspected.destination.path, inspected.entry)))

    def _observe(self, path: str, *, entry: bool = False) -> SSHWritePath:
        approved = self.approved.get((path, entry)) or self.approved.get((path, not entry))
        if approved is None:
            raise ValueError("The SSH path was not included in patch validation.")
        previous_edit = self.edited.get(approved.entry)
        if previous_edit is None and approved.entry in self.edited:
            raise ValueError("The SSH patch could not confirm the last edit at this path.")
        observed = inspect_ssh_write(self.ops, path, self.policy, entry=entry)
        destination, approvals = approved.destination.path, set(approved.approvals)
        entry_version = approved.entry_metadata
        referent_version = approved.destination.metadata
        if previous_edit is not None:
            destination = previous_edit.path
            approvals.update(previous_edit.approvals)
            entry_version = previous_edit.entry_metadata
            referent_version = previous_edit.referent_metadata
        if observed.entry_metadata != entry_version:
            raise ValueError("The inspected SSH entry changed before the edit.")
        if (observed.destination.path != destination
                or observed.requested != approved.requested or not set(observed.approvals) <= approvals
                or observed.entry != approved.entry):
            raise ValueError("The SSH patch path changed after validation.")
        if destination != observed.entry:
            if destination in self.edited:
                referent = self.edited[destination]
                if referent is None:
                    raise ValueError("The SSH patch could not confirm the last edit to the referent.")
                referent_version = referent.entry_metadata
            if observed.destination.metadata != referent_version:
                raise ValueError("The SSH patch referent changed after validation or an edit.")
        return observed

    def _checked_entry(self, path: str) -> SSHWritePath:
        observed = self._observe(path, entry=True)
        self.entries[path] = observed
        return observed

    def patch_path_key(self, path: str, *, entry: bool = False) -> str:
        approved = self.approved.get((path, entry)) or self.approved.get((path, not entry))
        return approved.entry if entry else approved.destination.path

    def patch_visible_text(self, path: str, text: str) -> str:
        return "" if self.hidden_source else ssh_edit_text(text, self.paths)

    def read_file_raw(self, path: str) -> ReadResult:
        self.last_read = None
        try:
            observed = self._observe(path)
            state = observed.destination
            if (path, True) in self.approved:
                self._checked_entry(path)
            if state.kind == "absent":
                self.last_read = path, observed, b"", None
                return ReadResult(error=f"File not found: {state.path}", not_found=True)
            size = state.metadata[2]
            if size > _RAW_READ_MAX_BYTES:
                return ReadResult(file_size=size, _too_large=True, error="The SSH file exceeds the patch read limit.")
            source = read_ssh_write_source(self.ops, state, _RAW_READ_MAX_BYTES)
            if self.ops._is_likely_binary_bytes(source[:1000]):
                return ReadResult(is_binary=True, error="The SSH file is binary.")
            version = (*state.metadata, hashlib.sha256(source).digest())
            self.last_read = path, observed, source, version
            content = _strip_bom(source.decode("utf-8", "surrogateescape"))[0]
            self.hidden_source |= ssh_edit_text(content, self.paths) != content
            return ReadResult(content=content,
                              file_size=size, _content_sha256=version[-1].hex(),
                              _entry_only=(path in self.entries and self.entries[path].entry != state.path))
        except (OSError, ValueError) as exc:
            return ReadResult(error=str(exc))

    def write_file(self, path: str, content: str, pre_content: str | None = None) -> WriteResult:
        if self.last_read is None or self.last_read[0] != path:
            return WriteResult(error="The SSH patch has no verified read for this write.")
        _, observed, source, version = self.last_read
        if error := ssh_content_denial(observed, self.task_id):
            return WriteResult(error=error)
        self.hidden_source |= ssh_edit_text(content, self.paths) != content
        result = _publish_whole_file(self.ops, path, observed, content, version, self.policy, source,
                                     hide_diagnostics=self.hidden_source)
        key = SSHFileKey.for_backend(self.env, observed.destination.path)
        if result._snapshot is not None and (version is None or ssh_write_baseline(self.task_id, key) == version):
            record_ssh_write(self.task_id, key, result._snapshot)
        if result._snapshot is not None:
            edited = observed.destination.path
            self.edited[edited] = AppliedEntry(edited, result._snapshot[:5], None, observed.approvals)
        elif result._entry_landed:
            self.edited[observed.destination.path] = None
        return result

    def delete_file(self, path: str) -> WriteResult:
        expected = self.entries[path]
        result = mutate_ssh_entry(self.ops, path, expected, self.policy)
        if result._entry_landed:
            self.edited[expected.entry] = AppliedEntry(expected.entry, None, None, expected.approvals)
        return result

    def move_file(self, source: str, destination: str) -> WriteResult:
        source_entry = self._checked_entry(source)
        target = self.entries[destination]
        result = mutate_ssh_entry(self.ops, source, source_entry, self.policy, destination=(destination, target))
        if result._entry_landed:
            self.edited[source_entry.entry] = AppliedEntry(source_entry.entry, None, None, source_entry.approvals)
            self.edited[target.entry] = None
            if result._entry_metadata is not None:
                referent = source_entry.destination.path
                if source_entry.entry == referent:
                    referent, referent_version = target.entry, None
                else:
                    referent_version = source_entry.destination.metadata
                self.edited[target.entry] = AppliedEntry(
                    referent, result._entry_metadata, referent_version, source_entry.approvals)
        return result


def _anchor_patch_path(path: str, task_id: str) -> str:
    if has_traversal_component(path):
        raise ValueError(f"SSH patch header contains '..' traversal: {path!r}.")
    if error := get_nt_namespace_error(path, verb="Write"):
        raise ValueError(error)
    return _anchor_ssh_path(_hosted_tilde(path, task_id), task_id)


def _inspect_patch_paths(ops, operations, policy):
    approved = {}
    content_paths = set()
    links = {}
    edited_entries = set()
    for operation in operations:
        path = operation.file_path
        entry = operation.operation in (OperationType.DELETE, OperationType.MOVE)
        if not entry:
            content_paths.add(path)
        if (path, entry) not in approved:
            approved[path, entry] = inspect_ssh_write(ops, path, policy, entry=entry)
        source = approved[path, entry]
        if edited_entries.intersection(source.intermediate_links):
            raise ValueError("The patch changes an intermediate symlink before using it again. Split these edits.")
        if operation.new_path is not None and (operation.new_path, True) not in approved:
            approved[operation.new_path, True] = inspect_ssh_write(ops, operation.new_path, policy, entry=True)
        if operation.operation == OperationType.MOVE and operation.new_path is not None:
            destination = approved[operation.new_path, True]
            original_link = links.get(source.entry, source if source.entry != source.destination.path else None)
            if original_link is not None:
                if edited_entries.intersection(original_link.intermediate_links):
                    raise ValueError("The patch changes an intermediate symlink before using it again. Split these edits.")
                check_ssh_link_move(ops, original_link, destination)
            links[destination.entry] = original_link
            links[source.entry] = None
            edited_entries.update((source.entry, destination.entry))
        elif operation.operation in (OperationType.ADD, OperationType.DELETE):
            links[source.entry] = None
            edited_entries.add(source.entry)
    return approved, content_paths


def _approve_patch_paths(approved, content_paths, task_id):
    instructions = []
    approvals = {}
    for (path, entry), inspected in approved.items():
        candidates = (inspected.requested, inspected.destination.path, inspected.entry)
        for candidate in candidates:
            if error := get_read_block_error(candidate, ssh_path=True):
                return error
        if not entry and path in content_paths and (error := ssh_content_denial(inspected, task_id)):
            return error
        instructions.extend(candidates)
        for key in inspected.approvals:
            approvals.setdefault(key, []).append(inspected.requested)
    return (_check_ssh_instruction_write(list(dict.fromkeys(instructions)), task_id)
            or _request_write_approvals(approvals))


def patch_ssh_v4a(ops, patch: str, task_id: str) -> str:
    operations, error = parse_v4a_patch(patch)
    if error:
        return tool_error(f"Failed to parse patch: {error}")
    operations = [replace(operation, file_path=_anchor_patch_path(operation.file_path, task_id),
                          new_path=_anchor_patch_path(operation.new_path, task_id) if operation.new_path else None)
                  for operation in operations]
    policy = ssh_write_policy()
    approved, content_paths = _inspect_patch_paths(ops, operations, policy)
    if error := _approve_patch_paths(approved, content_paths, task_id):
        return tool_error(error)
    keys = {SSHFileKey.for_backend(ops.env, path)
            for target in approved.values() for path in (target.destination.path, target.entry)}
    with ExitStack() as locks:
        for key in sorted(keys):
            locks.enter_context(file_state.lock_path(repr(key)))
        adapter = SSHV4AFileOperations(ops, task_id, policy, approved)
        response = apply_v4a_operations(operations, adapter, stop_on_apply_error=True).to_dict()
        if adapter.hidden_source and adapter.edited:
            response.pop("diff", None)
            response.pop("lint", None)
            note = "The SSH patch diff and lint diagnostics are hidden because the edit includes redacted text."
            response["note"] = "\n".join(filter(None, (response.get("note"), note)))
        response["files_landed"] = list(adapter.edited)
        return ssh_edit_report(response, adapter.paths)

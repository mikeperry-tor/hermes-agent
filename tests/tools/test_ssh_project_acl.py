"""The executor must preserve effective ACL permissions before replacing a file."""

import ctypes
from types import SimpleNamespace

import pytest

from tools.file_operations_ssh_acl import SSH_PROJECT_ACL


def test_unavailable_owning_group_is_compared_after_the_acl_mask():
    def unavailable(*_args):
        raise PermissionError

    namespace = {
        "ctypes": ctypes,
        "os": SimpleNamespace(fstat=lambda _: SimpleNamespace(st_gid=10000), fchown=unavailable),
        "WriteError": ValueError,
    }
    exec(SSH_PROJECT_ACL, namespace)
    namespace["overflow_id"] = lambda _: 65534
    owner = SimpleNamespace(st_gid=1022)
    rows = {"user:": "rw-", "user:10000": "rw-", "group:": "rw-", "mask:": "r--", "other:": "rw-"}
    with pytest.raises(ValueError, match="cannot preserve the file's group access"):
        namespace["retain_group"](owner, 0, rows)

    rows["other:"] = "r--"
    namespace["retain_group"](owner, 0, rows)
    namespace["os"].fstat = lambda _: SimpleNamespace(st_gid=65534)
    with pytest.raises(ValueError, match="unmapped file group"):
        namespace["retain_group"](SimpleNamespace(st_gid=65534), 0, rows)


def test_unmapped_named_acl_entries_are_refused():
    namespace = {"ctypes": ctypes, "WriteError": ValueError}
    exec(SSH_PROJECT_ACL, namespace)
    raw = ctypes.create_string_buffer(b"user::rwx\nuser:4294967295:r--\ngroup::r-x\nmask::r-x\nother::r-x")
    fake = SimpleNamespace(acl_get_fd=lambda _: 1, acl_to_any_text=lambda *_: ctypes.addressof(raw),
                           acl_free=lambda _: None)
    with pytest.raises(ValueError, match="unmapped identity"):
        namespace["acl_rows"](fake, 0)

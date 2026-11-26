"""Executor-side preservation of access to managed project files."""

SSH_PROJECT_ACL = r"""
class SharedAccessError(WriteError):
    pass

def acl_library():
    try:
        lib = ctypes.CDLL("libacl.so.1", use_errno=True)
    except OSError:
        raise SharedAccessError("The SSH executor has no POSIX ACL library.") from None
    lib.acl_get_fd.argtypes = (ctypes.c_int,)
    lib.acl_get_fd.restype = ctypes.c_void_p
    lib.acl_to_any_text.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char, ctypes.c_int)
    lib.acl_to_any_text.restype = ctypes.c_void_p
    lib.acl_from_text.argtypes = (ctypes.c_char_p,)
    lib.acl_from_text.restype = ctypes.c_void_p
    lib.acl_set_fd.argtypes = (ctypes.c_int, ctypes.c_void_p)
    lib.acl_set_fd.restype = ctypes.c_int
    lib.acl_free.argtypes = (ctypes.c_void_p,)
    return lib


def acl_rows(lib, fd):
    acl = lib.acl_get_fd(fd)
    if not acl:
        raise OSError(ctypes.get_errno(), "read file ACL")
    try:
        numeric_ids = 0x08
        text = lib.acl_to_any_text(acl, None, b"\n", numeric_ids)
        if not text:
            raise OSError(ctypes.get_errno(), "read numeric file ACL")
        try:
            lines = ctypes.string_at(text).decode("ascii").splitlines()
            rows = dict(line.rsplit(":", 1) for line in lines)
            if len(rows) != len(lines):
                raise SharedAccessError("The file ACL contains duplicate identities.")
            for name in rows:
                qualifier = name.split(":", 1)[1]
                if qualifier and (not qualifier.isdecimal() or int(qualifier) >= 2**32 - 1):
                    raise SharedAccessError("The file ACL has an unmapped identity.")
            return rows
        finally:
            lib.acl_free(text)
    finally:
        lib.acl_free(acl)


def set_acl_rows(lib, fd, rows):
    payload = "\n".join(f"{name}:{bits}" for name, bits in rows.items()).encode("ascii")
    acl = lib.acl_from_text(payload)
    if not acl:
        raise OSError(ctypes.get_errno(), "prepare replacement ACL")
    try:
        if lib.acl_set_fd(fd, acl):
            raise OSError(ctypes.get_errno(), "set replacement ACL")
    finally:
        lib.acl_free(acl)


def access_bits(fd):
    path = f"/proc/self/fd/{fd}"
    return "".join(ch if os.access(path, bit, effective_ids=True) else "-"
                   for ch, bit in zip("rwx", (os.R_OK, os.W_OK, os.X_OK)))


def mask_permissions(permissions, mask):
    return "".join(bit if bit == ceiling else "-" for bit, ceiling in zip(permissions, mask))


def overflow_id(kind):
    with open(f"/proc/sys/kernel/overflow{kind}", encoding="ascii") as value:
        return int(value.read())


def retain_group(previous, stage_fd, rows):
    if previous.st_gid == overflow_id("gid"):
        raise SharedAccessError("The SSH user cannot preserve an unmapped file group.")
    if os.fstat(stage_fd).st_gid == previous.st_gid:
        return
    try:
        os.fchown(stage_fd, -1, previous.st_gid)
    except PermissionError:
        group = mask_permissions(rows["group:"], rows.get("mask:", "rwx"))
        if group != rows["other:"] or any(key.startswith("group:") and key != "group:" for key in rows):
            raise SharedAccessError("The SSH user cannot preserve the file's group access.") from None
    else:
        if os.fstat(stage_fd).st_gid != previous.st_gid:
            raise SharedAccessError("The destination filesystem did not retain the file's group.")


def retain_owner(previous_fd, previous, stage_fd, rows):
    if os.fstat(stage_fd).st_uid == previous.st_uid:
        return
    owner = rows["user:"]
    if owner != access_bits(previous_fd):
        raise SharedAccessError("The SSH user's access differs from the file owner's permissions.")
    if "mask:" not in rows and rows["group:"] == rows["other:"] == owner:
        return
    mask = rows.get("mask:", rows["group:"])
    if mask_permissions(owner, mask) != owner:
        raise SharedAccessError("The file ACL mask cannot preserve the owner's permissions.")
    if previous.st_uid == overflow_id("uid"):
        raise SharedAccessError("The SSH user cannot preserve an unmapped file owner.")
    rows[f"user:{previous.st_uid}"] = owner
    rows["mask:"] = mask


def preserve_shared_access(previous_fd, stage_fd):
    previous = os.fstat(previous_fd)
    if previous.st_mode & (stat.S_ISUID | stat.S_ISGID):
        raise SharedAccessError("The SSH editor cannot preserve set-user-ID or set-group-ID access.")
    lib = acl_library()
    rows = acl_rows(lib, previous_fd)
    retain_group(previous, stage_fd, rows)
    retain_owner(previous_fd, previous, stage_fd, rows)
    os.fchmod(stage_fd, stat.S_IMODE(previous.st_mode))
    if acl_rows(lib, stage_fd) != rows:
        set_acl_rows(lib, stage_fd, rows)
    if acl_rows(lib, stage_fd) != rows or stat.S_IMODE(os.fstat(stage_fd).st_mode) != stat.S_IMODE(previous.st_mode):
        raise SharedAccessError("The destination filesystem did not preserve the file's access.")
"""

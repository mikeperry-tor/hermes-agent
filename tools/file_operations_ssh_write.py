"""Executor protocol for guarded SSH writes and directory entry edits."""

import base64
import hashlib
import json
from typing import NamedTuple

from tools.file_operations import SSHPathState
from tools.file_operations_common import WriteResult
from tools.file_operations_ssh_path import SSH_PATH_STATE


class SSHWritePath(NamedTuple):
    requested: str
    destination: SSHPathState
    approvals: tuple[str, ...]
    entry: str
    entry_metadata: tuple | None
    intermediate_links: tuple[str, ...]


SSH_WRITE = r"""
import binascii
import ctypes
import errno
import pwd
import uuid

class WriteError(Exception):
    pass

def under(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)

def write_homes(requested):
    homes = {Path.home(), Path(pwd.getpwuid(os.getuid()).pw_dir)}
    if requested.startswith('~') and not requested.startswith(('~/', '~\\')):
        account = requested.split('/', 1)[0]
        expanded = os.path.expanduser(account)
        if not expanded.startswith('~'):
            homes.add(Path(expanded))
    return homes

def write_policy(requested, supplied, resolved, policy, *, entry=False, check_roots=True):
    read_protected, home_files, home_dirs, shell_rc, protected, sensitive, exact, roots = policy
    targets = {policy_key(Path(supplied)), policy_key(resolved)}
    if roots and check_roots:
        allowed = [os.path.realpath(os.path.expanduser(root)) for root in roots]
        changed_paths = (resolved, supplied) if entry else (resolved,)
        if any(not any(under(path, root) for root in allowed) for path in changed_paths):
            return 'The path is outside the allowed SSH write roots (HERMES_WRITE_SAFE_ROOT).', None
    exact_paths = {policy_key(candidate) for path in exact for candidate in (Path(path), policy_path(Path(path)))}
    dirs = {policy_key(candidate) for path in sensitive for candidate in (Path(path), policy_path(Path(path)))}
    if targets & exact_paths or any(under(path, directory) for path in targets for directory in dirs):
        return 'The path is a protected system file on the SSH executor.', None
    denied, config = read_policy(supplied, resolved, read_protected)
    if denied or config:
        return 'The path is a protected Hermes file on the SSH executor.', None
    approvals = set()
    app_homes = hermes_homes({Path(supplied), Path(resolved)})
    homes = write_homes(requested)
    homes.update(app_home / 'home' for app_home in app_homes if (app_home / 'home').is_dir())
    for home in homes:
        ssh_config = home / '.ssh' / 'config'
        config_entries = {policy_key(ssh_config), policy_key(policy_path(ssh_config.parent) / 'config')}
        configs = config_entries | {policy_key(policy_path(ssh_config))}
        shell_paths = {policy_key(candidate) for name in shell_rc
                       for candidate in (home / name, policy_path(home / name))}
        if targets & configs:
            approvals.add('ssh_config_write')
        if targets & shell_paths:
            approvals.add('shell_rc_write')
        files = {policy_key(candidate) for name in home_files
                 for candidate in (home / name, policy_path(home / name))}
        if targets & files:
            return 'The path is a credential file on the SSH executor.', None
        for name in home_dirs:
            folder = home / name
            paths = targets - config_entries if name == '.ssh' else targets
            if any(under(path, policy_key(root)) for path in paths for root in (folder, policy_path(folder))):
                return 'The path is a credential file on the SSH executor.', None
    app_homes.update(home / '.hermes' for home in write_homes(requested))
    for app_home in app_homes:
        for name in protected:
            sub = app_home / name
            if any(under(path, policy_key(folder)) for path in targets for folder in (sub, policy_path(sub))):
                return 'The path is a protected Hermes file on the SSH executor.', None
    return None, sorted(approvals)

def intermediate_links(entry):
    current = entry
    seen = set()
    while current not in seen:
        try:
            link = os.readlink(current)
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.EINVAL):
                raise
            return sorted(seen - {entry})
        seen.add(current)
        parent, leaf = os.path.split(os.path.join(os.path.dirname(current), link))
        current = os.path.join(resolve(parent)[0], leaf)
    raise WriteError('The SSH file path has a symbolic link loop.')

def inspect_write(requested, policy, entry=False):
    supplied = os.path.expanduser(requested)
    if not os.path.isabs(supplied):
        raise WriteError('The SSH write needs an absolute executor path.')
    resolved, kind, info = observe(supplied)
    denied, approvals = write_policy(requested, supplied, resolved, policy)
    parent, leaf = os.path.split(supplied)
    if not leaf or leaf in ('.', '..'):
        raise WriteError('The SSH path must identify a file entry.')
    location = os.path.join(resolve(parent)[0], leaf)
    entry_metadata = metadata(os.lstat(location)) if kind != 'absent' else None
    if entry or location != supplied:
        entry_denied, entry_approvals = write_policy(
            requested, location, resolved, policy, entry=entry, check_roots=entry)
        denied = denied or entry_denied
        approvals = sorted(set(approvals or ()) | set(entry_approvals or ()))
    return {'requested': supplied, 'path': resolved, 'kind': kind, 'metadata': info,
            'entry': location, 'error': denied, 'approvals': approvals,
            'entry_metadata': entry_metadata,
            'intermediate_links': intermediate_links(location) if location != resolved and not denied else []}

@contextmanager
def opened_parent(path, *, create=False):
    # Walk from the root with directory descriptors so later symlink changes
    # cannot redirect staged writes.
    parent, name = os.path.split(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    try:
        for part in Path(parent).parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield parent, fd, name
    finally:
        os.close(fd)

def receive_bytes(source, dest, length, digest):
    hasher = hashlib.sha256()
    remaining = length
    try:
        for block in iter(lambda: source.read(65536), b''):
            data = base64.b64decode(block, validate=True)
            remaining -= len(data)
            if remaining < 0:
                raise WriteError('The SSH client sent more bytes than expected.')
            hasher.update(data)
            dest.write(data)
    except binascii.Error:
        raise WriteError('The SSH client sent malformed file data.') from None
    if remaining or hasher.hexdigest() != digest:
        raise WriteError('The SSH client did not send the expected file data.')
    dest.flush()

def target_permissions(parent_fd, name, expected, stage_fd):
    previous_fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    with os.fdopen(previous_fd, 'rb') as previous:
        before = os.fstat(previous_fd)
        if not stat.S_ISREG(before.st_mode) or metadata(before) != expected[:5]:
            raise WriteError('The SSH file changed before the write.')
        current_digest = hashlib.sha256()
        for block in iter(lambda: previous.read(65536), b''):
            current_digest.update(block)
        if current_digest.hexdigest() != expected[5] or metadata(os.fstat(previous_fd)) != expected[:5]:
            raise WriteError('The SSH file changed before the write.')
        os.fchmod(stage_fd, stat.S_IMODE(before.st_mode) & 0o777)

def checked_parent(parent, fd):
    current = os.stat(parent, follow_symlinks=False)
    pinned = os.fstat(fd)
    if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
        raise WriteError('The SSH destination directory changed before the write.')

def publish(requested, path, entry, entry_version, expected, source, length, digest, policy, approvals):
    landed = False
    try:
        if not os.path.isabs(path) or path == '/' or os.path.normpath(path) != path:
            raise WriteError('The SSH write needs a verified absolute file path.')
        with opened_parent(path, create=True) as (parent, parent_fd, name):
            scratch = '.hermes-write-' + uuid.uuid4().hex
            os.mkdir(scratch, 0o700, dir_fd=parent_fd)
            try:
                scratch_fd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                try:
                    stage_fd = os.open('file', os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o666, dir_fd=scratch_fd)
                    with os.fdopen(stage_fd, 'w+b') as stage:
                        receive_bytes(source, stage, length, digest)
                        if expected is not None:
                            target_permissions(parent_fd, name, expected, stage_fd)
                        checked_parent(parent, parent_fd)
                        current = inspect_write(requested, policy)
                        if (current['path'] != path or current['entry'] != entry
                                or current['entry_metadata'] != entry_version
                                or current['kind'] != ('absent' if expected is None else 'regular')):
                            raise WriteError('The SSH path changed before the write.')
                        if current['error'] or tuple(current['approvals']) != approvals:
                            raise WriteError(current['error'] or 'The SSH permissions changed before the write.')
                        if expected is None:
                            os.link('file', name, src_dir_fd=scratch_fd, dst_dir_fd=parent_fd)
                            landed = True
                            os.unlink('file', dir_fd=scratch_fd)
                        else:
                            if metadata(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) != expected[:5]:
                                raise WriteError('The SSH file changed before the write.')
                            os.replace('file', name, src_dir_fd=scratch_fd, dst_dir_fd=parent_fd)
                            landed = True
                        return {'landed': True, 'written': length, 'metadata': metadata(os.fstat(stage_fd))}
                finally:
                    try:
                        os.unlink('file', dir_fd=scratch_fd)
                    except FileNotFoundError:
                        pass
                    os.close(scratch_fd)
            finally:
                os.rmdir(scratch, dir_fd=parent_fd)
    except OSError as exc:
        if landed:
            return {'landed': True, 'error': 'The SSH write landed before an I/O error. Inspect the file.'}
        if isinstance(exc, FileExistsError):
            return {'error': 'The SSH destination appeared before the write.'}
        if isinstance(exc, PermissionError):
            return {'error': 'The SSH user cannot write the file or its parent directory.'}
        return {'error': 'The SSH filesystem rejected the write: ' + os.strerror(exc.errno) + '.'}
    except WriteError as exc:
        return {'error': str(exc)}

def verify_entry(requested, expected, policy, fd, name):
    inspected = inspect_write(requested, policy, entry=True)
    if inspected['error']:
        raise WriteError(inspected['error'])
    if not all(inspected[key] == expected[key] for key in ('path', 'entry', 'kind', 'metadata', 'entry_metadata', 'approvals')):
        raise WriteError('The inspected SSH entry changed before the edit.')
    if inspected['kind'] != 'absent':
        observed = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if metadata(observed) != expected['entry_metadata']:
            raise WriteError('The inspected SSH entry changed before the edit.')
        return observed.st_mode
    return None

def rename_without_replacement(src_fd, src, dst_fd, dst):
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'linux' and hasattr(libc, 'renameat2'):
        rename, flag = libc.renameat2, 1
    elif sys.platform == 'darwin' and hasattr(libc, 'renameatx_np'):
        rename, flag = libc.renameatx_np, 4
    else:
        raise WriteError('This SSH executor cannot move a file without replacing its destination.')
    if rename(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), flag) != 0:
        error = ctypes.get_errno()
        if error == errno.EXDEV:
            raise WriteError('The SSH executor cannot atomically move a file across filesystems.')
        raise OSError(error, os.strerror(error))

def keeps_link_target(link, source, destination):
    return os.path.isabs(link) or os.path.dirname(source) == os.path.dirname(destination)

def mutate_entry(requested, expected, policy, destination=None):
    try:
        with opened_parent(expected['entry']) as (parent, fd, name):
            mode = verify_entry(requested, expected, policy, fd, name)
            if mode is None:
                raise WriteError('The SSH source entry does not exist.')
            checked_parent(parent, fd)
            if destination is None:
                os.unlink(name, dir_fd=fd)
                return {'landed': True}
            target, expected_destination = destination
            with opened_parent(expected_destination['entry'], create=True) as (parent_to, fd_to, name_to):
                if verify_entry(target, expected_destination, policy, fd_to, name_to) is not None:
                    raise WriteError('The SSH destination already exists.')
                if stat.S_ISLNK(mode):
                    link = os.readlink(name, dir_fd=fd)
                    if not keeps_link_target(link, expected['entry'], expected_destination['entry']):
                        raise WriteError('The SSH move could change a relative symlink target.')
                checked_parent(parent_to, fd_to)
                checked_parent(parent, fd)
                verify_entry(requested, expected, policy, fd, name)
                rename_without_replacement(fd, name, fd_to, name_to)
                moved = {'landed': True}
                try:
                    current = metadata(os.stat(name_to, dir_fd=fd_to, follow_symlinks=False))
                    if current[:4] == expected['entry_metadata'][:4]:
                        moved['metadata'] = current
                        return moved
                except OSError:
                    pass
                moved['error'] = 'The SSH move landed, then its destination changed. Inspect both paths.'
                return moved
    except FileExistsError:
        return {'error': 'The SSH destination appeared before the move.'}
    except WriteError as exc:
        return {'error': str(exc)}
    except OSError as exc:
        return {'error': 'The SSH filesystem rejected the edit: ' + os.strerror(exc.errno) + '.'}
"""


def inspect_ssh_write(ops, path: str, policy: tuple, *, entry: bool = False) -> SSHWritePath:
    try:
        data = json.loads(ops._framed_ssh_file_output(
            SSH_PATH_STATE + SSH_WRITE
            + f"\nprint(json.dumps(inspect_write({path!r}, {policy!r}, entry={entry!r})))\n"))
        destination = data["path"]
        requested = data["requested"]
        location = data["entry"]
        info, kind = data["metadata"], data["kind"]
        if isinstance(data.get("error"), str):
            raise PermissionError(data["error"])
        if not all(isinstance(name, str) and name.startswith("/") for name in (requested, destination, location)):
            raise ValueError
        if kind == "absent" and info is None:
            state = SSHPathState(destination, kind, None)
        elif (kind == "regular" and isinstance(info, list) and len(info) == 5
              and all(type(value) is int for value in info)):
            state = SSHPathState(destination, kind, tuple(info))
        else:
            raise PermissionError("The SSH write target is not a regular file.")
        approvals = tuple(data["approvals"])
        if any(key not in {"ssh_config_write", "shell_rc_write"} for key in approvals):
            raise ValueError
        links = tuple(data["intermediate_links"])
        if not all(isinstance(link, str) and link.startswith("/") for link in links):
            raise ValueError
        entry_metadata = data["entry_metadata"]
        if entry_metadata is not None and (not isinstance(entry_metadata, list) or len(entry_metadata) != 5
                                           or any(type(value) is not int for value in entry_metadata)):
            raise ValueError
        return SSHWritePath(requested, state, approvals, location,
                            tuple(entry_metadata) if entry_metadata is not None else None, links)
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"Could not inspect the SSH write target {path!r}.") from None

def mutate_ssh_entry(ops, requested: str, inspected: SSHWritePath, policy: tuple,
                     destination: tuple[str, SSHWritePath] | None = None) -> WriteResult:
    def expected(path: SSHWritePath) -> dict:
        state = path.destination
        return {"path": state.path, "entry": path.entry, "kind": state.kind,
                "metadata": state.metadata, "entry_metadata": path.entry_metadata, "approvals": list(path.approvals)}

    target = None if destination is None else (destination[0], expected(destination[1]))
    args = f"{requested!r}, {expected(inspected)!r}, {policy!r}, {target!r}"
    try:
        data = json.loads(ops._framed_ssh_file_output(
            SSH_PATH_STATE + SSH_WRITE + f"\nprint(json.dumps(mutate_entry({args})))\n"))
        if data.get("landed") is True:
            info = data.get("metadata")
            version = tuple(info) if isinstance(info, list) and len(info) == 5 else None
            if destination is not None and version is None and not isinstance(data.get("error"), str):
                raise ValueError
            return WriteResult(error=data.get("error"), _entry_landed=True, _entry_metadata=version)
        if isinstance(data.get("error"), str):
            return WriteResult(error=data["error"])
    except (AttributeError, TypeError, ValueError):
        pass
    return WriteResult(error="Could not verify the SSH entry edit. Inspect the path before retrying.")


def check_ssh_link_move(ops, source: SSHWritePath, destination: SSHWritePath) -> None:
    if source.entry == source.destination.path:
        return
    args = f"os.readlink({source.entry!r}), {source.entry!r}, {destination.entry!r}"
    response = ops._framed_ssh_file_output(
        SSH_PATH_STATE + SSH_WRITE + f"\nprint(json.dumps(keeps_link_target({args})))\n")
    if json.loads(response) is not True:
        raise ValueError("The SSH move could change a relative symlink target.")

def read_ssh_write_source(ops, state: SSHPathState, maximum: int) -> bytes:
    if state.kind == "absent":
        return b""
    size = min(state.metadata[2], maximum)
    try:
        encoded = ops._ssh_file_value(f"read_bytes({state.path!r}, {state.metadata!r}, {size - 1})")
        source = base64.b64decode(encoded, validate=True)
        if len(source) != size:
            raise ValueError
        return source
    except (TypeError, ValueError):
        raise ValueError("Could not read the inspected SSH file before writing.") from None

def publish_ssh_file(ops, input_path: str, inspected: SSHWritePath, expected: tuple | None,
                     content: bytes, policy: tuple) -> WriteResult:
    """Publish verified bytes against the previously inspected SSH file."""
    state = inspected.destination
    if state.kind == "absent" and expected is None:
        remote_version = None
    elif state.kind == "regular" and expected is not None and expected[:5] == state.metadata:
        remote_version = (*expected[:5], expected[5].hex())
    else:
        return WriteResult(error="The SSH file changed before the write. Read it again.")
    digest = hashlib.sha256(content).digest()
    args = (f"{input_path!r}, {state.path!r}, {inspected.entry!r}, {inspected.entry_metadata!r}, "
            f"{remote_version!r}, sys.stdin.buffer, {len(content)}, {digest.hex()!r}, "
            f"{policy!r}, {inspected.approvals!r}")
    try:
        response = json.loads(ops._framed_ssh_file_output(
            SSH_PATH_STATE + SSH_WRITE + f"\nprint(json.dumps(publish({args})))\n",
            stdin_data=base64.b64encode(content).decode("ascii")))
        if isinstance(response.get("error"), str):
            return WriteResult(error=response["error"], _entry_landed=response.get("landed") is True)
        info = response["metadata"]
        if (response["written"] != len(content) or len(info) != 5
                or any(type(value) is not int for value in info) or info[2] != len(content)):
            raise ValueError
        return WriteResult(bytes_written=len(content), dirs_created=True, verified=True,
                           _content_sha256=digest.hex(), _snapshot=(*info, digest), _entry_landed=True)
    except (KeyError, TypeError, ValueError):
        return WriteResult(error="Could not verify the SSH write. Inspect the file before retrying.")

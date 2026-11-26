"""File path and descriptor checks run on the SSH executor."""

SSH_PATH_STATE = """\
import base64, hashlib, json, os, stat, sys
from contextlib import contextmanager
from pathlib import Path

def resolve(path):
    missing = []
    while True:
        try:
            resolved = str(Path(path).resolve(strict=True))
        except FileNotFoundError:
            # Refuse a dangling link before classifying a path as missing.
            try:
                os.lstat(path)
            except FileNotFoundError:
                parent, name = os.path.split(path)
                if name in ('', '.', '..') or parent == path:
                    raise
                missing.append(name)
                path = parent
                continue
            raise
        if missing and not stat.S_ISDIR(os.stat(resolved).st_mode):
            raise NotADirectoryError(resolved)
        return os.path.join(resolved, *reversed(missing)), bool(missing)

def metadata(st):
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns

@contextmanager
def open_regular(path, expected):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or metadata(opened) != expected:
            raise OSError('file changed before opening')
        yield stream
        if metadata(os.fstat(stream.fileno())) != expected or metadata(os.lstat(path)) != expected:
            raise OSError('file changed while open')

def observe(path):
    try:
        original = os.stat(path)
    except FileNotFoundError:
        original = None
    resolved, missing = resolve(path)
    if missing:
        if original is not None:
            raise OSError('path disappeared during resolution')
        return resolved, 'absent', None
    st = os.lstat(resolved)
    info = metadata(st)
    if original is None or metadata(original) != info:
        raise OSError('path changed during resolution')
    if stat.S_ISDIR(st.st_mode):
        return resolved, 'directory', info
    if stat.S_ISLNK(st.st_mode):
        raise OSError('path changed during resolution')
    if not stat.S_ISREG(st.st_mode):
        return resolved, 'special', info
    return resolved, 'regular', info

def read_page(path, expected, offset, end, clamp):
    hasher = hashlib.sha256()
    sample = b''
    pending = b''
    page = []
    line = 1
    last_byte = b''
    size = 0
    with open_regular(path, expected) as stream:
        for chunk in iter(lambda: stream.read(1048576), b''):
            if not sample:
                sample = chunk[:1000]
            hasher.update(chunk)
            size += len(chunk)
            last_byte = chunk[-1:]
            if line > end:
                line += chunk.count(b'\\n')
                continue
            pieces = chunk.split(b'\\n')
            for head in pieces[:-1]:
                if offset <= line <= end:
                    page.append((pending + head)[:clamp] + b'\\n')
                pending = b''
                line += 1
            if offset <= line <= end:
                pending += pieces[-1][:clamp - len(pending)]
        if last_byte not in (b'', b'\\n') and offset <= line <= end:
            page.append(pending + b'\\n')
    return (base64.b64encode(b''.join(page)).decode('ascii'),
            base64.b64encode(sample).decode('ascii'), line - 1,
            last_byte == b'\\n', size, hasher.hexdigest())

def read_bytes(path, expected, maximum):
    with open_regular(path, expected) as stream:
        return base64.b64encode(stream.read(maximum + 1)).decode('ascii')

def read_reference_window(path, expected, start, end, maximum):
    with open_regular(path, expected) as stream:
        sample = stream.read(4096)
        stream.seek(0)
        if start is None:
            data = stream.read(maximum + 1)
        else:
            line = 1
            while line < start:
                chunk = stream.readline(65536)
                if not chunk:
                    break
                if chunk.endswith(b'\\n'):
                    line += 1
            data = bytearray()
            while start <= line <= end and len(data) <= maximum:
                chunk = stream.readline(maximum + 1 - len(data))
                if not chunk:
                    break
                data.extend(chunk)
                line += 1
    return [base64.b64encode(part).decode('ascii') for part in (sample, data)]

def rg_reference_rows(limit):
    import subprocess
    try:
        result = subprocess.run(['rg', '--files', '-0', '.'], capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    rows = {}
    for raw in result.stdout.split(b'\\x00'):
        if not raw:
            continue
        path = os.fsdecode(raw)
        if os.path.isabs(path) or '..' in path.split('/'):
            continue
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            continue
        parent = os.path.dirname(path)
        while parent not in ('', '.') and len(rows) < limit:
            rows.setdefault(os.path.relpath(parent), None)
            parent = os.path.dirname(parent)
        if len(rows) >= limit:
            break
        rows[os.path.relpath(path)] = info.st_size
    return sorted(rows.items())

def recursive_reference_rows(limit):
    if (rows := rg_reference_rows(limit)) is not None:
        return rows
    rows = []
    for root, directories, files in os.walk('.'):
        directories[:] = sorted(name for name in directories
                                if not name.startswith('.') and name != '__pycache__'
                                and not os.path.islink(os.path.join(root, name)))
        for name in (*directories, *sorted(files)):
            if name.startswith('.'):
                continue
            path = os.path.join(root, name)
            info = os.lstat(path)
            if stat.S_ISDIR(info.st_mode):
                rows.append((os.path.relpath(path), None))
            elif stat.S_ISREG(info.st_mode):
                rows.append((os.path.relpath(path), info.st_size))
            if len(rows) >= limit:
                return rows
    return rows

def reference_directory(path, expected, limit, recursive=False, prefix='', include_hidden=False):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if metadata(os.fstat(fd)) != expected:
            raise OSError('directory changed before opening')
        if recursive:
            os.fchdir(fd)
            rows = recursive_reference_rows(limit)
        else:
            rows = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    if ((not include_hidden and entry.name.startswith('.')) or entry.is_symlink()
                            or not entry.name.lower().startswith(prefix.lower())):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        rows.append((entry.name, None))
                    elif entry.is_file(follow_symlinks=False):
                        rows.append((entry.name, entry.stat(follow_symlinks=False).st_size))
                    if len(rows) >= limit:
                        break
        if metadata(os.fstat(fd)) != expected or metadata(os.lstat(path)) != expected:
            raise OSError('directory changed while open')
        return sorted(rows)
    finally:
        os.close(fd)

def directory_names(path, limit):
    try:
        return sorted(name for name in os.listdir(path) if not name.startswith('.'))[:limit]
    except (FileNotFoundError, NotADirectoryError):
        return []

def policy_path(path):
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path

def policy_key(path):
    text = str(path)
    return text.casefold() if sys.platform in ('darwin', 'win32') else text

def hermes_homes(targets):
    user_home = Path.home()
    suffix = os.environ.get('HERMES_DATA_DIR_SUFFIX', '')
    homes = {user_home / '.hermes', user_home / ('.hermes' + suffix)}
    configured = os.environ.get('HERMES_HOME', '').strip()
    if configured:
        home = Path(os.path.expandvars(configured)).expanduser().absolute()
        homes.add(home)
        if home.parent.name == 'profiles':
            homes.add(home.parent.parent)
    for base in tuple(homes):
        profiles = base / 'profiles'
        try:
            homes.update(entry for entry in profiles.iterdir() if entry.is_dir())
        except OSError:
            pass
        # An execute-only profiles directory can contain a readable named profile.
        for parent in {profiles, policy_path(profiles)}:
            prefix = policy_key(parent).rstrip(os.sep) + os.sep
            for target in targets:
                if policy_key(target).startswith(prefix):
                    homes.add(Path(*target.parts[:len(parent.parts) + 1]))
    return homes

def read_policy(input_path, resolved, protected):
    if not protected:
        return False, False
    files, directories = protected
    targets = {Path(input_path), Path(resolved)}
    target_keys = {policy_key(target) for target in targets}
    secret = False
    for home in hermes_homes(targets):
        if not home.is_dir():
            continue
        dirs = {home, policy_path(home)}
        for target in targets:
            name = target.name.lower()
            is_config = name == 'config.yaml' or name.startswith(('config.yaml.good.', 'config.yaml.corrupt.'))
            parents = {policy_key(parent) for parent in target.parents}
            if is_config and any(policy_key(folder) in parents for folder in dirs):
                secret = True
        for name in files:
            entry = home / name
            if policy_key(entry) in target_keys or policy_key(policy_path(entry)) in target_keys:
                return True, secret
        for name in directories:
            entry = home / name
            blocked = {policy_key(entry), policy_key(policy_path(entry))}
            if any(target == folder or target.startswith(folder.rstrip(os.sep) + os.sep)
                   for folder in blocked for target in target_keys):
                return True, secret
    return False, secret

def inspect_path(path, protected):
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        raise ValueError('SSH path does not resolve to an absolute path')
    state = observe(path)
    return (*state, *read_policy(path, state[0], protected))

"""

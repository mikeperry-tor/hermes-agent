"""Executor-side search over guarded file descriptors."""

SSH_SEARCH = r"""
import fnmatch
import heapq
from contextlib import closing
from itertools import chain, islice
import re
import shutil
import subprocess
import tempfile

class SearchError(Exception):
    pass

def blocked_path(path, rules):
    env_names, devices, proc_suffixes = rules
    normalized = os.path.normpath(path)
    return (Path(path).name.casefold() in env_names or normalized in devices
            or (normalized.startswith('/proc/') and normalized.endswith(proc_suffixes)))

def search_entry(path, protected, rules):
    if blocked_path(path, rules):
        return None
    resolved, kind, info, denied, secret = inspect_path(path, protected)
    if denied or blocked_path(resolved, rules) or kind == 'special':
        return None
    return resolved, kind, info, secret

def search_candidates(path, root, protected, rules, skipped, hidden_files=False):
    if root[1] == 'regular':
        yield path, root
        return
    try:
        with os.scandir(root[0]) as entries:
            for item in entries:
                if item.is_symlink():
                    continue
                if item.name.startswith('.') and (not hidden_files or not item.is_file(follow_symlinks=False)):
                    continue
                name = os.path.join(path, item.name)
                try:
                    entry = search_entry(name, protected, rules)
                    if entry is None or entry[1] == 'absent':
                        continue
                    yield name, entry
                    if entry[1] == 'directory':
                        yield from search_candidates(name, entry, protected, rules, skipped, hidden_files)
                except (OSError, RuntimeError):
                    skipped[0] = True
    except OSError:
        skipped[0] = True

def discovery_names(command, absolute, skipped, success_codes, path_error_status):
    with tempfile.TemporaryFile() as diagnostics, subprocess.Popen(
            command, cwd=absolute, stdout=subprocess.PIPE, stderr=diagnostics,
            env={**os.environ, 'LC_ALL': 'C'}) as process:
        try:
            pending = b''
            while chunk := process.stdout.read1(65536):
                *names, pending = (pending + chunk).split(b'\0')
                for relative in names:
                    yield os.fsdecode(relative).removeprefix('./')
            if pending:
                raise SearchError('The SSH file discovery command returned an incomplete path.')
            status = process.wait()
            if status not in success_codes:
                diagnostics.seek(0)
                reason = diagnostics.readline(4096)
                if reason.startswith((b'rg: error parsing glob', b'error parsing glob')):
                    raise SearchError('Invalid file glob on the SSH executor.')
                path_failure = rb': (?:Permission denied|Operation not permitted|No such file or directory|' \
                               rb'Not a directory|Input/output error)(?: \(os error \d+\))?$'
                if status != path_error_status or not reason:
                    raise SearchError('Could not discover paths on the SSH executor.')
                while reason:
                    if not reason.endswith(b'\n') or not re.search(path_failure, reason.rstrip(b'\r\n')):
                        raise SearchError('Could not discover paths on the SSH executor.')
                    reason = diagnostics.readline(4096)
                skipped[0] = True
        finally:
            if process.poll() is None:
                process.kill()

def discovered_candidates(command, path, absolute, kind, protected, rules, skipped):
    success_codes, path_error_status = ((0,), 1) if kind == 'directory' else ((0, 1), 2)
    with closing(discovery_names(command, absolute, skipped, success_codes, path_error_status)) as names:
        for suffix in names:
            name = os.path.join(path, suffix)
            try:
                entry = search_entry(name, protected, rules)
                if entry is not None and entry[1] == kind:
                    yield name, entry
            except (OSError, RuntimeError):
                skipped[0] = True

def rg_candidates(rg, path, absolute, glob, protected, rules, skipped):
    command = [rg, '--no-config', '--files', '-0', '--threads=1']
    if glob:
        command += ['--glob', glob]
    yield from discovered_candidates(command + ['--', '.'], path, absolute, 'regular',
                                     protected, rules, skipped)

def rg_directories(path, root, pattern, protected, rules, skipped):
    absolute = root[0]
    find = shutil.which('find')
    if find:
        command = [find, '.', '-mindepth', '1', '-type', 'd', '-name', '.*', '-prune',
                   '-o', '-type', 'd', '-name', pattern, '-print0']
        entries = discovered_candidates(command, path, absolute, 'directory', protected, rules, skipped)
    else:
        entries = ((name, entry) for name, entry in search_candidates(path, root, protected, rules, skipped)
                   if entry[1] == 'directory' and fnmatch.fnmatchcase(os.path.basename(name), pattern))
    git = shutil.which('git')
    if not git:
        yield from entries
        return
    git_command = [git, '-c', 'core.fsmonitor=false', '-C', absolute]
    probe = subprocess.run(git_command + ['rev-parse', '--is-inside-work-tree'],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if probe.stdout.strip() != b'true':
        yield from entries
        return
    while batch := list(islice(entries, 256)):
        paths = [os.fsencode(entry[0]) for _, entry in batch]
        ignored = subprocess.run(git_command + ['check-ignore', '-z', '--stdin'],
                                 input=b'\0'.join(paths) + b'\0',
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if ignored.returncode not in (0, 1):
            skipped[0] = True
            continue
        excluded = set(ignored.stdout.split(b'\0'))
        for row, encoded in zip(batch, paths):
            if encoded not in excluded:
                yield row

def modified_order(row):
    name, entry = row
    return -entry[2][3], name

def filename_candidates(roots, rg, glob, protected, rules, skipped):
    for path, root in roots:
        if not rg:
            yield from ((name, entry) for name, entry in search_candidates(path, root, protected, rules, skipped)
                        if not os.path.basename(name).startswith('.')
                        and fnmatch.fnmatchcase(os.path.basename(name), glob))
        elif root[1] == 'regular':
            yield path, root
        else:
            yield from rg_candidates(rg, path, root[0], None if glob == '*' else glob,
                                     protected, rules, skipped)

def filename_directories(roots, glob, protected, rules, skipped):
    for path, root in roots:
        if root[1] == 'directory':
            yield from rg_directories(path, root, glob, protected, rules, skipped)

def merge_discovery(files, directories):
    file = next(files, None)
    for directory in directories:
        while file is not None and modified_order(file) <= modified_order(directory):
            yield file
            file = next(files, None)
        yield directory
    if file is not None:
        yield file
    yield from files

def file_names(roots, rg, pattern, order, offset, limit, protected, rules, skipped):
    glob = pattern.rsplit('/', 1)[-1] or '*'
    upper = offset + limit
    fetch = upper + 1
    if rg:
        glob = glob if glob.startswith('*') else '*' + glob
    if rg and glob != '*' and subprocess.run(
            [rg, '--no-config', '--files', '--glob', glob, '--', os.devnull],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode not in (0, 1):
        raise SearchError('Invalid file glob on the SSH executor.')
    files = filename_candidates(roots, rg, glob, protected, rules, skipped)
    directories = filename_directories(roots, glob, protected, rules, skipped) if rg else ()
    if order == 'modified':
        selected = heapq.nsmallest(fetch, chain(files, directories), key=modified_order)
    elif rg:
        matching_dirs = heapq.nsmallest(fetch, directories, key=modified_order)
        with closing(files):
            selected = list(islice(merge_discovery(files, matching_dirs), fetch))
    else:
        with closing(files):
            selected = list(islice(files, fetch))
    return {'rows': [name for name, _ in selected[offset:upper]],
            'total': len(selected), 'truncated': len(selected) > upper, 'skipped': skipped[0]}

def search_command(rg, pattern, mode, context, multiline, explicit_glob):
    if rg:
        args = [rg, '--no-config', '--color=never', '--no-filename', '-n']
        if multiline:
            args.append('--multiline')
        if explicit_glob:
            args += ['--glob', explicit_glob]
        if mode == 'content':
            args += ['--max-columns', '2000', '--max-columns-preview']
    else:
        grep = shutil.which('grep')
        if not grep:
            return None
        args = [grep, '-nEI']
        if pattern.startswith('(?i)'):
            args.append('-i')
            pattern = pattern[4:]
    if mode == 'count':
        args.append('-c')
    elif mode == 'files_only':
        args.append('-q')
    elif mode == 'content' and context:
        args += ['-C', str(context)]
    return args + ['-e', pattern, '--']

def binary_stream(stream):
    first = stream.read(65536)
    if first.startswith((b'\xff\xfe', b'\xfe\xff')):
        stream.seek(0)
        return False
    for chunk in chain((first,), iter(lambda: stream.read(65536), b'')):
        if b'\0' in chunk:
            return True
    stream.seek(0)
    return False

def search_stream(command, stream, mode, remaining, source='-'):
    try:
        process = subprocess.Popen(command + [source], stdin=stream, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
    except OSError:
        raise SearchError('Could not start the search command on the SSH executor.') from None
    with process:
        rows = []
        count = 0
        while row := process.stdout.readline(4096):
            prefix = row
            while not row.endswith(b'\n'):
                row = process.stdout.readline(4096)
                if not row:
                    break
            if mode == 'count':
                count = int(prefix)
                continue
            matched = re.match(rb'^(\d+)[:-](.*)', prefix)
            if matched:
                rows.append([int(matched[1]), matched[2].rstrip(b'\r').decode('utf-8', 'replace')[:500]])
                if len(rows) >= remaining:
                    process.kill()
                    process.wait()
                    return rows
        status = process.wait()
        if status not in (0, 1):
            raise SearchError('File search failed. Check the pattern, file glob, and executor search command.')
        if mode == 'count':
            return [count] if count else []
        return [True] if mode == 'files_only' and status == 0 else rows

def search_roots(path, alternatives, protected, rules, skipped):
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        raise SearchError('Cannot determine an absolute search path on the SSH executor.')
    root = search_entry(path, protected, rules)
    if root is None:
        raise SearchError('Cannot search this protected or special path on the SSH executor.')
    if root[1] != 'absent':
        return [(path, root)], False
    roots = []
    for alternative in dict.fromkeys(alternatives if len(alternatives) > 1 else ()):
        candidate = os.path.expanduser(alternative)
        if not os.path.isabs(candidate):
            continue
        try:
            entry = search_entry(candidate, protected, rules)
        except (OSError, RuntimeError):
            skipped[0] = True
            continue
        if entry is not None and entry[1] != 'absent':
            roots.append((candidate, entry))
    if not roots:
        if skipped[0]:
            raise SearchError('The SSH executor could not inspect the requested search paths.')
        raise SearchError('Path not found: ' + path)
    return roots, True

def content_candidates(roots, rg, glob, protected, rules, skipped):
    for path, root in roots:
        if rg and root[1] == 'directory':
            with closing(rg_candidates(rg, path, root[0], glob, protected, rules, skipped)) as entries:
                for name, entry in entries:
                    yield name, entry, True
        else:
            for name, entry in search_candidates(path, root, protected, rules, skipped, hidden_files=not rg):
                if entry[1] == 'regular' and (rg or not glob or fnmatch.fnmatchcase(os.path.basename(name), glob)):
                    yield name, entry, False

def content_rows(roots, rg, pattern, glob, mode, context, offset, limit, multiline, protected, rules, skipped):
    if multiline and not rg:
        return {'error': 'Multiline content search requires ripgrep on the SSH executor.'}
    command = search_command(rg, pattern, mode, context, multiline, glob)
    if command is None:
        return {'error': 'Content search requires ripgrep or grep on the SSH executor.'}
    upper = offset + limit
    total = 0
    rows = []
    seen = 0
    with open(os.devnull, 'rb') as empty:
        search_stream(command, empty, mode, 1)
    source = '/proc/self/fd/0' if rg and multiline and os.path.isdir('/proc/self/fd') else '-'
    entries = content_candidates(roots, rg, glob, protected, rules, skipped)
    for name, (resolved, _kind, info, secret), recursive_rg in entries:
        try:
            with open_regular(resolved, info) as stream:
                if recursive_rg and binary_stream(stream):
                    continue
                hits = search_stream(command, stream, mode, upper + 1 - seen, source)
        except OSError:
            skipped[0] = True
            continue
        for hit in hits:
            total += hit if mode == 'count' else 1
            if offset <= seen < upper:
                rows.append([name, resolved, secret, hit])
            seen += 1
        if seen > upper:
            break
    return {'rows': rows, 'total': total, 'truncated': seen > upper, 'skipped': skipped[0]}

def search(path, protected, rules, pattern, target, glob, mode, context,
           order, offset, limit, multiline, alternatives=()):
    try:
        skipped = [False]
        roots, multiple_roots = search_roots(path, alternatives, protected, rules, skipped)
        rg = shutil.which('rg')
        if target == 'files':
            result = file_names(roots, rg, pattern, order, offset, limit, protected, rules, skipped)
        else:
            result = content_rows(roots, rg, pattern, glob, mode, context, offset, limit,
                                  multiline, protected, rules, skipped)
        result['multiple_roots'] = multiple_roots
        return result
    except SearchError as exc:
        return {'error': str(exc)}
"""

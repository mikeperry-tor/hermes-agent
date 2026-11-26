"""The terminal-cwd save checks the conversation in the same step as its write (#41128): a ``/new``
that lands between the turn's ownership check and the write leaves the fresh conversation alone."""
from datetime import datetime
from types import SimpleNamespace

from gateway.session import SessionEntry
from gateway.session_terminal_cwd import operator_ssh_cwd, persist_turn_cwd, restore_turn_cwd

KEY = 'agent:main:telegram:dm:202'


class _Store:
    """SessionStore's two write paths: ``set_session_metadata`` writes whichever entry is current,
    ``_update_entry`` applies *mutate* to the current entry under the store lock (False: no save)."""

    def __init__(self):
        self._entries = {}

    def set_session_metadata(self, key, field, value):
        self._entries[key].metadata[field] = value

    def _update_entry(self, key, mutate):
        entry = self._entries.get(key)
        return entry is not None and mutate(entry) is not False


def _entry(session_id, created_at):
    return SessionEntry(session_key=KEY, session_id=session_id, created_at=created_at, updated_at=created_at)


def test_a_new_between_the_check_and_the_write_leaves_the_fresh_conversation_alone(tmp_path, monkeypatch):
    from tools import terminal_tool
    monkeypatch.setattr(terminal_tool, '_session_cwd', {})
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    store = _Store()
    runner = SimpleNamespace(session_store=store)
    store._entries[KEY] = _entry('s1', datetime(2026, 11, 1))
    token = restore_turn_cwd(runner, KEY, None)
    fresh = _entry('s2', datetime(2026, 11, 2))

    def new_lands_after_the_check(key):  # /new publishes the fresh entry right after the turn's check
        store._entries[KEY] = fresh
        return str(tmp_path)
    monkeypatch.setattr(terminal_tool, 'get_session_cwd', new_lands_after_the_check)
    persist_turn_cwd(runner, KEY, token)
    assert 'terminal_cwd' not in fresh.metadata


def test_operator_ssh_cwd_keeps_a_record_only_for_the_same_project_binding(monkeypatch):
    from gateway import session_terminal_cwd
    from tools import terminal_tool

    monkeypatch.setattr(terminal_tool, '_session_cwd', {})
    monkeypatch.setattr(session_terminal_cwd, '_record_owner', {})
    monkeypatch.setattr(session_terminal_cwd, '_managed_ssh', lambda policy: True)
    store = _Store()
    store._entries[KEY] = _entry('session', datetime(2026, 11, 1))
    runner = SimpleNamespace(session_store=store)
    policy = SimpleNamespace(terminal_json='{"TERMINAL_ENV":"ssh","TERMINAL_CWD":"/executor project"}')
    assert operator_ssh_cwd(runner, KEY, policy) == '/executor project'

    token = restore_turn_cwd(runner, KEY, policy)
    terminal_tool.record_session_cwd(KEY, '/executor project/subdir')
    assert operator_ssh_cwd(runner, KEY, policy) == '/executor project/subdir'
    persist_turn_cwd(runner, KEY, token)
    terminal_tool.clear_session_cwd(KEY)
    session_terminal_cwd._record_owner.clear()
    assert operator_ssh_cwd(runner, KEY, policy) == '/executor project/subdir'
    assert restore_turn_cwd(runner, KEY, policy) is not None
    assert terminal_tool.get_session_cwd(KEY) == '/executor project/subdir'

    moved = SimpleNamespace(terminal_json='{"TERMINAL_ENV":"ssh","TERMINAL_CWD":"/different project"}')
    assert operator_ssh_cwd(runner, KEY, moved) == '/different project'
    assert restore_turn_cwd(runner, KEY, moved) is not None
    assert terminal_tool.get_session_cwd(KEY) is None

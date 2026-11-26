"""Tests for Ctrl+G external-editor submit in the classic CLI.

Ctrl+G opens the current draft in ``$EDITOR``; on a clean save the draft is
submitted (TUI parity) rather than left in the input area. Submission in the
CLI is driven by the custom Enter keybinding, not the buffer accept_handler,
so ``_open_external_editor`` chains a done-callback that calls
``_submit_editor_buffer``. These exercise that submit helper directly.
"""

import queue
from unittest.mock import Mock

import pytest

from cli import HermesCLI


class _FakeBuf:
    def __init__(self, text: str):
        self.text = text
        self.reset_called = False

    def reset(self, append_to_history: bool = False):
        self.reset_called = True
        self.text = ""


def _make(agent_running: bool = False, busy: str = "queue") -> HermesCLI:
    c = HermesCLI.__new__(HermesCLI)
    c._pending_input = queue.Queue()
    c._interrupt_queue = queue.Queue()
    c._agent_running = agent_running
    c.busy_input_mode = busy
    c._app = None
    c._should_exit = False
    return c


def test_idle_prompt_routed_to_pending_input():
    c = _make()
    buf = _FakeBuf("Explain vector databases.\nKeep it short.")

    c._submit_editor_buffer(buf)

    assert c._pending_input.get_nowait() == "Explain vector databases.\nKeep it short."
    assert buf.reset_called


def test_running_interrupt_mode_uses_interrupt_queue():
    c = _make(agent_running=True, busy="interrupt")
    buf = _FakeBuf("interrupt this")

    c._submit_editor_buffer(buf)

    assert c._interrupt_queue.get_nowait() == "interrupt this"
    assert c._pending_input.empty()


@pytest.mark.parametrize("command", ["!pwd", "/status", "/executor-pwd"])
def test_editor_command_goes_to_repl_worker(command):
    c = _make(agent_running=True, busy="interrupt")
    c._app = Mock()
    c.handle_bang_shell = Mock()
    c.process_command = Mock()
    buf = _FakeBuf(command)

    c._submit_editor_buffer(buf)

    assert c._pending_input.get_nowait() == command
    assert c._interrupt_queue.empty()
    assert buf.reset_called
    c.handle_bang_shell.assert_not_called()
    c.process_command.assert_not_called()

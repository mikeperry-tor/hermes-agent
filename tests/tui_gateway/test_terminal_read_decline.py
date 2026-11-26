"""Regression tests for terminal.read decline settlement in tui_gateway."""
import threading

from tests.tui_gateway import test_protocol as _proto

tui_server = _proto.server


def test_terminal_read_decline_does_not_beat_the_owner_window(tui_server):
    """A bystander declining terminal.read with -32601 leaves the request open for the owner window."""
    from tui_gateway import server_requests

    bystander, owner = _proto._two_window_session(tui_server, "s-own")
    box = {}
    thread = threading.Thread(
        target=lambda: box.setdefault("r", tui_server._ask("terminal.read", "s-own", {}, timeout=5)),
        daemon=True,
    )
    thread.start()
    req = _proto._wait_open(server_requests, windows=(bystander, owner))
    frame = _proto._not_shown(req)
    frame["error"]["code"] = -32601
    tui_server.dispatch(frame, bystander)
    assert server_requests.resolve_response(_proto._not_shown(req)) is True
    assert thread.is_alive() and [r["id"] for r in server_requests.open_requests("s-own")] == [req.id]
    tui_server.dispatch({"jsonrpc": "2.0", "id": req.id, "result": {"value": '{"text": "term"}'}}, owner)
    thread.join(timeout=2)
    assert box["r"] == '{"text": "term"}'

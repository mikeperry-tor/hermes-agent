"""A viewer answers requests for a sibling profile over a shared socket."""
import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.gateway.test_session_authorities_multiplex import _reserve_homes, _runner
from tests.waiting import async_wait_until
from persistence_assembly import close_stores_under


class _Transport:
    def __init__(self):
        self.frames = []

    def write(self, frame):
        self.frames.append(frame)
        return True

    def close(self):
        pass


@pytest.mark.asyncio
async def test_host_socket_routes_sibling_sudo_and_terminal_responses(tmp_path, monkeypatch):
    """The Desktop's shared-primary socket chats on profile beta and answers beta's ``sudo`` request with
    a response frame (which carries no profile): the waiting tool gets the password, not a timeout. A
    second socket's beta session stays its own: the first socket's beta sibling is not attached there,
    so its answer to that request id is refused and the owning socket's answer settles it.
    When both sockets share a chat, a terminal decline leaves the other viewer free to answer."""
    from gateway.run_runtime import initialize_gateway_runtime
    from gateway.runtime_ownership import process_ownership
    from gateway.session_authorities import authority_for_profile_id, owner_scope
    from gateway.session_controls import AuthorityConnection
    from gateway.session_server_requests import ask_viewer, waiting
    from tui_gateway import server_requests
    root, homes = _reserve_homes(tmp_path, monkeypatch)
    process_ownership.reserve([home for _, home in homes])
    connections, runner = [], None
    try:
        runner = _runner(root, homes)
        await initialize_gateway_runtime(runner)
        store = runner.session_ticket_store
        beta = runner.session_authorities.for_home(homes[2][1])

        async def open_beta_chat(request_id):
            ticket = store.mint(profile_id=str(root.resolve()), subject='uid:desk', purpose='interactive',
                                scope='host')
            grant = store.redeem(ticket, profile_id=None, purpose='interactive')
            launch = authority_for_profile_id(runner, grant['profile_id'])
            identity = {'user_id': grant['subject'], 'provider': 'local', 'profile_id': grant['profile_id'],
                        'instance_id': grant['instance_id'], 'capabilities': grant['capabilities'],
                        'native_bootstrap': True, 'profile_scope': 'host'}
            transport = _Transport()
            with owner_scope(launch):
                connection = AuthorityConnection(launch, transport, identity, operator=True)
                connections.append(connection)
                created = await connection.dispatch({'id': 1, 'method': 'session.create', 'params': {
                    'profile': 'beta', 'request_id': request_id, 'source': 'cli', 'cwd': str(tmp_path),
                    'model': 'm', 'toolsets': []}})
            session_id = created['result']['session_id']
            assert beta is not launch and session_id in beta.sessions
            return connection, transport, session_id

        async def ask_sudo(transport, session_id):
            events = beta.sessions[session_id].event_stream
            out = {}
            worker = threading.Thread(target=lambda: out.update(value=ask_viewer(
                events, session_id, 'sudo', {'command': 'sudo true'}, timeout=5)), daemon=True)
            worker.start()
            await async_wait_until(lambda: any(f.get('method') == 'sudo' for f in transport.frames), timeout=5,
                                   what='sudo request frame')
            rid = next(f['id'] for f in transport.frames if f.get('method') == 'sudo')
            return worker, out, rid, events

        async def answer(connection, rid, value):
            with owner_scope(connection.authority):
                await connection.dispatch({'jsonrpc': '2.0', 'id': rid, 'result': {'value': value}})

        desk, desk_transport, desk_session = await open_beta_chat('c0')
        worker, out, rid, _ = await ask_sudo(desk_transport, desk_session)
        await answer(desk, rid, 'hunter2')
        await asyncio.to_thread(worker.join, 10)
        assert out == {'value': 'hunter2'}, out

        other, other_transport, other_session = await open_beta_chat('c1')
        worker, out, rid, events = await ask_sudo(other_transport, other_session)
        assert not any(f.get('id') == rid for f in desk_transport.frames)
        await answer(desk, rid, 'leaked')
        assert waiting(events, other_session, 'sudo') and worker.is_alive() and out == {}
        await answer(other, rid, 'mine')
        await asyncio.to_thread(worker.join, 10)
        assert out == {'value': 'mine'}, out

        with owner_scope(other.authority):
            joined = await other.dispatch({'id': 2, 'method': 'session.resume', 'params': {
                'profile': 'beta', 'session_id': desk_session}})
        assert 'error' not in joined, joined
        subscription = joined['result']['subscription_id']
        for transport in (desk_transport, other_transport):
            server_requests.advertise(transport, True)

        async def ask_terminal():
            from gateway.local_desktop_ui import _read_block
            turn = SimpleNamespace(_approval_owner=(beta, desk_session, 1))
            read = _read_block(turn, SimpleNamespace(_interrupt_requested=False), 'terminal.read', 5)
            out = {}
            desk_transport.frames.clear()
            other_transport.frames.clear()
            worker = threading.Thread(target=lambda: out.update(value=read(0, 2)), daemon=True)
            worker.start()
            await async_wait_until(lambda: any(f.get('method') == 'terminal.read' for f in desk_transport.frames),
                                   timeout=5, what='terminal read request')
            frame = next(f for f in desk_transport.frames if f.get('method') == 'terminal.read')
            assert frame['params'] == {'session_id': desk_session, 'owner_profile': 'beta', 'start': 0, 'count': 2}
            return worker, out, frame['id']

        async def decline(connection, request_id, code):
            with owner_scope(connection.authority):
                await connection.dispatch({'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code}})

        worker, out, rid = await ask_terminal()
        await decline(other, rid, -32601)
        await decline(other, rid, server_requests.NOT_SHOWN_CODE)
        assert worker.is_alive() and out == {}
        await answer(desk, rid, 'code output')
        await asyncio.to_thread(worker.join, 5)
        assert out == {'value': 'code output'}

        worker, out, rid = await ask_terminal()
        await decline(desk, rid, server_requests.NOT_SHOWN_CODE)
        assert worker.is_alive() and out == {}
        await decline(other, rid, server_requests.NOT_SHOWN_CODE)
        await asyncio.to_thread(worker.join, 2)
        assert json.loads(out['value'])['success'] is False

        worker, out, rid = await ask_terminal()
        await decline(desk, rid, server_requests.NOT_SHOWN_CODE)
        with owner_scope(other.authority):
            detached = await other.dispatch({'id': 3, 'method': 'session.detach', 'params': {
                'profile': 'beta', 'session_id': desk_session, 'subscription_id': subscription}})
        assert 'error' not in detached, detached
        await asyncio.to_thread(worker.join, 2)
        assert json.loads(out['value'])['success'] is False
    finally:
        for connection in connections:
            server_requests.forget(connection.transport)
            with owner_scope(connection.authority):
                await connection.close()
        for authority in getattr(runner, 'session_authorities', None) or []:
            close_stores_under(authority.persistence.scope.profile_dir)
        for _, home in homes:
            process_ownership.release(Path(home))

"""Cancellation before reservation closes any acquired child and settles the unexecuted claim."""
import asyncio
import os
import subprocess
import sys
import threading

import pytest

from gateway.session_contract import Submission
from tests.fixtures.storage_holder import raw_holder
from tests.gateway.test_session_authority_cancel_settlement import _authority, ACTOR, REF
from tests.waiting import async_wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['environment', 'spawn', 'hello'])
async def test_cancel_before_worker_reservation_settles_interrupted(tmp_path, monkeypatch, phase):
    from gateway import session_managed_worker as managed
    db, authority = _authority(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    children = []
    original_spawn = subprocess.Popen
    def environment(owner):
        if phase == 'environment':
            entered.set()
            assert release.wait(10)
        return dict(os.environ)
    def spawn(args, **kwargs):
        if phase == 'spawn':
            entered.set()
            assert release.wait(10)
        child = original_spawn([sys.executable, '-c', 'import sys; sys.stdin.buffer.read()'], **kwargs)
        children.append(child)
        return child
    original_next = managed.ManagedWorker.next_frame
    async def next_frame(worker, *args, **kwargs):
        if phase == 'hello':
            entered.set()
        return await original_next(worker, *args, **kwargs)
    monkeypatch.setattr(managed, '_worker_env', environment)
    monkeypatch.setattr(managed.subprocess, 'Popen', spawn)
    monkeypatch.setattr(managed.ManagedWorker, 'next_frame', next_frame)
    async def execute(owner, ref, row):
        return await managed.execute_managed(owner, ref, row, policy=None)
    monkeypatch.setattr('gateway.session_finite.execute_finite_admission', execute)
    with db:
        receipt = await authority.submit(ACTOR, Submission('cancelled-startup', REF, {'text': 'work'}, 'queue'))
        task = authority.sessions['s'].task
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            release.set()
            await asyncio.wait_for(task, 10)
            row = authority.persistence.admissions.get(admission_id=receipt.admission_id)
            assert row.status == 'terminal' and row.outcome == 'interrupted'
            assert authority.sessions['s'].event_stream.execution == {}
            if phase != 'environment':
                await async_wait_until(lambda: children and all(child.poll() is not None for child in children),
                                       timeout=10, what='the managed worker child exited')
            with raw_holder(authority.persistence)._read_ctx() as conn:
                assert conn.execute('SELECT COUNT(*) FROM worker_executions').fetchone()[0] == 0
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)


@pytest.mark.asyncio
async def test_stop_during_a_failed_ssh_workdir_probe_settles_before_spawn(monkeypatch):
    from types import SimpleNamespace

    from gateway import session_managed_worker

    entered, release = threading.Event(), threading.Event()

    def unavailable(_):
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr("gateway.session_policy.ready_workspace", unavailable)
    monkeypatch.setattr("hermes_cli.main_resume_args.uses_managed_executor", lambda: True)
    authority = SimpleNamespace(pending_results={}, pending_stops={}, pending_stop_causes={})
    ref = SimpleNamespace(session_id="chat")
    row = {"admission_id": "turn", "generation": 8}
    turn = asyncio.create_task(session_managed_worker.execute_managed(authority, ref, row, SimpleNamespace(source="cli")))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        assert authority._managed_spawning == {"chat": 8}
        authority.pending_stops["chat"] = 8
        authority.pending_stop_causes["chat"] = (8, "Gateway shutdown")
    finally:
        release.set()

    assert await turn == ""
    assert authority._managed_spawning == {}
    assert authority.pending_results["turn"]["result"] == {
        "final_response": "",
        "interrupted": True,
        "interrupt_message": "Gateway shutdown",
    }

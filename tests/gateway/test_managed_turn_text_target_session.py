"""Managed turn context-reference expansion binds staged attachments to target_session_id."""

import json
from types import SimpleNamespace

import pytest

from gateway import session_context_refs


@pytest.mark.asyncio
async def test_managed_turn_text_passes_target_session_id_to_expand_references(monkeypatch):
    seen = {}

    async def fake_context_length(*_args, **_kwargs):
        return 8192

    async def fake_expand(text, **kwargs):
        seen["text"] = text
        seen.update(kwargs)
        return text + " [expanded]", None

    monkeypatch.setattr("agent.model_metadata.get_model_context_length_async", fake_context_length)
    monkeypatch.setattr(session_context_refs, "expand_references", fake_expand)

    policy = SimpleNamespace(
        cwd="/workspace/project",
        ignore_user_config=False,
        terminal_json=json.dumps({"TERMINAL_ENV": "ssh", "TERMINAL_CWD": "/workspace/project"}),
        base_url="",
        provider="test",
        config=lambda _authority: {"model": {}},
        current_model=lambda _authority: "test/model",
    )
    row = {
        "admission_id": "adm-1",
        "target_session_id": "sess-owner-42",
        "payload": {"text": "inspect @file:staged:upload-1"},
    }

    text, refusal = await session_context_refs.managed_turn_text(object(), row, policy)

    assert refusal is None
    assert text == "inspect @file:staged:upload-1 [expanded]"
    assert seen["attachment_session_id"] == "sess-owner-42"
    assert seen["cwd"] == "/workspace/project"

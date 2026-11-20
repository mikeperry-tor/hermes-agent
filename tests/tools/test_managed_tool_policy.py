"""Managed denial reaches discovery and every native execution boundary without hosted mode."""
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def policy(tmp_path, monkeypatch):
    folder = tmp_path / 'managed'
    folder.mkdir()
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(folder))
    def write(value):
        (folder / 'config.yaml').write_text(value)
    write('tools:\n  disabled: []\n')
    return write


@pytest.mark.parametrize("settled", [False, True])
@pytest.mark.parametrize("kind", ["mcp", "plugin"])
def test_setup_result_does_not_advertise_denied_names_or_schemas(policy, settled, kind):
    from tools.connectors.contract import TargetState
    from tools.connectors.operation import ConnectionOperation, Target
    from tools.registry import registry

    allowed = "mcp__policy_fixture__allowed"
    denied = "mcp__policy_fixture__denied"
    visible = "Allowed fixture description"
    hidden = "Denied fixture description"
    for name, description in ((allowed, visible), (denied, hidden)):
        registry.register(name=name, toolset="mcp-policy-fixture",
            schema={"name": name, "description": description, "parameters": {}},
            handler=lambda args, **kw: "{}")
    try:
        operation = ConnectionOperation([Target("fixture", kind, "connect" if kind == "mcp" else "install",
                                                 state=TargetState.connected,
                                                 extra={"tools": [allowed, denied]})],
                                        settled_at=time.time() if settled else None)
        policy(f"tools:\n  disabled: [{denied}]\n")
        [target] = operation.result()["targets"]
        assert target["tools"] == [allowed]
        if settled and kind == "mcp":
            assert allowed in target["tools_listing"]
            assert hidden not in target["tools_listing"]
        policy(f"tools:\n  disabled: [{denied}, {allowed}]\n")
        [target] = operation.result()["targets"]
        assert target["state"] == "connected"
        assert target["tools"] == [] and "tools_listing" not in target
        assert operation.snapshot()["targets"][0]["tools"] == [allowed, denied]
    finally:
        for name in (allowed, denied):
            registry.deregister(name)


def test_connected_mcp_server_stays_selected_when_all_tools_are_denied(policy):
    from agent.inline_tool_executors import _scope_in_connected_mcp_servers
    from model_tools import get_tool_definitions
    from tools.connectors.contract import TargetState
    from tools.connectors.operation import ConnectionOperation, Target
    from tools.registry import registry

    server = "deny_then_relax"
    name = f"mcp__{server}__lookup"
    registry.register(name=name, toolset=f"mcp-{server}",
        schema={"name": name, "description": "Fixture lookup", "parameters": {}},
        handler=lambda args, **kw: "{}")
    registry.register_toolset_alias(server, f"mcp-{server}")
    try:
        def connect(**extra):
            operation = ConnectionOperation([Target(
                server, "mcp", "connect", state=TargetState.connected,
                extra={"tools": [name], **extra})])
            result = operation.result()
            assert result["targets"][0]["tools"] == []
            _scope_in_connected_mcp_servers(agent, json.dumps(result))

        def visible():
            return {tool["function"]["name"] for tool in
                    get_tool_definitions(enabled_toolsets=agent.enabled_toolsets,
                                         quiet_mode=True, skip_tool_search_assembly=True)}

        agent = SimpleNamespace(enabled_toolsets=["memory"])
        policy(f"tools:\n  disabled: [{name}]\n")
        connect(discovery_error="Fixture discovery failed")
        assert agent.enabled_toolsets == ["memory"]
        connect()
        assert agent.enabled_toolsets == ["memory", server]
        assert name not in visible()
        policy("tools:\n  disabled: []\n")
        assert name in visible()
    finally:
        registry.deregister(name)


def test_wrong_tool_name_suggests_only_permitted_registry_matches(policy):
    from tools.registry import registry
    from tools.tool_search_validation import not_deferrable_error

    allowed = "mcp__policy_allowed__shared_suffix"
    denied = "mcp__policy_denied__shared_suffix"
    for name in (allowed, denied):
        registry.register(name=name, toolset="mcp-policy-fixture",
            schema={"name": name, "description": "Fixture", "parameters": {}},
            handler=lambda args, **kw: "{}")
    try:
        policy(f"tools:\n  disabled: [{denied}]\n")
        hint = not_deferrable_error("shared_suffix")
        assert allowed in hint and denied not in hint
    finally:
        for name in (allowed, denied):
            registry.deregister(name)


def test_managed_denial_overrides_profile_and_reaches_registry_inline_rpc(policy, tmp_path, monkeypatch):
    from agent.runtime_scope import bind_profile_scope
    from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
    from hermes_cli.env_loader import load_hermes_dotenv
    from tools.registry import registry
    from tools.code_execution_rpc import _default_dispatch, _handle_rpc_request
    from tools.hosted_tool_policy import tool_refusal
    from tui_gateway.launch_profile_policy import activate_multi_profile_hosting
    reached = []
    registry.register(name='policy_probe', toolset='policy_probe',
        schema={'name': 'policy_probe', 'description': 'probe', 'parameters': {}},
        handler=lambda args, **kw: reached.append(args) or '{}')
    home = tmp_path / 'profile'; home.mkdir()
    (home / 'config.yaml').write_text('tools:\n  disabled: []\nagent:\n  disabled_toolsets: []\n')
    (home / '.env').write_text('HERMES_MANAGED_DIR=/profile-controlled\n')
    activate_multi_profile_hosting()
    authority = os.environ['HERMES_MANAGED_DIR']
    try:
        assert registry.dispatch('policy_probe', {}) == '{}'
        reached.clear()
        policy('tools:\n  disabled: [policy_probe, memory]\n')
        with bind_profile_scope(home):
            load_hermes_dotenv(hermes_home=str(home))
            assert os.environ['HERMES_MANAGED_DIR'] == authority
            assert tool_refusal('policy_probe')
            assert 'managed' in registry.dispatch('policy_probe', {})
            result = INLINE_TOOL_EXECUTORS['memory'](SimpleNamespace(), {}, InlineToolContext('t'))
            assert 'managed' in result
            result = _handle_rpc_request({'tool': 'policy_probe', 'args': {}},
                allowed_tools=frozenset({'policy_probe'}), tool_call_counter=[0], max_tool_calls=1,
                dispatch=_default_dispatch('t'), tool_call_log=[], call_start=time.monotonic(), where='fixture')
            assert 'managed' in result
            child = subprocess.run([sys.executable, '-c',
                'from tools.hosted_tool_policy import tool_refusal; assert tool_refusal("policy_probe")'],
                capture_output=True, text=True, check=True)
            assert child.returncode == 0
        assert reached == []
    finally:
        registry.deregister('policy_probe')


@pytest.mark.parametrize('path', ['sequential', 'concurrent'])
def test_explicit_child_toolsets_and_stale_schema_cannot_run_denied_tools(policy, tmp_path, path):
    from tests.tools.test_hosted_deny_by_default import _make_agent, _ProviderTools, _call
    from agent import tool_executor as te
    agent = _make_agent(tmp_path)
    agent.enabled_toolsets = ['all']
    agent._memory_manager = provider = _ProviderTools()
    policy('tools:\n  disabled: [provider_recall]\n')
    messages = []
    run = te.execute_tool_calls_sequential if path == 'sequential' else te.execute_tool_calls_concurrent
    run(agent, SimpleNamespace(tool_calls=[_call('c1', 'provider_recall')]), messages, 't')
    assert provider.reached == []
    assert 'managed' in messages[0]['content']


@pytest.mark.parametrize('path', ['sequential', 'concurrent'])
@pytest.mark.parametrize('denied', ['tool_call', 'underlying', None])
def test_tool_call_bridge_and_underlying_both_require_permission(policy, monkeypatch, tmp_path, path, denied):
    from agent import tool_executor
    from tests.tools.test_hosted_deny_by_default import _make_agent, _call
    from tools.registry import registry

    name = 'mcp__policy_fixture__inspect'
    agent = _make_agent(tmp_path)
    agent.valid_tool_names = {'tool_call', name}
    monkeypatch.setattr(tool_executor, '_tool_search_scoped_names', lambda _agent: {name})
    handler = Mock(return_value='{"ok": true}')
    ready = Mock(return_value=True)
    registry.register(name=name, toolset='mcp-policy-fixture',
                      schema={'name': name, 'description': 'Fixture', 'parameters': {}},
                      check_fn=ready, handler=handler)
    try:
        disabled = [name if denied == 'underlying' else denied] if denied else []
        policy(f'tools:\n  disabled: {json.dumps(disabled)}\n')
        message = _call('bridge-call', 'tool_call', {'calls': [{'name': name, 'arguments': {}}]})
        outputs = []
        run = (tool_executor.execute_tool_calls_sequential if path == 'sequential'
               else tool_executor.execute_tool_calls_concurrent)
        run(agent, SimpleNamespace(tool_calls=[message]), outputs, 'task')
        assert outputs[0]['tool_call_id'] == 'bridge-call'
        if denied:
            handler.assert_not_called()
            ready.assert_not_called()
            assert 'managed deployment policy' in outputs[0]['content']
        else:
            handler.assert_called_once()
            assert json.loads(outputs[0]['content']) == {'ok': True}
    finally:
        registry.deregister(name)


def test_empty_managed_mapping_keeps_native_defaults(policy):
    from tools.hosted_tool_policy import tool_refusal
    policy('{}')
    assert tool_refusal('terminal') is None


@pytest.mark.parametrize('document', ['', '# optional managed settings\n', 'null', '[]', 'false', '42', 'tools: []', 'tools:\n  disabled: true',
    'tools:\n  disabled: [42]', 'tools:\n  disabled: [" padded "]', 'tools: [unterminated'])
def test_invalid_policy_refuses_then_recovers_without_values(policy, document):
    from tools.hosted_tool_policy import tool_refusal
    # Warm the native config cache before the invalid replacement.
    assert tool_refusal('terminal') is None
    policy(document)
    result = tool_refusal('terminal')
    assert result and 'could not be read or validated' in result
    assert not document or document not in result
    policy('tools:\n  disabled: []\n')
    assert tool_refusal('terminal') is None


def test_unreadable_policy_and_optional_absence(policy, monkeypatch):
    from pathlib import Path
    from tools.hosted_tool_policy import tool_refusal
    original = Path.read_text
    def unreadable(path, *args, **kwargs):
        if str(path) == os.environ['HERMES_MANAGED_DIR'] + '/config.yaml':
            raise PermissionError('SYNTHETIC-PRIVATE-POLICY')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', unreadable)
    assert 'could not be read' in tool_refusal('terminal')
    monkeypatch.setattr(Path, 'read_text', original)
    Path(os.environ['HERMES_MANAGED_DIR'], 'config.yaml').unlink()
    assert tool_refusal('terminal') is None


@pytest.mark.parametrize("failure", ["directory", "backend"])
def test_managed_authority_failure_blocks_discovery_and_dispatch(policy, monkeypatch, failure):
    from pathlib import Path
    import model_tools
    from hermes_cli import config_backend
    from tools.hosted_tool_policy import tool_refusal

    managed = Path(os.environ["HERMES_MANAGED_DIR"])
    stat = Path.stat

    def unsearchable(path, *args, **kwargs):
        if path in (managed, managed / "config.yaml"):
            raise PermissionError("SYNTHETIC-PRIVATE-POLICY")
        return stat(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        if failure == "backend":
            scoped.setattr(config_backend, "get_config_backend",
                           Mock(side_effect=config_backend.ConfigBackendUnavailable("SYNTHETIC-PRIVATE-POLICY")))
        else:
            is_dir = Path.is_dir
            scoped.setattr(Path, "is_dir", lambda path: False if path == managed else is_dir(path))
            scoped.setattr(Path, "stat", unsearchable)
        assert "could not be read or validated" in tool_refusal("terminal")
        assert not model_tools.get_tool_definitions(quiet_mode=True, skip_tool_search_assembly=True)
    assert tool_refusal("terminal") is None


def test_discovery_cache_tightening_relaxation_and_tool_search_listing(policy, monkeypatch):
    import model_tools
    from tools.registry import registry
    from tools import tool_search
    names = ['mcp_policy_allowed', 'mcp_policy_denied']
    for name in names:
        registry.register(name=name, toolset='policy_probe',
            schema={'name': name, 'description': name, 'parameters': {}}, handler=lambda args, **kw: '{}')
    monkeypatch.setattr(model_tools, '_select_tool_names', lambda *args: set(names))
    monkeypatch.setattr(tool_search, 'load_config', lambda: tool_search.ToolSearchConfig.from_raw({'enabled': 'on', 'defer': names}))
    try:
        unrestricted = model_tools.get_tool_definitions(quiet_mode=True)
        assert 'mcp_policy_denied' in json.dumps(unrestricted)
        policy('tools:\n  disabled: [mcp_policy_denied]\n')
        restricted = model_tools.get_tool_definitions(quiet_mode=True)
        assert 'mcp_policy_denied' not in json.dumps(restricted)
        assert 'mcp_policy_allowed' in json.dumps(restricted)
        policy('tools: [unterminated')
        assert model_tools.get_tool_definitions(quiet_mode=True) == []
        policy('tools:\n  disabled: []\n')
        assert 'mcp_policy_denied' in json.dumps(model_tools.get_tool_definitions(quiet_mode=True))
    finally:
        for name in names: registry.deregister(name)


def test_catalog_and_schema_cache_use_the_policy_captured_before_readiness(policy, monkeypatch):
    import model_tools
    from tools import tool_search
    from tools.registry import registry

    name = "policy_ready_fixture"
    monkeypatch.setattr(model_tools, "_select_tool_names", lambda *args: {name})
    monkeypatch.setattr(tool_search, "load_config", lambda: tool_search.ToolSearchConfig.from_raw(
        {"enabled": "on", "defer": [name]}))

    def during_probe():
        policy("tools:\n  disabled: [tool_call]\n")
        return True

    registry.register(name=name, toolset="policy_fixture", check_fn=during_probe,
                      schema={"name": name, "description": "Fixture", "parameters": {}},
                      handler=lambda args, **kw: "{}")
    try:
        initial = model_tools.get_tool_definitions(quiet_mode=True)
        assert "tool_call" in {td["function"]["name"] for td in initial}
        restricted = model_tools.get_tool_definitions(quiet_mode=True)
        assert {td["function"]["name"] for td in restricted} == {name}
        policy("tools:\n  disabled: []\n")
        assert model_tools.get_tool_definitions(quiet_mode=True) == initial
    finally:
        registry.deregister(name)


def test_denied_tools_are_absent_from_sibling_hints_and_readiness_probes(policy, monkeypatch):
    import model_tools
    from tools.registry import ToolRegistry, invalidate_check_fn_cache

    registry = ToolRegistry()
    web_ready = Mock(return_value=True)
    names = {'execute_code', 'browser_cdp', 'browser_vault', 'drive_preview', 'web_search', 'web_extract'}
    for name in sorted(names):
        registry.register(name=name, toolset='policy_fixture',
                          schema={'name': name, 'description': 'Fixture', 'parameters': {}},
                          check_fn=web_ready if name.startswith('web_') else None,
                          handler=lambda args, **kw: '{}')
    monkeypatch.setattr(model_tools, 'registry', registry)
    monkeypatch.setattr(model_tools, '_select_tool_names', lambda *args: names)
    def descriptions():
        return {item['function']['name']: item['function']['description']
                for item in model_tools.get_tool_definitions(quiet_mode=True, skip_tool_search_assembly=True)}

    unrestricted = descriptions()
    assert 'web_search(query' in unrestricted['execute_code']
    assert 'web_extract tool is available' in unrestricted['browser_cdp']
    web_ready.reset_mock()
    invalidate_check_fn_cache()
    policy('tools:\n  disabled: [web_search, web_extract, tool_call]\n')
    restricted = descriptions()
    assert set(restricted) == {'execute_code', 'browser_cdp', 'browser_vault', 'drive_preview'}
    assert 'web_search(query' not in restricted['execute_code']
    assert 'web_extract tool is available' not in restricted['browser_cdp']
    assert 'call browser_vault' in restricted['drive_preview']
    assert 'through tool_call' not in restricted['drive_preview']
    web_ready.assert_not_called()


def test_disabling_a_bridge_tool_keeps_permitted_definitions_callable_directly(policy):
    from tools.tool_search import ToolSearchConfig, assemble_tool_defs

    name = 'mcp__policy_fixture__inspect'
    schema = {'type': 'function', 'function': {'name': name, 'description': 'Fixture', 'parameters': {}}}
    config = ToolSearchConfig.from_raw({'enabled': 'on', 'defer': [name]})
    assembled = assemble_tool_defs([schema], config=config)
    assert assembled.activated and 'tool_call' in json.dumps(assembled.tool_defs)
    policy('tools:\n  disabled: [tool_call]\n')
    assembled = assemble_tool_defs([schema], config=config)
    assert not assembled.activated and assembled.tool_defs == [schema]


def test_vault_hint_uses_the_available_call_route(policy, monkeypatch):
    import model_tools
    from tools.registry import ToolRegistry
    from tools.tool_search import ToolSearchConfig

    registry = ToolRegistry()
    names = {'browser_vault', 'drive_preview'}
    for name in names:
        registry.register(name=name, toolset='policy_fixture',
                          schema={'name': name, 'description': 'Fixture', 'parameters': {}},
                          handler=lambda args, **kw: '{}')
    monkeypatch.setattr(model_tools, 'registry', registry)
    monkeypatch.setattr(model_tools, '_select_tool_names', lambda *args: names)
    monkeypatch.setattr('tools.tool_search.load_config',
                        lambda: ToolSearchConfig.from_raw({'enabled': 'on', 'defer': ['browser_vault']}))

    def definitions():
        return {item['function']['name']: item['function']['description']
                for item in model_tools.get_tool_definitions()}

    deferred = definitions()
    assert 'browser_vault' not in deferred and 'tool_call' in deferred
    assert 'call browser_vault through tool_call' in deferred['drive_preview']

    both_deferred = ToolSearchConfig.from_raw({'enabled': 'on', 'defer': ['browser_vault', 'drive_preview']})
    with monkeypatch.context() as context:
        context.setattr('tools.tool_search.load_config', lambda: both_deferred)
        context.setattr('tools.tool_search.load_config_readonly', lambda: both_deferred)
        assert not names.intersection(definitions())
        reply, _ = model_tools._dispatch_bridge_tool('tool_describe', {'names': ['drive_preview']}, None, None)
        assert 'using tool_call if it is deferred' in json.loads(reply)['tools']['drive_preview']['description']

    policy('tools:\n  disabled: [tool_call]\n')
    direct = definitions()
    assert 'browser_vault' in direct and 'tool_call' not in direct
    assert 'call browser_vault (action=list first)' in direct['drive_preview']
    policy('tools:\n  disabled: [browser_vault]\n')
    assert 'call browser_vault' not in definitions()['drive_preview']


def test_tool_listing_omits_managed_denials_before_readiness(policy, monkeypatch, capsys):
    import model_tools
    import run_agent
    from tools.registry import ToolRegistry, invalidate_check_fn_cache

    registry = ToolRegistry()
    denied = Mock(return_value=True)
    allowed = Mock(return_value=True)
    for name, check in [('policy_fixture_hidden', denied), ('policy_fixture_visible', allowed)]:
        registry.register(name=name, toolset='policy_fixture',
                          schema={'name': name, 'description': 'Fixture', 'parameters': {}},
                          check_fn=check, handler=lambda args, **kw: '{}')
    monkeypatch.setattr(model_tools, 'registry', registry)
    monkeypatch.setattr('toolsets.get_all_toolsets', lambda: ['policy_fixture'])
    monkeypatch.setattr('toolsets.get_toolset_info', lambda name: {
        'description': 'Fixture', 'resolved_tools': registry.get_all_tool_names(), 'includes': [],
    })
    invalidate_check_fn_cache()
    policy('tools:\n  disabled: [policy_fixture_hidden]\n')
    run_agent._print_tool_listing()
    output = capsys.readouterr().out
    assert 'policy_fixture_visible' in output
    assert 'policy_fixture_hidden' not in output
    denied.assert_not_called()
    allowed.assert_called()


def test_invalid_managed_policy_reports_discovery_failure_without_contents(policy, caplog):
    import model_tools

    policy('tools:\n  disabled: private-placeholder\n')
    assert model_tools.get_tool_definitions(quiet_mode=True) == []
    assert 'Managed tool policy could not be read or validated' in caplog.text
    assert 'private-placeholder' not in caplog.text


def test_unavailable_source_summaries_only_probe_and_count_permitted_entries(policy, monkeypatch):
    from hermes_platform import declaration
    from tools import mcp_liveness, registry, tool_search
    from tools.tool_search_catalog import build_catalog_listing_with_form

    allowed = Mock(return_value=False)
    denied = Mock(return_value=False)
    entries = [
        SimpleNamespace(name='fixture_allowed', toolset='mcp-mixed', check_fn=allowed),
        SimpleNamespace(name='fixture_mixed_denied', toolset='mcp-mixed', check_fn=denied),
        SimpleNamespace(name='fixture_hidden', toolset='mcp-hidden', check_fn=denied),
    ]
    monkeypatch.setattr(registry.registry, 'get_all_entries', lambda: entries)
    monkeypatch.setattr(declaration, 'lookup', lambda name: object())
    monkeypatch.setattr(mcp_liveness, 'unavailable_details',
                        lambda name: (None, None, f'{name} is down'))
    policy('tools:\n  disabled: [fixture_mixed_denied, fixture_hidden]\n')

    listing, _ = build_catalog_listing_with_form([])
    assert listing and 'mixed (1 tools unavailable' in listing and 'hidden' not in listing
    assert tool_search._available_source_summary([]) == [
        {'name': 'mixed', 'tool_count': 1, 'unavailable': 'mixed is down'}]
    assert allowed.called
    denied.assert_not_called()


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("disable_bridge", [False, True])
def test_agent_snapshot_replaces_stale_bridge_and_dynamic_tools(policy, monkeypatch, resume, disable_bridge):
    import model_tools
    from agent.turn_context import _refresh_mcp_tools_between_turns
    from tools import bot_mode_dm, mcp_tool_agent, tool_search
    from tools.mcp_tool_discovery import has_registered_mcp_tools
    from tools.registry import registry
    from toolsets import tool_selection_key

    def schema(name, description="Fixture"):
        return {"name": name, "description": description, "parameters": {}}

    def definition(name, description="Fixture"):
        return {"type": "function", "function": schema(name, description)}

    allowed, denied = "policy_fixture_allowed", "policy_fixture_denied"
    names = {allowed, denied}
    monkeypatch.setattr(model_tools, "_select_tool_names", lambda *args: names)
    monkeypatch.setattr(tool_search, "load_config", lambda: tool_search.ToolSearchConfig.from_raw(
        {"enabled": "on", "defer": list(names)}))
    monkeypatch.setattr(bot_mode_dm, "message_agent_authorized", lambda _agent: True)
    for name in names:
        registry.register(name=name, toolset="policy_fixture", schema=schema(name),
                          handler=lambda args, **kw: "{}")
    try:
        assert not has_registered_mcp_tools()
        previous = [definition("tool_search", f"Search {denied}"), definition("tool_call"),
                    definition(denied), definition("lcm_hidden"), definition("memory_hidden"),
                    definition("message_agent")]
        agent = SimpleNamespace(tools=previous.copy(),
            valid_tool_names={item["function"]["name"] for item in previous},
            _tool_snapshot_selection=tool_selection_key(None, None), _skip_mcp_refresh=True,
            _memory_manager=SimpleNamespace(get_all_tool_schemas=lambda: [schema("memory_hidden")]),
            context_compressor=SimpleNamespace(get_tool_schemas=lambda: [schema("lcm_hidden")]),
            _context_engine_tool_names={"lcm_hidden"})
        disabled = [denied, "lcm_hidden", "memory_hidden", "message_agent"]
        if disable_bridge:
            disabled.append("tool_call")
        policy(f"tools:\n  disabled: {json.dumps(disabled)}\n")
        fresh = model_tools.get_tool_definitions(quiet_mode=True)
        fresh_names = {item["function"]["name"] for item in fresh}
        if disable_bridge:
            assert fresh_names == {allowed}
        else:
            assert "tool_search" in fresh_names and allowed in json.dumps(fresh)
        assert denied not in json.dumps(fresh)
        if resume:
            agent.tools = fresh.copy()
            agent.valid_tool_names = fresh_names.copy()
            agent._tool_snapshot_selection = tool_selection_key(None, None)
            agent._context_engine_tool_names.clear()
            saved = {"version": mcp_tool_agent.tool_pin_version(), "tools": previous}
            mcp_tool_agent.restore_agent_tool_prefix(agent, saved)
        else:
            _refresh_mcp_tools_between_turns(agent)
        assert agent.tools == fresh
        assert agent.valid_tool_names == fresh_names and not agent._context_engine_tool_names

        # The Bot Chat per-turn injection also drops a stale schema and routing entry.
        agent.tools.append(definition("message_agent"))
        agent.valid_tool_names.add("message_agent")
        assert not bot_mode_dm.ensure_message_agent_tool(agent)
        assert agent.tools == fresh and agent.valid_tool_names == fresh_names
        if not resume:
            for invalid in ("tools: [unterminated", "null"):
                policy(invalid)
                _refresh_mcp_tools_between_turns(agent)
                assert not agent.tools and not agent.valid_tool_names and not agent._context_engine_tool_names
            policy("tools:\n  disabled: []\n")
            _refresh_mcp_tools_between_turns(agent)
            assert denied in json.dumps(agent.tools)
            assert {"memory_hidden", "lcm_hidden", "message_agent"} <= agent.valid_tool_names
            assert agent._context_engine_tool_names == {"lcm_hidden"}
    finally:
        for name in names:
            registry.deregister(name)


def test_managed_pin_keeps_frozen_schema_and_delegation_until_policy_changes(policy, monkeypatch):
    import model_tools
    from tools import mcp_tool_agent
    from tools.registry import registry
    from toolsets import tool_selection_key

    def definition(name, description):
        return {"type": "function", "function": {"name": name, "description": description, "parameters": {}}}

    saved_defs = [definition("delegate_task", "Pinned delegation variant"),
                  definition("tool_search", "Pinned permitted catalog")]
    policy("tools:\n  disabled: [terminal]\n")
    db = Mock()

    def agent_with(definitions, backend):
        return SimpleNamespace(tools=definitions, session_id="pinned-managed-tools",
            valid_tool_names={item["function"]["name"] for item in definitions}, _session_db=db,
            _delegation_backend=backend, _tool_snapshot_selection=tool_selection_key(None, None),
            _tool_snapshot_generation=registry._generation)

    sent = agent_with(saved_defs, "pinned-backend")
    mcp_tool_agent.persist_agent_tool_names(sent)
    pin = db.update_session_tool_names.call_args.args[1]
    assert pin["managed_disabled_tools"] == ["terminal"]
    db.reset_mock()
    changed = [definition("delegate_task", "Fresh delegation variant"), definition("tool_search", "Fresh catalog")]
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **_kw: changed)
    resident = agent_with(saved_defs, "pinned-backend")
    mcp_tool_agent.refresh_agent_mcp_tools(resident, preserve_prefix=True, content_aware=True)
    assert resident.tools == saved_defs and resident._delegation_backend == "pinned-backend"

    resumed = agent_with(changed, "fresh-backend")
    assert mcp_tool_agent.restore_agent_tool_prefix(resumed, pin)
    assert resumed.tools == saved_defs and resumed._delegation_backend == "pinned-backend"
    db.update_session_tool_names.assert_not_called()

    policy("tools:\n  disabled: []\n")
    resumed = agent_with(changed, "fresh-backend")
    mcp_tool_agent.restore_agent_tool_prefix(resumed, pin)
    assert resumed.tools == changed and resumed._delegation_backend == "fresh-backend"
    new_pin = db.update_session_tool_names.call_args.args[1]
    assert new_pin["tools"] == changed and "managed_disabled_tools" not in new_pin


@pytest.mark.parametrize("competing_refresh", [False, True])
def test_refresh_discards_a_policy_superseded_mid_build(policy, monkeypatch, competing_refresh):
    import model_tools
    from tools import mcp_tool_agent
    from tools.registry import registry
    from toolsets import tool_selection_key

    def tool(name, description):
        return {"type": "function", "function": {"name": name, "description": description}}

    mixed = [tool("allowed", "See policy_fixture_denied"),
             tool("policy_fixture_denied", "Old capability"), tool("tool_call", "Old bridge")]
    fresh = [tool("allowed", "Direct allowed tool")]
    db = Mock()
    generation = registry._generation
    agent = SimpleNamespace(tools=mixed.copy(), valid_tool_names={"allowed", "policy_fixture_denied", "tool_call"},
        _tool_snapshot_selection=tool_selection_key(None, None), _tool_snapshot_generation=generation,
        _session_db=db, session_id="policy-race")
    builds = []

    def schema_build(*, managed_policy=None, **_kwargs):
        builds.append(managed_policy)
        if not managed_policy:
            policy("tools:\n  disabled: [policy_fixture_denied, tool_call]\n")
            if competing_refresh:
                mcp_tool_agent.refresh_agent_mcp_tools(agent, preserve_prefix=True)
            return mixed.copy()
        return fresh.copy()

    monkeypatch.setattr(model_tools, "get_tool_definitions", schema_build)
    mcp_tool_agent.refresh_agent_mcp_tools(agent, preserve_prefix=True)
    assert registry._generation == generation
    assert builds[0] == frozenset() and builds[-1] == frozenset({"policy_fixture_denied", "tool_call"})
    assert agent.tools == fresh and agent.valid_tool_names == {"allowed"}
    assert agent._tool_snapshot_selection[2] == frozenset({"policy_fixture_denied", "tool_call"})
    assert db.update_session_tool_names.call_count == 1
    pin = db.update_session_tool_names.call_args.args[1]
    assert pin["tools"] == fresh and pin["managed_disabled_tools"] == ["policy_fixture_denied", "tool_call"]


def test_concurrent_refresh_wins_over_restoring_a_saved_pin(policy, monkeypatch):
    import model_tools
    from tools import mcp_tool_agent
    from tools.registry import registry
    from toolsets import tool_selection_key

    def tool(name, description):
        return {"type": "function", "function": {"name": name, "description": description}}

    old = [tool("allowed", "Old prefix mentioning terminal"), tool("terminal", "Old terminal")]
    fresh = [tool("allowed", "Current permitted tool")]
    initial = [tool("allowed", "Initial discovery permit"), old[1]]
    agent = SimpleNamespace(tools=initial, valid_tool_names={"allowed", "terminal"},
        _tool_snapshot_selection=tool_selection_key(None, None), _tool_snapshot_generation=registry._generation,
        _session_db=Mock(), session_id="restore-race", _delegation_backend="fresh")
    saved = {"version": mcp_tool_agent.tool_pin_version(), "tools": old, "delegation_backend": "old"}
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **_kwargs: fresh.copy())
    original_drop = mcp_tool_agent._drop_session_tools

    def concurrent_drop(target, tools, names, permitted):
        if "terminal" in names:
            policy("tools:\n  disabled: [terminal]\n")
            mcp_tool_agent.refresh_agent_mcp_tools(target, preserve_prefix=True)
        return original_drop(target, tools, names, permitted)

    monkeypatch.setattr(mcp_tool_agent, "_drop_session_tools", concurrent_drop)
    assert not mcp_tool_agent.restore_agent_tool_prefix(agent, saved)
    assert agent.tools == fresh and agent.valid_tool_names == {"allowed"}
    assert agent._tool_snapshot_selection[2] == frozenset({"terminal"})
    assert agent._delegation_backend != "old"


def test_prompt_cannot_observe_names_halfway_through_snapshot_publication(policy, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock, current_thread

    import model_tools
    from agent.prompt_refresh import build_prompt_with_policy
    from tools import mcp_tool_agent
    from toolsets import tool_selection_key

    selection_written, resume_publish, reader_ready = Event(), Event(), Event()

    class PausedAgent(SimpleNamespace):
        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            if name == "_tool_snapshot_selection" and value[2]:
                selection_written.set()
                assert resume_publish.wait(5)

    class ObservedLock:
        def __init__(self):
            self.lock = Lock()

        def __enter__(self):
            if current_thread().name.startswith("prompt-reader"):
                reader_ready.set()
            return self.lock.__enter__()

        def __exit__(self, *args):
            return self.lock.__exit__(*args)

    denied = "policy_fixture_retired"
    old = {"type": "function", "function": {"name": denied, "description": "Old tool"}}
    agent = PausedAgent(tools=[old], valid_tool_names={denied},
                        _tool_snapshot_selection=tool_selection_key(None, None))
    agent._build_system_prompt = lambda _: ",".join(sorted(agent.valid_tool_names))
    monkeypatch.setattr(mcp_tool_agent, "_agent_tools_lock", ObservedLock())
    monkeypatch.setattr(model_tools, "get_tool_definitions", lambda **_kwargs: [])
    policy(f"tools:\n  disabled: [{denied}]\n")

    def render():
        result = build_prompt_with_policy(agent, None)
        reader_ready.set()  # old implementation reads the mixed fields without attempting the lock
        return result

    with ThreadPoolExecutor(1, thread_name_prefix="tool-publisher") as publisher:
        with ThreadPoolExecutor(1, thread_name_prefix="prompt-reader") as reader:
            pending_publish = publisher.submit(mcp_tool_agent.refresh_agent_mcp_tools, agent)
            try:
                assert selection_written.wait(5)
                pending_prompt = reader.submit(render)
                assert reader_ready.wait(5)
            finally:
                resume_publish.set()
            pending_publish.result(timeout=5)
            assert pending_prompt.result(timeout=5) == ("", [denied])


def test_appended_memory_and_context_engine_schemas_obey_denials(policy):
    from agent.memory_manager_tools import inject_memory_provider_tools
    from agent.agent_init import _inject_context_engine_tools
    schemas = [{'name': name, 'description': name, 'parameters': {}} for name in ['retained', 'denied']]
    policy('tools:\n  disabled: [denied]\n')
    agent = SimpleNamespace(_memory_manager=SimpleNamespace(get_all_tool_schemas=lambda: schemas),
        tools=[], valid_tool_names=set(), enabled_toolsets=None)
    assert inject_memory_provider_tools(agent) == 1
    assert agent.valid_tool_names == {'retained'}
    compressor = Mock(); compressor.get_tool_schemas.return_value = schemas
    agent.context_compressor = compressor
    _inject_context_engine_tools(agent)
    assert agent.valid_tool_names == {'retained'} and agent._context_engine_tool_names == set()


def test_remote_backend_retains_native_managed_authority(policy, monkeypatch):
    from hermes_cli import config_backend
    from tools.hosted_tool_policy import tool_refusal
    policy('tools:\n  disabled: [terminal]\n')
    monkeypatch.setattr(config_backend, 'get_config_backend', lambda: SimpleNamespace(honors_managed_config=lambda: False))
    assert tool_refusal('terminal') is None


def test_authenticated_socket_rpc_rechecks_current_managed_ceiling(policy):
    import socket
    import threading
    from tools.registry import registry
    from tools.code_execution_rpc import _rpc_server_loop
    reached = []
    registry.register(name='socket_policy_probe', toolset='policy_probe',
        schema={'name': 'socket_policy_probe', 'description': 'probe', 'parameters': {}},
        handler=lambda args, **kw: reached.append(args) or '{}')
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0)); listener.listen()
    stop = threading.Event()
    thread = threading.Thread(target=_rpc_server_loop, args=(listener, 'fixture', [], [0], 4,
        frozenset({'socket_policy_probe'}), stop, 'synthetic-token'), daemon=True)
    thread.start()
    try:
        with socket.create_connection(listener.getsockname(), timeout=5) as client:
            stream = client.makefile('rb')
            def call(token):
                request = {'tool': 'socket_policy_probe', 'args': {}, 'token': token}
                client.sendall((json.dumps(request)+'\n').encode())
                return stream.readline().decode()
            assert 'Unauthorized' in call('wrong-token')
            assert json.loads(call('synthetic-token')) == {}
            policy('tools:\n  disabled: [socket_policy_probe]\n')
            assert 'managed' in call('synthetic-token')
            policy('tools:\n  disabled: []\n')
            assert json.loads(call('synthetic-token')) == {}
            stream.close()
        assert reached == [{}, {}]
    finally:
        stop.set(); listener.close(); thread.join(timeout=5)
        registry.deregister('socket_policy_probe')
    assert not thread.is_alive()


def test_connector_search_and_describe_obey_live_policy_with_allowed_controls(policy):
    from tools import tool_search
    from tools.connectors.gateway.names import format_connector_name
    names = [format_connector_name("calendar", slug) for slug in ("allowed", "denied")]
    definitions = [{"type": "function", "function": {"name": "manage_connections", "parameters": {}}}]
    def search(queries):
        return SimpleNamespace(failure=None, payload={
            "results": [{"use_case": query["use_case"], "tools": ["allowed", "denied"]} for query in queries],
            "schemas": {slug: {"connector": "calendar", "description": "calendar event", "input_schema": {}}
                        for slug in ("allowed", "denied")}})
    requested = []
    def describe(selected):
        requested.append(selected)
        # Returning both also verifies that an unsolicited schema cannot bypass policy.
        return SimpleNamespace(failure=None, payload={"tools": {
            name: {"description": name, "parameters": {}} for name in names}})
    def found():
        return json.loads(tool_search.dispatch_tool_search({"queries": ["calendar"]},
            current_tool_defs=definitions, connector_search=search))
    assert set(found()["tools"]) == set(names)
    policy(f"tools:\n  disabled: [{names[1]}]\n")
    result = found()
    assert set(result["tools"]) == {names[0]}
    assert result["results"][0]["matches"] == [names[0]]
    result = json.loads(tool_search.dispatch_tool_describe({"names": names},
        current_tool_defs=definitions, connector_describe=describe))
    assert requested == [[names[0]]]
    assert set(result["tools"]) == {names[0]}
    assert "managed" in result["errors"][names[1]]
    policy('tools:\n  disabled: []\n')
    assert set(found()["tools"]) == set(names)


@pytest.mark.parametrize("revoke_connections", [False, True])
def test_search_and_describe_project_current_policy_after_connector_request(policy, monkeypatch, revoke_connections):
    from tools import tool_search
    from tools.connectors.gateway.names import format_connector_name

    remote = format_connector_name("calendar", "events")
    local_names = ["visible_fixture", "revoked_fixture"]
    definitions = [{"type": "function", "function": {"name": name, "description": "calendar event"}}
                   for name in ["manage_connections", *local_names]]
    config = tool_search.ToolSearchConfig.from_raw({"defer": local_names})
    monkeypatch.setattr(tool_search, "load_config_readonly", lambda: config)
    denied = [local_names[1], "manage_connections"] if revoke_connections else [local_names[1]]

    def tighten_policy():
        policy(f"tools:\n  disabled: {json.dumps(denied)}\n")

    def search(queries):
        tighten_policy()
        return SimpleNamespace(failure=None, payload={
            "results": [{"use_case": q["use_case"], "tools": ["events"]} for q in queries],
            "schemas": {"events": {"connector": "calendar", "description": "calendar event", "input_schema": {}}}})

    def describe(selected):
        assert selected == [remote]
        tighten_policy()
        return SimpleNamespace(failure=None, payload={"tools": {remote: {"description": "calendar event"}}})

    expected = {local_names[0]} if revoke_connections else {local_names[0], remote}
    search_args = {"queries": ["calendar", "unrelated phrase"]}
    describe_args = {"names": [*local_names, remote]}
    found = json.loads(tool_search.dispatch_tool_search(search_args, current_tool_defs=definitions,
                                                       connector_search=search, config=config))
    assert set(found["tools"]) == expected
    assert found["total_available"] == len(expected)
    assert sum(row["tool_count"] for row in found["results"][1]["available_sources"]) == 1

    policy("tools:\n  disabled: []\n")
    described = json.loads(tool_search.dispatch_tool_describe(describe_args, current_tool_defs=definitions,
                                                             connector_describe=describe, config=config))
    assert set(described["tools"]) == expected
    assert "managed" in described["errors"][local_names[1]]
    if revoke_connections:
        connector = Mock(side_effect=AssertionError("revoked connector request"))
        tool_search.dispatch_tool_search(search_args, current_tool_defs=definitions, connector_search=connector)
        tool_search.dispatch_tool_describe(describe_args, current_tool_defs=definitions, connector_describe=connector)
        connector.assert_not_called()


def test_nonquiet_startup_never_checks_denied_tool_requirements(policy, monkeypatch):
    import model_tools
    from agent.agent_init import _load_tools
    from tools.registry import registry

    denied, allowed = 'managed_fixture_denied_check', 'managed_fixture_allowed_check'
    deny_probe, allow_probe = Mock(return_value=False), Mock(return_value=True)
    for name, check in ((denied, deny_probe), (allowed, allow_probe)):
        registry.register(name=name, toolset='managed-fixture-checks', check_fn=check,
                          schema={'name': name, 'description': name, 'parameters': {}},
                          handler=lambda args, **kw: '{}')

    monkeypatch.setattr('hermes_cli.plugins.discover_plugins', lambda: None)
    monkeypatch.setattr(model_tools, 'get_tool_definitions', lambda **kw: [{'function': {'name': allowed}}])
    agent = SimpleNamespace(quiet_mode=False, save_trajectories=False, ephemeral_system_prompt=None,
                            _use_prompt_caching=False, _safe_print=Mock())
    policy(f'tools:\n  disabled: [{denied}]\n')
    try:
        _load_tools(agent, enabled_toolsets=None, disabled_toolsets=None)
        deny_probe.assert_not_called()
        allow_probe.assert_called()
    finally:
        registry.deregister(denied)
        registry.deregister(allowed)


def test_generated_tool_guidance_respects_partial_schema_selection(monkeypatch, policy):
    from agent.prompt_builder import KANBAN_GUIDANCE
    from agent.system_prompt import _bot_mode_parts, _tool_guidance_block
    from tools.bot_mode_dm import ensure_message_agent_tool
    from tools.bot_mode_probe import BOT_CHAT_TITLE
    from toolsets import tool_selection_key

    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_worker')
    monkeypatch.setattr('tools.bot_mode_probe.get_bot_mode_protocol_section', lambda home: 'Send with message_agent')
    monkeypatch.setattr('tools.bot_mode_probe.epoch_line', lambda home: 'EPOCH')
    monkeypatch.setattr('tools.bot_mode_dm.message_agent_authorized', lambda agent: True)
    agent = SimpleNamespace(_session_title_hint=BOT_CHAT_TITLE, tools=[],
                            valid_tool_names={'kanban_show', 'kanban_create'})
    policy('tools:\n  disabled: [message_agent]\n')
    agent._tool_snapshot_selection = tool_selection_key(None, None)
    bot, marker = _bot_mode_parts(agent)
    assert 'message_agent' not in bot and 'final response' in bot
    assert marker == 'EPOCH'
    assert not ensure_message_agent_tool(agent)
    kanban = _tool_guidance_block(agent)
    assert 'kanban_show' in kanban and 'kanban_create' in kanban
    for denied in ('kanban_heartbeat', 'kanban_complete', 'kanban_block', 'kanban_request_review'):
        assert denied not in kanban

    import re
    agent.valid_tool_names.update(re.findall(r'\bkanban_\w+', KANBAN_GUIDANCE))
    policy('tools:\n  disabled: []\n')
    agent._tool_snapshot_selection = tool_selection_key(None, None)
    # Fresh prompts are assembled before the tool is appended for the turn.
    assert 'message_agent' not in agent.valid_tool_names
    assert _bot_mode_parts(agent) == ['Send with message_agent', 'EPOCH']
    assert ensure_message_agent_tool(agent)
    assert 'message_agent' in agent.valid_tool_names
    assert _tool_guidance_block(agent) == KANBAN_GUIDANCE


def test_cli_banner_probes_only_permitted_tools_in_mixed_and_denied_toolsets(policy):
    from hermes_cli.banner import compute_toolset_availability
    from tools.registry import registry

    mixed = 'managed-fixture-mixed'
    hidden = 'managed-fixture-hidden'
    denied, denied_only, allowed = 'banner_denied', 'banner_denied_only', 'banner_allowed'
    deny_probe, deny_only_probe, allow_probe = (Mock(return_value=False) for _ in range(3))
    for name, toolset, check in (
        (denied, mixed, deny_probe), (denied_only, hidden, deny_only_probe), (allowed, mixed, allow_probe),
    ):
        registry.register(name=name, toolset=toolset, check_fn=check,
                          schema={'name': name, 'description': name, 'parameters': {}},
                          handler=lambda args, **kw: '{}')
    try:
        policy(f'tools:\n  disabled: [{denied}, {denied_only}]\n')
        report = compute_toolset_availability([mixed, hidden])
        [unavailable] = report['unavailable_toolsets']
        assert unavailable['name'] == mixed and unavailable['tools'] == [allowed]
        allow_probe.assert_called()
        deny_probe.assert_not_called()
        deny_only_probe.assert_not_called()
    finally:
        for name in (denied, denied_only, allowed):
            registry.deregister(name)

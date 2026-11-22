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

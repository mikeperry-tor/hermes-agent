"""Required sandbox policy reaches agent and dock launches without weakening defaults."""
import pytest

from tools import browser_launcher as launcher
from tools.bot_desktop import browser
from tools import browser_session_cloud


@pytest.mark.parametrize('required', [False, True])
def test_dock_and_agent_agree_on_sandbox_policy(monkeypatch, required):
    monkeypatch.setattr(browser_session_cloud, 'browser_section', lambda: {'require_sandbox': required})
    monkeypatch.setattr(launcher, 'needs_chromium_sandbox_bypass', lambda: True)
    monkeypatch.setattr(launcher, '_configured_chromium_args', list)
    agent = launcher.build_chromium_argv('chrome', '/profile', packaged=False, headed=True, sandbox_bypass=True)
    dock = browser.dock_argv('chrome', '/profile', sandbox_bypass=True)
    for argv in (agent, dock):
        assert ('--no-sandbox' in argv) is (not required)


@pytest.mark.parametrize('origin', ['extra', 'config'])
def test_required_sandbox_rejects_conflicting_flags(monkeypatch, origin):
    monkeypatch.setattr(browser_session_cloud, 'browser_section', lambda: {'require_sandbox': True})
    monkeypatch.setattr(launcher, '_configured_chromium_args', lambda: ['--no-sandbox'] if origin == 'config' else [])
    with pytest.raises(ValueError, match='conflicts'):
        launcher.build_chromium_argv('chrome', '/profile', packaged=False, headed=True,
                                     sandbox_bypass=True, extra_args=['--no-sandbox'] if origin == 'extra' else [])


def test_sandbox_chromium_preserves_repeated_arg_values_and_validates_config_key(monkeypatch):
    import subprocess
    from hermes_cli.config import _validate_config_key
    from tools import browser_session_sandbox
    from tools.environments import streams

    assert _validate_config_key('browser.require_sandbox') == (True, None)

    commands = []
    monkeypatch.setattr(
        streams,
        'run_in',
        lambda _env, argv, **_kw: (commands.append(argv), subprocess.CompletedProcess(argv, 0, b'4242 999\n', b''))[1],
    )
    monkeypatch.setattr(
        browser_session_cloud,
        'browser_section',
        lambda: {'chromium_args': ['--window-position', '0', '--window-size', '0']},
    )
    assert browser_session_sandbox._start_chromium(object(), None, '/usr/bin/chromium', '/tmp/p', {'DISPLAY': ':5'}) == (4242, '999')
    assert '--window-position 0 --window-size 0' in commands[-1][-1]

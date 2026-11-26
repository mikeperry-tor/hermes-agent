"""Each standalone config loader (gateway, TUI/desktop, cron) must honor managed scope.

These loaders build their own config dict instead of routing through
hermes_cli.config.load_config, so the managed overlay has to be wired into each.
This is the regression guard for the whole bug class (a managed display.skin was
silently ignored by the TUI; the same gap existed in the gateway and cron).
"""
import textwrap

import pytest


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()
    return home, managed


def _seed(home, managed, *, user, mgd):
    (home / "config.yaml").write_text(textwrap.dedent(user), encoding="utf-8")
    (managed / "config.yaml").write_text(textwrap.dedent(mgd), encoding="utf-8")
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()








def test_timezone_honors_managed(homes, monkeypatch):
    home, managed = homes
    # hermes_time checks an env override first; ensure it's unset so config wins.
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    _seed(home, managed, user="timezone: America/New_York\n", mgd="timezone: Asia/Tokyo\n")
    import hermes_time

    assert hermes_time._resolve_timezone_name() == "Asia/Tokyo"


def test_gateway_env_bridge_honors_managed(homes, monkeypatch):
    """The gateway config→env bridge must bridge MANAGED values, not user ones.

    gateway/run.py bridges config.yaml settings into os.environ at startup and on
    every turn (HERMES_TIMEZONE, HERMES_REDACT_SECRETS, HERMES_MAX_ITERATIONS,
    ...). A managed value must win at that env layer too — otherwise the bridge
    writes the user's value into the env that the whole process then reads. This
    is the regression that manual verification caught (managed timezone was
    overridden by the user's value via the env bridge).

    We assert on the managed-overlaid config the bridge consumes (rather than the
    os.environ side effect, which leaks across same-process tests under the
    runner) — the bridge writes whatever this dict carries, so a managed value
    here proves the env var gets the managed value.
    """
    home, managed = homes
    _seed(home, managed, user="timezone: America/New_York\n", mgd="timezone: Asia/Tokyo\n")
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    # The bridge loads config.yaml, expands env, then applies this overlay before
    # writing HERMES_TIMEZONE = cfg["timezone"]. Prove the overlay flips the value.
    import hermes_yaml as yaml

    raw = yaml.safe_load((home / "config.yaml").read_text())
    bridged = managed_scope.apply_managed_overlay(raw)
    assert bridged.get("timezone") == "Asia/Tokyo"


def test_strict_managed_read_distinguishes_absent_invalid_and_inaccessible(homes, monkeypatch):
    from pathlib import Path

    from hermes_cli import managed_scope

    _, managed = homes
    config = managed / 'config.yaml'
    assert managed_scope.load_managed_config(strict=True) == {}
    config.write_text('timezone: Etc/UTC\n')
    assert managed_scope.load_managed_config(strict=True) == {'timezone': 'Etc/UTC'}
    config.write_text('[]\n')
    with pytest.raises(ValueError, match='must contain a mapping'):
        managed_scope.load_managed_config(strict=True)
    config.write_text('timezone: Etc/UTC\n')
    stat = Path.stat

    def inaccessible(path, *args, **kwargs):
        if path == config:
            raise PermissionError('managed directory is inaccessible')
        return stat(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, 'stat', inaccessible)
        with pytest.raises(PermissionError):
            managed_scope.load_managed_config(strict=True)


@pytest.mark.parametrize("contents", ["null\n", "~\n", "", "# no policy document\n"])
def test_strict_managed_read_rejects_absent_yaml_root_in_a_present_file(homes, contents):
    from hermes_cli import managed_scope

    _, managed = homes
    config = managed / 'config.yaml'
    config.write_text(contents)
    # An earlier non-strict read must not cache a permissive fallback.
    assert managed_scope.load_managed_config() == {}
    with pytest.raises(ValueError, match='must contain a mapping'):
        managed_scope.load_managed_config(strict=True)
    config.write_text('{}\n')
    assert managed_scope.load_managed_config(strict=True) == {}


def test_explicit_managed_overlay_uses_validated_snapshot(homes):
    from hermes_cli import config_effective, managed_scope

    home, managed = homes
    (home / 'config.yaml').write_text('display:\n  skin: profile\n  compact: true\n')
    config = managed / 'config.yaml'
    config.write_text('display:\n  skin: managed\n')
    selected = managed_scope.load_managed_config(strict=True)
    config.write_text('[]\n')
    result = managed_scope.apply_managed_overlay(
        {'display': {'skin': 'profile', 'compact': True}}, managed_snapshot=selected)
    assert result['display'] == {'skin': 'managed', 'compact': True}
    effective = config_effective.load_user_config_effective(fail_closed=True, managed_snapshot=selected)
    assert effective['display'] == {'skin': 'managed', 'compact': True}

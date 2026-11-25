"""Model speech uses the profile cache while internal playback chooses its destination."""

import io
import json
from pathlib import Path
import wave

import pytest

from hermes_constants import get_hermes_dir, reset_hermes_home_override, set_hermes_home_override
from tools import tts_tool


@pytest.fixture
def controlled_speech(monkeypatch, tmp_path):
    audio = io.BytesIO()
    with wave.open(audio, "wb") as clip:
        clip.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        clip.writeframes(b"\0\0" * 80)
    contents = audio.getvalue()

    def synthesize(_provider, _text, path, _config, _instructions):
        Path(path).write_bytes(contents)

    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "edge"})
    monkeypatch.setattr(tts_tool, "_select_builtin_engine", lambda provider: (provider, None))
    monkeypatch.setattr(tts_tool, "_synthesize_builtin", synthesize)
    monkeypatch.setattr(tts_tool, "_session_platform", lambda: ("", False))
    profile = tmp_path / "profiles" / "research"
    profile.mkdir(parents=True)
    scope = set_hermes_home_override(profile)
    try:
        yield profile, contents
    finally:
        reset_hermes_home_override(scope)


@pytest.mark.parametrize("legacy", [False, True])
def test_model_speech_dispatch_uses_cache_and_delivers_audio(controlled_speech, tmp_path, legacy):
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.run import _collect_auto_append_media_tags
    from tools.registry import registry

    profile, contents = controlled_speech
    cache = profile / ("audio_cache" if legacy else "cache/audio")
    if legacy:
        cache.mkdir()
        (cache / "prior.wav").write_bytes(contents)
    destination = tmp_path / "elsewhere.mp3"
    destination.write_bytes(b"preserve this file")
    entry = registry.get_entry("text_to_speech")
    assert "output_path" not in entry.schema["parameters"]["properties"]
    args = {"text": "Hello from a named profile.", "output_path": str(destination)}
    result = registry.dispatch("text_to_speech", args)
    payload = json.loads(result)
    assert payload["success"], payload
    generated = Path(payload["file_path"])
    assert generated.parent == cache
    assert generated.read_bytes() == contents
    assert destination.read_bytes() == b"preserve this file"

    call_id = "speech"
    history = [
        {"role": "assistant", "tool_calls": [{
            "id": call_id, "function": {"name": "text_to_speech", "arguments": json.dumps(args)},
        }]},
        {"role": "tool", "tool_call_id": call_id, "content": result},
    ]
    tags, as_voice = _collect_auto_append_media_tags(history)
    media, _ = BasePlatformAdapter.extract_media("\n".join(tags))
    assert not as_voice and media == [(str(generated), False)]
    assert Path(media[0][0]).read_bytes() == contents


@pytest.mark.real_audio_playback
def test_internal_playback_uses_and_cleans_its_explicit_destination(
    controlled_speech, tmp_path, monkeypatch,
):
    from hermes_cli import voice

    profile, contents = controlled_speech
    monkeypatch.setattr(voice.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(voice, "_speak_streaming", lambda *_: False)
    played = []

    def play(path):
        path = Path(path)
        played.append((path, path.read_bytes()))

    monkeypatch.setattr(voice, "play_audio_file", play)
    voice.speak_text("Hello from internal voice playback.")
    [(destination, heard)] = played
    assert destination.parent == tmp_path / "hermes_voice" and heard == contents
    assert not destination.exists()
    assert not list(get_hermes_dir("cache/audio", "audio_cache", home=profile).glob("tts_*"))

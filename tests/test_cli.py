import json

from typer.testing import CliRunner
from voice_studio_cli.main import app


def test_cloud_start_uses_environment_configuration(monkeypatch, tmp_path):
    from voice_studio_service import cloud

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VOICE_DAYTONA_SECRET_NAMES", '{"VOICE_DATABASE_URL":"private-db"}')
    monkeypatch.setenv("VOICE_DAYTONA_CONTROLLER_SNAPSHOT", "pinned-controller")
    seen = []
    monkeypatch.setattr(cloud, "launch", lambda *args: seen.append(args) or {"started": True})
    result = CliRunner().invoke(app, ["cloud", "start"])
    assert result.exit_code == 0, result.output
    assert seen == [("pinned-controller", {"VOICE_DATABASE_URL": "private-db"}, "voice-studio")]
    assert json.loads(result.stdout)["started"]


def test_cloud_start_explicit_mapping_overrides_environment(monkeypatch, tmp_path):
    from voice_studio_service import cloud

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VOICE_DAYTONA_SECRET_NAMES", '{"VOICE_DATABASE_URL":"private-db"}')
    seen = []
    monkeypatch.setattr(cloud, "launch", lambda *args: seen.append(args) or {})
    result = CliRunner().invoke(
        app, ["cloud", "start", "--snapshot", "selected", "--secret-names", "{}"]
    )
    assert result.exit_code == 0, result.output
    assert seen == [("selected", {}, "voice-studio")]


def test_generate_accepts_documented_options_without_sending_unauthenticated_requests(monkeypatch):
    monkeypatch.delenv("VOICE_API_KEY", raising=False)
    monkeypatch.delenv("VOICE_LOCAL_TOKEN", raising=False)
    result = CliRunner().invoke(app, ["generate", "--text", "Hello", "--voice-id", "version"])
    assert result.exit_code == 2
    assert "Set VOICE_API_KEY or VOICE_LOCAL_TOKEN" in result.output


def test_local_generates_distinct_256_bit_keys_and_explains_expiry(monkeypatch, tmp_path):
    import re

    from voice_studio_cli import main
    from voice_studio_core.config import Settings

    captured = []
    monkeypatch.setattr(main, "settings", lambda: Settings(_env_file=None, data_dir=tmp_path))
    monkeypatch.setattr(main, "serve_app", captured.append)
    for _ in range(2):
        result = CliRunner().invoke(app, ["local"])
        assert result.exit_code == 0, result.output
        assert "at most 480 minutes" in result.output
        assert "does not renew it" in result.output
    tokens = [c.local_token.get_secret_value() for c in captured]
    assert tokens[0] != tokens[1]
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{43}", token) for token in tokens)

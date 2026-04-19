from datetime import datetime

import cli


def test_save_conversation_writes_under_hermes_home_sessions(tmp_path, monkeypatch, capsys):
    hermes_home = tmp_path / "hermes-home"
    vault_root = tmp_path / "vault-root"
    vault_root.mkdir()
    monkeypatch.chdir(vault_root)
    monkeypatch.setattr(cli, "get_hermes_home", lambda: hermes_home)

    agent = cli.HermesCLI.__new__(cli.HermesCLI)
    agent.conversation_history = [{"role": "user", "content": "hello"}]
    agent.model = "test-model"
    agent.session_start = datetime(2026, 4, 12, 17, 20, 26)

    agent.save_conversation()

    expected = hermes_home / "sessions" / "hermes_conversation_20260412_172026.json"
    assert expected.exists()
    assert not (vault_root / expected.name).exists()

    out = capsys.readouterr().out
    assert str(expected) in out

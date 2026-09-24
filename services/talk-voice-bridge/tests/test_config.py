import config


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_BASE_URL", "https://nc.example.org/")
    monkeypatch.setenv("NEXTCLOUD_VOICE_APP_PASSWORD", "secret")
    cfg = config.load()
    assert cfg.nextcloud_base_url == "https://nc.example.org"   # trailing slash stripped
    assert cfg.talk_user == "ai-agent"                          # default
    assert cfg.voice_app_password == "secret"
    assert cfg.audio_rate == 24000
    assert cfg.port == 3338
    assert cfg.home_room == ""              # outbound report-back defaults
    assert cfg.telegram_bot_token == ""


def test_config_reads_outbound_report_env(monkeypatch):
    monkeypatch.setenv("NEXTCLOUD_TALK_HOME_CONVERSATION", "room8tok")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-abc")
    cfg = config.load()
    assert cfg.home_room == "room8tok"
    assert cfg.telegram_bot_token == "bot-abc"

"""config.py (regions.yaml loading, secret resolution) and regions.py
(subscribe-topic construction, the region allow-list check)."""

from pathlib import Path

from meshdb_common.config import load_regions_config, resolve_secret
from meshdb_common.decode import resolve_psk
from meshdb_common.regions import (
    build_channel_psks,
    build_subscribe_topics,
    is_region_allowed,
)

SAMPLE_PATH = Path(__file__).parent.parent / "config" / "regions.yaml.sample"


def test_load_regions_config_expands_env_vars(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "mqtt.example.org")
    cfg = load_regions_config(SAMPLE_PATH)

    assert cfg.mqtt.brokers[0].host == "mqtt.example.org"
    assert cfg.mqtt.channels[0].name == "LongFast"
    # Shipped empty by design — there's no universally-safe default region.
    assert cfg.allowed_regions == ()


def test_load_regions_config_with_allowed_regions(tmp_path, monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "mqtt.example.org")
    regions_yaml = tmp_path / "regions.yaml"
    regions_yaml.write_text(SAMPLE_PATH.read_text().replace("allowed_regions: []", "allowed_regions: [MY_919, MY_433]"))

    cfg = load_regions_config(regions_yaml)
    assert cfg.allowed_regions == ("MY_919", "MY_433")

    topics = build_subscribe_topics(cfg)
    assert "msh/MY_919/2/e/#" in topics
    assert "msh/MY_919/2/c/#" in topics
    assert "msh/MY_433/2/e/#" in topics
    assert len(topics) == 4

    assert is_region_allowed("MY_919", cfg) is True
    assert is_region_allowed("EU_868", cfg) is False


def test_load_regions_config_resolves_channel_psks(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "mqtt.example.org")
    cfg = load_regions_config(SAMPLE_PATH)

    psks = build_channel_psks(cfg)
    assert psks["LongFast"] == resolve_psk("AQ==")


def test_resolve_secret_prefers_file_over_plain_env(tmp_path, monkeypatch):
    secret_file = tmp_path / "mqtt_password.txt"
    secret_file.write_text("from-file\n")

    monkeypatch.setenv("MQTT_PASSWORD", "from-plain-env")
    monkeypatch.setenv("MQTT_PASSWORD_FILE", str(secret_file))
    assert resolve_secret("MQTT_PASSWORD") == "from-file"

    monkeypatch.delenv("MQTT_PASSWORD_FILE")
    assert resolve_secret("MQTT_PASSWORD") == "from-plain-env"

    monkeypatch.delenv("MQTT_PASSWORD")
    assert resolve_secret("MQTT_PASSWORD") is None

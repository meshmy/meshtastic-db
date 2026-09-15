"""Region opt-in: MQTT topic construction and the write-path allow-list
check. Nothing is ingested for a region not in config/regions.yaml's
allowed_regions, even if traffic for it is visible on a subscribed broker."""

from __future__ import annotations

from .config import RegionsConfig
from .decode import resolve_psk


def build_subscribe_topics(cfg: RegionsConfig) -> list[str]:
    """One subscription per allowed region, covering both the current ('e')
    and legacy ('c') topic versions — the opt-in is enforced at the
    subscription itself, not via a wildcard-then-filter."""
    return [
        cfg.mqtt.topic_template.format(region=region, version=version)
        for region in cfg.allowed_regions
        for version in ("e", "c")
    ]


def is_region_allowed(region: str, cfg: RegionsConfig) -> bool:
    """Defense-in-depth check for the write path: verifies a packet's
    tagged region against the allow-list even though the MQTT subscription
    is already scoped to it, to catch a misconfigured gateway-agent."""
    return region in cfg.allowed_regions


def build_channel_psks(cfg: RegionsConfig) -> dict[str, bytes]:
    """Resolve every configured channel's PSK to raw key bytes, keyed by
    channel name, ready for decode.decode_service_envelope."""
    return {channel.name: resolve_psk(channel.psk_base64) for channel in cfg.mqtt.channels}

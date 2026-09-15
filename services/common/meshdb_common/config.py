"""Loading config/regions.yaml and resolving Compose-secret credentials."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


def resolve_secret(env_var: str) -> str | None:
    """Resolve a credential from `<ENV_VAR>_FILE` (a Docker Compose secret
    mount, preferred — keeps the value out of `docker inspect`/the process
    environment) or a plain `<ENV_VAR>` if only that is set."""
    file_path = os.environ.get(f"{env_var}_FILE")
    if file_path:
        return Path(file_path).read_text().strip()
    return os.environ.get(env_var)


@dataclass(frozen=True)
class MqttBroker:
    host: str
    port: int
    tls: bool


@dataclass(frozen=True)
class MqttChannel:
    name: str
    psk_base64: str


@dataclass(frozen=True)
class MqttConfig:
    topic_template: str
    brokers: tuple[MqttBroker, ...]
    channels: tuple[MqttChannel, ...]


@dataclass(frozen=True)
class TcpNode:
    host: str
    region: str


@dataclass(frozen=True)
class GatewayAgentConfig:
    region: str | None
    connection: dict


@dataclass(frozen=True)
class RegionsConfig:
    allowed_regions: tuple[str, ...]
    mqtt: MqttConfig
    tcp_nodes: tuple[TcpNode, ...]
    gateway_agent: GatewayAgentConfig


def load_regions_config(path: str | Path) -> RegionsConfig:
    """Load config/regions.yaml, expanding `${VAR}` references (e.g.
    `mqtt.brokers[].host: ${MQTT_HOST}`) against the process environment —
    the same variables docker-compose.yml passes into each ingestion
    service's container."""
    text = os.path.expandvars(Path(path).read_text())
    raw = yaml.safe_load(text)

    mqtt_raw = raw["mqtt"]
    gateway_agent_raw = raw["gateway_agent"]

    return RegionsConfig(
        allowed_regions=tuple(raw.get("allowed_regions") or ()),
        mqtt=MqttConfig(
            topic_template=mqtt_raw["topic_template"],
            brokers=tuple(
                MqttBroker(host=b["host"], port=b["port"], tls=b.get("tls", True)) for b in mqtt_raw["brokers"]
            ),
            channels=tuple(
                MqttChannel(name=c["name"], psk_base64=c["psk_base64"]) for c in mqtt_raw["channels"]
            ),
        ),
        tcp_nodes=tuple(TcpNode(host=n["host"], region=n["region"]) for n in raw.get("tcp_nodes") or ()),
        gateway_agent=GatewayAgentConfig(
            region=gateway_agent_raw.get("region"),
            connection=gateway_agent_raw.get("connection") or {},
        ),
    )

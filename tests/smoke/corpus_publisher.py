"""One-shot publisher for bin/smoke-test.sh: builds a small, plaintext
synthetic corpus (one Telemetry, one Position, one NodeInfo packet -- the
three envelope shapes that land in `metric`, `node_position_history`, and
`node_identity` respectively) and publishes it to the disposable `mosquitto`
broker docker-compose.test.yml adds, on topics shaped like real Meshtastic
MQTT traffic. Encryption/decrypt correctness is already covered by
tests/test_mqtt_ingest_replay.py; this script's only job is to put real
bytes on a real broker so the actual `docker compose`-built mqtt-ingest
container has something to decode.

Run inside the mqtt-ingest image (has meshdb_common + paho-mqtt already
installed) via `docker compose run --rm --no-deps mqtt-ingest python
/tests/smoke/corpus_publisher.py ...` -- see bin/smoke-test.sh.
"""

from __future__ import annotations

import argparse
import time

import meshdb_common  # noqa: F401  (import side effect: puts generated/ on sys.path)
from meshtastic import mesh_pb2, mqtt_pb2, portnums_pb2, telemetry_pb2
from paho.mqtt.client import CallbackAPIVersion, Client

NODE_ID = 0x5A0C111E


def _data(portnum: int, payload_bytes: bytes) -> mesh_pb2.Data:
    return mesh_pb2.Data(portnum=portnum, payload=payload_bytes)


def _packet(packet_id: int, data: mesh_pb2.Data) -> mesh_pb2.MeshPacket:
    packet = mesh_pb2.MeshPacket(id=packet_id)
    setattr(packet, "from", NODE_ID)
    packet.decoded.CopyFrom(data)
    return packet


def build_packets() -> list[mesh_pb2.MeshPacket]:
    telemetry = telemetry_pb2.Telemetry(device_metrics=telemetry_pb2.DeviceMetrics(battery_level=88))
    position = mesh_pb2.Position(latitude_i=int(3.1390 * 1e7), longitude_i=int(101.6869 * 1e7), altitude=42)
    user = mesh_pb2.User(long_name="Smoke Test Node", short_name="SMOK")
    return [
        _packet(1001, _data(portnums_pb2.PortNum.TELEMETRY_APP, telemetry.SerializeToString())),
        _packet(1002, _data(portnums_pb2.PortNum.POSITION_APP, position.SerializeToString())),
        _packet(1003, _data(portnums_pb2.PortNum.NODEINFO_APP, user.SerializeToString())),
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--region", required=True)
    parser.add_argument("--channel", default="LongFast")
    args = parser.parse_args()

    packets = build_packets()
    client = Client(CallbackAPIVersion.VERSION2)
    client.connect(args.host, args.port)
    client.loop_start()
    try:
        for packet in packets:
            envelope = mqtt_pb2.ServiceEnvelope(packet=packet, channel_id=args.channel, gateway_id=f"!{NODE_ID:08x}")
            topic = f"msh/{args.region}/2/e/{args.channel}/!{NODE_ID:08x}"
            info = client.publish(topic, envelope.SerializeToString(), qos=1)
            info.wait_for_publish(timeout=10)
        time.sleep(1)  # let the broker flush the last publish before disconnecting
    finally:
        client.loop_stop()
        client.disconnect()

    print(f"published {len(packets)} packets to {args.host}:{args.port} for region {args.region}")


if __name__ == "__main__":
    main()

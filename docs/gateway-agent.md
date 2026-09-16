# Gateway-agent deployment notes

`gateway-agent` is the only ingestion service that needs physical proximity
to a radio — BLE or USB-serial. It runs separately from the main
`docker-compose.yml` stack, on whatever host has that access (a Raspberry
Pi next to the node, a laptop with the radio plugged in, etc.), and forwards
decoded packets to a centrally-reachable `ingest-api` instance over HTTP
rather than writing to Postgres directly.

## Prerequisites

- A checkout of this repo on the agent host — `docker-compose.gateway-agent.yml`
  builds `services/gateway-agent/Dockerfile` with a build context that also
  needs `services/common` alongside it, so a single copied file isn't
  enough; clone (or otherwise transfer) the whole repo.
- A running, reachable `ingest-api` instance: the central stack's
  `docker compose --profile extra-sources up -d ingest-api` (see the main
  [README](../README.md)), reachable from the agent host at whatever
  `ingest_api.url` will point at.
- The same `INGEST_API_TOKEN` value used to start that `ingest-api`
  instance, copied into `secrets/ingest_api_token.txt` on the agent host —
  the bearer token is shared between the two sides deliberately; a mismatch
  fails every request with 401.

## One-time setup on the agent host

```bash
cp services/gateway-agent/config.example.yaml config.yaml
# edit config.yaml: region, connection (serial port or BLE address), ingest_api.url

cp secrets/ingest_api_token.txt.sample secrets/ingest_api_token.txt
# overwrite with the real token the central ingest-api was started with

docker compose -f docker-compose.gateway-agent.yml up -d
```

`config.yaml` and `secrets/ingest_api_token.txt` are both gitignored, the
same pattern as the main stack's `.env`/`config/regions.yaml`/`secrets/*` —
copy from the committed `.sample`/`.example` and fill in real values
locally, never commit the real files.

## Serial vs. BLE

- **Serial** (`connection: {type: serial, port: /dev/ttyUSB0}`): the
  supported, tested path. `docker-compose.gateway-agent.yml` mounts
  `/dev/ttyUSB0` into the container by default — edit the `devices:` entry
  if the actual device node differs (check with `ls /dev/tty*` after
  plugging the radio in, or `dmesg | tail` right after plugging it in).
- **BLE** (`connection: {type: ble, ble_address: AA:BB:CC:DD:EE:FF}`): uses
  `bleak` directly against Meshtastic's documented GATT characteristics,
  bypassing Docker's usual container isolation concerns for Bluetooth —
  typically needs `network_mode: host` plus a D-Bus socket mount for BlueZ,
  neither of which is wired into `docker-compose.gateway-agent.yml` yet.
  **Not validated against real BLE hardware** — only the serial transport
  and everything downstream of "raw `FromRadio` bytes off the wire" (decode,
  batching, HTTP forwarding, WAL spillover) has an automated test. If BLE
  under Docker doesn't cooperate on a given host, run the agent directly on
  host Python instead:

  ```bash
  pip install -e services/common
  pip install -r services/gateway-agent/requirements.txt
  python services/gateway-agent/main.py
  ```

  (reading `config.yaml`/env vars the same way the container does).

## What the agent does not depend on

Deliberately **not** a dependency: the `meshtastic` PyPI package. It was
expected to be needed for BLE/serial transport, but a compatibility check
during implementation found that package's bundled protobuf classes live at
the exact same import path (`meshtastic.mesh_pb2`, etc.) this repo's own
generated classes use — installing both in one interpreter would silently
make decoding fall back to the pip package's (possibly stale) schema
instead of this repo's freshly-regenerated one, defeating the entire
zero-code-change-for-new-fields design. Serial talks to the radio directly
via `pyserial` using this repo's own wire-framing and generated protobuf
classes (the same framing `tcp-poller` already proves against the TCP local
API, since Meshtastic documents serial and TCP as sharing it); BLE uses
`bleak` against Meshtastic's documented GATT UUIDs directly. If a future
change ever considers adding the `meshtastic` package as a dependency here,
re-read `services/gateway-agent/main.py`'s module docstring first — the
reasoning above.

## WAL spillover

`wal.path` (default `/var/lib/gateway-agent/wal.jsonl`, on a named volume so
it survives a container restart) is an append-only local file the agent
spills a batch to if a POST to `ingest_api.url` fails — a network partition,
or `ingest-api` itself restarting. Every send attempt first replays any
pending WAL backlog, in order, before sending the current batch; a failure
during replay re-spills the backlog rather than losing it. Nothing needs to
be done manually to recover from a transient outage — the agent catches up
on its own once `ingest-api` is reachable again. A WAL file that grows
without bound is the signal something is wrong (agent can't reach
`ingest-api` at all) rather than proof the mechanism itself is broken.

## Multiple agents

Each gateway-agent instance is independent and stateless relative to every
other one — all POST to the same central `ingest-api`, each tagging its own
packets with its own configured `region`. There's no coordination needed
between agents; adding a second radio/host is just repeating the one-time
setup above with its own `config.yaml`.

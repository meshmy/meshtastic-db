# Region configuration runbook

`config/regions.yaml` (copied from `config/regions.yaml.sample`, gitignored)
controls which Meshtastic regions this deployment ingests, and how it
reaches them. Nothing is ingested for a region not listed in
`allowed_regions` — even if traffic for it is visible on a subscribed
broker — so a new region needs an explicit edit here, not just a new device
showing up.

## Adding a region

1. Add the region code to `allowed_regions` (e.g. `[MY_919, MY_433]`).
   Meshtastic region codes come from upstream's `config.lora.region` enum
   (`US`, `EU_868`, `MY_919`, `MY_433`, ...); use whatever code the nodes in
   that region are actually configured with.
2. If those nodes publish to a broker not already listed, add it under
   `mqtt.brokers` (`host`, `port`, `tls`).
3. If the region's traffic uses a non-default channel PSK, add a
   `mqtt.channels` entry named after the channel (see "Channel PSKs" below).
4. Restart `mqtt-ingest` (`docker compose restart mqtt-ingest`) to pick up
   the edited file — it's read once at process start, not watched for
   changes.

`mqtt-ingest` subscribes per allowed region individually
(`msh/<region>/2/e/#` and the legacy `.../2/c/#`, per `topic_template`), not
a wildcard-then-filter — the region opt-in is enforced at the subscription
itself, not just downstream. `config.py`'s `handle_message` also re-checks
the tagged region against `allowed_regions` as defense in depth against a
misconfigured gateway-agent tagging traffic with an unlisted region.

## Channel PSKs

Meshtastic encrypts payloads per channel with AES-256-CTR, keyed by that
channel's PSK. `mqtt.channels` maps a channel name (or the wildcard `"*"`)
to a base64 PSK:

```yaml
mqtt:
  channels:
    - name: "*"
      psk_base64: "AQ=="
```

`"AQ=="` is Meshtastic's single-byte "simple key" sentinel, expanded
internally to the fixed default channel key — the key every
default-modem-preset channel (`LongFast`, `ShortFast`, `MediumSlow`, ...)
uses unless someone has deliberately set a custom PSK. Because that default
key is the same regardless of channel name, one `"*"` entry decrypts all of
them; there's no need to enumerate preset names individually. Add a
channel by its real name only when it uses a genuinely custom, non-default
PSK — an exact `channel_id` match always takes priority over `"*"` when
both are present. A channel using an unknown custom PSK will still be
tried against `"*"` and fail to parse as a valid message; it's recorded
with `packet_type = 'UNDECRYPTABLE'` (transport metadata kept, no metric
rows) rather than erroring the whole batch.

## TCP-polled nodes

`tcp_nodes` lists specific `host:region` pairs for `tcp-poller` to connect
to directly over the Meshtastic local API (default port 4403) instead of
via MQTT — useful for a node reachable on the local network that isn't (or
shouldn't be) relaying to a broker. `tcp-poller` doesn't start by default;
bring it up with `docker compose --profile extra-sources up -d tcp-poller`
once `tcp_nodes` is non-empty.

## Gateway-agent region

`gateway_agent.region` in `config/regions.yaml` is not used by any central
service — it's a documentation placeholder next to the rest of the region
config. Each actual gateway-agent instance is configured separately, one
static region per instance, in its own `config.yaml` (see
[gateway-agent.md](gateway-agent.md)) — a single agent has physical access
to one radio, and that radio is only ever in one region at a time.

## Testing a region config

`bin/smoke-test.sh` / `make smoke-test` exercises the whole decode-to-Postgres
path end to end against a disposable broker (region `SMOKE`, not a real
one) — useful for confirming the mqtt-ingest container itself works, but it
doesn't validate a specific region's real broker connectivity or PSKs.

To check a real region's config once traffic is flowing, query directly:

```sql
SELECT region, count(*), max(time) FROM metric GROUP BY region;
```

`docker compose logs -f mqtt-ingest` only logs a `dropping message on
unrecognized/disallowed topic` warning (a topic/region mismatch) or a
`failed to decode message` exception with traceback (a malformed payload) —
it does **not** currently log anything for a packet that decrypts to
garbage with the wrong PSK: `decode_service_envelope` returns a
`packet_type = 'UNDECRYPTABLE'` envelope with no fields, which then
produces zero rows anywhere (not `metric`, not a separate table — there is
no side channel for it), silently. A region present in `allowed_regions`
with zero rows after several minutes and no logged warnings/errors most
likely means a wrong PSK for that region's channel, not a broker/topic
problem (which would show up as a logged drop or decode exception
instead). Confirming a PSK guess is currently a manual affair: temporarily
run `meshdb_common.decode.decode_service_envelope` against a captured
payload in a Python shell with candidate PSKs and see which one parses.

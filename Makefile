.PHONY: up down proto-gen test test-integration lint retune-retention

up:
	docker compose up -d timescaledb grafana

down:
	docker compose down

proto-gen:
	python -m grpc_tools.protoc -I vendor/protobufs \
	  --python_out=services/common/meshdb_common/generated \
	  --pyi_out=services/common/meshdb_common/generated \
	  $$(find vendor/protobufs/meshtastic -name '*.proto')

test:
	pytest tests/

# Needs Docker (via colima on macOS — see AGENTS.md); spins up a disposable
# TimescaleDB container per test, not covered by the default `make test`.
# TESTCONTAINERS_RYUK_DISABLED works around colima's Docker socket not being
# bind-mountable into the Ryuk reaper container (see AGENTS.md); container
# cleanup on the normal exit path is unaffected, since testcontainers still
# stops the container itself, just without the crash-safety-net reaper.
test-integration:
	TESTCONTAINERS_RYUK_DISABLED=true pytest tests/ -m integration

lint:
	ruff check services/ tests/

retune-retention:
	@echo "retune-retention: not yet implemented"
	@exit 1

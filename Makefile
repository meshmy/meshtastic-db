.PHONY: up down proto-gen test lint retune-retention

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

lint:
	ruff check services/ tests/

retune-retention:
	@echo "retune-retention: not yet implemented — needs the schema from Phase 1"
	@exit 1

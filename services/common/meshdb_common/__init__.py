import sys as _sys
from pathlib import Path as _Path

# protoc's generated imports (e.g. `from meshtastic import mesh_pb2`) are
# absolute, assuming generated/ itself is on sys.path rather than being a
# regular importable subpackage of meshdb_common.
_GENERATED_DIR = _Path(__file__).parent / "generated"
if str(_GENERATED_DIR) not in _sys.path:
    _sys.path.insert(0, str(_GENERATED_DIR))

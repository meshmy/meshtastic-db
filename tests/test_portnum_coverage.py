"""Every value in portnums_pb2.PortNum must be classified as either handled
(PORTNUM_MESSAGE_MAP) or deliberately unhandled (KNOWN_UNHANDLED_PORTNUMS).
This fails loudly the moment a vendor/protobufs submodule bump introduces a
new portnum, instead of silently dropping its data."""

from meshdb_common.decode import KNOWN_UNHANDLED_PORTNUMS, PORTNUM_MESSAGE_MAP
from meshtastic import portnums_pb2


def test_every_portnum_is_mapped_or_allow_listed():
    all_values = {v.number for v in portnums_pb2.PortNum.DESCRIPTOR.values}
    classified = set(PORTNUM_MESSAGE_MAP) | KNOWN_UNHANDLED_PORTNUMS

    missing = all_values - classified
    assert not missing, f"unclassified PortNum values: {sorted(missing)}"


def test_no_portnum_is_both_mapped_and_allow_listed():
    overlap = set(PORTNUM_MESSAGE_MAP) & KNOWN_UNHANDLED_PORTNUMS
    assert not overlap

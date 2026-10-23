from pathlib import Path

import pandas as pd
import pytest

from mavric.rfid.protocol import (
    KIND_BAD,
    KIND_BOOT,
    KIND_NONE,
    KIND_SYNC,
    KIND_TAG,
    WRAP,
    MicrosUnwrapper,
    em4100_checksum_ok,
    parse_line,
)

EXAMPLE = Path(r"C:\Code\multirfid-standalone\examples\rfid_log_30062026_033938.csv")


def test_parse_tag_line_with_crlf():
    ln = parse_line(b"4838680,0A000000010B,1\r\n")
    assert ln.kind == KIND_TAG
    assert ln.micros == 4838680
    assert ln.tag == "0A000000010B"
    assert ln.reader == 1
    assert ln.checksum_ok is True


@pytest.mark.parametrize(
    "line,kind",
    [("125008,NONE,0", KIND_NONE), ("99,SYNC,0", KIND_SYNC), ("0,BOOT,0", KIND_BOOT)],
)
def test_parse_special(line, kind):
    assert parse_line(line).kind == kind


@pytest.mark.parametrize(
    "line",
    ["", "garbage", "12,ABC", "x,0A000000010B,1", "12,0A000000010B,9", "-5,NONE,0", f"{WRAP},NONE,0"],
)
def test_parse_bad(line):
    assert parse_line(line).kind == KIND_BAD


def test_checksum():
    assert em4100_checksum_ok("0A000000010B")
    assert not em4100_checksum_ok("0A000000010C")
    assert not em4100_checksum_ok("0A00000001")
    assert not em4100_checksum_ok("0A00000001ZZ")


def test_unwrap_wrap_and_reset():
    u = MicrosUnwrapper()
    assert u(WRAP - 100_000) == (WRAP - 100_000, 0)
    assert u(25_000) == (WRAP + 25_000, 0)  # wrap -> continues
    assert u(5_000_000) == (WRAP + 5_000_000, 0)
    assert u(1_000) == (1_000, 1)  # far from wrap boundary -> reset, new segment
    assert u(2_000, boot=True) == (2_000, 2)


@pytest.mark.skipif(not EXAMPLE.exists(), reason="MultiRFID example log not available")
def test_example_log_checksums():
    df = pd.read_csv(EXAMPLE, dtype={"TagID": str})
    ard = df[df["Port"] == "COM4"]
    tags = ard.loc[ard["TagID"] != "NONE", "TagID"].unique()
    assert len(tags) >= 1
    assert all(em4100_checksum_ok(t) for t in tags)

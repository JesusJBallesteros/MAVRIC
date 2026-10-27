"""Every test runs against a fresh copy of config/examples, never the rig's own config/ files."""

import pytest

from mavric.config import init_config


@pytest.fixture(autouse=True)
def example_rig(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("rig")
    init_config(root)
    monkeypatch.setenv("MAVRIC_ROOT", str(root))
    monkeypatch.delenv("VRFID_ROOT", raising=False)
    return root

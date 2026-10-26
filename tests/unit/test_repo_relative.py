import os
from pathlib import Path

from qaplatform.ingest.js_parser import build_inventory
from tests.unit.fixtures_app import write_app


def test_build_inventory_with_relative_root(tmp_path, monkeypatch):
    root = write_app(tmp_path / "app")
    monkeypatch.chdir(tmp_path)
    inv = build_inventory(Path("app"), "react")
    assert inv.page_files["/home"].endswith("Home.jsx")
    assert len(inv.elements) > 5

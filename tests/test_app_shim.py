"""Tests for the ``app.py`` App Service shim -- the deliberately stdlib-only bootstrap entry point."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

import curator.app
import curator.telemetry as telemetry

_APP_SHIM_PATH = pathlib.Path(__file__).resolve().parent.parent / "app.py"


def _load_app_shim(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(curator.app, "create_app", lambda: object())
    spec = importlib.util.spec_from_file_location("curator_app_shim", _APP_SHIM_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_failure_reporter_targets_the_same_data_stream_as_runtime_logging(monkeypatch: pytest.MonkeyPatch):
    shim = _load_app_shim(monkeypatch)

    assert shim._ES_DATA_STREAM == telemetry._ES_DATA_STREAM


def test_shim_does_not_import_curator_or_elasticsearch_at_module_scope():
    source = _APP_SHIM_PATH.read_text(encoding="utf-8")
    import_lines = [line.strip() for line in source.splitlines() if line.startswith(("import ", "from "))]

    assert not [line for line in import_lines if "elasticsearch" in line]
    assert not [line for line in import_lines if line.startswith(("import curator", "from curator"))]

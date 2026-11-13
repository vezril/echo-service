"""The scaffold's own guarantees: the package imports and the entry point is honest."""

from __future__ import annotations

import importlib

import pytest

import echo
from echo.__main__ import main

MODULES = ["echo.follow", "echo.fetch", "echo.ingest", "echo.notify"]


def test_version_is_exposed() -> None:
    assert echo.__version__ == "0.1.0"


@pytest.mark.parametrize("name", MODULES)
def test_every_designed_module_imports(name: str) -> None:
    """follow/fetch/ingest/notify are the four pieces docs/DESIGN.md names."""
    assert importlib.import_module(name) is not None


@pytest.mark.parametrize("name", MODULES)
def test_every_module_documents_what_it_owns(name: str) -> None:
    doc = importlib.import_module(name).__doc__
    assert doc is not None and doc.strip(), f"{name} must document its responsibility"


def test_entry_point_fails_until_implemented(capsys: pytest.CaptureFixture[str]) -> None:
    """An unbuilt worker must not exit 0 — a clean start would read as a healthy deploy."""
    assert main() == 1
    assert "not implemented" in capsys.readouterr().err

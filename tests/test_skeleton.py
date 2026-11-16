"""The package's own guarantees: it imports, and every designed module is present.

The entry point's behaviour now lives in ``test_cli.py`` — ``python -m echo`` stopped being a
deliberate failure once the worker existed.
"""

from __future__ import annotations

import importlib

import pytest

import echo

MODULES = ["echo.follow", "echo.fetch", "echo.ingest", "echo.notify", "echo.state", "echo.worker"]


def test_version_is_exposed() -> None:
    assert echo.__version__ == "0.1.0"


@pytest.mark.parametrize("name", MODULES)
def test_every_designed_module_imports(name: str) -> None:
    """follow/fetch/ingest/notify are the four pieces docs/DESIGN.md names, plus state/."""
    assert importlib.import_module(name) is not None


@pytest.mark.parametrize("name", MODULES)
def test_every_module_documents_what_it_owns(name: str) -> None:
    doc = importlib.import_module(name).__doc__
    assert doc is not None and doc.strip(), f"{name} must document its responsibility"

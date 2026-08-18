"""Make the plugin importable as a package in tests.

The plugin ships as a directory that Hermes imports by path, so tests load it
under a stable name here rather than depending on Hermes being installed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugin"


def _load_plugin_package():
    """Import ``plugin/`` as the package ``databricks_plugin``."""
    name = "databricks_plugin"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def plugin_pkg():
    return _load_plugin_package()


@pytest.fixture(scope="session")
def api(plugin_pkg):
    import importlib

    return importlib.import_module("databricks_plugin.api")


@pytest.fixture(scope="session")
def config_mod(plugin_pkg):
    import importlib

    return importlib.import_module("databricks_plugin.config")


@pytest.fixture(scope="session")
def profiles_mod(plugin_pkg):
    import importlib

    return importlib.import_module("databricks_plugin.profiles")

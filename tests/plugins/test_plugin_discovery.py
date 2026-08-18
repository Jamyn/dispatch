"""Every plugin Dispatch declares must actually load.

``dispatch.common.utils.cli.install_plugins`` walks
``entry_points().select(group="dispatch.plugins")`` and calls ``ep.load()`` on
each one, catching every exception into a log line. A plugin that cannot import
therefore does not stop the app: it boots, serves every route, and is simply
missing a capability. Booting the app and exercising real API routes -- what
``image-smoke`` does -- all passes in that state, so liveness checks cannot
stand in for this. That job carries a companion step asserting the same thing
against the built image; this module covers the source tree.

A dependency bump is the usual way in: ``docker/Dockerfile`` installs
``requirements-lock.txt`` with ``--no-deps``, so a transitive package the lock
omits is absent, and the plugin importing it disappears with no failing check.

Per-plugin discovery suites pin the specifics of one plugin -- its target
class, its type, its slug. This module covers the other axis: that *every*
declared plugin still imports, so a dependency change cannot quietly remove one
that nothing else tests.

Parametrized from pyproject.toml rather than from installed metadata on
purpose. Building the cases from what is installed means an environment where
nothing is installed yields zero cases, and a parametrized test with no cases
passes silently -- which is the same failure this module exists to catch.
"""

import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from dispatch.plugins.base.v1 import Plugin

ENTRY_POINT_GROUP = "dispatch.plugins"
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

with PYPROJECT.open("rb") as _f:
    DECLARED: dict[str, str] = dict(tomllib.load(_f)["project"]["entry-points"][ENTRY_POINT_GROUP])


@pytest.fixture(scope="module")
def installed() -> dict:
    """The plugin entry points as ``install_plugins`` sees them."""
    return {ep.name: ep for ep in entry_points().select(group=ENTRY_POINT_GROUP)}


def test_pyproject_declares_plugins():
    """Guards the parametrization above: no declarations means no test cases,
    and a parametrized test with no cases reports success."""
    assert DECLARED, f"no '{ENTRY_POINT_GROUP}' entry points declared in {PYPROJECT}"


def test_the_installed_entry_points_match_the_declared_ones(installed):
    """Installed metadata only tracks pyproject.toml after a reinstall, so a
    mismatch means the environment predates the declaration -- which would make
    every load case below vacuous rather than failing."""
    assert set(installed) == set(DECLARED)


@pytest.mark.parametrize("name", sorted(DECLARED))
def test_the_declared_plugin_loads(name, installed):
    """``ep.load()`` is the call ``install_plugins`` makes and the one that a
    missing dependency, a moved module or a renamed class breaks."""
    assert name in installed, (
        f"'{name}' is declared in pyproject.toml but absent from the installed "
        "metadata; the package needs reinstalling"
    )
    ep = installed[name]
    assert ep.value == DECLARED[name]

    loaded = ep.load()

    # register() puts the class in the plugin registry the API and the plugin
    # service read; anything that is not a Plugin cannot serve that role.
    assert issubclass(loaded, Plugin), f"'{name}' loaded {loaded!r}, which is not a Plugin"

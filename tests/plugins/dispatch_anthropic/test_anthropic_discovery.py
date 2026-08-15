"""The Anthropic plugin as Dispatch actually finds it (issue #79).

Everything else in this directory imports ``AnthropicPlugin`` directly, which
proves nothing about whether a Dispatch deployment can *reach* it. Production
loads plugins from package metadata -- ``dispatch.common.utils.cli.install_plugins``
walks ``entry_points().select(group="dispatch.plugins")`` and calls ``ep.load()``
-- so a wrong module path, a misspelled class name or an entry point that never
made it into ``pyproject.toml`` is invisible to an import-based test and fatal
in a deployment. ``install_plugins`` swallows every exception into a log line,
so the symptom would be a plugin that silently does not appear in the UI.

These tests run against installed metadata, so they only reflect a
``pyproject.toml`` edit after the package is reinstalled -- which is exactly the
packaging step they exist to catch being skipped.
"""

from importlib.metadata import entry_points

import pytest

from dispatch.plugins.bases import ArtificialIntelligencePlugin

ENTRY_POINT_NAME = "anthropic_artificial_intelligence"


@pytest.fixture
def entry_point():
    """The Anthropic entry point, found the way ``install_plugins`` finds it."""
    found = [
        ep for ep in entry_points().select(group="dispatch.plugins") if ep.name == ENTRY_POINT_NAME
    ]
    if not found:
        pytest.fail(
            f"no '{ENTRY_POINT_NAME}' entry point in the installed metadata. "
            "If pyproject.toml declares it, the package needs reinstalling."
        )
    assert len(found) == 1, f"'{ENTRY_POINT_NAME}' is declared more than once"
    return found[0]


def test_the_entry_point_is_declared(entry_point):
    assert entry_point.value == "dispatch.plugins.dispatch_anthropic.plugin:AnthropicPlugin"


def test_the_entry_point_loads(entry_point):
    """``ep.load()`` is the call ``install_plugins`` makes, and the one that
    fails on a wrong module path or class name."""
    from dispatch.plugins.dispatch_anthropic.plugin import AnthropicPlugin

    assert entry_point.load() is AnthropicPlugin


def test_the_loaded_plugin_is_an_artificial_intelligence_plugin(entry_point):
    """``plugin_service.get_active_instance(..., plugin_type=...)`` selects on
    this. A plugin registered under any other type is unreachable from the ai
    service no matter how correct the rest of it is."""
    plugin = entry_point.load()

    assert issubclass(plugin, ArtificialIntelligencePlugin)
    assert plugin.type == "artificial-intelligence"


def test_the_loaded_plugin_instantiates_with_its_configuration_schema(entry_point):
    """``register`` instantiates the class, and the settings form is built from
    ``configuration_schema``."""
    from dispatch.plugins.dispatch_anthropic.config import AnthropicConfiguration

    instance = entry_point.load()()

    assert instance.configuration_schema is AnthropicConfiguration
    assert instance.slug == "anthropic-artificial-intelligence"


def test_it_does_not_collide_with_the_openai_plugin(entry_point):
    """Both are artificial-intelligence plugins and only one can be active per
    project, so they must at least be distinguishable by slug."""
    from dispatch.plugins.dispatch_openai.plugin import OpenAIPlugin

    assert entry_point.load().slug != OpenAIPlugin.slug

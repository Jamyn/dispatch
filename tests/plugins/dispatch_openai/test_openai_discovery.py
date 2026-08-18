"""The OpenAI plugin as Dispatch actually finds it.

Mirrors ``tests/plugins/dispatch_anthropic/test_anthropic_discovery.py``:
production loads plugins from package metadata via
``dispatch.common.utils.cli.install_plugins``, which swallows every exception
into a log line, so a plugin that cannot import is invisible rather than fatal.

That swallowing is what makes a missing *transitive* dependency dangerous here.
openai 3.x imports ``httpx2``, which it no longer pulls in implicitly on some
install paths; ``docker/Dockerfile`` installs ``requirements-lock.txt`` with
``--no-deps``, so anything absent from the lock is simply absent. The image
still builds, still boots and still serves every route -- the only symptom is
an OpenAI plugin that never appears. ``ep.load()`` is the call that notices.
"""

from importlib.metadata import entry_points

import pytest

from dispatch.plugins.bases import ArtificialIntelligencePlugin

ENTRY_POINT_NAME = "openai_artificial_intelligence"


@pytest.fixture
def entry_point():
    """The OpenAI entry point, found the way ``install_plugins`` finds it."""
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
    assert entry_point.value == "dispatch.plugins.dispatch_openai.plugin:OpenAIPlugin"


def test_the_entry_point_loads(entry_point):
    """``ep.load()`` imports the plugin module, and with it the openai SDK.

    This is the assertion that fails when the SDK's own dependencies are
    missing from the lock -- the case ``image-smoke`` cannot see, because a
    plugin that fails to load does not stop the app from serving.
    """
    from dispatch.plugins.dispatch_openai.plugin import OpenAIPlugin

    assert entry_point.load() is OpenAIPlugin


def test_the_loaded_plugin_is_an_artificial_intelligence_plugin(entry_point):
    """``plugin_service.get_active_instance(..., plugin_type=...)`` selects on
    this. A plugin registered under any other type is unreachable from the ai
    service no matter how correct the rest of it is."""
    plugin = entry_point.load()

    assert issubclass(plugin, ArtificialIntelligencePlugin)
    assert plugin.type == "artificial-intelligence"

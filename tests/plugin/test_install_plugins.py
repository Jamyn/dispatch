"""``install_plugins`` must not swallow a broken first-party plugin.

Its tolerance of failure is deliberate -- a provider integration that cannot
load should not stop Dispatch answering requests -- but that tolerance used to
extend to plugins shipped in the wheel, where the only possible cause is a
packaging bug. The app then boots, serves every route and silently lacks a
capability, which no liveness check can see.

The entry points here are fakes rather than the real metadata: the real ones
all import, and the case under test is the one that must never reach a
deployment.
"""

import pytest

from dispatch.common.utils.cli import install_plugins


class FakeEntryPoint:
    """Enough of importlib.metadata.EntryPoint for install_plugins."""

    def __init__(self, name, value, exc=None):
        self.name = name
        self.value = value
        self._exc = exc

    def load(self):
        if self._exc:
            raise self._exc
        return _LoadablePlugin


class _LoadablePlugin:
    pass


@pytest.fixture
def fake_entry_points(monkeypatch):
    """Replaces the entry point scan install_plugins performs."""

    def install(entry_points_list):
        class _Selectable(list):
            def select(self, group=None):
                return self

        monkeypatch.setattr(
            "dispatch.common.utils.cli.entry_points",
            lambda: _Selectable(entry_points_list),
        )
        # register() writes into the global plugin registry; the loadable stub
        # is not a real Plugin, so keep it out of there.
        monkeypatch.setattr("dispatch.common.utils.cli.register", lambda plugin: None)

    return install


def test_a_first_party_plugin_that_cannot_import_is_fatal(fake_entry_points):
    """A dependency missing from the lock lands here."""
    fake_entry_points(
        [
            FakeEntryPoint(
                "openai_artificial_intelligence",
                "dispatch.plugins.dispatch_openai.plugin:OpenAIPlugin",
                exc=ModuleNotFoundError("No module named 'httpx2'"),
            )
        ]
    )

    with pytest.raises(RuntimeError) as excinfo:
        install_plugins()

    assert "openai_artificial_intelligence" in str(excinfo.value)


def test_every_broken_first_party_plugin_is_named(fake_entry_points):
    """One run reports all of them, so a broken build is fixed in one pass."""
    fake_entry_points(
        [
            FakeEntryPoint("a_plugin", "dispatch.plugins.a.plugin:A", exc=ImportError("boom")),
            FakeEntryPoint("healthy_plugin", "dispatch.plugins.b.plugin:B"),
            FakeEntryPoint("z_plugin", "dispatch.plugins.z.plugin:Z", exc=ImportError("boom")),
        ]
    )

    with pytest.raises(RuntimeError) as excinfo:
        install_plugins()

    message = str(excinfo.value)
    assert "a_plugin" in message and "z_plugin" in message
    assert "healthy_plugin" not in message


def test_a_third_party_plugin_that_cannot_import_is_tolerated(fake_entry_points):
    """Nothing in this repository can guarantee an external plugin's
    dependencies, so it keeps the old log-and-continue behaviour."""
    fake_entry_points(
        [FakeEntryPoint("vendor_thing", "vendor.plugins.thing:Thing", exc=ImportError("boom"))]
    )

    install_plugins()


def test_other_failures_are_still_tolerated(fake_entry_points):
    """A first-party plugin raising something that is not an ImportError is not
    evidence of a packaging problem, so it must not become fatal."""
    fake_entry_points(
        [FakeEntryPoint("odd_plugin", "dispatch.plugins.odd.plugin:Odd", exc=ValueError("boom"))]
    )

    install_plugins()


def test_a_healthy_set_of_plugins_installs_quietly(fake_entry_points):
    fake_entry_points(
        [
            FakeEntryPoint("one", "dispatch.plugins.one.plugin:One"),
            FakeEntryPoint("two", "dispatch.plugins.two.plugin:Two"),
        ]
    )

    install_plugins()

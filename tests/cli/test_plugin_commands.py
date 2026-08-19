"""Uninstalling plugins from the CLI.

`uninstall` takes any number of slugs. It printed a message for one it could not
find and then read `.id` off the `None` it had just checked, so a single typo
became an AttributeError -- and took every slug still queued behind it with it.
"""

import pytest
from click.testing import CliRunner

from dispatch.cli import dispatch_cli
from dispatch.plugin import service as plugin_service


def still_installed(session, slug):
    """Whether `slug` survives, asked fresh.

    The command runs on its own session, so the test's own identity map still
    holds the pre-delete row until it is expired.
    """
    session.expire_all()
    return plugin_service.get_by_slug(db_session=session, slug=slug) is not None


@pytest.fixture
def runner():
    return CliRunner()


def test_a_known_plugin_is_uninstalled(session, runner, plugin):
    """Given an installed plugin, when uninstalling it, then it is gone."""
    result = runner.invoke(dispatch_cli, ["plugins", "uninstall", plugin.slug])

    assert result.exit_code == 0, result.output
    assert not still_installed(session, plugin.slug)


def test_an_unknown_slug_is_reported_without_a_traceback(session, runner):
    """Given a slug nobody installed, when uninstalling it, then it fails readably."""
    result = runner.invoke(dispatch_cli, ["plugins", "uninstall", "no-such-plugin"])

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"operator saw a traceback: {result.exception!r}"
    )
    assert "does not exist" in result.output


def test_one_bad_slug_does_not_abandon_the_rest(session, runner, plugin):
    """Given a typo among several slugs, when uninstalling, then the valid ones still go.

    The operator asked for a batch; failing the first should not silently leave
    the remainder installed.
    """
    result = runner.invoke(dispatch_cli, ["plugins", "uninstall", "no-such-plugin", plugin.slug])

    assert result.exit_code != 0, "the missing slug should still be reported as a failure"
    assert not still_installed(session, plugin.slug), (
        "a slug queued after the bad one was never processed"
    )


def test_a_plugin_a_project_still_uses_is_kept(session, runner, plugin_instance):
    """Given a configured plugin, when uninstalling it, then it is refused and kept.

    The instance holds that project's encrypted credentials for the provider.
    Removing the plugin definition underneath it would destroy them, so the
    command names the project and changes nothing.
    """
    plugin = plugin_instance.plugin

    result = runner.invoke(dispatch_cli, ["plugins", "uninstall", plugin.slug])

    assert result.exit_code != 0
    assert plugin_instance.project.name in result.output
    assert still_installed(session, plugin.slug), "a configured plugin was uninstalled"


def test_uninstalling_removes_the_events_registered_for_the_plugin(session, runner, plugin):
    """Given a plugin with registered events, when uninstalling, then they go with it.

    Events hold no configuration and `plugins install` rebuilds them, but the
    foreign key means leaving them behind would block the delete outright.
    """
    from dispatch.plugin.models import PluginEvent
    from tests.factories import PluginEventFactory

    PluginEventFactory(plugin=plugin)
    session.commit()
    # read before the row goes -- the instance expires along with it
    plugin_id, slug = plugin.id, plugin.slug

    result = runner.invoke(dispatch_cli, ["plugins", "uninstall", slug])

    assert result.exit_code == 0, result.output
    assert not still_installed(session, slug)
    assert session.query(PluginEvent).filter(PluginEvent.plugin_id == plugin_id).count() == 0

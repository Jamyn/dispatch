"""The tenant migration that carries the Confluence storage root across.

The plugin's `root_id` changed meaning -- space key to page id -- and its
`parent_id` field went away. An instance configured before that keeps working
only because this migration moves the value over; without it the plugin asks
Confluence for a page whose id is a space key, gets a 404, and every incident
is created with no storage while logging a single line.

The migration is exercised against a real `plugin_instance` row rather than
called as a function, because the value it rewrites lives in an encrypted
column and a round trip through it is most of what could go wrong.
"""

import importlib.util
import json
import pathlib

import pytest

from dispatch.plugin.models import PluginInstance
from tests.factories import PluginFactory, PluginInstanceFactory

REVISION = (
    pathlib.Path(__file__).parents[2]
    / "src/dispatch/database/revisions/tenant/versions/2026-08-20_b8d2f61c07ae.py"
)


@pytest.fixture
def migration():
    specification = importlib.util.spec_from_file_location("confluence_root_id", REVISION)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def stored(session, slug: str, configuration: dict) -> PluginInstance:
    """An instance holding exactly this JSON.

    Written to the column rather than through `configuration`, whose setter
    validates against the plugin's current schema -- which is precisely what
    the configuration being migrated no longer satisfies.
    """
    instance = PluginInstanceFactory(plugin=PluginFactory(slug=slug, type="storage"))
    instance._configuration = json.dumps(configuration)
    session.commit()
    return instance


def configuration_of(session, instance: PluginInstance) -> dict:
    session.expire(instance)
    return json.loads(instance._configuration)


@pytest.fixture
def run(session, monkeypatch, migration):
    def _run():
        monkeypatch.setattr("alembic.op.get_bind", lambda: session.connection())
        migration.upgrade()

    return _run


def test_the_page_incidents_were_created_under_becomes_the_root(session, run):
    """`parent_id` was already a page id, and is the one that survives."""
    instance = stored(session, "confluence", {"root_id": "INC", "parent_id": "424242"})

    run()

    assert configuration_of(session, instance)["root_id"] == "424242"


def test_the_field_that_no_longer_exists_is_not_left_behind(session, run):
    """Configuration schemas ignore undeclared keys, so a leftover would read
    like live configuration to whoever opened the record next."""
    instance = stored(session, "confluence", {"root_id": "INC", "parent_id": "424242"})

    run()

    assert "parent_id" not in configuration_of(session, instance)


def test_an_instance_configured_after_the_change_is_untouched(session, run):
    """Which is also what makes the migration safe to re-run."""
    instance = stored(session, "confluence", {"root_id": "424242", "template_id": "111"})

    run()

    assert configuration_of(session, instance) == {"root_id": "424242", "template_id": "111"}


def test_the_rest_of_the_configuration_survives(session, run):
    instance = stored(
        session,
        "confluence",
        {"root_id": "INC", "parent_id": "424242", "template_id": "111", "open_on_close": True},
    )

    run()

    configuration = configuration_of(session, instance)
    assert configuration["template_id"] == "111"
    assert configuration["open_on_close"] is True


def test_no_other_storage_plugin_is_rewritten(session, run):
    """Google Drive's `root_id` is already a folder id and it has no
    `parent_id`; a query that missed the join would rewrite it anyway."""
    drive = stored(session, "google-drive-storage", {"root_id": "0ABCdef", "parent_id": "999"})

    run()

    assert configuration_of(session, drive) == {"root_id": "0ABCdef", "parent_id": "999"}

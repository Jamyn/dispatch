"""create_storage's handling of a storage plugin that cannot make sub-folders.

Confluence has no folders: its storage plugin creates the incident's home page
and returns None for the "Logs" and "Screengrabs" calls that follow. With
`storage_use_folder_one_as_primary` set -- the default on a project created
through the API -- that None used to be subscripted, and nothing in
`incident_create_flow` catches the resulting TypeError.
"""

from types import SimpleNamespace

import pytest


@pytest.fixture
def storage_plugin_returning(monkeypatch):
    """Installs a storage plugin whose create_file returns `results` in order."""

    def install(results):
        calls = []

        def create_file(**kwargs):
            calls.append(kwargs)
            return results[len(calls) - 1]

        instance = SimpleNamespace(create_file=create_file)
        plugin = SimpleNamespace(
            instance=instance,
            configuration=SimpleNamespace(root_id="INC"),
            plugin=SimpleNamespace(slug="confluence", title="Confluence"),
        )
        monkeypatch.setattr(
            "dispatch.storage.flows.plugin_service.get_active_instance",
            lambda **kwargs: plugin,
        )
        monkeypatch.setattr(
            "dispatch.storage.flows.tag_type_service.get_storage_tag_type_for_project",
            lambda **kwargs: None,
        )
        return calls

    return install


def test_storage_is_not_created_when_the_primary_folder_could_not_be_made(
    session, incident, storage_plugin_returning, caplog
):
    from dispatch.storage.flows import create_storage

    incident.project.storage_use_folder_one_as_primary = True
    home_page = {"id": "900001", "weblink": "https://example.atlassian.net/wiki/x"}
    storage_plugin_returning([home_page, None, None])

    create_storage(subject=incident, storage_members=[], db_session=session)

    assert incident.storage is None
    assert "could not create" in caplog.text


def test_storage_uses_the_root_when_folder_one_is_not_primary(
    session, incident, storage_plugin_returning
):
    from dispatch.storage.flows import create_storage

    incident.project.storage_use_folder_one_as_primary = False
    home_page = {"id": "900001", "weblink": "https://example.atlassian.net/wiki/x"}
    storage_plugin_returning([home_page, None, None])

    create_storage(subject=incident, storage_members=[], db_session=session)

    assert incident.storage.resource_id == "900001"


def test_storage_uses_folder_one_when_the_plugin_makes_folders(
    session, incident, storage_plugin_returning
):
    from dispatch.storage.flows import create_storage

    incident.project.storage_use_folder_one_as_primary = True
    root = {"id": "root", "weblink": "https://drive.example.com/root"}
    logs = {"id": "logs", "weblink": "https://drive.example.com/logs"}
    grabs = {"id": "grabs", "weblink": "https://drive.example.com/grabs"}
    storage_plugin_returning([root, logs, grabs])

    create_storage(subject=incident, storage_members=[], db_session=session)

    assert incident.storage.resource_id == "logs"

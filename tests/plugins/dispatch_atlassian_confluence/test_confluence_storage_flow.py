"""Dispatch's own storage and document flows, run against the Confluence fake.

The per-method tests next door prove each call builds the right request. This
proves the sequence core actually executes -- three create_file calls and a
copy_file, each fed the id the previous one returned -- lands a real hierarchy.
Issue #242 is precisely a case where every individual call looked reasonable
and the sequence could not work, because the identifier the plugin returned was
not the kind it accepted.
"""

from types import SimpleNamespace

import pytest

from tests.plugins.dispatch_atlassian_confluence.conftest import storage_plugin
from tests.plugins.dispatch_atlassian_confluence.fake_confluence import (
    ROOT_PAGE_ID,
    TEMPLATE_BODY,
)


@pytest.fixture
def confluence_storage(monkeypatch, hosting_type):
    """Installs the real Confluence plugin as the project's storage plugin."""
    instance = storage_plugin(hosting_type)
    plugin = SimpleNamespace(
        instance=instance,
        configuration=instance.configuration,
        plugin=SimpleNamespace(slug="confluence", title="Confluence"),
    )
    for module in ("dispatch.storage.flows", "dispatch.document.flows"):
        monkeypatch.setattr(f"{module}.plugin_service.get_active_instance", lambda **k: plugin)
    monkeypatch.setattr(
        "dispatch.storage.flows.tag_type_service.get_storage_tag_type_for_project",
        lambda **kwargs: None,
    )
    return plugin


def test_creating_storage_files_the_whole_hierarchy_under_the_root_page(
    session, incident, confluence, confluence_storage
):
    from dispatch.storage.flows import create_storage

    incident.project.storage_use_folder_one_as_primary = False

    create_storage(subject=incident, storage_members=[], db_session=session)

    incident_page = confluence.page(incident.storage.resource_id)
    assert incident_page.parent_id == ROOT_PAGE_ID

    children = [p for p in confluence.pages.values() if p.parent_id == incident_page.id]
    # Titled for the incident, because Confluence holds titles unique per
    # space and every incident asks for these same two names.
    assert sorted(page.title for page in children) == [
        f"{incident.name} - Logs",
        f"{incident.name} - Screengrabs",
    ]


def test_creating_storage_can_make_folder_one_the_primary_resource(
    session, incident, confluence, confluence_storage
):
    """`storage_use_folder_one_as_primary` is the default on a project created
    through the API, and it stores the first sub-folder rather than the
    incident's own page. Before #242 that sub-folder could not be created."""
    from dispatch.storage.flows import create_storage

    incident.project.storage_use_folder_one_as_primary = True

    create_storage(subject=incident, storage_members=[], db_session=session)

    primary = confluence.page(incident.storage.resource_id)
    assert primary.title == f"{incident.name} - Logs"
    assert confluence.page(primary.parent_id).parent_id == ROOT_PAGE_ID


def test_a_second_incident_gets_its_own_storage(session, incident, confluence, confluence_storage):
    """The folder names are project settings, identical for every incident, and
    Confluence titles are unique per space. Creating them verbatim leaves the
    second incident with no storage at all, because create_storage returns
    without one when folder one -- the primary -- could not be created."""
    from tests.factories import IncidentFactory
    from dispatch.storage.flows import create_storage

    for subject in (incident, IncidentFactory()):
        subject.project.storage_use_folder_one_as_primary = True
        create_storage(subject=subject, storage_members=[], db_session=session)
        assert subject.storage is not None, "no storage was created"
        assert confluence.page(subject.storage.resource_id).title.endswith("Logs")


def test_creating_a_document_copies_the_template_under_the_incident_page(
    session, incident, confluence, confluence_storage
):
    from dispatch.document.flows import create_document
    from dispatch.storage.flows import create_storage

    incident.project.storage_use_folder_one_as_primary = False
    create_storage(subject=incident, storage_members=[], db_session=session)
    template = SimpleNamespace(resource_id="111111", description="Incident template")

    document = create_document(
        subject=incident,
        document_type="incident-document",
        document_template=template,
        db_session=session,
    )

    created = confluence.page(document.resource_id)
    assert created.parent_id == incident.storage.resource_id
    assert created.body == TEMPLATE_BODY


def test_creating_a_document_without_a_template_makes_a_blank_page(
    session, incident, confluence, confluence_storage
):
    """The other half of create_document: no template configured, so it falls
    back to create_file with the incident's storage id."""
    from dispatch.document.flows import create_document
    from dispatch.storage.flows import create_storage

    incident.project.storage_use_folder_one_as_primary = False
    create_storage(subject=incident, storage_members=[], db_session=session)

    document = create_document(
        subject=incident,
        document_type="incident-document",
        document_template=None,
        db_session=session,
    )

    assert confluence.page(document.resource_id).parent_id == incident.storage.resource_id

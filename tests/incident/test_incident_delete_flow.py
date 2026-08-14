"""The incident resource lifecycle: what deletion tears down, and what closing leaves alone.

Issue #105: the flow deleted the ticket, the groups, the storage and the
conversation, and left the conference bridge behind.

These tests drive the real flow through real plugin resolution -- a
``PluginInstance`` row per plugin type, with ``.instance`` resolving to a
recorder instead of a live client. Nothing about the flow itself is patched, so
a change in *which* helper the flow calls, or in what it passes, is visible
here.

Two properties are load-bearing:

- the conference is deleted with the provider's meeting id, not Dispatch's own
  ``resource_id`` (they hold the same string today, so the fixture forces them
  apart);
- a conference the provider will not delete cannot abort the deletion, which
  the parametrised failure test detects by asserting every *other* resource was
  still torn down.

Position is asserted separately, by ``test_the_teardown_order``: the
parametrised failure test only proves nothing was skipped, so it stays green if
the conference teardown is moved to the end of the flow.
"""

import logging

from types import SimpleNamespace

import pytest

from dispatch.enums import Visibility
from dispatch.exceptions import DispatchPluginException
from dispatch.incident.flows import incident_closed_status_flow, incident_delete_flow
from dispatch.plugin.models import PluginInstance

from tests.factories import (
    ConferenceFactory,
    ConversationFactory,
    GroupFactory,
    PluginFactory,
    PluginInstanceFactory,
    StorageFactory,
    TicketFactory,
)

PROVIDER_MEETING_ID = "provider-meeting-123"
INTERNAL_RESOURCE_ID = "internal-resource-456"

# The plugin type each external resource is resolved through.
RESOURCE_PLUGINS = ["ticket", "participant-group", "storage", "conversation", "conference"]


class RecordingPlugin:
    """One fake plugin instance, recording every call the delete flows make.

    Deliberately not a ``Mock``: an auto-speccing mock answers to any method
    name, which would hide a flow calling something the real plugin does not
    implement.
    """

    def __init__(self, plugin_type, sequence):
        self.plugin_type = plugin_type
        # shared across every recorder, so the *relative* order of the
        # teardowns is observable and not just each one's own call list
        self.sequence = sequence
        self.calls: list[tuple[str, tuple, dict]] = []
        self.failure: Exception | None = None

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        self.sequence.append(self.plugin_type)
        if self.failure:
            raise self.failure

    @property
    def called(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    # ticket_flows.delete_ticket, group_flows.delete_group and
    # conference_flows.delete_conference all land on `delete`, each with its own
    # calling convention -- keyword `ticket_id`, keyword `email`, and a
    # positional event id respectively.
    def delete(self, *args, **kwargs):
        self._record("delete", *args, **kwargs)

    def delete_file(self, **kwargs):
        self._record("delete_file", **kwargs)

    def rename(self, *args):
        self._record("rename", *args)

    def archive(self, *args):
        self._record("archive", *args)

    # only the close flow reaches this, when it messages the commander
    def send_direct(self, *args, **kwargs):
        self._record("send_direct", *args, **kwargs)


@pytest.fixture
def teardown_sequence():
    """Every plugin call across all recorders, in the order the flow made them."""
    return []


@pytest.fixture
def plugins(session, incident, monkeypatch, teardown_sequence):
    """An enabled plugin instance of every resource type, all recording."""
    recorders = {
        plugin_type: RecordingPlugin(plugin_type, teardown_sequence)
        for plugin_type in RESOURCE_PLUGINS
    }

    for plugin_type in RESOURCE_PLUGINS:
        PluginInstanceFactory(
            project=incident.project,
            plugin=PluginFactory(type=plugin_type, slug=f"test-{plugin_type}-delete"),
            enabled=True,
        )

    monkeypatch.setattr(
        PluginInstance,
        "instance",
        property(lambda self: recorders[self.plugin.type]),
    )

    # The close flow reads `storage_plugin.configuration.open_on_close`, and an
    # unconfigured instance returns None there -- without this the flow dies at
    # that line and every assertion after it becomes unreachable.
    #
    # Saved and restored by hand rather than with monkeypatch: `configuration`
    # is a hybrid_property that raises on class-level access, so monkeypatch
    # cannot see it, and `raising=False` would *delete* it on teardown for the
    # rest of the session.
    original = PluginInstance.__dict__["configuration"]
    PluginInstance.configuration = property(
        lambda self: SimpleNamespace(open_on_close=False, read_only=False)
    )
    try:
        yield recorders
    finally:
        PluginInstance.configuration = original


@pytest.fixture
def incident_with_resources(session, incident):
    """An incident carrying every external resource the delete flow tears down."""
    incident.ticket = TicketFactory()
    incident.groups = [GroupFactory()]
    incident.storage = StorageFactory()
    incident.conversation = ConversationFactory()
    incident.conference = ConferenceFactory(
        incident=incident,
        conference_id=PROVIDER_MEETING_ID,
        resource_id=INTERNAL_RESOURCE_ID,
    )
    session.add(incident)
    session.commit()
    return incident


# --- the conference is torn down too ----------------------------------------


def test_the_conference_is_deleted(session, incident_with_resources, plugins):
    incident_delete_flow(incident=incident_with_resources, db_session=session)

    assert plugins["conference"].called == ["delete"]


def test_the_conference_is_deleted_with_the_provider_meeting_id(
    session, incident_with_resources, plugins
):
    """Both plugins take the provider's meeting id positionally."""
    incident_delete_flow(incident=incident_with_resources, db_session=session)

    assert plugins["conference"].calls == [("delete", (PROVIDER_MEETING_ID,), {})]


def test_the_internal_resource_id_never_reaches_the_conference_plugin(
    session, incident_with_resources, plugins
):
    """Catches an implementation that reaches for ``resource_id`` instead."""
    incident_delete_flow(incident=incident_with_resources, db_session=session)

    sent = [args for _, args, _ in plugins["conference"].calls]
    assert (INTERNAL_RESOURCE_ID,) not in sent


def test_an_incident_without_a_conference_does_not_call_the_conference_plugin(
    session, incident_with_resources, plugins
):
    incident_with_resources.conference = None
    session.commit()

    incident_delete_flow(incident=incident_with_resources, db_session=session)

    assert plugins["conference"].calls == []


# --- the other resources are unaffected -------------------------------------


def test_every_other_resource_is_still_deleted(session, incident_with_resources, plugins):
    """The behaviour that existed before #105, unchanged."""
    incident_delete_flow(incident=incident_with_resources, db_session=session)

    assert plugins["ticket"].calls == [
        ("delete", (), {"ticket_id": incident_with_resources.ticket.resource_id})
    ]
    assert plugins["participant-group"].calls == [
        ("delete", (), {"email": incident_with_resources.groups[0].email})
    ]
    assert plugins["storage"].calls == [
        ("delete_file", (), {"file_id": incident_with_resources.storage.resource_id})
    ]
    assert plugins["conversation"].called == ["rename", "archive"]


def test_the_teardown_order(session, incident_with_resources, plugins, teardown_sequence):
    """The conference goes between the storage and the conversation.

    That is where it is created (ticket -> groups -> storage -> document ->
    conference -> conversation), and the delete flow follows the same order
    rather than reversing it. Asserted explicitly because every other test here
    stays green with the conference teardown moved anywhere in the flow.
    """
    incident_delete_flow(incident=incident_with_resources, db_session=session)

    assert teardown_sequence == [
        "ticket",
        "participant-group",
        "storage",
        "conference",
        "conversation",  # rename
        "conversation",  # archive
    ]


# --- a failure anywhere is contained ----------------------------------------


@pytest.mark.parametrize("failing", RESOURCE_PLUGINS)
def test_one_failing_resource_does_not_stop_the_others(
    session, incident_with_resources, plugins, failing
):
    """Every teardown is best effort, the conference included.

    Parametrised over all five so that adding the conference cannot quietly
    broaden -- or narrow -- the handling around any of the original four.
    """
    plugins[failing].failure = DispatchPluginException("the provider said no")

    incident_delete_flow(incident=incident_with_resources, db_session=session)

    for plugin_type, recorder in plugins.items():
        assert recorder.calls, f"{plugin_type} was skipped after {failing} failed"


def test_a_failing_conference_delete_is_logged(session, incident_with_resources, plugins, caplog):
    plugins["conference"].failure = RuntimeError("Graph said no")

    with caplog.at_level(logging.ERROR):
        incident_delete_flow(incident=incident_with_resources, db_session=session)

    assert "Graph said no" in caplog.text


# --- closing is not deleting (issue #105) ------------------------------------


@pytest.mark.parametrize("visibility", [Visibility.open, Visibility.restricted])
def test_closing_an_incident_does_not_delete_the_conference(
    session, incident_with_resources, plugins, visibility
):
    """A deliberate lifecycle distinction, not an oversight.

    The close flow *archives* the conversation rather than deleting it, and a
    closed incident's bridge is still wanted -- late joiners, the review, the
    post-incident discussion. Deletion belongs to the delete flow alone.

    Parametrised over both visibilities because ``incident_closed_status_flow``
    has a whole ``Visibility.open``-only branch: a version of this test pinned
    to ``restricted`` never enters it, and a conference teardown added *inside*
    that branch would go undetected.
    """
    incident_with_resources.visibility = visibility
    session.commit()

    incident_closed_status_flow(incident=incident_with_resources, db_session=session)

    # the flow really ran, so the negative assertion below is not vacuous
    assert incident_with_resources.closed_at is not None
    assert "archive" in plugins["conversation"].called

    assert plugins["conference"].calls == []

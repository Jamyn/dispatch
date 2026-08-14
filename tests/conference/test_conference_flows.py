"""The conference participant flow helpers (issue #106).

The contract these tests defend is that a conference roster update is a
*secondary* integration: the tactical group and the conversation are what
actually get a responder into an incident, so a failed roster update must be
recorded and dropped rather than propagated. Every failure path below therefore
asserts that nothing is raised.
"""

import pytest

from dispatch.conference.flows import add_conference_participant, remove_conference_participant

from tests.factories import ConferenceFactory, PluginFactory, PluginInstanceFactory


class RecordingConferencePlugin:
    """Stands in for the plugin instance's ``.instance`` attribute."""

    def __init__(self, failure: Exception | None = None):
        self.failure = failure
        self.added: list[tuple[str, str]] = []
        self.removed: list[tuple[str, str]] = []

    def add_participant(self, event_id, participant):
        if self.failure:
            raise self.failure
        self.added.append((event_id, participant))

    def remove_participant(self, event_id, participant):
        if self.failure:
            raise self.failure
        self.removed.append((event_id, participant))


@pytest.fixture
def conference_incident(session, incident):
    """An incident with a conference, as one created by the incident flow has."""
    incident.conference = ConferenceFactory(conference_id="meeting-123")
    return incident


@pytest.fixture
def active_conference_plugin(session, conference_incident, monkeypatch):
    """Register a recording conference plugin for the incident's project."""
    recorder = RecordingConferencePlugin()
    instance = PluginInstanceFactory(
        project=conference_incident.project,
        plugin=PluginFactory(type="conference", slug="test-conference-roster"),
        enabled=True,
    )
    monkeypatch.setattr(type(instance), "instance", property(lambda self: recorder))
    return recorder


# --- the happy path ---------------------------------------------------------


def test_add_calls_the_plugin_with_the_conference_id(
    session, conference_incident, active_conference_plugin
):
    add_conference_participant(
        incident=conference_incident,
        participant_email="responder@example.com",
        db_session=session,
    )

    assert active_conference_plugin.added == [("meeting-123", "responder@example.com")]


def test_remove_calls_the_plugin_with_the_conference_id(
    session, conference_incident, active_conference_plugin
):
    remove_conference_participant(
        incident=conference_incident,
        participant_email="responder@example.com",
        db_session=session,
    )

    assert active_conference_plugin.removed == [("meeting-123", "responder@example.com")]


def test_add_does_not_also_remove(session, conference_incident, active_conference_plugin):
    """A copy-paste between the two helpers is otherwise invisible."""
    add_conference_participant(
        incident=conference_incident,
        participant_email="responder@example.com",
        db_session=session,
    )

    assert active_conference_plugin.removed == []


# --- nothing to do ----------------------------------------------------------


def test_an_incident_without_a_conference_is_a_no_op(session, incident, active_conference_plugin):
    """Conferences are optional; most projects run without one."""
    incident.conference = None

    add_conference_participant(
        incident=incident, participant_email="responder@example.com", db_session=session
    )

    assert active_conference_plugin.added == []


def test_no_conference_plugin_is_a_no_op(session, conference_incident):
    """No plugin instance is registered for this project at all."""
    add_conference_participant(
        incident=conference_incident,
        participant_email="responder@example.com",
        db_session=session,
    )


def test_removal_without_a_conference_is_a_no_op(session, incident, active_conference_plugin):
    incident.conference = None

    remove_conference_participant(
        incident=incident, participant_email="responder@example.com", db_session=session
    )

    assert active_conference_plugin.removed == []


# --- failures are contained -------------------------------------------------


def test_a_failing_add_does_not_raise(session, conference_incident, active_conference_plugin):
    """The load-bearing parts of the participant flow already succeeded."""
    active_conference_plugin.failure = RuntimeError("Graph said no")

    add_conference_participant(
        incident=conference_incident,
        participant_email="responder@example.com",
        db_session=session,
    )


def test_a_failing_remove_does_not_raise(session, conference_incident, active_conference_plugin):
    active_conference_plugin.failure = RuntimeError("Graph said no")

    remove_conference_participant(
        incident=conference_incident,
        participant_email="responder@example.com",
        db_session=session,
    )


def test_a_failing_add_is_logged(session, conference_incident, active_conference_plugin, caplog):
    """A silent swallow leaves an operator with no way to notice the drift."""
    import logging

    active_conference_plugin.failure = RuntimeError("Graph said no")

    with caplog.at_level(logging.ERROR):
        add_conference_participant(
            incident=conference_incident,
            participant_email="responder@example.com",
            db_session=session,
        )

    assert "Graph said no" in caplog.text


def test_a_failing_add_is_recorded_on_the_incident_timeline(
    session, conference_incident, active_conference_plugin
):
    from dispatch.event import service as event_service

    active_conference_plugin.failure = RuntimeError("Graph said no")

    add_conference_participant(
        incident=conference_incident,
        participant_email="responder@example.com",
        db_session=session,
    )

    events = event_service.get_all(db_session=session).all()
    assert any("conference" in e.description.lower() for e in events)


def test_a_failing_remove_is_logged(session, conference_incident, active_conference_plugin, caplog):
    import logging

    active_conference_plugin.failure = RuntimeError("Graph said no")

    with caplog.at_level(logging.ERROR):
        remove_conference_participant(
            incident=conference_incident,
            participant_email="responder@example.com",
            db_session=session,
        )

    assert "Graph said no" in caplog.text


def test_a_plugin_exception_is_contained_too(
    session, conference_incident, active_conference_plugin
):
    """The plugins raise DispatchPluginException specifically."""
    from dispatch.exceptions import DispatchPluginException

    active_conference_plugin.failure = DispatchPluginException("HTTP 403")

    add_conference_participant(
        incident=conference_incident,
        participant_email="responder@example.com",
        db_session=session,
    )

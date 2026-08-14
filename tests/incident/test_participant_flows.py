"""Conference wiring in the incident participant flows (issue #106).

The claim under test is a *relationship* between integrations, not any one of
them: the tactical group and the conversation are load-bearing, the conference
roster is not, and a failure of the latter must not cost a responder their place
in the former. Asserting the conference call merely happens would not establish
that -- so each test below also pins the ordering, or lets the conference fail
and checks that everything else still ran.

The collaborators are replaced at the module attribute the flow actually reads,
so a call that moves to a different integration fails these tests rather than
passing them silently.
"""

import pytest

from dispatch.incident import flows
from dispatch.participant_role.models import ParticipantRoleType

from tests.factories import ConferenceFactory, PluginFactory, PluginInstanceFactory


class Recorder:
    """Records the order in which the flow reaches each integration."""

    def __init__(self):
        self.calls: list[str] = []

    def note(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)

        return record

    def fail(self, name, exception):
        def explode(*args, **kwargs):
            self.calls.append(name)
            raise exception

        return explode


@pytest.fixture
def recorder(monkeypatch):
    """Stub every integration the participant flows fan out to."""
    rec = Recorder()

    monkeypatch.setattr(flows.group_flows, "update_group", rec.note("group"))
    monkeypatch.setattr(
        flows.conversation_flows,
        "add_incident_participants_to_conversation",
        rec.note("conversation"),
    )
    monkeypatch.setattr(
        flows.conference_flows, "add_conference_participant", rec.note("conference")
    )
    monkeypatch.setattr(
        flows.conference_flows, "remove_conference_participant", rec.note("conference")
    )
    monkeypatch.setattr(flows.canvas_flows, "update_participants_canvas", rec.note("canvas"))
    monkeypatch.setattr(flows, "send_participant_announcement_message", rec.note("announcement"))
    monkeypatch.setattr(flows, "send_incident_welcome_participant_messages", rec.note("welcome"))
    monkeypatch.setattr(flows.participant_flows, "remove_participant", rec.note("participant"))
    return rec


@pytest.fixture
def active_incident(session, incident):
    """An open incident with a conference and a conversation plugin enabled.

    The conversation plugin matters: without one the add flow takes a
    pre-existing early `return` that skips the welcome messages and returns
    None, which would mask what these tests are asserting.
    """
    from dispatch.incident.models import IncidentStatus

    incident.status = IncidentStatus.active
    incident.conference = ConferenceFactory(conference_id="meeting-123")
    PluginInstanceFactory(
        project=incident.project,
        plugin=PluginFactory(type="conversation", slug="test-conversation-participants"),
        enabled=True,
    )
    return incident


# --- add / reactivate -------------------------------------------------------


def test_adding_a_participant_updates_the_conference(session, active_incident, recorder):
    flows.incident_add_or_reactivate_participant_flow(
        user_email="responder@example.com",
        incident_id=active_incident.id,
        participant_role=ParticipantRoleType.observer,
        db_session=session,
    )

    assert "conference" in recorder.calls


def test_the_conference_is_updated_after_the_group_and_the_conversation(
    session, active_incident, recorder
):
    """Ordering is the design: the load-bearing integrations go first."""
    flows.incident_add_or_reactivate_participant_flow(
        user_email="responder@example.com",
        incident_id=active_incident.id,
        db_session=session,
    )

    assert recorder.calls.index("group") < recorder.calls.index("conference")
    assert recorder.calls.index("conversation") < recorder.calls.index("conference")


def test_the_conference_gets_the_participant_and_the_incident(
    session, active_incident, monkeypatch
):
    seen = {}

    def capture(incident, participant_email, db_session):
        seen["incident"] = incident
        seen["participant_email"] = participant_email

    monkeypatch.setattr(flows.group_flows, "update_group", lambda **kw: None)
    monkeypatch.setattr(
        flows.conversation_flows, "add_incident_participants_to_conversation", lambda **kw: None
    )
    monkeypatch.setattr(flows.canvas_flows, "update_participants_canvas", lambda **kw: None)
    monkeypatch.setattr(flows, "send_participant_announcement_message", lambda **kw: None)
    monkeypatch.setattr(flows, "send_incident_welcome_participant_messages", lambda *a: None)
    monkeypatch.setattr(flows.conference_flows, "add_conference_participant", capture)

    flows.incident_add_or_reactivate_participant_flow(
        user_email="responder@example.com",
        incident_id=active_incident.id,
        db_session=session,
    )

    assert seen["participant_email"] == "responder@example.com"
    assert seen["incident"].id == active_incident.id


def test_a_conference_failure_does_not_abort_the_add_flow(
    session, active_incident, recorder, monkeypatch
):
    """The whole point of issue #106's open question.

    The helper contains its own failures, but a bug there must not become a lost
    participant, so the flow is proven to survive a raising conference call too.
    """
    monkeypatch.setattr(
        flows.conference_flows,
        "add_conference_participant",
        recorder.fail("conference", RuntimeError("Graph said no")),
    )

    participant = flows.incident_add_or_reactivate_participant_flow(
        user_email="responder@example.com",
        incident_id=active_incident.id,
        db_session=session,
    )

    assert participant is not None
    assert "group" in recorder.calls
    assert "conversation" in recorder.calls


def test_the_participant_survives_a_conference_failure(
    session, active_incident, recorder, monkeypatch
):
    """A returned object is not proof; the participant must really be attached."""
    from dispatch.participant import service as participant_service

    monkeypatch.setattr(
        flows.conference_flows,
        "add_conference_participant",
        recorder.fail("conference", RuntimeError("Graph said no")),
    )

    flows.incident_add_or_reactivate_participant_flow(
        user_email="responder@example.com",
        incident_id=active_incident.id,
        db_session=session,
    )

    assert participant_service.get_by_incident_id_and_email(
        db_session=session, incident_id=active_incident.id, email="responder@example.com"
    )


def test_the_welcome_messages_still_go_out_after_a_conference_failure(
    session, active_incident, recorder, monkeypatch
):
    """Everything downstream of the conference call must still run."""
    monkeypatch.setattr(
        flows.conference_flows,
        "add_conference_participant",
        recorder.fail("conference", RuntimeError("Graph said no")),
    )

    flows.incident_add_or_reactivate_participant_flow(
        user_email="responder@example.com",
        incident_id=active_incident.id,
        db_session=session,
    )

    assert "welcome" in recorder.calls


def test_a_group_failure_still_aborts_the_add_flow(session, active_incident, recorder, monkeypatch):
    """Guards the scope of the new handling: it must not swallow this.

    A broad try/except around the fan-out would turn a lost tactical group into
    a silent success, which is strictly worse than the bug being fixed.
    """
    monkeypatch.setattr(
        flows.group_flows,
        "update_group",
        recorder.fail("group", RuntimeError("group is down")),
    )

    with pytest.raises(RuntimeError, match="group is down"):
        flows.incident_add_or_reactivate_participant_flow(
            user_email="responder@example.com",
            incident_id=active_incident.id,
            db_session=session,
        )


def test_a_conversation_failure_still_aborts_the_add_flow(
    session, active_incident, recorder, monkeypatch
):
    monkeypatch.setattr(
        flows.conversation_flows,
        "add_incident_participants_to_conversation",
        recorder.fail("conversation", RuntimeError("slack is down")),
    )

    with pytest.raises(RuntimeError, match="slack is down"):
        flows.incident_add_or_reactivate_participant_flow(
            user_email="responder@example.com",
            incident_id=active_incident.id,
            db_session=session,
        )


def test_a_closed_incident_does_not_get_a_conference_update(session, incident, recorder):
    """The conference call belongs inside the existing closed-incident guard."""
    from dispatch.incident.models import IncidentStatus

    incident.status = IncidentStatus.closed
    incident.conference = ConferenceFactory(conference_id="meeting-123")

    flows.incident_add_or_reactivate_participant_flow(
        user_email="responder@example.com",
        incident_id=incident.id,
        db_session=session,
    )

    assert "conference" not in recorder.calls


# --- remove -----------------------------------------------------------------


def test_removing_a_participant_updates_the_conference(session, active_incident, recorder):
    flows.incident_remove_participant_flow(
        user_email="responder@example.com",
        incident_id=active_incident.id,
        db_session=session,
    )

    assert "conference" in recorder.calls


def test_the_conference_removal_follows_the_group_removal(session, active_incident, recorder):
    flows.incident_remove_participant_flow(
        user_email="responder@example.com",
        incident_id=active_incident.id,
        db_session=session,
    )

    assert recorder.calls.index("group") < recorder.calls.index("conference")


def test_a_conference_failure_does_not_abort_the_remove_flow(
    session, active_incident, recorder, monkeypatch
):
    monkeypatch.setattr(
        flows.conference_flows,
        "remove_conference_participant",
        recorder.fail("conference", RuntimeError("Graph said no")),
    )

    flows.incident_remove_participant_flow(
        user_email="responder@example.com",
        incident_id=active_incident.id,
        db_session=session,
    )

    # The canvas update runs *after* the conference call. Asserting only on the
    # group and participant removals would pass even if the failure aborted
    # everything downstream, since both already ran by then.
    assert "canvas" in recorder.calls
    assert "group" in recorder.calls
    assert "participant" in recorder.calls


def test_the_conference_is_updated_even_without_a_conversation_plugin(session, incident, recorder):
    """The removal flow returns early when no conversation plugin is enabled.

    Placing the conference call after that guard would skip it entirely on any
    deployment without Slack -- a silent, environment-dependent no-op. Uses the
    bare `incident` fixture rather than `active_incident` precisely because the
    latter registers a conversation plugin, which would keep the early return
    this test exists to get past from ever being taken.
    """
    from dispatch.incident.models import IncidentStatus

    incident.status = IncidentStatus.active
    incident.conference = ConferenceFactory(conference_id="meeting-123")

    flows.incident_remove_participant_flow(
        user_email="responder@example.com",
        incident_id=incident.id,
        db_session=session,
    )

    assert "conference" in recorder.calls


def test_a_group_failure_still_aborts_the_remove_flow(
    session, active_incident, recorder, monkeypatch
):
    monkeypatch.setattr(
        flows.group_flows,
        "update_group",
        recorder.fail("group", RuntimeError("group is down")),
    )

    with pytest.raises(RuntimeError, match="group is down"):
        flows.incident_remove_participant_flow(
            user_email="responder@example.com",
            incident_id=active_incident.id,
            db_session=session,
        )

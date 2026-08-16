"""The Teams plugin driven through ``dispatch.conference.flows``.

This is the path an incident actually takes. It is also the only place that
shows what the operator sees when the plugin fails: issue #81's complaint was
that a broad ``except`` in the plugin turned every cause into the generic
"plugin encountered an error" line, which is logged nowhere the responder looks.
"""

from types import SimpleNamespace

import pytest

from tests.plugins.dispatch_microsoft_teams.graph_fake import (
    JOIN_MEETING_ID,
    JOIN_URL,
    MEETING_ID,
    PASSCODE,
    USER_ID,
)


@pytest.fixture
def active_teams_plugin(monkeypatch, teams_plugin):
    """Make the conference flow pick up our configured Teams plugin."""
    instance = SimpleNamespace(
        instance=teams_plugin,
        plugin=SimpleNamespace(
            slug="microsoft-teams-conference",
            title="Microsoft Teams Plugin - Conference Management",
        ),
    )
    monkeypatch.setattr(
        "dispatch.conference.flows.plugin_service.get_active_instance",
        lambda **kwargs: instance,
    )
    return instance


def test_a_successful_create_stores_the_conference_on_the_incident(
    graph, session, incident, active_teams_plugin
):
    from dispatch.conference.flows import create_conference

    conference = create_conference(incident=incident, participants=[], db_session=session)

    assert conference is not None
    assert conference.weblink == JOIN_URL
    assert conference.conference_id == MEETING_ID
    assert conference.conference_challenge == f"{PASSCODE} (meeting ID {JOIN_MEETING_ID})"
    assert incident.conference == conference


def test_the_incident_event_log_records_why_teams_failed(
    graph, session, incident, active_teams_plugin
):
    """The reason must survive all the way to the incident timeline."""
    from dispatch.conference.flows import create_conference

    graph.meeting = (
        403,
        {"error": {"code": "Forbidden", "message": "Application access policy is missing."}},
        {},
    )

    assert create_conference(incident=incident, participants=[], db_session=session) is None

    descriptions = [event.description for event in incident.events]
    assert any("Application access policy is missing." in d for d in descriptions), descriptions


def test_an_authentication_failure_is_named_in_the_incident_event_log(
    graph, session, incident, active_teams_plugin
):
    from dispatch.conference.flows import create_conference

    graph.token = (
        401,
        {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret."},
        {},
    )

    create_conference(incident=incident, participants=[], db_session=session)

    descriptions = " ".join(event.description for event in incident.events)
    assert "AADSTS7000215" in descriptions


def test_a_failed_create_leaves_no_conference_attached(
    graph, session, incident, active_teams_plugin
):
    from dispatch.conference.flows import create_conference

    graph.meeting = (500, {"error": {"code": "InternalServerError", "message": "Try later."}}, {})

    create_conference(incident=incident, participants=[], db_session=session)

    assert incident.conference is None


# --- compensating cleanup (issue #114) --------------------------------------
#
# The generic lifecycle lives in `tests/conference/test_conference_create_flow.py`
# and runs against a recording plugin. These tests exist because the id the flow
# deletes by has to be the one *Graph* understands, and only the real client and
# the real URL can show that. They assert on the request that reached the fake
# transport, not on a call to the plugin.


def deleted_meeting_paths(graph) -> list[str]:
    """The full paths Graph was asked to DELETE, read off the wire.

    The whole path, not the trailing segment: an id interpolated into the wrong
    route, or a delete aimed at a different user's meetings, would both pass a
    check that only compared the last component.
    """
    return [request.path for request in graph.graph_requests() if request.method == "DELETE"]


def meeting_path(meeting_id: str) -> str:
    return f"/v1.0/users/{USER_ID}/onlineMeetings/{meeting_id}"


def test_a_successful_create_deletes_nothing(graph, session, incident, active_teams_plugin):
    from dispatch.conference.flows import create_conference

    create_conference(incident=incident, participants=[], db_session=session)

    assert deleted_meeting_paths(graph) == []


def test_a_meeting_graph_created_without_a_join_url_is_deleted(
    graph, session, incident, active_teams_plugin
):
    """Graph accepted the meeting; the plugin then found it unusable.

    Without the delete this is a live Teams meeting with no `Conference` row,
    and `incident_delete_flow` has no way to reach it.
    """
    from dispatch.conference.flows import create_conference

    graph.meeting = (201, {"id": MEETING_ID}, {})

    assert create_conference(incident=incident, participants=[], db_session=session) is None

    assert deleted_meeting_paths(graph) == [meeting_path(MEETING_ID)]
    assert incident.conference is None


def test_a_meeting_dispatch_cannot_persist_is_deleted(
    graph, session, incident, active_teams_plugin, monkeypatch
):
    """Graph returned a perfectly good meeting and the database refused it."""
    from dispatch.conference.flows import create_conference

    def refuse(**kwargs):
        raise RuntimeError("the conference could not be persisted")

    monkeypatch.setattr("dispatch.conference.flows.create", refuse)

    with pytest.raises(RuntimeError):
        create_conference(incident=incident, participants=[], db_session=session)

    assert deleted_meeting_paths(graph) == [meeting_path(MEETING_ID)]
    assert incident.conference is None


# --- issue #156: source-to-sink -- an arbitrary Graph response must never
# reach the incident timeline, which is broadly readable by every incident
# participant. The request that fails carries a live bearer token, so a proxy,
# captive portal, or WAF answering in Graph's place could echo it straight
# into the timeline via the raw-body fallback this test guards against.

SECRET_CANARY = "TEST_SECRET_DO_NOT_LEAK_12345"


def test_a_reflected_secret_does_not_reach_the_incident_timeline(
    graph, session, incident, active_teams_plugin
):
    from dispatch.conference.flows import create_conference

    graph.meeting = (
        502,
        f"<html>upstream error\nAuthorization: Bearer {SECRET_CANARY}</html>".encode(),
        {},
    )

    assert create_conference(incident=incident, participants=[], db_session=session) is None

    descriptions = [event.description for event in incident.events]
    assert descriptions, "expected a timeline entry recording the failure"
    assert all(SECRET_CANARY not in d for d in descriptions), descriptions
    # The failure is still recognisable to a responder -- status is preserved.
    assert any("502" in d for d in descriptions), descriptions


def test_a_graph_delete_failure_does_not_replace_the_original_error(
    graph, session, incident, active_teams_plugin, monkeypatch
):
    from dispatch.conference.flows import create_conference

    graph.delete = (403, {"error": {"code": "Forbidden", "message": "no delete for you"}}, {})

    def refuse(**kwargs):
        raise RuntimeError("the conference could not be persisted")

    monkeypatch.setattr("dispatch.conference.flows.create", refuse)

    with pytest.raises(RuntimeError) as excinfo:
        create_conference(incident=incident, participants=[], db_session=session)

    assert "could not be persisted" in str(excinfo.value)
    assert deleted_meeting_paths(graph) == [meeting_path(MEETING_ID)]

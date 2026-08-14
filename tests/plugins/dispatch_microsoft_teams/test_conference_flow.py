"""The Teams plugin driven through ``dispatch.conference.flows``.

This is the path an incident actually takes. It is also the only place that
shows what the operator sees when the plugin fails: issue #81's complaint was
that a broad ``except`` in the plugin turned every cause into the generic
"plugin encountered an error" line, which is logged nowhere the responder looks.
"""

from types import SimpleNamespace

import pytest

from tests.plugins.dispatch_microsoft_teams.graph_fake import JOIN_URL, MEETING_ID


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
    assert conference.conference_challenge == "aB3dEf7h"
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

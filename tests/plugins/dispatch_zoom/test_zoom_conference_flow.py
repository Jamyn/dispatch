"""The Zoom plugin driven through ``dispatch.conference.flows`` (issue #114).

The generic lifecycle lives in ``tests/conference/test_conference_create_flow.py``
and runs against a recording plugin. This module exists because the id the flow
deletes by has to be the one *Zoom* understands, and only the real client and
the real URL can show that. The assertions read the request that reached the
fake transport rather than a call to the plugin.

Zoom is the parity baseline for Teams; the same four cases exist in
``tests/plugins/dispatch_microsoft_teams/test_conference_flow.py``.
"""

from types import SimpleNamespace

import pytest

from tests.plugins.dispatch_zoom.conftest import JOIN_URL, MEETING_ID


@pytest.fixture
def active_zoom_plugin(monkeypatch, zoom_plugin):
    """Make the conference flow pick up our configured Zoom plugin."""
    instance = SimpleNamespace(
        instance=zoom_plugin,
        plugin=SimpleNamespace(
            slug="zoom-conference",
            title="Zoom Plugin - Conference Management",
        ),
    )
    monkeypatch.setattr(
        "dispatch.conference.flows.plugin_service.get_active_instance",
        lambda **kwargs: instance,
    )
    return instance


def deleted_meeting_urls(zoom) -> list[str]:
    """The full URLs Zoom was asked to DELETE, read off the wire.

    The whole URL, not the trailing segment: an id interpolated into the wrong
    route would pass a check that only compared the last component.
    """
    return [request.url for request in zoom.api_requests() if request.method == "DELETE"]


def meeting_url(meeting_id: str) -> str:
    return f"https://api.zoom.us/v2/meetings/{meeting_id}"


def test_a_successful_create_stores_the_conference_and_deletes_nothing(
    zoom, session, incident, active_zoom_plugin
):
    from dispatch.conference.flows import create_conference

    conference = create_conference(incident=incident, participants=[], db_session=session)

    assert conference is not None
    assert conference.weblink == JOIN_URL
    # Zoom sends the id as a JSON number; the plugin hands back a string.
    assert conference.conference_id == MEETING_ID
    assert incident.conference is conference
    assert deleted_meeting_urls(zoom) == []


def test_a_meeting_zoom_created_without_a_join_url_is_deleted(
    zoom, session, incident, active_zoom_plugin
):
    """Zoom accepted the meeting; the plugin then found it unusable.

    Without the delete this is a live Zoom meeting with no `Conference` row, and
    `incident_delete_flow` has no way to reach it.
    """
    from dispatch.conference.flows import create_conference

    zoom.response = (201, {"id": 987654321, "password": "zoompass"})

    assert create_conference(incident=incident, participants=[], db_session=session) is None

    assert deleted_meeting_urls(zoom) == [meeting_url(MEETING_ID)]
    assert incident.conference is None


def test_a_meeting_dispatch_cannot_persist_is_deleted(
    zoom, session, incident, active_zoom_plugin, monkeypatch
):
    """Zoom returned a perfectly good meeting and the database refused it."""
    from dispatch.conference.flows import create_conference

    def refuse(**kwargs):
        raise RuntimeError("the conference could not be persisted")

    monkeypatch.setattr("dispatch.conference.flows.create", refuse)

    with pytest.raises(RuntimeError):
        create_conference(incident=incident, participants=[], db_session=session)

    assert deleted_meeting_urls(zoom) == [meeting_url(MEETING_ID)]
    assert incident.conference is None


def test_a_zoom_delete_failure_does_not_replace_the_original_error(
    zoom, session, incident, active_zoom_plugin, monkeypatch
):
    from dispatch.conference.flows import create_conference

    # Only the DELETE fails; the create still has to succeed, or there would be
    # no meeting to compensate for.
    zoom.delete = (403, {"message": "no delete for you"})

    def refuse(**kwargs):
        raise RuntimeError("the conference could not be persisted")

    monkeypatch.setattr("dispatch.conference.flows.create", refuse)

    with pytest.raises(RuntimeError) as excinfo:
        create_conference(incident=incident, participants=[], db_session=session)

    assert "could not be persisted" in str(excinfo.value)
    assert deleted_meeting_urls(zoom) == [meeting_url(MEETING_ID)]

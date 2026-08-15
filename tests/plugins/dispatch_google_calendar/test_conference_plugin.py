"""The Google Calendar conference plugin's post-create contract (issue #114).

Google attaches the Meet conference asynchronously -- ``create_event`` carries
an upstream TODO saying so -- and reports
``conferenceData.createRequest.status.statusCode == "pending"`` with no
``entryPoints`` while it does. The event exists and is joinable by then, so a
plugin that raises a bare ``KeyError`` there strands a live bridge with no
``Conference`` row and no way for ``incident_delete_flow`` to reach it.

This plugin is the reason the contract lives on ``ConferencePlugin`` rather than
in Zoom and Teams alone: it was the shipped plugin that still had the hole after
those two were fixed.
"""

from types import SimpleNamespace

import pytest

from dispatch.exceptions import ConferenceCreatedButUnusable
from dispatch.plugins.dispatch_google.calendar.plugin import GoogleCalendarConferencePlugin

EVENT_ID = "abcdef1234567890"
MEET_URL = "https://meet.google.com/abc-defg-hij"


@pytest.fixture
def calendar_plugin(monkeypatch):
    """The real plugin with the Google client swapped for a canned response.

    ``get_service`` builds an authenticated googleapiclient; only the response
    shape matters here, so ``create_event`` is what gets replaced.
    """
    plugin = GoogleCalendarConferencePlugin()
    plugin.configuration = SimpleNamespace(default_duration_minutes=60)
    monkeypatch.setattr(
        "dispatch.plugins.dispatch_google.calendar.plugin.get_service",
        lambda *args, **kwargs: object(),
    )
    return plugin


def respond_with(monkeypatch, event: dict):
    monkeypatch.setattr(
        "dispatch.plugins.dispatch_google.calendar.plugin.create_event",
        lambda *args, **kwargs: event,
    )


def complete_event(**overrides) -> dict:
    return {
        "id": EVENT_ID,
        "conferenceData": {
            "entryPoints": [
                {"entryPointType": "more", "uri": "https://tel.meet/abc"},
                {"entryPointType": "video", "uri": MEET_URL},
            ]
        },
        **overrides,
    }


def test_a_complete_event_yields_the_video_entry_point(calendar_plugin, monkeypatch):
    respond_with(monkeypatch, complete_event())

    conference = calendar_plugin.create("dispatch-incident-1")

    assert conference == {"weblink": MEET_URL, "id": EVENT_ID, "challenge": ""}


def test_an_event_google_has_not_attached_a_meet_to_carries_its_id_for_cleanup(
    calendar_plugin, monkeypatch
):
    """The pending-conference case, which the plugin's own TODO says is real."""
    respond_with(
        monkeypatch,
        {
            "id": EVENT_ID,
            "conferenceData": {"createRequest": {"status": {"statusCode": "pending"}}},
        },
    )

    with pytest.raises(ConferenceCreatedButUnusable) as excinfo:
        calendar_plugin.create("dispatch-incident-1")

    assert excinfo.value.resource_id == EVENT_ID


def test_an_event_with_no_conference_data_at_all_carries_its_id_for_cleanup(
    calendar_plugin, monkeypatch
):
    respond_with(monkeypatch, {"id": EVENT_ID})

    with pytest.raises(ConferenceCreatedButUnusable) as excinfo:
        calendar_plugin.create("dispatch-incident-1")

    assert excinfo.value.resource_id == EVENT_ID


def test_an_event_with_no_video_entry_point_carries_its_id_for_cleanup(
    calendar_plugin, monkeypatch
):
    """Dial-in only. The incident bridge is a video link, so this is unusable."""
    respond_with(
        monkeypatch,
        {
            "id": EVENT_ID,
            "conferenceData": {"entryPoints": [{"entryPointType": "phone", "uri": "tel:+1"}]},
        },
    )

    with pytest.raises(ConferenceCreatedButUnusable) as excinfo:
        calendar_plugin.create("dispatch-incident-1")

    assert excinfo.value.resource_id == EVENT_ID


def test_an_event_with_no_id_still_reports_a_meeting_may_have_leaked(calendar_plugin, monkeypatch):
    """Nothing safe to delete by, so the log is the only trace it will get.

    Never falls back to the summary or the htmlLink: neither is unique, and
    either could name a different incident's event.
    """
    respond_with(monkeypatch, {"conferenceData": {"entryPoints": []}})

    with pytest.raises(ConferenceCreatedButUnusable) as excinfo:
        calendar_plugin.create("dispatch-incident-1")

    assert excinfo.value.resource_id is None


def test_the_failure_message_does_not_quote_the_event_body(calendar_plugin, monkeypatch):
    """It reaches the incident timeline, and the body carries the attendee list."""
    respond_with(
        monkeypatch,
        {"id": EVENT_ID, "attendees": [{"email": "responder@example.com"}], "conferenceData": {}},
    )

    with pytest.raises(ConferenceCreatedButUnusable) as excinfo:
        calendar_plugin.create("dispatch-incident-1")

    assert "responder@example.com" not in str(excinfo.value)

"""A Zoom delete answered 404 means the meeting is already gone (issue #120).

``check`` collapses every non-2xx into ``DispatchPluginException`` with the
status only in the message text, so the flow layer could not tell "the bridge
leaked" from "the bridge was never there" without parsing that string. These
tests pin the structural signal instead.

The conversion is scoped to deletes, and the last test is why: Zoom answers 404
to a *create* naming an ``api_user_id`` it cannot resolve, which is a real
failure and must not be reported as an already-deleted meeting.
"""

import pytest

from dispatch.exceptions import ConferenceAlreadyGone, DispatchPluginException

MEETING_ID = "987654321"

# Zoom's real not-found shape for a meeting; 3001 is "Meeting does not exist".
MEETING_NOT_FOUND = (404, {"code": 3001, "message": f"Meeting does not exist: {MEETING_ID}."})


def test_a_delete_zoom_answers_404_reports_the_meeting_as_already_gone(zoom, zoom_plugin):
    zoom.delete = MEETING_NOT_FOUND

    with pytest.raises(ConferenceAlreadyGone):
        zoom_plugin.delete(MEETING_ID)


def test_already_gone_is_still_a_plugin_exception(zoom, zoom_plugin):
    """Subclassing is what keeps every caller that does not opt in unchanged."""
    zoom.delete = MEETING_NOT_FOUND

    with pytest.raises(DispatchPluginException):
        zoom_plugin.delete(MEETING_ID)


def test_the_meeting_id_is_not_repeated_from_zooms_message(zoom, zoom_plugin):
    """Only Dispatch's own wording, for the same reason ``check`` gives.

    The raw body is never echoed: the request carries a live bearer token and an
    intermediary answering in Zoom's place may quote it back.
    """
    zoom.delete = (404, {"code": 3001, "message": "Meeting does not exist. Bearer leaked-token"})

    with pytest.raises(ConferenceAlreadyGone) as excinfo:
        zoom_plugin.delete(MEETING_ID)

    assert "leaked-token" not in str(excinfo.value)


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_any_other_delete_failure_is_still_an_ordinary_failure(zoom, zoom_plugin, status):
    zoom.delete = (status, {"code": 3002, "message": "Meeting is in progress."})

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.delete(MEETING_ID)

    # `not isinstance`, not `type(...) is`: the point is that the flow's
    # already-gone handler does not catch this, not which class it is.
    assert not isinstance(excinfo.value, ConferenceAlreadyGone)


def test_a_404_on_a_create_is_not_reclassified(zoom, zoom_plugin):
    """1001 is "user does not exist" -- the create failed, nothing was deleted."""
    zoom.response = (404, {"code": 1001, "message": "User does not exist: responder@example.com."})

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.create("incident-1")

    assert not isinstance(excinfo.value, ConferenceAlreadyGone)


def test_a_404_on_a_roster_read_is_not_reclassified(zoom, zoom_plugin):
    """The read half of ``add_participant``: nothing was deleted here either."""
    zoom.get = MEETING_NOT_FOUND

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert not isinstance(excinfo.value, ConferenceAlreadyGone)

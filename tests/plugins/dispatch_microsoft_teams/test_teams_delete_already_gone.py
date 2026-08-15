"""A Graph delete answered 404 means the meeting is already gone (issue #120).

``_request`` collapses every non-2xx into ``DispatchPluginException`` with the
status only in the message text, so the flow layer could not tell "the bridge
leaked" from "the bridge was never there" without parsing that string. These
tests pin the structural signal instead.

Scoped to deletes: the read half of the attendee read-modify-write can 404 too,
and there nothing has been deleted.
"""

import pytest

from dispatch.exceptions import ConferenceAlreadyGone, DispatchPluginException

from tests.plugins.dispatch_microsoft_teams.graph_fake import MEETING_ID

NOT_FOUND_BODY = {"error": {"code": "NotFound", "message": "The requested meeting was not found."}}


def test_a_delete_graph_answers_404_reports_the_meeting_as_already_gone(graph, teams_plugin):
    graph.delete = (404, NOT_FOUND_BODY, {})

    with pytest.raises(ConferenceAlreadyGone):
        teams_plugin.delete(MEETING_ID)


def test_already_gone_is_still_a_plugin_exception(graph, teams_plugin):
    """Subclassing is what keeps every caller that does not opt in unchanged."""
    graph.delete = (404, NOT_FOUND_BODY, {})

    with pytest.raises(DispatchPluginException):
        teams_plugin.delete(MEETING_ID)


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
def test_any_other_delete_failure_is_still_an_ordinary_failure(graph, teams_plugin, status):
    graph.delete = (status, {"error": {"code": "Boom", "message": "no"}}, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        teams_plugin.delete(MEETING_ID)

    assert not isinstance(excinfo.value, ConferenceAlreadyGone)


def test_a_404_on_a_roster_read_is_not_reclassified(graph, teams_plugin):
    """``add_participant`` reads before it writes, and a failed read deleted nothing."""
    graph.get = (404, NOT_FOUND_BODY, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert not isinstance(excinfo.value, ConferenceAlreadyGone)

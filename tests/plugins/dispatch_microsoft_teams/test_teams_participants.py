"""Attendee add/remove for the Microsoft Teams conference plugin (issue #106).

This is roster metadata, not access control. A Teams ``joinWebUrl`` is joinable
by anyone holding it; adding an attendee does not grant access and removing one
does not evict them. The tests below assert what we *send*, which is the only
thing a fake can prove -- ``test_teams_live.py`` covers what Graph accepts.

The central constraint, straight from the Graph reference for
``PATCH /users/{id}/onlineMeetings/{id}``: adjusting the ``attendees`` field
"always requires the full list of attendees in the request body". There is no
incremental form, so every one of these operations is a read-modify-write and
the danger is truncating the list rather than extending it.
"""

import pytest

from dispatch.exceptions import DispatchPluginException

from tests.plugins.dispatch_microsoft_teams.graph_fake import (
    MEETING_ID,
    USER_ID,
    attendee,
)


def upns(request) -> list[str]:
    return [a["upn"] for a in request.json["participants"]["attendees"]]


# --- add: the request itself ------------------------------------------------


def test_add_participant_patches_the_meeting(graph, teams_plugin):
    graph.get = (200, graph.meeting_with_attendees(), {})

    teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    request = graph.last_graph_request()
    assert request.method == "PATCH"
    assert (
        request.url
        == f"https://graph.microsoft.com/v1.0/users/{USER_ID}/onlineMeetings/{MEETING_ID}"
    )


def test_add_participant_reads_the_meeting_before_patching_it(graph, teams_plugin):
    """Graph demands the full attendee list, so the current one must be read."""
    graph.get = (200, graph.meeting_with_attendees(), {})

    teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    methods = [r.method for r in graph.graph_requests()]
    assert methods == ["GET", "PATCH"]


def test_add_participant_sends_the_participant_as_an_attendee(graph, teams_plugin):
    graph.get = (200, graph.meeting_with_attendees(), {})

    teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    attendees = graph.last_graph_request().json["participants"]["attendees"]
    assert attendees == [{"upn": "responder@example.com", "role": "attendee"}]


def test_an_added_participant_is_only_ever_an_attendee(graph, teams_plugin):
    """``presenter``/``coorganizer`` are unsupported for non-Entra identities.

    Responders are ordinary humans with an email address, some of them external,
    so the only role that is safe to assign unconditionally is ``attendee``.
    """
    graph.get = (200, graph.meeting_with_attendees(), {})

    teams_plugin.add_participant(MEETING_ID, "external@partner.example")

    roles = {a["role"] for a in graph.last_graph_request().json["participants"]["attendees"]}
    assert roles == {"attendee"}


# --- add: existing attendees survive ----------------------------------------


def test_add_participant_preserves_the_existing_attendees(graph, teams_plugin):
    """The whole list is replaced on every PATCH; dropping one is silent."""
    graph.get = (
        200,
        graph.meeting_with_attendees(attendee("first@example.com"), attendee("second@example.com")),
        {},
    )

    teams_plugin.add_participant(MEETING_ID, "third@example.com")

    assert upns(graph.last_graph_request()) == [
        "first@example.com",
        "second@example.com",
        "third@example.com",
    ]


def test_add_participant_preserves_an_existing_attendees_role(graph, teams_plugin):
    """A presenter must not be demoted by someone else joining the incident."""
    graph.get = (
        200,
        graph.meeting_with_attendees(attendee("lead@example.com", role="presenter")),
        {},
    )

    teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    attendees = graph.last_graph_request().json["participants"]["attendees"]
    assert attendees[0]["role"] == "presenter"


def test_add_participant_preserves_the_identity_graph_resolved(graph, teams_plugin):
    """Graph resolves ``upn`` to an identity; re-sending the upn alone loses it."""
    graph.get = (200, graph.meeting_with_attendees(attendee("lead@example.com")), {})

    teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    attendees = graph.last_graph_request().json["participants"]["attendees"]
    assert attendees[0]["identity"] == {
        "user": {"id": "id-for-lead@example.com", "displayName": "lead@example.com"}
    }


def test_the_patch_body_carries_no_organizer(graph, teams_plugin):
    """Graph refuses to update `organizer`, so it must not be echoed back.

    Asserting the organizer is missing from the *attendee* list would pass
    vacuously -- it is read from `participants.organizer`, which the plugin never
    reads. This asserts on the key actually at risk of being resent.
    """
    graph.get = (200, graph.meeting_with_attendees(), {})

    teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    participants = graph.last_graph_request().json["participants"]
    assert set(participants) == {"attendees"}


# --- add: idempotency -------------------------------------------------------


def test_adding_an_already_present_participant_sends_no_patch(graph, teams_plugin):
    """Participant flows retry; a no-op must not cost a write."""
    graph.get = (200, graph.meeting_with_attendees(attendee("responder@example.com")), {})

    teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert [r.method for r in graph.graph_requests()] == ["GET"]


def test_adding_an_already_present_participant_does_not_duplicate_them(graph, teams_plugin):
    graph.get = (200, graph.meeting_with_attendees(attendee("responder@example.com")), {})

    teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert graph.get[1]["participants"]["attendees"] == [attendee("responder@example.com")]


def test_a_participant_already_present_under_another_casing_is_not_re_added(graph, teams_plugin):
    """UPNs are case-insensitive, so a casing difference is the same person."""
    graph.get = (200, graph.meeting_with_attendees(attendee("Responder@Example.com")), {})

    teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert [r.method for r in graph.graph_requests()] == ["GET"]


# --- remove -----------------------------------------------------------------


def test_remove_participant_patches_the_meeting(graph, teams_plugin):
    graph.get = (200, graph.meeting_with_attendees(attendee("responder@example.com")), {})

    teams_plugin.remove_participant(MEETING_ID, "responder@example.com")

    request = graph.last_graph_request()
    assert request.method == "PATCH"
    assert (
        request.url
        == f"https://graph.microsoft.com/v1.0/users/{USER_ID}/onlineMeetings/{MEETING_ID}"
    )


def test_remove_participant_drops_only_that_participant(graph, teams_plugin):
    graph.get = (
        200,
        graph.meeting_with_attendees(
            attendee("first@example.com"),
            attendee("leaving@example.com"),
            attendee("third@example.com"),
        ),
        {},
    )

    teams_plugin.remove_participant(MEETING_ID, "leaving@example.com")

    assert upns(graph.last_graph_request()) == ["first@example.com", "third@example.com"]


def test_removing_the_last_attendee_sends_an_empty_list(graph, teams_plugin):
    """Not a missing key: omitting ``attendees`` leaves them in place."""
    graph.get = (200, graph.meeting_with_attendees(attendee("responder@example.com")), {})

    teams_plugin.remove_participant(MEETING_ID, "responder@example.com")

    assert graph.last_graph_request().json["participants"]["attendees"] == []


def test_remove_participant_matches_case_insensitively(graph, teams_plugin):
    graph.get = (200, graph.meeting_with_attendees(attendee("Responder@Example.com")), {})

    teams_plugin.remove_participant(MEETING_ID, "responder@example.com")

    assert graph.last_graph_request().json["participants"]["attendees"] == []


def test_remove_participant_preserves_the_other_attendees_roles(graph, teams_plugin):
    graph.get = (
        200,
        graph.meeting_with_attendees(
            attendee("lead@example.com", role="presenter"),
            attendee("leaving@example.com"),
        ),
        {},
    )

    teams_plugin.remove_participant(MEETING_ID, "leaving@example.com")

    attendees = graph.last_graph_request().json["participants"]["attendees"]
    assert attendees == [attendee("lead@example.com", role="presenter")]


def test_removing_an_absent_participant_sends_no_patch(graph, teams_plugin):
    """Removal is retried after a partial failure; absence is success."""
    graph.get = (200, graph.meeting_with_attendees(attendee("someone@example.com")), {})

    teams_plugin.remove_participant(MEETING_ID, "notthere@example.com")

    assert [r.method for r in graph.graph_requests()] == ["GET"]


def test_removing_from_a_meeting_with_no_attendees_sends_no_patch(graph, teams_plugin):
    graph.get = (200, graph.meeting_with_attendees(), {})

    teams_plugin.remove_participant(MEETING_ID, "notthere@example.com")

    assert [r.method for r in graph.graph_requests()] == ["GET"]


# --- shapes Graph really returns --------------------------------------------


def test_a_meeting_with_no_participants_key_can_still_be_added_to(graph, teams_plugin):
    from tests.plugins.dispatch_microsoft_teams.graph_fake import MEETING_BODY

    graph.get = (200, MEETING_BODY, {})

    teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert upns(graph.last_graph_request()) == ["responder@example.com"]


def test_a_null_attendees_list_is_treated_as_empty(graph, teams_plugin):
    """Graph sends an explicit null rather than omitting the key."""
    graph.get = (
        200,
        {"id": MEETING_ID, "participants": {"organizer": {}, "attendees": None}},
        {},
    )

    teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert upns(graph.last_graph_request()) == ["responder@example.com"]


def test_an_attendee_without_a_upn_is_preserved_rather_than_dropped(graph, teams_plugin):
    """A phone/anonymous attendee has an identity but no upn."""
    anonymous = {"upn": None, "role": "attendee", "identity": {"phone": {"id": "+15550100"}}}
    graph.get = (200, graph.meeting_with_attendees(anonymous), {})

    teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    attendees = graph.last_graph_request().json["participants"]["attendees"]
    assert anonymous in attendees
    assert len(attendees) == 2


# --- failures ---------------------------------------------------------------


def test_a_failed_read_raises_and_sends_no_patch(graph, teams_plugin):
    """Patching a list we failed to read would erase every attendee."""
    graph.get = (403, {"error": {"code": "Forbidden", "message": "No access policy."}}, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert "No access policy." in str(excinfo.value)
    assert [r.method for r in graph.graph_requests()] == ["GET"]


def test_a_failed_add_raises(graph, teams_plugin):
    graph.get = (200, graph.meeting_with_attendees(), {})
    graph.patch = (400, {"error": {"code": "BadRequest", "message": "Invalid attendee."}}, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert "Invalid attendee." in str(excinfo.value)


def test_a_failed_remove_raises(graph, teams_plugin):
    graph.get = (200, graph.meeting_with_attendees(attendee("responder@example.com")), {})
    graph.patch = (404, {"error": {"code": "NotFound", "message": "Meeting not found."}}, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        teams_plugin.remove_participant(MEETING_ID, "responder@example.com")

    assert "404" in str(excinfo.value)


def test_a_failed_read_on_remove_raises_and_sends_no_patch(graph, teams_plugin):
    graph.get = (500, {"error": {"code": "InternalServerError", "message": "Try again."}}, {})

    with pytest.raises(DispatchPluginException):
        teams_plugin.remove_participant(MEETING_ID, "responder@example.com")

    assert [r.method for r in graph.graph_requests()] == ["GET"]


def test_participant_operations_are_instrumented(graph, teams_plugin, metrics):
    """``apply`` decorates the class; a method-level mistake emits nothing."""
    graph.get = (200, graph.meeting_with_attendees(), {})

    teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    counted = [tags["function"] for _, tags in metrics.counters]
    assert any("MicrosoftTeamsConferencePlugin.add_participant" in name for name in counted)


def test_the_participant_email_is_not_logged(graph, teams_plugin, caplog):
    """Incident rosters are PII; the timeline already records who was added."""
    import logging

    graph.get = (200, graph.meeting_with_attendees(), {})

    with caplog.at_level(logging.DEBUG):
        teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert "responder@example.com" not in caplog.text

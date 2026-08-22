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

from dispatch.exceptions import ConferenceRosterUnreadable, DispatchPluginException

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
    """The whole list is replaced on every PATCH; dropping one is silent.

    Also the defect's own scenario on the path where Graph answers: ``create``
    seeds the founding responders (issue #110) and the next one to join triggers
    an add. Refusing when Graph does *not* answer (issue #130) exists to protect
    this same outcome -- see the unreadable-shape section below.
    """
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


def test_an_empty_attendee_list_is_trusted(graph, teams_plugin):
    """``[]`` is Graph answering, and is the shape its own GET example reports.

    The distinction the rest of this section turns on: a meeting that really has
    no attendees must still be addable to, or every incident whose bridge was
    created without a roster could never gain one.
    """
    graph.get = (200, graph.meeting_with_attendees(), {})

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


# --- shapes that are not an answer (issue #130) ------------------------------
#
# Graph replaces `participants.attendees` wholesale, so a roster rebuilt from a
# read that reported nothing is a roster erased: the seeded founding responders
# (issue #110) would be replaced by whoever joined next. These pin that a
# non-answer is refused rather than read as "the meeting has no attendees".


UNREADABLE = {
    "no-participants-key": {"id": MEETING_ID},
    "participants-null": {"id": MEETING_ID, "participants": None},
    "participants-not-an-object": {"id": MEETING_ID, "participants": []},
    "attendees-key-absent": {"id": MEETING_ID, "participants": {"organizer": {}}},
    "attendees-null": {"id": MEETING_ID, "participants": {"organizer": {}, "attendees": None}},
    # `list("abc")` is `["a", "b", "c"]`, so the old code turned this into three
    # attendees and `matches` then raised AttributeError on `str.get`.
    "attendees-a-string": {"id": MEETING_ID, "participants": {"attendees": "responder@x"}},
    "attendees-a-number": {"id": MEETING_ID, "participants": {"attendees": 3}},
    "an-entry-that-is-not-an-object": {"id": MEETING_ID, "participants": {"attendees": ["a@b.c"]}},
    "an-entry-whose-upn-is-not-a-string": {
        "id": MEETING_ID,
        "participants": {"attendees": [{"upn": 42, "role": "attendee"}]},
    },
}


@pytest.mark.parametrize("body", list(UNREADABLE.values()), ids=list(UNREADABLE))
def test_a_roster_graph_did_not_report_is_refused_on_add(graph, teams_plugin, body):
    graph.get = (200, body, {})

    with pytest.raises(ConferenceRosterUnreadable):
        teams_plugin.add_participant(MEETING_ID, "responder@example.com")


@pytest.mark.parametrize("body", list(UNREADABLE.values()), ids=list(UNREADABLE))
def test_a_roster_graph_did_not_report_sends_no_patch_on_add(graph, teams_plugin, body):
    """The whole point: the PATCH that would have erased the roster never goes."""
    graph.get = (200, body, {})

    with pytest.raises(ConferenceRosterUnreadable):
        teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert [r.method for r in graph.graph_requests()] == ["GET"]


@pytest.mark.parametrize("body", list(UNREADABLE.values()), ids=list(UNREADABLE))
def test_a_roster_graph_did_not_report_is_refused_on_remove(graph, teams_plugin, body):
    """Remove rewrites the same wholesale list, so it refuses on the same reads."""
    graph.get = (200, body, {})

    with pytest.raises(ConferenceRosterUnreadable):
        teams_plugin.remove_participant(MEETING_ID, "responder@example.com")

    assert [r.method for r in graph.graph_requests()] == ["GET"]


def test_the_refusal_names_the_shape_graph_answered_with(graph, teams_plugin):
    """Four different non-answers reach one exception; the message separates them.

    In a deployment where this fires it is the only evidence of which one, and
    #130 is open precisely because no tenant has been read against.
    """
    graph.get = (200, UNREADABLE["attendees-null"], {})

    with pytest.raises(ConferenceRosterUnreadable) as excinfo:
        teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert "attendees was an explicit null" in str(excinfo.value)


def test_the_refusal_names_no_address(graph, teams_plugin):
    """It reaches the application log; the roster is the incident's responders."""
    graph.get = (
        200,
        {"id": MEETING_ID, "participants": {"attendees": [{"upn": 42}, "listed@example.com"]}},
        {},
    )

    with pytest.raises(ConferenceRosterUnreadable) as excinfo:
        teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    message = str(excinfo.value)
    assert "responder@example.com" not in message
    assert "listed@example.com" not in message
    assert "2 entries" in message


def test_the_refusal_says_the_roster_does_not_gate_joining(graph, teams_plugin):
    """Read by whoever is running the incident; "could not be added" invites the
    opposite reading. A Teams join link works for whoever holds it."""
    graph.get = (200, UNREADABLE["attendees-null"], {})

    with pytest.raises(ConferenceRosterUnreadable) as excinfo:
        teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert "does not control who can join" in str(excinfo.value)


def test_a_refusal_is_still_a_plugin_exception(graph, teams_plugin):
    """``ConferenceRosterUnreadable`` subclasses ``DispatchPluginException``, so a
    caller that has not opted into telling them apart keeps its old behaviour."""
    graph.get = (200, UNREADABLE["attendees-null"], {})

    with pytest.raises(DispatchPluginException):
        teams_plugin.add_participant(MEETING_ID, "responder@example.com")


def test_a_provider_error_is_not_reported_as_an_unreadable_roster(graph, teams_plugin):
    """A 403 is a failure and belongs on the timeline; a refusal does not."""
    graph.get = (403, {"error": {"code": "Forbidden", "message": "No access policy."}}, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        teams_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert not isinstance(excinfo.value, ConferenceRosterUnreadable)


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

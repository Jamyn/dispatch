"""Invitee add/remove for the Zoom conference plugin (issue #106).

Roster metadata, not access control: a Zoom ``join_url`` is joinable by anyone
holding it, so adding an invitee grants nothing and removing one evicts nobody.

Two limitations are recorded here rather than worked around, because both are
properties of Zoom rather than of this code:

- ``settings.meeting_invitees`` is reported by Zoom's own staff on the developer
  forum to be consumed only by their calendar integrations, so an invitee added
  through it may never appear in the Zoom client. The plugin sends what issue
  #106 specifies; whether Zoom acts on it is not something these tests can say.
- Whether a read reports the roster back is likewise unsettled: Zoom's API
  reference documents the field on the 200 of ``GET /meetings/{meetingId}``,
  and its staff have said invitees cannot be queried back (issue #129). Only
  ``test_zoom_live.py``'s write-gated round trip can decide it.

What the tests below *do* establish is that the request we build is the one the
API documents -- the right verb, the right path, and a complete invitee list --
and that the answer to that second question cannot cost anyone their place on
the roster either way, because a read that reports nothing is never treated as
a read that reported an empty list.
"""

import pytest

from dispatch.exceptions import ConferenceRosterUnreadable, DispatchPluginException

from tests.plugins.dispatch_zoom.conftest import MEETING_ID


def emails(request) -> list[str]:
    return [i["email"] for i in request.json["settings"]["meeting_invitees"]]


# --- add: the request itself ------------------------------------------------


def test_add_participant_patches_the_meeting(zoom, zoom_plugin):
    zoom.get = (200, zoom.meeting_with_invitees())

    zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    request = zoom.last_api_request()
    assert request.method == "PATCH"
    assert request.url == f"https://api.zoom.us/v2/meetings/{MEETING_ID}"


def test_add_participant_reads_the_meeting_before_patching_it(zoom, zoom_plugin):
    """``meeting_invitees`` is replaced wholesale, so it has to be read first."""
    zoom.get = (200, zoom.meeting_with_invitees())

    zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert [r.method for r in zoom.api_requests()] == ["GET", "PATCH"]


def test_add_participant_sends_the_invitee(zoom, zoom_plugin):
    zoom.get = (200, zoom.meeting_with_invitees())

    zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    invitees = zoom.last_api_request().json["settings"]["meeting_invitees"]
    assert invitees == [{"email": "responder@example.com"}]


def test_add_participant_sends_only_the_invitee_setting(zoom, zoom_plugin):
    """Resending the whole settings object would rewrite unrelated settings.

    A PATCH that echoes back everything read is a lost update waiting to happen:
    anything changed in the Zoom UI between the read and the write is reverted.
    """
    zoom.get = (200, zoom.meeting_with_invitees())

    zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    body = zoom.last_api_request().json
    assert body == {"settings": {"meeting_invitees": [{"email": "responder@example.com"}]}}


def test_the_add_request_has_a_timeout(zoom, zoom_plugin):
    zoom.get = (200, zoom.meeting_with_invitees())

    zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert all(r.timeout == 15 for r in zoom.requests)


# --- add: existing invitees survive -----------------------------------------


def test_add_participant_preserves_the_existing_invitees(zoom, zoom_plugin):
    zoom.get = (200, zoom.meeting_with_invitees("first@example.com", "second@example.com"))

    zoom_plugin.add_participant(MEETING_ID, "third@example.com")

    assert emails(zoom.last_api_request()) == [
        "first@example.com",
        "second@example.com",
        "third@example.com",
    ]


# --- add: idempotency -------------------------------------------------------


def test_adding_an_already_present_invitee_sends_no_patch(zoom, zoom_plugin):
    zoom.get = (200, zoom.meeting_with_invitees("responder@example.com"))

    zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert [r.method for r in zoom.api_requests()] == ["GET"]


def test_an_invitee_present_under_another_casing_is_not_re_added(zoom, zoom_plugin):
    zoom.get = (200, zoom.meeting_with_invitees("Responder@Example.com"))

    zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert [r.method for r in zoom.api_requests()] == ["GET"]


def test_a_duplicated_invitee_list_is_not_multiplied(zoom, zoom_plugin):
    """Zoom has been observed storing duplicates; adding must not compound it."""
    zoom.get = (200, zoom.meeting_with_invitees("dupe@example.com", "dupe@example.com"))

    zoom_plugin.add_participant(MEETING_ID, "dupe@example.com")

    assert [r.method for r in zoom.api_requests()] == ["GET"]


# --- remove -----------------------------------------------------------------


def test_remove_participant_patches_the_meeting(zoom, zoom_plugin):
    zoom.get = (200, zoom.meeting_with_invitees("responder@example.com"))

    zoom_plugin.remove_participant(MEETING_ID, "responder@example.com")

    request = zoom.last_api_request()
    assert request.method == "PATCH"
    assert request.url == f"https://api.zoom.us/v2/meetings/{MEETING_ID}"


def test_remove_participant_drops_only_that_invitee(zoom, zoom_plugin):
    zoom.get = (
        200,
        zoom.meeting_with_invitees("first@example.com", "leaving@example.com", "third@example.com"),
    )

    zoom_plugin.remove_participant(MEETING_ID, "leaving@example.com")

    assert emails(zoom.last_api_request()) == ["first@example.com", "third@example.com"]


def test_removing_the_last_invitee_sends_an_empty_list(zoom, zoom_plugin):
    zoom.get = (200, zoom.meeting_with_invitees("responder@example.com"))

    zoom_plugin.remove_participant(MEETING_ID, "responder@example.com")

    assert zoom.last_api_request().json["settings"]["meeting_invitees"] == []


def test_remove_participant_matches_case_insensitively(zoom, zoom_plugin):
    zoom.get = (200, zoom.meeting_with_invitees("Responder@Example.com"))

    zoom_plugin.remove_participant(MEETING_ID, "responder@example.com")

    assert zoom.last_api_request().json["settings"]["meeting_invitees"] == []


def test_remove_participant_drops_every_duplicate_of_that_invitee(zoom, zoom_plugin):
    """Leaving one behind would keep the person on the roster they just left."""
    zoom.get = (200, zoom.meeting_with_invitees("dupe@example.com", "dupe@example.com"))

    zoom_plugin.remove_participant(MEETING_ID, "dupe@example.com")

    assert zoom.last_api_request().json["settings"]["meeting_invitees"] == []


def test_removing_an_absent_invitee_sends_no_patch(zoom, zoom_plugin):
    zoom.get = (200, zoom.meeting_with_invitees("someone@example.com"))

    zoom_plugin.remove_participant(MEETING_ID, "notthere@example.com")

    assert [r.method for r in zoom.api_requests()] == ["GET"]


def test_removing_from_a_meeting_with_no_invitees_sends_no_patch(zoom, zoom_plugin):
    zoom.get = (200, zoom.meeting_with_invitees())

    zoom_plugin.remove_participant(MEETING_ID, "notthere@example.com")

    assert [r.method for r in zoom.api_requests()] == ["GET"]


# --- response shapes ---------------------------------------------------------
#
# Two of these are grounded: the key being absent is spec-legal (Zoom's schema
# lists no `required` fields), and the explicit null is the behaviour the old
# docstring here recorded as observed. The malformed ones are invented, and are
# robustness cases rather than claims about what Zoom does.


def test_an_explicitly_empty_invitee_list_is_an_answer_and_is_added_to(zoom, zoom_plugin):
    """``[]`` is Zoom saying the roster is empty, which is a thing it can say.

    The pair below is the same wire shape with the answer withheld, and the two
    must not be confused -- that conflation is issue #129.
    """
    zoom.get = (200, {"id": 987654321, "settings": {"meeting_invitees": []}})

    zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert emails(zoom.last_api_request()) == ["responder@example.com"]


@pytest.mark.parametrize(
    "body",
    [
        {"id": 987654321},
        {"id": 987654321, "settings": {}},
        {"id": 987654321, "settings": {"meeting_invitees": None}},
        {"id": 987654321, "settings": None},
        {"id": 987654321, "settings": []},
        {"id": 987654321, "settings": {"meeting_invitees": {"email": "someone@example.com"}}},
        {"id": 987654321, "settings": {"meeting_invitees": "someone@example.com"}},
        {"id": 987654321, "settings": {"meeting_invitees": ["someone@example.com"]}},
    ],
    ids=[
        "settings-absent",
        "key-absent",
        "explicit-null",
        "settings-null",
        "settings-not-an-object",
        "roster-is-an-object",
        "roster-is-a-string",
        "roster-holds-a-string",
    ],
)
def test_a_roster_zoom_did_not_report_is_never_rewritten(zoom, zoom_plugin, body):
    """Every shape that is not a list of objects means Zoom did not answer.

    Rewriting the list from a non-answer is what would discard the roster #127
    seeds, since the API replaces it wholesale (issue #129). The last three are
    malformed rather than merely absent, and land in the same place on purpose:
    a roster that cannot be round-tripped faithfully must not be round-tripped.
    """
    zoom.get = (200, body)

    with pytest.raises(ConferenceRosterUnreadable) as excinfo:
        zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert "did not report" in str(excinfo.value)
    assert [r.method for r in zoom.api_requests()] == ["GET"]


@pytest.mark.parametrize(
    "roster",
    [
        [{"email": "real@example.com"}, "bob@example.com"],
        [{"email": "real@example.com"}, None],
        [{"email": "real@example.com"}, {"email": 12345}],
        [None],
    ],
    ids=["one-string", "one-null", "one-numeric-email", "only-a-null"],
)
def test_a_roster_with_one_bad_entry_is_refused_whole(zoom, zoom_plugin, roster):
    """The shape a permissive reader silently shortens.

    Dropping the entry it cannot understand and keeping the rest looks like
    robustness and is data loss: the surviving list is then written back as the
    whole roster, so the bad entry's owner is off the meeting. Refusing the
    whole read leaves every one of them where they were.

    The numeric email is the one that used to escape as an ``AttributeError``
    out of ``invitee_matches`` rather than as a plugin exception, which reaches
    the incident timeline reading like a Dispatch bug.
    """
    zoom.get = (200, {"id": 987654321, "settings": {"meeting_invitees": roster}})

    with pytest.raises(ConferenceRosterUnreadable):
        zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert [r.method for r in zoom.api_requests()] == ["GET"]


def test_a_read_that_is_not_json_is_a_failure_and_not_a_decline(zoom, zoom_plugin):
    """HTTP 200 with an HTML body: a captive portal or a proxy answering for Zoom.

    A different path from the refusal -- `get_meeting` raises before
    `reported_invitees` sees anything -- and it must stay a failure, because
    something is wrong rather than merely unreported.
    """
    zoom.get = (200, b"<html>Sign in to continue</html>")

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert "not JSON" in str(excinfo.value)
    assert not isinstance(excinfo.value, ConferenceRosterUnreadable)
    assert [r.method for r in zoom.api_requests()] == ["GET"]


def test_a_roster_zoom_did_not_report_is_not_rewritten_on_removal_either(zoom, zoom_plugin):
    """The removal half of the same rule.

    A rewrite here could only shorten the list, and the pre-#129 code did in
    fact send nothing -- but it returned as though the participant had already
    been off the roster, which is a claim about state Zoom never made.
    """
    zoom.get = (200, {"id": 987654321})

    with pytest.raises(ConferenceRosterUnreadable) as excinfo:
        zoom_plugin.remove_participant(MEETING_ID, "responder@example.com")

    assert "did not report" in str(excinfo.value)
    assert [r.method for r in zoom.api_requests()] == ["GET"]


@pytest.mark.parametrize(
    "failure",
    [
        (404, {"code": 3001, "message": "Meeting does not exist."}),
        (503, {"message": "Unavailable."}),
    ],
    ids=["gone", "unavailable"],
)
def test_a_read_that_failed_is_a_failure_and_not_a_decline(zoom, zoom_plugin, failure):
    """The two must not collapse into one another.

    A decline is kept off the incident timeline because nothing went wrong; a
    read Zoom refused *is* something going wrong and has to stay visible there.
    ``ConferenceRosterUnreadable`` subclasses ``DispatchPluginException``, so
    only asserting the parent would pass with the distinction gone.
    """
    zoom.get = failure

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert not isinstance(excinfo.value, ConferenceRosterUnreadable)


def test_the_refusal_names_neither_the_participant_nor_the_meeting(zoom, zoom_plugin):
    """It is logged, and a caller is free to surface it."""
    zoom.get = (200, {"id": 987654321})

    with pytest.raises(ConferenceRosterUnreadable) as excinfo:
        zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    message = str(excinfo.value)
    assert "responder@example.com" not in message
    assert MEETING_ID not in message


def test_the_refusal_says_the_roster_is_not_access_control(zoom, zoom_plugin):
    """ "Could not be added to the conference" invites exactly one wrong reading,
    and the person reading it is running an incident. The message says outright
    that nobody's ability to join changed."""
    zoom.get = (200, {"id": 987654321})

    with pytest.raises(ConferenceRosterUnreadable) as excinfo:
        zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert "does not control who can join" in str(excinfo.value)


def test_the_refusal_names_the_shape_zoom_answered_with(zoom, zoom_plugin):
    """Four different providers otherwise share one string. The phrase is what
    an operator records on issue #129."""
    zoom.get = (200, {"id": 987654321, "settings": {"meeting_invitees": None}})

    with pytest.raises(ConferenceRosterUnreadable) as excinfo:
        zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert "explicit null" in str(excinfo.value)


def test_an_invitee_without_an_email_is_preserved_rather_than_dropped(zoom, zoom_plugin):
    zoom.get = (200, {"id": 987654321, "settings": {"meeting_invitees": [{"email": None}]}})

    zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    invitees = zoom.last_api_request().json["settings"]["meeting_invitees"]
    assert invitees == [{"email": None}, {"email": "responder@example.com"}]


# --- failures ---------------------------------------------------------------


def test_a_failed_read_raises_and_sends_no_patch(zoom, zoom_plugin):
    """Patching a list we failed to read would erase every invitee."""
    zoom.get = (404, {"code": 3001, "message": "Meeting does not exist."})

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert "Meeting does not exist." in str(excinfo.value)
    assert [r.method for r in zoom.api_requests()] == ["GET"]


def test_a_failed_add_raises(zoom, zoom_plugin):
    zoom.get = (200, zoom.meeting_with_invitees())
    zoom.patch = (400, {"code": 300, "message": "Invalid invitee."})

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert "Invalid invitee." in str(excinfo.value)


def test_a_failed_remove_raises(zoom, zoom_plugin):
    zoom.get = (200, zoom.meeting_with_invitees("responder@example.com"))
    zoom.patch = (401, {"code": 124, "message": "Access token is expired."})

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.remove_participant(MEETING_ID, "responder@example.com")

    assert "401" in str(excinfo.value)


def test_an_authentication_failure_is_reported_as_such(zoom, zoom_plugin):
    """Code 124 is what Zoom's API returns for a token it will not accept."""
    zoom.get = (401, {"code": 124, "message": "Invalid access token."})

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert "Invalid access token." in str(excinfo.value)


def test_a_non_json_error_body_still_raises_cleanly(zoom, zoom_plugin):
    zoom.get = (502, "<html>Bad Gateway</html>")

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert "502" in str(excinfo.value)


def test_a_network_failure_raises(zoom, zoom_plugin, monkeypatch):
    import requests
    from requests.adapters import HTTPAdapter

    def explode(self, request, **kwargs):
        raise requests.exceptions.ConnectTimeout("connection timed out")

    monkeypatch.setattr(HTTPAdapter, "send", explode)

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert "timed out" in str(excinfo.value)


def test_participant_operations_are_instrumented(zoom, zoom_plugin, monkeypatch):
    emitted = []
    monkeypatch.setattr(
        "dispatch.decorators.metrics_provider",
        type(
            "Recorder",
            (),
            {
                "counter": lambda self, name, value=None, tags=None: emitted.append(
                    tags["function"]
                ),
                "timer": lambda self, name, value=None, tags=None: emitted.append(tags["function"]),
                "gauge": lambda self, name, value=None, tags=None: None,
            },
        )(),
    )
    zoom.get = (200, zoom.meeting_with_invitees())

    zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert any("ZoomConferencePlugin.add_participant" in name for name in emitted)


def test_the_participant_email_is_not_logged(zoom, zoom_plugin, caplog):
    import logging

    zoom.get = (200, zoom.meeting_with_invitees())

    with caplog.at_level(logging.DEBUG):
        zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert "responder@example.com" not in caplog.text


# --- one token per operation ------------------------------------------------


def test_adding_a_participant_acquires_exactly_one_token(zoom, zoom_plugin):
    """The read and the write share a client, so they share a token.

    Asserted at the plugin level on purpose: the client-level caching test
    builds its own ``ZoomClient`` and so cannot see a refactor that gave
    ``get_meeting`` and ``update_invitees`` a client each -- which would double
    the token traffic with every existing test still green.
    """
    zoom.get = (200, zoom.meeting_with_invitees())

    zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert len(zoom.token_requests()) == 1
    assert [r.method for r in zoom.api_requests()] == ["GET", "PATCH"]


def test_removing_a_participant_acquires_exactly_one_token(zoom, zoom_plugin):
    zoom.get = (200, zoom.meeting_with_invitees("responder@example.com"))

    zoom_plugin.remove_participant(MEETING_ID, "responder@example.com")

    assert len(zoom.token_requests()) == 1
    assert [r.method for r in zoom.api_requests()] == ["GET", "PATCH"]


def test_a_no_op_participant_update_makes_no_extra_request(zoom, zoom_plugin):
    """An already-present invitee costs one token and one read, never a write."""
    zoom.get = (200, zoom.meeting_with_invitees("responder@example.com"))

    zoom_plugin.add_participant(MEETING_ID, "responder@example.com")

    assert len(zoom.token_requests()) == 1
    assert [r.method for r in zoom.api_requests()] == ["GET"]
    assert len(zoom.requests) == 2, "unexpected traffic to a third host"

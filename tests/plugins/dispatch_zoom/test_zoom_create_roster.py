"""The initial invitee roster in Zoom's create-meeting request (issue #110).

This is the regression suite for the half of #110 that was actually broken:
``ZoomConferencePlugin.create`` accepted ``participants`` and never read it, so
every bridge Dispatch created started with an empty invitee list no matter what
the responders' bridge-participation preferences said.

Every assertion below reads the JSON body of the request that reaches the
transport. Asserting that ``create`` was *called* with a list would have passed
against the unfixed plugin, which is the whole point -- the old code accepted the
argument perfectly happily.

Two things this suite deliberately does not claim:

- that an invitee can join and a non-invitee cannot. ``settings.meeting_invitees``
  is roster metadata; a Zoom ``join_url`` works for anyone holding it.
- that Zoom surfaces these invitees anywhere. Zoom's own staff describe the field
  as "just a list of the meeting's invitees" consumed by their calendar
  integrations. What is testable here is the request we build.

It also pins the notification property that made seeding safe: creating with
invitees sends nothing, because the create carries no notification switch at all
(Zoom has ``registrants`` for that, which this plugin does not touch).
"""

import pytest

from dispatch.exceptions import ConferenceCreatedButUnusable, ConferenceRosterUnreadable

from tests.plugins.dispatch_zoom.conftest import CREATE_BODY

ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"
CAROL = "carol@example.invalid"


def create_request(zoom):
    """The POST that created the meeting."""
    posts = [r for r in zoom.api_requests() if r.method == "POST"]
    assert len(posts) == 1, "the roster must ride along in the create, not a second call"
    return posts[0]


def invitees(zoom) -> list[dict]:
    return create_request(zoom).json["settings"]["meeting_invitees"]


# --- the roster is in the request Zoom receives -------------------------------


def test_create_sends_the_participants_as_invitees(zoom, zoom_plugin):
    zoom_plugin.create("incident-1", title="Situation Room", participants=[ALICE, BOB])

    assert invitees(zoom) == [{"email": ALICE}, {"email": BOB}]


def test_create_sends_one_participant(zoom, zoom_plugin):
    zoom_plugin.create("incident-1", title="Situation Room", participants=[ALICE])

    assert invitees(zoom) == [{"email": ALICE}]


def test_create_preserves_the_roster_order(zoom, zoom_plugin):
    zoom_plugin.create("incident-1", participants=[CAROL, ALICE, BOB])

    assert [i["email"] for i in invitees(zoom)] == [CAROL, ALICE, BOB]


def test_a_filtered_roster_sends_only_the_addresses_it_was_given(zoom, zoom_plugin):
    """The bridge-participation filter runs upstream; what reaches Zoom is
    exactly what survived it, and nothing is re-added here."""
    zoom_plugin.create("incident-1", participants=[ALICE, CAROL])

    assert [i["email"] for i in invitees(zoom)] == [ALICE, CAROL]


def test_the_invitees_ride_along_in_the_create(zoom, zoom_plugin):
    """One request, not a create followed by a roster PATCH.

    A follow-up write is exactly the shape issue #114 exists to close: the
    meeting is committed, the second call fails, and the id is lost. Inside the
    create, a roster Zoom rejects fails before Zoom commits anything.
    """
    zoom_plugin.create("incident-1", participants=[ALICE, BOB])

    assert [r.method for r in zoom.api_requests()] == ["POST"]


def test_the_create_still_carries_everything_else(zoom, zoom_plugin):
    """The roster is added to the request, not substituted for it."""
    zoom_plugin.create("incident-1", title="Situation Room", participants=[ALICE])

    body = create_request(zoom).json
    assert body["topic"] == "Situation Room"
    assert body["duration"] == 1440
    assert body["password"]
    assert body["settings"]["join_before_host"] is True


# --- the empty roster ---------------------------------------------------------


@pytest.mark.parametrize("empty", [[], None], ids=["empty-list", "omitted"])
def test_an_empty_roster_omits_the_invitee_setting(zoom, zoom_plugin, empty):
    """Omitted rather than sent as ``[]``, so a deployment with no bridge
    participants sends byte-for-byte the request it sent before this roster
    existed. An explicitly empty list would be a new instruction to Zoom about a
    setting Dispatch has no opinion on."""
    zoom_plugin.create("incident-1", participants=empty)

    settings = create_request(zoom).json["settings"]
    assert "meeting_invitees" not in settings
    assert settings == {"join_before_host": True}


@pytest.mark.parametrize("empty", [[], None], ids=["empty-list", "omitted"])
def test_an_empty_roster_still_creates_a_usable_meeting(zoom, zoom_plugin, empty):
    conference = zoom_plugin.create("incident-1", participants=empty)

    assert conference["id"] == "987654321"
    assert conference["weblink"] == "https://zoom.us/j/987654321"


# --- duplicates ---------------------------------------------------------------


def test_the_plugin_sends_the_roster_it_is_given(zoom, zoom_plugin):
    """Deduplication is ``create_conference``'s job, not the plugin's -- one
    place, so all three shipped providers get the same list rather than three
    private notions of sameness. This test records where that boundary is, so a
    reviewer does not read the absence of dedup here as an oversight.
    """
    zoom_plugin.create("incident-1", participants=[ALICE, ALICE])

    assert invitees(zoom) == [{"email": ALICE}, {"email": ALICE}]


def test_a_seeded_roster_zoom_does_not_report_survives_the_first_add(zoom, zoom_plugin):
    """The regression this file exists to prevent (issue #129).

    ``add_participant`` replaces ``meeting_invitees`` wholesale with whatever the
    GET returned plus one, so a Zoom that does not report invitees on a read
    would have the first responder to join silently replace the seeded roster
    with a single entry -- the create-time roster of #127 buying nothing beyond
    the create request itself.

    Whether Zoom reports them is still unsettled: its API reference documents
    the field on the 200 of ``GET /meetings/{meetingId}``, while its staff have
    said on the developer forum that invitees cannot be queried back. This test
    does not decide that. It fixes what happens in the branch where the answer
    is bad -- the roster is left standing and the failure is reported, rather
    than the roster being overwritten from an answer that was never given.

    Deliberately asserts on the *absence* of a PATCH. Asserting the exception
    alone would still pass against an implementation that raised after writing.
    """
    zoom_plugin.create("incident-1", participants=[ALICE, BOB])
    zoom.get = (200, {"id": 987654321, "topic": "Situation Room"})

    with pytest.raises(ConferenceRosterUnreadable) as excinfo:
        zoom_plugin.add_participant("987654321", CAROL)

    assert "did not report" in str(excinfo.value)
    assert [r.method for r in zoom.api_requests()] == ["POST", "GET"], (
        "ALICE and BOB must not be replaced by a roster rebuilt from a read that reported nothing"
    )


# --- limits and failures ------------------------------------------------------


def test_a_large_roster_is_sent_whole(zoom, zoom_plugin):
    """Never truncated. Zoom documents no invitee cap, and a silently shortened
    roster is a worse answer than a create that fails and says why."""
    roster = [f"responder-{n}@example.invalid" for n in range(300)]

    zoom_plugin.create("incident-1", participants=roster)

    assert [i["email"] for i in invitees(zoom)] == roster


# --- what the create response says about the roster ----------------------------
#
# Zoom documents `settings.meeting_invitees` on the create's own 201, so the
# create already holds the answer to issue #129 for the account it ran against.
# Logging it is what turns an unanswerable question into one the first incident
# of any deployment settles.


def test_a_create_response_that_reports_no_roster_is_logged(zoom, zoom_plugin, caplog):
    import logging

    zoom.response = (201, dict(CREATE_BODY))

    with caplog.at_level(logging.INFO):
        zoom_plugin.create("incident-1", participants=[ALICE, BOB])

    assert "no usable invitee list" in caplog.text
    assert "issue #129" in caplog.text


def test_the_create_observation_does_not_claim_anything_about_a_read(zoom, zoom_plugin, caplog):
    """The 201 and the 200 are different schemas -- the read's item carries an
    ``internal_user`` no request can send -- so an absent field on the create is
    not evidence about ``GET /meetings/{id}``. An earlier version of this line
    said the roster was "write-only on this account", which is the conclusion an
    operator would have closed issue #129 on, from an observation that does not
    support it."""
    import logging

    zoom.response = (201, dict(CREATE_BODY))

    with caplog.at_level(logging.INFO):
        zoom_plugin.create("incident-1", participants=[ALICE, BOB])

    assert "write-only" not in caplog.text
    assert "separate question" in caplog.text


def test_a_create_response_that_reports_the_roster_is_logged(zoom, zoom_plugin, caplog):
    import logging

    zoom.response = (
        201,
        dict(CREATE_BODY, settings={"meeting_invitees": [{"email": ALICE}, {"email": BOB}]}),
    )

    with caplog.at_level(logging.INFO):
        zoom_plugin.create("incident-1", participants=[ALICE, BOB])

    assert "reported 2 of 2" in caplog.text


@pytest.mark.parametrize(
    "echoed",
    [[], [{"email": ALICE}]],
    ids=["none-of-them", "one-of-two"],
)
def test_a_create_response_that_drops_invitees_warns(zoom, zoom_plugin, caplog, echoed):
    """The one outcome the read side cannot defend against.

    A Zoom that stores the roster and reports it short is indistinguishable at
    update time from one that really holds that much, so ``current_invitees``
    trusts what it is told and ``add_participant`` rewrites from it. The create
    is the only moment Dispatch holds both what it sent and what came back, so
    it is the only place this is visible at all.
    """
    import logging

    zoom.response = (201, dict(CREATE_BODY, settings={"meeting_invitees": echoed}))

    with caplog.at_level(logging.INFO):
        zoom_plugin.create("incident-1", participants=[ALICE, BOB])

    assert "replace them rather than extend them" in caplog.text
    assert [r.levelname for r in caplog.records] == ["WARNING"]


def test_a_provider_that_collapses_duplicates_is_not_reported_as_dropping_anyone(
    zoom, zoom_plugin, caplog
):
    """``create_conference`` deduplicates and the plugin forwards what it is
    given, so a raw count comparison would warn about a roster that is complete."""
    import logging

    zoom.response = (201, dict(CREATE_BODY, settings={"meeting_invitees": [{"email": ALICE}]}))

    with caplog.at_level(logging.INFO):
        zoom_plugin.create("incident-1", participants=[ALICE, ALICE])

    assert "reported 1 of 1" in caplog.text
    assert [r.levelname for r in caplog.records] == ["INFO"]


def test_the_roster_observation_names_no_participant(zoom, zoom_plugin, caplog):
    """It is counts only: this line reaches the application log."""
    import logging

    zoom.response = (
        201,
        dict(CREATE_BODY, settings={"meeting_invitees": [{"email": ALICE}]}),
    )

    with caplog.at_level(logging.DEBUG):
        zoom_plugin.create("incident-1", participants=[ALICE, BOB])

    assert ALICE not in caplog.text
    assert BOB not in caplog.text


def test_no_roster_observation_when_none_was_requested(zoom, zoom_plugin, caplog):
    """Nothing was sent, so there is nothing for Zoom to have echoed."""
    import logging

    with caplog.at_level(logging.DEBUG):
        zoom_plugin.create("incident-1", participants=[])

    assert "invitee" not in caplog.text


def test_a_create_that_fails_validation_is_not_reported_as_a_roster_observation(
    zoom, zoom_plugin, caplog
):
    """A meeting Zoom accepted but returned unusable raises first.

    Otherwise the log would carry a roster note about a bridge that is about to
    be deleted by `create_conference`'s compensation (issue #114).

    Asserted on the log, not only on the raise: asserting the exception alone
    passes with the observation moved above the validation, which is the
    ordering the test exists to pin.
    """
    import logging

    zoom.response = (201, {"id": 987654321})

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ConferenceCreatedButUnusable):
            zoom_plugin.create("incident-1", participants=[ALICE])

    assert "invitee" not in caplog.text


# --- notifications ------------------------------------------------------------


def test_the_create_asks_zoom_to_notify_no_one(zoom, zoom_plugin):
    """Seeding a roster must not turn silence into mail.

    Zoom staff confirm ``meeting_invitees`` "does not generate an email"; the
    mechanism that would is the registrants API, which this plugin never calls.
    Asserted structurally: the request carries no registration or notification
    instruction of any kind, so there is nothing for Zoom to act on.
    """
    zoom_plugin.create("incident-1", participants=[ALICE, BOB])

    body = create_request(zoom).json
    assert "approval_type" not in body["settings"]
    assert "registration_type" not in body["settings"]
    assert "registrants_email_notification" not in body["settings"]
    assert "contact_email" not in body["settings"]

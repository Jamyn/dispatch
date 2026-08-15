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


def test_a_seeded_roster_zoom_does_not_echo_back_is_lost_on_the_first_add(zoom, zoom_plugin):
    """Pins the consequence of a question this suite cannot settle.

    ``add_participant`` replaces `meeting_invitees` wholesale with whatever the
    GET returned plus one. So if Zoom does not report invitees on a read, the
    seeded roster is silently replaced by a single entry the first time anyone
    joins, and the create-time roster buys nothing beyond the create itself.

    Zoom staff have said invitees cannot be queried back; the field's own
    ``.get(key) or []`` handling in this plugin was written as though they can.
    Both cannot be right and no mocked test can decide it -- only
    ``test_zoom_live.py``'s write-gated round trip can, and it needs credentials
    this repository does not have. So the behaviour is asserted rather than
    assumed: if this test ever starts failing, Zoom's read semantics changed and
    the docstrings that hedge on this can be settled.
    """
    zoom_plugin.create("incident-1", participants=[ALICE, BOB])
    # The conftest default: a meeting Zoom reports with no invitees at all.
    zoom.get = (200, {"id": 987654321, "settings": {"meeting_invitees": []}})

    zoom_plugin.add_participant("987654321", CAROL)

    assert zoom.last_api_request().json == {"settings": {"meeting_invitees": [{"email": CAROL}]}}, (
        "ALICE and BOB are gone -- the roster is only as durable as Zoom's read"
    )


# --- limits and failures ------------------------------------------------------


def test_a_large_roster_is_sent_whole(zoom, zoom_plugin):
    """Never truncated. Zoom documents no invitee cap, and a silently shortened
    roster is a worse answer than a create that fails and says why."""
    roster = [f"responder-{n}@example.invalid" for n in range(300)]

    zoom_plugin.create("incident-1", participants=roster)

    assert [i["email"] for i in invitees(zoom)] == roster


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

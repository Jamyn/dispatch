"""The initial attendee roster in Graph's create-meeting request (issue #110).

The regression suite for the Teams half of #110. ``create`` accepted
``participants``, documented it as "accepted for interface parity", and dropped
it -- so every bridge started with an empty attendee list whatever the
responders' bridge-participation preferences said.

Every assertion reads the JSON body of the request that reaches the transport.
Asserting that ``create`` was *called* with a list would have passed against the
unfixed plugin, which accepted the argument perfectly happily.

Roster membership is metadata, not access control: a Teams ``joinWebUrl`` works
for whoever holds it, so an attendee entry grants nothing and its absence
withholds nothing. Nothing below claims otherwise.

The notification property that made seeding safe is asserted structurally rather
than assumed: Graph documents this endpoint as creating "a standalone meeting
that isn't associated with any event on the user's calendar", so there is no
calendar item and no invitation to send.
"""

import pytest

from tests.plugins.dispatch_microsoft_teams.graph_fake import MEETING_ID, MEETINGS_URL

ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"
CAROL = "carol@example.invalid"


def create_request(graph):
    """The POST that created the meeting."""
    posts = [r for r in graph.requests if r.method == "POST" and r.url == MEETINGS_URL]
    assert len(posts) == 1, "the roster must ride along in the create, not a second call"
    return posts[0]


def attendees(graph) -> list[dict]:
    return create_request(graph).json["participants"]["attendees"]


# --- the roster is in the request Graph receives ------------------------------


def test_create_sends_the_participants_as_attendees(graph, teams_plugin):
    teams_plugin.create("incident-1", title="Situation Room", participants=[ALICE, BOB])

    assert attendees(graph) == [
        {"upn": ALICE, "role": "attendee"},
        {"upn": BOB, "role": "attendee"},
    ]


def test_create_sends_one_participant(graph, teams_plugin):
    teams_plugin.create("incident-1", participants=[ALICE])

    assert attendees(graph) == [{"upn": ALICE, "role": "attendee"}]


def test_create_preserves_the_roster_order(graph, teams_plugin):
    teams_plugin.create("incident-1", participants=[CAROL, ALICE, BOB])

    assert [a["upn"] for a in attendees(graph)] == [CAROL, ALICE, BOB]


def test_a_filtered_roster_sends_only_the_addresses_it_was_given(graph, teams_plugin):
    teams_plugin.create("incident-1", participants=[ALICE, CAROL])

    assert [a["upn"] for a in attendees(graph)] == [ALICE, CAROL]


def test_every_seeded_attendee_gets_the_attendee_role(graph, teams_plugin):
    """``presenter`` and ``coorganizer`` are unsupported for identities Entra
    cannot resolve, and responders may be external -- so ``attendee`` is the only
    role that can be assigned without knowing who the address belongs to."""
    teams_plugin.create("incident-1", participants=[ALICE, BOB, CAROL])

    assert {a["role"] for a in attendees(graph)} == {"attendee"}


def test_a_seeded_attendee_carries_no_identity(graph, teams_plugin):
    """Graph resolves it. Populating ``identity`` would need the address turned
    into an Entra object id, which needs User.Read.All -- a permission this plugin
    deliberately does not request."""
    teams_plugin.create("incident-1", participants=[ALICE])

    assert attendees(graph) == [{"upn": ALICE, "role": "attendee"}]


def test_the_create_does_not_try_to_set_the_organizer(graph, teams_plugin):
    """Graph assigns the organizer from the user in the path and rejects an
    attempt to set it, exactly as it does on the attendee PATCH."""
    teams_plugin.create("incident-1", participants=[ALICE])

    assert "organizer" not in create_request(graph).json["participants"]


def test_the_attendees_ride_along_in_the_create(graph, teams_plugin):
    """One request, not a create followed by a roster PATCH.

    A follow-up write is exactly the shape issue #114 exists to close: the
    meeting is committed, the second call fails, and the id is lost. Inside the
    create, a roster Graph rejects fails before Graph commits anything.
    """
    teams_plugin.create("incident-1", participants=[ALICE, BOB])

    assert [r.method for r in graph.requests if r.host == "graph.microsoft.com"] == ["POST"]


def test_the_create_still_carries_everything_else(graph, teams_plugin):
    """The roster is added to the request, not substituted for it."""
    teams_plugin.create("incident-1", title="Situation Room", participants=[ALICE])

    body = create_request(graph).json
    assert body["subject"] == "Situation Room"
    assert body["startDateTime"]
    assert body["endDateTime"]
    assert body["joinMeetingIdSettings"] == {"isPasscodeRequired": True}


# --- the empty roster ---------------------------------------------------------


@pytest.mark.parametrize("empty", [[], None], ids=["empty-list", "omitted"])
def test_an_empty_roster_omits_the_participants_key(graph, teams_plugin, empty):
    """Omitted rather than sent as ``{"attendees": []}``, so a deployment with no
    bridge participants sends the request it sent before this roster existed. An
    explicitly empty attendee list is an instruction to Graph about a collection
    Dispatch has no opinion on."""
    teams_plugin.create("incident-1", participants=empty)

    assert "participants" not in create_request(graph).json


@pytest.mark.parametrize("empty", [[], None], ids=["empty-list", "omitted"])
def test_an_empty_roster_still_creates_a_usable_meeting(graph, teams_plugin, empty):
    conference = teams_plugin.create("incident-1", participants=empty)

    assert conference["id"] == MEETING_ID
    assert conference["weblink"]


# --- duplicates ---------------------------------------------------------------


def test_the_plugin_sends_the_roster_it_is_given(graph, teams_plugin):
    """Deduplication is ``create_conference``'s job, not the plugin's -- one
    place, so all three shipped providers get the same list rather than three
    private notions of sameness. This test records where that boundary is, so a
    reviewer does not read the absence of dedup here as an oversight.
    """
    teams_plugin.create("incident-1", participants=[ALICE, ALICE])

    assert [a["upn"] for a in attendees(graph)] == [ALICE, ALICE]


# --- limits and failures ------------------------------------------------------


def test_a_large_roster_is_sent_whole(graph, teams_plugin):
    """Never truncated. Graph's documented ceilings are about contact lists over
    150 and 1000 members, and a silently shortened roster is a worse answer than
    a create that fails and says why."""
    roster = [f"responder-{n}@example.invalid" for n in range(300)]

    teams_plugin.create("incident-1", participants=roster)

    assert [a["upn"] for a in attendees(graph)] == roster


# --- notifications ------------------------------------------------------------


def test_the_create_makes_no_calendar_entry(graph, teams_plugin):
    """Seeding a roster must not turn silence into mail.

    Graph emails attendees for a *calendar* event; ``/onlineMeetings`` creates a
    standalone meeting with no calendar item, which is why seeding here sends
    nothing. Asserted structurally: the request goes to the onlineMeetings
    collection and carries none of the calendar-event fields that would make it
    one.
    """
    teams_plugin.create("incident-1", participants=[ALICE, BOB])

    request = create_request(graph)
    assert request.url == MEETINGS_URL
    assert "/events" not in request.path
    body = request.json
    assert "attendees" not in body, "a calendar event's attendee list, not an onlineMeeting's"
    assert "isOnlineMeeting" not in body

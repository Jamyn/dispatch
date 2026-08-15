"""The conference roster across a whole incident's worth of updates (issue #129).

The other Zoom suites assert one request at a time. This one runs sequences --
create, then adds and removals -- against a fake that actually stores what it is
sent, because the failure #129 is about only appears on the *second* call: the
create seeds a roster and the first ``add_participant`` replaces it.

The file is organised around two providers, and each test names which one it
drives:

- ``reports_roster=True`` -- **what Zoom actually does**, verified against a real
  account 2026-08-15: the roster comes back in full on the 200 of
  ``GET /meetings/{meetingId}``, and as ``[]`` when the meeting has none.
- ``reports_roster=False`` -- a provider that accepts the field and never
  reports it, which is what Zoom's staff described on the developer forum and
  what issue #129 feared. Not Zoom, on the evidence; kept because the plugin's
  guarantee must not rest on the provider staying friendly.

The invariant holds against both:

    one participant update never discards the others.

"One update" is the whole promise, and is not a hedge: two updates that
interleave still lose one of themselves, which Zoom's wholesale-replace field
makes unavoidable from inside a single call. That is pinned at the bottom of
this file rather than fixed.

Under a provider that reports, the roster is maintained. Under one that does
not, the roster is left exactly as ``create`` seeded it and the operation is
reported as declined -- never rebuilt from an answer Zoom did not give.

Roster membership is metadata throughout: a Zoom ``join_url`` works for whoever
holds it, so nothing here grants or revokes access.
"""

import pytest

from dispatch.exceptions import ConferenceRosterUnreadable, DispatchPluginException

from tests.plugins.dispatch_zoom.conftest import MEETING_ID

ALICE = "alice@example.invalid"
BOB = "bob@example.invalid"
CAROL = "carol@example.invalid"
DAVE = "dave@example.invalid"

# Parametrised by name so a failure report says which provider it was.
PROVIDERS = pytest.mark.parametrize(
    "reports_roster", [True, False], ids=["zoom-reports-the-roster", "zoom-reports-nothing"]
)


def declined(excinfo) -> bool:
    return "did not report" in str(excinfo.value)


# --- create, then add ---------------------------------------------------------


def test_create_then_add_keeps_the_seeded_roster_when_zoom_reports_it(stateful_zoom, zoom_plugin):
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[ALICE, BOB])

    zoom_plugin.add_participant(MEETING_ID, CAROL)

    assert zoom.emails() == [ALICE, BOB, CAROL]


def test_create_then_add_keeps_the_seeded_roster_when_zoom_reports_nothing(
    stateful_zoom, zoom_plugin
):
    """The #129 regression, stated as the invariant rather than as the symptom.

    ALICE and BOB stay on the meeting. CAROL does not get added -- there is no
    way to add her without rewriting a list Zoom will not show us -- and that is
    reported rather than swallowed.
    """
    zoom = stateful_zoom(reports_roster=False)
    zoom_plugin.create("incident-1", participants=[ALICE, BOB])

    with pytest.raises(ConferenceRosterUnreadable) as excinfo:
        zoom_plugin.add_participant(MEETING_ID, CAROL)

    assert declined(excinfo)
    assert zoom.emails() == [ALICE, BOB]


def test_create_then_two_adds_accumulate(stateful_zoom, zoom_plugin):
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[ALICE, BOB])

    zoom_plugin.add_participant(MEETING_ID, CAROL)
    zoom_plugin.add_participant(MEETING_ID, DAVE)

    assert zoom.emails() == [ALICE, BOB, CAROL, DAVE]


@PROVIDERS
def test_an_empty_create_leaves_zoom_holding_nothing(stateful_zoom, zoom_plugin, reports_roster):
    """``create`` omits the key rather than sending ``[]``, so there is nothing
    to store -- and that is true whichever provider this is."""
    zoom = stateful_zoom(reports_roster=reports_roster)

    zoom_plugin.create("incident-1", participants=[])

    assert zoom.emails() == []


def test_add_to_an_empty_roster_works_when_zoom_reports_it(stateful_zoom, zoom_plugin):
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[])

    zoom_plugin.add_participant(MEETING_ID, ALICE)

    assert zoom.emails() == [ALICE]


def test_add_to_an_empty_roster_is_declined_when_zoom_reports_nothing(stateful_zoom, zoom_plugin):
    """The cost of the fix, recorded rather than hidden.

    Here there is genuinely nothing to lose, and the add is still declined --
    because on the wire this is the same response as a meeting holding a roster
    Zoom will not show. Declining costs a list entry Zoom's own staff say only
    their calendar integrations read; guessing costs the founding roster of
    every incident that seeded one. The trade is deliberate and is what the
    live suite exists to retire.
    """
    zoom = stateful_zoom(reports_roster=False)
    zoom_plugin.create("incident-1", participants=[])

    with pytest.raises(ConferenceRosterUnreadable) as excinfo:
        zoom_plugin.add_participant(MEETING_ID, ALICE)

    assert declined(excinfo)
    assert zoom.emails() == []


# --- create, then remove ------------------------------------------------------


def test_create_then_remove_drops_only_that_participant(stateful_zoom, zoom_plugin):
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[ALICE, BOB, CAROL])

    zoom_plugin.remove_participant(MEETING_ID, BOB)

    assert zoom.emails() == [ALICE, CAROL]


def test_create_then_remove_leaves_the_roster_intact_when_zoom_reports_nothing(
    stateful_zoom, zoom_plugin
):
    zoom = stateful_zoom(reports_roster=False)
    zoom_plugin.create("incident-1", participants=[ALICE, BOB, CAROL])

    with pytest.raises(ConferenceRosterUnreadable) as excinfo:
        zoom_plugin.remove_participant(MEETING_ID, BOB)

    assert declined(excinfo)
    assert zoom.emails() == [ALICE, BOB, CAROL]


def test_create_add_then_remove(stateful_zoom, zoom_plugin):
    """The sequence from the issue: create([A, B]) -> add(C) -> remove(A)."""
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[ALICE, BOB])

    zoom_plugin.add_participant(MEETING_ID, CAROL)
    zoom_plugin.remove_participant(MEETING_ID, ALICE)

    assert zoom.emails() == [BOB, CAROL]


def test_a_long_lifecycle_converges_on_the_intended_roster(stateful_zoom, zoom_plugin):
    """create([A, B]) -> add(C) -> add(D) -> remove(A) == [B, C, D].

    Create-time participants are not privileged: A leaves as easily as anyone
    added later, and C and D are as durable as B.
    """
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[ALICE, BOB])

    zoom_plugin.add_participant(MEETING_ID, CAROL)
    zoom_plugin.add_participant(MEETING_ID, DAVE)
    zoom_plugin.remove_participant(MEETING_ID, ALICE)

    assert zoom.emails() == [BOB, CAROL, DAVE]


# --- duplicates and retries ---------------------------------------------------


def test_adding_someone_the_create_already_seeded_changes_nothing(stateful_zoom, zoom_plugin):
    """``incident_create_resources_flow`` seeds the bridge and then walks the
    same responders through the add flow, so this is the ordinary path, not an
    edge case."""
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[ALICE])

    zoom_plugin.add_participant(MEETING_ID, ALICE)

    assert zoom.emails() == [ALICE]


def test_a_retried_add_does_not_duplicate(stateful_zoom, zoom_plugin):
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[ALICE])

    zoom_plugin.add_participant(MEETING_ID, BOB)
    zoom_plugin.add_participant(MEETING_ID, BOB)

    assert zoom.emails() == [ALICE, BOB]


def test_a_retried_remove_is_a_no_op(stateful_zoom, zoom_plugin):
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[ALICE, BOB])

    zoom_plugin.remove_participant(MEETING_ID, BOB)
    zoom_plugin.remove_participant(MEETING_ID, BOB)

    assert zoom.emails() == [ALICE]


def test_re_adding_after_a_removal_restores_the_participant(stateful_zoom, zoom_plugin):
    """A responder who opts out and back in again."""
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[ALICE, BOB])

    zoom_plugin.remove_participant(MEETING_ID, BOB)
    zoom_plugin.add_participant(MEETING_ID, BOB)

    assert zoom.emails() == [ALICE, BOB]


def test_a_duplicate_seeded_by_create_is_not_multiplied_by_an_add(stateful_zoom, zoom_plugin):
    """``create_conference`` deduplicates, but the plugin sends what it is given
    -- so a duplicate can reach Zoom, and adding must not compound it."""
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[ALICE, ALICE])

    zoom_plugin.add_participant(MEETING_ID, ALICE)

    assert zoom.emails() == [ALICE, ALICE]


def test_the_roster_survives_a_change_of_casing_throughout(stateful_zoom, zoom_plugin):
    """One person, three spellings, across three calls.

    The single-request tests cover each comparison; this covers the sequence,
    where an inconsistency shows up as a duplicate that then cannot be removed.
    """
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=["Alice@Example.Invalid", BOB])

    zoom_plugin.add_participant(MEETING_ID, "alice@example.invalid")
    assert zoom.emails() == ["Alice@Example.Invalid", BOB]

    zoom_plugin.remove_participant(MEETING_ID, "ALICE@EXAMPLE.INVALID")
    assert zoom.emails() == [BOB]


def test_an_emptied_roster_can_be_added_to_again(stateful_zoom, zoom_plugin):
    """Removing the last participant leaves an *answer*, not an absence.

    The sequence that would break if the write side ever started omitting the
    key when the list empties: the next read would report nothing and every
    later add would be declined for the rest of the incident.
    """
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[ALICE])

    zoom_plugin.remove_participant(MEETING_ID, ALICE)
    assert zoom.emails() == []

    zoom_plugin.add_participant(MEETING_ID, BOB)
    assert zoom.emails() == [BOB]


# --- failures leave the provider alone ----------------------------------------


def test_a_failed_patch_leaves_the_seeded_roster_standing(stateful_zoom, zoom_plugin):
    """Nothing is reported as synchronised when the write did not land."""
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[ALICE, BOB])
    zoom.patch = (400, {"code": 300, "message": "Invalid invitee."})

    with pytest.raises(DispatchPluginException):
        zoom_plugin.add_participant(MEETING_ID, CAROL)

    assert zoom.emails() == [ALICE, BOB]


def test_an_add_retried_after_a_failed_add_still_lands(stateful_zoom, zoom_plugin):
    """A rate-limited or transient write must not poison the roster.

    Participant flows swallow the failure and the responder can be re-added
    later, so what matters is that the retry sees the pre-failure roster and
    extends it -- not the truncated one a partial write would have left.
    """
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[ALICE, BOB])
    zoom.patch = (429, {"code": 429, "message": "Too many requests."})

    with pytest.raises(DispatchPluginException):
        zoom_plugin.add_participant(MEETING_ID, CAROL)

    zoom.patch = (204, None)
    zoom_plugin.add_participant(MEETING_ID, CAROL)

    assert zoom.emails() == [ALICE, BOB, CAROL]


def test_a_failed_read_leaves_the_seeded_roster_standing(stateful_zoom, zoom_plugin):
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[ALICE, BOB])
    zoom.get = (503, {"message": "Service unavailable."})

    with pytest.raises(DispatchPluginException):
        zoom_plugin.add_participant(MEETING_ID, CAROL)

    # The read has to have been attempted for this to mean anything: an earlier
    # version of this test broke the token exchange instead, so `add_participant`
    # died before reaching the API and the assertion below passed vacuously.
    assert [r.method for r in zoom.api_requests()] == ["POST", "GET"]
    assert zoom.emails() == [ALICE, BOB]


# --- what Zoom's read carries that its write schema does not ------------------


def test_an_invitee_zoom_enriched_is_resent_as_it_arrived(stateful_zoom, zoom_plugin):
    """Pins the ``internal_user`` round trip as a deliberate choice.

    Zoom documents ``internal_user`` on the 200 of the read and on neither write
    schema, so it can only ever arrive from Zoom -- the fake cannot produce it by
    echoing what was written, which is why the roster is seeded directly here.

    It is resent verbatim. **Hypothesis, untested against a real account:** the
    spec declares no ``additionalProperties`` anywhere, so it neither permits nor
    forbids the extra key on the way back. Stripping to ``{"email": ...}`` would
    be the other guess and is the reason this is pinned rather than left to
    chance -- if Zoom rejects it, it does so with a 400 through ``check``, loudly,
    and ``test_the_roster_lifecycle_against_a_real_account`` is what would see it.
    """
    zoom = stateful_zoom(reports_roster=True)
    zoom.roster = [{"email": ALICE, "internal_user": True}, {"email": BOB}]

    zoom_plugin.add_participant(MEETING_ID, CAROL)

    assert zoom.roster == [
        {"email": ALICE, "internal_user": True},
        {"email": BOB},
        {"email": CAROL},
    ]


def test_an_enriched_invitee_is_still_recognised_as_already_present(stateful_zoom, zoom_plugin):
    """The extra key must not stop ``invitee_matches`` from finding them."""
    zoom = stateful_zoom(reports_roster=True)
    zoom.roster = [{"email": ALICE, "internal_user": True}]

    zoom_plugin.add_participant(MEETING_ID, ALICE)

    assert [r.method for r in zoom.api_requests()] == ["GET"]


def test_an_enriched_invitee_can_be_removed(stateful_zoom, zoom_plugin):
    zoom = stateful_zoom(reports_roster=True)
    zoom.roster = [{"email": ALICE, "internal_user": True}, {"email": BOB}]

    zoom_plugin.remove_participant(MEETING_ID, ALICE)

    assert zoom.roster == [{"email": BOB}]


# --- concurrency --------------------------------------------------------------


def test_an_add_racing_another_writer_overwrites_it(stateful_zoom, zoom_plugin):
    """Records a lost update the fix for #129 does *not* close.

    ``meeting_invitees`` is replaced wholesale and Zoom publishes no conditional
    write for it -- no ETag, no ``If-Match``, no append primitive -- so two
    roster updates that interleave read/read/write/write cannot both survive.
    Retrying is not an option either: Zoom answers 204 to both writers, so there
    is no conflict to retry on.

    Dispatch really can run them concurrently. The shipped web tier is a single
    uvicorn worker, but every participant flow is a sync callable dispatched to
    the anyio threadpool -- FastAPI ``BackgroundTasks`` on the REST routes and
    the Slack adapter's own ``BackgroundTask`` on ``member_joined_channel`` --
    and the scheduler is a second process with its own thread pool. Two
    responders accepting a Slack invite at once is enough.

    Out of scope for #129, which is about a roster discarded by a *single*
    update. Worth being accurate about what closing it would take: not a new
    database model -- Dispatch already holds the intended roster in
    ``incident.participants`` filtered by ``wants_bridge_participation`` -- but a
    plugin method that sends the whole desired list rather than a delta, which
    is convergent and so has no lost update to lose. That is a change to the
    ``ConferencePlugin`` interface and to all three shipped providers.

    Driven through ``add_participant`` deliberately: an earlier version asserted
    against hand-built lists and ``update_invitees``, which pinned a property of
    the fake rather than of the plugin, and would have stayed green through any
    fix made where a fix belongs.
    """
    zoom = stateful_zoom(reports_roster=True)
    zoom_plugin.create("incident-1", participants=[ALICE, BOB])

    # DAVE lands between this add's read and the write that read feeds.
    zoom.compete_once([{"email": ALICE}, {"email": BOB}, {"email": DAVE}])

    zoom_plugin.add_participant(MEETING_ID, CAROL)

    assert zoom.emails() == [ALICE, BOB, CAROL], "DAVE is the lost update"

"""Drive the Zoom client against a real Zoom account (issue #70).

Everything else in this directory asserts what the client *sends*. Only Zoom can
say whether it accepts those credentials, and that is the whole question issue
#70 turns on: the previous JWT flow was retired in 2023 and no mocked test could
notice, because a fake happily answers a request no real server would.

Skipped unless an account is configured, so it is inert locally and in CI by
default.

Configuration
-------------
``DISPATCH_ZOOM_TEST_ACCOUNT_ID``     Account ID from the app's App Credentials page.
``DISPATCH_ZOOM_TEST_CLIENT_ID``      Client ID of the Server-to-Server OAuth app.
``DISPATCH_ZOOM_TEST_CLIENT_SECRET``  Client secret of that app.
``DISPATCH_ZOOM_TEST_API_USER_ID``    Email or user ID meetings would be created for.

All four are required; the suite skips unless every one is set.

Creating the app
----------------
1. Zoom App Marketplace -> Develop -> Build App -> **Server-to-Server OAuth**,
   against a **test** account. (The JWT app type this plugin used to require can
   no longer be created at all.)
2. Fill in the required basic information and activate the app.
3. Add the scopes the plugin needs. Newly created apps are generally offered the
   granular family only:

   - read:   ``meeting:read:admin``   or ``meeting:read:meeting:admin``
   - create: ``meeting:write:admin``  or ``meeting:write:meeting:admin``
   - update: ``meeting:write:admin``  or ``meeting:update:meeting:admin``
   - delete: ``meeting:write:admin``  or ``meeting:delete:meeting:admin``

   A missing scope is not visible at token time -- the token is issued and the
   API call fails later. This suite only needs the read scope.
4. Copy the Account ID, Client ID and Client Secret from App Credentials.

What this covers that the mocked suite cannot
---------------------------------------------
- The ``account_credentials`` grant is really what Zoom accepts, and a
  Server-to-Server app really is not entitled to ``client_credentials``.
- The Basic-auth credential encoding is what Zoom expects.
- A real access token comes back and authenticates a real API call.
- Wrong credentials fail the way the client reports them.

These tests are deliberately **read-only**: they create, modify and delete
nothing in the account, so there is nothing to clean up and nothing that can be
left behind. Authentication is proven by listing meetings, which needs only the
read scope this file tells you to add -- a ``GET /users/me`` probe would need a
``user:read`` scope that the plugin itself never uses.

The exceptions are the ``@writes`` tests at the bottom, which have to create a
meeting to have anything to assert about: the two teardown tests -- the
``delete_conference`` flow (issue #105) and the compensating cleanup in
``create_conference`` (issue #114) -- and the roster tests (issues #110, #129),
which are the only thing anywhere that can say whether Zoom stores and reports
the invitees it is sent. They are skipped unless
``DISPATCH_ZOOM_TEST_ALLOW_WRITES=1`` is set as well, so the four variables
above still buy you a suite that touches nothing.

``test_zoom_reports_the_seeded_invitees_on_a_read`` is the one to run to settle
issue #129, and a failure there is an answer rather than a defect: its message
names the shape Zoom answered with -- via the plugin's own ``describe_roster``,
so the classification cannot drift from the one the plugin acts on -- and that
phrase is what to record on the issue.

Note on assertions: a failing ``assert SECRET not in text`` renders **both**
operands into the pytest report, publishing the very secret it checks for. Every
assertion below that touches a credential compares a precomputed bool instead.
"""

import os
import time
import uuid

from types import SimpleNamespace

import pytest
import requests

from dispatch.conference import flows as conference_flows
from dispatch.conference.models import Conference
from requests.auth import HTTPBasicAuth

from dispatch.exceptions import (
    ConferenceCreatedButUnusable,
    ConferenceRosterUnreadable,
    DispatchPluginException,
)
from dispatch.plugins.dispatch_zoom.plugin import describe_roster, reported_invitees

ACCOUNT_ID = os.environ.get("DISPATCH_ZOOM_TEST_ACCOUNT_ID")
CLIENT_ID = os.environ.get("DISPATCH_ZOOM_TEST_CLIENT_ID")
CLIENT_SECRET = os.environ.get("DISPATCH_ZOOM_TEST_CLIENT_SECRET")
API_USER_ID = os.environ.get("DISPATCH_ZOOM_TEST_API_USER_ID")

pytestmark = pytest.mark.skipif(
    not all([ACCOUNT_ID, CLIENT_ID, CLIENT_SECRET, API_USER_ID]),
    reason=(
        "needs DISPATCH_ZOOM_TEST_ACCOUNT_ID, _CLIENT_ID, _CLIENT_SECRET and _API_USER_ID "
        "for a real Zoom Server-to-Server OAuth app"
    ),
)

# Read-only: lists meetings, changes nothing.
READ_ONLY_PROBE = "users/{}/meetings?page_size=1"


@pytest.fixture
def zoom_client():
    """Named to avoid shadowing the root conftest's FastAPI ``client``."""
    from dispatch.plugins.dispatch_zoom.client import ZoomClient

    return ZoomClient(
        account_id=ACCOUNT_ID,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )


# --- authentication ---------------------------------------------------------


def test_the_server_to_server_oauth_flow_authenticates(zoom_client):
    """Deliberately asserts nothing about the token's *format*.

    Zoom's own documentation is inconsistent on whether the access token is a
    JWT or an opaque string, and the plugin neither parses nor cares. Asserting
    a shape here would break on a Zoom change that cannot affect Dispatch.
    """
    token = zoom_client._token()

    assert token
    assert isinstance(token, str)


def test_the_token_authenticates_a_real_api_call(zoom_client):
    """A token Zoom issues but will not accept is the failure worth catching."""
    response = zoom_client.get(READ_ONLY_PROBE.format(API_USER_ID))

    # The body is not echoed: it carries account data, and this runs in CI.
    assert response.status_code == 200, (
        f"Zoom rejected an authenticated read with HTTP {response.status_code}; "
        "check the app's meeting read scope."
    )
    # Precomputed, like the credential assertions below: `assert "meetings" in
    # response.json()` renders the account's whole meeting list -- topics and
    # join URLs -- into the pytest report when it fails.
    listed = "meetings" in response.json()
    assert listed, "Zoom answered 200 without a meetings key"


def test_a_wrong_client_secret_is_reported_as_an_authentication_failure(zoom_client):
    """Zoom answers the token endpoint with 400, not 401."""
    zoom_client.client_secret = "definitely-not-the-right-secret"
    zoom_client._access_token = None
    zoom_client._expires_at = 0.0

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_client._token()

    message = str(excinfo.value)
    assert "HTTP 400" in message or "HTTP 401" in message, message
    assert "invalid_client" in message or "client_id or client_secret" in message, message


def test_a_wrong_account_id_is_reported(zoom_client):
    zoom_client.account_id = "definitely-not-a-real-account-id"
    zoom_client._access_token = None
    zoom_client._expires_at = 0.0

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_client._token()

    message = str(excinfo.value)
    assert "HTTP 400" in message or "HTTP 401" in message, message


def test_zoom_does_not_grant_this_app_the_client_credentials_grant():
    """Settles the one claim the issue got wrong.

    Issue #70 proposed a ``client_credentials`` grant. Zoom's token endpoint
    does recognise that grant -- it belongs to General Apps -- but a
    Server-to-Server app is not entitled to it, which is why the client sends
    ``account_credentials``. If this ever starts succeeding, that choice is no
    longer load-bearing, and that is worth knowing either way.
    """
    response = requests.post(
        "https://zoom.us/oauth/token",
        data={"grant_type": "client_credentials", "account_id": ACCOUNT_ID},
        auth=HTTPBasicAuth(CLIENT_ID, CLIENT_SECRET),
        timeout=15,
    )

    if response.ok:
        pytest.fail(
            "Zoom accepted the client_credentials grant for a Server-to-Server app; "
            "the account_credentials value is no longer load-bearing."
        )

    assert response.status_code in (400, 401), f"unexpected HTTP {response.status_code}"


def test_the_token_is_reused_within_its_lifetime(zoom_client, monkeypatch):
    """Asserts the cache by counting acquisitions, not by comparing tokens.

    Zoom may well return an identical token string for two requests made a
    moment apart, so comparing the two values would pass with the cache removed
    entirely.
    """
    acquisitions = []
    original = zoom_client._acquire_token

    def counting():
        acquisitions.append(1)
        return original()

    monkeypatch.setattr(zoom_client, "_acquire_token", counting)

    zoom_client._token()
    zoom_client._token()

    assert len(acquisitions) == 1


# --- secrets stay out of the output -----------------------------------------


def test_no_secret_appears_in_the_logs(zoom_client, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        zoom_client.get(READ_ONLY_PROBE.format(API_USER_ID))

    # Precomputed so a failure cannot render the secret into the report.
    leaked = CLIENT_SECRET in caplog.text
    assert not leaked, "the client secret appeared in the logs"


def test_no_access_token_appears_in_the_logs(zoom_client, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        token = zoom_client._token()
        zoom_client.get(READ_ONLY_PROBE.format(API_USER_ID))

    leaked = token in caplog.text
    assert not leaked, "the access token appeared in the logs"


# --- conference teardown, opt-in twice over (issue #105) ---------------------
#
# Everything above is read-only, and that is a property of this file worth
# keeping: the four variables at the top get you authentication coverage and
# nothing that can leave a trace in the account. The teardown flow cannot be
# proven that way -- there is nothing to delete without first creating
# something -- so it sits behind a second, separate switch and needs a write
# scope the rest of the suite tells you not to add.

ALLOW_WRITES = os.environ.get("DISPATCH_ZOOM_TEST_ALLOW_WRITES") == "1"

writes = pytest.mark.skipif(
    not ALLOW_WRITES,
    reason=(
        "creates and deletes a real Zoom meeting; set DISPATCH_ZOOM_TEST_ALLOW_WRITES=1 "
        "and add meeting:write:admin (or meeting:write:meeting:admin) to opt in"
    ),
)


@pytest.fixture
def zoom_plugin_live():
    from dispatch.plugins.dispatch_zoom.config import ZoomConfiguration
    from dispatch.plugins.dispatch_zoom.plugin import ZoomConferencePlugin

    instance = ZoomConferencePlugin()
    instance.configuration = ZoomConfiguration(
        account_id=ACCOUNT_ID,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        api_user_id=API_USER_ID,
    )
    return instance


@writes
def test_the_delete_conference_flow_really_removes_the_meeting(
    zoom_client, zoom_plugin_live, monkeypatch
):
    """``delete_conference`` end to end against a real Zoom account.

    The mocked suite proves what the plugin sends. Only Zoom can say that the
    identifier the *flow* reaches for -- ``conference.conference_id`` -- is the
    one it accepts; a flow passing ``resource_id`` would leave the meeting
    standing while every mocked test stayed green.

    No database is involved: ``delete_conference`` touches ``db_session`` only
    to resolve the active plugin, which is stubbed here, and the ``Conference``
    is built rather than persisted.
    """
    created = zoom_plugin_live.create(f"dispatch-live-test-{uuid.uuid4().hex[:8]}")
    meeting_id = created["id"]

    # Registered before anything else can raise: everything from here on is
    # inside the cleanup's scope, so a failure cannot leak a real meeting.
    try:
        conference = Conference(
            conference_id=meeting_id,
            # Deliberately wrong, and deliberately not the meeting id: a flow
            # that reaches for this deletes nothing and the probe below still
            # finds the meeting.
            resource_id="not-the-provider-meeting-id",
            weblink=created["weblink"],
            resource_type="zoom-conference",
        )

        instance = SimpleNamespace(
            instance=zoom_plugin_live,
            plugin=SimpleNamespace(
                slug="zoom-conference", title="Zoom Plugin - Conference Management"
            ),
        )
        # `conference_flows.plugin_service` *is* `dispatch.plugin.service`, so
        # this rebinds a shared module attribute; monkeypatch undoes it even if
        # the assertions below bail out.
        monkeypatch.setattr(
            conference_flows.plugin_service, "get_active_instance", lambda **kwargs: instance
        )

        conference_flows.delete_conference(conference=conference, project_id=1, db_session=None)

        # Polled rather than asserted once, as the Teams equivalent is: if Zoom
        # is eventually consistent on this read, a single probe flakes.
        deadline = time.monotonic() + 30
        while zoom_client.get(f"meetings/{meeting_id}").status_code != 404:
            if time.monotonic() > deadline:
                pytest.fail("the meeting was still readable 30s after delete_conference")
            time.sleep(2)
    finally:
        # Only does anything when the flow did *not* delete it -- otherwise this
        # is a 404 and a no-op. It must stay after the probe above, never
        # before, or the cleanup would be what satisfies the check.
        zoom_client.delete(f"meetings/{meeting_id}")


@writes
def test_a_meeting_dispatch_cannot_persist_is_really_deleted(
    zoom_client, zoom_plugin_live, monkeypatch
):
    """The compensating cleanup, end to end against a real Zoom account (issue #114).

    The mocked suite proves the flow issues a DELETE. Only Zoom can say the id
    the flow hands over is one it accepts -- and this is the path with no
    ``Conference`` row behind it, so if the delete misses, nothing else will
    ever find the meeting.

    No database is involved and none is written to. The persistence step is
    replaced by a raise, which is the failure under test; every other
    ``db_session`` use on this path is stubbed with it.
    """
    instance = SimpleNamespace(
        instance=zoom_plugin_live,
        plugin=SimpleNamespace(slug="zoom-conference", title="Zoom Plugin - Conference Management"),
    )
    monkeypatch.setattr(
        conference_flows.plugin_service, "get_active_instance", lambda **kwargs: instance
    )

    # Recorded as the plugin creates it, because the flow re-raises without ever
    # returning the meeting -- and an id we never captured is one this test
    # could not clean up either.
    created_ids = []
    real_create = zoom_plugin_live.create

    def record_then_create(*args, **kwargs):
        meeting = real_create(*args, **kwargs)
        created_ids.append(meeting["id"])
        return meeting

    monkeypatch.setattr(zoom_plugin_live, "create", record_then_create)

    def refuse(**kwargs):
        raise RuntimeError("simulated Dispatch persistence failure")

    monkeypatch.setattr(conference_flows, "create", refuse)

    incident = SimpleNamespace(
        id=1,
        name=f"dispatch-live-test-{uuid.uuid4().hex[:8]}",
        title="Dispatch live test (safe to delete)",
        project=SimpleNamespace(id=1),
    )

    try:
        with pytest.raises(RuntimeError):
            conference_flows.create_conference(incident=incident, participants=[], db_session=None)

        assert created_ids, "Zoom never created a meeting, so nothing was under test"
        meeting_id = created_ids[0]

        # Polled, as the teardown test above is: a single probe flakes if Zoom
        # is eventually consistent on this read.
        deadline = time.monotonic() + 30
        while zoom_client.get(f"meetings/{meeting_id}").status_code != 404:
            if time.monotonic() > deadline:
                pytest.fail("the orphaned meeting was still readable 30s after the failure")
            time.sleep(2)
    finally:
        # Only does anything when the compensation did *not* run -- otherwise a
        # 404 and a no-op. After the probe, never before, or this would be what
        # satisfies the check.
        for meeting_id in created_ids:
            zoom_client.delete(f"meetings/{meeting_id}")


def create_test_meeting(zoom_plugin_live, created_ids: list, **kwargs) -> dict:
    """Create a meeting, recording its id even when the plugin refuses to return it.

    `ConferenceCreatedButUnusable` means Zoom committed a meeting and the plugin
    then rejected the response (issue #114) -- so a bare `create()` outside a
    `try` strands a real meeting on the test account. The id rides on the
    exception for exactly this reason; the caller's `finally` deletes whatever
    lands in `created_ids`.
    """
    try:
        created = zoom_plugin_live.create(
            f"dispatch-live-test-{uuid.uuid4().hex[:8]}",
            title="Dispatch live test (safe to delete)",
            **kwargs,
        )
    except ConferenceCreatedButUnusable as e:
        if e.resource_id:
            created_ids.append(e.resource_id)
        raise

    created_ids.append(created["id"])
    return created


@writes
def test_zoom_reports_the_seeded_invitees_on_a_read(zoom_client, zoom_plugin_live):
    """The one test that settles issue #129.

    Zoom's API reference documents `settings.meeting_invitees` on the 200 of
    `GET /meetings/{meetingId}` -- with a richer item schema than the request,
    carrying an `internal_user` no request can send. Zoom's own staff have said
    on the developer forum that invitees cannot be queried back. Both cannot
    hold, and no mocked test can decide it: the fake answers whatever it is
    told to.

    A failure here is not necessarily a bug in Dispatch. It is the answer, and
    the message says which of the four outcomes was seen so it can be recorded
    on the issue: A invitees returned, B an empty list, C an explicit null, D
    the key absent. Only A is compatible with maintaining the roster through
    read-modify-write, which is why `add_participant` refuses in the others
    rather than rebuilding the list from them.

    Synthetic invitees only. `.invalid` is reserved by RFC 2606 and can never
    resolve, so no real mailbox can be named here even by accident -- and Zoom
    does not email invitees regardless (its own staff describe the field as
    "just a list of the meeting's invitees"), which is why creating with a
    roster is safe to do against a real account at all.
    """
    invited = [
        f"alice-{uuid.uuid4().hex[:8]}@example.invalid",
        f"bob-{uuid.uuid4().hex[:8]}@example.invalid",
    ]

    created_ids = []

    try:
        created = create_test_meeting(zoom_plugin_live, created_ids, participants=invited)
        response = zoom_client.get(f"meetings/{created['id']}")
        assert response.status_code == 200

        meeting = response.json()
        stored_invitees = reported_invitees(meeting)
        assert stored_invitees, (
            f"Zoom did not report the {len(invited)} invitees it was sent -- "
            f"{describe_roster(meeting)}. That is the issue #129 hypothesis confirmed: "
            "the roster is write-only and read-modify-write cannot maintain it. Record "
            "the phrase above on the issue."
        )

        stored = [i.get("email") for i in stored_invitees]
        # Zoom is not documented to preserve order, so this compares sets. The
        # request order is asserted against the payload in the mocked suite,
        # which is where that question belongs.
        assert set(stored) == set(invited)
    finally:
        for meeting_id in created_ids:
            zoom_client.delete(f"meetings/{meeting_id}")


@writes
def test_a_meeting_created_with_no_roster_really_has_none(zoom_client, zoom_plugin_live):
    """The empty case against a real account.

    Records *how* Zoom spells an empty roster, which decides whether
    `add_participant` works at all on a bridge created with no seeded
    participants: B is an answer and is added to, D is not and is declined.
    Both are correct behaviour under issue #129 -- this test exists to say
    which one a real account produces, so the question is not guessed at.
    """
    created_ids = []

    try:
        created = create_test_meeting(zoom_plugin_live, created_ids, participants=[])
        response = zoom_client.get(f"meetings/{created['id']}")
        assert response.status_code == 200

        assert not reported_invitees(response.json()), (
            f"a meeting created with no invitees reported {describe_roster(response.json())}"
        )
        assert created["weblink"]
    finally:
        for meeting_id in created_ids:
            zoom_client.delete(f"meetings/{meeting_id}")


@writes
def test_the_roster_lifecycle_against_a_real_account(zoom_client, zoom_plugin_live):
    """create([A, B]) -> add(C) -> remove(A), end to end (issue #129).

    The invariant, against the only thing that can enforce it: adding or
    removing one participant must not discard the others. Every mocked
    equivalent of this asserts against a roster the fake was handed; this one
    reads Zoom's own.

    If Zoom does not report invitees, the plugin declines the add rather than
    replacing the seeded roster, and this test says so plainly instead of
    passing on a technicality -- the point is to learn what really happens.
    """
    alice = f"alice-{uuid.uuid4().hex[:8]}@example.invalid"
    bob = f"bob-{uuid.uuid4().hex[:8]}@example.invalid"
    carol = f"carol-{uuid.uuid4().hex[:8]}@example.invalid"

    created_ids = []

    def roster() -> set[str]:
        response = zoom_client.get(f"meetings/{created_ids[0]}")
        assert response.status_code == 200
        meeting = response.json()
        invitees = reported_invitees(meeting)
        if invitees is None:
            pytest.fail(
                f"Zoom stopped reporting the roster mid-lifecycle -- {describe_roster(meeting)}"
            )
        return {i.get("email") for i in invitees}

    try:
        create_test_meeting(zoom_plugin_live, created_ids, participants=[alice, bob])
        meeting_id = created_ids[0]

        try:
            zoom_plugin_live.add_participant(meeting_id, carol)
        except ConferenceRosterUnreadable as e:
            pytest.fail(
                f"Zoom would not report the seeded roster, so the add was declined: {e} "
                "Issue #129 is confirmed; the roster needs an authoritative copy in Dispatch."
            )

        assert roster() == {alice, bob, carol}, "the add did not preserve the seeded roster"

        zoom_plugin_live.remove_participant(meeting_id, alice)

        assert roster() == {bob, carol}, "the removal took someone else with it"
    finally:
        for meeting_id in created_ids:
            zoom_client.delete(f"meetings/{meeting_id}")


@writes
def test_a_repeated_add_does_not_duplicate_the_invitee(zoom_client, zoom_plugin_live):
    """Retry safety against a real account: participant flows can re-run.

    BOB is deliberately *not* seeded at create. Re-adding someone the create
    already listed short-circuits on the read and never issues a second write,
    so it would prove nothing about a repeated PATCH.
    """
    alice = f"alice-{uuid.uuid4().hex[:8]}@example.invalid"
    bob = f"bob-{uuid.uuid4().hex[:8]}@example.invalid"

    created_ids = []

    try:
        create_test_meeting(zoom_plugin_live, created_ids, participants=[alice])
        meeting_id = created_ids[0]

        try:
            zoom_plugin_live.add_participant(meeting_id, bob)
            zoom_plugin_live.add_participant(meeting_id, bob)
        except ConferenceRosterUnreadable as e:
            pytest.skip(f"Zoom does not report the roster, so there is nothing to retry: {e}")

        response = zoom_client.get(f"meetings/{meeting_id}")
        invitees = reported_invitees(response.json())
        assert invitees is not None, describe_roster(response.json())

        assert sorted(i.get("email") for i in invitees) == sorted([alice, bob])
    finally:
        for meeting_id in created_ids:
            zoom_client.delete(f"meetings/{meeting_id}")

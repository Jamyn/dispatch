"""Drive the Teams conference plugin against a real Microsoft 365 tenant.

Everything else in this directory asserts what the plugin *sends*. Only Graph
can say what it *accepts*, and the two are not the same: the request-body types
below were read off Microsoft's schema, not observed, so this suite is what
turns "inferred" into "verified".

Skipped unless a tenant is configured, so it is inert locally and in CI by
default.

Configuration
-------------
``DISPATCH_MSTEAMS_TEST_AUTHORITY``  ``https://login.microsoftonline.com/<tenant-id>``
``DISPATCH_MSTEAMS_TEST_CLIENT_ID``  Application (client) ID of the app registration.
``DISPATCH_MSTEAMS_TEST_SECRET``     Client secret for that app registration.
``DISPATCH_MSTEAMS_TEST_USER_ID``    Object ID of the user meetings are created for.

All four are required; the suite skips unless every one is set.

Creating the app registration
-----------------------------
1. Microsoft Entra admin center -> App registrations -> New registration, against
   a **test** tenant.
2. API permissions -> Microsoft Graph -> Application permissions ->
   ``OnlineMeetings.ReadWrite.All`` -> Grant admin consent. Delegated permissions
   are not enough; this plugin uses the client-credentials flow.
3. Certificates & secrets -> New client secret.
4. Grant the application access to the target user, which is a separate step and
   the one people miss -- without it Graph answers 403 with a valid token::

       Import-Module MicrosoftTeams
       Connect-MicrosoftTeams
       New-CsApplicationAccessPolicy -Identity Dispatch-Test \
           -AppIds "<client-id>" -Description "Dispatch conference plugin"
       Grant-CsApplicationAccessPolicy -PolicyName Dispatch-Test \
           -Identity "<user-object-id>"

   Policy assignment can take up to 30 minutes to take effect.

What this covers that the mocked suite cannot
---------------------------------------------
- The client-credentials flow really authenticates.
- Graph accepts the payload we build, including the JSON types.
- Graph *rejects* the string-typed booleans the plugin used to send, which is
  the claim issue #81 could only mark inferred.
- A real ``joinWebUrl`` and passcode come back.
- ``delete`` really removes the meeting.

This creates real meetings in the configured tenant. Point it at a throwaway
one. Every test cleans up after itself, and the cleanup is best-effort: a failed
delete is reported but does not fail the test that created the meeting.
"""

import os
import time
import uuid
import warnings

import pytest
import requests

from datetime import UTC, datetime, timedelta

from dispatch.exceptions import DispatchPluginException

AUTHORITY = os.environ.get("DISPATCH_MSTEAMS_TEST_AUTHORITY")
CLIENT_ID = os.environ.get("DISPATCH_MSTEAMS_TEST_CLIENT_ID")
SECRET = os.environ.get("DISPATCH_MSTEAMS_TEST_SECRET")
USER_ID = os.environ.get("DISPATCH_MSTEAMS_TEST_USER_ID")

pytestmark = pytest.mark.skipif(
    not all([AUTHORITY, CLIENT_ID, SECRET, USER_ID]),
    reason=(
        "needs DISPATCH_MSTEAMS_TEST_AUTHORITY, _CLIENT_ID, _SECRET and _USER_ID "
        "for a real Microsoft 365 tenant"
    ),
)


@pytest.fixture
def client():
    from dispatch.plugins.dispatch_microsoft_teams.conference.client import MSTeamsClient

    return MSTeamsClient(
        client_id=CLIENT_ID,
        authority=AUTHORITY,
        credential=SECRET,
        user_id=USER_ID,
    )


@pytest.fixture
def plugin():
    from dispatch.plugins.dispatch_microsoft_teams.conference.config import (
        MicrosoftTeamsConfiguration,
    )
    from dispatch.plugins.dispatch_microsoft_teams.conference.plugin import (
        MicrosoftTeamsConferencePlugin,
    )

    instance = MicrosoftTeamsConferencePlugin()
    instance.configuration = MicrosoftTeamsConfiguration(
        authority=AUTHORITY, client_id=CLIENT_ID, secret=SECRET, user_id=USER_ID
    )
    return instance


@pytest.fixture
def cleanup(client):
    """Delete every meeting the test registered, whatever the test did."""
    created = []
    yield created
    for meeting_id in created:
        try:
            client.delete_meeting(meeting_id)
        except Exception as e:  # noqa: BLE001 - cleanup must not mask the real failure
            # A bare print is swallowed by pytest's capture on a passing test,
            # which is exactly when a leaked meeting goes unnoticed.
            warnings.warn(
                f"live test leaked meeting {meeting_id}, delete it by hand: {e}",
                stacklevel=1,
            )


def _subject() -> str:
    """Unique and obviously disposable, so a leaked meeting is identifiable."""
    return f"Dispatch live test {uuid.uuid4().hex[:8]} (safe to delete)"


# --- authentication ---------------------------------------------------------


def test_the_client_credentials_flow_authenticates(client):
    token = client._acquire_token()

    assert token
    assert token.count(".") == 2, "expected a JWT"


def test_a_wrong_secret_is_reported_as_an_authentication_failure(client):
    client.client_credential = "definitely-not-the-right-secret"

    with pytest.raises(DispatchPluginException) as excinfo:
        client._acquire_token()

    assert "invalid_client" in str(excinfo.value)


# --- Graph accepts what we build --------------------------------------------


def test_graph_accepts_the_payload_and_returns_a_join_url(client, cleanup):
    meeting = client.create_meeting(subject=_subject(), duration_minutes=1440)
    cleanup.append(meeting["id"])

    assert meeting["joinWebUrl"].startswith("https://teams.microsoft.com/")
    assert meeting["id"]


def test_a_requested_passcode_comes_back_in_the_response(client, cleanup):
    meeting = client.create_meeting(subject=_subject(), require_passcode=True)
    cleanup.append(meeting["id"])

    settings = meeting.get("joinMeetingIdSettings", {})
    assert settings.get("isPasscodeRequired") is True
    assert settings.get("passcode"), "Graph should have generated a passcode"


def test_no_passcode_is_generated_when_none_is_required(client, cleanup):
    meeting = client.create_meeting(subject=_subject(), require_passcode=False)
    cleanup.append(meeting["id"])

    assert not meeting.get("joinMeetingIdSettings", {}).get("passcode")


def test_the_requested_duration_is_what_graph_stores(client, cleanup):
    meeting = client.create_meeting(subject=_subject(), duration_minutes=1440)
    cleanup.append(meeting["id"])

    start = datetime.fromisoformat(meeting["startDateTime"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(meeting["endDateTime"].replace("Z", "+00:00"))
    assert (end - start).total_seconds() == pytest.approx(1440 * 60, abs=60)


def test_graph_rejects_the_string_typed_booleans_the_plugin_used_to_send(client, cleanup):
    """Settles the one claim in issue #81 that source review could not.

    The plugin sent ``"true"``/``"false"`` where Graph declares Boolean. If this
    assertion ever fails, Graph has started coercing them and the type fix was
    cosmetic rather than load-bearing -- worth knowing either way.
    """
    start = datetime.now(UTC)
    token = client._acquire_token()
    response = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{USER_ID}/onlineMeetings",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "subject": _subject(),
            "startDateTime": start.isoformat(),
            "endDateTime": (start + timedelta(minutes=60)).isoformat(),
            # The only difference from a request the client would send.
            "recordAutomatically": "false",
            "joinMeetingIdSettings": {"isPasscodeRequired": "false"},
        },
        timeout=15,
    )

    if response.ok:
        cleanup.append(response.json()["id"])
        pytest.fail(
            "Graph accepted string-typed booleans; the type fix is no longer load-bearing. "
            f"Response: {response.status_code}"
        )

    assert response.status_code == 400, response.text
    # The body is otherwise identical to what the client sends, so a 400 that
    # names neither field means the request was rejected for another reason and
    # this test proves nothing about the boolean types.
    assert "recordAutomatically" in response.text or "isPasscodeRequired" in response.text, (
        response.text
    )


# --- the plugin surface -----------------------------------------------------


def test_the_plugin_returns_what_the_conference_flow_needs(plugin, cleanup):
    conference = plugin.create("dispatch-live-test", title=_subject())
    cleanup.append(conference["id"])

    assert conference["weblink"].startswith("https://teams.microsoft.com/")
    assert conference["id"]
    assert conference["challenge"], "passcode is on by default"


def test_delete_really_removes_the_meeting(client, plugin, cleanup):
    conference = plugin.create("dispatch-live-test", title=_subject())
    # Registered before the delete: if delete is what's broken -- the very thing
    # this test exists to catch -- the meeting would otherwise be orphaned.
    cleanup.append(conference["id"])

    plugin.delete(conference["id"])

    # Graph is eventually consistent on this read; poll rather than assert once.
    deadline = time.monotonic() + 30
    while True:
        try:
            client._request("GET", f"/users/{USER_ID}/onlineMeetings/{conference['id']}")
        except DispatchPluginException as e:
            assert "HTTP 404" in str(e), str(e)
            break
        if time.monotonic() > deadline:
            pytest.fail("the meeting was still readable 30s after delete")
        time.sleep(2)


def test_a_meeting_for_an_unknown_user_is_reported_clearly(client):
    client.user_id = "00000000-0000-0000-0000-000000000000"

    with pytest.raises(DispatchPluginException) as excinfo:
        client.create_meeting(subject=_subject())

    message = str(excinfo.value)
    assert "HTTP 403" in message or "HTTP 404" in message, message


# --- secrets stay out of the output -----------------------------------------


def test_no_secret_appears_in_the_logs(client, cleanup, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        meeting = client.create_meeting(subject=_subject())
    cleanup.append(meeting["id"])

    assert SECRET not in caplog.text

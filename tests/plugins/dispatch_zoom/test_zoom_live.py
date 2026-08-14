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

Note on assertions: a failing ``assert SECRET not in text`` renders **both**
operands into the pytest report, publishing the very secret it checks for. Every
assertion below that touches a credential compares a precomputed bool instead.
"""

import os

import pytest
import requests
from requests.auth import HTTPBasicAuth

from dispatch.exceptions import DispatchPluginException

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
    assert "meetings" in response.json()


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

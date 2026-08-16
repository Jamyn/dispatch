"""Behavioural tests for the Microsoft Graph client behind the Teams plugin.

Every test here drives real ``msal`` and real ``requests`` code against the fake
transport in ``graph_fake``; nothing patches the client's own methods.
"""

import logging
from urllib.parse import urlparse

import pytest

from dispatch.exceptions import DispatchPluginException

from tests.plugins.dispatch_microsoft_teams.graph_fake import (
    MEETINGS_URL as MEETINGS_URL_FOR_TEST,
)
from tests.plugins.dispatch_microsoft_teams.graph_fake import (
    ACCESS_TOKEN,
    GRAPH_HOST,
    AUTHORITY,
    CLIENT_ID,
    JOIN_URL,
    MEETING_ID,
    SECRET,
    USER_ID,
)


def build_client(**overrides):
    from dispatch.plugins.dispatch_microsoft_teams.conference.client import MSTeamsClient

    kwargs = {
        "client_id": CLIENT_ID,
        "authority": AUTHORITY,
        "credential": SECRET,
        "user_id": USER_ID,
    }
    kwargs.update(overrides)
    return MSTeamsClient(**kwargs)


# --- authentication ---------------------------------------------------------


def test_create_meeting_acquires_a_token_and_returns_the_meeting(graph):
    """The whole point: MSAL must accept the scopes we hand it.

    Passing a bare ``str`` makes ``acquire_token_for_client`` trip an internal
    ``assert isinstance(scopes, list)``, which is why this call used to raise
    before it ever reached Graph.
    """
    meeting = build_client().create_meeting(subject="Situation Room")

    assert meeting["id"] == MEETING_ID
    assert meeting["joinWebUrl"] == JOIN_URL


def test_the_graph_call_carries_the_acquired_bearer_token(graph):
    build_client().create_meeting(subject="Situation Room")

    assert graph.last_graph_request().headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


def test_a_failed_token_request_raises_with_the_reason(graph):
    graph.token = (
        401,
        {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret."},
        {},
    )

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert "invalid_client" in str(excinfo.value)
    assert "AADSTS7000215" in str(excinfo.value)


def test_a_failed_token_request_does_not_reach_graph(graph):
    graph.token = (401, {"error": "invalid_client"}, {})

    with pytest.raises(DispatchPluginException):
        build_client().create_meeting(subject="Situation Room")

    assert graph.graph_requests() == []


def test_the_access_token_is_never_logged(graph, caplog):
    with caplog.at_level(logging.DEBUG):
        build_client().create_meeting(subject="Situation Room")

    assert ACCESS_TOKEN not in caplog.text


def test_the_client_secret_is_never_logged(graph, caplog):
    with caplog.at_level(logging.DEBUG):
        build_client().create_meeting(subject="Situation Room")

    assert SECRET not in caplog.text


# --- request construction ---------------------------------------------------


def test_the_subject_is_sent_as_given(graph):
    build_client().create_meeting(subject="Situation Room for dispatch-incident-1")

    assert graph.last_graph_request().json["subject"] == "Situation Room for dispatch-incident-1"


@pytest.mark.parametrize("record_automatically", [True, False])
def test_record_automatically_is_sent_as_a_boolean(graph, record_automatically):
    """Graph declares ``recordAutomatically`` as Boolean.

    It used to be sent as the strings ``"true"``/``"false"``.
    """
    build_client().create_meeting(
        subject="Situation Room", record_automatically=record_automatically
    )

    assert graph.last_graph_request().json["recordAutomatically"] is record_automatically


@pytest.mark.parametrize("require_passcode", [True, False])
def test_is_passcode_required_is_sent_as_a_boolean(graph, require_passcode):
    build_client().create_meeting(subject="Situation Room", require_passcode=require_passcode)

    settings = graph.last_graph_request().json["joinMeetingIdSettings"]
    assert settings["isPasscodeRequired"] is require_passcode


def test_the_meeting_is_given_an_explicit_start_and_end(graph):
    """Graph documents ``endDateTime`` as required on create."""
    from datetime import datetime

    build_client().create_meeting(subject="Situation Room", duration_minutes=90)

    body = graph.last_graph_request().json
    start = datetime.fromisoformat(body["startDateTime"])
    end = datetime.fromisoformat(body["endDateTime"])

    assert (end - start).total_seconds() == 90 * 60
    assert start.tzinfo is not None, "Graph expects the timestamps in UTC"


def test_every_outbound_call_has_the_configured_timeout(graph):
    """A request with no timeout hangs the incident-creation flow forever.

    msal defaults to ``timeout=None`` and makes two calls of its own -- OIDC
    discovery and the token exchange -- before ours, so asserting on the Graph
    request alone leaves the hang fully reachable.
    """
    assert build_client().create_meeting(subject="Situation Room")

    assert len(graph.requests) >= 3, "expected discovery, token and Graph calls"
    for request in graph.requests:
        assert request.timeout == 15, f"{request.method} {request.url} timeout={request.timeout}"


def test_the_meeting_is_created_for_the_configured_user(graph):
    """Pins the host and API version too; /beta is not supported for production."""
    build_client().create_meeting(subject="Situation Room")

    request = graph.last_graph_request()
    assert request.url == f"https://graph.microsoft.com/v1.0/users/{USER_ID}/onlineMeetings"
    assert request.method == "POST"


# --- Graph failure handling -------------------------------------------------


@pytest.mark.parametrize(
    "status,body",
    [
        (400, {"error": {"code": "BadRequest", "message": "Invalid payload."}}),
        (
            401,
            {"error": {"code": "InvalidAuthenticationToken", "message": "Access token expired."}},
        ),
        (403, {"error": {"code": "Forbidden", "message": "Application access policy missing."}}),
        (404, {"error": {"code": "NotFound", "message": "User not found."}}),
        (500, {"error": {"code": "InternalServerError", "message": "Try again."}}),
        (503, {"error": {"code": "ServiceUnavailable", "message": "Try again."}}),
    ],
)
def test_a_graph_error_raises_rather_than_returning_a_body(graph, status, body):
    """The error body used to flow on and fail later as a ``KeyError``."""
    graph.meeting = (status, body, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert str(status) in str(excinfo.value)
    assert body["error"]["message"] in str(excinfo.value)


def test_a_rate_limited_call_surfaces_the_retry_after_hint(graph):
    """Graph answers 429 with a Retry-After the operator needs to see."""
    graph.meeting = (
        429,
        {"error": {"code": "TooManyRequests", "message": "Rate limit exceeded."}},
        {"Retry-After": "17"},
    )

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert "429" in str(excinfo.value)
    assert "17" in str(excinfo.value)


def test_a_rate_limited_call_is_not_retried(graph):
    """Retrying is the plugin's business, not the client's.

    A blind retry on a write endpoint is how you get two meetings for one
    incident, so the client must issue exactly one request.
    """
    graph.meeting = (429, {"error": {"code": "TooManyRequests", "message": "Slow down."}}, {})

    with pytest.raises(DispatchPluginException):
        build_client().create_meeting(subject="Situation Room")

    assert len(graph.graph_requests()) == 1


def test_a_non_json_error_body_still_raises_cleanly(graph):
    """A proxy or gateway in front of Graph will return HTML, not JSON."""
    graph.meeting = (502, b"<html><body>Bad Gateway</body></html>", {})

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert "502" in str(excinfo.value)


def test_a_success_with_an_unparsable_body_raises(graph):
    graph.meeting = (201, b"not json at all", {})

    with pytest.raises(DispatchPluginException):
        build_client().create_meeting(subject="Situation Room")


def test_a_network_failure_raises(graph, monkeypatch):
    import requests
    from requests.adapters import HTTPAdapter

    def explode(self, request, **kwargs):
        if urlparse(request.url).hostname == GRAPH_HOST:
            raise requests.exceptions.ConnectTimeout("connection timed out")
        return graph.send(request, **kwargs)

    monkeypatch.setattr(HTTPAdapter, "send", explode)

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert "timed out" in str(excinfo.value)


# --- delete -----------------------------------------------------------------


def test_delete_meeting_calls_graph_for_the_given_meeting(graph):
    build_client().delete_meeting(MEETING_ID)

    request = graph.last_graph_request()
    assert request.method == "DELETE"
    assert (
        request.url
        == f"https://graph.microsoft.com/v1.0/users/{USER_ID}/onlineMeetings/{MEETING_ID}"
    )
    assert request.timeout == 15
    assert request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


def test_delete_meeting_raises_on_a_graph_error(graph):
    graph.delete = (404, {"error": {"code": "NotFound", "message": "Meeting not found."}}, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().delete_meeting(MEETING_ID)

    assert "404" in str(excinfo.value)


# --- what msal is actually given --------------------------------------------
#
# Without these, every credential the plugin passes to msal is untestable: the
# fake would hand back a token for any client_id, secret or scope, so
# mis-plumbing any of them stays green.


def test_the_token_request_uses_the_client_credentials_grant(graph):
    build_client().create_meeting(subject="Situation Room")

    assert graph.last_token_request().form["grant_type"] == "client_credentials"


def test_the_token_request_carries_the_configured_client_id(graph):
    build_client(client_id="a-different-client-id").create_meeting(subject="Situation Room")

    assert graph.last_token_request().form["client_id"] == "a-different-client-id"


def test_the_token_request_carries_the_configured_secret(graph):
    build_client(credential="a-different-secret").create_meeting(subject="Situation Room")

    assert graph.last_token_request().form["client_secret"] == "a-different-secret"


def test_the_token_request_asks_for_the_default_graph_scope(graph):
    """Only ``/.default`` is valid for client credentials.

    A resource scope such as ``User.Read`` is rejected with AADSTS1002012, and
    the fake cannot tell the difference -- so pin the value here.
    """
    build_client().create_meeting(subject="Situation Room")

    assert graph.last_token_request().form["scope"] == "https://graph.microsoft.com/.default"


def test_the_token_is_requested_from_the_configured_authority(graph):
    build_client().create_meeting(subject="Situation Room")

    assert graph.last_token_request().url.startswith(AUTHORITY)


def test_the_token_is_reused_across_calls_on_one_client(graph):
    """msal caches per application instance, so the client must hold one."""
    client = build_client()
    client.create_meeting(subject="First")
    before = len(graph.token_requests())
    client.create_meeting(subject="Second")

    assert len(graph.token_requests()) == before, "a second token was fetched"


# --- more failure modes ------------------------------------------------------


def test_a_network_failure_on_the_token_request_raises_cleanly(graph, monkeypatch):
    """msal raises requests' own exceptions, which are not DispatchPluginException."""
    import requests
    from requests.adapters import HTTPAdapter

    def explode(self, request, **kwargs):
        if "/oauth2/v2.0/token" in request.url:
            raise requests.exceptions.ConnectTimeout("token endpoint timed out")
        return graph.send(request, **kwargs)

    monkeypatch.setattr(HTTPAdapter, "send", explode)

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert "timed out" in str(excinfo.value)


def test_an_unreachable_authority_raises_cleanly(graph, monkeypatch):
    """msal raises a bare ValueError when tenant discovery fails."""
    from requests.adapters import HTTPAdapter

    from tests.plugins.dispatch_microsoft_teams.graph_fake import _response

    def block(self, request, **kwargs):
        if "openid-configuration" in request.url:
            return _response(403, b"<html>blocked by proxy</html>")
        return graph.send(request, **kwargs)

    monkeypatch.setattr(HTTPAdapter, "send", block)

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert AUTHORITY in str(excinfo.value)


def test_a_retry_after_http_date_is_repeated_verbatim(graph):
    """Graph may send Retry-After as an HTTP date rather than a seconds count."""
    graph.meeting = (
        429,
        {"error": {"code": "TooManyRequests", "message": "Slow down."}},
        {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
    )

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert "Wed, 21 Oct 2026 07:28:00 GMT" in str(excinfo.value)
    assert "GMTs" not in str(excinfo.value), "a unit was appended to a date"


def test_the_graph_request_id_is_surfaced_for_support(graph):
    graph.meeting = (
        500,
        {"error": {"code": "InternalServerError", "message": "Try again."}},
        {"request-id": "a1b2c3d4-0000-1111-2222-333344445555"},
    )

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert "a1b2c3d4-0000-1111-2222-333344445555" in str(excinfo.value)


def test_a_non_json_error_body_is_not_echoed(graph):
    """A non-JSON body must never reach the exception (issue #156).

    The fallback used to be ``response.text.strip()[:200]``, which repeats
    whatever an intermediary answering in Graph's place puts in its body --
    and that request carries a live bearer token in its Authorization header.
    """
    graph.meeting = (502, b"upstream connect error reading from gateway", {})

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert "upstream connect error" not in str(excinfo.value)
    assert "no reason given" in str(excinfo.value)


def test_an_empty_error_body_still_produces_a_message(graph):
    graph.meeting = (500, b"", {})

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert "no reason given" in str(excinfo.value)


@pytest.mark.parametrize("body", [[], "a string", 12])
def test_a_json_body_that_is_not_an_object_raises(graph, body):
    """``.get()`` on a list is an AttributeError, not a plugin exception."""
    import json as _json

    graph.meeting = (201, _json.dumps(body).encode(), {})

    with pytest.raises(DispatchPluginException):
        build_client().create_meeting(subject="Situation Room")


def test_a_redirect_is_not_followed(graph):
    """A same-host 307 would replay the POST and create a second meeting."""
    graph.meeting = (307, None, {"Location": f"{MEETINGS_URL_FOR_TEST}"})

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert "307" in str(excinfo.value)
    assert len(graph.graph_requests()) == 1


# --- issue #156: arbitrary response bodies must never reach the exception ---
#
# The request carries a live `Authorization: Bearer <token>` header. A non-JSON
# response fires precisely when something other than Graph answers -- a proxy,
# captive portal, WAF, or gateway -- and those are exactly the responders that
# sometimes echo inbound request headers back in the body. `TEST_SECRET_DO_NOT_LEAK_12345`
# stands in for that token: it must never appear in the raised exception, no
# matter where in the body it sits or how long the body is.

SECRET_CANARY = "TEST_SECRET_DO_NOT_LEAK_12345"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            f"<html><body>upstream error\nAuthorization: Bearer {SECRET_CANARY}</body></html>".encode(),
            id="html-with-reflected-authorization-header",
        ),
        pytest.param(
            f"prefix... {SECRET_CANARY} ...suffix".encode(), id="plaintext-secret-in-middle"
        ),
        pytest.param(f"{SECRET_CANARY} trailing text".encode(), id="secret-at-start"),
        pytest.param(f"leading text {SECRET_CANARY}".encode(), id="secret-at-end"),
        pytest.param(b"{not valid json", id="malformed-json"),
        pytest.param(b"upstream service failure", id="plain-text"),
        pytest.param(
            ("x" * 50 + SECRET_CANARY + "y" * 500).encode(), id="long-body-secret-near-start"
        ),
        pytest.param(
            ("x" * 500 + SECRET_CANARY + "y" * 50).encode(), id="long-body-secret-near-end"
        ),
    ],
)
def test_a_reflected_secret_never_reaches_the_exception(graph, body):
    graph.meeting = (502, body, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert SECRET_CANARY not in str(excinfo.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502, 503])
def test_a_reflected_secret_never_reaches_the_exception_for_any_status(graph, status):
    """Status is preserved; body is not -- across the whole range the client sees."""
    body = f"gateway says: Authorization: Bearer {SECRET_CANARY}".encode()
    graph.meeting = (status, body, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert SECRET_CANARY not in str(excinfo.value)
    assert str(status) in str(excinfo.value)


# A rogue responder controls every header on the forged response, not just the
# body -- a reflected `Authorization` header landing in `Retry-After` or
# `request-id` is the identical leak, just moved to a different field.


def test_a_reflected_secret_in_retry_after_never_reaches_the_exception(graph):
    graph.meeting = (
        429,
        {"error": {"code": "TooManyRequests", "message": "Slow down."}},
        {"Retry-After": f"Bearer {SECRET_CANARY}"},
    )

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert SECRET_CANARY not in str(excinfo.value)


def test_a_reflected_secret_in_request_id_never_reaches_the_exception(graph):
    graph.meeting = (
        500,
        {"error": {"code": "InternalServerError", "message": "Try again."}},
        {"request-id": f"Bearer {SECRET_CANARY}"},
    )

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert SECRET_CANARY not in str(excinfo.value)


def test_a_retry_after_seconds_count_still_surfaces(graph):
    """The security fix must not break the legitimate, documented shape."""
    graph.meeting = (
        429,
        {"error": {"code": "TooManyRequests", "message": "Slow down."}},
        {"Retry-After": "17"},
    )

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert "Retry-After: 17" in str(excinfo.value)


def test_a_retry_after_http_date_still_surfaces(graph):
    graph.meeting = (
        429,
        {"error": {"code": "TooManyRequests", "message": "Slow down."}},
        {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
    )

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert "Retry-After: Wed, 21 Oct 2026 07:28:00 GMT" in str(excinfo.value)


def test_a_valid_request_id_still_surfaces(graph):
    graph.meeting = (
        500,
        {"error": {"code": "InternalServerError", "message": "Try again."}},
        {"request-id": "a1b2c3d4-0000-1111-2222-333344445555"},
    )

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert "a1b2c3d4-0000-1111-2222-333344445555" in str(excinfo.value)


def test_a_valid_json_graph_error_still_surfaces_its_message(graph):
    """The security fix must not make useful, structured errors disappear."""
    graph.meeting = (
        403,
        {"error": {"code": "Forbidden", "message": "Application access policy is missing."}},
        {},
    )

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().create_meeting(subject="Situation Room")

    assert "Application access policy is missing." in str(excinfo.value)
    assert SECRET_CANARY not in str(excinfo.value)

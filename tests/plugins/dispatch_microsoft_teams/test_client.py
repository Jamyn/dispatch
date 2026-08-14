"""Behavioural tests for the Microsoft Graph client behind the Teams plugin.

Every test here drives real ``msal`` and real ``requests`` code against the fake
transport in ``conftest.py``; nothing patches the client's own methods.
"""

import logging

import pytest

from dispatch.exceptions import DispatchPluginException

from tests.plugins.dispatch_microsoft_teams.graph_fake import (
    ACCESS_TOKEN,
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
        "record_automatically": False,
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
    build_client(record_automatically=record_automatically).create_meeting(subject="Situation Room")

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


def test_the_graph_call_has_a_timeout(graph):
    """A request with no timeout hangs the incident-creation flow forever."""
    assert build_client().create_meeting(subject="Situation Room")

    for request in graph.graph_requests():
        assert request.timeout, f"{request.method} {request.url} was sent without a timeout"


def test_the_meeting_is_created_for_the_configured_user(graph):
    build_client().create_meeting(subject="Situation Room")

    assert graph.last_graph_request().url.endswith(f"/users/{USER_ID}/onlineMeetings")


# --- Graph failure handling -------------------------------------------------


@pytest.mark.parametrize(
    "status,body",
    [
        (400, {"error": {"code": "BadRequest", "message": "Invalid payload."}}),
        (401, {"error": {"code": "InvalidAuthenticationToken", "message": "Access token expired."}}),
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
    """Graph throttles ``/onlineMeetings`` at 4 requests/second per app per tenant."""
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


def test_a_success_with_an_unparseable_body_raises(graph):
    graph.meeting = (201, b"not json at all", {})

    with pytest.raises(DispatchPluginException):
        build_client().create_meeting(subject="Situation Room")


def test_a_network_failure_raises(graph, monkeypatch):
    import requests
    from requests.adapters import HTTPAdapter

    def explode(self, request, **kwargs):
        if "graph.microsoft.com" in request.url:
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
    assert request.url.endswith(f"/users/{USER_ID}/onlineMeetings/{MEETING_ID}")
    assert request.timeout


def test_delete_meeting_raises_on_a_graph_error(graph):
    graph.delete = (404, {"error": {"code": "NotFound", "message": "Meeting not found."}}, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        build_client().delete_meeting(MEETING_ID)

    assert "404" in str(excinfo.value)

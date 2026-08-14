"""Regression cover for the Zoom conference plugin.

Zoom is the parity baseline for the Microsoft Teams plugin and the two share
``dispatch.decorators.apply`` and the ``ConferencePlugin`` contract. These tests
exist so a change made for Teams cannot quietly alter Zoom.
"""

import json

import pytest
from requests.adapters import HTTPAdapter
from requests.models import Response

API_KEY = "not-a-real-api-key"
API_SECRET = "not-a-real-api-secret"
API_USER_ID = "responder@example.com"


class FakeZoom:
    def __init__(self):
        self.requests = []
        self.response = (
            201,
            {
                "id": 987654321,
                "join_url": "https://zoom.us/j/987654321",
                "password": "zoompass",
            },
        )

    def send(self, request, timeout=None, **kwargs):
        self.requests.append(
            SimpleRequest(request.method, request.url, request.body, timeout, dict(request.headers))
        )
        status, body = self.response
        response = Response()
        response.status_code = status
        response.headers["content-type"] = "application/json"
        response._content = json.dumps(body).encode()
        response.url = request.url
        return response


class SimpleRequest:
    def __init__(self, method, url, body, timeout, headers):
        self.method = method
        self.url = url
        self.body = body
        self.timeout = timeout
        self.headers = headers

    @property
    def json(self):
        return json.loads(self.body)


@pytest.fixture
def zoom(monkeypatch):
    fake = FakeZoom()
    monkeypatch.setattr(HTTPAdapter, "send", lambda self, request, **kw: fake.send(request, **kw))
    return fake


@pytest.fixture
def zoom_plugin():
    from dispatch.plugins.dispatch_zoom.config import ZoomConfiguration
    from dispatch.plugins.dispatch_zoom.plugin import ZoomConferencePlugin

    plugin = ZoomConferencePlugin()
    plugin.configuration = ZoomConfiguration(
        api_user_id=API_USER_ID, api_key=API_KEY, api_secret=API_SECRET
    )
    return plugin


def test_create_returns_the_weblink_id_and_challenge(zoom, zoom_plugin):
    conference = zoom_plugin.create("dispatch-incident-1")

    assert conference["weblink"] == "https://zoom.us/j/987654321"
    assert conference["id"] == 987654321
    assert conference["challenge"] == "zoompass"


def test_the_title_becomes_the_meeting_topic(zoom, zoom_plugin):
    zoom_plugin.create("dispatch-incident-1", title="Payments API degraded")

    assert zoom.requests[-1].json["topic"] == "Payments API degraded"


def test_the_topic_falls_back_to_a_situation_room(zoom, zoom_plugin):
    zoom_plugin.create("dispatch-incident-1")

    assert zoom.requests[-1].json["topic"] == "Situation Room for dispatch-incident-1"


def test_the_configured_duration_is_sent(zoom, zoom_plugin):
    zoom_plugin.configuration.default_duration_minutes = 90
    zoom_plugin.create("dispatch-incident-1")

    assert zoom.requests[-1].json["duration"] == 90


def test_the_meeting_is_password_protected(zoom, zoom_plugin):
    zoom_plugin.create("dispatch-incident-1")

    assert zoom.requests[-1].json["password"]


def test_the_zoom_call_has_a_timeout(zoom, zoom_plugin):
    zoom_plugin.create("dispatch-incident-1")

    assert zoom.requests[-1].timeout


def test_delete_calls_zoom(zoom, zoom_plugin):
    zoom_plugin.delete("987654321")

    assert zoom.requests[-1].method == "DELETE"


def test_create_is_still_instrumented(zoom, zoom_plugin, monkeypatch):
    """``apply`` is shared with the Teams plugin; this guards Zoom's use of it."""
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
                "timer": lambda self, name, value=None, tags=None: emitted.append(
                    tags["function"]
                ),
                "gauge": lambda self, name, value=None, tags=None: None,
            },
        )(),
    )

    zoom_plugin.create("dispatch-incident-1")

    assert any("ZoomConferencePlugin.create" in name for name in emitted)


def test_gen_conference_challenge_is_within_zooms_length_limit():
    from dispatch.plugins.dispatch_zoom.plugin import gen_conference_challenge

    assert len(gen_conference_challenge(8)) == 8
    assert len(gen_conference_challenge(50)) == 10

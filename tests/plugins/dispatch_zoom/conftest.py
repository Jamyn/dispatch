"""Fixtures for the Zoom conference tests.

The fake is installed at ``HTTPAdapter.send``, below ``requests``, so the
plugin's own request construction runs for real against it. Routing matches on
the full URL and the HTTP method, so a wrong path or verb is an error rather
than a silent match.
"""

import json

import pytest
from requests.adapters import HTTPAdapter
from requests.models import Response

API_KEY = "not-a-real-api-key"
API_SECRET = "not-a-real-api-secret"
API_USER_ID = "responder@example.com"

MEETING_ID = "987654321"
JOIN_URL = "https://zoom.us/j/987654321"

CREATE_BODY = {"id": 987654321, "join_url": JOIN_URL, "password": "zoompass"}


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


class FakeZoom:
    """Records every call and answers the endpoints the plugin touches.

    ``response`` is the catch-all the pre-existing tests drive. ``get`` and
    ``patch`` are the read-modify-write halves of the invitee roster; tests
    override either with a ``(status, body)`` pair to force a failure.
    """

    def __init__(self):
        self.requests = []
        self.response = (201, CREATE_BODY)
        self.get = (200, {"id": 987654321, "settings": {"meeting_invitees": []}})
        self.patch = (204, None)

    def meeting_with_invitees(self, *emails) -> dict:
        return {
            "id": 987654321,
            "topic": "Situation Room",
            "settings": {
                "join_before_host": True,
                "meeting_invitees": [{"email": e} for e in emails],
            },
        }

    def _route(self, method, url):
        if method == "GET" and "/meetings/" in url:
            return self.get
        if method == "PATCH" and "/meetings/" in url:
            return self.patch
        return self.response

    def send(self, request, timeout=None, **kwargs):
        self.requests.append(
            SimpleRequest(request.method, request.url, request.body, timeout, dict(request.headers))
        )
        status, body = self._route(request.method, request.url)
        response = Response()
        response.status_code = status
        response.headers["content-type"] = "application/json"
        response._content = b"" if body is None else json.dumps(body).encode()
        response.url = request.url
        response.request = request
        return response


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

"""Fixtures for the Zoom conference tests.

The fake is installed at ``HTTPAdapter.send``, below ``requests``, so the
plugin's own request construction runs for real against it. Routing matches the
token endpoint on its full URL, and the meeting endpoints on verb plus path;
anything unmatched falls through to ``response``.

A ``bytes`` body is returned verbatim, so a test can serve something that is not
JSON at all. Passing a ``str`` would be JSON-encoded into a valid JSON string
and would exercise no parse failure.

Both the OAuth token exchange and the API call are routed, because the token
request is itself a thing under test since issue #70 -- mocking it away would
leave every credential the client sends unasserted.
"""

import json

import pytest
from requests.adapters import HTTPAdapter
from requests.models import Response
from requests.structures import CaseInsensitiveDict

# Obviously fake, and never a real-looking credential.
ACCOUNT_ID = "test-account-id"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
ACCESS_TOKEN = "test-access-token"
API_USER_ID = "responder@example.com"

MEETING_ID = "987654321"
JOIN_URL = "https://zoom.us/j/987654321"

OAUTH_TOKEN_URL = "https://zoom.us/oauth/token"

CREATE_BODY = {"id": 987654321, "join_url": JOIN_URL, "password": "zoompass"}

TOKEN_OK = {
    "access_token": ACCESS_TOKEN,
    "token_type": "bearer",
    "expires_in": 3599,
    "scope": "meeting:write:admin",
}


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

    @property
    def form(self) -> dict:
        """A urlencoded body, as the OAuth token endpoint receives."""
        from urllib.parse import parse_qs

        body = self.body.decode() if isinstance(self.body, bytes) else (self.body or "")
        return {k: v[0] for k, v in parse_qs(body).items()}


class FakeZoom:
    """Records every call and answers the endpoints the plugin touches.

    Tests override ``token`` / ``get`` / ``patch`` / ``response`` with a
    ``(status, body)`` pair to drive a failure path.
    """

    def __init__(self):
        self.requests = []
        self.token = (200, TOKEN_OK)
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
        if url.split("?")[0] == OAUTH_TOKEN_URL and method == "POST":
            return self.token
        if method == "GET" and "/meetings/" in url:
            return self.get
        if method == "PATCH" and "/meetings/" in url:
            return self.patch
        return self.response

    def send(self, request, timeout=None, **kwargs):
        self.requests.append(
            SimpleRequest(
                request.method,
                request.url,
                request.body,
                timeout,
                # Kept case-insensitive, as requests delivers it. Flattening to a
                # plain dict would make every header assertion case-exact and let
                # a harmless re-capitalisation break the suite.
                CaseInsensitiveDict(request.headers),
            )
        )
        status, body = self._route(request.method, request.url)
        response = Response()
        response.status_code = status
        if isinstance(body, bytes):
            # Verbatim, so a test can serve a non-JSON body.
            response.headers["content-type"] = "text/html"
            response._content = body
        else:
            response.headers["content-type"] = "application/json"
            response._content = b"" if body is None else json.dumps(body).encode()
        response.url = request.url
        response.request = request
        return response

    def token_requests(self) -> list[SimpleRequest]:
        return [r for r in self.requests if r.url.split("?")[0] == OAUTH_TOKEN_URL]

    def api_requests(self) -> list[SimpleRequest]:
        """Only the calls to the Zoom API, dropping the token traffic."""
        return [r for r in self.requests if r.url.startswith("https://api.zoom.us/")]

    def last_api_request(self) -> SimpleRequest:
        return self.api_requests()[-1]


@pytest.fixture
def zoom(monkeypatch):
    fake = FakeZoom()
    monkeypatch.setattr(HTTPAdapter, "send", lambda self, request, **kw: fake.send(request, **kw))
    return fake


@pytest.fixture
def zoom_configuration():
    from dispatch.plugins.dispatch_zoom.config import ZoomConfiguration

    return ZoomConfiguration(
        api_user_id=API_USER_ID,
        account_id=ACCOUNT_ID,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )


@pytest.fixture
def zoom_plugin(zoom_configuration):
    from dispatch.plugins.dispatch_zoom.plugin import ZoomConferencePlugin

    plugin = ZoomConferencePlugin()
    plugin.configuration = zoom_configuration
    return plugin

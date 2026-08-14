"""A fake HTTP transport shared by the Microsoft Teams conference tests.

The fake is installed at ``HTTPAdapter.send``, i.e. below ``requests`` and below
MSAL, so both the token exchange and the Graph call run their real code against
it. That placement is the point: mocking ``msal`` or ``requests.post`` directly
would have hidden the defect this suite was written for -- MSAL rejects a
``str`` scope with ``AssertionError`` inside ``acquire_token_for_client``, and
only real MSAL can say so.
"""

import json

from requests.adapters import HTTPAdapter
from requests.models import Response

TENANT_ID = "00000000-0000-0000-0000-000000000001"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
CLIENT_ID = "00000000-0000-0000-0000-000000000002"
USER_ID = "00000000-0000-0000-0000-000000000003"
SECRET = "not-a-real-client-secret"
ACCESS_TOKEN = "not-a-real-access-token"

MEETING_ID = "MSpkYzE3Njc0Yy04MWQ5LTRhZGItYmZi"
JOIN_URL = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_test%40thread.v2/0"

# A trimmed 201 body, keyed to the fields the plugin actually reads.
MEETING_BODY = {
    "id": MEETING_ID,
    "joinWebUrl": JOIN_URL,
    "subject": "Situation Room",
    "joinMeetingIdSettings": {
        "isPasscodeRequired": True,
        "joinMeetingId": "123456789",
        "passcode": "aB3dEf7h",
    },
}

_OIDC_URL = f"{AUTHORITY}/v2.0/.well-known/openid-configuration"

_OIDC_CONFIG = {
    "token_endpoint": f"{AUTHORITY}/oauth2/v2.0/token",
    "authorization_endpoint": f"{AUTHORITY}/oauth2/v2.0/authorize",
    "device_authorization_endpoint": f"{AUTHORITY}/oauth2/v2.0/devicecode",
    "issuer": f"{AUTHORITY}/v2.0",
}

_TOKEN_OK = {"token_type": "Bearer", "expires_in": 3599, "access_token": ACCESS_TOKEN}


def _response(status, body, headers=None):
    response = Response()
    response.status_code = status
    response.headers["content-type"] = "application/json"
    for key, value in (headers or {}).items():
        response.headers[key] = value
    if body is None:
        response._content = b""
    elif isinstance(body, bytes):
        response._content = body
    else:
        response._content = json.dumps(body).encode()
    return response


class RecordedRequest:
    """One intercepted request, with the bits a test wants to assert on."""

    def __init__(self, request, timeout):
        self.method = request.method
        self.url = request.url
        self.headers = dict(request.headers)
        self.timeout = timeout
        self.body = request.body

    @property
    def json(self) -> dict:
        return json.loads(self.body)


class FakeGraph:
    """Routes the three endpoints this plugin touches and records every call.

    Tests override ``token`` / ``meeting`` / ``delete`` with an
    ``(status, body, headers)`` triple to drive a failure path.
    """

    def __init__(self):
        self.requests: list[RecordedRequest] = []
        self.token = (200, _TOKEN_OK, {})
        self.meeting = (201, MEETING_BODY, {})
        self.delete = (204, None, {})

    def _route(self, url):
        if "openid-configuration" in url:
            return (200, _OIDC_CONFIG, {})
        # msal consults instance discovery for authority aliases when a token
        # request fails; an empty metadata list ends that search.
        if "discovery/instance" in url:
            return (200, {"tenant_discovery_endpoint": _OIDC_URL, "metadata": []}, {})
        if "/oauth2/v2.0/token" in url:
            return self.token
        if "/onlineMeetings" in url:
            return self.meeting
        raise AssertionError(f"unexpected request to {url}")

    def send(self, request, timeout=None, **kwargs):
        recorded = RecordedRequest(request, timeout)
        self.requests.append(recorded)
        if request.method == "DELETE":
            status, body, headers = self.delete
        else:
            status, body, headers = self._route(request.url)
        response = _response(status, body, headers)
        response.url = request.url
        response.request = request
        return response

    def graph_requests(self) -> list[RecordedRequest]:
        """Only the calls to Graph, dropping MSAL's token traffic."""
        return [r for r in self.requests if "graph.microsoft.com" in r.url]

    def last_graph_request(self) -> RecordedRequest:
        return self.graph_requests()[-1]

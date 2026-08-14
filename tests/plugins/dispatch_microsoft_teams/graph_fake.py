"""A fake HTTP transport shared by the Microsoft Teams conference tests.

The fake is installed at ``HTTPAdapter.send``, i.e. below ``requests`` and below
MSAL, so both the token exchange and the Graph call run their real code against
it. That placement is the point: mocking ``msal`` or ``requests.post`` directly
would have hidden the defect this suite was written for -- MSAL rejects a
``str`` scope with ``AssertionError`` inside ``acquire_token_for_client``, and
only real MSAL can say so.

Routing matches on the *full* URL prefix and on the HTTP method, not on a
substring, so a wrong host, a wrong API version or a wrong verb is an error
rather than a silent match. The token request body is recorded too -- without
that, every credential the plugin hands MSAL is untestable.
"""

import json
from urllib.parse import parse_qs, urlparse

from requests.models import Response

TENANT_ID = "00000000-0000-0000-0000-000000000001"
AUTHORITY = f"https://login.microsoftonline.com/{TENANT_ID}"
CLIENT_ID = "00000000-0000-0000-0000-000000000002"
USER_ID = "00000000-0000-0000-0000-000000000003"
SECRET = "not-a-real-client-secret"
ACCESS_TOKEN = "not-a-real-access-token"

GRAPH_HOST = "graph.microsoft.com"
GRAPH_BASE = f"https://{GRAPH_HOST}/v1.0"
MEETINGS_URL = f"{GRAPH_BASE}/users/{USER_ID}/onlineMeetings"

MEETING_ID = "MSpkYzE3Njc0Yy04MWQ5LTRhZGItYmZi"
JOIN_URL = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_test%40thread.v2/0"
PASSCODE = "aB3dEf7h"
JOIN_MEETING_ID = "123 456 789"

# A trimmed 201 body, keyed to the fields the plugin actually reads.
MEETING_BODY = {
    "id": MEETING_ID,
    "joinWebUrl": JOIN_URL,
    "subject": "Situation Room",
    "joinMeetingIdSettings": {
        "isPasscodeRequired": True,
        "joinMeetingId": JOIN_MEETING_ID,
        "passcode": PASSCODE,
    },
}

# Graph returns the organizer alongside the attendees and rejects any attempt to
# change it, so the plugin has to carry it through a read-modify-write untouched.
ORGANIZER = {
    "upn": "organizer@example.com",
    "role": "presenter",
    "identity": {"user": {"id": USER_ID, "displayName": None, "tenantId": TENANT_ID}},
}


def attendee(upn: str, role: str = "attendee", **extra) -> dict:
    """One attendee as Graph returns it, including the identity it resolved."""
    return {
        "upn": upn,
        "role": role,
        "identity": {"user": {"id": f"id-for-{upn}", "displayName": upn}},
        **extra,
    }


_OIDC_URL = f"{AUTHORITY}/v2.0/.well-known/openid-configuration"
_TOKEN_URL = f"{AUTHORITY}/oauth2/v2.0/token"

_OIDC_CONFIG = {
    "token_endpoint": _TOKEN_URL,
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

    @property
    def form(self) -> dict:
        """A urlencoded body, as the OAuth token endpoint receives."""
        body = self.body.decode() if isinstance(self.body, bytes) else (self.body or "")
        return {k: v[0] for k, v in parse_qs(body).items()}

    @property
    def path(self) -> str:
        return urlparse(self.url).path

    @property
    def host(self) -> str:
        return urlparse(self.url).hostname or ""


class FakeGraph:
    """Routes the endpoints this plugin touches and records every call.

    Tests override ``token`` / ``meeting`` / ``delete`` / ``get`` / ``patch``
    with an ``(status, body, headers)`` triple to drive a failure path.
    """

    def __init__(self):
        self.requests: list[RecordedRequest] = []
        self.token = (200, _TOKEN_OK, {})
        self.meeting = (201, MEETING_BODY, {})
        self.delete = (204, None, {})
        # The read half of the attendee read-modify-write. Graph requires the
        # full attendee list on every PATCH, so the plugin must GET first.
        self.get = (200, MEETING_BODY, {})
        self.patch = (200, MEETING_BODY, {})

    def _route(self, method, url):
        base = url.split("?")[0]

        if base == _OIDC_URL and method == "GET":
            return (200, _OIDC_CONFIG, {})
        # msal consults instance discovery for authority aliases when a token
        # request fails; an empty metadata list ends that search.
        if base.startswith("https://login.microsoftonline.com/common/discovery/instance"):
            return (200, {"tenant_discovery_endpoint": _OIDC_URL, "metadata": []}, {})
        if base == _TOKEN_URL and method == "POST":
            return self.token
        if base == MEETINGS_URL and method == "POST":
            return self.meeting
        if base.startswith(f"{MEETINGS_URL}/") and method == "DELETE":
            return self.delete
        if base.startswith(f"{MEETINGS_URL}/") and method == "GET":
            return self.get
        if base.startswith(f"{MEETINGS_URL}/") and method == "PATCH":
            return self.patch

        raise AssertionError(f"unexpected request: {method} {url}")

    def send(self, request, timeout=None, **kwargs):
        recorded = RecordedRequest(request, timeout)
        self.requests.append(recorded)
        status, body, headers = self._route(request.method, request.url)
        response = _response(status, body, headers)
        response.url = request.url
        response.request = request
        return response

    def meeting_with_attendees(self, *attendees) -> dict:
        """A GET body carrying the given attendees, as Graph returns them."""
        return {
            **MEETING_BODY,
            "participants": {"organizer": ORGANIZER, "attendees": list(attendees)},
        }

    def graph_requests(self) -> list[RecordedRequest]:
        """Only the calls to Graph, dropping MSAL's token traffic.

        Matches the parsed host rather than a substring, so a URL that merely
        mentions the host somewhere -- a query parameter, say -- is not counted
        as a call to it.
        """
        return [r for r in self.requests if r.host == GRAPH_HOST]

    def last_graph_request(self) -> RecordedRequest:
        return self.graph_requests()[-1]

    def token_requests(self) -> list[RecordedRequest]:
        return [r for r in self.requests if r.url.split("?")[0] == _TOKEN_URL]

    def last_token_request(self) -> RecordedRequest:
        return self.token_requests()[-1]

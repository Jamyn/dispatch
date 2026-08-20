"""An in-memory Confluence, for driving the real plugins against (issue #214).

The fake is installed as a ``requests`` transport adapter, below
atlassian-python-api, so the client's own URL building, ``/wiki`` context-path
handling, basic auth, JSON encoding, response parsing and error raising all run
for real against it. Replacing the client object with a mock instead would
assert only that the plugin called something -- and issue #214 is precisely a
case where the call was made, the constructor succeeded, and no method existed.

Both REST versions are served, because 5.x reaches Confluence Cloud over v2 and
Confluence Server over v1, and the point of the migration is that each
deployment type reaches the right one.
"""

import json

import requests
from requests.adapters import HTTPAdapter

# Obviously fake, and never a real-looking credential.
USERNAME = "dispatch-tests@example.com"
PASSWORD = "not-a-real-api-token"

# Deliberately not "https://example.atlassian.net": ConfluenceV2 appends the
# /wiki context path only for Cloud hostnames, and a test written against a
# non-Cloud host would not notice if that stopped happening.
CLOUD_URL = "https://dispatch-tests.atlassian.net/"
SERVER_URL = "https://confluence.internal.example.com/"

# The configured space key, and the numeric id Cloud's v2 API demands instead.
SPACE_KEY = "INC"
SPACE_ID = 655361

TEMPLATE_ID = "111111"
PARENT_ID = "222222"
ROOT_PAGE_ID = "333333"

TEMPLATE_BODY = "<p>Commander: {{commander}} Status: {{status}}</p>"


def build_configuration(hosting_type: str, **overrides):
    """A ConfluenceConfiguration as the storage plugin really receives it."""
    from dispatch.plugins.dispatch_atlassian_confluence.plugin import ConfluenceConfiguration

    values = {
        "api_url": CLOUD_URL if hosting_type == "cloud" else SERVER_URL,
        "hosting_type": hosting_type,
        "username": USERNAME,
        "password": PASSWORD,
        "template_id": TEMPLATE_ID,
        "root_id": SPACE_KEY,
        "parent_id": PARENT_ID,
    }
    values.update(overrides)
    return ConfluenceConfiguration(**values)


def build_document_configuration(hosting_type: str, **overrides):
    """A ConfluenceConfigurationBase, as the document plugin really receives it."""
    from dispatch.plugins.dispatch_atlassian_confluence.config import (
        ConfluenceConfigurationBase,
    )

    values = {
        "api_url": CLOUD_URL if hosting_type == "cloud" else SERVER_URL,
        "hosting_type": hosting_type,
        "username": USERNAME,
        "password": PASSWORD,
    }
    values.update(overrides)
    return ConfluenceConfigurationBase(**values)


class Request:
    """One HTTP request as Confluence would have received it."""

    def __init__(self, prepared):
        self.method = prepared.method
        self.url = prepared.url
        self.path = requests.utils.urlparse(prepared.url).path
        self.query = requests.utils.urlparse(prepared.url).query
        self.headers = prepared.headers
        self.body = json.loads(prepared.body) if prepared.body else None

    def __repr__(self):
        return f"<Request {self.method} {self.url}>"


class FakeConfluence:
    """Records what reached the wire and decides what comes back.

    ``requests`` holds one entry per HTTP request the client actually issued, so
    a test asserts on the request Confluence would have received rather than on
    the arguments of an intercepted method call.
    """

    def __init__(self):
        self.requests = []
        self.failure = None

    # -- what comes back ---------------------------------------------------

    def fail_with(self, status: int, body: dict | None = None):
        """Every subsequent request fails, in Confluence's error envelope."""
        self.failure = (status, body if body is not None else {"message": "boom"})

    # -- the routes --------------------------------------------------------

    def handle(self, request: Request):
        if self.failure:
            status, body = self.failure
            return status, body

        path, method = request.path, request.method

        # v2 (Cloud)
        if path == "/wiki/api/v2/spaces" and method == "GET":
            return 200, {"results": [{"id": SPACE_ID, "key": SPACE_KEY, "name": "Incidents"}]}
        if path == "/wiki/api/v2/pages" and method == "POST":
            return 200, self._v2_page(
                page_id="900001",
                title=request.body["title"],
                body=request.body["body"]["storage"]["value"],
            )
        if path.startswith("/wiki/api/v2/pages/") and method == "GET":
            return 200, self._v2_page(
                page_id=path.rsplit("/", 1)[1], title="Incident Template", body=TEMPLATE_BODY
            )
        if path.startswith("/wiki/api/v2/pages/") and method == "PUT":
            return 200, self._v2_page(
                page_id=path.rsplit("/", 1)[1],
                title=request.body["title"],
                body=request.body["body"]["storage"]["value"],
                version=request.body["version"]["number"],
            )

        # move_file_confluence issues this one with `requests` directly rather
        # than through the client, and was left on v1 by the 5.x migration.
        if "/move/append/" in path and method == "PUT":
            return 200, {}

        # v1 (Server)
        if path.endswith("/history") and method == "GET":
            # update_page reads the current version from here before its PUT.
            return 200, {"lastUpdated": {"number": 1}}
        if path.rstrip("/") == "/rest/api/content" and method == "POST":
            return 200, self._v1_page(
                page_id="900001",
                title=request.body["title"],
                body=request.body["body"]["storage"]["value"],
            )
        if path.startswith("/rest/api/content/") and method == "GET":
            return 200, self._v1_page(
                page_id=path.rsplit("/", 1)[1], title="Incident Template", body=TEMPLATE_BODY
            )
        if path.startswith("/rest/api/content/") and method == "PUT":
            return 200, self._v1_page(
                page_id=path.rsplit("/", 1)[1],
                title=request.body["title"],
                body=request.body["body"]["storage"]["value"],
                version=request.body["version"]["number"],
            )

        return 404, {"message": f"no route for {method} {path}"}

    @staticmethod
    def _v2_page(*, page_id, title, body, version=1):
        return {
            "id": page_id,
            "status": "current",
            "title": title,
            "spaceId": str(SPACE_ID),
            "version": {"number": version},
            "body": {"storage": {"value": body, "representation": "storage"}},
        }

    @staticmethod
    def _v1_page(*, page_id, title, body, version=1):
        return {
            "id": page_id,
            "type": "page",
            "status": "current",
            "title": title,
            "space": {"key": SPACE_KEY},
            "version": {"number": version},
            "body": {"storage": {"value": body, "representation": "storage"}},
        }

    # -- assertions helpers ------------------------------------------------

    def last(self, method: str) -> Request:
        """The most recent request issued with ``method``."""
        for request in reversed(self.requests):
            if request.method == method:
                return request
        raise AssertionError(f"no {method} request was issued; got {self.requests}")


class _Adapter(HTTPAdapter):
    """Serves ``FakeConfluence`` instead of opening a socket."""

    def __init__(self, api: FakeConfluence):
        self.api = api
        super().__init__()

    def send(self, request, **kwargs):
        recorded = Request(request)
        self.api.requests.append(recorded)
        status, payload = self.api.handle(recorded)

        response = requests.Response()
        response.status_code = status
        response.reason = "OK" if status < 400 else "Error"
        response.url = request.url
        response.request = request
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(payload).encode()
        return response


def session_for(api: FakeConfluence) -> requests.Session:
    """A requests session whose every request is served from ``api``."""
    session = requests.Session()
    session.mount("https://", _Adapter(api))
    session.mount("http://", _Adapter(api))
    return session

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

Pages are stored rather than synthesised, so a page's parent and space are
whatever it was really created with. Issue #242 is about a hierarchy, and a
fake that answers every read with the same invented page cannot tell a page
filed under the right parent from one filed under none.

Titles are held unique per space, as Confluence holds them. Dispatch names a
subject's folders from project settings, so every subject asks for the same
two titles, and a fake that accepts them proves the opposite of the truth.
"""

import json
from urllib.parse import parse_qsl, urlparse

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

# The space every seeded page lives in, under both of its identifiers.
SPACE_KEY = "INC"
SPACE_ID = 655361

TEMPLATE_ID = "111111"
ROOT_PAGE_ID = "333333"

TEMPLATE_BODY = "<p>Commander: {{commander}} Status: {{status}}</p>"

# What the instance believes its own address to be, which is not the address
# Dispatch was configured with. A weblink built from `_links.base` lands here.
REPORTED_BASE = "https://confluence.reported.invalid/wiki"

# Straight out of Atlassian's published v2 OpenAPI spec
# (developer.atlassian.com/cloud/confluence/openapi-v2.v3.json): the client
# sends only what it is told to, so a wrapper that omits one of these builds a
# request the API rejects and no amount of method-level mocking would notice.
V2_CREATE_REQUIRED = ("spaceId",)
V2_UPDATE_REQUIRED = ("id", "status", "title", "body", "version")

# PrimaryBodyRepresentationSingle, from the same spec. `none` is not a member,
# so the client's get_body=False is not a way to skip the body.
V2_BODY_FORMATS = ("storage", "atlas_doc_format", "view", "export_view", "editor")


def build_configuration(hosting_type: str, **overrides):
    """A ConfluenceConfiguration as the storage plugin really receives it."""
    from dispatch.plugins.dispatch_atlassian_confluence.plugin import ConfluenceConfiguration

    values = {
        "api_url": CLOUD_URL if hosting_type == "cloud" else SERVER_URL,
        "hosting_type": hosting_type,
        "username": USERNAME,
        "password": PASSWORD,
        "template_id": TEMPLATE_ID,
        "root_id": ROOT_PAGE_ID,
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
        parsed = urlparse(prepared.url)
        self.path = parsed.path
        self.query = parsed.query
        self.params = dict(parse_qsl(parsed.query))
        self.headers = prepared.headers
        self.body = json.loads(prepared.body) if prepared.body else None

    def __repr__(self):
        return f"<Request {self.method} {self.url}>"


class Page:
    """A stored page, rendered into whichever API version asked for it."""

    def __init__(self, page_id, title, body, parent_id=None, version=1):
        self.id = page_id
        self.title = title
        self.body = body
        self.parent_id = parent_id
        self.version = version

    @property
    def _webui(self):
        return f"/spaces/{SPACE_KEY}/pages/{self.id}/{self.title.replace(' ', '+')}"

    def as_v2(self, *, body_format=None, with_base=False):
        page = {
            "id": self.id,
            "status": "current",
            "title": self.title,
            "spaceId": str(SPACE_ID),
            "parentId": self.parent_id,
            "version": {"number": self.version},
            "_links": {"webui": self._webui},
        }
        # v2 returns a body only for the representation that was asked for.
        if body_format:
            page["body"] = {body_format: {"value": self.body, "representation": body_format}}
        if with_base:
            page["_links"]["base"] = REPORTED_BASE
        return page

    def as_v1(self, *, expand=()):
        page = {
            "id": self.id,
            "type": "page",
            "status": "current",
            "title": self.title,
            "version": {"number": self.version},
            "_links": {
                "base": REPORTED_BASE,
                "webui": f"/display/{SPACE_KEY}/{self.title.replace(' ', '+')}",
            },
        }
        # v1 returns only what the caller expanded, so a missing expand is
        # visible here rather than silently supplied.
        if "space" in expand:
            page["space"] = {"key": SPACE_KEY, "name": "Incidents"}
        if "body.storage" in expand:
            page["body"] = {"storage": {"value": self.body, "representation": "storage"}}
        if self.parent_id and "ancestors" in expand:
            page["ancestors"] = [{"type": "page", "id": self.parent_id}]
        return page


class FakeConfluence:
    """Records what reached the wire and decides what comes back.

    ``requests`` holds one entry per HTTP request the client actually issued, so
    a test asserts on the request Confluence would have received rather than on
    the arguments of an intercepted method call.
    """

    def __init__(self):
        self.requests = []
        self.failure = None
        self.omit_webui = False
        self.next_id = 900001
        self.pages = {
            ROOT_PAGE_ID: Page(ROOT_PAGE_ID, "Incidents", "<p>Incident home</p>"),
            TEMPLATE_ID: Page(TEMPLATE_ID, "Incident Template", TEMPLATE_BODY),
        }

    # -- what comes back ---------------------------------------------------

    def fail_with(self, status: int, body: dict | None = None):
        """Every subsequent request fails, in Confluence's error envelope."""
        self.failure = (status, body if body is not None else {"message": "boom"})

    def page(self, page_id: str) -> Page:
        """The stored page, for asserting on where it ended up."""
        return self.pages[page_id]

    def titled(self, title: str) -> Page:
        """The one stored page with this title -- there can only be one."""
        matches = [page for page in self.pages.values() if page.title == title]
        assert len(matches) == 1, f"{title!r} matched {len(matches)} pages"
        return matches[0]

    def _title_taken(self, title: str) -> bool:
        # Confluence enforces unique titles per space, not per parent: two
        # incidents cannot each own a page called "Logs". Every page the fake
        # holds is in one space, so any match is a collision.
        return any(page.title == title for page in self.pages.values())

    def _store(self, title, body, parent_id):
        page = Page(str(self.next_id), title, body, parent_id=parent_id)
        self.next_id += 1
        self.pages[page.id] = page
        return page

    def _render(self, page, **kwargs):
        rendered = page.as_v2(**kwargs) if "body_format" in kwargs else page.as_v1(**kwargs)
        if self.omit_webui:
            rendered["_links"].pop("webui", None)
        return rendered

    # -- the routes --------------------------------------------------------

    def handle(self, request: Request):
        if self.failure:
            return self.failure

        path, method = request.path, request.method

        # v2 (Cloud). Matched on the API path alone: the context path in front
        # of it is /wiki for a tenant URL and /ex/confluence/{cloudId} for a
        # gateway one, and getting that wrong is the bug under test.
        if path.endswith("/api/v2/pages") and method == "POST":
            return self._v2_create(request)
        if "/api/v2/pages/" in path and method == "GET":
            return self._v2_get(request)
        if "/api/v2/pages/" in path and method == "PUT":
            return self._v2_update(request)

        # v1 (Server)
        if path.endswith("/history") and method == "GET":
            # update_page reads the current version from here before its PUT.
            page = self.pages.get(path.split("/")[-2])
            if page is None:
                return 404, {"message": "no such page"}
            return 200, {"lastUpdated": {"number": page.version}}
        if path.rstrip("/").endswith("/rest/api/content") and method == "POST":
            return self._v1_create(request)
        if "/rest/api/content/" in path and method == "GET":
            return self._v1_get(request)
        if "/rest/api/content/" in path and method == "PUT":
            return self._v1_update(request)

        return 404, {"message": f"no route for {method} {path}"}

    # -- v2 ----------------------------------------------------------------

    def _v2_create(self, request):
        missing = self._missing(request.body, V2_CREATE_REQUIRED)
        if missing:
            return 400, {"errors": [{"title": f"missing required field(s): {missing}"}]}
        if request.body["spaceId"] != str(SPACE_ID):
            return 404, {"errors": [{"title": "space not found"}]}
        parent_id = request.body.get("parentId")
        if parent_id is not None and parent_id not in self.pages:
            return 400, {"errors": [{"title": f"no such parent {parent_id}"}]}
        if self._title_taken(request.body["title"]):
            return 400, {
                "errors": [
                    {"title": f"A page with this title already exists: {request.body['title']}"}
                ]
            }
        page = self._store(
            request.body["title"], request.body["body"]["storage"]["value"], parent_id
        )
        return 200, self._render(page, body_format="storage", with_base=True)

    def _v2_get(self, request):
        page = self.pages.get(request.path.rsplit("/", 1)[1])
        if page is None:
            return 404, {"errors": [{"title": "page not found"}]}
        body_format = request.params.get("body-format")
        if body_format is not None and body_format not in V2_BODY_FORMATS:
            return 400, {"errors": [{"title": f"invalid body-format {body_format}"}]}
        return 200, self._render(page, body_format=body_format)

    def _v2_update(self, request):
        missing = self._missing(request.body, V2_UPDATE_REQUIRED)
        if missing:
            return 400, {"errors": [{"title": f"missing required field(s): {missing}"}]}
        page = self.pages.get(request.path.rsplit("/", 1)[1])
        if page is None:
            return 404, {"errors": [{"title": "page not found"}]}
        page.title = request.body["title"]
        page.body = request.body["body"]["storage"]["value"]
        page.version = request.body["version"]["number"]
        return 200, self._render(page, body_format="storage")

    # -- v1 ----------------------------------------------------------------

    def _v1_create(self, request):
        if request.body["space"]["key"] != SPACE_KEY:
            return 400, {"message": f"unknown space key {request.body['space']['key']}"}
        ancestors = request.body.get("ancestors") or []
        parent_id = ancestors[0]["id"] if ancestors else None
        if parent_id is not None and parent_id not in self.pages:
            return 400, {"message": f"no such parent {parent_id}"}
        if self._title_taken(request.body["title"]):
            return 400, {
                "message": f"A page with this title already exists: {request.body['title']}"
            }
        page = self._store(
            request.body["title"], request.body["body"]["storage"]["value"], parent_id
        )
        return 200, self._render(page, expand=("space", "body.storage", "ancestors"))

    def _v1_get(self, request):
        page = self.pages.get(request.path.rsplit("/", 1)[1])
        if page is None:
            return 404, {"message": "page not found"}
        expand = tuple(filter(None, request.params.get("expand", "").split(",")))
        return 200, self._render(page, expand=expand)

    def _v1_update(self, request):
        page = self.pages.get(request.path.rsplit("/", 1)[1])
        if page is None:
            return 404, {"message": "page not found"}
        page.title = request.body["title"]
        page.body = request.body["body"]["storage"]["value"]
        page.version = request.body["version"]["number"]
        return 200, self._render(page, expand=("space", "body.storage"))

    @staticmethod
    def _missing(body, required):
        return sorted(field for field in required if not body.get(field))

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

"""An in-memory Jira, for driving the real ticket plugin against.

Mounted as a ``requests`` transport adapter beneath the ``jira`` client, so the
client's own URL building, authentication, JSON encoding and error raising all
run for real. Replacing the client with a mock would assert only that the
plugin called something, and the plugin's failure mode is precisely that it
calls something, catches whatever comes back, and returns a ticket that never
reached Jira.

Issues are stored rather than synthesised, so what a test reads back is what
the plugin actually sent.

One path escapes the client: ``get_cloud_user_account_id_by_email`` builds its
own ``requests`` call rather than going through the session, so the conftest
points the plugin's ``requests`` module here too.
"""

import json
from urllib.parse import parse_qsl, urlparse

import requests
from requests.adapters import HTTPAdapter

# Obviously fake, and never a real-looking credential.
URL = "https://jira.example.com"
USERNAME = "dispatch@example.com"
PASSWORD = "not-a-real-api-token"

PROJECT_KEY = "INC"
PROJECT_ID = "10000"
ISSUE_TYPE = "Task"

# The account the plugin authenticates as, and the one it falls back to when it
# cannot resolve a commander.
DISPATCH_ACCOUNT_ID = "557058:dispatch-service-account"
DISPATCH_NAME = "dispatch"

COMMANDER_EMAIL = "ada@example.com"
COMMANDER_ACCOUNT_ID = "557058:ada"
COMMANDER_NAME = "ada"

TRANSITIONS = [
    {"id": "11", "name": "To Do"},
    {"id": "21", "name": "In Progress"},
    {"id": "31", "name": "Done"},
]


def build_configuration(hosting_type: str = "cloud", **overrides):
    """A JiraConfiguration as the plugin really receives it."""
    from dispatch.plugins.dispatch_jira.plugin import JiraConfiguration

    values = {
        "api_url": URL,
        "browser_url": URL,
        "hosting_type": hosting_type,
        "username": USERNAME,
        "password": PASSWORD,
        "default_project_id": PROJECT_KEY,
        "default_issue_type_name": ISSUE_TYPE,
    }
    values.update(overrides)
    return JiraConfiguration(**values)


class Request:
    """One HTTP request as Jira would have received it."""

    def __init__(self, method, url, body):
        self.method = method
        self.url = url
        parsed = urlparse(url)
        self.path = parsed.path
        self.params = dict(parse_qsl(parsed.query))
        self.body = json.loads(body) if body else None

    def __repr__(self):
        return f"<Request {self.method} {self.url}>"


class Issue:
    """A stored issue, rendered as the REST API renders one."""

    def __init__(self, key, fields):
        self.key = key
        self.fields = dict(fields)
        self.status = "To Do"

    def as_json(self):
        fields = dict(self.fields)
        fields["status"] = {"name": self.status}
        return {
            "id": self.key.split("-")[-1],
            "key": self.key,
            # The client dereferences this to update or delete the issue.
            "self": f"{URL}/rest/api/2/issue/{self.key}",
            "fields": fields,
        }


class FakeJira:
    """Records what reached the wire and decides what comes back."""

    def __init__(self):
        self.requests = []
        self.failure = None
        self.issues = {}
        self.next_number = 1
        # Who groupuserpicker can find. An address absent here is the case the
        # plugin answers by quietly assigning its own account.
        self.users = {
            COMMANDER_EMAIL: COMMANDER_ACCOUNT_ID,
            USERNAME: DISPATCH_ACCOUNT_ID,
        }

    def fail_with(self, status: int, body: dict | None = None):
        self.failure = (status, body if body is not None else {"errorMessages": ["boom"]})

    def issue(self, key: str) -> Issue:
        return self.issues[key]

    def last(self, method: str) -> Request:
        for request in reversed(self.requests):
            if request.method == method:
                return request
        raise AssertionError(f"no {method} request was issued; got {self.requests}")

    # -- the routes --------------------------------------------------------

    def handle(self, request: Request):
        if self.failure:
            return self.failure

        path, method = request.path, request.method

        if path.endswith("/serverInfo"):
            return 200, {
                "baseUrl": URL,
                "version": "9.4.0",
                "versionNumbers": [9, 4, 0],
                "deploymentType": "Cloud",
            }
        if path.endswith("/field"):
            return 200, []
        if path.endswith("/groupuserpicker"):
            return self._user_picker(request)
        if "/user/search" in path or path.endswith("/user/picker"):
            return self._user_search(request)
        if path.endswith("/transitions"):
            if method == "GET":
                return 200, {"transitions": TRANSITIONS}
            return self._transition(request)
        if path.endswith("/rest/api/2/issue") and method == "POST":
            return self._create(request)
        if "/rest/api/2/issue/" in path:
            return self._issue_route(request)

        return 404, {"errorMessages": [f"no route for {method} {path}"]}

    def _user_picker(self, request):
        query = request.params.get("query", "")
        account_id = self.users.get(query)
        users = [{"accountId": account_id, "name": query}] if account_id else []
        return 200, {"users": {"users": users}}

    def _user_search(self, request):
        query = request.params.get("username") or request.params.get("query", "")
        names = {COMMANDER_NAME, DISPATCH_NAME}
        matches = (
            [{"name": query, "accountId": query, "self": f"{URL}/rest/api/2/user?username={query}"}]
            if query in names
            else []
        )
        return 200, matches

    def _create(self, request):
        fields = request.body["fields"]
        key = f"{PROJECT_KEY}-{self.next_number}"
        self.next_number += 1
        self.issues[key] = Issue(key, fields)
        return 201, {"id": key.split("-")[-1], "key": key, "self": f"{URL}/rest/api/2/issue/{key}"}

    def _issue_route(self, request):
        key = request.path.rstrip("/").split("/rest/api/2/issue/")[1].split("/")[0]
        issue = self.issues.get(key)
        if issue is None:
            return 404, {"errorMessages": [f"Issue does not exist: {key}"]}
        if request.method == "GET":
            return 200, issue.as_json()
        if request.method == "PUT":
            issue.fields.update(request.body.get("fields") or {})
            return 204, {}
        if request.method == "DELETE":
            del self.issues[key]
            return 204, {}
        return 405, {"errorMessages": ["method not allowed"]}

    def _transition(self, request):
        key = request.path.rstrip("/").split("/rest/api/2/issue/")[1].split("/")[0]
        wanted = str(request.body["transition"]["id"])
        for transition in TRANSITIONS:
            if str(transition["id"]) == wanted:
                self.issues[key].status = transition["name"]
                return 204, {}
        return 400, {"errorMessages": [f"no transition {wanted}"]}


class _Adapter(HTTPAdapter):
    """Serves ``FakeJira`` instead of opening a socket."""

    def __init__(self, api: FakeJira):
        self.api = api
        super().__init__()

    def send(self, request, **kwargs):
        recorded = Request(request.method, request.url, request.body)
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


def mount(session, api: FakeJira):
    session.mount("https://", _Adapter(api))
    session.mount("http://", _Adapter(api))
    return session

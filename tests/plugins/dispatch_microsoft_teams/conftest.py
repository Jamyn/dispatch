"""Fixtures for the Microsoft Teams conference tests.

The fake transport itself lives in ``graph_fake`` so the test modules can
import its constants without importing a conftest.
"""

from urllib.parse import urlparse

import pytest
from requests.adapters import HTTPAdapter

from dispatch.plugins.dispatch_microsoft_teams.conference import client as client_module
from tests.plugins.dispatch_microsoft_teams.graph_fake import (
    AUTHORITY,
    CLIENT_ID,
    GRAPH_HOST,
    MEETING_BODY,
    SECRET,
    USER_ID,
    FakeGraph,
    RecordedRequest,
    _response,
)


@pytest.fixture
def graph(monkeypatch):
    """Install the fake transport for the duration of one test.

    The client shares one msal ``http_cache`` process-wide so tenant discovery
    and msal's throttling state survive across calls. That is deliberate in
    production and order-dependence in tests, so it is emptied per test.
    """
    client_module.__dict__["_MSAL_HTTP_CACHE"].clear()
    fake = FakeGraph()
    monkeypatch.setattr(HTTPAdapter, "send", lambda self, request, **kw: fake.send(request, **kw))
    return fake


@pytest.fixture
def graph_capture(graph, monkeypatch):
    """Like ``graph``, but answers *any* Graph-host request instead of routing
    on a literal ``MEETINGS_URL``-prefix match.

    Path-encoding regression tests deliberately supply ids that -- before the
    fix -- retarget the request to a different path entirely. ``FakeGraph``'s
    strict routing would raise "unexpected request" for those before a test
    ever got to inspect the URL. This fixture still delegates token/OIDC
    traffic to the real fake so authentication continues to run for real.
    """
    captured: list[RecordedRequest] = []

    def send(self, request, timeout=None, **kwargs):
        if urlparse(request.url).hostname == GRAPH_HOST:
            captured.append(RecordedRequest(request, timeout))
            status = 204 if request.method == "DELETE" else 200
            body = None if request.method == "DELETE" else MEETING_BODY
            response = _response(status, body)
            response.url = request.url
            response.request = request
            return response
        return graph.send(request, **kwargs)

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return captured


@pytest.fixture
def teams_configuration():
    from dispatch.plugins.dispatch_microsoft_teams.conference.config import (
        MicrosoftTeamsConfiguration,
    )

    return MicrosoftTeamsConfiguration(
        authority=AUTHORITY,
        client_id=CLIENT_ID,
        secret=SECRET,
        user_id=USER_ID,
    )


class RecordingMetrics:
    def __init__(self):
        self.counters = []
        self.timers = []

    def counter(self, name, value=None, tags=None):
        self.counters.append((name, tags))

    def timer(self, name, value=None, tags=None):
        self.timers.append((name, tags))

    def gauge(self, name, value=None, tags=None):
        pass


@pytest.fixture
def metrics(monkeypatch):
    recorder = RecordingMetrics()
    monkeypatch.setattr("dispatch.decorators.metrics_provider", recorder)
    return recorder


@pytest.fixture
def teams_plugin(teams_configuration):
    from dispatch.plugins.dispatch_microsoft_teams.conference.plugin import (
        MicrosoftTeamsConferencePlugin,
    )

    plugin = MicrosoftTeamsConferencePlugin()
    plugin.configuration = teams_configuration
    return plugin

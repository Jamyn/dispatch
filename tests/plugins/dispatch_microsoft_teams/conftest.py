"""Fixtures for the Microsoft Teams conference tests.

The fake transport itself lives in ``graph_fake`` so the test modules can
import its constants without importing a conftest.
"""

import pytest
from requests.adapters import HTTPAdapter

from dispatch.plugins.dispatch_microsoft_teams.conference import client as client_module
from tests.plugins.dispatch_microsoft_teams.graph_fake import (
    AUTHORITY,
    CLIENT_ID,
    SECRET,
    USER_ID,
    FakeGraph,
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

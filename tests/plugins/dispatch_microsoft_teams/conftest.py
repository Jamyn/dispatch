"""Fixtures for the Microsoft Teams conference tests.

The fake transport itself lives in ``graph_fake`` so the test modules can
import its constants without importing a conftest.
"""

import pytest
from requests.adapters import HTTPAdapter

from tests.plugins.dispatch_microsoft_teams.graph_fake import (
    AUTHORITY,
    CLIENT_ID,
    SECRET,
    USER_ID,
    FakeGraph,
)


@pytest.fixture
def graph(monkeypatch):
    """Install the fake transport for the duration of one test."""
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


@pytest.fixture
def teams_plugin(teams_configuration):
    from dispatch.plugins.dispatch_microsoft_teams.conference.plugin import (
        MicrosoftTeamsConferencePlugin,
    )

    plugin = MicrosoftTeamsConferencePlugin()
    plugin.configuration = teams_configuration
    return plugin

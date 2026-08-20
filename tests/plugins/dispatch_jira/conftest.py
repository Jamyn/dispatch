"""Points the plugin's two routes to Jira at the in-memory instance.

The `jira` client fetches server info while it is being constructed, so the
adapter has to be mounted when the session is made rather than afterwards --
hence patching the session class rather than the finished client.
"""

import pytest
import requests
from jira.resilientsession import ResilientSession

from tests.plugins.dispatch_jira.fake_jira import FakeJira, mount


@pytest.fixture
def jira(monkeypatch):
    api = FakeJira()

    class FakeSession(ResilientSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            mount(self, api)

    monkeypatch.setattr("jira.client.ResilientSession", FakeSession)

    # `get_cloud_user_account_id_by_email` builds its own request rather than
    # going through the client's session, so it needs pointing separately.
    class DirectRequests:
        @staticmethod
        def request(method, url, headers=None, auth=None, **kwargs):
            return mount(requests.Session(), api).request(method, url, headers=headers)

    monkeypatch.setattr("dispatch.plugins.dispatch_jira.plugin.requests", DirectRequests)
    return api


@pytest.fixture
def plugin(jira):
    """The real plugin, configured for Cloud, talking to the fake."""
    from dispatch.plugins.dispatch_jira.plugin import JiraTicketPlugin

    from tests.plugins.dispatch_jira.fake_jira import build_configuration

    instance = JiraTicketPlugin()
    instance.configuration = build_configuration("cloud")
    return instance

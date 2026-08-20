"""Fixtures for the Confluence plugin tests (issue #214).

The fake itself lives in ``fake_confluence.py`` so it can be imported by name
from more than one test module without pytest and the importer giving the file
two module identities.

``confluence_transport`` replaces only the session the real 5.x client would
have opened. The client class, its constructor arguments, its URL building and
its authentication all still run, so a test failure here means the plugin would
really have failed against Confluence.
"""

import pytest
from atlassian import ConfluenceServer, ConfluenceV2

from dispatch.plugins.dispatch_atlassian_confluence.client import CloudApi, ServerApi
from tests.plugins.dispatch_atlassian_confluence.fake_confluence import (
    FakeConfluence,
    build_configuration,
    build_document_configuration,
    session_for,
)


@pytest.fixture
def confluence(monkeypatch):
    """Serve Confluence from memory for the duration of a test."""
    api = FakeConfluence()

    def transport(client_class):
        def factory(**kwargs):
            return client_class(session=session_for(api), **kwargs)

        return staticmethod(factory)

    monkeypatch.setattr(CloudApi, "client_class", transport(ConfluenceV2))
    monkeypatch.setattr(ServerApi, "client_class", transport(ConfluenceServer))
    return api


@pytest.fixture(params=["cloud", "server"])
def hosting_type(request):
    """Runs a test once per deployment type."""
    return request.param


def storage_plugin(hosting_type, **overrides):
    """A real ConfluencePagePlugin holding a real configuration."""
    from dispatch.plugins.dispatch_atlassian_confluence.plugin import ConfluencePagePlugin

    instance = ConfluencePagePlugin()
    instance.configuration = build_configuration(hosting_type, **overrides)
    return instance


def document_plugin(hosting_type, **overrides):
    """A real ConfluencePageDocPlugin holding a real configuration."""
    from dispatch.plugins.dispatch_atlassian_confluence.docs.plugin import (
        ConfluencePageDocPlugin,
    )

    instance = ConfluencePageDocPlugin()
    instance.configuration = build_document_configuration(hosting_type, **overrides)
    return instance

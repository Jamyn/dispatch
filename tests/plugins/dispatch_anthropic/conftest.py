"""Fixtures for the Anthropic plugin tests (issue #79).

The fake itself lives in ``fake_anthropic.py``, not here: ``tests/ai/conftest.py``
needs it too, and importing one package's ``conftest`` as a library gives the
file two module identities -- pytest imports it once under its own name and the
importer gets a second copy.
"""

import httpx
import pytest
from anthropic import Anthropic

from tests.plugins.dispatch_anthropic.fake_anthropic import FakeAnthropicAPI, build_configuration


@pytest.fixture
def anthropic_api(monkeypatch):
    """Serve the Anthropic API from memory for the duration of a test."""
    api = FakeAnthropicAPI()

    def client_factory(**kwargs):
        # max_retries=0 only so a failure test issues one request rather than
        # three with backoff; it does not change what is sent.
        return Anthropic(
            http_client=httpx.Client(transport=httpx.MockTransport(api)),
            max_retries=0,
            **kwargs,
        )

    monkeypatch.setattr(
        "dispatch.plugins.dispatch_anthropic.plugin.Anthropic", client_factory, raising=True
    )
    return api


@pytest.fixture
def anthropic_plugin(anthropic_api):
    """A real AnthropicPlugin holding a real AnthropicConfiguration.

    Named ``anthropic_plugin``, not ``plugin``: ``tests/conftest.py`` already
    defines ``plugin`` as a database ``Plugin`` row.
    """
    from dispatch.plugins.dispatch_anthropic.plugin import AnthropicPlugin

    instance = AnthropicPlugin()
    instance.configuration = build_configuration()
    return instance

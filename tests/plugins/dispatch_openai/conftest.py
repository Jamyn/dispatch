"""Fixtures for the OpenAI plugin tests (issue #75).

The fake itself lives in ``fake_openai.py``, not here: ``tests/ai/conftest.py``
needs it too, and importing one package's ``conftest`` as a library gives the
file two module identities -- pytest imports it once under its own name and the
importer gets a second copy.
"""

import httpx2
import pytest
from openai import OpenAI

from tests.plugins.dispatch_openai.fake_openai import FakeOpenAIAPI, build_configuration


@pytest.fixture
def openai_api(monkeypatch):
    """Serve the OpenAI API from memory for the duration of a test."""
    api = FakeOpenAIAPI()

    def client_factory(**kwargs):
        # max_retries=0 only so a failure test issues one request rather than
        # three with backoff; it does not change what is sent.
        return OpenAI(
            http_client=httpx2.Client(transport=httpx2.MockTransport(api)),
            max_retries=0,
            **kwargs,
        )

    monkeypatch.setattr(
        "dispatch.plugins.dispatch_openai.plugin.OpenAI", client_factory, raising=True
    )
    return api


@pytest.fixture
def openai_plugin(openai_api):
    """A real OpenAIPlugin holding a real OpenAIConfiguration.

    Named ``openai_plugin``, not ``plugin``: ``tests/conftest.py`` already
    defines ``plugin`` as a database ``Plugin`` row.
    """
    from dispatch.plugins.dispatch_openai.plugin import OpenAIPlugin

    instance = OpenAIPlugin()
    instance.configuration = build_configuration()
    return instance

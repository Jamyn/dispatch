"""Drive the OpenAI plugin against the real OpenAI API (issue #75).

Everything else in this directory asserts what the plugin *sends*, against a
fake that answers whatever it is handed. Only OpenAI can say whether it accepts
that request -- whether the model name resolves, whether the Authorization
header is well formed, and above all whether a `response_format` built from a
pydantic model really comes back as structured output. A fake cannot notice a
schema OpenAI would reject, which is the same blind spot that let issue #75 sit
for three releases.

Skipped unless credentials are configured, so it is inert locally and in CI by
default.

Configuration
-------------
``DISPATCH_OPENAI_TEST_API_KEY``  An OpenAI API key. Required.
``DISPATCH_OPENAI_TEST_MODEL``    Model to use. Optional, defaults to
                                  ``gpt-4o-mini`` -- the cheapest model that
                                  supports structured output.

Note this is billed, and that exporting the key in a shell -- or putting it in
``docker/.env``, which the documented workflow sources before every pytest run
-- opts *every* subsequent run in, with no further prompt. In CI it would come
from a repository secret the way ``DISPATCH_SLACK_TEST_*`` does; no such secret
is configured today, so this suite never runs on a runner.

Cost
----
Two requests, both capped by tiny prompts and short answers: a handful of tokens
each. Nothing is created in the account and there is nothing to clean up.

Credential handling
-------------------
The key is read from the environment straight into a ``SecretStr`` and is never
asserted on, printed or logged here. It is *not* true that OpenAI never echoes
it: the 401 body quotes the submitted key back, which is why
``OpenAIPlugin`` renders only the structured error fields. A failure in this
file therefore reports a plugin exception, not that body. ``--tb=native``
(pyproject) also keeps locals out of the traceback.
"""

import os

import pytest

API_KEY = os.environ.get("DISPATCH_OPENAI_TEST_API_KEY")
MODEL = os.environ.get("DISPATCH_OPENAI_TEST_MODEL", "gpt-4o-mini")

pytestmark = pytest.mark.skipif(
    not API_KEY,
    reason="live OpenAI credentials not configured (set DISPATCH_OPENAI_TEST_API_KEY)",
)


@pytest.fixture
def live_plugin():
    from dispatch.plugins.dispatch_openai.config import OpenAIConfiguration
    from dispatch.plugins.dispatch_openai.plugin import OpenAIPlugin

    plugin = OpenAIPlugin()
    plugin.configuration = OpenAIConfiguration(
        api_key=API_KEY,
        model=MODEL,
        system_message="Answer with a single lowercase word and nothing else.",
    )
    return plugin


def test_chat_completion_against_openai(live_plugin):
    """An ordinary completion, using the plugin's configured system message."""
    answer = live_plugin.chat_completion(prompt="What colour is a clear midday sky?")

    assert isinstance(answer, str)
    assert answer.strip()


def test_chat_parse_against_openai(live_plugin):
    """Structured output, with the caller's system message overriding the config.

    Uses ``TagRecommendations`` -- a real product model, not a toy -- because
    that is the only thing no mocked test can vouch for. The SDK reproduces
    pydantic constructs into the strict json_schema verbatim, and this model is
    the one carrying both risky kinds: ``default`` on its field, and
    ``exclusiveMinimum``/``exclusiveMaximum`` from the ``PrimaryKey`` annotation
    inside ``TagTypeRecommendation``. Whether OpenAI's strict validator accepts
    those cannot be settled offline.

    An empty recommendation list is a valid answer; the assertion is that the
    request was accepted and parsed, not that the model recommended anything.
    """
    from dispatch.ai.models import TagRecommendations

    result = live_plugin.chat_parse(
        prompt="No tags are available. Return an empty list of recommendations.",
        response_model=TagRecommendations,
        system_message="Answer only in the requested structure.",
    )

    assert isinstance(result, TagRecommendations)

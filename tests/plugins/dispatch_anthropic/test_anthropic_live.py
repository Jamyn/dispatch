"""Drive the Anthropic plugin against the real Anthropic API (issue #79).

Everything else in this directory asserts what the plugin *sends*, against a
fake that answers whatever it is handed. Only Anthropic can say whether it
accepts that request -- whether the model name resolves, whether the
``x-api-key`` header is well formed, and above all whether an
``output_config.format`` schema built from a pydantic model really comes back as
structured output. A fake cannot notice a schema Anthropic would reject, which
is the same blind spot that let issue #75 sit for three releases.

Skipped unless credentials are configured, so it is inert locally and in CI by
default.

Configuration
-------------
``DISPATCH_ANTHROPIC_TEST_API_KEY``  An Anthropic API key. Required.
``DISPATCH_ANTHROPIC_TEST_MODEL``    Model to use. Optional, defaults to
                                     ``claude-haiku-4-5`` -- the cheapest model
                                     that supports structured output. This is
                                     deliberately *not* the plugin's own default
                                     (``claude-opus-5``), so an accidental run
                                     is cheap.

Note this is billed, and that exporting the key in a shell -- or putting it in
``docker/.env``, which the documented workflow sources before every pytest run
-- opts *every* subsequent run in, with no further prompt. In CI it would come
from a repository secret the way ``DISPATCH_SLACK_TEST_*`` does; no such secret
is configured today, so this suite never runs on a runner.

Cost
----
Two requests, one each for the two methods. Both prompts are a single synthetic
sentence, and ``max_tokens`` is pinned to 256 here rather than the plugin's
default so a runaway answer cannot become a runaway bill. Nothing is created in
the account and there is nothing to clean up. No incident, case, signal or
conversation data is sent -- both prompts are invented for this file.

Credential handling
-------------------
The key is read from the environment straight into a ``SecretStr`` and is never
asserted on, printed or logged here. It is *not* true that Anthropic never
echoes it: the 401 body quotes the submitted key back, which is why
``AnthropicPlugin`` renders only the structured error fields. A failure in this
file therefore reports a plugin exception, not that body. ``--tb=native``
(pyproject) also keeps locals out of the traceback.
"""

import os

import pytest

API_KEY = os.environ.get("DISPATCH_ANTHROPIC_TEST_API_KEY")
MODEL = os.environ.get("DISPATCH_ANTHROPIC_TEST_MODEL", "claude-haiku-4-5")

# Small enough that a misbehaving model cannot run up a bill, large enough for a
# one-word answer and a small structured object.
MAX_TOKENS = 256

pytestmark = pytest.mark.skipif(
    not API_KEY,
    reason="live Anthropic credentials not configured (set DISPATCH_ANTHROPIC_TEST_API_KEY)",
)


@pytest.fixture
def live_plugin():
    from dispatch.plugins.dispatch_anthropic.config import AnthropicConfiguration
    from dispatch.plugins.dispatch_anthropic.plugin import AnthropicPlugin

    plugin = AnthropicPlugin()
    plugin.configuration = AnthropicConfiguration(
        api_key=API_KEY,
        model=MODEL,
        system_message="Answer with a single lowercase word and nothing else.",
        max_tokens=MAX_TOKENS,
    )
    return plugin


def test_chat_completion_against_anthropic(live_plugin):
    """An ordinary completion, using the plugin's configured system message.

    Also the only check that the response really is a list of content blocks the
    plugin flattens correctly -- including any thinking block a model may emit
    by default, which is invented by the fake and observed only here.
    """
    answer = live_plugin.chat_completion(prompt="What colour is a clear midday sky?")

    assert isinstance(answer, str)
    assert answer.strip()


def test_chat_parse_against_anthropic(live_plugin):
    """Structured output, with the caller's system message overriding the config.

    Uses ``TagRecommendations`` -- a real product model, not a toy -- because
    that is the only thing no mocked test can vouch for. It is the one service
    model with nested ``$defs``/``$ref``.

    What this does *not* test, contrary to the obvious guess: numeric and length
    constraints. The SDK rewrites the schema before sending it, folding
    ``minimum``/``maxLength`` and friends into the field's ``description``, so
    Anthropic never sees them and cannot reject them. That is settled offline in
    ``test_anthropic_plugin.py``. What is genuinely unsettleable offline is
    whether Anthropic accepts the *shape* the SDK produces -- in particular that
    every field of every service model has a default, so the schema carries no
    ``required`` array at all.

    An empty recommendation list is a valid answer; the assertion is that the
    request was accepted and parsed back into the exact model, not that the
    model recommended anything.
    """
    from dispatch.ai.models import TagRecommendations

    result = live_plugin.chat_parse(
        prompt="No tags are available. Return an empty list of recommendations.",
        response_model=TagRecommendations,
        system_message="Answer only in the requested structure.",
    )

    assert type(result) is TagRecommendations

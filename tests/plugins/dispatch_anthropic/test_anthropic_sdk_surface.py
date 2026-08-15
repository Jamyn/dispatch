"""Pins the anthropic SDK surface AnthropicPlugin is written against.

``messages.parse`` -- the native structured-output path ``plugin.py`` calls --
is what these tests pin. It is a comparatively recent addition to the SDK, and
the rest of this directory drives the plugin against a fake transport, which
answers whatever the SDK asks of it. A downgrade to a version without `parse`
would therefore surface there as a missing attribute deep inside a fixture
rather than as the dependency problem it is. That is what these tests are for.

The alternative to `parse` would be hand-rolled tool-use with a schema derived
from the pydantic model, or a "please return JSON" instruction. Both are weaker,
so a version floor that keeps `parse` available is part of the design.
"""

import inspect

from anthropic.resources.messages import Messages


def test_messages_exposes_parse():
    assert hasattr(Messages, "parse")


def test_parse_accepts_the_arguments_chat_parse_sends():
    params = inspect.signature(Messages.parse).parameters
    for name in ("model", "messages", "system", "max_tokens", "output_format"):
        assert name in params, name


def test_create_accepts_the_arguments_chat_completion_sends():
    params = inspect.signature(Messages.create).parameters
    for name in ("model", "messages", "system", "max_tokens"):
        assert name in params, name


def test_max_tokens_is_required():
    """It has no default, which is why the configuration must carry one. If the
    SDK ever gives it one, the configuration field stops being load-bearing."""
    assert (
        inspect.signature(Messages.create).parameters["max_tokens"].default
        is inspect.Parameter.empty
    )


def test_the_error_hierarchy_the_plugin_catches_is_the_root_one():
    """``chat_completion``/``chat_parse`` catch ``AnthropicError``. If the API
    error classes ever stopped descending from it, every API failure would
    escape the plugin's redaction and reach the case channel with the response
    body -- and the API key -- intact."""
    from anthropic import (
        AnthropicError,
        APIConnectionError,
        APIStatusError,
        AuthenticationError,
        RateLimitError,
    )

    for cls in (APIStatusError, APIConnectionError, AuthenticationError, RateLimitError):
        assert issubclass(cls, AnthropicError), cls.__name__

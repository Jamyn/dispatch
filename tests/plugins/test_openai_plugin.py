"""Pins the openai SDK surface OpenAIPlugin is written against.

openai 1.x exposed structured-output parsing at `beta.chat.completions.parse`;
2.x promoted it to `chat.completions.parse`, which is the path plugin.py calls.
Downgrading openai below 2.x therefore breaks that call, and no other test can
see it because every AI test mocks the plugin instance.

This guards the SDK contract only. `chat_parse` is separately unreachable --
its call sites in dispatch.ai.service pass a `system_message` kwarg it does not
accept and read a `chat_completion_model` config field that does not exist.
"""

import inspect

from openai.resources.chat.completions import Completions


def test_chat_completions_exposes_parse():
    assert hasattr(Completions, "parse")


def test_parse_accepts_the_arguments_chat_parse_sends():
    params = inspect.signature(Completions.parse).parameters
    for name in ("model", "messages", "response_format"):
        assert name in params

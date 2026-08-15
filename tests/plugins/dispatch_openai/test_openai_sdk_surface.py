"""Pins the openai SDK surface OpenAIPlugin is written against.

`chat.completions.parse` -- the structured-output path `plugin.py` calls -- is
what these two tests pin. Early openai 1.x exposed parsing only under
`beta.chat.completions`; `beta.chat` is still an alias to the same `Chat` class
in 2.53.0, so the risk is not that the alias disappears but that a downgrade far
enough back leaves `parse` absent from the non-beta path entirely.

The rest of this directory drives the plugin against a fake transport, which
answers whatever the SDK asks of it -- so such a downgrade would show up there
as a missing attribute deep inside a fixture rather than as the dependency
problem it is. That is what these two tests are for.
"""

import inspect

from openai.resources.chat.completions import Completions


def test_chat_completions_exposes_parse():
    assert hasattr(Completions, "parse")


def test_parse_accepts_the_arguments_chat_parse_sends():
    params = inspect.signature(Completions.parse).parameters
    for name in ("model", "messages", "response_format"):
        assert name in params

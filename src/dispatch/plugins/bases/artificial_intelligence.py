"""
.. module: dispatch.plugins.bases.artificial_intelligence
    :platform: Unix
    :copyright: (c) 2019 by Netflix Inc., see AUTHORS for more
    :license: Apache, see LICENSE for more details.
.. moduleauthor:: Marc Vilanova <mvilanova@netflix.com>
"""

# `Type[T]`, not `type[T]`: `type` is a class attribute here, and Python 3.14
# evaluates annotations lazily in the class namespace -- where `type[T]` indexes
# the string "artificial-intelligence" and raises.
from typing import Type, TypeVar

from pydantic import BaseModel

from dispatch.plugins.base import Plugin

T = TypeVar("T", bound=BaseModel)


class ArtificialIntelligencePlugin(Plugin):
    """The interface `dispatch.ai.service` is written against.

    The signatures are part of the contract, not documentation: these were
    `(self, items, **kwargs)` while the service called them by keyword, so an
    implementation could drift out of step with its only caller and nothing --
    not the base class, not a type checker, not the test suite -- would say so
    (issue #75).

    A plugin's `configuration` must also expose a `model` field naming the model
    it will send requests to, because the service sizes prompts against it.
    """

    type = "artificial-intelligence"

    def chat_completion(self, prompt: str, system_message: str | None = None) -> str:
        """Free-text completion. `system_message` falls back to the plugin's own."""
        raise NotImplementedError

    def chat_parse(
        self, prompt: str, response_model: Type[T], system_message: str | None = None
    ) -> T:
        """Structured completion, returning an instance of `response_model`."""
        raise NotImplementedError

    def list_models(self) -> list[str]:
        """The models this plugin can be configured with. Nothing calls this yet."""
        raise NotImplementedError

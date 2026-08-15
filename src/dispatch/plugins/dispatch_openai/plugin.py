"""
.. module: dispatch.plugins.openai.plugin
    :platform: Unix
    :copyright: (c) 2019 by Netflix Inc., see AUTHORS for more
    :license: Apache, see LICENSE for more details.
.. moduleauthor:: Marc Vilanova <mvilanova@netflix.com>
"""

import logging
from typing import Type, TypeVar

from openai import APIStatusError, OpenAI, OpenAIError
from pydantic import BaseModel

from dispatch.decorators import apply, counter, timer
from dispatch.exceptions import DispatchPluginException
from dispatch.plugins import dispatch_openai as openai_plugin
from dispatch.plugins.bases import ArtificialIntelligencePlugin
from dispatch.plugins.dispatch_openai.config import (
    OpenAIConfiguration,
)

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def build_messages(
    configuration: OpenAIConfiguration, prompt: str, system_message: str | None
) -> list[dict]:
    """The two-message chat the plugin sends.

    The caller's system message wins when it supplies one; otherwise the
    configured default applies, which is the behaviour every call site had
    before the ai service began passing prompts of its own.
    """
    return [
        {"role": "system", "content": system_message or configuration.system_message},
        {"role": "user", "content": prompt},
    ]


def api_error(e: OpenAIError) -> DispatchPluginException:
    """A safe rendering of an openai SDK error.

    `str(e)` on an `APIStatusError` is OpenAI's response body verbatim, and the
    401 body quotes the submitted API key back. `dispatch.ai.service`
    interpolates the exception into `error_message`, which is posted into the
    case channel and returned to the browser -- so only the structured fields
    are reproduced here. They are enum-like and carry no free text.
    """
    if isinstance(e, APIStatusError):
        detail = f"HTTP {e.status_code}"
        if e.code:
            detail += f", {e.code}"
        if e.request_id:
            detail += f", request id {e.request_id}"
    else:
        detail = type(e).__name__

    return DispatchPluginException(f"The OpenAI request failed ({detail}).")


def refusal_suffix(refusal: str | None) -> str:
    """The model's stated reason for declining, when it gave one."""
    return f": {refusal}" if refusal else ""


@apply(counter, exclude=["__init__", "_client", "_failed"])
@apply(timer, exclude=["__init__", "_client", "_failed"])
class OpenAIPlugin(ArtificialIntelligencePlugin):
    title = "OpenAI Plugin - Generative Artificial Intelligence"
    slug = "openai-artificial-intelligence"
    description = "Uses OpenAI's platform to allow users to ask questions in natural language."
    version = openai_plugin.__version__

    author = "Netflix"
    author_url = "https://github.com/netflix/dispatch.git"

    def __init__(self):
        self.configuration_schema = OpenAIConfiguration

    def _failed(self, e: OpenAIError, api_key: str) -> DispatchPluginException:
        """Log what OpenAI said, then hand back something safe to show.

        The body is where the useful detail lives -- a rejected response_format
        names the offending schema keyword -- so it is logged rather than
        dropped, with the one thing it may quote back removed. `from None` at
        the raise site keeps the original out of the caller's `log.exception`
        and out of Sentry, which honours `__suppress_context__`.
        """
        log.warning("OpenAI request failed: %s", str(e).replace(api_key, "***"))
        return api_error(e)

    def _client(self) -> tuple[OpenAI, OpenAIConfiguration]:
        # `PluginInstance.configuration` returns None for an instance that was
        # never configured, or whose stored JSON no longer satisfies the schema
        # -- it logs a warning and nothing else. Reaching through that None
        # gives "'NoneType' object has no attribute 'model'", which is what
        # reaches the incident timeline.
        configuration = self.configuration
        if configuration is None:
            raise DispatchPluginException(
                "The OpenAI plugin configuration could not be read. Enter an API key and "
                "model under Settings > Project > Plugins."
            )

        api_key = configuration.api_key.get_secret_value()
        if not api_key:
            # `OpenAI(api_key="")` raises OpenAIError from the constructor, which
            # escapes every caller's try block. The schema accepts an empty
            # SecretStr, so this is reachable from the plugin settings form.
            raise DispatchPluginException(
                "The OpenAI plugin has no API key. Enter one under Settings > Project > Plugins."
            )

        return OpenAI(api_key=api_key), configuration

    def chat_completion(self, prompt: str, system_message: str | None = None) -> str:
        client, configuration = self._client()

        try:
            completion = client.chat.completions.create(
                model=configuration.model,
                messages=build_messages(configuration, prompt, system_message),
            )
        except OpenAIError as e:
            raise self._failed(e, configuration.api_key.get_secret_value()) from None

        choice = completion.choices[0]

        # `create` reports a truncated or filtered reply in finish_reason and
        # returns the partial text; only `parse` raises for it. Returning it
        # would commit half a sentence to Incident.summary and log
        # "AI-generated incident summary created" over the top.
        if choice.finish_reason != "stop":
            raise DispatchPluginException(
                f"OpenAI did not complete the chat completion ({choice.finish_reason})."
            )

        # Empty content is as unusable as none: an empty summary is stored and
        # then reads as "no summary was generated".
        if not choice.message.content:
            raise DispatchPluginException(
                "OpenAI returned no content for the chat completion"
                f"{refusal_suffix(choice.message.refusal)}."
            )

        return choice.message.content

    def chat_parse(
        # `Type[T]`, not `type[T]`, to stay identical to the base class -- see
        # the comment on its own import, and the signature-parity test.
        self,
        prompt: str,
        response_model: Type[T],
        system_message: str | None = None,
    ) -> T:
        client, configuration = self._client()

        try:
            completion = client.chat.completions.parse(
                model=configuration.model,
                response_format=response_model,
                messages=build_messages(configuration, prompt, system_message),
            )
        except OpenAIError as e:
            raise self._failed(e, configuration.api_key.get_secret_value()) from None

        choice = completion.choices[0]
        if choice.message.parsed is None:
            # None means the model refused rather than answering in the schema.
            # Every caller reads a field off the result, so returning it would
            # surface as an AttributeError two frames away. A truncated or
            # filtered reply never gets here -- the SDK raises for those.
            raise DispatchPluginException(
                f"OpenAI returned no {response_model.__name__}"
                f"{refusal_suffix(choice.message.refusal)}."
            )

        return choice.message.parsed

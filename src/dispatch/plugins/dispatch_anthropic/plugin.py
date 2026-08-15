"""The Anthropic (Claude) artificial-intelligence plugin.

Selecting this plugin does not stop Dispatch talking to OpenAI. Before every
call `dispatch.ai.service.prepare_prompt_for_model` sizes the prompt with
tiktoken, which has no encoding for any Claude model and falls back to
`o200k_base` -- an encoding it downloads from an OpenAI-hosted blob and caches
under `/tmp`, i.e. re-downloads after every container restart. An operator who
adopts this plugin *in order to* firewall OpenAI gets an unhandled failure on
three of the five GenAI endpoints, because that call sits outside their `try`.
Set `TIKTOKEN_CACHE_DIR` to a persistent path, or keep that egress open.

The consequence for prompt budgets is deliberate and is left alone: an unmapped
model falls to a 128k default, so Claude prompts are truncated conservatively
rather than overflowing. Fixing that properly needs provider-aware token
counting, which is a change to the ai service and not to this plugin.
"""

import logging
from typing import Type, TypeVar, get_args

from anthropic import Anthropic, AnthropicError, APIStatusError
from anthropic.types import Message
from anthropic.types.refusal_stop_details import RefusalStopDetails
from anthropic.types.shared.error_type import ErrorType
from anthropic.types.stop_reason import StopReason
from pydantic import BaseModel, ValidationError

from dispatch.decorators import apply, counter, timer
from dispatch.exceptions import DispatchPluginException
from dispatch.plugins import dispatch_anthropic as anthropic_plugin
from dispatch.plugins.bases import ArtificialIntelligencePlugin
from dispatch.plugins.dispatch_anthropic.config import AnthropicConfiguration

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Everything Anthropic documents for the three response fields this plugin
# reproduces in an error message.
#
# None of them is validated on the way in. The SDK *constructs* its response
# models rather than validating them, and `APIStatusError.type` is a bare `cast`
# out of the response body -- so each of these is free text chosen by whatever
# answered the request, and `ANTHROPIC_BASE_URL` means that need not be
# Anthropic. `dispatch.ai.service` renders these messages into a Slack channel
# as mrkdwn, where `<!channel>` is a live notification, so a value is reproduced
# only when it is one Anthropic actually documents. `safe` does the check;
# nothing should interpolate these fields directly.
ERROR_TYPES = frozenset(get_args(ErrorType))
STOP_REASONS = frozenset(get_args(StopReason))
REFUSAL_CATEGORIES = frozenset(
    get_args(get_args(RefusalStopDetails.model_fields["category"].annotation)[0])
)


def safe(value: str | None, documented: frozenset[str]) -> str | None:
    """`value` if Anthropic documents it, otherwise None."""
    return value if value in documented else None


def build_request(
    configuration: AnthropicConfiguration, prompt: str, system_message: str | None
) -> dict:
    """The request every call sends, minus the model-specific parts.

    Anthropic takes the system prompt as its own top-level `system` field rather
    than as a leading message, so the caller's system message and the prompt
    never share a list. The caller's wins when it supplies one; otherwise the
    configured default applies, which is the fallback the base class documents.

    `thinking` is deliberately absent. Each model's default applies -- on the
    models where thinking is on by default the reasoning arrives in `thinking`
    blocks, which `extract_text` skips and `max_tokens` has to cover. Sending
    the parameter would be the compatibility risk, not omitting it: its accepted
    values differ by model, and one of them is a 400 on Fable 5.
    """
    return {
        "model": configuration.model,
        "max_tokens": configuration.max_tokens,
        "system": system_message or configuration.system_message,
        "messages": [{"role": "user", "content": prompt}],
    }


def unbuildable_request(e: Exception) -> DispatchPluginException:
    """A request the SDK refused to build, rendered safely.

    Two of these are reachable from the plugin settings form or from a caller's
    response model, and neither is an `AnthropicError`, so neither would be
    caught alongside the API failures:

    * `ValueError` from the SDK's non-streaming guard, when `max_tokens` implies
      more than ten minutes of generation. `AnthropicConfiguration` bounds the
      field, but the legacy `claude-opus-4-0`/`4-1` carry a lower cap of their
      own, so a valid configuration can still trip it on those models.
    * `ValueError`/`TypeError` from schema generation, for a `response_model`
      carrying a field the SDK cannot express as a JSON schema.

    Only the type name is reproduced. These messages are the SDK's own and carry
    no response body, but `dispatch.ai.service` interpolates whatever it is given
    into `error_message`, so the same rule applies as to `api_error`.
    """
    return DispatchPluginException(
        f"The Anthropic request could not be built ({type(e).__name__}). Check the plugin's "
        "configured maximum output tokens."
    )


def api_error(e: AnthropicError) -> DispatchPluginException:
    """A safe rendering of an anthropic SDK error.

    `str(e)` on an `APIStatusError` is Anthropic's response body verbatim, and
    the 401 body quotes the submitted API key back (verified against anthropic
    0.122.0). `dispatch.ai.service` interpolates the exception into
    `error_message`, which is posted into the case channel and returned to the
    browser -- so only the structured fields are reproduced here, and `.type`
    only when Anthropic documents it (see `safe`). `.status_code` is an int and
    `.request_id` is a header the SDK reads verbatim; neither can carry markup.
    """
    if isinstance(e, APIStatusError):
        detail = f"HTTP {e.status_code}"
        if error_type := safe(e.type, ERROR_TYPES):
            detail += f", {error_type}"
        if e.request_id:
            detail += f", request id {e.request_id}"
    else:
        detail = type(e).__name__

    return DispatchPluginException(f"The Anthropic request failed ({detail}).")


def stop_reason(message: Message) -> str:
    """Why the model stopped, as a value safe to render."""
    return safe(message.stop_reason, STOP_REASONS) or "unknown"


def refusal_suffix(message: Message) -> str:
    """The model's stated reason for declining, when it gave one.

    `stop_details` is populated only for `stop_reason == "refusal"` and is null
    for every other stop reason, so it is read through `getattr` and checked.
    Only the category -- a documented policy label -- is reproduced; the
    accompanying `explanation` is free text and is left to the log.
    """
    details = getattr(message, "stop_details", None)
    category = safe(getattr(details, "category", None), REFUSAL_CATEGORIES) if details else None
    return f": {category}" if category else ""


def extract_text(message: Message) -> str:
    """The assistant's text, and only its text.

    A response is a list of content blocks, not a single string. Blocks that are
    not text -- `thinking` above all, which is present by default on the newer
    models -- carry no answer and are skipped rather than coerced. Several text
    blocks are one reply split at a citation boundary, so they concatenate.
    """
    return "".join(block.text for block in message.content if block.type == "text")


@apply(counter, exclude=["__init__", "_client", "_failed"])
@apply(timer, exclude=["__init__", "_client", "_failed"])
class AnthropicPlugin(ArtificialIntelligencePlugin):
    title = "Anthropic Plugin - Generative Artificial Intelligence"
    slug = "anthropic-artificial-intelligence"
    description = (
        "Uses Anthropic's Claude models to allow users to ask questions in natural language."
    )
    version = anthropic_plugin.__version__

    # Not "Netflix": this plugin postdates the archived upstream, and `author`
    # is per-plugin attribution shown in the plugin table, not boilerplate.
    author = "Dispatch"
    author_url = "https://github.com/Jamyn/dispatch"

    def __init__(self):
        self.configuration_schema = AnthropicConfiguration

    def _failed(self, e: AnthropicError, api_key: str) -> DispatchPluginException:
        """Log what Anthropic said, then hand back something safe to show.

        The body is where the useful detail lives -- a rejected schema names the
        offending keyword -- so it is logged rather than dropped, with the one
        thing it may quote back removed. `from None` at the raise site keeps the
        original out of the caller's `log.exception` and out of Sentry, which
        honours `__suppress_context__`.
        """
        log.warning("Anthropic request failed: %s", str(e).replace(api_key, "***"))
        return api_error(e)

    def _client(self) -> tuple[Anthropic, AnthropicConfiguration]:
        # `PluginInstance.configuration` returns None for an instance that was
        # never configured, or whose stored JSON no longer satisfies the schema
        # -- it logs a warning and nothing else. Reaching through that None
        # gives "'NoneType' object has no attribute 'model'", which is what
        # reaches the incident timeline.
        configuration = self.configuration
        if configuration is None:
            raise DispatchPluginException(
                "The Anthropic plugin configuration could not be read. Enter an API key and "
                "model under Settings > Project > Plugins."
            )

        api_key = configuration.api_key.get_secret_value()
        if not api_key:
            # `Anthropic(api_key="")` constructs happily -- unlike OpenAI's,
            # which raises from the constructor -- and then raises a bare
            # `TypeError` ("Could not resolve authentication method") when the
            # request is issued. That is not an `AnthropicError`, so it would
            # escape the handler below rather than surface as a configuration
            # mistake. The schema accepts an empty SecretStr, so this is
            # reachable from the plugin settings form.
            raise DispatchPluginException(
                "The Anthropic plugin has no API key. Enter one under Settings > Project > Plugins."
            )

        # Retries are left at the SDK's own default. `dispatch.ai.service` has no
        # retry layer of its own, and adding a second one here would multiply
        # against the SDK's backoff rather than replace it.
        return Anthropic(api_key=api_key), configuration

    def chat_completion(self, prompt: str, system_message: str | None = None) -> str:
        client, configuration = self._client()

        try:
            message = client.messages.create(**build_request(configuration, prompt, system_message))
        except AnthropicError as e:
            raise self._failed(e, configuration.api_key.get_secret_value()) from None
        except (ValueError, TypeError) as e:
            # Raised before any request goes out -- see `unbuildable_request`.
            log.warning("Anthropic request could not be built: %s", e)
            raise unbuildable_request(e) from None

        # A reply cut off at `max_tokens`, or declined outright, still arrives as
        # a 200 with whatever text was produced. Returning it would commit half a
        # sentence to Incident.summary and log "AI-generated incident summary
        # created" over the top.
        if message.stop_reason != "end_turn":
            raise DispatchPluginException(
                f"Anthropic did not complete the chat completion ({stop_reason(message)})"
                f"{refusal_suffix(message)}."
            )

        text = extract_text(message)

        # Empty text is as unusable as none: an empty summary is stored and then
        # reads as "no summary was generated". This is also what a reply carrying
        # only non-text blocks collapses to.
        if not text:
            raise DispatchPluginException("Anthropic returned no content for the chat completion.")

        return text

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
            # `messages.parse` is Anthropic's native structured-output path: it
            # derives a JSON schema from `response_model`, sends it as
            # `output_config.format`, and validates the reply back through the
            # same model. No tool is defined and the schema is never described to
            # the model in prose.
            message = client.messages.parse(
                output_format=response_model,
                **build_request(configuration, prompt, system_message),
            )
        except AnthropicError as e:
            raise self._failed(e, configuration.api_key.get_secret_value()) from None
        except ValidationError as e:
            # The reply did not satisfy the model it was validated against.
            # Three different causes land here and the message cannot tell them
            # apart, so it does not try to: the reply was cut mid-JSON
            # (truncation), the model answered with something else entirely, or
            # the reply satisfied the schema Anthropic enforced but not a
            # constraint only pydantic knows about -- the SDK moves `minimum`,
            # `maxLength` and friends into the field description rather than
            # sending them, so those are checked here and nowhere else.
            # `ValidationError` subclasses `ValueError`, so this clause must
            # stay above the one below.
            log.warning("Anthropic returned an unusable %s: %s", response_model.__name__, e)
            raise DispatchPluginException(
                f"Anthropic returned a {response_model.__name__} that did not match it. The "
                "reply may have been truncated, or may not satisfy a field constraint."
            ) from None
        except (ValueError, TypeError) as e:
            # Raised before any request goes out -- see `unbuildable_request`.
            # For `chat_parse` this also covers a `response_model` the SDK
            # cannot turn into a JSON schema.
            log.warning("Anthropic request could not be built: %s", e)
            raise unbuildable_request(e) from None

        # Checked before `parsed_output`, because a reply that was cut off or
        # declined carries no text block to parse and would otherwise be
        # reported as an empty answer rather than as the truncation or refusal
        # it is.
        if message.stop_reason != "end_turn":
            raise DispatchPluginException(
                f"Anthropic did not complete the {response_model.__name__} "
                f"({stop_reason(message)}){refusal_suffix(message)}."
            )

        if message.parsed_output is None:
            # A completed reply carrying no parseable text block at all. Every
            # caller reads a field off the result, so returning it would surface
            # as an AttributeError two frames away.
            raise DispatchPluginException(f"Anthropic returned no {response_model.__name__}.")

        return message.parsed_output

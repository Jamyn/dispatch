"""The Anthropic plugin, driven against an in-memory Anthropic API (issue #79).

Issue #79 exists because ``dispatch_openai`` had drifted out of step with its
only caller and nothing said so: the service read a configuration field the
schema did not declare and passed a keyword the plugin did not accept, while a
suite built on ``Mock`` answered both happily (issue #75).

So the tests here assert on the request that *reached the wire* rather than on
the arguments of an intercepted method call, and the contract-shaped ones --
that the configured model is sent, that ``system_message=`` is accepted and
forwarded -- are named for the defect they exist to catch.
"""

import inspect
from enum import Enum

import pytest
from pydantic import BaseModel, Field

from dispatch.exceptions import DispatchPluginException
from dispatch.plugins.bases import ArtificialIntelligencePlugin
from dispatch.plugins.dispatch_anthropic.plugin import AnthropicPlugin
from tests.plugins.dispatch_anthropic.fake_anthropic import (
    API_KEY,
    CONFIGURED_MAX_TOKENS,
    CONFIGURED_MODEL,
    CONFIGURED_SYSTEM_MESSAGE,
    build_configuration,
)

CALLER_SYSTEM_MESSAGE = "You are analyzing a security signal."


class Priority(str, Enum):
    low = "low"
    high = "high"


class Participant(BaseModel):
    name: str
    role: str | None = None


class Recommendation(BaseModel):
    """A flat model whose fields are all required.

    Deliberately *not* the shape the ai service's own models take -- every field
    of every model in `dispatch/ai/models.py` carries a default, so none of them
    can express "the model left this out". Required fields are what make the
    malformed-reply tests below able to fail at all; `Defaulted` covers the
    other shape.
    """

    summary: str
    confidence: int


class Defaulted(BaseModel):
    """The shape the ai service's models really take: no field is required."""

    summary: str = ""
    confidence: int = 0
    tags: list[str] = Field(default_factory=list)


class Bounded(BaseModel):
    """Carries a constraint the SDK strips out of the wire schema.

    `identifier` mirrors `dispatch.models.PrimaryKey` (`gt=0`), which reaches
    Anthropic through `TagRecommendations`.
    """

    summary: str
    confidence: int
    identifier: int = Field(gt=0)


class NestedRecommendation(BaseModel):
    """Nested models, a list of models, an enum, a bool and an optional --
    everything ``$defs``/``$ref`` schema generation has to survive."""

    title: str
    priority: Priority
    approved: bool
    participants: list[Participant]
    tags: list[str] = Field(default_factory=list)
    note: str | None = None


class TestContract:
    """The signatures and configuration fields ``dispatch.ai.service`` requires.

    These are the checks that would have failed on ``dispatch_openai`` before
    issue #75 was fixed, written against the base class so they cannot drift.
    """

    def test_is_an_artificial_intelligence_plugin(self):
        assert issubclass(AnthropicPlugin, ArtificialIntelligencePlugin)
        assert AnthropicPlugin.type == "artificial-intelligence"

    @pytest.mark.parametrize("method", ["chat_completion", "chat_parse"])
    def test_signature_matches_the_base_class(self, method):
        """Defect 2 was a plugin whose signature its caller could not satisfy.

        ``inspect.signature`` follows ``__wrapped__``, so the metrics decorators
        are transparent here. Rendered, not compared by identity: ``T`` is a
        distinct TypeVar object in each module, so ``Signature.__eq__`` fails on
        annotations that are the same type in every way that matters.
        """
        implemented = inspect.signature(getattr(AnthropicPlugin, method))
        declared = inspect.signature(getattr(ArtificialIntelligencePlugin, method))

        assert str(implemented) == str(declared)

    def test_configuration_declares_the_field_the_service_reads(self):
        """Defect 1: the service reads ``configuration.model``. A configuration
        that named it anything else raised AttributeError at five call sites."""
        assert "model" in build_configuration().model_fields

    def test_the_service_can_read_the_model_off_a_configured_plugin(self, anthropic_plugin):
        """The real accessor, not a restatement of the field name."""
        from dispatch.ai.service import get_genai_model
        from types import SimpleNamespace

        assert get_genai_model(SimpleNamespace(instance=anthropic_plugin)) == CONFIGURED_MODEL


class TestChatCompletion:
    def test_returns_the_reply(self, anthropic_plugin, anthropic_api):
        anthropic_api.respond_with_text("A short incident summary.")

        assert (
            anthropic_plugin.chat_completion(prompt="summarize this") == "A short incident summary."
        )

    def test_sends_the_configured_model(self, anthropic_plugin, anthropic_api):
        """Defect 1, at the wire: not the schema default, the stored value."""
        anthropic_plugin.chat_completion(prompt="summarize this")

        assert anthropic_api.request["body"]["model"] == CONFIGURED_MODEL

    def test_sends_the_configured_api_key(self, anthropic_plugin, anthropic_api):
        anthropic_plugin.chat_completion(prompt="summarize this")

        # Anthropic authenticates on x-api-key, not a bearer Authorization header.
        assert anthropic_api.request["api_key"] == API_KEY

    def test_sends_the_configured_max_tokens(self, anthropic_plugin, anthropic_api):
        """Anthropic rejects a request without max_tokens, so this is required,
        not optional -- and it must come from the configuration."""
        anthropic_plugin.chat_completion(prompt="summarize this")

        assert anthropic_api.request["body"]["max_tokens"] == CONFIGURED_MAX_TOKENS

    def test_sends_the_prompt_as_the_user_message(self, anthropic_plugin, anthropic_api):
        anthropic_plugin.chat_completion(prompt="summarize this")

        assert anthropic_api.request["body"]["messages"] == [
            {"role": "user", "content": "summarize this"}
        ]

    def test_accepts_a_system_message(self, anthropic_plugin, anthropic_api):
        """Defect 2: the service passes ``system_message=`` by keyword."""
        anthropic_plugin.chat_completion(
            prompt="summarize this", system_message=CALLER_SYSTEM_MESSAGE
        )

        # Anthropic takes the system prompt top-level, not as a leading message.
        assert anthropic_api.request["body"]["system"] == CALLER_SYSTEM_MESSAGE
        assert anthropic_api.request["body"]["messages"] == [
            {"role": "user", "content": "summarize this"}
        ]

    def test_falls_back_to_the_configured_system_message(self, anthropic_plugin, anthropic_api):
        anthropic_plugin.chat_completion(prompt="summarize this")

        assert anthropic_api.request["body"]["system"] == CONFIGURED_SYSTEM_MESSAGE

    def test_an_explicit_none_system_message_also_falls_back(self, anthropic_plugin, anthropic_api):
        anthropic_plugin.chat_completion(prompt="summarize this", system_message=None)

        assert anthropic_api.request["body"]["system"] == CONFIGURED_SYSTEM_MESSAGE

    def test_does_not_send_a_thinking_parameter(self, anthropic_plugin, anthropic_api):
        """Each model's own default applies. The parameter's accepted values
        differ by model and one of them is a 400 on Fable 5, so sending it is
        the compatibility risk, not omitting it."""
        anthropic_plugin.chat_completion(prompt="summarize this")

        assert "thinking" not in anthropic_api.request["body"]

    def test_joins_several_text_blocks(self, anthropic_plugin, anthropic_api):
        """Anthropic splits one reply at citation boundaries; the pieces are
        contiguous text, not separate answers."""
        anthropic_api.respond_with_blocks(
            [
                {"type": "text", "text": "The incident began "},
                {"type": "text", "text": "at 10:00."},
            ]
        )

        assert (
            anthropic_plugin.chat_completion(prompt="summarize this")
            == "The incident began at 10:00."
        )

    def test_skips_thinking_blocks(self, anthropic_plugin, anthropic_api):
        """Thinking blocks are present by default on the newer models. They are
        not the answer, and concatenating them into the summary would publish
        the model's reasoning into the incident record."""
        anthropic_api.respond_with_blocks(
            [
                {"type": "thinking", "thinking": "internal reasoning", "signature": "sig"},
                {"type": "text", "text": "The real summary."},
            ]
        )

        assert anthropic_plugin.chat_completion(prompt="summarize this") == "The real summary."

    def test_rejects_a_reply_with_no_content_blocks(self, anthropic_plugin, anthropic_api):
        anthropic_api.respond_with_blocks([])

        with pytest.raises(DispatchPluginException, match="no content"):
            anthropic_plugin.chat_completion(prompt="summarize this")

    def test_rejects_a_reply_with_only_non_text_blocks(self, anthropic_plugin, anthropic_api):
        """A thinking-only reply is empty as far as the caller is concerned;
        storing "" reads back as "no summary was generated"."""
        anthropic_api.respond_with_blocks(
            [{"type": "thinking", "thinking": "internal reasoning", "signature": "sig"}]
        )

        with pytest.raises(DispatchPluginException, match="no content"):
            anthropic_plugin.chat_completion(prompt="summarize this")

    def test_rejects_a_truncated_reply(self, anthropic_plugin, anthropic_api):
        """Anthropic returns the fragment with a 200; committing it would store
        half a sentence as the incident summary."""
        anthropic_api.respond_with_truncation("The incident began at 10:0")

        with pytest.raises(DispatchPluginException, match="max_tokens"):
            anthropic_plugin.chat_completion(prompt="summarize this")

    def test_rejects_a_refusal_and_names_the_category(self, anthropic_plugin, anthropic_api):
        anthropic_api.respond_with_refusal(category="cyber")

        with pytest.raises(DispatchPluginException, match="refusal.*cyber"):
            anthropic_plugin.chat_completion(prompt="summarize this")


class TestChatParse:
    def test_returns_an_instance_of_the_requested_model(self, anthropic_plugin, anthropic_api):
        anthropic_api.respond_with_object({"summary": "Likely benign.", "confidence": 80})

        result = anthropic_plugin.chat_parse(
            prompt="extract the data", response_model=Recommendation
        )

        # `type(...) is`, not isinstance: the caller was promised this class, not
        # a subclass and not a dict that happens to have the attributes.
        assert type(result) is Recommendation
        assert result == Recommendation(summary="Likely benign.", confidence=80)

    def test_sends_the_configured_model_and_key(self, anthropic_plugin, anthropic_api):
        """Defect 1, on the structured-output path."""
        anthropic_api.respond_with_object({"summary": "s", "confidence": 1})

        anthropic_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

        assert anthropic_api.request["body"]["model"] == CONFIGURED_MODEL
        assert anthropic_api.request["api_key"] == API_KEY
        assert anthropic_api.request["body"]["max_tokens"] == CONFIGURED_MAX_TOKENS
        # Same reasoning as the chat_completion twin: each model's own default.
        assert "thinking" not in anthropic_api.request["body"]

    def test_an_empty_system_message_falls_back_to_the_configured_one(
        self, anthropic_plugin, anthropic_api
    ):
        """`system_message or configuration.system_message` coalesces on
        falsiness, not on None, so "" takes the configured default rather than
        sending an empty system prompt. Deliberate -- Anthropic rejects an empty
        `system` string -- and pinned because the code does not say so."""
        anthropic_api.respond_with_object({"summary": "s", "confidence": 1})

        anthropic_plugin.chat_parse(
            prompt="extract the data", response_model=Recommendation, system_message=""
        )

        assert anthropic_api.request["body"]["system"] == CONFIGURED_SYSTEM_MESSAGE

    def test_accepts_a_system_message(self, anthropic_plugin, anthropic_api):
        """Defect 2, on the structured-output path -- the keyword form all four
        of the ai service's ``chat_parse`` call sites use."""
        anthropic_api.respond_with_object({"summary": "s", "confidence": 1})

        anthropic_plugin.chat_parse(
            prompt="extract the data",
            response_model=Recommendation,
            system_message=CALLER_SYSTEM_MESSAGE,
        )

        assert anthropic_api.request["body"]["system"] == CALLER_SYSTEM_MESSAGE

    def test_sends_the_schema_natively_not_in_the_prompt(self, anthropic_plugin, anthropic_api):
        """The schema is API-enforced structured output, not a "please return
        JSON" instruction. If it ever migrated into the prompt or the system
        message, this catches it."""
        anthropic_api.respond_with_object({"summary": "s", "confidence": 1})

        anthropic_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

        body = anthropic_api.request["body"]
        schema = body["output_config"]["format"]["schema"]

        assert body["output_config"]["format"]["type"] == "json_schema"
        assert schema["title"] == "Recommendation"
        assert set(schema["properties"]) == {"summary", "confidence"}
        assert schema["properties"]["confidence"]["type"] == "integer"

        # ...and nowhere else. No tool is defined and no prose describes it.
        assert "tools" not in body
        assert "confidence" not in body["system"]
        assert "confidence" not in body["messages"][0]["content"]

    def test_sends_a_nested_schema(self, anthropic_plugin, anthropic_api):
        """Nested models, lists of models and enums have to survive schema
        generation as ``$defs``/``$ref``."""
        anthropic_api.respond_with_object(
            {
                "title": "T",
                "priority": "high",
                "approved": True,
                "participants": [{"name": "a", "role": "ic"}],
            }
        )

        anthropic_plugin.chat_parse(prompt="extract the data", response_model=NestedRecommendation)

        schema = anthropic_api.request["body"]["output_config"]["format"]["schema"]

        assert set(schema["$defs"]) == {"Participant", "Priority"}
        assert schema["$defs"]["Priority"]["enum"] == ["low", "high"]
        assert schema["properties"]["participants"]["items"]["$ref"] == "#/$defs/Participant"

    def test_returns_a_populated_nested_model(self, anthropic_plugin, anthropic_api):
        anthropic_api.respond_with_object(
            {
                "title": "Suspicious login",
                "priority": "high",
                "approved": True,
                "participants": [{"name": "alice", "role": "ic"}, {"name": "bob"}],
                "tags": ["auth", "prod"],
                "note": None,
            }
        )

        result = anthropic_plugin.chat_parse(
            prompt="extract the data", response_model=NestedRecommendation
        )

        assert type(result) is NestedRecommendation
        assert result.priority is Priority.high
        assert result.approved is True
        assert result.participants == [
            Participant(name="alice", role="ic"),
            Participant(name="bob", role=None),
        ]
        assert result.tags == ["auth", "prod"]
        assert result.note is None

    def test_rejects_a_reply_that_is_not_json(self, anthropic_plugin, anthropic_api):
        anthropic_api.respond_with_text("I am afraid I cannot do that.")

        with pytest.raises(DispatchPluginException, match="did not match it"):
            anthropic_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

    def test_rejects_a_reply_missing_a_required_field(self, anthropic_plugin, anthropic_api):
        anthropic_api.respond_with_object({"summary": "no confidence given"})

        with pytest.raises(DispatchPluginException, match="did not match it"):
            anthropic_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

    def test_rejects_a_reply_with_a_wrongly_typed_field(self, anthropic_plugin, anthropic_api):
        anthropic_api.respond_with_object({"summary": "s", "confidence": "very"})

        with pytest.raises(DispatchPluginException, match="did not match it"):
            anthropic_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

    def test_rejects_a_reply_of_an_unexpected_shape(self, anthropic_plugin, anthropic_api):
        anthropic_api.respond_with_object(["not", "an", "object"])

        with pytest.raises(DispatchPluginException, match="did not match it"):
            anthropic_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

    def test_rejects_a_truncated_structured_reply(self, anthropic_plugin, anthropic_api):
        """Cut mid-JSON, so validation fails before stop_reason can be read --
        which is why the message mentions truncation as a likely cause."""
        anthropic_api.respond_with_truncation('{"summary": "Likely be')

        with pytest.raises(DispatchPluginException, match="truncated"):
            anthropic_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

    def test_rejects_a_refusal_and_names_the_category(self, anthropic_plugin, anthropic_api):
        """A refusal carries no text block, so it is caught by `stop_reason`
        rather than by validation. Returning it would surface as an
        AttributeError two frames away, inside the ai service."""
        anthropic_api.respond_with_refusal(category="cyber")

        with pytest.raises(DispatchPluginException, match=r"\(refusal\): cyber"):
            anthropic_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

    def test_a_truncated_reply_is_not_reported_as_an_empty_one(
        self, anthropic_plugin, anthropic_api
    ):
        """A reply cut off before any text block is produced -- only thinking,
        or nothing at all. `parsed_output` is None either way, so checking it
        first would blame the model for answering nothing when in fact the token
        budget ran out, and would omit the one remediation that helps."""
        anthropic_api.respond_with_blocks(
            [{"type": "thinking", "thinking": "reasoning that used the budget", "signature": "s"}],
            stop_reason="max_tokens",
        )

        with pytest.raises(DispatchPluginException, match="max_tokens"):
            anthropic_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

    def test_a_field_constraint_the_api_never_saw_is_still_enforced(
        self, anthropic_plugin, anthropic_api
    ):
        """The SDK does not send `minimum`/`maxLength` and friends -- it folds
        them into the field's description -- so Anthropic enforces only the
        types. Dispatch's own `PrimaryKey` is `gt=0`, and `TagRecommendations`
        asks the model to produce ids, so a reply that satisfies the schema
        Anthropic enforced can still violate the model. Pydantic is the only
        thing that checks, which is why validation must not be bypassed."""
        anthropic_api.respond_with_object({"summary": "s", "confidence": 5, "identifier": 0})

        with pytest.raises(DispatchPluginException, match="did not match it"):
            anthropic_plugin.chat_parse(prompt="extract the data", response_model=Bounded)

    def test_a_field_with_a_default_is_populated_rather_than_rejected(
        self, anthropic_plugin, anthropic_api
    ):
        """Not a defect, and deliberately pinned because it is surprising: every
        field of every model in `dispatch/ai/models.py` has a default, so those
        models emit no `required` array and `{}` is a valid reply. The plugin
        returns the all-default instance rather than raising -- it cannot tell
        "the model omitted this" from "the model meant the default". Whether an
        empty summary should reach an incident channel is the ai service's
        question, not this plugin's, and it is the same on the OpenAI path."""
        anthropic_api.respond_with_object({"summary": "s"})

        result = anthropic_plugin.chat_parse(prompt="extract the data", response_model=Defaulted)

        assert result == Defaulted(summary="s", confidence=0, tags=[])


class TestUnbuildableRequest:
    """Failures the SDK raises *before* issuing a request.

    None of these is an ``AnthropicError``, so before they were handled they
    escaped the plugin as a bare ``ValueError``/``TypeError`` -- past the
    redaction in ``api_error`` and into the ai service's ``str(e)``.
    """

    def test_the_configuration_cannot_hold_a_max_tokens_the_sdk_refuses(self):
        """The SDK estimates `3600 * max_tokens / 128000` seconds for a
        non-streaming request and raises above ten minutes, i.e. over 21333.
        The bound keeps that out of the settings form entirely."""
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError):
            build_configuration(max_tokens=21334)

        assert build_configuration(max_tokens=21333).max_tokens == 21333

    def test_max_tokens_must_be_positive(self):
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError):
            build_configuration(max_tokens=0)

    @pytest.mark.parametrize("method", ["chat_completion", "chat_parse"])
    def test_a_model_specific_cap_becomes_a_plugin_exception(self, anthropic_api, method):
        """`claude-opus-4-0` and `4-1` cap non-streaming replies at 8192, below
        the configuration's own ceiling -- so a configuration the schema accepts
        can still be refused, and only the plugin can catch it."""
        plugin = AnthropicPlugin()
        plugin.configuration = build_configuration(model="claude-opus-4-0", max_tokens=16000)

        with pytest.raises(DispatchPluginException, match="could not be built"):
            if method == "chat_completion":
                plugin.chat_completion(prompt="summarize this")
            else:
                plugin.chat_parse(prompt="extract", response_model=Recommendation)

        assert anthropic_api.requests == []

    def test_a_response_model_the_sdk_cannot_express_becomes_a_plugin_exception(
        self, anthropic_plugin, anthropic_api
    ):
        """Schema generation raises a bare ValueError for a field with no JSON
        schema type. `ValidationError` subclasses `ValueError`, so this also
        pins that the two except clauses stay in the right order."""
        from typing import Any

        class Unexpressible(BaseModel):
            anything: Any

        with pytest.raises(DispatchPluginException, match="could not be built"):
            anthropic_plugin.chat_parse(prompt="extract", response_model=Unexpressible)

        assert anthropic_api.requests == []


class TestConfiguration:
    def test_an_unreadable_configuration_is_reported_as_such(self, anthropic_api):
        """``PluginInstance.configuration`` is None for an instance that was
        never configured. Reaching through it gives "'NoneType' object has no
        attribute 'model'", which is what would reach the incident timeline."""
        plugin = AnthropicPlugin()
        plugin.configuration = None

        with pytest.raises(DispatchPluginException, match="could not be read"):
            plugin.chat_completion(prompt="summarize this")

    def test_an_empty_api_key_is_reported_before_the_request(self, anthropic_api):
        """``Anthropic(api_key="")`` constructs happily and sends an empty
        header, so without this the operator sees an opaque 401."""
        plugin = AnthropicPlugin()
        plugin.configuration = build_configuration(api_key="")

        with pytest.raises(DispatchPluginException, match="no API key"):
            plugin.chat_completion(prompt="summarize this")

        assert anthropic_api.requests == []

    def test_the_api_key_is_not_in_the_configuration_repr(self):
        """``SecretStr`` is what keeps the key out of a logged configuration."""
        assert API_KEY not in repr(build_configuration())
        assert API_KEY not in str(build_configuration())


class TestErrorHandling:
    def test_an_api_failure_becomes_a_plugin_exception(self, anthropic_plugin, anthropic_api):
        anthropic_api.respond_with_status(500, "internal error")

        with pytest.raises(DispatchPluginException, match="HTTP 500"):
            anthropic_plugin.chat_completion(prompt="summarize this")

    def test_the_failure_names_the_structured_fields(self, anthropic_plugin, anthropic_api):
        """status, error type and request id -- all enum-like or opaque, none of
        them free text from the response body."""
        anthropic_api.respond_with_status(429, "slow down", error_type="rate_limit_error")

        with pytest.raises(DispatchPluginException) as excinfo:
            anthropic_plugin.chat_completion(prompt="summarize this")

        assert "HTTP 429" in str(excinfo.value)
        assert "rate_limit_error" in str(excinfo.value)
        assert "req_test_0001" in str(excinfo.value)

    def test_the_message_body_is_not_republished(self, anthropic_plugin, anthropic_api):
        """``dispatch.ai.service`` interpolates this exception into
        ``error_message``, which is posted into the case channel."""
        anthropic_api.respond_with_status(400, "a very detailed internal message")

        with pytest.raises(DispatchPluginException) as excinfo:
            anthropic_plugin.chat_completion(prompt="summarize this")

        assert "a very detailed internal message" not in str(excinfo.value)

    def test_an_undocumented_error_type_is_not_republished(self, anthropic_plugin, anthropic_api):
        """The SDK ``cast``s ``error.type`` out of the body without validating
        it, so it is free text chosen by whatever answered -- and
        ``ANTHROPIC_BASE_URL`` means that need not be Anthropic. This message is
        rendered into a Slack channel as mrkdwn, where ``<!channel>`` is a live
        notification, so only the nine documented values are reproduced."""
        anthropic_api.respond_with_status(
            400, "boom", error_type="<!channel> not a real error type"
        )

        with pytest.raises(DispatchPluginException) as excinfo:
            anthropic_plugin.chat_completion(prompt="summarize this")

        assert "<!channel>" not in str(excinfo.value)
        assert "not a real error type" not in str(excinfo.value)
        # The status and request id still get through.
        assert "HTTP 400" in str(excinfo.value)

    @pytest.mark.parametrize(
        "responder, injected",
        [
            ("undocumented_stop_reason", "<!channel> stopped"),
            ("undocumented_refusal_category", "<!here> declined"),
        ],
    )
    def test_an_undocumented_response_field_is_not_republished(
        self, anthropic_plugin, anthropic_api, responder, injected
    ):
        """``stop_reason`` and ``stop_details.category`` reach the same rendered
        string as ``error.type``, and the SDK *constructs* its response models
        rather than validating them -- so a ``Literal``-typed field accepts
        arbitrary text just as readily. All three are allowlisted together;
        hardening only one of them was the inconsistency."""
        getattr(anthropic_api, f"respond_with_{responder}")(injected)

        with pytest.raises(DispatchPluginException) as excinfo:
            anthropic_plugin.chat_completion(prompt="summarize this")

        assert injected not in str(excinfo.value)
        assert "<!" not in str(excinfo.value)

    def test_an_error_body_that_is_not_an_envelope_still_reports(
        self, anthropic_plugin, anthropic_api
    ):
        """A proxy or gateway answering with HTML leaves ``type`` unset."""
        anthropic_api.respond_with_non_envelope(502)

        with pytest.raises(DispatchPluginException, match="HTTP 502"):
            anthropic_plugin.chat_completion(prompt="summarize this")

    @pytest.mark.parametrize("method", ["chat_completion", "chat_parse"])
    def test_a_connection_failure_becomes_a_plugin_exception(
        self, anthropic_plugin, anthropic_api, method
    ):
        """The most common production failure of all, and the only one that
        exercises ``api_error``'s non-``APIStatusError`` branch."""
        anthropic_api.respond_with_connection_error()

        with pytest.raises(DispatchPluginException, match="APIConnectionError"):
            if method == "chat_completion":
                anthropic_plugin.chat_completion(prompt="summarize this")
            else:
                anthropic_plugin.chat_parse(
                    prompt="extract the data", response_model=Recommendation
                )

    @pytest.mark.parametrize("method", ["chat_completion", "chat_parse"])
    def test_the_api_key_never_reaches_the_raised_exception(
        self, anthropic_plugin, anthropic_api, method, caplog
    ):
        """Anthropic's real 401 quotes the submitted key back in the body, and
        ``str(APIStatusError)`` renders that body verbatim."""
        anthropic_api.respond_with_key_echo()

        with pytest.raises(DispatchPluginException) as excinfo:
            if method == "chat_completion":
                anthropic_plugin.chat_completion(prompt="summarize this")
            else:
                anthropic_plugin.chat_parse(
                    prompt="extract the data", response_model=Recommendation
                )

        assert API_KEY not in str(excinfo.value)
        # ...nor into the log line that deliberately keeps the rest of the body.
        assert API_KEY not in caplog.text
        assert "***" in caplog.text

    @pytest.mark.parametrize("method", ["chat_completion", "chat_parse"])
    def test_the_original_error_is_suppressed_from_the_traceback(
        self, anthropic_plugin, anthropic_api, method
    ):
        """``raise ... from None``: the ai service calls ``log.exception``, and a
        chained cause would republish the body it just kept out."""
        anthropic_api.respond_with_key_echo()

        with pytest.raises(DispatchPluginException) as excinfo:
            if method == "chat_completion":
                anthropic_plugin.chat_completion(prompt="summarize this")
            else:
                anthropic_plugin.chat_parse(
                    prompt="extract the data", response_model=Recommendation
                )

        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__ is True


class TestMetrics:
    """``apply`` iterates ``cls.__dict__``, so it only works as a *class*
    decorator -- applied to a method it silently returns the function untouched
    and no metric is ever emitted (this is why ``dispatch_microsoft_teams`` has
    none). Nothing else in the suite would notice."""

    @pytest.mark.parametrize("method", ["chat_completion", "chat_parse"])
    def test_the_decorators_are_applied(self, method):
        assert hasattr(getattr(AnthropicPlugin, method), "__wrapped__")

    @pytest.mark.parametrize("method", ["chat_completion", "chat_parse"])
    def test_a_call_emits_one_counter_and_one_timer(
        self, anthropic_plugin, anthropic_api, monkeypatch, method
    ):
        """Applied twice by accident, one request would count as two.

        The tag is asserted on both series, and it is the fully-qualified name,
        so an operator can tell this plugin's calls from the OpenAI plugin's.
        """
        counters, timers = [], []
        monkeypatch.setattr(
            "dispatch.decorators.metrics_provider.counter",
            lambda name, tags=None: counters.append(tags["function"]),
        )
        monkeypatch.setattr(
            "dispatch.decorators.metrics_provider.timer",
            lambda name, value=None, tags=None: timers.append(tags["function"]),
        )

        anthropic_api.respond_with_object({"summary": "s", "confidence": 1})
        if method == "chat_completion":
            anthropic_plugin.chat_completion(prompt="summarize this")
        else:
            anthropic_plugin.chat_parse(prompt="extract", response_model=Recommendation)

        expected = f"dispatch.plugins.dispatch_anthropic.plugin.AnthropicPlugin.{method}"
        assert counters == [expected]
        assert timers == [expected]

    def test_a_failing_call_is_not_excluded_from_the_counter(
        self, anthropic_plugin, anthropic_api, monkeypatch
    ):
        """`counter` wraps `timer`, so the counter fires before the call. It is
        the only one that does: `timer` has no `try/finally`, so a failure is
        counted but never timed and `function.elapsed.time` is a success-only
        series. Pinned because it is repo-wide behaviour, not this plugin's, and
        an operator reading the metrics needs to know which is which."""
        counters, timers = [], []
        monkeypatch.setattr(
            "dispatch.decorators.metrics_provider.counter",
            lambda name, tags=None: counters.append(tags["function"]),
        )
        monkeypatch.setattr(
            "dispatch.decorators.metrics_provider.timer",
            lambda name, value=None, tags=None: timers.append(tags["function"]),
        )
        anthropic_api.respond_with_status(500)

        with pytest.raises(DispatchPluginException):
            anthropic_plugin.chat_completion(prompt="summarize this")

        assert len(counters) == 1
        assert timers == []

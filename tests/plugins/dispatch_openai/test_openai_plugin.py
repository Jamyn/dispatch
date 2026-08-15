"""The OpenAIPlugin contract, driven through the real plugin (issue #75).

Every test here builds a real ``OpenAIPlugin`` holding a real
``OpenAIConfiguration`` and asserts on the HTTP request the openai SDK actually
produced. That is deliberate: issue #75 was three attribute and signature errors
that no test could see, because ``tests/ai/test_ai_service.py`` replaced the
whole plugin with a ``Mock`` -- and a ``Mock`` answers ``configuration.
chat_completion_model`` as readily as it answers a field that exists.

The three defects, and what pins each one here:

1. the service read ``configuration.chat_completion_model``; the configuration
   declares ``model`` -- ``test_chat_completion_sends_the_configured_model`` and
   the service-level tests in ``tests/ai/test_ai_service_contract.py``
2. the service passed ``system_message=``; neither plugin method accepted it --
   ``test_chat_completion_accepts_a_system_message`` and its ``chat_parse`` twin
3. the plugin read ``self.api_key`` / ``self.model`` / ``self.system_message``,
   none of which is ever assigned -- every test here, since a real plugin raises
   ``AttributeError`` on the first of them
"""

import pytest
from pydantic import BaseModel

from dispatch.exceptions import DispatchPluginException
from tests.plugins.dispatch_openai.fake_openai import (
    API_KEY,
    CONFIGURED_MODEL,
    CONFIGURED_SYSTEM_MESSAGE,
    build_configuration,
)


class Recommendation(BaseModel):
    """A structured-output model standing in for the ai service's own."""

    name: str
    confidence: int


CALLER_SYSTEM_MESSAGE = "Return structured data only."


def messages(request):
    """The chat messages the SDK put on the wire, by role."""
    return {m["role"]: m["content"] for m in request["body"]["messages"]}


class TestChatCompletion:
    def test_returns_the_assistant_text(self, openai_plugin, openai_api):
        openai_api.respond_with_text("A short incident summary.")

        assert openai_plugin.chat_completion(prompt="summarize this") == "A short incident summary."

    def test_sends_the_configured_model(self, openai_plugin, openai_api):
        """Regression, defect 1: the model comes from ``configuration.model``.

        ``CONFIGURED_MODEL`` is not the schema default, so a plugin that ignored
        the stored configuration would fail here rather than pass by accident.
        """
        openai_plugin.chat_completion(prompt="summarize this")

        assert openai_api.request["body"]["model"] == CONFIGURED_MODEL

    def test_authenticates_with_the_configured_api_key(self, openai_plugin, openai_api):
        """Regression, defect 3: the key is read from the configuration and
        unwrapped from ``SecretStr`` -- sending the wrapper would put the
        literal ``**********`` in the header."""
        openai_plugin.chat_completion(prompt="summarize this")

        assert openai_api.request["authorization"] == f"Bearer {API_KEY}"

    def test_sends_the_prompt_as_the_user_message(self, openai_plugin, openai_api):
        openai_plugin.chat_completion(prompt="summarize this")

        assert messages(openai_api.request)["user"] == "summarize this"

    def test_accepts_a_system_message(self, openai_plugin, openai_api):
        """Regression, defect 2: the kwarg the ai service passes is accepted."""
        openai_plugin.chat_completion(prompt="summarize this", system_message=CALLER_SYSTEM_MESSAGE)

        assert messages(openai_api.request)["system"] == CALLER_SYSTEM_MESSAGE

    def test_falls_back_to_the_configured_system_message(self, openai_plugin, openai_api):
        """No system message supplied is the pre-#75 behavior: use the config."""
        openai_plugin.chat_completion(prompt="summarize this")

        assert messages(openai_api.request)["system"] == CONFIGURED_SYSTEM_MESSAGE

    def test_an_explicit_none_system_message_falls_back_too(self, openai_plugin, openai_api):
        openai_plugin.chat_completion(prompt="summarize this", system_message=None)

        assert messages(openai_api.request)["system"] == CONFIGURED_SYSTEM_MESSAGE

    def test_a_refusal_is_an_error_not_an_empty_summary(self, openai_plugin, openai_api):
        """Content is null when the model refuses. Returning it would write None
        into ``Incident.summary`` and read as 'no summary generated'."""
        openai_api.respond_with_refusal("I cannot help with that.")

        with pytest.raises(DispatchPluginException):
            openai_plugin.chat_completion(prompt="summarize this")

    def test_a_truncated_reply_is_an_error(self, openai_plugin, openai_api):
        """``create`` reports truncation in finish_reason and returns the partial
        text -- unlike ``parse``, which raises. Half a sentence must not be
        committed to ``Incident.summary`` as a finished summary."""
        openai_api.respond_with_truncation("A truncated summary that stops mid-sen")

        with pytest.raises(DispatchPluginException):
            openai_plugin.chat_completion(prompt="summarize this")

    def test_empty_content_is_an_error(self, openai_plugin, openai_api):
        """An empty summary is stored and then reads as 'not generated'."""
        openai_api.respond_with_text("")

        with pytest.raises(DispatchPluginException):
            openai_plugin.chat_completion(prompt="summarize this")

    def test_an_api_error_becomes_a_plugin_exception(self, openai_plugin, openai_api):
        """The ai service turns this into its own error_message; the plugin does
        not swallow it into a plausible-looking empty result."""
        openai_api.respond_with_status(500, code="server_error")

        with pytest.raises(DispatchPluginException) as excinfo:
            openai_plugin.chat_completion(prompt="summarize this")

        assert "500" in str(excinfo.value)

    def test_an_authentication_failure_never_repeats_the_api_key(
        self, openai_plugin, openai_api, caplog
    ):
        """OpenAI's 401 body quotes the submitted key back, and the ai service
        interpolates the exception into an ``error_message`` that is posted to
        the case channel and returned to the browser."""
        openai_api.respond_with_key_echo()

        with pytest.raises(DispatchPluginException) as excinfo:
            openai_plugin.chat_completion(prompt="summarize this")

        assert API_KEY not in str(excinfo.value)
        assert API_KEY not in repr(excinfo.value)
        assert excinfo.value.__cause__ is None, "a chained cause would carry the body into logs"
        assert "invalid_api_key" in str(excinfo.value)

    def test_the_logged_body_is_redacted_but_kept(self, openai_plugin, openai_api, caplog):
        """The body is where the useful detail is, so it is logged -- with the
        one thing OpenAI may quote back removed."""
        import logging

        openai_api.respond_with_key_echo()

        with caplog.at_level(logging.WARNING, logger="dispatch.plugins.dispatch_openai.plugin"):
            with pytest.raises(DispatchPluginException):
                openai_plugin.chat_completion(prompt="summarize this")

        logged = caplog.text
        assert "Incorrect API key provided" in logged, "the body must not be dropped"
        assert API_KEY not in logged


class TestChatParse:
    def test_returns_an_instance_of_the_response_model(self, openai_plugin, openai_api):
        openai_api.respond_with_object({"name": "alpha", "confidence": 3})

        result = openai_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

        assert isinstance(result, Recommendation)
        assert result.name == "alpha"
        assert result.confidence == 3

    def test_sends_the_response_model_as_a_json_schema(self, openai_plugin, openai_api):
        """Structured output, not free text: the request carries the model's own
        schema, which is what makes the reply parseable."""
        openai_api.respond_with_object({"name": "alpha", "confidence": 3})

        openai_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

        response_format = openai_api.request["body"]["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["name"] == "Recommendation"
        assert set(response_format["json_schema"]["schema"]["required"]) == {"name", "confidence"}

    def test_sends_the_configured_model(self, openai_plugin, openai_api):
        """Regression, defect 1, on the structured-output path."""
        openai_api.respond_with_object({"name": "alpha", "confidence": 3})

        openai_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

        assert openai_api.request["body"]["model"] == CONFIGURED_MODEL

    def test_authenticates_with_the_configured_api_key(self, openai_plugin, openai_api):
        """Regression, defect 3, on the structured-output path."""
        openai_api.respond_with_object({"name": "alpha", "confidence": 3})

        openai_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

        assert openai_api.request["authorization"] == f"Bearer {API_KEY}"

    def test_sends_the_prompt_as_the_user_message(self, openai_plugin, openai_api):
        openai_api.respond_with_object({"name": "alpha", "confidence": 3})

        openai_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

        assert messages(openai_api.request)["user"] == "extract the data"

    def test_accepts_a_system_message(self, openai_plugin, openai_api):
        """Regression, defect 2, on the structured-output path -- the one all
        four of the ai service's ``chat_parse`` call sites use."""
        openai_api.respond_with_object({"name": "alpha", "confidence": 3})

        openai_plugin.chat_parse(
            prompt="extract the data",
            response_model=Recommendation,
            system_message=CALLER_SYSTEM_MESSAGE,
        )

        assert messages(openai_api.request)["system"] == CALLER_SYSTEM_MESSAGE

    def test_falls_back_to_the_configured_system_message(self, openai_plugin, openai_api):
        openai_api.respond_with_object({"name": "alpha", "confidence": 3})

        openai_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

        assert messages(openai_api.request)["system"] == CONFIGURED_SYSTEM_MESSAGE

    def test_a_refusal_is_an_error_not_a_none_result(self, openai_plugin, openai_api):
        """``parsed`` is None on a refusal. Returning it would surface as an
        AttributeError inside the caller's ``result.recommendations``."""
        openai_api.respond_with_refusal("I cannot help with that.")

        with pytest.raises(DispatchPluginException):
            openai_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

    def test_a_malformed_reply_propagates(self, openai_plugin, openai_api):
        """The SDK raises rather than returning a half-built model.

        A pydantic error, not an ``OpenAIError`` -- so it deliberately does not
        go through ``api_error``; there is no response body to redact and the
        caller should see the real parse failure.
        """
        from pydantic import ValidationError

        openai_api.respond_with_malformed_content("not json at all")

        with pytest.raises(ValidationError):
            openai_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

    def test_a_truncated_reply_is_an_error(self, openai_plugin, openai_api):
        """Unlike ``create``, the SDK itself raises ``LengthFinishReasonError``
        here -- which is an ``OpenAIError``, so it arrives as a plugin
        exception rather than escaping raw."""
        openai_api.respond_with_truncation('{"name": "alp')

        with pytest.raises(DispatchPluginException):
            openai_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

    def test_the_refusal_reason_is_reported(self, openai_plugin, openai_api):
        openai_api.respond_with_refusal("I cannot help with that.")

        with pytest.raises(DispatchPluginException) as excinfo:
            openai_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

        assert "I cannot help with that." in str(excinfo.value)

    def test_an_api_error_becomes_a_plugin_exception(self, openai_plugin, openai_api):
        openai_api.respond_with_status(500, code="server_error")

        with pytest.raises(DispatchPluginException) as excinfo:
            openai_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

        assert "500" in str(excinfo.value)

    def test_an_authentication_failure_never_repeats_the_api_key(self, openai_plugin, openai_api):
        openai_api.respond_with_key_echo()

        with pytest.raises(DispatchPluginException) as excinfo:
            openai_plugin.chat_parse(prompt="extract the data", response_model=Recommendation)

        assert API_KEY not in str(excinfo.value)
        assert excinfo.value.__cause__ is None


class TestConfiguration:
    def test_the_api_key_is_a_secret(self):
        """It must not appear in a repr, a log line or an exception rendering."""
        configuration = build_configuration()

        assert API_KEY not in repr(configuration)
        assert API_KEY not in str(configuration)
        assert configuration.api_key.get_secret_value() == API_KEY

    def test_an_unreadable_configuration_says_what_to_do(self, openai_api):
        """``PluginInstance.configuration`` yields None for a plugin instance
        that was never configured, or whose stored JSON no longer satisfies the
        schema. Reaching through that None gives ``'NoneType' object has no
        attribute 'model'``, which reaches the incident timeline."""
        from dispatch.plugins.dispatch_openai.plugin import OpenAIPlugin

        unconfigured = OpenAIPlugin()
        unconfigured.configuration = None

        with pytest.raises(DispatchPluginException) as excinfo:
            unconfigured.chat_completion(prompt="summarize this")

        assert "OpenAI" in str(excinfo.value)
        assert openai_api.requests == []

    def test_the_model_defaults_when_unset(self):
        from dispatch.plugins.dispatch_openai.config import OpenAIConfiguration

        assert OpenAIConfiguration(api_key=API_KEY).model == "gpt-4o"

    def test_the_configuration_field_is_model(self):
        """The schema side of defect 1, stated directly: ``chat_completion_model``
        is not and has never been a field here, so no caller may read it.

        Documentation, not a regression test -- this passed before the fix too.
        The service was wrong, not the schema.
        """
        from dispatch.plugins.dispatch_openai.config import OpenAIConfiguration

        assert "model" in OpenAIConfiguration.model_fields
        assert "chat_completion_model" not in OpenAIConfiguration.model_fields


class TestProductionResponseModels:
    """Every structured-output model the ai service actually sends.

    ``Recommendation`` above is a two-string toy. The real models carry
    constructs the toy does not -- ``default`` on most fields, and
    ``exclusiveMinimum``/``exclusiveMaximum`` via ``PrimaryKey`` inside
    ``TagRecommendations`` -- and the SDK reproduces them verbatim into the
    strict json_schema. If one of them ever becomes something the SDK cannot
    convert, this is where it shows up rather than in production.
    """

    @pytest.mark.parametrize(
        "response_model, reply",
        [
            ("CaseSignalSummary", {"summary": "s"}),
            ("ReadInSummary", {"timeline": [], "actions_taken": []}),
            ("TacticalReport", {"conditions": "c", "actions": [], "needs": []}),
            ("TagRecommendations", {"recommendations": []}),
        ],
    )
    def test_the_schema_is_built_and_the_reply_parses(
        self, openai_plugin, openai_api, response_model, reply
    ):
        from dispatch.ai import models as ai_models

        model = getattr(ai_models, response_model)
        openai_api.respond_with_object(reply)

        result = openai_plugin.chat_parse(prompt="go", response_model=model)

        assert isinstance(result, model)
        schema = openai_api.request["body"]["response_format"]["json_schema"]
        assert schema["name"] == response_model
        assert schema["strict"] is True


class TestPluginInterface:
    """The base class is the contract the ai service is written against."""

    @pytest.mark.parametrize("name", ["chat_completion", "chat_parse"])
    def test_every_implementation_matches_the_base_class(self, name):
        """Full signatures, not just parameter names, and every subclass -- a
        second AI plugin must not be free to drift the way this one did."""
        import inspect

        from dispatch.plugins.bases import ArtificialIntelligencePlugin
        from dispatch.plugins.dispatch_openai.plugin import OpenAIPlugin  # noqa: F401

        base = inspect.signature(getattr(ArtificialIntelligencePlugin, name))

        implementations = ArtificialIntelligencePlugin.__subclasses__()
        assert OpenAIPlugin in implementations, "the plugin under test was not registered"

        for implementation in implementations:
            actual = inspect.signature(getattr(implementation, name))
            # Rendered, not compared by identity: `T` is a distinct TypeVar
            # object in each module, so `Signature.__eq__` fails on annotations
            # that are the same type in every way that matters.
            assert str(actual) == str(base), f"{implementation.__name__}.{name}"

    def test_the_base_class_accepts_what_the_service_sends(self):
        """A signature check, not a call: the base raises NotImplementedError."""
        import inspect

        from dispatch.plugins.bases import ArtificialIntelligencePlugin

        completion = inspect.signature(ArtificialIntelligencePlugin.chat_completion).parameters
        assert "prompt" in completion
        assert "system_message" in completion

        parse = inspect.signature(ArtificialIntelligencePlugin.chat_parse).parameters
        assert "prompt" in parse
        assert "response_model" in parse
        assert "system_message" in parse

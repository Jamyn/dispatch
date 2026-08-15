"""The ai service driven against the real Anthropic plugin (issue #79).

The sibling ``test_ai_service_contract.py`` does this for OpenAI, and exists
because ``test_ai_service.py`` replaces the whole plugin with a ``Mock`` -- which
is how three years of a broken plugin passed CI, a ``Mock`` answering a
configuration field that did not exist and swallowing a keyword the plugin never
accepted (issue #75).

Issue #79's requirement is that the new plugin must not inherit that break, and
the only way to show it is to run the two together with nothing mocked between
them:

    real ai service -> real AnthropicPlugin -> real AnthropicConfiguration -> fake API

One test class per GenAI call site in ``dispatch/ai/service.py``, because each
reads the model out of the plugin configuration independently.

Nothing here touches the OpenAI plugin: the Anthropic plugin has to satisfy the
service's existing callers unchanged, which is the whole point.
"""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dispatch.ai.models import (
    CaseSignalSummaryResponse,
    ReadInSummaryResponse,
    TacticalReportResponse,
)
from dispatch.ai.service import (
    generate_case_signal_summary,
    generate_incident_summary,
    generate_read_in_summary,
    generate_tactical_report,
    get_tag_recommendations,
)
from dispatch.enums import Visibility
from dispatch.plugins.dispatch_slack.models import IncidentSubjects, SubjectMetadata
from dispatch.tag.models import TagRecommendationResponse
from tests.factories import DocumentFactory
from tests.plugins.dispatch_anthropic.fake_anthropic import (
    API_KEY,
    CONFIGURED_MAX_TOKENS,
    CONFIGURED_MODEL,
)


@pytest.fixture
def genai_plugin(anthropic_plugin):
    """A plugin *record* wrapping the real plugin.

    ``PluginInstance.instance`` is a property that hands back the plugin object
    with its configuration attached, which is exactly what this stands in for.
    The plugin itself is not faked -- that is the whole point of this module.
    """
    return SimpleNamespace(instance=anthropic_plugin)


def only_conversation(mock_conv_plugin, genai_plugin):
    """A ``get_active_instance`` side effect serving the AI and conversation plugins."""

    def side_effect(db_session, plugin_type, project_id):
        if plugin_type == "artificial-intelligence":
            return genai_plugin
        if plugin_type == "conversation":
            return mock_conv_plugin
        return None

    return side_effect


def sent(request):
    """The system prompt and the user prompt, as Anthropic received them.

    Anthropic takes the system prompt as a top-level field rather than as a
    leading message, so this is not the same shape as the OpenAI sibling's
    ``messages()`` helper -- and that difference is precisely what the plugin
    exists to absorb.
    """
    body = request["body"]
    return {"system": body["system"], "user": body["messages"][0]["content"]}


class TestReadInSummary:
    """``generate_read_in_summary`` -- chat_parse with a system message."""

    @pytest.fixture
    def conversation_plugin(self):
        conv = Mock()
        conv.instance.get_conversation.return_value = [
            {"user": "user1", "text": "Alert received", "timestamp": "2024-01-01 10:00"}
        ]
        return conv

    def call(self, session, conversation_plugin, genai_plugin, project):
        # A real `SubjectMetadata`, which is what the Slack callers pass, not a
        # `Mock`: a Mock answers a misspelled attribute exactly the way it
        # answered `configuration.chat_completion_model`.
        subject = SubjectMetadata(id="123", type=IncidentSubjects.incident)

        with (
            patch("dispatch.ai.service.event_service.get_recent_summary_event", return_value=None),
            patch("dispatch.ai.service.event_service.log_incident_event"),
            patch("dispatch.ai.service.plugin_service.get_active_instance") as get_plugin,
        ):
            get_plugin.side_effect = only_conversation(conversation_plugin, genai_plugin)
            return generate_read_in_summary(
                db_session=session,
                subject=subject,
                project=project,
                channel_id="test-channel",
                important_reaction="white_check_mark",
                participant_email="test@example.com",
            )

    def test_returns_the_parsed_summary(
        self, session, conversation_plugin, genai_plugin, anthropic_api, project
    ):
        anthropic_api.respond_with_object(
            {
                "timeline": ["10:00 alert received"],
                "actions_taken": ["investigated"],
                "current_status": "Resolved",
                "summary": "Investigated and resolved.",
            }
        )

        result = self.call(session, conversation_plugin, genai_plugin, project)

        assert isinstance(result, ReadInSummaryResponse)
        assert result.error_message is None
        assert result.summary.current_status == "Resolved"
        assert result.summary.summary == "Investigated and resolved."

    def test_uses_the_configured_model_and_key(
        self, session, conversation_plugin, genai_plugin, anthropic_api, project
    ):
        """The field the service actually reads is ``configuration.model``."""
        anthropic_api.respond_with_object({"timeline": [], "actions_taken": []})

        self.call(session, conversation_plugin, genai_plugin, project)

        assert anthropic_api.request["body"]["model"] == CONFIGURED_MODEL
        assert anthropic_api.request["api_key"] == API_KEY
        assert anthropic_api.request["body"]["max_tokens"] == CONFIGURED_MAX_TOKENS

    def test_the_services_system_message_reaches_anthropic(
        self, session, conversation_plugin, genai_plugin, anthropic_api, project
    ):
        """The service's system message is not merely accepted, it displaces the
        plugin's configured default."""
        from dispatch.ai.strings import READ_IN_SUMMARY_SYSTEM_MESSAGE
        from tests.plugins.dispatch_anthropic.fake_anthropic import CONFIGURED_SYSTEM_MESSAGE

        anthropic_api.respond_with_object({"timeline": [], "actions_taken": []})

        self.call(session, conversation_plugin, genai_plugin, project)

        system = sent(anthropic_api.request)["system"]
        assert system.startswith(READ_IN_SUMMARY_SYSTEM_MESSAGE)
        assert system != CONFIGURED_SYSTEM_MESSAGE

    def test_the_response_model_reaches_anthropic_as_a_schema(
        self, session, conversation_plugin, genai_plugin, anthropic_api, project
    ):
        """The service's own ``ReadInSummary`` -- not a test fixture model --
        has to survive schema generation and come back validated."""
        anthropic_api.respond_with_object({"timeline": [], "actions_taken": []})

        self.call(session, conversation_plugin, genai_plugin, project)

        schema = anthropic_api.request["body"]["output_config"]["format"]["schema"]
        assert schema["title"] == "ReadInSummary"
        assert set(schema["properties"]) == {
            "timeline",
            "actions_taken",
            "current_status",
            "summary",
        }

    def test_an_api_failure_becomes_an_error_message(
        self, session, conversation_plugin, genai_plugin, anthropic_api, project
    ):
        anthropic_api.respond_with_status(500)

        result = self.call(session, conversation_plugin, genai_plugin, project)

        assert result.summary is None
        assert result.error_message.startswith("Error generating read-in summary")

    def test_the_api_key_is_not_in_the_error_message(
        self, session, conversation_plugin, genai_plugin, anthropic_api, project
    ):
        """``error_message`` is posted into the case channel and returned to the
        browser, and Anthropic's 401 body quotes the submitted key back."""
        anthropic_api.respond_with_key_echo()

        result = self.call(session, conversation_plugin, genai_plugin, project)

        assert result.error_message
        assert API_KEY not in result.error_message

    def test_an_unconfigured_plugin_becomes_an_error_message(
        self, session, conversation_plugin, genai_plugin, anthropic_api, project
    ):
        genai_plugin.instance.configuration = None

        result = self.call(session, conversation_plugin, genai_plugin, project)

        assert result.summary is None
        assert "not configured" in result.error_message
        assert anthropic_api.requests == []


class TestTacticalReport:
    """``generate_tactical_report`` -- chat_parse against an incident."""

    def call(self, session, genai_plugin, incident, project):
        conv = Mock()
        conv.instance.get_conversation.return_value = [{"user": "u", "text": "t"}]

        with (
            patch("dispatch.ai.service.event_service.log_incident_event"),
            patch("dispatch.ai.service.plugin_service.get_active_instance") as get_plugin,
        ):
            get_plugin.side_effect = only_conversation(conv, genai_plugin)
            return generate_tactical_report(
                db_session=session,
                incident=incident,
                project=project,
                important_reaction="white_check_mark",
            )

    def test_returns_the_parsed_report(
        self, session, genai_plugin, anthropic_api, incident, project
    ):
        anthropic_api.respond_with_object(
            {"conditions": "Contained", "actions": ["rotated keys"], "needs": []}
        )

        result = self.call(session, genai_plugin, incident, project)

        assert isinstance(result, TacticalReportResponse)
        assert result.error_message is None
        assert result.tactical_report.conditions == "Contained"

    def test_uses_the_configured_model(
        self, session, genai_plugin, anthropic_api, incident, project
    ):
        anthropic_api.respond_with_object({"conditions": "", "actions": [], "needs": []})

        self.call(session, genai_plugin, incident, project)

        assert anthropic_api.request["body"]["model"] == CONFIGURED_MODEL

    def test_the_services_system_message_reaches_anthropic(
        self, session, genai_plugin, anthropic_api, incident, project
    ):
        from dispatch.ai.strings import TACTICAL_REPORT_SYSTEM_MESSAGE

        anthropic_api.respond_with_object({"conditions": "", "actions": [], "needs": []})

        self.call(session, genai_plugin, incident, project)

        assert sent(anthropic_api.request)["system"].startswith(TACTICAL_REPORT_SYSTEM_MESSAGE)

    def test_an_unconfigured_plugin_becomes_an_error_message(
        self, session, genai_plugin, anthropic_api, incident, project
    ):
        genai_plugin.instance.configuration = None

        result = self.call(session, genai_plugin, incident, project)

        assert result.tactical_report is None
        assert "not configured" in result.error_message
        assert anthropic_api.requests == []


class TestIncidentSummary:
    """``generate_incident_summary`` -- the only ``chat_completion`` call site."""

    def call(self, session, incident, genai_plugin):
        storage = Mock()
        storage.instance.get.return_value = "The post-incident review document text."

        def side_effect(db_session, plugin_type, project_id):
            if plugin_type == "artificial-intelligence":
                return genai_plugin
            if plugin_type == "storage":
                return storage
            return None

        with (
            patch("dispatch.ai.service.event_service.log_incident_event"),
            patch("dispatch.ai.service.plugin_service.get_active_instance") as get_plugin,
        ):
            get_plugin.side_effect = side_effect
            return generate_incident_summary(incident=incident, db_session=session)

    @pytest.fixture
    def incident(self, session, incident):
        incident.incident_review_document = DocumentFactory()
        session.commit()
        return incident

    def test_returns_the_assistant_text_and_stores_it(
        self, session, incident, genai_plugin, anthropic_api
    ):
        """``Incident.summary`` is a string column and this function is annotated
        ``-> str``; returning the SDK's message object, or its content-block
        list, would fail on commit rather than on assignment."""
        anthropic_api.respond_with_text("A concise incident summary.")

        result = self.call(session, incident, genai_plugin)

        assert result == "A concise incident summary."
        assert incident.summary == "A concise incident summary."

    def test_uses_the_configured_model_and_key(
        self, session, incident, genai_plugin, anthropic_api
    ):
        anthropic_api.respond_with_text("A concise incident summary.")

        self.call(session, incident, genai_plugin)

        assert anthropic_api.request["body"]["model"] == CONFIGURED_MODEL
        assert anthropic_api.request["api_key"] == API_KEY

    def test_the_services_system_message_reaches_anthropic(
        self, session, incident, genai_plugin, anthropic_api
    ):
        from dispatch.ai.strings import INCIDENT_SUMMARY_SYSTEM_MESSAGE

        anthropic_api.respond_with_text("A concise incident summary.")

        self.call(session, incident, genai_plugin)

        assert sent(anthropic_api.request)["system"] == INCIDENT_SUMMARY_SYSTEM_MESSAGE

    def test_the_review_document_reaches_the_prompt(
        self, session, incident, genai_plugin, anthropic_api
    ):
        anthropic_api.respond_with_text("A concise incident summary.")

        self.call(session, incident, genai_plugin)

        assert "The post-incident review document text." in sent(anthropic_api.request)["user"]

    def test_an_api_failure_does_not_store_a_summary(
        self, session, incident, genai_plugin, anthropic_api
    ):
        anthropic_api.respond_with_status(500)

        result = self.call(session, incident, genai_plugin)

        # The request must have been issued: a contract error before the call
        # would produce the same message and prove nothing.
        assert anthropic_api.requests
        assert result == "Incident summary not generated. An error occurred."
        assert incident.summary is None

    def test_a_truncated_reply_does_not_store_a_summary(
        self, session, incident, genai_plugin, anthropic_api
    ):
        """Anthropic returns a truncated reply as a 200 with the fragment. The
        plugin rejecting it is what keeps half a sentence out of the incident
        record and out of the "AI-generated incident summary created" event."""
        anthropic_api.respond_with_truncation("A concise incident sum")

        result = self.call(session, incident, genai_plugin)

        assert result == "Incident summary not generated. An error occurred."
        assert incident.summary is None

    def test_an_unconfigured_plugin_does_not_call_anthropic(
        self, session, incident, genai_plugin, anthropic_api
    ):
        genai_plugin.instance.configuration = None

        result = self.call(session, incident, genai_plugin)

        assert "not configured" in result
        assert incident.summary is None
        assert anthropic_api.requests == []


class TestTagRecommendations:
    """``get_tag_recommendations`` -- chat_parse over the project's tags."""

    @pytest.fixture
    def taggable_case(self, session, case, tag_type):
        from tests.factories import TagFactory

        tag_type.genai_suggestions = True
        tag_type.project = case.project
        TagFactory(project=case.project, tag_type=tag_type)
        session.commit()
        return case

    def call(self, session, taggable_case, genai_plugin):
        with patch("dispatch.ai.service.plugin_service.get_active_instance") as get_plugin:
            get_plugin.side_effect = lambda db_session, plugin_type, project_id: (
                genai_plugin if plugin_type == "artificial-intelligence" else None
            )
            return get_tag_recommendations(
                db_session=session,
                project_id=taggable_case.project.id,
                case_id=taggable_case.id,
            )

    def test_returns_the_parsed_recommendations(
        self, session, taggable_case, genai_plugin, anthropic_api
    ):
        anthropic_api.respond_with_object({"recommendations": []})

        result = self.call(session, taggable_case, genai_plugin)

        assert isinstance(result, TagRecommendationResponse)
        assert result.error_message is None

    def test_uses_the_configured_model_and_key(
        self, session, taggable_case, genai_plugin, anthropic_api
    ):
        anthropic_api.respond_with_object({"recommendations": []})

        self.call(session, taggable_case, genai_plugin)

        assert anthropic_api.request["body"]["model"] == CONFIGURED_MODEL
        assert anthropic_api.request["api_key"] == API_KEY

    def test_the_nested_response_model_reaches_anthropic_as_a_schema(
        self, session, taggable_case, genai_plugin, anthropic_api
    ):
        """``TagRecommendations`` is the one service model with nested models,
        so it is the one that exercises ``$defs``/``$ref`` generation against a
        real Dispatch schema rather than a fixture."""
        anthropic_api.respond_with_object({"recommendations": []})

        self.call(session, taggable_case, genai_plugin)

        schema = anthropic_api.request["body"]["output_config"]["format"]["schema"]
        assert schema["title"] == "TagRecommendations"
        assert "TagTypeRecommendation" in schema["$defs"]

    def test_the_services_system_message_reaches_anthropic(
        self, session, taggable_case, genai_plugin, anthropic_api
    ):
        from dispatch.ai.strings import TAG_RECOMMENDATION_SYSTEM_MESSAGE

        anthropic_api.respond_with_object({"recommendations": []})

        self.call(session, taggable_case, genai_plugin)

        assert sent(anthropic_api.request)["system"] == TAG_RECOMMENDATION_SYSTEM_MESSAGE

    def test_an_api_failure_becomes_an_error_message(
        self, session, taggable_case, genai_plugin, anthropic_api
    ):
        anthropic_api.respond_with_status(500)

        result = self.call(session, taggable_case, genai_plugin)

        assert anthropic_api.requests
        assert result.recommendations == []
        assert result.error_message


class TestCaseSignalSummary:
    """``generate_case_signal_summary`` -- the signal-analysis call site.

    The one call site whose system message is chosen at runtime (signal ->
    prompt -> default, `service.py:365-373`), which is the reason
    `system_message` exists on the plugin interface at all.
    """

    @pytest.fixture
    def genai_case(self, session, case, signal_instance):
        signal_instance.case = case
        signal_instance.signal.genai_enabled = True
        signal_instance.signal.genai_prompt = "Analyze this detection."
        signal_instance.signal.genai_system_message = "You are a detection analyst."
        case.visibility = Visibility.open
        session.commit()
        return case

    def call(self, session, genai_case, genai_plugin):
        with patch("dispatch.ai.service.plugin_service.get_active_instance") as get_plugin:
            get_plugin.side_effect = lambda db_session, plugin_type, project_id: (
                genai_plugin if plugin_type == "artificial-intelligence" else None
            )
            return generate_case_signal_summary(case=genai_case, db_session=session)

    def test_returns_the_parsed_summary(self, session, genai_case, genai_plugin, anthropic_api):
        anthropic_api.respond_with_object({"summary": "Benign, matches the runbook."})

        result = self.call(session, genai_case, genai_plugin)

        assert isinstance(result, CaseSignalSummaryResponse)
        assert result.error_message is None
        assert result.summary.summary == "Benign, matches the runbook."

    def test_uses_the_configured_model_and_key(
        self, session, genai_case, genai_plugin, anthropic_api
    ):
        anthropic_api.respond_with_object({"summary": "Benign."})

        self.call(session, genai_case, genai_plugin)

        assert anthropic_api.request["body"]["model"] == CONFIGURED_MODEL
        assert anthropic_api.request["api_key"] == API_KEY

    def test_the_signals_system_message_reaches_anthropic(
        self, session, genai_case, genai_plugin, anthropic_api
    ):
        """The per-signal system message, chosen over both the service default
        and the plugin's own configured one."""
        anthropic_api.respond_with_object({"summary": "Benign."})

        self.call(session, genai_case, genai_plugin)

        assert sent(anthropic_api.request)["system"] == "You are a detection analyst."

    def test_the_signals_prompt_reaches_anthropic(
        self, session, genai_case, genai_plugin, anthropic_api
    ):
        anthropic_api.respond_with_object({"summary": "Benign."})

        self.call(session, genai_case, genai_plugin)

        assert "Analyze this detection." in sent(anthropic_api.request)["user"]

    def test_the_response_model_reaches_anthropic_as_a_schema(
        self, session, genai_case, genai_plugin, anthropic_api
    ):
        anthropic_api.respond_with_object({"summary": "Benign."})

        self.call(session, genai_case, genai_plugin)

        schema = anthropic_api.request["body"]["output_config"]["format"]["schema"]
        assert schema["title"] == "CaseSignalSummary"

    def test_an_unconfigured_plugin_raises_genai_exception(
        self, session, genai_case, genai_plugin, anthropic_api
    ):
        """This call site raises rather than returning error_message -- matching
        its own "no artificial-intelligence plugin enabled" branch, which its
        Slack caller already handles."""
        from dispatch.ai.exceptions import GenAIException

        genai_plugin.instance.configuration = None

        with pytest.raises(GenAIException):
            self.call(session, genai_case, genai_plugin)


class TestPromptSizing:
    """The service's *own* use of the configured model.

    Every other test here asserts the model on the request, and the *plugin*
    puts it there -- so they would all still pass if `get_genai_model` returned
    a hardcoded name. The service's only use of it is `prepare_prompt_for_model`,
    and that is only observable in how much of the prompt survives.

    It also pins the state of `get_model_token_limit` for Claude, which is worse
    than it looks. `tiktoken` has no encoding for any Claude id, so every request
    falls back to `o200k_base` with a warning and counts Claude tokens with
    OpenAI's tokeniser. The limit table names two Claude models, both since
    retired; every current one -- including this plugin's own default,
    `claude-opus-5` -- misses and takes the 128k default. The effective budget is
    therefore 121,600 tokens against a 1M-token context window.

    That is safe (it truncates early, it never oversends) and it is *not* fixed
    here: issue #79 says not to change prompt preparation merely because the
    provider differs, and a real fix needs Anthropic's own token counting rather
    than more rows in a hand-written table. These tests exist so the behaviour is
    recorded rather than assumed, and so the follow-up has a failing-by-design
    starting point.
    """

    @pytest.fixture
    def conversation_plugin(self):
        # ~130k tokens: above the 121,600 default budget, below the 190,000 of
        # the mapped Claude entry.
        conv = Mock()
        conv.instance.get_conversation.return_value = "lorem " * 130_000
        return conv

    def sent_tokens(self, anthropic_api):
        import tiktoken

        return len(tiktoken.get_encoding("o200k_base").encode(sent(anthropic_api.request)["user"]))

    @pytest.mark.parametrize(
        "configured_model, truncated",
        [
            # In `get_model_token_limit` -- 200,000 * 0.95.
            ("claude-3-5-sonnet-20241022", False),
            # Not in it. This is the plugin's own default, and it is the case
            # that documents the gap.
            ("claude-opus-5", True),
        ],
    )
    def test_the_configured_model_decides_the_prompt_budget(
        self,
        session,
        conversation_plugin,
        genai_plugin,
        anthropic_api,
        project,
        configured_model,
        truncated,
    ):
        from tests.plugins.dispatch_anthropic.fake_anthropic import build_configuration

        genai_plugin.instance.configuration = build_configuration(model=configured_model)
        anthropic_api.respond_with_object({"timeline": [], "actions_taken": []})

        TestReadInSummary().call(session, conversation_plugin, genai_plugin, project)

        default_budget = 121_600
        assert (self.sent_tokens(anthropic_api) <= default_budget) is truncated

    def test_a_claude_model_name_does_not_break_prompt_preparation(self):
        """The one thing that would have been fatal: `prepare_prompt_for_model`
        calls `tiktoken.encoding_for_model`, which raises KeyError for every
        Claude id. It is caught and falls back, so this is a warning path rather
        than a crash -- but nothing else in the suite would notice if it stopped
        being."""
        from dispatch.ai.service import prepare_prompt_for_model

        assert prepare_prompt_for_model("a short prompt", "claude-opus-5") == "a short prompt"

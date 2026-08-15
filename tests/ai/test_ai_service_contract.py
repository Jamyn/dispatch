"""The ai service driven against the real OpenAI plugin (issue #75).

``test_ai_service.py`` replaces the whole plugin with a ``Mock``, which is why
three years of a broken plugin passed CI: a ``Mock`` answers
``configuration.chat_completion_model`` and swallows a ``system_message=``
kwarg the real implementation never accepted. Here the only thing faked is
OpenAI's HTTP endpoint, so the service, the plugin, the configuration schema and
the openai SDK all run for real:

    real ai service -> real OpenAIPlugin -> real OpenAIConfiguration -> fake API

One test per GenAI call site in ``dispatch/ai/service.py``, because each reads
the model out of the plugin configuration independently and each was broken
independently.
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
from tests.plugins.dispatch_openai.fake_openai import API_KEY, CONFIGURED_MODEL


@pytest.fixture
def genai_plugin(openai_plugin):
    """A plugin *record* wrapping the real plugin.

    ``PluginInstance.instance`` is a property that hands back the plugin object
    with its configuration attached, which is exactly what this stands in for.
    The plugin itself is not faked -- that is the whole point of this module.
    """
    return SimpleNamespace(instance=openai_plugin)


def only_conversation(mock_conv_plugin, genai_plugin):
    """A ``get_active_instance`` side effect serving the AI and conversation plugins."""

    def side_effect(db_session, plugin_type, project_id):
        if plugin_type == "artificial-intelligence":
            return genai_plugin
        if plugin_type == "conversation":
            return mock_conv_plugin
        return None

    return side_effect


def messages(request):
    return {m["role"]: m["content"] for m in request["body"]["messages"]}


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
        self, session, conversation_plugin, genai_plugin, openai_api, project
    ):
        openai_api.respond_with_object(
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
        self, session, conversation_plugin, genai_plugin, openai_api, project
    ):
        """Regression, defect 1: this call site read ``chat_completion_model``."""
        openai_api.respond_with_object({"timeline": [], "actions_taken": []})

        self.call(session, conversation_plugin, genai_plugin, project)

        assert openai_api.request["body"]["model"] == CONFIGURED_MODEL
        assert openai_api.request["authorization"] == f"Bearer {API_KEY}"

    def test_the_services_system_message_reaches_openai(
        self, session, conversation_plugin, genai_plugin, openai_api, project
    ):
        """Regression, defect 2: the service's system message is not merely
        accepted, it displaces the plugin's configured default."""
        from dispatch.ai.strings import READ_IN_SUMMARY_SYSTEM_MESSAGE

        openai_api.respond_with_object({"timeline": [], "actions_taken": []})

        self.call(session, conversation_plugin, genai_plugin, project)

        sent = messages(openai_api.request)["system"]
        assert sent.startswith(READ_IN_SUMMARY_SYSTEM_MESSAGE)

    def test_an_api_failure_becomes_an_error_message(
        self, session, conversation_plugin, genai_plugin, openai_api, project
    ):
        openai_api.respond_with_status(500)

        result = self.call(session, conversation_plugin, genai_plugin, project)

        assert result.summary is None
        assert result.error_message.startswith("Error generating read-in summary")

    def test_the_api_key_is_not_in_the_error_message(
        self, session, conversation_plugin, genai_plugin, openai_api, project
    ):
        """``error_message`` is posted into the case channel and returned to the
        browser, and OpenAI's 401 body quotes the submitted key back."""
        openai_api.respond_with_key_echo()

        result = self.call(session, conversation_plugin, genai_plugin, project)

        assert result.error_message
        assert API_KEY not in result.error_message

    def test_no_ai_plugin_returns_a_message_rather_than_raising(
        self, session, conversation_plugin, genai_plugin, openai_api, project
    ):
        """The most common configuration of all: the plugin is simply not
        enabled. This branch used to interpolate ``subject.name``, which a
        ``SubjectMetadata`` does not have, so it raised AttributeError instead
        of returning. Only a real subject can catch that -- the Mock in
        ``test_ai_service.py`` answers ``.name`` happily."""
        result = self.call(session, conversation_plugin, None, project)

        assert result.summary is None
        assert "No artificial-intelligence plugin enabled" in result.error_message
        assert openai_api.requests == []

    def test_an_unconfigured_plugin_becomes_an_error_message(
        self, session, conversation_plugin, genai_plugin, openai_api, project
    ):
        """``PluginInstance.configuration`` is None for a plugin instance that
        was never configured. Reading the model through that None used to raise
        an AttributeError outside the try, i.e. a 500 rather than this."""
        genai_plugin.instance.configuration = None

        result = self.call(session, conversation_plugin, genai_plugin, project)

        assert result.summary is None
        assert "not configured" in result.error_message
        assert openai_api.requests == []


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

    def test_returns_the_parsed_report(self, session, genai_plugin, openai_api, incident, project):
        openai_api.respond_with_object(
            {"conditions": "Contained", "actions": ["rotated keys"], "needs": []}
        )

        result = self.call(session, genai_plugin, incident, project)

        assert isinstance(result, TacticalReportResponse)
        assert result.error_message is None
        assert result.tactical_report.conditions == "Contained"

    def test_uses_the_configured_model(self, session, genai_plugin, openai_api, incident, project):
        openai_api.respond_with_object({"conditions": "", "actions": [], "needs": []})

        self.call(session, genai_plugin, incident, project)

        assert openai_api.request["body"]["model"] == CONFIGURED_MODEL

    def test_the_services_system_message_reaches_openai(
        self, session, genai_plugin, openai_api, incident, project
    ):
        from dispatch.ai.strings import TACTICAL_REPORT_SYSTEM_MESSAGE

        openai_api.respond_with_object({"conditions": "", "actions": [], "needs": []})

        self.call(session, genai_plugin, incident, project)

        assert messages(openai_api.request)["system"].startswith(TACTICAL_REPORT_SYSTEM_MESSAGE)

    def test_an_unconfigured_plugin_becomes_an_error_message(
        self, session, genai_plugin, openai_api, incident, project
    ):
        genai_plugin.instance.configuration = None

        result = self.call(session, genai_plugin, incident, project)

        assert result.tactical_report is None
        assert "not configured" in result.error_message
        assert openai_api.requests == []


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
        self, session, incident, genai_plugin, openai_api
    ):
        """``Incident.summary`` is a string column and this function is annotated
        ``-> str``; returning the SDK's message object instead would fail on
        commit rather than on assignment."""
        openai_api.respond_with_text("A concise incident summary.")

        result = self.call(session, incident, genai_plugin)

        assert result == "A concise incident summary."
        assert incident.summary == "A concise incident summary."

    def test_uses_the_configured_model_and_key(self, session, incident, genai_plugin, openai_api):
        """Regression, defect 1, on the ``chat_completion`` call site."""
        openai_api.respond_with_text("A concise incident summary.")

        self.call(session, incident, genai_plugin)

        assert openai_api.request["body"]["model"] == CONFIGURED_MODEL
        assert openai_api.request["authorization"] == f"Bearer {API_KEY}"

    def test_the_services_system_message_reaches_openai(
        self, session, incident, genai_plugin, openai_api
    ):
        """Regression, defect 2, on the ``chat_completion`` call site."""
        from dispatch.ai.strings import INCIDENT_SUMMARY_SYSTEM_MESSAGE

        openai_api.respond_with_text("A concise incident summary.")

        self.call(session, incident, genai_plugin)

        assert messages(openai_api.request)["system"] == INCIDENT_SUMMARY_SYSTEM_MESSAGE

    def test_the_review_document_reaches_the_prompt(
        self, session, incident, genai_plugin, openai_api
    ):
        openai_api.respond_with_text("A concise incident summary.")

        self.call(session, incident, genai_plugin)

        assert "The post-incident review document text." in messages(openai_api.request)["user"]

    def test_an_api_failure_does_not_store_a_summary(
        self, session, incident, genai_plugin, openai_api
    ):
        openai_api.respond_with_status(500)

        result = self.call(session, incident, genai_plugin)

        # The request must have been issued: a contract error before the call
        # would produce the same message and prove nothing.
        assert openai_api.requests
        assert result == "Incident summary not generated. An error occurred."
        assert incident.summary is None

    def test_an_unconfigured_plugin_does_not_call_openai(
        self, session, incident, genai_plugin, openai_api
    ):
        genai_plugin.instance.configuration = None

        result = self.call(session, incident, genai_plugin)

        assert "not configured" in result
        assert incident.summary is None
        assert openai_api.requests == []


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
        self, session, taggable_case, genai_plugin, openai_api
    ):
        openai_api.respond_with_object({"recommendations": []})

        result = self.call(session, taggable_case, genai_plugin)

        assert isinstance(result, TagRecommendationResponse)
        assert result.error_message is None

    def test_uses_the_configured_model_and_key(
        self, session, taggable_case, genai_plugin, openai_api
    ):
        """Regression, defect 1, on the tag-recommendation call site."""
        openai_api.respond_with_object({"recommendations": []})

        self.call(session, taggable_case, genai_plugin)

        assert openai_api.request["body"]["model"] == CONFIGURED_MODEL
        assert openai_api.request["authorization"] == f"Bearer {API_KEY}"

    def test_the_services_system_message_reaches_openai(
        self, session, taggable_case, genai_plugin, openai_api
    ):
        from dispatch.ai.strings import TAG_RECOMMENDATION_SYSTEM_MESSAGE

        openai_api.respond_with_object({"recommendations": []})

        self.call(session, taggable_case, genai_plugin)

        assert messages(openai_api.request)["system"] == TAG_RECOMMENDATION_SYSTEM_MESSAGE

    def test_an_api_failure_becomes_an_error_message(
        self, session, taggable_case, genai_plugin, openai_api
    ):
        openai_api.respond_with_status(500)

        result = self.call(session, taggable_case, genai_plugin)

        assert openai_api.requests
        assert result.recommendations == []
        assert result.error_message

    def test_an_unconfigured_plugin_becomes_an_error_message(
        self, session, taggable_case, genai_plugin, openai_api
    ):
        """`tag/views.py` returns this straight to FastAPI, so an AttributeError
        here is an HTTP 500 that bypasses the error_message channel."""
        genai_plugin.instance.configuration = None

        result = self.call(session, taggable_case, genai_plugin)

        assert result.recommendations == []
        assert "not configured" in result.error_message
        assert openai_api.requests == []


class TestCaseSignalSummary:
    """``generate_case_signal_summary`` -- the signal-analysis call site."""

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

    def test_returns_the_parsed_summary(self, session, genai_case, genai_plugin, openai_api):
        openai_api.respond_with_object({"summary": "Benign, matches the runbook."})

        result = self.call(session, genai_case, genai_plugin)

        assert isinstance(result, CaseSignalSummaryResponse)
        assert result.error_message is None

    def test_uses_the_configured_model_and_key(self, session, genai_case, genai_plugin, openai_api):
        """Regression, defect 1, on the signal-analysis call site -- the one the
        issue reports as reachable from the Slack case flow."""
        openai_api.respond_with_object({"summary": "Benign."})

        self.call(session, genai_case, genai_plugin)

        assert openai_api.request["body"]["model"] == CONFIGURED_MODEL
        assert openai_api.request["authorization"] == f"Bearer {API_KEY}"

    def test_the_signals_system_message_reaches_openai(
        self, session, genai_case, genai_plugin, openai_api
    ):
        """Regression, defect 2: the per-signal system message is the reason
        ``system_message`` exists on the plugin interface at all."""
        openai_api.respond_with_object({"summary": "Benign."})

        self.call(session, genai_case, genai_plugin)

        assert messages(openai_api.request)["system"] == "You are a detection analyst."

    def test_the_signals_prompt_reaches_openai(self, session, genai_case, genai_plugin, openai_api):
        openai_api.respond_with_object({"summary": "Benign."})

        self.call(session, genai_case, genai_plugin)

        assert "Analyze this detection." in messages(openai_api.request)["user"]

    def test_an_unconfigured_plugin_raises_genai_exception(
        self, session, genai_case, genai_plugin, openai_api
    ):
        """This call site raises rather than returning error_message -- matching
        its own "no artificial-intelligence plugin enabled" branch, which its
        Slack caller already handles."""
        from dispatch.ai.exceptions import GenAIException

        genai_plugin.instance.configuration = None

        with pytest.raises(GenAIException):
            self.call(session, genai_case, genai_plugin)

        assert openai_api.requests == []


class TestPromptSizing:
    """The service's *own* use of the configured model.

    Every other test here asserts the model on the request, and the plugin puts
    it there -- so they would all still pass if `get_genai_model` returned a
    hardcoded name (verified by mutation: they do). The service's only use of it
    is `prepare_prompt_for_model`, and that is only observable in how much of
    the prompt survives, which is what these two pin.

    The pair is what makes it work: `gpt-4o` and the unknown-model default share
    a 128k limit, so only a model with a *different* mapped limit can tell the
    configured value apart from a fallback.
    """

    @pytest.fixture
    def conversation_plugin(self):
        # ~130k tokens: above gpt-4o's 121,600 effective limit, below the
        # 190,000 of the Claude entry in `get_model_token_limit`.
        conv = Mock()
        conv.instance.get_conversation.return_value = "lorem " * 130_000
        return conv

    def sent_tokens(self, openai_api):
        import tiktoken

        user = messages(openai_api.request)["user"]
        return len(tiktoken.get_encoding("o200k_base").encode(user))

    @pytest.mark.parametrize(
        "configured_model, truncated",
        [
            ("gpt-4o", True),
            ("claude-3-5-sonnet-20241022", False),
        ],
    )
    def test_the_configured_model_decides_the_prompt_budget(
        self,
        session,
        conversation_plugin,
        genai_plugin,
        openai_api,
        project,
        configured_model,
        truncated,
    ):
        from tests.plugins.dispatch_openai.fake_openai import build_configuration

        genai_plugin.instance.configuration = build_configuration(model=configured_model)
        openai_api.respond_with_object({"timeline": [], "actions_taken": []})

        TestReadInSummary().call(session, conversation_plugin, genai_plugin, project)

        gpt_4o_limit = 121_600
        assert (self.sent_tokens(openai_api) <= gpt_4o_limit) is truncated


class TestPluginInstanceRoundTrip:
    """`model` is the *persisted* configuration key, not just a class attribute.

    Everything else here hands the service a `SimpleNamespace(instance=plugin)`,
    which skips `PluginInstance.instance` -- the property that decrypts the
    stored JSON, parses it back through the schema and attaches it. This is the
    one test that proves an operator's saved configuration survives that trip
    under the key the service reads.
    """

    @pytest.fixture
    def registered_openai_plugin(self):
        from dispatch.plugins.base import plugins, register, unregister
        from dispatch.plugins.dispatch_openai.plugin import OpenAIPlugin

        already = OpenAIPlugin.slug in [p.slug for p in plugins.all()]
        if not already:
            register(OpenAIPlugin)
        yield
        if already:
            # `PluginInstance.instance` assigns onto the registry's cached
            # singleton, so leaving the fake key on it would leak into whatever
            # runs next.
            plugins.get(OpenAIPlugin.slug).configuration = None
        else:
            unregister(OpenAIPlugin)

    def test_the_stored_configuration_reaches_the_service_as_model(
        self, session, registered_openai_plugin
    ):
        from dispatch.ai.service import get_genai_model
        from dispatch.plugins.dispatch_openai.plugin import OpenAIPlugin
        from tests.factories import PluginFactory, PluginInstanceFactory
        from tests.plugins.dispatch_openai.fake_openai import API_KEY, CONFIGURED_MODEL

        instance = PluginInstanceFactory(
            plugin=PluginFactory(slug=OpenAIPlugin.slug, type="artificial-intelligence")
        )
        # Assigned, not passed to the factory: `configuration` is a hybrid
        # property whose setter is what validates and encrypts.
        instance.configuration = {
            "api_key": API_KEY,
            "model": CONFIGURED_MODEL,
            "system_message": "You are the configured default assistant.",
        }
        session.commit()

        assert instance._configuration, "the configuration was not persisted"

        assert get_genai_model(instance) == CONFIGURED_MODEL
        assert instance.instance.configuration.api_key.get_secret_value() == API_KEY

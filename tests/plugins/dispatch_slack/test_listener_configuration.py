"""Which configuration a listener is served with (#163).

`configuration_middleware` used to resolve `context["config"]` from the default
organization's default project rather than from the organization whose
signature the request was verified against, so a listener handling one
tenant's request could run with another's configuration -- an object that
carries the bot token and signing secret, not just behavioural settings.
"""

import json
from unittest.mock import patch

import pytest
from slack_bolt.request import BoltRequest
from slack_sdk.web import SlackResponse, WebClient

import dispatch.plugins.dispatch_slack.middleware as slack_middleware
from dispatch.plugins.dispatch_slack.app import build_app
from dispatch.plugins.dispatch_slack.config import SlackConversationConfiguration
from dispatch.plugins.dispatch_slack.middleware import (
    configuration_middleware,
    subject_middleware,
)

AUTH_TEST_RESPONSE = SlackResponse(
    client=None,
    http_verb="POST",
    api_url="https://slack.com/api/auth.test",
    req_args={},
    data={
        "ok": True,
        "url": "https://example.slack.com/",
        "team": "Example",
        "user": "dispatch",
        "team_id": "T123",
        "user_id": "UBOT",
        "bot_id": "BBOT",
    },
    headers={},
    status_code=200,
)


def make_configuration(prefix: str) -> SlackConversationConfiguration:
    return SlackConversationConfiguration(
        api_bot_token=f"xoxb-{prefix}-not-real",
        signing_secret=f"{prefix}-signing-secret-not-real",
        socket_mode_app_token=f"xapp-{prefix}-not-real",
        app_user_slug=f"{prefix}-bot",
    )


class FakeSession:
    """Stands in for the session `configuration_middleware` would open on the
    default organization. Only the finalization Bolt runs afterwards touches
    it.
    """

    def __init__(self):
        self.closed = False

    def commit(self):
        pass

    def close(self):
        self.closed = True


class FakePluginInstance:
    def __init__(self, configuration):
        self.configuration = configuration


class FakeProject:
    id = 1


@pytest.fixture
def default_org_lookup(monkeypatch):
    """Make the default-organization fallback answer with the *other* tenant's
    configuration, so a listener reading it is unmistakable.
    """
    other = make_configuration("default-org")
    session = FakeSession()
    monkeypatch.setattr(slack_middleware, "get_default_org_slug", lambda: "default")
    monkeypatch.setattr(slack_middleware, "refetch_db_session", lambda slug: session)
    monkeypatch.setattr(
        slack_middleware.project_service, "get_default", lambda db_session: FakeProject()
    )
    monkeypatch.setattr(
        slack_middleware.plugin_service,
        "get_active_instance",
        lambda db_session, project_id, plugin_type: FakePluginInstance(other),
    )
    return other, session


def block_actions_body(action_id: str) -> str:
    return json.dumps(
        {
            "type": "block_actions",
            "team": {"id": "T123"},
            "user": {"id": "U123"},
            "api_app_id": "A123",
            "trigger_id": "TRIGGER",
            "channel": {"id": "C123"},
            "container": {"type": "message", "message_ts": "1.0"},
            "actions": [
                {"type": "button", "action_id": action_id, "block_id": "B1", "action_ts": "1.0"}
            ],
        }
    )


def app_recording_listener_context(configuration, seen):
    """An app for `configuration`, with one listener carrying the same
    middleware chain the real commands that have no `db_middleware` use.
    """
    app = build_app(configuration)
    # Action listeners otherwise run on a worker thread and dispatch would
    # return before the listener recorded anything.
    app.listener_runner.process_before_response = True

    @app.action("record_context", middleware=[subject_middleware, configuration_middleware])
    def record(ack, context):
        seen["config"] = context.get("config")
        seen["db_session"] = context.get("db_session")
        ack()

    return app


def test_a_listener_is_served_the_configuration_its_request_was_verified_against(
    default_org_lookup,
):
    """Given a request verified against one configuration, when a listener
    runs, then it reads that configuration and not the default organization's.
    """
    other, _ = default_org_lookup
    verified = make_configuration("verified-tenant")
    seen = {}
    app = app_recording_listener_context(verified, seen)

    request = BoltRequest(body=f"payload={block_actions_body('record_context')}", mode="http")
    with patch.object(WebClient, "auth_test", return_value=AUTH_TEST_RESPONSE):
        response = app.dispatch(request)

    assert response.status == 200, response.body
    assert seen["config"] is verified
    assert seen["config"] is not other


def test_the_session_a_listener_needs_survives_the_configuration_being_supplied(
    default_org_lookup,
):
    """Given the configuration no longer has to be looked up, when a listener
    runs, then it still gets the session that lookup used to leave behind.

    `[subject_middleware, configuration_middleware]` is a real chain -- the
    report-incident and list-incidents commands use it -- and both listeners
    take `db_session` from the context with no `db_middleware` to open it.
    """
    _, session = default_org_lookup
    seen = {}
    app = app_recording_listener_context(make_configuration("verified-tenant"), seen)

    request = BoltRequest(body=f"payload={block_actions_body('record_context')}", mode="http")
    with patch.object(WebClient, "auth_test", return_value=AUTH_TEST_RESPONSE):
        app.dispatch(request)

    assert seen["db_session"] is session


def test_socket_mode_requests_are_configured_the_same_way(default_org_lookup):
    """Socket mode has no HTTP request to hang a configuration off, so the
    seeding has to belong to the App rather than to the HTTP adapter.
    """
    other, _ = default_org_lookup
    verified = make_configuration("verified-tenant")
    seen = {}
    app = app_recording_listener_context(verified, seen)

    request = BoltRequest(
        body=f"payload={block_actions_body('record_context')}", mode="socket_mode"
    )
    with patch.object(WebClient, "auth_test", return_value=AUTH_TEST_RESPONSE):
        app.dispatch(request)

    assert seen["config"] is verified
    assert seen["config"] is not other

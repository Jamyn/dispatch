"""Regression coverage for #143.

`get_request_handler` used to call the tenant `configure()` functions on
every Slack request. Bolt's registration appends rather than replaces, so the
listener list grew by roughly 24 entries per request, forever, for the life
of the process -- unbounded memory growth and an ever-slower linear scan on
every dispatch. The fix (#162) moved configuration onto a per-tenant `App`
built once and cached by `get_app`; these tests pin that behaviour at the
boundary that actually broke, `get_request_handler` itself, rather than only
at `get_app`.

Also covers the related half of #143: `slack_actions` was `async def` while
`handler.handle` is fully synchronous and Bolt polls for the listener's ack
in a blocking sleep loop, so an async route blocks the event loop for the
duration of every action.
"""

import asyncio
import inspect
import json
import threading
import time
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from slack_bolt import App
from slack_sdk.signature import SignatureVerifier
from slack_sdk.web import SlackResponse, WebClient
from starlette.requests import Request

from dispatch.plugins.dispatch_slack import app as slack_app
from dispatch.plugins.dispatch_slack import endpoints as slack_endpoints
from dispatch.plugins.dispatch_slack.config import SlackConversationConfiguration

SIGNING_SECRET = "not-a-real-signing-secret-tests-only"

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


def make_configuration() -> SlackConversationConfiguration:
    return SlackConversationConfiguration(
        api_bot_token="xoxb-valid",
        signing_secret=SIGNING_SECRET,
        socket_mode_app_token="xapp-not-real-tests-only",
        app_user_slug="dispatch",
    )


class FakePluginInstance:
    """Stands in for the row `get_request_handler` selects after verifying the
    request signature. Only `id` and `configuration` are reached from there.
    """

    def __init__(self, instance_id: int, configuration: SlackConversationConfiguration):
        self.id = instance_id
        self.configuration = configuration


class FakeSession:
    """Stands in for the session `get_request_handler` queries `PluginInstance`
    from. Always answers with the instances it was given -- the filtering
    Postgres would do at the SQL layer is not what this suite exercises.
    """

    def __init__(self, instances):
        self._instances = instances
        self.closed = False

    def query(self, *args, **kwargs):
        return self

    def join(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._instances)

    def close(self):
        self.closed = True


def signed_request(
    body: bytes, path: str = "/default/slack/action", content_type: str = "application/json"
) -> Request:
    """A real Starlette `Request`, signed the way Slack signs its own."""
    timestamp = str(int(time.time()))
    signature = SignatureVerifier(SIGNING_SECRET).generate_signature(timestamp=timestamp, body=body)

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (b"x-slack-request-timestamp", timestamp.encode()),
            (b"x-slack-signature", signature.encode()),
            (b"content-type", content_type.encode()),
        ],
        "server": ("testserver", 80),
        "client": ("testclient", 123),
        "scheme": "http",
    }
    return Request(scope, receive)


def block_actions_body(action_id: str) -> bytes:
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
    ).encode()


@pytest.fixture
def isolated_app_cache(monkeypatch):
    """Give each test its own App cache, so ordering cannot leak between them."""
    monkeypatch.setattr(slack_app, "_apps", {})
    monkeypatch.setattr(slack_app, "_apps_lock", threading.Lock())


@pytest.fixture
def fake_plugin_lookup(monkeypatch):
    """Point `get_request_handler`'s DB lookup at a fake tenant instead of a
    real organization schema."""
    configuration = make_configuration()
    plugin_instance = FakePluginInstance(1, configuration)
    monkeypatch.setattr(
        slack_endpoints, "refetch_db_session", lambda organization: FakeSession([plugin_instance])
    )
    return configuration


def test_slack_actions_is_not_a_coroutine_function():
    """`handler.handle` is fully synchronous and Bolt polls for the listener's
    ack in a blocking sleep loop. An `async def` route runs that poll on the
    event loop and blocks every other request for its duration -- `slack_menus`
    was already fixed this way for the same reason (#86); `slack_actions` gets
    the same treatment here.
    """
    assert not inspect.iscoroutinefunction(slack_endpoints.slack_actions)
    assert not inspect.iscoroutinefunction(slack_endpoints.slack_menus)


def test_get_request_handler_reaches_a_real_listener_on_every_request(
    isolated_app_cache, fake_plugin_lookup, monkeypatch
):
    """A handler from `get_request_handler` must still deliver every request to
    a matching listener, and exactly once per request -- zero would mean the
    request never reached it, and more than one is what duplicate registration
    looks like from the listener's side.
    """
    received = []

    test_app = App(
        token="xoxb-valid",
        signing_secret=SIGNING_SECRET,
        request_verification_enabled=False,
        token_verification_enabled=False,
    )

    @test_app.action("test_action")
    def handle_test_action(ack, body):
        received.append(body)
        ack()

    # Action listeners normally run on a worker thread, so dispatch would
    # return before the handler above had done anything to assert on.
    test_app._listener_runner.process_before_response = True

    monkeypatch.setattr(slack_endpoints, "get_app", lambda organization, plugin_instance: test_app)

    baseline = len(test_app._listeners)

    with patch.object(WebClient, "auth_test", return_value=AUTH_TEST_RESPONSE):
        for _ in range(5):
            body = block_actions_body("test_action")
            request = signed_request(body)
            handler, _ = slack_endpoints.get_request_handler(
                request=request, body=body, organization="org-a"
            )
            response = handler.handle(req=request, body=body)
            assert response.status_code == 200, response.body

    assert len(received) == 5, "not every request reached the listener exactly once"
    assert len(test_app._listeners) == baseline, "dispatching requests must not register listeners"


def test_get_request_handler_does_not_grow_listeners_across_many_requests(
    isolated_app_cache, fake_plugin_lookup
):
    """The exact regression from #143, reproduced at `get_request_handler`
    rather than at `get_app`: processing many requests for one tenant must
    neither grow the listener list nor rebuild the App.
    """
    configuration = fake_plugin_lookup

    handlers = []
    for _ in range(10):
        body = block_actions_body("whatever")
        request = signed_request(body)
        handler, returned_config = slack_endpoints.get_request_handler(
            request=request, body=body, organization="org-a"
        )
        handlers.append(handler)
        assert returned_config is configuration, "configuration is not accidentally skipped"

    listener_counts = {len(h.app._listeners) for h in handlers}
    assert len(listener_counts) == 1, f"listener count changed across requests: {listener_counts}"

    # configure() must actually have run once -- not merely have failed to grow.
    assert len(handlers[0].app._listeners) > len(slack_app.listeners._registrations)

    # Every request after the first must reuse the same App, not just one with
    # the same listener count.
    assert all(h.app is handlers[0].app for h in handlers)


def test_all_slack_routes_still_work_across_repeated_requests(
    isolated_app_cache, fake_plugin_lookup
):
    """Event, command, action and menu requests all go through
    `get_request_handler` -- a fix scoped to only one of the four routes would
    leave the others still growing listeners.
    """
    configuration = fake_plugin_lookup

    for _ in range(3):
        event_body = json.dumps({"type": "url_verification", "challenge": "abc123"}).encode()
        event_request = signed_request(event_body, path="/default/slack/event")
        event_response = asyncio.run(
            slack_endpoints.slack_events(
                request=event_request, organization="org-a", body=event_body
            )
        )
        assert event_response.status_code == 200

        command_body = urlencode(
            {
                "command": configuration.slack_command_create_case,
                "text": "",
                "user_id": "U1",
                "channel_id": "C1",
            }
        ).encode()
        command_request = signed_request(
            command_body,
            path="/default/slack/command",
            content_type="application/x-www-form-urlencoded",
        )
        command_response = asyncio.run(
            slack_endpoints.slack_commands(
                organization="org-a", request=command_request, body=command_body
            )
        )
        assert command_response.status_code == 200

        action_body = block_actions_body("whatever")
        action_request = signed_request(action_body, path="/default/slack/action")
        action_response = slack_endpoints.slack_actions(
            request=action_request, organization="org-a", body=action_body
        )
        assert action_response.status_code in (200, 404)

        menu_body = block_actions_body("whatever")
        menu_request = signed_request(menu_body, path="/default/slack/menu")
        menu_response = slack_endpoints.slack_menus(
            request=menu_request, organization="org-a", body=menu_body
        )
        assert menu_response.status_code in (200, 404)

    handler, _ = slack_endpoints.get_request_handler(
        request=signed_request(block_actions_body("whatever")),
        body=block_actions_body("whatever"),
        organization="org-a",
    )
    assert len(handler.app._listeners) == len(
        slack_app.get_app("org-a", FakePluginInstance(1, configuration))._listeners
    )

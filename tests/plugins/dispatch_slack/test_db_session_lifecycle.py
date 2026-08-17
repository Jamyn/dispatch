"""Regression coverage for #139: db_middleware's session must stay open for
the actual listener execution, and be closed only after -- on both the
success and the failure path.

Bolt runs a listener's middleware to completion before the listener body
executes: the `next()` a middleware calls only sets a flag, it does not
invoke the listener. So `db_middleware` can only *open* the session;
closing it is `build_app`'s job on success
(`middleware.finalize_listener_db_session_on_success`, wired as the app's
`listener_completion_handler`) and `bolt.app_error_handler`'s job on failure
(`middleware.finalize_listener_db_session_on_error`). These tests drive a
real Bolt App/dispatch cycle -- including that wiring -- rather than calling
either hook directly, since the bug was entirely about whether they run at
the right time relative to the listener body; a unit test of either hook in
isolation would not have caught it.
"""

import json
from unittest.mock import patch

import pytest
from slack_bolt.request import BoltRequest
from slack_sdk.web import SlackResponse, WebClient

import dispatch.plugins.dispatch_slack.middleware as slack_middleware
from dispatch.database.logging import SessionTracker
from dispatch.plugins.dispatch_slack.app import build_app
from dispatch.plugins.dispatch_slack.config import SlackConversationConfiguration
from dispatch.plugins.dispatch_slack.fields import DefaultActionIds

ACTION_ID = "test-db-session-lifecycle"


def _action_payload(action_id: str = ACTION_ID) -> dict:
    return {
        "type": "block_actions",
        "team": {"id": "T123"},
        "user": {"id": "U123"},
        "api_app_id": "A123",
        "trigger_id": "TRIGGER",
        "container": {"type": "message"},
        "channel": {"id": "C123"},
        "actions": [
            {
                "type": "button",
                "action_id": action_id,
                "block_id": "B1",
                "action_ts": "1.0",
            }
        ],
    }


@pytest.fixture
def lifecycle_app():
    """A real Bolt app, built the production way, that a test can register
    one extra test-only listener onto.

    `db_middleware` and the app-wide completion/error handling are exactly
    `build_app`'s production wiring; only the listener body -- which drives
    success/failure and observes the session mid-execution -- is test-only.
    Registering directly on the built `App` (rather than through
    `dispatch_slack.bolt.listeners`) sidesteps that registry's "closed after
    first build" guard, which exists to stop tenants disagreeing about which
    listeners exist and doesn't apply to a throwaway per-test App.
    """
    app = build_app(
        SlackConversationConfiguration(
            api_bot_token="xoxb-valid",
            signing_secret="not-a-real-signing-secret-tests-only",
            socket_mode_app_token="xapp-not-real-tests-only",
            app_user_slug="dispatch",
        )
    )
    # Action listeners normally run on a worker thread; this suite asserts on
    # state the listener body observes, so it has to run inline instead.
    app._listener_runner.process_before_response = True

    auth_test = SlackResponse(
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

    with patch.object(WebClient, "auth_test", return_value=auth_test):
        yield app


@pytest.fixture
def recorded_sessions():
    """Every session `db_middleware` opens, instrumented to record the order
    its commit/rollback/close are called in.

    Shadows only those three bound methods on the instance -- the object
    handed to the listener is still a genuine `Session`, so production code
    downstream behaves exactly as it would outside the test.
    """
    sessions: list = []
    real_refetch = slack_middleware.refetch_db_session

    def record(slug: str):
        db_session = real_refetch(slug)
        db_session.events = []
        real_commit, real_rollback, real_close = (
            db_session.commit,
            db_session.rollback,
            db_session.close,
        )

        def commit():
            db_session.events.append("commit")
            return real_commit()

        def rollback():
            db_session.events.append("rollback")
            return real_rollback()

        def close():
            db_session.events.append("close")
            return real_close()

        db_session.commit, db_session.rollback, db_session.close = commit, rollback, close
        sessions.append(db_session)
        return db_session

    with patch.object(slack_middleware, "refetch_db_session", record):
        yield sessions


def test_session_is_open_and_untouched_when_the_listener_runs(
    session, lifecycle_app, recorded_sessions
):
    """The exact defect #139 describes: the session must not already be
    committed or closed by the time the listener body touches it.
    """
    events_at_call_time = []

    @lifecycle_app.action(ACTION_ID, middleware=[slack_middleware.db_middleware])
    def _listener(ack, db_session) -> None:
        events_at_call_time.append(list(recorded_sessions[-1].events))
        ack()

    response = lifecycle_app.dispatch(BoltRequest(body=_action_payload(), mode="socket_mode"))

    assert response.status == 200, response.body
    assert events_at_call_time == [[]], "session was committed or closed before the listener ran"


def test_successful_listener_execution_commits_then_closes(
    session, lifecycle_app, recorded_sessions
):
    @lifecycle_app.action(ACTION_ID, middleware=[slack_middleware.db_middleware])
    def _listener(ack, db_session) -> None:
        ack()

    response = lifecycle_app.dispatch(BoltRequest(body=_action_payload(), mode="socket_mode"))

    assert response.status == 200, response.body
    assert len(recorded_sessions) == 1
    assert recorded_sessions[0].events == ["commit", "close"]


def test_failed_listener_execution_rolls_back_then_closes(
    session, lifecycle_app, recorded_sessions
):
    @lifecycle_app.action(ACTION_ID, middleware=[slack_middleware.db_middleware])
    def _listener(ack, db_session) -> None:
        raise RuntimeError("boom")

    lifecycle_app.dispatch(BoltRequest(body=_action_payload(), mode="socket_mode"))

    assert len(recorded_sessions) == 1
    assert recorded_sessions[0].events == ["rollback", "close"]


def test_middleware_raised_after_db_middleware_still_rolls_back_and_closes(
    session, lifecycle_app, recorded_sessions
):
    """A listener *middleware* raising (e.g. `restricted_command_middleware`
    or `user_middleware`) never reaches the listener body or Bolt's
    `listener_completion_handler` -- only the app-wide error handler does.
    """

    def _explode(context, next):
        raise RuntimeError("middleware boom")

    @lifecycle_app.action(ACTION_ID, middleware=[slack_middleware.db_middleware, _explode])
    def _listener(ack, db_session) -> None:  # pragma: no cover - never reached
        ack()

    lifecycle_app.dispatch(BoltRequest(body=_action_payload(), mode="socket_mode"))

    assert len(recorded_sessions) == 1
    assert recorded_sessions[0].events == ["rollback", "close"]


def test_session_is_tracked_during_the_listener_and_untracked_after(
    session, lifecycle_app, recorded_sessions
):
    tracked_during_listener = []

    @lifecycle_app.action(ACTION_ID, middleware=[slack_middleware.db_middleware])
    def _listener(ack, db_session) -> None:
        active_ids = {s["session_id"] for s in SessionTracker.get_active_sessions()}
        tracked_during_listener.append(db_session._dispatch_session_id in active_ids)
        ack()

    lifecycle_app.dispatch(BoltRequest(body=_action_payload(), mode="socket_mode"))

    assert tracked_during_listener == [True], "session was untracked before the listener ran"

    active_ids_after = {s["session_id"] for s in SessionTracker.get_active_sessions()}
    assert recorded_sessions[0]._dispatch_session_id not in active_ids_after, (
        "session was never untracked once the listener finished"
    )


def test_options_request_does_not_leak_the_session(
    session, dispatch_interaction, recorded_sessions, only_projects
):
    """Options requests fire once per keystroke on the project type-ahead
    (#86); each one must close its session or the pool exhausts under normal
    typing traffic. Drives the real production options listener rather than
    a test-only one.
    """
    only_projects(3)
    payload = {
        "type": "block_suggestion",
        "team": {"id": "T123", "domain": "example"},
        "user": {"id": "U123", "name": "someone"},
        "api_app_id": "A123",
        "token": "verification-token",
        "container": {"type": "view", "view_id": "V123"},
        "action_id": DefaultActionIds.project_select,
        "block_id": "project-select",
        "value": "",
        "view": {
            "id": "V123",
            "type": "modal",
            "private_metadata": json.dumps(
                {"type": "case", "organization_slug": "default", "channel_id": "C123"}
            ),
            "state": {"values": {}},
        },
    }

    response = dispatch_interaction(payload)

    assert response.status == 200, response.body
    assert len(recorded_sessions) == 1
    assert recorded_sessions[0].events == ["commit", "close"]

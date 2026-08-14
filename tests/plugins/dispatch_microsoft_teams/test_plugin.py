"""Behavioural tests for the Microsoft Teams conference plugin.

The Zoom plugin is the parity baseline: same return shape, same subject
fallback, same instrumentation, same interface surface.
"""

import pytest

from dispatch.exceptions import DispatchPluginException

from tests.plugins.dispatch_microsoft_teams.graph_fake import (
    JOIN_URL,
    MEETING_ID,
    USER_ID,
)


# --- the contract conference/flows.py depends on ----------------------------


def test_create_returns_the_weblink_id_and_challenge(graph, teams_plugin):
    conference = teams_plugin.create("dispatch-incident-1")

    assert conference["weblink"] == JOIN_URL
    assert conference["id"] == MEETING_ID
    assert conference["challenge"] == "aB3dEf7h"


def test_create_returns_every_key_the_conference_flow_reads(graph, teams_plugin):
    """``conference/flows.py`` subscripts all three without a default."""
    conference = teams_plugin.create("dispatch-incident-1")

    assert set(conference) >= {"weblink", "id", "challenge"}


def test_the_challenge_is_empty_when_no_passcode_is_required(graph, teams_plugin):
    teams_plugin.configuration.require_passcode = False
    graph.meeting = (201, {"id": MEETING_ID, "joinWebUrl": JOIN_URL}, {})

    assert teams_plugin.create("dispatch-incident-1")["challenge"] == ""


def test_a_meeting_without_a_join_url_raises_rather_than_returning_a_partial(graph, teams_plugin):
    graph.meeting = (201, {"id": MEETING_ID}, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        teams_plugin.create("dispatch-incident-1")

    assert "joinWebUrl" in str(excinfo.value)


# --- subject construction (Zoom parity) -------------------------------------


def test_the_title_becomes_the_meeting_subject(graph, teams_plugin):
    teams_plugin.create("dispatch-incident-1", title="Payments API degraded")

    assert graph.last_graph_request().json["subject"] == "Payments API degraded"


def test_the_subject_falls_back_to_a_situation_room_when_no_title_is_given(graph, teams_plugin):
    """Matches ``create_meeting`` in the Zoom plugin."""
    teams_plugin.create("dispatch-incident-1")

    assert graph.last_graph_request().json["subject"] == "Situation Room for dispatch-incident-1"


# --- configuration is actually honoured -------------------------------------


def test_the_configured_duration_sets_the_meeting_window(graph, teams_plugin):
    from datetime import datetime

    teams_plugin.configuration.default_duration_minutes = 1440
    teams_plugin.create("dispatch-incident-1")

    body = graph.last_graph_request().json
    start = datetime.fromisoformat(body["startDateTime"])
    end = datetime.fromisoformat(body["endDateTime"])
    assert (end - start).total_seconds() == 1440 * 60


def test_auto_recording_is_off_unless_configured(graph, teams_plugin):
    teams_plugin.create("dispatch-incident-1")

    assert graph.last_graph_request().json["recordAutomatically"] is False


def test_auto_recording_is_sent_when_configured(graph, teams_plugin):
    teams_plugin.configuration.allow_auto_recording = True
    teams_plugin.create("dispatch-incident-1")

    assert graph.last_graph_request().json["recordAutomatically"] is True


def test_a_passcode_is_requested_by_default(graph, teams_plugin):
    """Zoom always password-protects its bridge; Teams now matches by default."""
    teams_plugin.create("dispatch-incident-1")

    settings = graph.last_graph_request().json["joinMeetingIdSettings"]
    assert settings["isPasscodeRequired"] is True


def test_the_meeting_is_created_for_the_configured_user(graph, teams_plugin):
    teams_plugin.create("dispatch-incident-1")

    assert f"/users/{USER_ID}/onlineMeetings" in graph.last_graph_request().url


# --- failures reach the caller ----------------------------------------------


def test_a_graph_failure_propagates_instead_of_returning_none(graph, teams_plugin):
    """The old broad ``except`` returned ``None`` and lost the reason."""
    graph.meeting = (403, {"error": {"code": "Forbidden", "message": "No access policy."}}, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        teams_plugin.create("dispatch-incident-1")

    assert "No access policy." in str(excinfo.value)


def test_an_authentication_failure_propagates(graph, teams_plugin):
    graph.token = (401, {"error": "invalid_client", "error_description": "Bad secret."}, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        teams_plugin.create("dispatch-incident-1")

    assert "invalid_client" in str(excinfo.value)


# --- interface parity with Zoom ---------------------------------------------


def test_delete_removes_the_meeting(graph, teams_plugin):
    teams_plugin.delete(MEETING_ID)

    request = graph.last_graph_request()
    assert request.method == "DELETE"
    assert request.url.endswith(f"/onlineMeetings/{MEETING_ID}")


@pytest.mark.parametrize("method", ["create", "delete", "add_participant", "remove_participant"])
def test_the_plugin_implements_the_same_surface_as_zoom(method):
    from dispatch.plugins.dispatch_microsoft_teams.conference.plugin import (
        MicrosoftTeamsConferencePlugin,
    )
    from dispatch.plugins.dispatch_zoom.plugin import ZoomConferencePlugin

    assert hasattr(ZoomConferencePlugin, method), "baseline changed; update this test"
    assert hasattr(MicrosoftTeamsConferencePlugin, method)


def test_participant_management_is_a_no_op_like_zoom(graph, teams_plugin):
    """Neither platform's plugin manages participants; say so rather than crash."""
    assert teams_plugin.add_participant(MEETING_ID, "someone@example.com") is None
    assert teams_plugin.remove_participant(MEETING_ID, "someone@example.com") is None
    assert graph.graph_requests() == []


# --- instrumentation --------------------------------------------------------


class RecordingMetrics:
    def __init__(self):
        self.counters = []
        self.timers = []

    def counter(self, name, value=None, tags=None):
        self.counters.append((name, tags))

    def timer(self, name, value=None, tags=None):
        self.timers.append((name, tags))

    def gauge(self, name, value=None, tags=None):
        pass


@pytest.fixture
def metrics(monkeypatch):
    recorder = RecordingMetrics()
    monkeypatch.setattr("dispatch.decorators.metrics_provider", recorder)
    return recorder


def test_create_emits_a_call_counter_and_a_timer(graph, teams_plugin, metrics):
    """Regression guard for the ``apply`` misuse in issue #81.

    ``apply`` is a *class* decorator: it rewrites the entries in ``cls.__dict__``.
    Applied to a method instead, it iterates that function's empty ``__dict__``,
    returns it untouched, and no metric is ever emitted -- silently.
    """
    teams_plugin.create("dispatch-incident-1")

    counted = [tags["function"] for _, tags in metrics.counters]
    timed = [tags["function"] for _, tags in metrics.timers]

    assert any("MicrosoftTeamsConferencePlugin.create" in name for name in counted)
    assert any("MicrosoftTeamsConferencePlugin.create" in name for name in timed)


def test_delete_is_instrumented_too(graph, teams_plugin, metrics):
    teams_plugin.delete(MEETING_ID)

    counted = [tags["function"] for _, tags in metrics.counters]
    assert any("MicrosoftTeamsConferencePlugin.delete" in name for name in counted)


def test_the_constructor_is_not_instrumented(teams_plugin, metrics):
    from dispatch.plugins.dispatch_microsoft_teams.conference.plugin import (
        MicrosoftTeamsConferencePlugin,
    )

    MicrosoftTeamsConferencePlugin()

    assert not any("__init__" in tags["function"] for _, tags in metrics.counters)

"""Behavioural tests for the Microsoft Teams conference plugin.

The Zoom plugin is the parity baseline: same return shape, same subject
fallback, same instrumentation, same interface surface.
"""

import pytest
from pydantic import SecretStr

from dispatch.exceptions import DispatchPluginException

from tests.plugins.dispatch_microsoft_teams.graph_fake import (
    AUTHORITY,
    CLIENT_ID,
    JOIN_MEETING_ID,
    JOIN_URL,
    MEETING_ID,
    PASSCODE,
    USER_ID,
)


# --- the contract conference/flows.py depends on ----------------------------


def test_create_returns_the_weblink_id_and_challenge(graph, teams_plugin):
    conference = teams_plugin.create("dispatch-incident-1")

    assert conference["weblink"] == JOIN_URL
    assert conference["id"] == MEETING_ID
    assert conference["challenge"] == f"{PASSCODE} (meeting ID {JOIN_MEETING_ID})"


def test_create_returns_every_key_the_conference_flow_reads(graph, teams_plugin):
    """``conference/flows.py`` subscripts all three without a default."""
    conference = teams_plugin.create("dispatch-incident-1")

    assert set(conference) >= {"weblink", "id", "challenge"}


def test_the_challenge_is_empty_when_no_passcode_is_required(graph, teams_plugin):
    teams_plugin.configuration.require_passcode = False
    graph.meeting = (201, {"id": MEETING_ID, "joinWebUrl": JOIN_URL}, {})

    assert teams_plugin.create("dispatch-incident-1")["challenge"] == ""


@pytest.mark.parametrize("require_passcode", [True, False])
def test_the_passcode_setting_reaches_the_graph_request(graph, teams_plugin, require_passcode):
    """Asserting on the returned challenge alone lets the config be ignored."""
    teams_plugin.configuration.require_passcode = require_passcode
    teams_plugin.create("dispatch-incident-1")

    settings = graph.last_graph_request().json["joinMeetingIdSettings"]
    assert settings["isPasscodeRequired"] is require_passcode


def test_a_null_passcode_becomes_an_empty_challenge(graph, teams_plugin):
    """Graph sends an explicit null rather than omitting the settings object."""
    graph.meeting = (
        201,
        {
            "id": MEETING_ID,
            "joinWebUrl": JOIN_URL,
            "joinMeetingIdSettings": {"isPasscodeRequired": False, "passcode": None},
        },
        {},
    )

    assert teams_plugin.create("dispatch-incident-1")["challenge"] == ""


def test_a_null_join_meeting_id_settings_does_not_crash(graph, teams_plugin):
    """`.get(k, {})` returns the default only when the key is absent."""
    graph.meeting = (
        201,
        {"id": MEETING_ID, "joinWebUrl": JOIN_URL, "joinMeetingIdSettings": None},
        {},
    )

    assert teams_plugin.create("dispatch-incident-1")["challenge"] == ""


def test_a_passcode_without_a_meeting_id_is_returned_alone(graph, teams_plugin):
    graph.meeting = (
        201,
        {
            "id": MEETING_ID,
            "joinWebUrl": JOIN_URL,
            "joinMeetingIdSettings": {"isPasscodeRequired": True, "passcode": PASSCODE},
        },
        {},
    )

    assert teams_plugin.create("dispatch-incident-1")["challenge"] == PASSCODE


@pytest.mark.parametrize(
    "body,missing",
    [
        ({"id": MEETING_ID}, "joinWebUrl"),
        ({"joinWebUrl": JOIN_URL}, "id"),
        ({"id": MEETING_ID, "joinWebUrl": None}, "joinWebUrl"),
        ({"id": "", "joinWebUrl": JOIN_URL}, "id"),
    ],
)
def test_an_incomplete_meeting_raises_rather_than_returning_a_partial(
    graph, teams_plugin, body, missing
):
    """Otherwise conference/flows.py fails later with a bare KeyError."""
    graph.meeting = (201, body, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        teams_plugin.create("dispatch-incident-1")

    assert missing in str(excinfo.value)


def test_a_meeting_missing_the_join_url_carries_its_id_for_cleanup(graph, teams_plugin):
    """Graph already made the meeting, so the flow has to be able to unmake it.

    This check runs after the create succeeded, which is what leaves a live
    bridge with no database row behind it (issue #114). The id travels on the
    exception because it is the only thing that can identify the meeting, and
    the plugin is the only place that still has it.
    """
    from dispatch.exceptions import ConferenceCreatedButUnusable

    graph.meeting = (201, {"id": MEETING_ID}, {})

    with pytest.raises(ConferenceCreatedButUnusable) as excinfo:
        teams_plugin.create("dispatch-incident-1")

    assert excinfo.value.resource_id == MEETING_ID


def test_a_meeting_missing_its_id_still_reports_a_meeting_may_have_leaked(graph, teams_plugin):
    """No id means no safe target -- but it is still the worst leak of the lot.

    Graph made a joinable meeting and named nothing Dispatch can delete it by,
    so it is unrecoverable. Raising the plain `DispatchPluginException` here
    would file it under "the provider was down", which reads identically to the
    harmless case; `ConferenceCreatedButUnusable` with no id is what gets the
    flow to log it as a possible orphan. Deletion behaviour is unchanged --
    `delete_unowned_conference` refuses a falsy id -- and the alternative of
    looking the meeting up by subject or joinWebUrl could match another
    incident's bridge.
    """
    from dispatch.exceptions import ConferenceCreatedButUnusable

    graph.meeting = (201, {"joinWebUrl": JOIN_URL}, {})

    with pytest.raises(ConferenceCreatedButUnusable) as excinfo:
        teams_plugin.create("dispatch-incident-1")

    assert excinfo.value.resource_id is None
    assert "id" in str(excinfo.value)


def test_a_meeting_missing_both_fields_names_the_absent_id_too(graph, teams_plugin):
    """The id is the fact that explains why nothing could be cleaned up.

    Raising on the first miss reported only `joinWebUrl` and dropped it.
    """
    graph.meeting = (201, {"subject": "Situation Room"}, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        teams_plugin.create("dispatch-incident-1")

    assert "joinWebUrl" in str(excinfo.value)
    assert "id" in str(excinfo.value)


def test_a_meeting_whose_passcode_settings_are_malformed_keeps_its_id_for_cleanup(
    graph, teams_plugin
):
    """`build_challenge` runs after the id guard, holding a good meeting id.

    A non-object `joinMeetingIdSettings` raises inside it, and letting that
    escape would throw the id away along with any chance of cleaning up.
    """
    from dispatch.exceptions import ConferenceCreatedButUnusable

    graph.meeting = (
        201,
        {"id": MEETING_ID, "joinWebUrl": JOIN_URL, "joinMeetingIdSettings": "not-an-object"},
        {},
    )

    with pytest.raises(ConferenceCreatedButUnusable) as excinfo:
        teams_plugin.create("dispatch-incident-1")

    assert excinfo.value.resource_id == MEETING_ID


def test_a_body_graph_returns_that_is_not_json_is_reported_as_a_possible_orphan(
    graph, teams_plugin
):
    """Graph committed the meeting before answering; the type must say so."""
    from dispatch.exceptions import ConferenceCreatedButUnusable

    graph.meeting = (201, b"<html>not json</html>", {})

    with pytest.raises(ConferenceCreatedButUnusable) as excinfo:
        teams_plugin.create("dispatch-incident-1")

    assert excinfo.value.resource_id is None


def test_the_failure_message_does_not_quote_the_meeting_body(graph, teams_plugin):
    """This message reaches the incident timeline, which is exported and AI-fed.

    A Graph meeting body carries the passcode and the dial-in conference id.
    """
    graph.meeting = (
        201,
        {
            "id": MEETING_ID,
            "joinMeetingIdSettings": {"passcode": "s3cr3tpass"},
            "audioConferencing": {"conferenceId": "9876543"},
        },
        {},
    )

    with pytest.raises(DispatchPluginException) as excinfo:
        teams_plugin.create("dispatch-incident-1")

    assert "s3cr3tpass" not in str(excinfo.value)
    assert "9876543" not in str(excinfo.value)


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


# --- the configured credentials are the ones actually used ------------------
#
# Asserting these at the client level only proves the client works. The wiring
# in `_client()` is its own defect surface: pointing `client_id` at `user_id`,
# or the secret at a literal, makes the plugin non-functional against a real
# tenant while every client-level test stays green.


def test_the_configured_client_id_reaches_the_token_request(graph, teams_plugin):
    teams_plugin.configuration.client_id = "the-configured-client-id"
    teams_plugin.create("dispatch-incident-1")

    assert graph.last_token_request().form["client_id"] == "the-configured-client-id"


def test_the_configured_secret_reaches_the_token_request(graph, teams_plugin):
    teams_plugin.configuration.secret = SecretStr("the-configured-secret")
    teams_plugin.create("dispatch-incident-1")

    assert graph.last_token_request().form["client_secret"] == "the-configured-secret"


def test_the_configured_authority_is_the_one_contacted(graph, teams_plugin):
    teams_plugin.create("dispatch-incident-1")

    assert graph.last_token_request().url.startswith(AUTHORITY)


def test_the_client_id_and_user_id_are_not_confused(graph, teams_plugin):
    """They are both GUIDs, so a swap is invisible without asserting on both."""
    teams_plugin.create("dispatch-incident-1")

    assert graph.last_token_request().form["client_id"] == CLIENT_ID
    assert f"/users/{USER_ID}/" in graph.last_graph_request().url


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
    """Compares signatures, not just presence.

    ``hasattr`` alone is satisfied by ``ConferencePlugin.create`` on the base
    class, so it would pass for a plugin that implements nothing.
    """
    import inspect

    from dispatch.plugins.dispatch_microsoft_teams.conference.plugin import (
        MicrosoftTeamsConferencePlugin,
    )
    from dispatch.plugins.dispatch_zoom.plugin import ZoomConferencePlugin

    assert method in ZoomConferencePlugin.__dict__, "baseline changed; update this test"
    assert method in MicrosoftTeamsConferencePlugin.__dict__

    def parameters(cls):
        func = inspect.unwrap(cls.__dict__[method])
        return list(inspect.signature(func).parameters)

    assert parameters(MicrosoftTeamsConferencePlugin) == parameters(ZoomConferencePlugin)


def test_deleting_a_meeting_that_is_already_gone_raises(graph, teams_plugin):
    """Graph answers 404, not 204, for a meeting that was already deleted.

    Zoom behaves the same way (``test_a_rejected_deletion_raises``). Neither
    plugin reclassifies it as success, so a retried teardown is a logged
    failure rather than a silent no-op -- ``delete_conference`` swallows it,
    which is what keeps a repeat attempt from wedging the incident delete flow.
    """
    graph.delete = (404, {"error": {"code": "NotFound", "message": "Meeting not found."}}, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        teams_plugin.delete(MEETING_ID)

    assert "Meeting not found." in str(excinfo.value)


def test_delete_propagates_a_graph_failure(graph, teams_plugin):
    """A swallowed delete leaks a meeting on every incident delete, silently."""
    graph.delete = (403, {"error": {"code": "Forbidden", "message": "No access policy."}}, {})

    with pytest.raises(DispatchPluginException) as excinfo:
        teams_plugin.delete(MEETING_ID)

    assert "No access policy." in str(excinfo.value)


def test_participant_management_reaches_graph(graph, teams_plugin):
    """Both were no-op stubs until issue #106; the roster is now really updated.

    Behaviour lives in ``test_participants.py``; this only guards against a
    regression to ``return``, which no assertion on the return value would catch.
    """
    graph.get = (200, graph.meeting_with_attendees(), {})

    teams_plugin.add_participant(MEETING_ID, "someone@example.com")

    assert [r.method for r in graph.graph_requests()] == ["GET", "PATCH"]


# --- instrumentation --------------------------------------------------------


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

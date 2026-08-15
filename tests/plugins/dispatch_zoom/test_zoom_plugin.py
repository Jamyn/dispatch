"""Regression cover for the Zoom conference plugin.

Zoom is the parity baseline for the Microsoft Teams plugin and the two share
``dispatch.decorators.apply`` and the ``ConferencePlugin`` contract. These tests
exist so a change made for Teams cannot quietly alter Zoom.
"""

import pytest

from dispatch.exceptions import DispatchPluginException


def test_create_returns_the_weblink_id_and_challenge(zoom, zoom_plugin):
    conference = zoom_plugin.create("dispatch-incident-1")

    assert conference["weblink"] == "https://zoom.us/j/987654321"
    # A *string*, though Zoom sends the id as a JSON number -- see
    # test_the_conference_the_plugin_returns_is_accepted_by_the_flow.
    assert conference["id"] == "987654321"
    assert conference["challenge"] == "zoompass"


def test_the_conference_the_plugin_returns_is_accepted_by_the_flow(zoom, zoom_plugin):
    """The plugin's output must satisfy the model the flow builds from it.

    `conference/flows.py` constructs `ConferenceCreate` *outside* the try/except
    that guards the plugin call, so a type it rejects does not degrade to "no
    conference" -- it propagates out of `create_conference` and aborts
    `incident_create_flow` before the Slack channel is ever made.

    Zoom sends `id` as a JSON number and both fields are typed `str`; pydantic
    v2 does not coerce. Asserting the plugin's dict in isolation cannot catch
    this, which is why this test builds the real model.
    """
    from dispatch.conference.models import ConferenceCreate

    conference = zoom_plugin.create("dispatch-incident-1")

    # Exactly what conference/flows.py does with the returned dict.
    model = ConferenceCreate(
        resource_id=conference["id"],
        resource_type="zoom-conference",
        weblink=conference["weblink"],
        conference_id=conference["id"],
        conference_challenge=conference["challenge"],
    )

    assert model.conference_id == "987654321"
    assert model.resource_id == "987654321"


def test_a_meeting_without_a_passcode_still_yields_a_conference(zoom, zoom_plugin):
    """Account policy can disable passcodes; the join_url is still good.

    Requiring `password` here would turn a usable bridge into no bridge at all.
    Teams behaves the same way, returning an empty challenge.
    """
    zoom.response = (201, {"id": 987654321, "join_url": "https://zoom.us/j/987654321"})

    conference = zoom_plugin.create("dispatch-incident-1")

    assert conference["weblink"] == "https://zoom.us/j/987654321"
    assert conference["challenge"] == ""


def test_the_title_becomes_the_meeting_topic(zoom, zoom_plugin):
    zoom_plugin.create("dispatch-incident-1", title="Payments API degraded")

    assert zoom.requests[-1].json["topic"] == "Payments API degraded"


def test_the_topic_falls_back_to_a_situation_room(zoom, zoom_plugin):
    zoom_plugin.create("dispatch-incident-1")

    assert zoom.requests[-1].json["topic"] == "Situation Room for dispatch-incident-1"


def test_the_configured_duration_is_sent(zoom, zoom_plugin):
    zoom_plugin.configuration.default_duration_minutes = 90
    zoom_plugin.create("dispatch-incident-1")

    assert zoom.requests[-1].json["duration"] == 90


def test_the_meeting_is_password_protected(zoom, zoom_plugin):
    """Truthiness alone would accept a one-character constant."""
    zoom_plugin.create("dispatch-incident-1")

    password = zoom.requests[-1].json["password"]
    assert len(password) == 8
    assert len(set(password)) > 1, "a constant-character password"


def test_each_meeting_gets_a_different_password(zoom, zoom_plugin):
    passwords = set()
    for _ in range(10):
        zoom_plugin.create("dispatch-incident-1")
        passwords.add(zoom.requests[-1].json["password"])

    assert len(passwords) > 8, "passwords are not being regenerated"


def test_responders_can_enter_the_bridge_before_the_host(zoom, zoom_plugin):
    """Without this the bridge is unusable until the API user dials in."""
    zoom_plugin.create("dispatch-incident-1")

    assert zoom.requests[-1].json["settings"]["join_before_host"] is True


def test_the_description_becomes_the_agenda(zoom, zoom_plugin):
    zoom_plugin.create("dispatch-incident-1", description="Payments are failing")

    assert zoom.requests[-1].json["agenda"] == "Payments are failing"


def test_the_zoom_call_has_a_timeout(zoom, zoom_plugin):
    zoom_plugin.create("dispatch-incident-1")

    assert zoom.requests[-1].timeout == 15


def test_delete_calls_zoom(zoom, zoom_plugin):
    zoom_plugin.delete("987654321")

    assert zoom.requests[-1].method == "DELETE"
    assert zoom.requests[-1].timeout == 15, "delete was sent without a timeout"


def test_delete_targets_the_meeting_it_was_given(zoom, zoom_plugin):
    """Asserting the verb alone would pass for a DELETE of the wrong meeting.

    ``incident_delete_flow`` reaches this with the conference's provider id
    (issue #105); a path built from anything else deletes nothing, or somebody
    else's bridge.
    """
    zoom_plugin.delete("987654321")

    assert zoom.requests[-1].url.split("?")[0].endswith("/meetings/987654321")


def test_create_is_still_instrumented(zoom, zoom_plugin, monkeypatch):
    """``apply`` is shared with the Teams plugin; this guards Zoom's use of it."""
    emitted = []
    monkeypatch.setattr(
        "dispatch.decorators.metrics_provider",
        type(
            "Recorder",
            (),
            {
                "counter": lambda self, name, value=None, tags=None: emitted.append(
                    tags["function"]
                ),
                "timer": lambda self, name, value=None, tags=None: emitted.append(tags["function"]),
                "gauge": lambda self, name, value=None, tags=None: None,
            },
        )(),
    )

    zoom_plugin.create("dispatch-incident-1")

    assert any("ZoomConferencePlugin.create" in name for name in emitted)


def test_gen_conference_challenge_is_within_zooms_length_limit():
    from dispatch.plugins.dispatch_zoom.plugin import gen_conference_challenge

    assert len(gen_conference_challenge(8)) == 8
    assert len(gen_conference_challenge(50)) == 10


def test_gen_conference_challenge_actually_varies():
    """Length alone would accept a generator returning a constant."""
    from dispatch.plugins.dispatch_zoom.plugin import gen_conference_challenge

    assert len({gen_conference_challenge(8) for _ in range(20)}) > 15


def test_creating_a_meeting_authenticates_first(zoom, zoom_plugin):
    """The old self-signed JWT is gone; a token is now fetched from Zoom.

    Asserts the exact token, not a ``Bearer `` prefix -- the retired flow sent
    ``Bearer <self-signed jwt>`` too, so a prefix check cannot tell the two
    schemes apart.
    """
    from tests.plugins.dispatch_zoom.conftest import ACCESS_TOKEN, OAUTH_TOKEN_URL

    zoom_plugin.create("dispatch-incident-1")

    assert zoom.requests[0].url.split("?")[0] == OAUTH_TOKEN_URL, "the token was not fetched first"
    assert zoom.last_api_request().headers["authorization"] == f"Bearer {ACCESS_TOKEN}"


# --- failures are reported, never papered over ------------------------------


def test_a_rejected_creation_raises_rather_than_inventing_a_conference(zoom, zoom_plugin):
    """The defect this guards is silent, and worse than a crash.

    Reading Zoom's error body through ``.get(key, default)`` produced a
    conference pointing at zoom.us with id "1" and challenge "123", which the
    flow then committed and announced as success.
    """
    zoom.response = (
        401,
        {"code": 4711, "message": "Invalid access token, does not contain scopes:[meeting:write]"},
    )

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.create("dispatch-incident-1")

    message = str(excinfo.value)
    assert "meeting:write" in message, "Zoom's own reason was dropped"
    assert "zoom.us/j/" not in message


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_no_failure_status_yields_a_usable_looking_conference(zoom, zoom_plugin, status):
    zoom.response = (status, {"message": "nope"})

    with pytest.raises(DispatchPluginException):
        zoom_plugin.create("dispatch-incident-1")


def test_a_creation_missing_the_join_url_raises(zoom, zoom_plugin):
    """A 2xx is not enough; the fields the incident needs must be present."""
    zoom.response = (201, {"id": 987654321, "password": "zoompass"})

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.create("dispatch-incident-1")

    assert "join_url" in str(excinfo.value)


def test_a_creation_missing_the_join_url_carries_the_id_for_cleanup(zoom, zoom_plugin):
    """Zoom already made the meeting, so the flow has to be able to unmake it.

    This check runs after the create succeeded, which is what leaves a live
    bridge with no database row behind it (issue #114). The id travels on the
    exception because it is the only thing that can identify the meeting, and
    the plugin is the only place that still has it.
    """
    from dispatch.exceptions import ConferenceCreatedButUnusable

    zoom.response = (201, {"id": 987654321, "password": "zoompass"})

    with pytest.raises(ConferenceCreatedButUnusable) as excinfo:
        zoom_plugin.create("dispatch-incident-1")

    # A string, exactly as a successful create would have returned it -- Zoom
    # sends the id as a JSON number.
    assert excinfo.value.resource_id == "987654321"


def test_a_creation_missing_the_id_still_reports_a_meeting_may_have_leaked(zoom, zoom_plugin):
    """No id means no safe target -- but it is still the worst leak of the lot.

    Zoom made a joinable meeting and named nothing Dispatch can delete it by,
    so it is unrecoverable. Raising the plain `DispatchPluginException` here
    would file it under "the provider was down", which reads identically to the
    harmless case; `ConferenceCreatedButUnusable` with no id is what gets the
    flow to log it as a possible orphan. Deletion behaviour is unchanged --
    `delete_unowned_conference` refuses a falsy id -- and the alternative of
    looking the meeting up by topic or join_url could match another incident's
    bridge.
    """
    from dispatch.exceptions import ConferenceCreatedButUnusable

    zoom.response = (201, {"join_url": "https://zoom.us/j/987654321"})

    with pytest.raises(ConferenceCreatedButUnusable) as excinfo:
        zoom_plugin.create("dispatch-incident-1")

    assert excinfo.value.resource_id is None
    assert "id" in str(excinfo.value)


def test_a_body_zoom_returns_that_is_not_json_is_reported_as_a_possible_orphan(zoom, zoom_plugin):
    """The message already says Zoom accepted the create; the type must agree."""
    from dispatch.exceptions import ConferenceCreatedButUnusable

    zoom.response = (201, b"<html>not json</html>")

    with pytest.raises(ConferenceCreatedButUnusable) as excinfo:
        zoom_plugin.create("dispatch-incident-1")

    assert excinfo.value.resource_id is None


def test_a_body_zoom_returns_that_is_not_an_object_is_reported_as_a_possible_orphan(
    zoom, zoom_plugin
):
    """A JSON array would otherwise be an AttributeError on `.get`."""
    from dispatch.exceptions import ConferenceCreatedButUnusable

    zoom.response = (201, [{"id": 1}])

    with pytest.raises(ConferenceCreatedButUnusable):
        zoom_plugin.create("dispatch-incident-1")


def test_a_failure_body_is_not_echoed_into_the_timeline(zoom, zoom_plugin):
    """The API request carries a live bearer token.

    An intermediary answering in Zoom's place may quote the request back, and
    this message reaches the incident timeline, which is broadly readable. Only
    Zoom's structured `message` is ever repeated.
    """
    zoom.response = (403, b"Blocked. Authorization: Bearer test-access-token")

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.create("dispatch-incident-1")

    message = str(excinfo.value)
    assert "403" in message
    assert "Bearer" not in message
    assert "test-access-token" not in message


def test_a_rejected_deletion_raises(zoom, zoom_plugin):
    zoom.response = (404, {"message": "Meeting does not exist"})

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.delete("987654321")

    assert "Meeting does not exist" in str(excinfo.value)


def test_an_unreadable_configuration_says_what_to_do(zoom, zoom_plugin):
    """A config predating the OAuth migration parses to None.

    This message is what reaches the incident timeline, so it names the fix
    rather than surfacing an AttributeError on NoneType.
    """
    zoom_plugin.configuration = None

    with pytest.raises(DispatchPluginException) as excinfo:
        zoom_plugin.create("dispatch-incident-1")

    message = str(excinfo.value)
    assert "Server-to-Server OAuth" in message
    assert "Plugins" in message
    assert zoom.requests == [], "a token was requested with no credentials"

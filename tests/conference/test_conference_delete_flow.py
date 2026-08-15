"""``delete_conference`` -- incident bridge teardown (issue #105).

Every conference plugin has implemented ``delete`` since the beginning and
nothing ever called it, so an incident bridge stayed in the provider forever.

The contract defended here is the one the other external resources already
have (``ticket``, ``participant-group``, ``storage``, ``conversation``):
deletion is best effort. A provider that refuses is logged and dropped,
because the incident is going away either way and a stuck bridge must not
become a stuck deletion.

The identifier assertions are the other half. ``create_conference`` writes the
provider's meeting id into *both* ``conference_id`` and ``resource_id``, so the
two are equal in production and a test built on a factory default would pass
whichever the flow read. The fixtures below force them apart so the assertions
pin the field rather than the value.

(``resource_id`` is not an "internal" id -- ``ResourceMixin`` uses it for the
external handle everywhere in Dispatch, and ``delete_ticket`` passes it
straight to the ticket plugin. There is simply no reason for the conference
flow to read it when ``conference_id`` is what the rest of the conference
domain uses.)
"""

import logging

import pytest

from dispatch.conference.flows import delete_conference
from dispatch.exceptions import ConferenceAlreadyGone, DispatchPluginException

from tests.factories import ConferenceFactory, PluginFactory, PluginInstanceFactory

PROVIDER_MEETING_ID = "provider-meeting-123"
OTHER_RESOURCE_ID = "other-resource-456"


class RecordingConferencePlugin:
    """Stands in for the plugin instance's ``.instance`` attribute."""

    def __init__(self, failure: Exception | None = None):
        self.failure = failure
        self.deleted: list[str] = []

    def delete(self, event_id):
        self.deleted.append(event_id)
        if self.failure:
            raise self.failure


@pytest.fixture
def conference(session, incident):
    """The incident's conference, as ``create_conference`` leaves it."""
    return ConferenceFactory(
        incident=incident,
        conference_id=PROVIDER_MEETING_ID,
        resource_id=OTHER_RESOURCE_ID,
    )


@pytest.fixture
def active_conference_plugin(session, incident, monkeypatch):
    """Register a recording conference plugin for the incident's project."""
    recorder = RecordingConferencePlugin()
    instance = PluginInstanceFactory(
        project=incident.project,
        plugin=PluginFactory(type="conference", slug="test-conference-delete"),
        enabled=True,
    )
    monkeypatch.setattr(type(instance), "instance", property(lambda self: recorder))
    return recorder


# --- the happy path ---------------------------------------------------------


def test_delete_sends_the_conference_id_and_not_the_resource_id(
    session, incident, conference, active_conference_plugin
):
    """Exact equality, not ``resource_id not in deleted``.

    A "the wrong id was not sent" assertion also passes when *nothing* was
    sent, so it holds for a `delete_conference` that does nothing at all. This
    is the assertion that actually pins the field, and asserting the whole list
    pins the call count with it.
    """
    delete_conference(conference=conference, project_id=incident.project.id, db_session=session)

    assert active_conference_plugin.deleted == [PROVIDER_MEETING_ID]
    assert conference.resource_id == OTHER_RESOURCE_ID, "the fixture stopped forcing them apart"


def test_nothing_is_written_to_the_incident_timeline(
    session, incident, conference, active_conference_plugin
):
    """Unlike the participant flow in this module, which does log an event.

    The incident is about to be deleted and its events cascade with it, so a
    timeline entry here is written only to be destroyed. Pinned because the
    neighbouring function sets the opposite precedent.
    """
    from dispatch.event import service as event_service

    before = len(event_service.get_all(db_session=session).all())

    delete_conference(conference=conference, project_id=incident.project.id, db_session=session)

    assert len(event_service.get_all(db_session=session).all()) == before


# --- nothing to do ----------------------------------------------------------


def test_a_missing_conference_is_a_no_op(session, incident, active_conference_plugin):
    """Conferences are optional; most projects run without one."""
    delete_conference(conference=None, project_id=incident.project.id, db_session=session)

    assert active_conference_plugin.deleted == []


@pytest.mark.parametrize("missing", [None, ""])
def test_a_conference_without_a_provider_id_is_not_sent_to_the_plugin(
    session, incident, active_conference_plugin, caplog, missing
):
    """``conference_id`` is nullable, and ``DELETE /meetings/None`` is not a request.

    Warned rather than debugged: the caller already checked that a conference
    row exists, so reaching here means the bridge may be live provider-side
    with nothing left that can address it.
    """
    conference = ConferenceFactory(
        incident=incident, conference_id=missing, resource_id=OTHER_RESOURCE_ID
    )

    with caplog.at_level(logging.WARNING):
        delete_conference(conference=conference, project_id=incident.project.id, db_session=session)

    assert active_conference_plugin.deleted == []
    assert "no provider id" in caplog.text


def test_no_conference_plugin_warns(session, incident, conference, caplog):
    """Silence would leave an operator no way to notice bridges piling up.

    Also the "does not raise" case for a project with no conference plugin --
    an exception here would abort the incident deletion.
    """
    with caplog.at_level(logging.WARNING):
        delete_conference(conference=conference, project_id=incident.project.id, db_session=session)

    assert "No conference plugin enabled" in caplog.text


def test_a_disabled_conference_plugin_is_not_called(session, incident, conference, monkeypatch):
    """``get_active_instance`` filters on ``enabled``; an operator switched it off."""
    recorder = RecordingConferencePlugin()
    instance = PluginInstanceFactory(
        project=incident.project,
        plugin=PluginFactory(type="conference", slug="test-conference-disabled"),
        enabled=False,
    )
    monkeypatch.setattr(type(instance), "instance", property(lambda self: recorder))

    delete_conference(conference=conference, project_id=incident.project.id, db_session=session)

    assert recorder.deleted == []


# --- failures are contained -------------------------------------------------


def test_a_failing_delete_does_not_raise(session, incident, conference, active_conference_plugin):
    """The incident is being deleted; a leaked bridge must not block that.

    The trailing assertion is what stops this passing for a `delete_conference`
    that never calls the plugin at all -- the recorder appends before it
    raises, so it proves the exception came from inside the plugin call.
    """
    active_conference_plugin.failure = RuntimeError("Graph said no")

    delete_conference(conference=conference, project_id=incident.project.id, db_session=session)

    assert active_conference_plugin.deleted == [PROVIDER_MEETING_ID]


def test_a_plugin_exception_is_contained_too(
    session, incident, conference, active_conference_plugin
):
    """The plugins raise DispatchPluginException specifically."""
    active_conference_plugin.failure = DispatchPluginException("Zoom deletion failed with HTTP 403")

    delete_conference(conference=conference, project_id=incident.project.id, db_session=session)

    assert active_conference_plugin.deleted == [PROVIDER_MEETING_ID]


def test_a_failing_delete_is_logged(
    session, incident, conference, active_conference_plugin, caplog
):
    """A silent swallow leaves an operator with no way to notice the leak."""
    active_conference_plugin.failure = RuntimeError("Graph said no")

    with caplog.at_level(logging.ERROR):
        delete_conference(conference=conference, project_id=incident.project.id, db_session=session)

    assert "Graph said no" in caplog.text
    # the log line is the only record a leaked bridge gets, so it has to name it
    assert PROVIDER_MEETING_ID in caplog.text


def test_the_failure_log_does_not_leak_the_passcode_or_join_link(
    session, incident, conference, active_conference_plugin, caplog
):
    """``conference_challenge`` is the meeting passcode, and a Zoom join_url carries it."""
    conference.conference_challenge = "s3cret-passcode"
    conference.weblink = "https://zoom.us/j/123?pwd=s3cret-passcode"
    active_conference_plugin.failure = RuntimeError("Graph said no")

    with caplog.at_level(logging.DEBUG):
        delete_conference(conference=conference, project_id=incident.project.id, db_session=session)

    # Precomputed: a failing `assert x not in text` renders both operands into
    # the pytest report, publishing the very secret it checks for.
    leaked = "s3cret-passcode" in caplog.text
    assert not leaked


# --- the provider has no such meeting (issue #120) --------------------------


def test_a_meeting_that_is_already_gone_is_not_fatal(
    session, incident, conference, active_conference_plugin
):
    """A repeat attempt -- a retried request, a second click -- must not wedge the flow."""
    active_conference_plugin.failure = ConferenceAlreadyGone("no such meeting")

    delete_conference(conference=conference, project_id=incident.project.id, db_session=session)

    assert active_conference_plugin.deleted == [PROVIDER_MEETING_ID]


def test_a_meeting_that_is_already_gone_is_not_reported_as_a_failure(
    session, incident, conference, active_conference_plugin, caplog
):
    """The whole of issue #120: teardown's intent was met, so nothing leaked.

    Asserted at ERROR specifically. Before the fix this line was emitted by
    ``log.exception``, which is where an operator looks for leaked resources,
    and it named a bridge that did not exist.
    """
    active_conference_plugin.failure = ConferenceAlreadyGone("no such meeting")

    with caplog.at_level(logging.ERROR):
        delete_conference(conference=conference, project_id=incident.project.id, db_session=session)

    assert caplog.text == ""


def test_a_meeting_that_is_already_gone_is_still_recorded(
    session, incident, conference, active_conference_plugin, caplog
):
    """Not silence either: what the provider was holding is worth knowing."""
    active_conference_plugin.failure = ConferenceAlreadyGone("no such meeting")

    with caplog.at_level(logging.INFO):
        delete_conference(conference=conference, project_id=incident.project.id, db_session=session)

    assert PROVIDER_MEETING_ID in caplog.text
    assert "already gone" in caplog.text


def test_a_genuine_failure_is_not_swallowed_by_the_already_gone_handler(
    session, incident, conference, active_conference_plugin, caplog
):
    """``ConferenceAlreadyGone`` is a ``DispatchPluginException``, so ordering matters.

    A handler placed after the broad one, or a sibling that caught the base
    class, would report every provider refusal as an already-deleted meeting.
    """
    active_conference_plugin.failure = DispatchPluginException(
        "Zoom deletion of the meeting failed with HTTP 500: Internal error."
    )

    with caplog.at_level(logging.ERROR):
        delete_conference(conference=conference, project_id=incident.project.id, db_session=session)

    assert "HTTP 500" in caplog.text
    assert PROVIDER_MEETING_ID in caplog.text

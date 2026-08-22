"""``create_conference`` -- provider meeting versus Dispatch ownership (issue #114).

#105 closed the steady-state leak by teaching ``incident_delete_flow`` to delete
the bridge. This suite is about the window before that flow can help at all:
``incident_delete_flow`` reaches a meeting only through ``incident.conference``,
so a provider meeting that never got a ``Conference`` row is unreachable
forever. No amount of teardown fixes it after the fact -- the id has to be used
while it is still in hand.

The contract every test below defends is a single sentence:

    once the provider has accepted a meeting, either Dispatch commits a durable
    reference to it, or Dispatch deletes it.

That boundary is deliberately drawn at *ownership*, not at the kind of failure.
A meeting Dispatch could not persist is one nobody can reach or ever clean up,
whoever is at fault -- and on Zoom and Teams nobody holds its link either, since
``create_conference`` is what publishes it. The converse matters just as much
and is tested too: once the commit lands, a later failure must never trigger a
delete, and neither must a failure that only *looks* like the commit did not
land.

One caveat the contract does not cover, recorded so the sentence above is not
read as more than it is: a plugin only gets its meeting cleaned up if it raises
``ConferenceCreatedButUnusable``. The three shipped plugins do; the base class
cannot make a plugin shipped elsewhere do the same.
"""

import logging
import traceback

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm.attributes import set_committed_value

from dispatch.conference.flows import create_conference
from dispatch.conference.models import Conference
from dispatch.exceptions import (
    ConferenceAlreadyGone,
    DispatchException,
    ConferenceCreatedButUnusable,
    DispatchPluginException,
)
from dispatch.plugins.base import plugins, register
from dispatch.plugins.bases import ConferencePlugin

from tests.factories import PluginFactory, PluginInstanceFactory

MEETING_ID = "meeting-abc-123"
WEBLINK = "https://conference.example.com/j/meeting-abc-123?pwd=sup3rsecret"
CHALLENGE = "passc0de"


def meeting(**overrides) -> dict:
    """A well-formed provider response, as a conference plugin returns one."""
    return {"id": MEETING_ID, "weblink": WEBLINK, "challenge": CHALLENGE, **overrides}


class RecordingConferencePlugin(ConferencePlugin):
    """A genuinely registered conference plugin that records what it was asked.

    A real `ConferencePlugin` resolved through the real registry, **not** a
    monkeypatched `PluginInstance.instance` property. That distinction is
    load-bearing: `PluginInstance.instance` lazy-loads `self.plugin` to find the
    class, and that read is exactly what fails on a session a failed flush has
    left needing a rollback. Patching the property away makes the ORM
    dereference disappear and hides the defect these tests exist to catch.

    Tracks *live* meetings, not just calls. A test that only asserted "delete
    was called" would pass for an implementation that deleted the wrong id, so
    ``live`` is what the orphan assertions read.
    """

    title = "Dispatch Test Plugin - Conference Lifecycle"
    slug = "test-conference-lifecycle"
    description = "Records the conference lifecycle calls the flow makes."

    # Reset per test by the fixture; the registry hands out one shared instance.
    response = None
    create_error = None
    delete_error = None

    def reset(self):
        self.response = None
        self.create_error = None
        self.delete_error = None
        self.created: list[str] = []
        self.deleted: list = []
        self.live: set = set()
        return self

    def create(self, name, description=None, title=None, participants=None):
        self.created.append(name)

        if self.create_error is not None:
            # A plugin that rejects its own provider's response has already had
            # the meeting accepted; one that failed to reach the provider has
            # not. The distinction is the whole point of the id on the exception.
            resource_id = getattr(self.create_error, "resource_id", None)
            if resource_id:
                self.live.add(resource_id)
            raise self.create_error

        if self.response is None:
            return None

        if isinstance(self.response, dict) and "id" in self.response:
            self.live.add(self.response["id"])
            return dict(self.response)

        # A plugin free to return an unusable mapping is equally free to return
        # something that is not one at all.
        return self.response

    def delete(self, event_id):
        self.deleted.append(event_id)
        if self.delete_error is not None:
            raise self.delete_error
        self.live.discard(event_id)


@pytest.fixture
def register_conference_plugin(session, incident):
    """Register the recording plugin for the incident's project, for real.

    The `PluginInstance` row points at a `Plugin` row whose slug matches the
    registered class, so `PluginInstance.instance` resolves it the way
    production does -- lazy load included.
    """
    register(RecordingConferencePlugin)
    recorder = plugins.get(RecordingConferencePlugin.slug).reset()

    def _register(**state) -> RecordingConferencePlugin:
        for key, value in state.items():
            setattr(recorder, key, value)
        PluginInstanceFactory(
            project=incident.project,
            plugin=PluginFactory(type="conference", slug=RecordingConferencePlugin.slug),
            enabled=True,
        )
        return recorder

    yield _register
    recorder.reset()


@pytest.fixture
def plugin(register_conference_plugin):
    """The default case: a provider that hands back a usable meeting."""
    return register_conference_plugin(response=meeting())


def conference_rows(session) -> list[Conference]:
    return session.query(Conference).all()


# --- the successful path ----------------------------------------------------


def test_a_successful_create_attaches_the_conference_to_the_incident(session, incident, plugin):
    conference = create_conference(incident=incident, participants=[], db_session=session)

    assert conference is not None
    assert conference.conference_id == MEETING_ID
    assert conference.resource_id == MEETING_ID
    assert conference.weblink == WEBLINK
    assert conference.conference_challenge == CHALLENGE
    assert incident.conference is conference


def test_a_successful_create_deletes_nothing(session, incident, plugin):
    """Compensation must be the exception, not something the happy path pays."""
    create_conference(incident=incident, participants=[], db_session=session)

    assert plugin.deleted == []
    assert plugin.live == {MEETING_ID}


def test_the_conference_row_is_never_inserted_without_its_incident(session, incident, plugin):
    """The row and the link that makes it findable have to land together.

    A ``Conference`` committed with a NULL ``incident_id`` is already lost: the
    only path to it is ``incident.conference``. Asserting on the *final* state
    cannot see this -- the second commit repairs it -- so this listens to the
    INSERT itself.
    """
    from sqlalchemy import event

    inserted_with: list = []

    @event.listens_for(Conference, "after_insert")
    def _record(mapper, connection, target):
        inserted_with.append(target.incident_id)

    try:
        create_conference(incident=incident, participants=[], db_session=session)
    finally:
        event.remove(Conference, "after_insert", _record)

    assert inserted_with == [incident.id]


# --- the provider never accepted a meeting ----------------------------------


def test_a_provider_create_failure_deletes_nothing(session, incident, register_conference_plugin):
    """Nothing was created, so there is nothing to compensate for.

    Deleting here would mean issuing a delete against an id we do not have, or
    guessing at one -- both worse than the leak they would be trying to prevent.
    """
    plugin = register_conference_plugin(
        create_error=DispatchPluginException("HTTP 503 from the provider")
    )

    assert create_conference(incident=incident, participants=[], db_session=session) is None
    assert plugin.deleted == []


def test_a_provider_create_failure_is_still_recorded_on_the_timeline(
    session, incident, register_conference_plugin
):
    """Pre-existing behaviour from before #114; the fix must not drop it."""
    register_conference_plugin(create_error=DispatchPluginException("HTTP 503 from the provider"))

    create_conference(incident=incident, participants=[], db_session=session)

    descriptions = [event.description for event in incident.events]
    assert any("HTTP 503 from the provider" in d for d in descriptions), descriptions


def test_a_plugin_that_returns_nothing_deletes_nothing(
    session, incident, register_conference_plugin
):
    plugin = register_conference_plugin(response=None)

    assert create_conference(incident=incident, participants=[], db_session=session) is None
    assert plugin.deleted == []
    assert incident.conference is None


# --- the plugin rejected a meeting the provider had already accepted --------


def test_a_meeting_the_plugin_rejected_after_creating_it_is_deleted(
    session, incident, register_conference_plugin
):
    """Zoom and Teams both validate the response *after* the provider committed.

    That check is the reason this failure class exists at all, and it is the one
    place the flow cannot see the id for itself -- the plugin has to hand it over.
    """
    plugin = register_conference_plugin(
        create_error=ConferenceCreatedButUnusable(
            "the provider created the meeting but omitted join_url", resource_id=MEETING_ID
        )
    )

    assert create_conference(incident=incident, participants=[], db_session=session) is None

    assert plugin.deleted == [MEETING_ID]
    assert plugin.live == set()


def test_a_meeting_the_plugin_rejected_leaves_no_conference(
    session, incident, register_conference_plugin
):
    """Same return and timeline as any other provider failure -- only the cleanup is new."""
    register_conference_plugin(
        create_error=ConferenceCreatedButUnusable("omitted join_url", resource_id=MEETING_ID)
    )

    before = len(conference_rows(session))
    create_conference(incident=incident, participants=[], db_session=session)

    assert incident.conference is None
    assert len(conference_rows(session)) == before
    assert any("omitted join_url" in e.description for e in incident.events)


def test_a_rejected_meeting_with_no_provider_id_is_not_deleted(
    session, incident, register_conference_plugin, caplog
):
    """No id means no safe target. Guessing one from a name or a URL could match
    a bridge belonging to a different incident, so the leak is logged instead."""
    plugin = register_conference_plugin(
        create_error=ConferenceCreatedButUnusable(
            "the provider created the meeting but omitted id", resource_id=None
        )
    )

    with caplog.at_level(logging.ERROR):
        create_conference(incident=incident, participants=[], db_session=session)

    assert plugin.deleted == []
    assert "no id" in caplog.text.lower()


# --- Dispatch could not take ownership of a meeting the provider accepted ---


def test_a_response_conference_create_rejects_deletes_the_meeting(
    session, incident, register_conference_plugin
):
    """The literal regression from #114.

    A well-formed meeting whose id is a JSON number: pydantic v2 does not coerce
    int to str, so ``ConferenceCreate`` rejects it *after* the provider committed.
    This is the bug the Zoom plugin's ``str(...)`` papers over -- the flow must
    survive it for every other plugin too.
    """
    plugin = register_conference_plugin(response=meeting(id=987654321))

    with pytest.raises(ValidationError):
        create_conference(incident=incident, participants=[], db_session=session)

    assert plugin.deleted == [987654321]
    assert plugin.live == set()


def test_a_response_missing_the_weblink_deletes_the_meeting(
    session, incident, register_conference_plugin
):
    """The flow subscripts the response without a default, so this is a KeyError."""
    response = meeting()
    del response["weblink"]
    plugin = register_conference_plugin(response=response)

    with pytest.raises(KeyError):
        create_conference(incident=incident, participants=[], db_session=session)

    assert plugin.deleted == [MEETING_ID]
    assert plugin.live == set()


def test_a_database_failure_deletes_the_meeting(session, incident, register_conference_plugin):
    """A real driver-level insert failure, not a stubbed one.

    Postgres cannot store a NUL byte in text, so psycopg2 raises while the INSERT
    is being built. It arrives as a bare ``ValueError``, which is exactly the
    "unexpected internal exception" class that classifying exceptions would get
    wrong: the meeting is still unreachable, so it still has to go.
    """
    plugin = register_conference_plugin(response=meeting(weblink="https://example.com/\x00j/1"))

    with pytest.raises(ValueError):
        create_conference(incident=incident, participants=[], db_session=session)

    session.rollback()

    assert plugin.deleted == [MEETING_ID]
    assert plugin.live == set()


def test_a_commit_failure_deletes_the_meeting_with_a_session_the_failure_poisoned(
    session, incident, register_conference_plugin
):
    """The failure has to be a real one, or the test cannot see the real bug.

    A stubbed ``session.commit`` never flushes, so the session stays healthy and
    the compensation path runs against an ORM that still answers. Production
    does not look like that: after a failed flush SQLAlchemy expires every
    persistent object, and ``PluginInstance.instance`` -- which lazy-loads
    ``self.plugin`` -- then raises. That is why the flow resolves the plugin
    *before* the guarded span, and this is the test that would notice if it
    stopped.

    Driven through a real INSERT failure, so the session really is poisoned by
    the time compensation runs.
    """
    plugin = register_conference_plugin(response=meeting(weblink="https://example.com/\x00j/2"))

    with pytest.raises(ValueError):
        create_conference(incident=incident, participants=[], db_session=session)

    assert plugin.deleted == [MEETING_ID]
    assert plugin.live == set()
    session.rollback()


def test_an_owned_conference_is_never_deleted_by_a_failure_after_the_commit_landed(
    session, incident, register_conference_plugin, monkeypatch
):
    """A raised COMMIT is not proof of a rolled-back COMMIT.

    If the connection drops after Postgres committed but before the client reads
    the ack, the row is durably written and the incident owns it -- and nothing
    would ever replace it, because ``incident_create_resources_flow`` guards on
    ``if not incident.conference``. Deleting there would leave responders a live
    join link pointing at a meeting that no longer exists.

    Simulated by letting the commit succeed and raising immediately afterwards,
    which is the state the lost acknowledgement leaves behind.
    """
    plugin = register_conference_plugin(response=meeting())

    real_commit = session.commit
    calls = []

    def commit_then_lose_the_ack():
        real_commit()
        calls.append(1)
        raise OperationalError("COMMIT", {}, Exception("server closed the connection"))

    monkeypatch.setattr(session, "commit", commit_then_lose_the_ack)

    with pytest.raises(OperationalError):
        create_conference(incident=incident, participants=[], db_session=session)

    assert calls, "the commit never ran, so this is not the case under test"
    assert plugin.deleted == []
    assert plugin.live == {MEETING_ID}
    # And the row really is there -- which is what makes deleting it wrong.
    monkeypatch.undo()
    assert incident.conference is not None
    assert incident.conference.resource_id == MEETING_ID


def test_a_dispatch_side_failure_leaves_no_conference_row(
    session, incident, register_conference_plugin
):
    """Neither half of the orphan survives: no provider meeting, no stray row."""
    plugin = register_conference_plugin(response=meeting(id=987654321))

    before = len(conference_rows(session))
    with pytest.raises(ValidationError):
        create_conference(incident=incident, participants=[], db_session=session)
    session.rollback()

    assert len(conference_rows(session)) == before
    assert incident.conference is None
    assert plugin.live == set()


def test_a_dispatch_side_failure_still_raises(session, incident, register_conference_plugin):
    """Established semantics, deliberately unchanged.

    A Dispatch-side failure aborts ``incident_create_resources_flow``; the
    ``background_task`` decorator logs it with a traceback and rolls the session
    back. Swallowing it here would turn "the bridge could not be created" into a
    silently missing bridge on an incident that reported success.
    """
    register_conference_plugin(response=meeting(id=987654321))

    with pytest.raises(ValidationError) as excinfo:
        create_conference(incident=incident, participants=[], db_session=session)

    assert "conference_id" in str(excinfo.value)


# --- compensation is targeted, and never masks what caused it ---------------


def test_cleanup_leaves_another_incidents_meeting_running(
    session, incident, register_conference_plugin
):
    """Deletion is by the id this call received and by nothing else.

    Asserted against what the *provider* is still holding rather than against a
    database row, because the danger is provider-side: a cleanup that searched
    the account, or matched on title or weblink, would tear down a bridge
    belonging to a live incident without touching Dispatch's tables at all.

    Deliberately no ``session.commit()`` here. Production ``commit`` ends the
    outer transaction the ``session`` fixture opened its SAVEPOINT inside, so
    rows a test commits outlive its rollback and are visible to every later
    test in the run.
    """
    plugin = register_conference_plugin(response=meeting(id=987654321))
    # A bridge another incident is already using, live at the same provider.
    plugin.live.add("someone-elses-meeting")

    with pytest.raises(ValidationError):
        create_conference(incident=incident, participants=[], db_session=session)

    assert plugin.deleted == [987654321]
    assert plugin.live == {"someone-elses-meeting"}


def test_a_cleanup_failure_does_not_replace_the_original_error(
    session, incident, register_conference_plugin
):
    """The database error is the one worth reporting; the failed delete is not."""
    plugin = register_conference_plugin(
        response=meeting(id=987654321),
        delete_error=DispatchPluginException("provider DELETE failed with HTTP 500"),
    )

    with pytest.raises(ValidationError) as excinfo:
        create_conference(incident=incident, participants=[], db_session=session)

    assert "provider DELETE failed" not in str(excinfo.value)
    assert "conference_id" in str(excinfo.value)
    assert plugin.deleted == [987654321]


def test_a_cleanup_failure_is_logged_separately(
    session, incident, register_conference_plugin, caplog
):
    """A swallowed cleanup failure leaves a leak with no trace at all."""
    register_conference_plugin(
        response=meeting(id=987654321),
        delete_error=DispatchPluginException("provider DELETE failed with HTTP 500"),
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValidationError):
            create_conference(incident=incident, participants=[], db_session=session)

    assert "987654321" in caplog.text
    assert "provider DELETE failed with HTTP 500" in caplog.text


def test_a_cleanup_that_finds_no_meeting_is_not_reported_as_a_leak(
    session, incident, register_conference_plugin, caplog
):
    """Compensation exists so no meeting outlives the failed create (issue #120).

    A provider with no such meeting has delivered exactly that, so the log must
    not claim an orphan was left behind. Asserted at ERROR because that is the
    level the leak report uses and the level an operator filters on.
    """
    register_conference_plugin(
        response=meeting(id=987654321),
        delete_error=ConferenceAlreadyGone("no such meeting"),
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValidationError):
            create_conference(incident=incident, participants=[], db_session=session)

    assert caplog.text == ""


def test_a_cleanup_that_finds_no_meeting_is_still_recorded(
    session, incident, register_conference_plugin, caplog
):
    register_conference_plugin(
        response=meeting(id=987654321),
        delete_error=ConferenceAlreadyGone("no such meeting"),
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValidationError):
            create_conference(incident=incident, participants=[], db_session=session)

    assert "987654321" in caplog.text
    # The wording, not just the id: the pre-fix ERROR line contained both the id
    # and the exception's own text, so a looser assertion passed unfixed.
    assert "needed no deleting" in caplog.text


def test_the_cleanup_failure_log_does_not_claim_the_meeting_is_unreachable(
    session, incident, register_conference_plugin, caplog
):
    """It cannot know that, and for Zoom's 3002 it is plainly false.

    "Cannot delete a meeting that has started" means the bridge is live and in
    use -- the opposite of unreachable (issue #120). What is true either way is
    that no incident references it, so nothing in Dispatch will retry.
    """
    register_conference_plugin(
        response=meeting(id=987654321),
        delete_error=DispatchPluginException(
            "Zoom deletion of the meeting failed with HTTP 400: Meeting is in progress."
        ),
    )

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValidationError):
            create_conference(incident=incident, participants=[], db_session=session)

    assert "unreachable" not in caplog.text
    assert "987654321" in caplog.text
    assert "Meeting is in progress" in caplog.text


def test_the_cleanup_log_does_not_repeat_the_weblink_or_the_passcode(
    session, incident, register_conference_plugin, caplog
):
    """A Zoom join_url carries the passcode in ``?pwd=``.

    ``delete_conference`` already refuses to log either; the compensation path
    reaches the same values and must refuse too. The meeting id is the one
    identifier worth having, and it is not a credential.
    """
    register_conference_plugin(response=meeting(id=987654321))

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValidationError):
            create_conference(incident=incident, participants=[], db_session=session)

    assert "sup3rsecret" not in caplog.text
    assert CHALLENGE not in caplog.text


def test_nothing_is_deleted_once_ownership_is_committed(
    session, incident, register_conference_plugin, monkeypatch
):
    """The compensated span ends at the commit, not at the end of the function.

    Anything after it -- the timeline entry, and whatever a future edit adds --
    is happening to a bridge Dispatch owns and can already tear down normally.
    """
    plugin = register_conference_plugin(response=meeting())

    def refuse(**kwargs):
        raise RuntimeError("the timeline write failed")

    monkeypatch.setattr("dispatch.conference.flows.event_service.log_incident_event", refuse)

    with pytest.raises(RuntimeError):
        create_conference(incident=incident, participants=[], db_session=session)

    assert plugin.deleted == []
    assert incident.conference is not None
    assert incident.conference.conference_id == MEETING_ID


# --- the retry that #114 says compounds the leak ----------------------------


def test_a_retry_after_a_compensated_failure_leaves_exactly_one_meeting(
    session, incident, register_conference_plugin
):
    """``incident_create_resources_flow`` re-runs behind ``if not incident.conference``.

    Before the fix that guard stayed true while a live bridge sat at the
    provider, so every retry added another one and only the last was ever torn
    down. With the orphan deleted, a retry converges on one.
    """
    plugin = register_conference_plugin(response=meeting(id=987654321))

    with pytest.raises(ValidationError):
        create_conference(incident=incident, participants=[], db_session=session)
    session.rollback()

    assert plugin.live == set()

    plugin.response = meeting()
    conference = create_conference(incident=incident, participants=[], db_session=session)

    assert conference is not None
    assert plugin.live == {MEETING_ID}
    assert incident.conference is conference


# --- responses the flow cannot even read an id out of ------------------------


def test_a_response_that_is_not_a_mapping_is_reported_rather_than_lost(
    session, incident, register_conference_plugin, caplog
):
    """A truthy non-dict return must still reach the compensation path.

    Reading the id with a bare subscript would raise *before* the guarded span,
    so nothing would be logged and nothing cleaned up -- the provider's meeting
    would vanish from Dispatch's view without leaving a trace. There is no id to
    delete by here, so the trace is all there is.
    """
    plugin = register_conference_plugin(response=["not", "a", "mapping"])

    with caplog.at_level(logging.ERROR):
        with pytest.raises(AttributeError):
            create_conference(incident=incident, participants=[], db_session=session)

    assert plugin.deleted == []
    assert "no id" in caplog.text.lower()


def test_a_response_without_an_id_is_reported_rather_than_lost(
    session, incident, register_conference_plugin, caplog
):
    plugin = register_conference_plugin(response={"weblink": WEBLINK, "challenge": CHALLENGE})

    with caplog.at_level(logging.ERROR):
        with pytest.raises(KeyError):
            create_conference(incident=incident, participants=[], db_session=session)

    assert plugin.deleted == []
    assert "no id" in caplog.text.lower()


def test_an_empty_provider_id_is_never_used_as_a_delete_target(
    session, incident, register_conference_plugin, caplog
):
    """``""`` is falsy but not ``None``.

    A guard written as ``is None`` would let it through and issue a delete
    against an empty id -- on Zoom that is ``DELETE /meetings/``, which is a
    different endpoint entirely.
    """
    plugin = register_conference_plugin(response={"id": "", "weblink": WEBLINK})

    with caplog.at_level(logging.ERROR):
        with pytest.raises(KeyError):
            create_conference(incident=incident, participants=[], db_session=session)

    assert plugin.deleted == []
    assert "no id" in caplog.text.lower()


# --- cleanup failures on the other entry point -------------------------------


def test_a_failing_cleanup_on_the_plugin_rejection_path_is_contained(
    session, incident, register_conference_plugin, caplog
):
    """The other caller of the cleanup returns None rather than re-raising.

    Only the persistence path proves a failed DELETE cannot escape; this path
    has different code after it, so it needs its own proof.
    """
    plugin = register_conference_plugin(
        create_error=ConferenceCreatedButUnusable("omitted join_url", resource_id=MEETING_ID),
        delete_error=DispatchPluginException("provider DELETE failed with HTTP 500"),
    )

    with caplog.at_level(logging.ERROR):
        assert create_conference(incident=incident, participants=[], db_session=session) is None

    assert plugin.deleted == [MEETING_ID]
    assert "provider DELETE failed with HTTP 500" in caplog.text
    # The original reason still reaches the responder-visible timeline.
    assert any("omitted join_url" in e.description for e in incident.events)


def test_the_cleanup_log_does_not_repeat_the_original_error_when_the_delete_fails(
    session, incident, register_conference_plugin, caplog
):
    """``log.exception`` here would print the original exception too.

    It runs inside the persistence failure's own ``except``, so Python has set
    ``__context__`` and the traceback formatter walks the chain -- and a
    SQLAlchemy DBAPI error stringifies with its bound parameters, which are the
    weblink and the challenge.
    """
    register_conference_plugin(
        response=meeting(weblink="https://example.com/\x00j/3"),
        delete_error=DispatchPluginException("provider DELETE failed with HTTP 500"),
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ValueError):
            create_conference(incident=incident, participants=[], db_session=session)

    assert "provider DELETE failed with HTTP 500" in caplog.text
    assert CHALLENGE not in caplog.text
    assert "Traceback" not in caplog.text
    session.rollback()


def test_the_meeting_is_still_deleted_when_the_session_cannot_even_be_rolled_back(
    session, incident, register_conference_plugin, monkeypatch
):
    """A database that is genuinely gone fails the flush *and* the rollback.

    That is the state the plugin hoist exists for. ``PluginInstance.instance``
    lazy-loads ``self.plugin``, so resolving the plugin during compensation
    needs a working session -- and its failure handler then dies on a
    ``self.slug`` the row does not have, turning a lost connection into an
    ``AttributeError`` that replaces the error worth reporting.

    Every other failure path happens to leave the session usable, because
    ``dispatch_owns_conference`` rolls back before it asks its question. This is
    the one that does not, so it is the only test that can see the difference.
    """
    plugin = register_conference_plugin(response=meeting(weblink="https://example.com/\x00j/4"))

    real_rollback = session.rollback

    def refuse_rollback():
        raise OperationalError("ROLLBACK", {}, Exception("server closed the connection"))

    monkeypatch.setattr(session, "rollback", refuse_rollback)

    with pytest.raises(ValueError):
        create_conference(incident=incident, participants=[], db_session=session)

    assert plugin.deleted == [MEETING_ID]
    assert plugin.live == set()

    monkeypatch.undo()
    real_rollback()


# --- two runs at once (issue #119) ------------------------------------------
#
# `incident_create_resources_flow` is operator re-runnable and guards on
# `if not incident.conference`, and `background_task` gives every invocation its
# own session -- the scheduler is a second process and the web tier hands sync
# callables to a threadpool, so two runs really can overlap. Both then pass that
# guard, both ask the provider for a meeting, and only one of the two rows is
# ever reachable again through `uselist=False`.
#
# The guard `create` grew in #114 cannot see this: it reads `incident.conference`
# from a session that loaded it as None before the other run committed, and a
# loaded scalar relationship is not re-queried. So the database has to be the one
# to refuse -- which is what these two tests are about, one at each level.
#
# Neither runs two transactions for real. The suite holds the whole run inside
# one connection and one transaction so it can be rolled back, and two sessions
# taking savepoints on that connection invalidate each other's. What is
# reproduced instead is the losing run's exact observable state: a committed row
# it cannot see, and a cached `None` where the guard looked.


def test_the_database_refuses_a_second_conference_for_one_incident(session, incident):
    """The guarantee itself, with no flow in the way.

    A Python-level check cannot close a check-then-act window that spans two
    processes, so this is the only place the invariant can actually live.
    """
    for resource_id in ("meeting-first", "meeting-second"):
        session.add(
            Conference(
                incident_id=incident.id,
                conference_id=resource_id,
                resource_id=resource_id,
                resource_type="test-conference-lifecycle",
                weblink=f"https://example.com/{resource_id}",
                conference_challenge="x",
            )
        )

    with pytest.raises(IntegrityError, match="conference_incident_id_key"):
        session.flush()

    session.rollback()


def test_a_run_that_loses_the_race_deletes_the_meeting_it_just_created(
    session, incident, register_conference_plugin
):
    """The losing run must leave the winner's bridge alone and take back its own.

    Without the constraint both rows land, `incident.conference` resolves to one
    of them, and the other run's meeting is live with nothing pointing at it --
    the same permanent provider orphan #114 exists to prevent, reached without a
    single exception being raised anywhere.

    With it, the loser's INSERT fails *inside* `create_conference`'s guarded
    span, so the compensation #114 added does the rest.
    """
    plugin = register_conference_plugin(response=meeting(id="meeting-winner"))

    # The run that got there first, committed and therefore durable past the
    # rollback the loser is about to do.
    winner = create_conference(incident=incident, participants=[], db_session=session)
    assert winner is not None

    # The loser's session: it read `incident.conference` at the flow's guard
    # before that commit landed, and nothing has expired it since. Set as a
    # *committed* value, because a dirty one would be flushed back.
    set_committed_value(incident, "conference", None)
    assert not incident.conference

    plugin.response = meeting(id="meeting-loser", weblink=WEBLINK, challenge=CHALLENGE)

    with pytest.raises(DispatchException):
        create_conference(incident=incident, participants=[], db_session=session)

    # Exactly one bridge, and it is the one that was already published.
    rows = session.query(Conference).filter(Conference.incident_id == incident.id).all()
    assert [row.resource_id for row in rows] == ["meeting-winner"]

    # And exactly one meeting at the provider.
    assert plugin.deleted == ["meeting-loser"]
    assert plugin.live == {"meeting-winner"}


def test_losing_the_race_does_not_put_the_meeting_passcode_in_the_log(
    session, incident, register_conference_plugin, caplog
):
    """The loser aborts by raising, and `background_task` logs that with a traceback.

    A raw ``IntegrityError`` stringifies with its bound parameters -- here the
    weblink and the challenge, and Zoom puts the passcode in the weblink's
    ``?pwd=``. Losing a race is routine, so it must not be a standing sink for
    the passcode of the meeting Dispatch is in the middle of deleting anyway.
    """
    plugin = register_conference_plugin(response=meeting(id="meeting-winner"))
    create_conference(incident=incident, participants=[], db_session=session)

    set_committed_value(incident, "conference", None)
    plugin.response = meeting(id="meeting-loser", weblink=WEBLINK, challenge=CHALLENGE)

    # Deliberately not pinned to a type -- the sibling test above does that.
    # Pinning it here would make this fail before it ever read the log.
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(Exception) as raised:  # noqa: B017
            create_conference(incident=incident, participants=[], db_session=session)

    # Neither the exception the flow re-raises nor anything it logged on the way.
    rendered = f"{caplog.text}{raised.value}{traceback.format_exception(raised.value)}"
    assert CHALLENGE not in rendered
    assert "pwd=" not in rendered
    assert "conference_challenge" not in rendered

"""Retry-safety of Google Calendar event creation (issue #122).

``make_call`` retries, and ``create_event`` goes through it, so a create that
reaches Google and then fails on the response was retried -- producing a second
independent calendar event, each with its own Meet bridge and its own round of
invitations, while Dispatch kept only the last.

Two things the issue asserted turned out to be wrong, and the tests here pin
the corrected understanding rather than the original description:

1. **The ``requestId`` was already stable across retries.** tenacity decorates
   ``make_call``, and ``create_event`` builds the whole body -- uuid included --
   *before* calling it, so all three attempts already sent the same
   ``requestId``. Hoisting it, the fix the issue proposed, would have changed
   nothing.
2. **``requestId`` does not deduplicate the event anyway.** Per the API
   reference it is "the client-generated unique ID for this request", scoped to
   the *conference* creation request. The documented idempotency mechanism for
   the event itself is the client-supplied event ``id``: reusing one answers
   409 rather than creating a second event, and Google states this "prevents
   duplicate event creation if the operation fails at some point after it is
   successfully executed in the Calendar backend".

So the fix supplies an event ``id`` and treats 409 as "our own earlier attempt
already succeeded", and narrows retries to errors that can actually succeed on
a retry.

Everything below drives the real ``create_event`` -> ``make_call`` -> client
path against ``fake_calendar``; neither function is mocked.
"""

import pytest
from googleapiclient.errors import HttpError

from dispatch.exceptions import ConferenceCreatedButUnusable
from dispatch.plugins.dispatch_google.calendar import plugin as calendar_plugin
from dispatch.plugins.dispatch_google.calendar.plugin import create_event

from tests.plugins.dispatch_google_calendar.fake_calendar import FakeCalendar, http_error

# Google's own error guide: these are the ones it tells you to back off and
# retry. 403 is deliberately absent -- it is retryable only for the rate-limit
# reasons, which is asserted separately.
RETRYABLE_STATUSES = [429, 500, 502, 503, 504]

# 403 carries both meanings; only the rate-limit reasons may retry.
RETRYABLE_403_REASONS = ["rateLimitExceeded", "userRateLimitExceeded"]

# Permanent: retrying cannot change the answer, and doing so only delays and
# obscures the real reason.
NON_RETRYABLE = [
    (400, "badRequest"),
    (401, "authError"),
    (403, "forbidden"),
    (403, "forbiddenForNonOrganizer"),
    (404, "notFound"),
]


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    """Strip the exponential backoff so the suite does not sleep ~6s per test.

    Only the wait is neutralised; the stop condition and the retry predicate --
    the things actually under test -- are untouched.
    """
    monkeypatch.setattr(calendar_plugin.make_call.retry, "wait", lambda *a, **kw: 0)


# --- the normal path --------------------------------------------------------


def test_a_successful_create_makes_exactly_one_api_call():
    calendar = FakeCalendar()

    event = create_event(calendar, "incident-1")

    assert len(calendar.insert_calls()) == 1, "a successful create must not retry"
    assert event["id"]
    assert len(calendar.events_store) == 1


def test_the_created_event_carries_a_client_supplied_id():
    """The id is what makes the insert idempotent, so it must be sent."""
    calendar = FakeCalendar()

    create_event(calendar, "incident-1")

    sent_id = calendar.event_ids()[0]
    assert sent_id is not None, "insert sent no event id, so a retry cannot dedupe"
    # base32hex per the event id reference: lowercase a-v and digits, 5-1024.
    assert 5 <= len(sent_id) <= 1024
    assert set(sent_id) <= set("abcdefghijklmnopqrstuv0123456789")


# --- the dangerous case: Google accepted the write, the response was lost ----


@pytest.mark.parametrize("status", RETRYABLE_STATUSES)
def test_a_failure_after_google_created_the_event_does_not_create_a_second(status):
    """The core of issue #122.

    Google commits the event, then the response fails. The retry must resolve
    to the event that already exists rather than creating an independent one.
    """
    calendar = FakeCalendar(failures=[http_error(status)], fail_after_create=True)

    event = create_event(calendar, "incident-1")

    assert len(calendar.events_store) == 1, (
        f"a {status} after create produced {len(calendar.events_store)} events"
    )
    assert event["id"] in calendar.events_store
    assert len(calendar.insert_calls()) == 2, "expected the original attempt plus one retry"


def test_a_lost_response_returns_the_event_google_actually_created():
    calendar = FakeCalendar(failures=[http_error(503)], fail_after_create=True)

    event = create_event(calendar, "incident-1")

    created_id = calendar.event_ids()[0]
    assert event["id"] == created_id, "returned an event other than the one Google created"


def test_a_connection_level_failure_after_create_does_not_duplicate():
    """Not every transport failure is an HttpError; a dropped socket is not."""
    calendar = FakeCalendar(
        failures=[ConnectionError("connection reset by peer")], fail_after_create=True
    )

    create_event(calendar, "incident-1")

    assert len(calendar.events_store) == 1


# --- request id / event id lifetime -----------------------------------------


def test_every_attempt_of_one_create_reuses_the_same_event_id():
    calendar = FakeCalendar(failures=[http_error(503), http_error(503)])

    create_event(calendar, "incident-1")

    sent = calendar.event_ids()
    assert len(sent) == 3
    # `is not None` is load-bearing: an implementation that sends no id at all
    # yields [None, None, None], which is "all identical" and would pass a
    # sameness check while having no idempotency whatsoever.
    assert all(s is not None for s in sent), f"an attempt sent no event id: {sent}"
    assert len(set(sent)) == 1, f"attempts sent different event ids: {sent}"


def test_every_attempt_of_one_create_reuses_the_same_request_id():
    calendar = FakeCalendar(failures=[http_error(503), http_error(503)])

    create_event(calendar, "incident-1")

    sent = calendar.request_ids()
    assert len(sent) == 3
    assert len(set(sent)) == 1, f"attempts sent different request ids: {sent}"


def test_independent_creates_receive_different_ids():
    """A stable id must be scoped to one logical create, not to the process."""
    first, second = FakeCalendar(), FakeCalendar()

    create_event(first, "incident-A")
    create_event(second, "incident-B")

    assert first.event_ids()[0] != second.event_ids()[0]
    assert first.request_ids()[0] != second.request_ids()[0]


def test_a_retried_create_still_does_not_collide_with_an_independent_one():
    """The two properties together: stable within, distinct across."""
    retried = FakeCalendar(failures=[http_error(503)])
    other = FakeCalendar()

    create_event(retried, "incident-A")
    create_event(other, "incident-B")

    assert len(set(retried.event_ids())) == 1, "the retried create split into two ids"
    assert retried.event_ids()[0] != other.event_ids()[0]


# --- payload stability across attempts --------------------------------------


def test_the_event_payload_is_identical_across_retries():
    """Reusing an id with a mutated payload would be the dangerous form."""
    calendar = FakeCalendar(failures=[http_error(503), http_error(503)])

    create_event(
        calendar,
        "incident-1",
        title="Situation Room",
        description="please join",
        participants=["responder@example.com"],
    )

    bodies = [c.body for c in calendar.insert_calls()]
    assert len(bodies) == 3
    for field in ("summary", "description", "attendees", "conferenceData", "id", "start", "end"):
        values = [b.get(field) for b in bodies]
        assert all(v == values[0] for v in values), f"{field} changed between attempts: {values}"


def test_the_calendar_id_is_identical_across_retries():
    calendar = FakeCalendar(failures=[http_error(503)])

    create_event(calendar, "incident-1")

    calendar_ids = [c.kwargs.get("calendarId") for c in calendar.insert_calls()]
    assert len(set(calendar_ids)) == 1


def test_attendees_are_sent_once_per_logical_create():
    """A duplicate event puts a second copy on every attendee's calendar.

    Issue #122 says each duplicate would email the participants. That
    overstates it: `create_event` never sets `sendUpdates`, whose default is
    to notify no one, so the duplicates land silently on attendees' calendars
    rather than in their inboxes. Quieter, equally wrong, and still a second
    live Meet bridge.
    """
    calendar = FakeCalendar(failures=[http_error(503)], fail_after_create=True)

    create_event(calendar, "incident-1", participants=["a@example.com", "b@example.com"])

    stored = list(calendar.events_store.values())
    assert len(stored) == 1, "a second event would appear on every attendee's calendar"
    assert stored[0]["attendees"] == [{"email": "a@example.com"}, {"email": "b@example.com"}]


# --- retry classification ---------------------------------------------------


@pytest.mark.parametrize("status", RETRYABLE_STATUSES)
def test_a_retryable_status_is_retried_and_can_then_succeed(status):
    calendar = FakeCalendar(failures=[http_error(status)])

    event = create_event(calendar, "incident-1")

    assert len(calendar.insert_calls()) == 2
    assert event["id"]


@pytest.mark.parametrize("reason", RETRYABLE_403_REASONS)
def test_a_rate_limited_403_is_retried(reason):
    """Google returns 403 for rate limiting and tells you to back off."""
    calendar = FakeCalendar(failures=[http_error(403, "Rate Limit Exceeded", reason)])

    event = create_event(calendar, "incident-1")

    assert len(calendar.insert_calls()) == 2
    assert event["id"]


@pytest.mark.parametrize("status,reason", NON_RETRYABLE)
def test_a_non_retryable_error_is_not_retried(status, reason):
    calendar = FakeCalendar(failures=[http_error(status, "nope", reason)])

    with pytest.raises(HttpError):
        create_event(calendar, "incident-1")

    assert len(calendar.insert_calls()) == 1, f"{status}/{reason} was retried"


@pytest.mark.parametrize("status,reason", NON_RETRYABLE)
def test_a_non_retryable_error_preserves_the_google_reason(status, reason):
    """The old code raised bare ``TryAgain``, losing everything Google said."""
    calendar = FakeCalendar(failures=[http_error(status, "the real reason", reason)])

    with pytest.raises(HttpError) as excinfo:
        create_event(calendar, "incident-1")

    assert excinfo.value.resp.status == status
    assert "the real reason" in str(excinfo.value)


# --- exhaustion -------------------------------------------------------------


def test_exhausted_retries_stop_at_three_attempts():
    calendar = FakeCalendar(failures=[http_error(503), http_error(503), http_error(503)])

    # Specifically not `RetryError`: `reraise=True` means the caller gets the
    # failure Google actually reported, which is the reporting half of #122.
    with pytest.raises(HttpError):
        create_event(calendar, "incident-1")

    assert len(calendar.insert_calls()) == 3


def test_exhausted_retries_reuse_one_id_throughout():
    calendar = FakeCalendar(failures=[http_error(503), http_error(503), http_error(503)])

    with pytest.raises(HttpError):
        create_event(calendar, "incident-1")

    sent = calendar.event_ids()
    assert all(s is not None for s in sent), f"an attempt sent no event id: {sent}"
    assert len(set(sent)) == 1


def test_exhausted_retries_preserve_the_underlying_google_failure():
    calendar = FakeCalendar(
        failures=[
            http_error(503, "backend hiccup"),
            http_error(503, "backend hiccup"),
            http_error(503, "backend hiccup"),
        ]
    )

    with pytest.raises(Exception) as excinfo:
        create_event(calendar, "incident-1")

    assert "backend hiccup" in str(excinfo.value) or "backend hiccup" in str(
        excinfo.value.__cause__
    )


# --- when the recovery read itself fails ------------------------------------


def _calendar_whose_get_fails(failure):
    """A calendar that stores the event, loses the insert response, then fails
    the recovery read -- so Google is holding an event we cannot read back."""

    class RecoveryFails(FakeCalendar):
        def _get(self, **kwargs):
            raise failure

    return RecoveryFails(failures=[http_error(503)], fail_after_create=True)


@pytest.mark.parametrize(
    "failure",
    [http_error(404, "Not Found", "notFound"), http_error(403, "Forbidden", "forbidden")],
)
def test_a_failed_recovery_read_still_reports_the_stranded_event(failure):
    """Google told us the event exists (409). If we then cannot read it back,
    the caller must still learn the id, or `create_conference` files it under
    "the provider gave us nothing" and the event is orphaned for good."""
    calendar = _calendar_whose_get_fails(failure)

    with pytest.raises(ConferenceCreatedButUnusable) as excinfo:
        create_event(calendar, "incident-1")

    assert excinfo.value.resource_id, "the stranded event's id was not reported"
    assert excinfo.value.resource_id in calendar.events_store


def test_a_recovery_read_failing_below_the_api_still_reports_the_event():
    """The recovery read retries too, and exhausting it raises whatever the
    transport gave -- not an ``HttpError``. Guarding only ``HttpError`` there
    lets it escape and strands the very event this path exists to rescue."""
    calendar = _calendar_whose_get_fails(ConnectionError("connection reset by peer"))

    with pytest.raises(ConferenceCreatedButUnusable) as excinfo:
        create_event(calendar, "incident-1")

    assert excinfo.value.resource_id in calendar.events_store


def test_a_failed_recovery_read_reports_the_id_google_actually_holds():
    calendar = _calendar_whose_get_fails(http_error(404, "Not Found", "notFound"))

    with pytest.raises(ConferenceCreatedButUnusable) as excinfo:
        create_event(calendar, "incident-1")

    assert excinfo.value.resource_id == calendar.event_ids()[0]


# --- exhaustion while Google is holding the event ---------------------------


class _GatewayFailsAfterCommit(FakeCalendar):
    """The first insert reaches the backend and commits; every attempt then
    fails at a gateway, so the client never sees the 409 that would reveal it.

    This is the one path where the 409 recovery cannot fire: attempts 2 and 3
    are rejected before Google ever looks at the event id.
    """

    def __init__(self, attempts_that_commit=(1,)):
        super().__init__()
        self.attempt = 0
        self.attempts_that_commit = attempts_that_commit

    def _insert(self, **kwargs):
        self.attempt += 1
        if self.attempt in self.attempts_that_commit:
            self._store(kwargs.get("body") or {})
        raise http_error(502, "bad gateway")


def test_exhaustion_finds_an_event_google_committed_behind_a_failing_gateway():
    """Every attempt 5xx'd, but Google is holding the event. Declaring "nothing
    was created" would strand it with no Dispatch record at all."""
    calendar = _GatewayFailsAfterCommit()

    event = create_event(calendar, "incident-1")

    assert len(calendar.events_store) == 1
    assert event["id"] == calendar.event_ids()[0]


def test_exhaustion_with_nothing_committed_still_raises_the_google_error():
    """The probe must not invent a success when Google really has nothing."""
    calendar = _GatewayFailsAfterCommit(attempts_that_commit=())

    with pytest.raises(HttpError) as excinfo:
        create_event(calendar, "incident-1")

    assert excinfo.value.resp.status == 502
    assert calendar.events_store == {}


# --- the other calendar operations still work -------------------------------


def test_get_event_still_reads_through_make_call():
    calendar = FakeCalendar()
    created = create_event(calendar, "incident-1")

    assert calendar_plugin.get_event(calendar, created["id"])["id"] == created["id"]


def test_delete_event_still_works():
    calendar = FakeCalendar()
    created = create_event(calendar, "incident-1")

    calendar_plugin.delete_event(calendar, created["id"])

    assert created["id"] not in calendar.events_store


def test_a_non_retryable_error_on_a_read_is_not_retried():
    calendar = FakeCalendar()

    with pytest.raises(HttpError):
        calendar_plugin.get_event(calendar, "no-such-event")

    assert len([c for c in calendar.calls if c.method == "get"]) == 1

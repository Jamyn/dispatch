"""A Calendar delete answered not-found means the event is already gone (issue #120).

``delete_event`` let ``googleapiclient``'s ``HttpError`` straight out, so the
conference flow reported an event Calendar does not hold as a leaked bridge.

The conversion lives in ``delete_event`` and deliberately **not** in
``make_call``: ``create_event`` catches ``HttpError`` itself to recover its own
409 (issue #122), and a 404 from ``get``/``update`` is not this. The last two
tests are what hold that line.
"""

import pytest

from dispatch.exceptions import ConferenceAlreadyGone, DispatchPluginException
from dispatch.plugins.dispatch_google.calendar.plugin import (
    add_participant,
    create_event,
    delete_event,
    get_event,
)

from tests.plugins.dispatch_google_calendar.fake_calendar import FakeCalendar, http_error

EVENT_ID = "abcdef1234567890"


def stored_event(calendar: FakeCalendar) -> str:
    """Put one real event on the calendar and return its id."""
    return create_event(calendar, "incident-1")["id"]


def test_deleting_an_event_calendar_does_not_hold_reports_it_as_already_gone():
    calendar = FakeCalendar()

    with pytest.raises(ConferenceAlreadyGone):
        delete_event(calendar, EVENT_ID)


def test_a_410_reports_the_event_as_already_gone_too():
    """Calendar answers 410 Gone for an event it remembers deleting."""
    calendar = FakeCalendar(delete_failures=[http_error(410, "Resource has been deleted.")])

    with pytest.raises(ConferenceAlreadyGone):
        delete_event(calendar, EVENT_ID)


def test_already_gone_is_still_a_plugin_exception():
    """Subclassing is what keeps every caller that does not opt in unchanged."""
    calendar = FakeCalendar()

    with pytest.raises(DispatchPluginException):
        delete_event(calendar, EVENT_ID)


def test_deleting_an_event_calendar_does_hold_still_succeeds():
    calendar = FakeCalendar()
    event_id = stored_event(calendar)

    delete_event(calendar, event_id)

    assert event_id not in calendar.events_store


@pytest.mark.parametrize("status", [400, 401, 403])
def test_any_other_delete_failure_is_still_an_ordinary_failure(status):
    calendar = FakeCalendar(delete_failures=[http_error(status, "nope", "forbidden")])

    with pytest.raises(Exception) as excinfo:
        delete_event(calendar, EVENT_ID)

    assert not isinstance(excinfo.value, ConferenceAlreadyGone)


def test_a_404_on_a_read_is_not_reclassified():
    """``make_call`` is shared; converting there would catch this one too."""
    calendar = FakeCalendar()

    with pytest.raises(Exception) as excinfo:
        get_event(calendar, EVENT_ID)

    assert not isinstance(excinfo.value, ConferenceAlreadyGone)


def test_a_404_on_a_roster_update_is_not_reclassified():
    calendar = FakeCalendar()

    with pytest.raises(Exception) as excinfo:
        add_participant(calendar, EVENT_ID, "responder@example.com")

    assert not isinstance(excinfo.value, ConferenceAlreadyGone)


def test_create_still_recovers_its_own_duplicate():
    """The 409 path runs through ``make_call`` and must be untouched (issue #122)."""
    calendar = FakeCalendar(failures=[http_error(503)], fail_after_create=True)

    event = create_event(calendar, "incident-1")

    assert event["id"] in calendar.events_store

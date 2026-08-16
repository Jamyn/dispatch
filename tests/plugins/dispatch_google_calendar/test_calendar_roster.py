"""Roster updates against an event whose attendee list is absent.

`attendees[]` is optional on Google's Events resource, so an event that has no
attendees is validly represented with the key missing entirely -- not with an
empty list. `add_participant` and `remove_participant` both subscripted it
directly, so a roster update against such an event raised
`KeyError: 'attendees'`.

The tests seed the absent-key form directly rather than reaching it by removing
the last attendee. That keeps them resting on what the API reference actually
documents -- the field is optional -- rather than on an assumption about
exactly when Google chooses to omit it, which is not stated in the reference
and which no test here can observe without a live account.

`conference/flows.py` catches the failure and writes it to the incident
timeline, so the symptom is a repeating line reading `Reason: 'attendees'` and
a responder who is never invited -- Google Calendar being the one provider that
actually sends invitations.
"""

import pytest

from dispatch.plugins.dispatch_google.calendar.plugin import (
    add_participant,
    remove_participant,
)

from tests.plugins.dispatch_google_calendar.fake_calendar import FakeCalendar

EVENT_ID = "incident-bridge-event"
RESPONDER = "responder@example.com"


def seed(calendar: FakeCalendar, attendees=None) -> None:
    """Store an event, omitting `attendees` entirely when none is given."""
    event = {"id": EVENT_ID, "summary": "Situation Room"}
    if attendees is not None:
        event["attendees"] = attendees
    calendar.events_store[EVENT_ID] = event


def attendee_emails(calendar: FakeCalendar) -> list[str]:
    return [a["email"] for a in calendar.events_store[EVENT_ID].get("attendees", [])]


def test_add_participant_to_an_event_with_no_attendee_list():
    """The reported failure: adding to a bridge whose roster key is absent."""
    calendar = FakeCalendar()
    seed(calendar)

    add_participant(calendar, EVENT_ID, RESPONDER)

    assert attendee_emails(calendar) == [RESPONDER]


def test_remove_participant_from_an_event_with_no_attendee_list():
    """The same subscript, on the removal side.

    Removing from an event that lists nobody is a no-op rather than an error --
    the participant is already not on the roster, which is what the caller
    wanted.
    """
    calendar = FakeCalendar()
    seed(calendar)

    remove_participant(calendar, EVENT_ID, RESPONDER)

    assert attendee_emails(calendar) == []


def test_a_bridge_stays_open_after_its_last_attendee_leaves():
    """The latch the issue describes, driven end to end.

    Removing the last responder and then adding another is the sequence that
    left the bridge permanently closed. Asserting on the round trip rather than
    on one call is what makes this a regression test for the reported behaviour
    rather than for one subscript.
    """
    calendar = FakeCalendar()
    seed(calendar, [{"email": "first@example.com"}])

    remove_participant(calendar, EVENT_ID, "first@example.com")
    # Whatever the provider hands back for an empty roster -- omitted key or
    # empty list -- the next add has to work. Force the harder of the two.
    calendar.events_store[EVENT_ID].pop("attendees", None)

    add_participant(calendar, EVENT_ID, RESPONDER)

    assert attendee_emails(calendar) == [RESPONDER]


@pytest.mark.parametrize("attendees", [None, []], ids=["key-absent", "empty-list"])
def test_both_empty_roster_representations_are_accepted(attendees):
    """Neither representation may fail.

    The reference documents the field as optional without saying which form an
    empty roster takes, so the plugin has to accept both.
    """
    calendar = FakeCalendar()
    seed(calendar, attendees)

    add_participant(calendar, EVENT_ID, RESPONDER)

    assert attendee_emails(calendar) == [RESPONDER]

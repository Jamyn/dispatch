"""Roster membership matching: who the plugin considers already listed.

`add_participant` appended unconditionally and `remove_participant` compared
addresses exactly, so a bridge duplicated every responder and a removal spelled
differently from the add silently left them on the roster (issue #128).

Duplication is not an edge case on this plugin: `incident_create_resources`
seeds the bridge with the resolved responders and then walks the same people
through `incident_add_or_reactivate_participant_flow`, which adds each of them
again.

Whether Google itself deduplicates attendees on `events.update` is unverified --
no account was available -- so these assert on what Dispatch sends, which the
fake stores verbatim. The case-insensitive removal is a defect either way.
"""

from dispatch.plugins.dispatch_google.calendar.plugin import (
    add_participant,
    create_event,
    remove_participant,
)

from tests.plugins.dispatch_google_calendar.fake_calendar import FakeCalendar

EVENT_ID = "incident-bridge-event"
RESPONDER = "responder@example.com"


def seed(calendar: FakeCalendar, attendees) -> None:
    calendar.events_store[EVENT_ID] = {
        "id": EVENT_ID,
        "summary": "Situation Room",
        "attendees": attendees,
    }


def attendee_emails(calendar: FakeCalendar) -> list[str]:
    return [a.get("email") for a in calendar.events_store[EVENT_ID].get("attendees", [])]


def update_count(calendar: FakeCalendar) -> int:
    return len([c for c in calendar.calls if c.method == "update"])


def test_adding_someone_already_on_the_roster_does_not_duplicate_them():
    calendar = FakeCalendar()
    seed(calendar, [{"email": RESPONDER}])

    add_participant(calendar, EVENT_ID, RESPONDER)

    assert attendee_emails(calendar) == [RESPONDER]
    assert update_count(calendar) == 0


def test_adding_a_case_differing_spelling_does_not_duplicate_them():
    calendar = FakeCalendar()
    seed(calendar, [{"email": "responder@example.com"}])

    add_participant(calendar, EVENT_ID, "Responder@Example.COM")

    assert attendee_emails(calendar) == ["responder@example.com"]
    assert update_count(calendar) == 0


def test_adding_someone_new_still_appends_them():
    """The guard must not swallow a genuine add."""
    calendar = FakeCalendar()
    seed(calendar, [{"email": "first@example.com"}])

    add_participant(calendar, EVENT_ID, RESPONDER)

    assert attendee_emails(calendar) == ["first@example.com", RESPONDER]
    assert update_count(calendar) == 1


def test_removing_a_case_differing_spelling_takes_them_off():
    calendar = FakeCalendar()
    seed(calendar, [{"email": "responder@example.com"}, {"email": "other@example.com"}])

    remove_participant(calendar, EVENT_ID, "Responder@Example.COM")

    assert attendee_emails(calendar) == ["other@example.com"]
    assert update_count(calendar) == 1


def test_removing_someone_absent_sends_no_update():
    """A retried removal is a no-op rather than a rewrite of the whole roster."""
    calendar = FakeCalendar()
    seed(calendar, [{"email": "other@example.com"}])

    remove_participant(calendar, EVENT_ID, RESPONDER)

    assert attendee_emails(calendar) == ["other@example.com"]
    assert update_count(calendar) == 0


def test_an_attendee_google_sends_without_an_email_is_not_matched_or_dropped():
    """Matching reads `email` with a default, so an entry lacking it is left be.

    `attendees[].email` is required to *add* someone, so this is about what a
    read may hand back; the conservative answer is "not this participant".
    """
    calendar = FakeCalendar()
    seed(calendar, [{"resource": True}])

    remove_participant(calendar, EVENT_ID, RESPONDER)
    add_participant(calendar, EVENT_ID, RESPONDER)

    assert calendar.events_store[EVENT_ID]["attendees"] == [
        {"resource": True},
        {"email": RESPONDER},
    ]


def test_the_incident_creation_sequence_lists_each_responder_once():
    """The reported path, driven end to end.

    Seeding the bridge and then adding the same responders is what incident
    resource creation does on every incident.
    """
    calendar = FakeCalendar()
    responders = ["alice@example.com", "bob@example.com"]
    event = create_event(calendar, "an incident", participants=responders)
    event_id = event["id"]

    for responder in responders:
        add_participant(calendar, event_id, responder)

    stored = calendar.events_store[event_id]
    assert [a["email"] for a in stored["attendees"]] == responders

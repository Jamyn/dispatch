def test_get(session, conference):
    from dispatch.conference.service import get

    test_conference = get(db_session=session, conference_id=conference.id)
    assert test_conference.id == conference.id
    assert test_conference.conference_challenge == conference.conference_challenge


def test_get_by_resource_id(session, conference):
    from dispatch.conference.service import get_by_resource_id

    test_conference = get_by_resource_id(db_session=session, resource_id=conference.resource_id)

    assert test_conference.resource_id == conference.resource_id
    assert test_conference.conference_challenge == conference.conference_challenge


def test_get_by_incident_id(session, conference):
    from dispatch.conference.service import get_by_incident_id

    test_conference = get_by_incident_id(db_session=session, incident_id=conference.incident.id)

    assert test_conference.incident.id == conference.incident.id
    assert test_conference.conference_challenge == conference.conference_challenge


def test_get_all(session, conferences):
    """The test should not rely on the conferences created earlier, to pass.
    Therefore, we pass "conferences" as an argument, to manually create several in the DB.
    """
    from dispatch.conference.service import get_all

    test_conferences = get_all(db_session=session).all()
    assert test_conferences


def test_create(session):
    from dispatch.conference.service import create
    from dispatch.conference.models import ConferenceCreate

    resource_id = "000000"
    resource_type = "resourcetype"
    weblink = "https://www.example.com"
    conference_id = "12345"
    conference_challenge = "a0v0a0v9a"

    conference_in = ConferenceCreate(
        resource_id=resource_id,
        resource_type=resource_type,
        weblink=weblink,
        conference_id=conference_id,
        conference_challenge=conference_challenge,
    )
    conference = create(db_session=session, conference_in=conference_in)
    assert conference
    assert conference.resource_id == "000000"
    assert conference.resource_type == "resourcetype"
    assert conference.weblink == "https://www.example.com"
    assert conference.conference_id == "12345"
    assert conference.conference_challenge == "a0v0a0v9a"


def test_create_with_an_incident_owns_the_row_from_the_first_commit(session, incident):
    """Issue #114: a conference committed with a NULL incident_id is already lost.

    Nothing reads `Conference.incident_id` directly -- teardown, the bookmarks
    and the participant flow all go through `incident.conference` -- so a row
    that lands unattached is unreachable even though it is right there in the
    table. Attaching it in `create` is what makes the row and the link one
    transaction, so no failure can commit the first without the second.
    """
    from dispatch.conference.service import create
    from dispatch.conference.models import ConferenceCreate

    conference_in = ConferenceCreate(
        resource_id="000001",
        resource_type="resourcetype",
        weblink="https://www.example.com",
        conference_id="12346",
        conference_challenge="a0v0a0v9a",
    )

    conference = create(db_session=session, conference_in=conference_in, incident=incident)

    assert conference.incident_id == incident.id
    assert incident.conference is conference


def test_create_refuses_an_incident_that_already_has_a_conference(session, incident):
    """Silently replacing one is how a provider meeting gets stranded.

    ``Incident.conference`` cascades ``delete-orphan``, so assigning a second
    conference deletes the first row on commit -- with no exception anywhere and
    no provider-side delete, leaving exactly the permanent orphan #114 exists to
    prevent. Raising instead puts the caller inside ``create_conference``'s
    guarded span, where the meeting it just created is compensated away.
    """
    import pytest

    from dispatch.conference.service import create
    from dispatch.conference.models import ConferenceCreate
    from dispatch.exceptions import DispatchException

    first = create(
        db_session=session,
        conference_in=ConferenceCreate(resource_id="first-meeting", conference_id="first-meeting"),
        incident=incident,
    )

    with pytest.raises(DispatchException):
        create(
            db_session=session,
            conference_in=ConferenceCreate(
                resource_id="second-meeting", conference_id="second-meeting"
            ),
            incident=incident,
        )

    session.rollback()
    assert incident.conference.id == first.id
    assert incident.conference.resource_id == "first-meeting"

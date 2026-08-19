"""The endpoints behind a case's custom timeline entries.

Every one of these views only *queues* a background task; the work lands after
the response. Calling the view and asserting it did not raise therefore proves
nothing about the timeline -- the task has to be run before anything can be
checked, which is what these do.

The timeline is the case's audit record, so what matters is that the entry is
actually there afterwards, carries who made it, and is really gone when deleted.
"""

from datetime import datetime

import pytest
from starlette.background import BackgroundTasks

from dispatch.auth.models import DispatchUser
from dispatch.case.models import Case
from dispatch.event import service as event_service
from dispatch.event.models import EventCreateMinimal, EventUpdate


# The background task builds its own session against
# `dispatch_organization_<slug>`, and the suite has only the default schema, so
# the views must be called with that slug or the task looks for the case in a
# schema that was never created.
ORGANIZATION = "default"


@pytest.fixture
def author(session, case: Case, user: DispatchUser):
    """The case participant creating the entries.

    `log_case_event` stamps the entry with the author's individual contact in
    the case's project, so a user who is not a participant there has no name to
    attribute it to.
    """
    from dispatch.participant import flows as participant_flows
    from dispatch.participant_role.models import ParticipantRoleType

    participant_flows.add_participant(
        user.email, case, session, roles=[ParticipantRoleType.assignee]
    )
    session.commit()
    return user


def run_queued(background_tasks: BackgroundTasks, session):
    """Run what the view scheduled, on the test's session.

    The view returns before any of this happens, so a test that stops at the
    call has only checked that the task was queued. `@background_task` would
    otherwise build its own session from the engine bound at import time, which
    is not the connection this test's rows live on -- supplying one is what it
    already does for callers that have a session to hand.
    """
    for task in background_tasks.tasks:
        task.func(*task.args, **{**task.kwargs, "db_session": session})


def timeline(session, case: Case):
    session.expire_all()
    return event_service.get_by_case_id(db_session=session, case_id=case.id)


def test_creating_a_custom_event_puts_it_on_the_timeline(session, case: Case, author: DispatchUser):
    """Given a custom event, when it is created, then it appears on the case timeline."""
    from dispatch.case.views import create_custom_event

    background_tasks = BackgroundTasks()
    create_custom_event(
        db_session=session,
        organization=ORGANIZATION,
        case_id=case.id,
        current_case=case,
        event_in=EventCreateMinimal(
            source="Case Participant",
            description="a description only this test uses",
            started_at=datetime.utcnow(),
            type="Custom event",
            details={},
        ),
        current_user=author,
        background_tasks=background_tasks,
    )
    run_queued(background_tasks, session)

    descriptions = [event.description for event in timeline(session, case)]
    assert "a description only this test uses" in descriptions


def test_creating_a_custom_event_records_who_made_it(session, case: Case, author: DispatchUser):
    """Given a custom event, when it is created, then the author is recorded on it.

    The timeline is an audit record; an entry nobody is attributed to is worth
    much less during a review.
    """
    from dispatch.case.views import create_custom_event

    background_tasks = BackgroundTasks()
    create_custom_event(
        db_session=session,
        organization=ORGANIZATION,
        case_id=case.id,
        current_case=case,
        event_in=EventCreateMinimal(
            source="Case Participant",
            description="an attributed entry",
            started_at=datetime.utcnow(),
            type="Custom event",
            details={},
        ),
        current_user=author,
        background_tasks=background_tasks,
    )
    run_queued(background_tasks, session)

    created = next(e for e in timeline(session, case) if e.description == "an attributed entry")
    assert created.details["created_by"] == author.email


def test_updating_a_custom_event_changes_what_the_timeline_says(
    session, case: Case, author: DispatchUser
):
    """Given an existing entry, when it is updated, then the timeline shows the new text."""
    from dispatch.case.views import update_custom_event

    event = event_service.log_case_event(
        db_session=session,
        source="Case Participant",
        description="before the correction",
        case_id=case.id,
        started_at=datetime.utcnow(),
        type="Custom event",
    )

    now = datetime.utcnow()
    background_tasks = BackgroundTasks()
    update_custom_event(
        db_session=session,
        organization=ORGANIZATION,
        case_id=case.id,
        current_case=case,
        event_in=EventUpdate(
            uuid=event.uuid,
            source="Case Participant",
            description="after the correction",
            started_at=now,
            ended_at=now,
            type="Custom event",
            details={},
            owner="",
            pinned=False,
        ),
        current_user=author,
        background_tasks=background_tasks,
    )
    run_queued(background_tasks, session)

    descriptions = [e.description for e in timeline(session, case)]
    assert "after the correction" in descriptions
    assert "before the correction" not in descriptions


def test_updating_a_custom_event_keeps_the_source_it_came_from(
    session, case: Case, author: DispatchUser
):
    """Given an entry from Slack, when its text is corrected, then its source is unchanged.

    The source says where the entry came from. An edit changes the wording, not
    the provenance.
    """
    original_source = "Slack message from John Doe"
    event = event_service.log_case_event(
        db_session=session,
        source=original_source,
        description="Initial description",
        case_id=case.id,
        started_at=datetime.utcnow(),
        type="Custom event",
    )

    now = datetime.utcnow()
    event_service.update_case_event(
        db_session=session,
        event_in=EventUpdate(
            uuid=event.uuid,
            source=original_source,
            description="Updated description",
            started_at=now,
            ended_at=now,
            type="Custom event",
            details={},
            owner="",
            pinned=False,
        ),
    )

    updated = event_service.get_by_uuid(db_session=session, uuid=event.uuid)
    assert updated.source == original_source
    assert updated.description == "Updated description"


def test_deleting_a_custom_event_takes_it_off_the_timeline(
    session, case: Case, author: DispatchUser
):
    """Given an entry, when it is deleted, then it is gone from the timeline."""
    from dispatch.case.views import delete_custom_event

    event = event_service.log_case_event(
        db_session=session,
        source="Case Participant",
        description="an entry that gets removed",
        case_id=case.id,
        started_at=datetime.utcnow(),
        type="Custom event",
    )
    assert "an entry that gets removed" in [e.description for e in timeline(session, case)]

    background_tasks = BackgroundTasks()
    delete_custom_event(
        db_session=session,
        organization=ORGANIZATION,
        case_id=case.id,
        current_case=case,
        event_uuid=str(event.uuid),
        current_user=author,
        background_tasks=background_tasks,
    )
    run_queued(background_tasks, session)

    assert "an entry that gets removed" not in [e.description for e in timeline(session, case)]

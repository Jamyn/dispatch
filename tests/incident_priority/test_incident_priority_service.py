def test_get(session, incident_priority):
    from dispatch.incident.priority.service import get

    t_incident_priority = get(db_session=session, incident_priority_id=incident_priority.id)
    assert t_incident_priority.id == incident_priority.id


def test_get_by_name(session, incident_priority):
    from dispatch.incident.priority.service import get_by_name

    t_incident_priority = get_by_name(
        db_session=session, project_id=incident_priority.project.id, name=incident_priority.name
    )
    assert t_incident_priority.name == incident_priority.name


def test_get_all(session, project, incident_priorities):
    from dispatch.incident.priority.service import get_all

    t_incident_priorities = get_all(
        db_session=session, project_id=incident_priorities[0].project.id
    ).all()
    assert t_incident_priorities


def test_create(session, project):
    from dispatch.incident.priority.service import create
    from dispatch.incident.priority.models import IncidentPriorityCreate

    name = "XXX"
    description = "XXXXXX"

    incident_priority_in = IncidentPriorityCreate(
        name=name,
        description=description,
        project=project,
    )
    incident_priority = create(db_session=session, incident_priority_in=incident_priority_in)
    assert incident_priority


def test_update(session, incident_priority):
    from dispatch.incident.priority.service import update
    from dispatch.incident.priority.models import IncidentPriorityUpdate

    name = "Updated incident priority name"

    incident_priority_in = IncidentPriorityUpdate(name=name)
    incident_priority = update(
        db_session=session,
        incident_priority=incident_priority,
        incident_priority_in=incident_priority_in,
    )
    assert incident_priority.name == name


def test_delete(session, incident_priority):
    from dispatch.incident.priority.service import delete, get

    delete(db_session=session, incident_priority_id=incident_priority.id)
    assert not get(db_session=session, incident_priority_id=incident_priority.id)


def test_get_by_name_or_default__name(session, incident_priority):
    from dispatch.incident.priority.models import IncidentPriorityRead
    from dispatch.incident.priority.service import get_by_name_or_default

    incident_priority_in = IncidentPriorityRead.from_orm(incident_priority)
    result = get_by_name_or_default(
        db_session=session,
        project_id=incident_priority.project.id,
        incident_priority_in=incident_priority_in,
    )
    assert result.id == incident_priority.id


def test_get_by_name_or_default__default(session, incident_priority):
    from dispatch.incident.priority.models import IncidentPriorityRead
    from dispatch.incident.priority.service import get_by_name_or_default

    # Ensure only one default incident priority
    for ip in session.query(type(incident_priority)).all():
        ip.default = False
    incident_priority.default = True
    session.commit()
    # Pass an IncidentPriorityRead with a non-existent name and dummy id > 0
    incident_priority_in = IncidentPriorityRead(
        id=99999, name="nonexistent", project=incident_priority.project
    )
    result = get_by_name_or_default(
        db_session=session,
        project_id=incident_priority.project.id,
        incident_priority_in=incident_priority_in,
    )
    assert result.id == incident_priority.id


def test_an_unknown_priority_name_is_reported_as_a_validation_error(session, incident_priority):
    """Given a name no priority has, when resolving it, then a 422-able error is raised.

    `ExceptionMiddleware` renders a pydantic ValidationError as a 422 carrying
    `.errors()`; anything else reaches the caller as an opaque 500.
    """
    import pytest
    from pydantic import ValidationError

    from dispatch.incident.priority.models import IncidentPriorityRead
    from dispatch.incident.priority.service import get_by_name_or_raise

    priority_in = IncidentPriorityRead.from_orm(incident_priority)
    priority_in.name = "no such priority"

    with pytest.raises(ValidationError) as exc_info:
        get_by_name_or_raise(
            db_session=session,
            project_id=incident_priority.project.id,
            incident_priority_in=priority_in,
        )

    assert "Incident priority not found." in str(exc_info.value.errors())

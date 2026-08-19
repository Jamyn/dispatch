def test_get(session, service):
    from dispatch.service.service import get

    t_service = get(db_session=session, service_id=service.id)
    assert t_service.id == service.id


def test_create(session, project):
    from dispatch.service.service import create
    from dispatch.service.models import ServiceCreate

    name = "createName"
    service_in = ServiceCreate(
        name=name,
        project=project,
    )

    service = create(db_session=session, service_in=service_in)
    assert name == service.name


def test_update(session, service):
    from dispatch.service.service import update
    from dispatch.service.models import ServiceUpdate

    name = "Updated Name"
    service_in = ServiceUpdate(name=name)

    service = update(db_session=session, service=service, service_in=service_in)
    assert service.name == name


def test_delete(session, service):
    from dispatch.service.service import delete, get

    delete(db_session=session, service_id=service.id)
    assert not get(db_session=session, service_id=service.id)


def test_an_unknown_service_name_is_reported_as_a_validation_error(session, service):
    """Given a name no service has, when resolving it, then a 422-able error is raised.

    `ExceptionMiddleware` renders a pydantic ValidationError as a 422 carrying
    `.errors()`; anything else reaches the caller as an opaque 500.
    """
    import pytest
    from pydantic import ValidationError

    from dispatch.service.models import ServiceRead
    from dispatch.service.service import get_by_name_or_raise

    service_in = ServiceRead.from_orm(service)
    service_in.name = "no such service"

    with pytest.raises(ValidationError) as exc_info:
        get_by_name_or_raise(
            db_session=session, project_id=service.project.id, service_in=service_in
        )

    assert "Service not found" in str(exc_info.value.errors())


def test_an_unknown_external_id_is_reported_as_a_validation_error(session, service):
    """Given an external id no service has, when resolving it, then a 422-able error is raised.

    This one also read `service.external_id` off the `None` it had just checked
    for, so it raised AttributeError before reaching the error it meant to build.
    """
    import pytest
    from pydantic import ValidationError

    from dispatch.service.models import ServiceRead
    from dispatch.service.service import get_by_external_id_and_project_id_or_raise

    service_in = ServiceRead.from_orm(service)
    service_in.external_id = "no-such-external-id"

    with pytest.raises(ValidationError) as exc_info:
        get_by_external_id_and_project_id_or_raise(
            db_session=session, project_id=service.project.id, service_in=service_in
        )

    assert "Service not found." in str(exc_info.value.errors())

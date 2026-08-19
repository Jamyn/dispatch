import pytest
from pydantic import ValidationError

from dispatch.organization.models import OrganizationRead
from dispatch.organization.service import (
    get_by_name_or_raise,
    get_by_slug_or_raise,
    get_default_or_raise,
)


def test_get(session, organization):
    from dispatch.organization.service import get

    t_organization = get(db_session=session, organization_id=organization.id)
    assert t_organization.id == organization.id


def test_get_all(session, organizations):
    from dispatch.organization.service import get_all

    t_organizations = get_all(db_session=session).all()
    assert t_organizations


def test_create(session):
    from dispatch.organization.service import create
    from dispatch.organization.models import OrganizationCreate

    name = "name"
    description = "description"
    default = True
    banner_enabled = True
    banner_color = "red"
    banner_text = "banner"

    organization_in = OrganizationCreate(
        name=name,
        description=description,
        default=default,
        banner_enabled=banner_enabled,
        banner_color=banner_color,
        banner_text=banner_text,
    )
    organization = create(db_session=session, organization_in=organization_in)
    assert organization


def test_update(session, organization):
    from dispatch.organization.service import update
    from dispatch.organization.models import OrganizationUpdate

    description = "Updated description"

    organization_in = OrganizationUpdate(
        description=description,
    )

    organization = update(
        db_session=session,
        organization=organization,
        organization_in=organization_in,
    )

    assert organization.description == description


def test_delete(session, organization):
    from dispatch.organization.service import delete, get

    delete(db_session=session, organization_id=organization.id)
    assert not get(db_session=session, organization_id=organization.id)


def test_get_by_name_or_default__name(session, organization):
    from dispatch.organization.models import OrganizationRead
    from dispatch.organization.service import get_by_name_or_default

    organization_in = OrganizationRead.from_orm(organization)
    result = get_by_name_or_default(db_session=session, organization_in=organization_in)
    assert result.id == organization.id


def test_get_by_name_or_default__default(session, organization):
    from dispatch.organization.models import OrganizationRead
    from dispatch.organization.service import get_by_name_or_default

    # Ensure only one default organization
    for org in session.query(type(organization)).all():
        org.default = False
    organization.default = True
    session.commit()
    # Pass an OrganizationRead with a non-existent name
    organization_in = OrganizationRead(name="nonexistent")
    result = get_by_name_or_default(db_session=session, organization_in=organization_in)
    assert result.id == organization.id


# --- The `_or_raise` helpers ---------------------------------------------
#
# `ExceptionMiddleware` turns a pydantic `ValidationError` into a 422 carrying
# `.errors()`. Anything else reaches the caller as an opaque 500, so these
# assert the error is both raised and renderable, not merely that the lookup
# failed.


def test_an_unknown_slug_is_reported_as_a_validation_error(session, organization):
    """Given a slug no organization has, when resolving it, then a 422-able error is raised.

    The slug is well-formed, so it reaches the lookup rather than being turned
    away by `OrganizationRead`'s own pattern check.
    """
    organization_in = OrganizationRead(name="no_such_org", slug="no_such_org")

    with pytest.raises(ValidationError) as exc_info:
        get_by_slug_or_raise(db_session=session, organization_in=organization_in)

    assert "Organization not found." in str(exc_info.value.errors())


def test_an_unknown_name_is_reported_as_a_validation_error(session, organization):
    """Given a name no organization has, when resolving it, then a 422-able error is raised."""
    organization_in = OrganizationRead(name="no_such_org", slug="no_such_org")

    with pytest.raises(ValidationError) as exc_info:
        get_by_name_or_raise(db_session=session, organization_in=organization_in)

    assert "Organization not found." in str(exc_info.value.errors())


def test_having_no_default_organization_is_reported_as_a_validation_error(session, organization):
    """Given no organization is marked default, when asking for it, then a 422-able error is raised."""
    for org in session.query(type(organization)).all():
        org.default = False
    session.commit()

    with pytest.raises(ValidationError) as exc_info:
        get_default_or_raise(db_session=session)

    assert "No default organization defined." in str(exc_info.value.errors())

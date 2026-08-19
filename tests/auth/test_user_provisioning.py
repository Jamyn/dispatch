"""User provisioning and role assignment in dispatch.auth.service.

get_current_user runs on every authenticated request and creates the user row
on first sight, so what a brand new account is granted is an authorization
decision made implicitly, on a hot path, with no explicit admin action.

These use the "default" organization and project that init_database seeds,
because auth_service.create resolves the default project with one_or_none() --
a second default in the same schema makes it raise rather than choose.
"""

import pytest

from dispatch.auth import service as auth_service
from dispatch.auth.models import UserCreate, UserOrganization, UserRegister, UserUpdate
from dispatch.enums import UserRoles
from dispatch.organization import service as organization_service
from dispatch.organization.models import OrganizationRead
from dispatch.project import service as project_service

from tests.factories import DispatchUserFactory


@pytest.fixture
def default_organization(session):
    return organization_service.get_by_slug(db_session=session, slug="default")


def test_a_new_user_is_provisioned_as_a_member_not_an_admin(session, default_organization):
    """First sight of a user must not hand out elevated access.

    A default above member would grant everyone who authenticates the ability
    to act on other people's incidents -- OrganizationAdminPermission gates
    most write routes on exactly this value.
    """
    user = auth_service.create(
        db_session=session,
        organization=default_organization.slug,
        user_in=UserRegister(email="newcomer@example.com"),
    )

    assert user.get_organization_role(default_organization.slug) == UserRoles.member


def test_a_new_user_is_scoped_to_the_organization_they_registered_against(
    session, default_organization, organization
):
    """Provisioning grants one tenancy, not every tenancy."""
    user = auth_service.create(
        db_session=session,
        organization=default_organization.slug,
        user_in=UserRegister(email="newcomer@example.com"),
    )

    assert user.get_organization_role(default_organization.slug) == UserRoles.member
    assert user.get_organization_role(organization.slug) is None


def test_an_explicit_role_is_honoured_when_creating_a_user(session, default_organization):
    """The admin-driven create path may set a role; it must be applied."""
    user = auth_service.create(
        db_session=session,
        organization=default_organization.slug,
        user_in=UserCreate(email="boss@example.com", password="Str0ngEnough", role=UserRoles.owner),
    )

    assert user.get_organization_role(default_organization.slug) == UserRoles.owner
    assert user.is_owner(default_organization.slug)


def test_a_new_user_is_given_the_default_project(session, default_organization):
    """Without a project the user's first page load has nothing to show."""
    default_project = project_service.get_default(db_session=session)

    user = auth_service.create(
        db_session=session,
        organization=default_organization.slug,
        user_in=UserRegister(email="newcomer@example.com"),
    )

    assert [p.project_id for p in user.projects] == [default_project.id]
    assert user.projects[0].default is True


def test_get_or_create_returns_the_existing_user_rather_than_a_duplicate(
    session, default_organization
):
    """Every authenticated request re-enters this path, so it must be idempotent.

    A second row would also discard whatever role an admin had granted, which
    is how an idempotency bug here becomes an access-control bug.
    """
    first = auth_service.get_or_create(
        db_session=session,
        organization=default_organization.slug,
        user_in=UserRegister(email="repeat@example.com"),
    )
    session.commit()

    second = auth_service.get_or_create(
        db_session=session,
        organization=default_organization.slug,
        user_in=UserRegister(email="repeat@example.com"),
    )

    assert first.id == second.id


def test_get_or_create_preserves_a_role_granted_after_the_user_was_created(
    session, default_organization, grant_role
):
    """A returning user must keep an elevated role, not be reset to member."""
    user = auth_service.get_or_create(
        db_session=session,
        organization=default_organization.slug,
        user_in=UserRegister(email="promoted@example.com"),
    )
    session.commit()

    membership = next(
        o for o in user.organizations if o.organization.slug == default_organization.slug
    )
    membership.role = UserRoles.admin
    session.commit()

    again = auth_service.get_or_create(
        db_session=session,
        organization=default_organization.slug,
        user_in=UserRegister(email="promoted@example.com"),
    )

    assert again.get_organization_role(default_organization.slug) == UserRoles.admin


def test_get_or_create_does_not_add_an_existing_user_to_another_organization(
    session, default_organization, organization
):
    """get_or_create short-circuits on email, so a second tenant's slug is ignored.

    Pinning it because the alternative -- quietly adding the membership --
    would let any tenant's login endpoint enroll an existing user of another.
    """
    user = auth_service.create(
        db_session=session,
        organization=default_organization.slug,
        user_in=UserRegister(email="repeat@example.com"),
    )
    session.commit()

    again = auth_service.get_or_create(
        db_session=session,
        organization=organization.slug,
        user_in=UserRegister(email="repeat@example.com"),
    )

    assert again.id == user.id
    assert again.get_organization_role(organization.slug) is None


def test_updating_an_existing_membership_changes_what_the_user_may_do(
    session, organization, grant_role
):
    """The admin route for promoting and demoting a user.

    Asserted through get_organization_role rather than the returned rows,
    because that accessor is what every permission class consults.
    """
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.member)

    auth_service.update(
        db_session=session,
        user=user,
        user_in=UserUpdate(
            id=user.id,
            organizations=[
                UserOrganization(
                    organization=OrganizationRead(
                        id=organization.id, name=organization.name, slug=organization.slug
                    ),
                    role=UserRoles.admin,
                )
            ],
        ),
    )
    session.commit()
    session.refresh(user)

    assert user.get_organization_role(organization.slug) == UserRoles.admin


def test_demoting_a_user_takes_their_elevated_access_away(session, organization, grant_role):
    """Revocation has to work, not only promotion."""
    user = DispatchUserFactory()
    grant_role(user, organization, UserRoles.owner)
    assert user.is_owner(organization.slug)

    auth_service.update(
        db_session=session,
        user=user,
        user_in=UserUpdate(
            id=user.id,
            organizations=[
                UserOrganization(
                    organization=OrganizationRead(
                        id=organization.id, name=organization.name, slug=organization.slug
                    ),
                    role=UserRoles.member,
                )
            ],
        ),
    )
    session.commit()
    session.refresh(user)

    assert user.get_organization_role(organization.slug) == UserRoles.member
    assert not user.is_owner(organization.slug)


def test_get_organization_role_is_none_for_a_tenant_the_user_is_not_in(
    session, organizations, grant_role
):
    """Permission classes read this directly, so None must mean no role.

    A default role here instead of None would grant that role in every tenant
    at once, including ones the user has never been added to.
    """
    home, other = organizations
    user = DispatchUserFactory()
    grant_role(user, home, UserRoles.owner)

    assert user.get_organization_role(home.slug) == UserRoles.owner
    assert user.get_organization_role(other.slug) is None
    assert user.is_owner(home.slug)
    assert not user.is_owner(other.slug)


def test_a_user_can_be_added_to_an_organization_they_were_not_in(
    session, organizations, grant_role
):
    """Granting a role in a second tenant is the admin path for onboarding.

    create_or_update_organization_role read `organization.id`, which is only
    bound when the caller looks the organization up by name -- supplying the
    id instead raised UnboundLocalError, and the row it built was never added
    to the session, so the grant would not have persisted regardless.
    """
    home, other = organizations
    user = DispatchUserFactory()
    grant_role(user, home, UserRoles.member)

    auth_service.update(
        db_session=session,
        user=user,
        user_in=UserUpdate(
            id=user.id,
            organizations=[
                UserOrganization(
                    organization=OrganizationRead(id=other.id, name=other.name, slug=other.slug),
                    role=UserRoles.admin,
                )
            ],
        ),
    )
    session.commit()
    session.refresh(user)

    assert user.get_organization_role(other.slug) == UserRoles.admin
    assert user.get_organization_role(home.slug) == UserRoles.member


def test_a_role_can_be_granted_by_organization_name_without_an_id(
    session, organizations, grant_role
):
    """The lookup-by-name branch has to keep working alongside the id branch."""
    home, other = organizations
    user = DispatchUserFactory()
    grant_role(user, home, UserRoles.member)

    auth_service.create_or_update_organization_role(
        db_session=session,
        user=user,
        role_in=UserOrganization(
            organization=OrganizationRead(name=other.name, slug=other.slug),
            role=UserRoles.owner,
        ),
    )
    session.commit()
    session.refresh(user)

    assert user.get_organization_role(other.slug) == UserRoles.owner


def test_an_auto_provisioned_user_gets_a_generated_password_not_an_empty_one(
    session, default_organization
):
    """The account auth.service creates on first sight must have a credential.

    UserRegister leaves the field empty when a caller omits it, so create()
    generates one. Storing the empty value instead would leave the account one
    weakened verify_password guard away from authenticating against nothing.
    """
    user = auth_service.create(
        db_session=session,
        organization=default_organization.slug,
        user_in=UserRegister(email="newcomer@example.com"),
    )

    assert user.password.startswith(b"$2b$")
    assert not user.verify_password("")


def test_two_auto_provisioned_users_do_not_share_a_password(session, default_organization):
    """A constant here would give every auto-provisioned account one key."""
    first = auth_service.create(
        db_session=session,
        organization=default_organization.slug,
        user_in=UserRegister(email="one@example.com"),
    )
    second = auth_service.create(
        db_session=session,
        organization=default_organization.slug,
        user_in=UserRegister(email="two@example.com"),
    )

    assert first.password != second.password


def test_an_explicitly_supplied_password_is_kept(session, default_organization):
    """Generation must only fill a gap, never overwrite a real password."""
    registered = UserRegister(email="chosen@example.com", password="hunter2hunter2")

    user = auth_service.create(
        db_session=session,
        organization=default_organization.slug,
        user_in=registered,
    )

    assert user.verify_password("hunter2hunter2")

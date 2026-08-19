"""The CLI commands an operator uses to grant a role.

`dispatch user update --role owner` is how the first account on a new install
becomes an owner -- it is in the install instructions, and there is no UI for it
until someone holds the role. Nothing here was covered before.

`click.Choice(Enum)` has changed across click releases between matching an
enum's *names* and its *values*, and click is deliberately unpinned. These pin
the spelling the documented command actually uses, so a routine dependency bump
fails here rather than in front of an operator locked out of their own install.
"""

import pytest
from click.testing import CliRunner

from dispatch.auth.models import DispatchUserOrganization
from dispatch.cli import dispatch_cli
from dispatch.enums import UserRoles


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def member(session, organization):
    """A registered user holding the lowest role in `organization`."""
    from tests.factories import DispatchUserFactory

    user = DispatchUserFactory(email="operator-under-test@example.com")
    session.add(
        DispatchUserOrganization(
            dispatch_user_id=user.id, organization_id=organization.id, role=UserRoles.member
        )
    )
    session.commit()
    return user


def role_of(session, user, organization):
    return (
        session.query(DispatchUserOrganization)
        .filter(DispatchUserOrganization.dispatch_user_id == user.id)
        .filter(DispatchUserOrganization.organization_id == organization.id)
        .one()
        .role
    )


def test_granting_ownership_uses_the_spelling_the_install_docs_publish(
    session, runner, member, organization
):
    """Given the documented command, when it runs, then the role is granted.

    The published form is lowercase (`--role owner`), which is the enum's *name*.
    If a click release flips `Choice` to matching values, this is the failure.
    """
    result = runner.invoke(
        dispatch_cli,
        ["user", "update", "--organization", organization.name, "--role", "owner", member.email],
    )

    assert result.exit_code == 0, result.output
    assert role_of(session, member, organization) == UserRoles.owner


def test_every_role_the_enum_offers_can_be_granted(session, runner, member, organization):
    """Given any defined role, when granting it, then the CLI accepts that spelling.

    Guards the whole enum rather than one member: a click change would otherwise
    be caught for `owner` while `manager` and `admin` quietly stopped working.
    """
    for role in UserRoles:
        result = runner.invoke(
            dispatch_cli,
            [
                "user",
                "update",
                "--organization",
                organization.name,
                "--role",
                role.name,
                member.email,
            ],
        )

        assert result.exit_code == 0, f"--role {role.name} rejected: {result.output}"
        assert role_of(session, member, organization) == role


def test_an_undefined_role_is_refused(session, runner, member, organization):
    """Given a role that does not exist, when granting it, then the CLI refuses.

    A non-zero exit is what stops an install script from reporting success after
    granting nothing.
    """
    result = runner.invoke(
        dispatch_cli,
        [
            "user",
            "update",
            "--organization",
            organization.name,
            "--role",
            "superuser",
            member.email,
        ],
    )

    assert result.exit_code != 0
    assert role_of(session, member, organization) == UserRoles.member


def test_registering_an_owner_grants_the_role_at_the_same_time(session, runner, organization):
    """Given a new account, when registering it as owner, then it holds that role.

    The other half of first-user setup: `register` takes the same `--role`, so
    it carries the same exposure to a `Choice` change as `update`.
    """
    from dispatch.auth import service as user_service

    email = "registered-under-test@example.com"
    result = runner.invoke(
        dispatch_cli,
        [
            "user",
            "register",
            "--organization",
            organization.name,
            "--role",
            "owner",
            "--password",
            "not-a-real-password",
            email,
        ],
    )

    assert result.exit_code == 0, result.output
    user = user_service.get_by_email(db_session=session, email=email)
    assert user is not None, f"register reported success but created nothing: {result.output}"
    assert role_of(session, user, organization) == UserRoles.owner


def test_granting_a_role_to_an_unknown_address_fails_loudly(session, runner, organization):
    """Given an email nobody has, when granting a role, then the command fails.

    Exiting zero here is what lets an install script report success after a
    typo, leaving nobody able to administer the deployment.
    """
    result = runner.invoke(
        dispatch_cli,
        [
            "user",
            "update",
            "--organization",
            organization.name,
            "--role",
            "owner",
            "nobody-under-test@example.com",
        ],
    )

    assert result.exit_code != 0
    assert "No user found" in result.output


def test_granting_a_role_in_an_unknown_organization_fails_readably(
    session, runner, member, organization
):
    """Given an organization that does not exist, when granting a role, then it is refused.

    `--organization` is matched by name. An unresolved name used to read `.id`
    off None, so the operator got an AttributeError traceback.
    """
    result = runner.invoke(
        dispatch_cli,
        [
            "user",
            "update",
            "--organization",
            "no_such_organization",
            "--role",
            "owner",
            member.email,
        ],
    )

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"operator saw a traceback: {result.exception!r}"
    )
    assert "Organization not found." in result.output
    assert role_of(session, member, organization) == UserRoles.member

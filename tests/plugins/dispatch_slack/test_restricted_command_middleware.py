"""The authorization gate on Slack's sensitive incident commands.

`restricted_command_middleware` is all that stands between any member of an
incident channel and commands like `/dispatch-assign-role`. It authorizes on the
caller's *active* roles in the incident the command came from, so both halves
carry weight: a commander who hands off must lose access, and a commander of
some other incident must never gain it here.
"""

from datetime import datetime

import pytest
from slack_bolt import BoltContext

from dispatch.participant_role.enums import ParticipantRoleType
from dispatch.plugins.dispatch_slack.exceptions import RoleError
from dispatch.plugins.dispatch_slack.middleware import restricted_command_middleware
from dispatch.plugins.dispatch_slack.models import SubjectMetadata
from tests.factories import (
    DispatchUserFactory,
    IncidentFactory,
    IndividualContactFactory,
    ParticipantFactory,
    ParticipantRoleFactory,
)

COMMAND = "/dispatch-assign-role"
RENOUNCED_AT = datetime(2024, 1, 1, 12, 0, 0)


@pytest.fixture
def caller(session):
    """The Slack user running the command.

    Its email must stay clear of the contact factories' `user{n}@example.com`
    sequence: those counters are independent, so a colliding address makes the
    participant lookup match a second, unrelated contact.
    """
    return DispatchUserFactory(email="caller-under-test@example.com")


class Next:
    """Stands in for Bolt's `next`, recording whether the command was let through."""

    def __init__(self):
        self.called = False

    def __call__(self):
        self.called = True


def run_middleware(session, incident, caller, next_):
    """Invokes the middleware with the arguments Bolt injects into it."""
    restricted_command_middleware(
        context=BoltContext(
            {"subject": SubjectMetadata(type="incident", id=incident.id)},
        ),
        db_session=session,
        user=caller,
        next=next_,
        payload={"command": COMMAND},
    )


def add_to_incident(incident, caller, *roles, renounced=()):
    """Adds `caller` to `incident` holding `roles`, plus any already-renounced ones."""
    return ParticipantFactory(
        incident=incident,
        individual=IndividualContactFactory(email=caller.email, project=incident.project),
        participant_roles=[ParticipantRoleFactory(role=role) for role in roles]
        + [ParticipantRoleFactory(role=role, renounced_at=RENOUNCED_AT) for role in renounced],
    )


def test_the_incident_commander_may_run_a_restricted_command(session, incident, caller):
    """Given the caller commands the incident, when the gate runs, then the command proceeds."""
    add_to_incident(incident, caller, ParticipantRoleType.incident_commander)

    next_ = Next()
    run_middleware(session, incident, caller, next_)

    assert next_.called


def test_the_scribe_may_run_a_restricted_command(session, incident, caller):
    """Given the caller is the scribe, when the gate runs, then the command proceeds."""
    add_to_incident(incident, caller, ParticipantRoleType.scribe)

    next_ = Next()
    run_middleware(session, incident, caller, next_)

    assert next_.called


def test_one_allowed_role_is_enough_when_the_caller_holds_several(session, incident, caller):
    """Given the caller holds an allowed role among others, when the gate runs, then it proceeds.

    Assigning a role renounces only the generic participant role, so callers
    routinely arrive holding several active roles at once.
    """
    add_to_incident(
        incident,
        caller,
        ParticipantRoleType.liaison,
        ParticipantRoleType.scribe,
    )

    next_ = Next()
    run_middleware(session, incident, caller, next_)

    assert next_.called


def test_a_participant_without_an_allowed_role_is_rejected(session, incident, caller):
    """Given the caller only reported the incident, when the gate runs, then it is refused.

    Reporting an incident, or merely being in its channel, confers no command authority.
    """
    add_to_incident(incident, caller, ParticipantRoleType.reporter)

    next_ = Next()
    with pytest.raises(RoleError):
        run_middleware(session, incident, caller, next_)

    assert not next_.called


def test_a_caller_who_is_not_a_participant_is_rejected(session, incident, caller):
    """Given the caller was never added to the incident, when the gate runs, then it is refused."""
    next_ = Next()
    with pytest.raises(RoleError) as exc_info:
        run_middleware(session, incident, caller, next_)

    assert not next_.called
    assert COMMAND in str(exc_info.value), "the caller is not told which command was refused"


def test_a_commander_who_handed_off_no_longer_qualifies(session, incident, caller):
    """Given the caller's command role was renounced, when the gate runs, then it is refused.

    Handing off incident command renounces the role rather than deleting it, so
    authorizing on anything but active roles leaves every past commander in charge.
    """
    add_to_incident(
        incident,
        caller,
        ParticipantRoleType.participant,
        renounced=[ParticipantRoleType.incident_commander],
    )

    next_ = Next()
    with pytest.raises(RoleError):
        run_middleware(session, incident, caller, next_)

    assert not next_.called


def test_commanding_another_incident_does_not_authorize(session, incident, caller):
    """Given the caller commands a different incident, when the gate runs here, then it is refused.

    Authorization is scoped to the incident the command was issued from.
    """
    add_to_incident(IncidentFactory(), caller, ParticipantRoleType.incident_commander)

    next_ = Next()
    with pytest.raises(RoleError):
        run_middleware(session, incident, caller, next_)

    assert not next_.called

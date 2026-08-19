"""Addresses the factories hand out must not collide across factories.

Dispatch joins users to contacts by email. A `DispatchUser` and an
`IndividualContact` that happen to share a generated address are therefore the
same person as far as the application is concerned -- the user silently becomes
a participant, a commander, a reporter.

`DispatchUserFactory` and `ContactBaseFactory` used to number their own
`user{n}@example.com` from independent counters, so whether they collided came
down to how many factory calls had run in that worker. It cost a permissions
test a silent false pass, a Slack test a defensive fixture, and one red CI run
on an unrelated change.
"""

from tests.factories import (
    DispatchUserFactory,
    IndividualContactFactory,
    TeamContactFactory,
)

GENERATED = 60


def addresses(factory, attribute="email"):
    """Addresses from `GENERATED` builds, without touching the database."""
    return {getattr(factory.build(), attribute) for _ in range(GENERATED)}


def test_a_user_and_a_contact_never_share_a_generated_address():
    """Given many of each, when their addresses are compared, then none is shared.

    A shared address makes the user a participant on whatever the contact is
    attached to, which quietly inverts any test asserting they are not.
    """
    shared = addresses(DispatchUserFactory) & addresses(IndividualContactFactory)

    assert not shared, f"a user and an individual contact were given the same address: {shared}"


def test_a_user_and_a_team_contact_never_share_a_generated_address():
    """Given many of each, when their addresses are compared, then none is shared.

    Team contacts come off the same base as individual contacts, so they carry
    the same exposure.
    """
    shared = addresses(DispatchUserFactory) & addresses(TeamContactFactory)

    assert not shared, f"a user and a team contact were given the same address: {shared}"


def test_a_contacts_owner_is_not_another_contacts_address():
    """Given many contacts, when owners are compared to addresses, then none is shared.

    `owner` is an email too, and shared the same pattern.
    """
    shared = addresses(IndividualContactFactory, "owner") & addresses(IndividualContactFactory)

    assert not shared, f"a contact's owner was another contact's address: {shared}"

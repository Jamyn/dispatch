"""The bridge-participation preference and the roster it produces (issue #110).

``filter_participants_for_bridge`` reads a per-user switch -- "Add me
automatically to incident bridges" in the UI -- and used to hand its answer to a
conference plugin that threw it away. These tests pin what the switch means now
that the answer is consumed.

They are characterization tests, not regression tests for #110: the filtering
logic itself is unchanged by that fix (only its signature and its accessor
moved), so every test here would pass against the unfixed code. Their value is
that this behaviour had no coverage at all, and it is now load-bearing at two
call sites instead of one. The tests that fail against the unfixed code live in
the provider suites.

What it decides is who is **listed** on the bridge. It is not access control:
every provider's join link works for whoever holds it, and opting out removes an
entry from a roster rather than a person from a meeting. Nothing below asserts
that an opted-out responder cannot join, because nothing in Dispatch makes that
true.

Two defaults are load-bearing and both mean "keep them":

- a responder with no Dispatch user account has no way to express a preference,
  and
- ``auto_add_to_incident_bridges`` is ``True`` until someone turns it off.

Addresses are minted per test rather than shared as constants. These tests commit
so the rows are visible to the query under test, and a commit escapes the
``session`` fixture's savepoint, so a fixed address would collide with the
previous test's leftover row.
"""

from itertools import count

import pytest

from dispatch.auth.models import DispatchUserSettings
from dispatch.incident.flows import filter_participants_for_bridge, wants_bridge_participation

# Synthetic throughout. `.invalid` is reserved by RFC 2606 and can never resolve,
# so nothing here can name a real mailbox even by accident.
_addresses = count()


def synthetic(label: str) -> str:
    return f"{label}-{next(_addresses)}@example.invalid"


@pytest.fixture
def responder(session):
    """Register a Dispatch user with a stated bridge preference, and hand back
    the address the incident flow would know them by."""
    from tests.factories import DispatchUserFactory

    def _responder(label: str, opted_in: bool) -> str:
        email = synthetic(label)
        user = DispatchUserFactory(email=email)
        session.add(
            DispatchUserSettings(dispatch_user_id=user.id, auto_add_to_incident_bridges=opted_in)
        )
        session.commit()
        return email

    return _responder


def test_an_opted_in_responder_is_listed(session, responder):
    alice = responder("alice", opted_in=True)

    assert filter_participants_for_bridge([alice], session) == [alice]


def test_an_opted_out_responder_is_not_listed(session, responder):
    bob = responder("bob", opted_in=False)

    assert filter_participants_for_bridge([bob], session) == []


def test_only_the_opted_out_responder_is_dropped(session, responder):
    """The mixed case, which is the only one that can catch an inverted test.

    A filter that returned everyone and a filter that returned no one each pass
    one of the two tests above.
    """
    alice = responder("alice", opted_in=True)
    bob = responder("bob", opted_in=False)
    carol = responder("carol", opted_in=True)

    assert filter_participants_for_bridge([alice, bob, carol], session) == [alice, carol]


def test_the_input_order_survives(session, responder):
    alice = responder("alice", opted_in=True)
    carol = responder("carol", opted_in=True)

    assert filter_participants_for_bridge([carol, alice], session) == [carol, alice]


def test_a_responder_with_no_dispatch_account_is_listed(session):
    """The preference lives on the user record, so someone who has never signed
    in has no way to set it. Dropping them would silently shrink the roster for
    every responder Dispatch knows only as a contact."""
    stranger = synthetic("stranger")

    assert filter_participants_for_bridge([stranger], session) == [stranger]


def test_a_responder_who_never_touched_the_switch_is_listed(session):
    """No settings row at all is not an opt-out: the column defaults to True."""
    from tests.factories import DispatchUserFactory

    email = synthetic("newcomer")
    DispatchUserFactory(email=email)
    session.commit()

    assert filter_participants_for_bridge([email], session) == [email]


def test_reading_the_preference_writes_nothing(session):
    """A lookup must not create a settings row.

    The obvious accessor, ``get_or_create_user_settings``, COMMITs the row it
    creates. Reading the preference through it would put a write in the middle of
    ``incident_create_resources``, once per responder without a settings row, and
    commit the surrounding transaction with it.
    """
    from tests.factories import DispatchUserFactory

    email = synthetic("newcomer")
    user = DispatchUserFactory(email=email)
    session.commit()

    assert wants_bridge_participation(email, session) is True

    assert (
        session.query(DispatchUserSettings)
        .filter(DispatchUserSettings.dispatch_user_id == user.id)
        .one_or_none()
        is None
    )


def test_an_empty_roster_stays_empty(session):
    assert filter_participants_for_bridge([], session) == []


def test_the_lookup_is_case_sensitive(session, responder):
    """Recorded, not endorsed. ``auth_service.get_by_email`` compares the address
    exactly, so a contact whose email differs in case from the user's reads as
    "no account" and is kept. Only the default saves that from being wrong in the
    other direction -- an opt-out expressed under one spelling is not honoured
    under another.
    """
    bob = responder("bob", opted_in=False)

    assert filter_participants_for_bridge([bob.upper()], session) == [bob.upper()]


def test_everyone_opting_out_yields_an_empty_roster(session, responder):
    """Which the conference flow must still create a conference for -- an empty
    roster is a bridge with nobody listed, not a reason to skip the bridge."""
    alice = responder("alice", opted_in=False)
    bob = responder("bob", opted_in=False)

    assert filter_participants_for_bridge([alice, bob], session) == []

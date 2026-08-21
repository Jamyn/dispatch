"""Contracts for resolving a signal from an id that may be either kind.

Four public routes accept `signal_id` as a free-form path parameter, so the
branch this function takes is chosen by the caller, not by the schema.
"""

import uuid

import pytest

from dispatch.signal.service import get_by_primary_or_external_id, is_valid_uuid

from tests.factories import SignalFactory


def test_a_uuid_resolves_against_external_id(session, project):
    """Given a UUID, when looking up, then it is matched as an external id."""
    external_id = str(uuid.uuid4())
    signal = SignalFactory(project=project, external_id=external_id)
    session.commit()

    assert get_by_primary_or_external_id(db_session=session, signal_id=external_id).id == signal.id


def test_an_unknown_uuid_resolves_to_nothing(session, project):
    """Given an unregistered UUID, when looking up, then no signal is returned.

    The routes turn None into a 404; a fallback to the primary-key branch here
    would hand back an unrelated detection.
    """
    SignalFactory(project=project, external_id=str(uuid.uuid4()))
    session.commit()

    assert get_by_primary_or_external_id(db_session=session, signal_id=str(uuid.uuid4())) is None


def test_a_numeric_id_resolves_against_the_primary_key(session, project):
    """Given a primary key, when looking up, then that signal is returned.

    The id arrives as a string: the routes declare `str | PrimaryKey`, and
    pydantic resolves a path parameter against `str` first.
    """
    signal = SignalFactory(project=project, external_id="detection-9000")
    session.commit()

    found = get_by_primary_or_external_id(db_session=session, signal_id=str(signal.id))
    assert found.id == signal.id


def test_a_numeric_external_id_resolves_without_a_primary_key_match(session, project):
    """Given a numeric external id, when looking up, then it matches on either column."""
    signal = SignalFactory(project=project, external_id="80001")
    session.commit()

    assert get_by_primary_or_external_id(db_session=session, signal_id="80001").id == signal.id


def test_is_valid_uuid_rejects_what_is_not_one():
    """Given a value that is not a UUID, when checked, then the lookup takes the id branch."""
    assert is_valid_uuid(str(uuid.uuid4()))
    assert not is_valid_uuid("detection-a")
    assert not is_valid_uuid("1234")
    assert not is_valid_uuid(None)


def test_a_non_numeric_external_id_resolves(session, project):
    """Given a non-numeric external id, when looking up, then that signal is returned.

    Signal.id is an integer column, so comparing it against a value like
    "detection-a" made Postgres raise instead of returning no rows -- a 500
    on four public routes, and no way to reach a signal named that way.
    """
    signal = SignalFactory(project=project, external_id="detection-a")
    session.commit()

    found = get_by_primary_or_external_id(db_session=session, signal_id="detection-a")
    assert found.id == signal.id


@pytest.mark.parametrize(
    "unknown_id",
    ["never-registered", "'; DROP TABLE signal; --", "-1", "9" * 40],
    ids=["freeform", "sql-shaped", "negative", "overflows-int"],
)
def test_an_unresolvable_id_returns_none_rather_than_raising(session, project, unknown_id):
    """Given an id matching nothing, when looking up, then None comes back.

    The routes turn None into a 422; anything raised here surfaces as a 500.
    """
    SignalFactory(project=project, external_id="detection-a")
    session.commit()

    assert get_by_primary_or_external_id(db_session=session, signal_id=unknown_id) is None


def test_an_integer_primary_key_resolves(session, project):
    """Given an int rather than a str, when looking up, then it still resolves."""
    signal = SignalFactory(project=project, external_id="detection-a")
    session.commit()

    assert get_by_primary_or_external_id(db_session=session, signal_id=signal.id).id == signal.id

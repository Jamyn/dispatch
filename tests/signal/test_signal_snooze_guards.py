"""Contracts for how filter_snooze treats filters it cannot fully evaluate.

Snoozing suppresses an alert. Every guard in filter_snooze therefore has to
fail towards alerting: an incomplete filter must be skipped, not honoured, and
skipping it must not stop the remaining filters from being evaluated.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from dispatch.signal.models import SignalFilterAction, SignalFilterMode
from dispatch.signal.service import filter_signal

from tests.factories import EntityTypeFactory, SignalFilterFactory, SignalInstanceFactory


@pytest.fixture
def snoozable(session, signal, project, entity):
    """A signal instance carrying one entity, plus the expression that matches it."""
    entity_type = EntityTypeFactory(project=project)
    session.add(entity_type)
    signal.entity_types.append(entity_type)
    session.add(entity)

    signal_instance = SignalInstanceFactory(
        project=project,
        signal=signal,
        entities=[entity],
        raw=json.dumps({"id": "test"}),
    )
    session.add(signal_instance)
    session.commit()

    expression = [{"or": [{"model": "Entity", "field": "id", "op": "==", "value": entity.id}]}]
    return signal_instance, expression


def snooze_filter(*, project, expression, name, **overrides):
    """A filter that would snooze, before overrides knock a field out."""
    fields = {
        "name": name,
        "description": "test",
        "expression": expression,
        "action": SignalFilterAction.snooze,
        "mode": SignalFilterMode.active,
        "expiration": datetime.now(timezone.utc) + timedelta(minutes=5),
        "project": project,
    }
    fields.update(overrides)
    return SignalFilterFactory(**fields)


def test_a_snooze_filter_without_an_expiration_does_not_suppress(session, project, snoozable):
    """Given a snooze filter with no expiration, when filtering, then the alert still fires.

    An open-ended snooze would silence the detection permanently.
    """
    signal_instance, expression = snoozable
    signal_instance.signal.filters = [
        snooze_filter(project=project, expression=expression, name="no-expiration", expiration=None)
    ]
    session.commit()

    assert not filter_signal(db_session=session, signal_instance=signal_instance)
    assert signal_instance.filter_action == SignalFilterAction.none


@pytest.mark.parametrize(
    "mode",
    ["", SignalFilterMode.monitor, SignalFilterMode.inactive, SignalFilterMode.expired],
    ids=["unset", "monitor", "inactive", "expired"],
)
def test_only_an_active_filter_suppresses(session, project, snoozable, mode):
    """Given a non-active snooze filter, when filtering, then the alert still fires."""
    signal_instance, expression = snoozable
    signal_instance.signal.filters = [
        snooze_filter(project=project, expression=expression, name=f"mode-{mode}", mode=mode)
    ]
    session.commit()

    assert not filter_signal(db_session=session, signal_instance=signal_instance)
    assert signal_instance.filter_action == SignalFilterAction.none


def test_a_snooze_filter_without_an_expression_suppresses_the_whole_signal(
    session, project, snoozable
):
    """Given a snooze filter with no expression, when filtering, then entities are ignored.

    An absent expression means "snooze this detection", not "snooze nothing".
    """
    signal_instance, _ = snoozable
    signal_instance.signal.filters = [
        snooze_filter(project=project, expression=[], name="no-expression")
    ]
    session.commit()

    assert filter_signal(db_session=session, signal_instance=signal_instance)
    assert signal_instance.filter_action == SignalFilterAction.snooze


def test_a_snooze_filter_matching_another_entity_does_not_suppress(session, project, snoozable):
    """Given a snooze scoped to a different entity, when filtering, then the alert fires."""
    signal_instance, _ = snoozable
    unrelated = [{"or": [{"model": "Entity", "field": "id", "op": "==", "value": -1}]}]
    signal_instance.signal.filters = [
        snooze_filter(project=project, expression=unrelated, name="other-entity")
    ]
    session.commit()

    assert not filter_signal(db_session=session, signal_instance=signal_instance)
    assert signal_instance.filter_action == SignalFilterAction.none


def test_an_unusable_filter_does_not_mask_a_later_valid_one(session, project, snoozable):
    """Given an unusable filter listed first, when filtering, then later filters still apply.

    Each guard uses `continue`; a `break` or an early return would let one
    malformed row disable every snooze configured after it.
    """
    signal_instance, expression = snoozable
    signal_instance.signal.filters = [
        snooze_filter(project=project, expression=expression, name="broken", expiration=None),
        snooze_filter(project=project, expression=expression, name="usable"),
    ]
    session.commit()

    assert filter_signal(db_session=session, signal_instance=signal_instance)
    assert signal_instance.filter_action == SignalFilterAction.snooze

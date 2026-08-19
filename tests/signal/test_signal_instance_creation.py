"""Contracts for resolving a signal definition during instance ingestion.

create_signal_instance is the entry point the AWS SQS plugin ingests through.
Its caller swallows every exception and drops the message, so a wrong branch
here is a silently missed alert rather than a visible failure.
"""

import uuid

import pytest
from fastapi import HTTPException

from dispatch.project.models import ProjectRead
from dispatch.signal import service as signal_service
from dispatch.signal.exceptions import (
    SignalNotDefinedException,
    SignalNotIdentifiedException,
)
from dispatch.signal.models import SignalInstance, SignalInstanceCreate, SignalRead

from tests.factories import SignalFactory


def instance_in(*, project, raw=None, external_id=None, **kwargs) -> SignalInstanceCreate:
    """A payload shaped like the one the SQS plugin builds, minus the signal."""
    return SignalInstanceCreate(
        raw=raw if raw is not None else {"id": str(uuid.uuid4()), "name": "test-detection"},
        project=ProjectRead(**project.dict()),
        external_id=external_id,
        **kwargs,
    )


def test_unidentifiable_payload_is_rejected(session, project):
    """Given no signal and no external id, when ingesting, then it must refuse.

    Falling through to the default definition here would file every
    unattributable payload under one detection.
    """
    with pytest.raises(SignalNotIdentifiedException):
        signal_service.create_signal_instance(
            db_session=session, signal_instance_in=instance_in(project=project)
        )


def test_external_id_resolves_its_own_definition(session, project):
    """Given an external id, when ingesting, then that definition is attached."""
    wanted = SignalFactory(project=project, external_id="detection-a", default=False)
    SignalFactory(project=project, external_id="detection-b", default=False)
    session.commit()

    signal_instance = signal_service.create_signal_instance(
        db_session=session,
        signal_instance_in=instance_in(project=project, external_id="detection-a"),
    )

    assert signal_instance.signal.id == wanted.id


def test_unknown_external_id_falls_back_to_the_default_definition(session, project):
    """Given an unmatched external id, when a default exists, then it is used."""
    fallback = SignalFactory(project=project, external_id="known", default=True)
    session.commit()

    signal_instance = signal_service.create_signal_instance(
        db_session=session,
        signal_instance_in=instance_in(project=project, external_id="never-registered"),
    )

    assert signal_instance.signal.id == fallback.id


def test_unknown_external_id_without_a_default_is_rejected(session, project):
    """Given an unmatched external id and no default, when ingesting, then it must refuse."""
    SignalFactory(project=project, external_id="known", default=False)
    session.commit()

    with pytest.raises(SignalNotDefinedException):
        signal_service.create_signal_instance(
            db_session=session,
            signal_instance_in=instance_in(project=project, external_id="never-registered"),
        )


def test_the_default_definition_is_scoped_to_its_project(session, project):
    """Given a default in another project, when ingesting, then it must not be borrowed."""
    SignalFactory(external_id="other-project-default", default=True)
    session.commit()

    with pytest.raises(SignalNotDefinedException):
        signal_service.create_signal_instance(
            db_session=session,
            signal_instance_in=instance_in(project=project, external_id="never-registered"),
        )


def test_the_raw_id_becomes_the_instance_primary_key(session, project):
    """Given an id in the raw payload, when ingesting, then it is the primary key.

    The SQS plugin dedupes redelivered messages by looking the raw id up as a
    primary key, so a generated key would re-ingest every retry.
    """
    SignalFactory(project=project, external_id="detection-a", default=False)
    session.commit()
    raw_id = str(uuid.uuid4())

    signal_instance = signal_service.create_signal_instance(
        db_session=session,
        signal_instance_in=instance_in(
            project=project,
            external_id="detection-a",
            raw={"id": raw_id, "name": "test-detection"},
        ),
    )

    assert str(signal_instance.id) == raw_id
    assert (
        signal_service.get_signal_instance(db_session=session, signal_instance_id=raw_id)
        is not None
    )


@pytest.mark.parametrize(
    "bad_id",
    ["not-a-uuid", "TEST:1.A/c12a34a5-dd67-8910", "12345"],
    ids=["freeform", "truncated", "numeric"],
)
def test_a_non_uuid_raw_id_is_rejected_without_persisting(session, project, bad_id):
    """Given a malformed raw id, when ingesting, then it must 400 and store nothing."""
    SignalFactory(project=project, external_id="detection-a", default=False)
    session.commit()
    before = session.query(SignalInstance).count()

    with pytest.raises(HTTPException) as exc_info:
        signal_service.create_signal_instance(
            db_session=session,
            signal_instance_in=instance_in(
                project=project,
                external_id="detection-a",
                raw={"id": bad_id, "name": "test-detection"},
            ),
        )

    assert exc_info.value.status_code == 400
    session.rollback()
    assert session.query(SignalInstance).count() == before


def test_a_supplied_signal_definition_is_used_as_is(session, project):
    """Given a caller-supplied signal, when ingesting, then no lookup is attempted.

    The SQS plugin resolves the definition itself and passes it in. Reading
    the lookup result unconditionally raised UnboundLocalError on this path,
    which that caller swallows as a dropped alert.
    """
    supplied = SignalFactory(project=project, external_id="supplied", default=False)
    SignalFactory(project=project, external_id="a-default", default=True)
    session.commit()

    signal_instance = signal_service.create_signal_instance(
        db_session=session,
        signal_instance_in=instance_in(
            project=project,
            signal=SignalRead(**supplied.dict(), project=ProjectRead(**project.dict())),
        ),
    )

    assert signal_instance.signal.id == supplied.id


def test_a_supplied_signal_definition_wins_over_the_external_id(session, project):
    """Given both a signal and an external id, when ingesting, then the signal wins."""
    supplied = SignalFactory(project=project, external_id="supplied", default=False)
    SignalFactory(project=project, external_id="by-external-id", default=False)
    session.commit()

    signal_instance = signal_service.create_signal_instance(
        db_session=session,
        signal_instance_in=instance_in(
            project=project,
            external_id="by-external-id",
            signal=SignalRead(**supplied.dict(), project=ProjectRead(**project.dict())),
        ),
    )

    assert signal_instance.signal.id == supplied.id


def test_updating_an_instance_without_a_raw_id_is_rejected(session, signal_instance):
    """Given a payload with no raw id, when updating, then it must refuse.

    The id is the only thing identifying which instance to update; reading it
    unconditionally raised UnboundLocalError instead.
    """
    with pytest.raises(SignalNotIdentifiedException):
        signal_service.update_instance(
            db_session=session,
            signal_instance_in=SignalInstanceCreate(raw={"name": "no-id-here"}),
        )


def test_updating_an_instance_replaces_its_raw_payload(session, signal_instance):
    """Given a raw id, when updating, then that instance's payload is replaced."""
    updated = signal_service.update_instance(
        db_session=session,
        signal_instance_in=SignalInstanceCreate(
            raw={"id": str(signal_instance.id), "name": "revised"}
        ),
    )

    assert updated.id == signal_instance.id
    assert updated.raw["name"] == "revised"

"""Deciding who a Slack request is from.

`user_middleware` turns a Slack payload into a `DispatchUser`, and
`restricted_command_middleware` authorizes on whatever it decides. Slack states
the requester in a different place for modals, messages and commands, so a
missed shape is a command that stops working; a wrong answer is a command that
runs as somebody else.
"""

from unittest.mock import MagicMock, patch

import pytest
from slack_bolt import BoltContext
from slack_bolt.request import BoltRequest

from dispatch.plugins.dispatch_slack.exceptions import ContextError
from dispatch.plugins.dispatch_slack.middleware import user_middleware
from dispatch.plugins.dispatch_slack.models import SubjectMetadata
from tests.factories import IndividualContactFactory, ParticipantFactory

SLACK_USER_ID = "U0123SLACK"


def slack_client(profile_email=None, is_bot=False):
    """A Slack client answering `users.info` for one user."""
    client = MagicMock()
    user = {"is_bot": is_bot}
    if profile_email:
        user["profile"] = {"email": profile_email}
    client.users_info.return_value = {"user": user}
    return client


def run(session, *, body=None, payload=None, subject=None, client=None):
    """Invokes the middleware with the arguments Bolt injects, returning the context."""
    context = BoltContext(
        {
            "subject": subject
            if subject is not None
            else SubjectMetadata(organization_slug="default"),
            "ack": MagicMock(),
        }
    )
    calls = []
    user_middleware(
        body=body or {},
        client=client if client is not None else slack_client("someone-under-test@example.com"),
        context=context,
        db_session=session,
        request=BoltRequest(body={}, mode="socket_mode"),
        next=lambda: calls.append(1),
        payload=payload or {},
    )
    return context, calls


def test_a_modal_identifies_its_requester(session):
    """Given a modal submission, when identifying the user, then `body.user.id` is used."""
    context, calls = run(session, body={"user": {"id": SLACK_USER_ID}})

    assert calls == [1]
    assert context["user_id"] == SLACK_USER_ID


def test_a_message_identifies_its_author(session):
    """Given a message event, when identifying the user, then `payload.user` is used."""
    context, calls = run(session, payload={"user": SLACK_USER_ID})

    assert calls == [1]
    assert context["user_id"] == SLACK_USER_ID


def test_a_slash_command_identifies_its_caller(session):
    """Given a slash command, when identifying the user, then `payload.user_id` is used."""
    context, calls = run(session, payload={"user_id": SLACK_USER_ID})

    assert calls == [1]
    assert context["user_id"] == SLACK_USER_ID


def test_a_request_naming_no_user_is_refused(session):
    """Given a payload naming no user, when identifying, then the request is refused.

    Continuing here would hand the listener no user at all, and everything that
    authorizes on one would have to guess.
    """
    with pytest.raises(ContextError):
        run(session, body={}, payload={})


def test_a_participant_is_identified_without_asking_slack(session, incident):
    """Given the caller already participates, when identifying, then the incident's record is used.

    The participant's own email is authoritative and already at hand, so a
    lookup against Slack is both unnecessary and a per-request API call.
    """
    participant = ParticipantFactory(
        incident=incident,
        individual=IndividualContactFactory(
            email="participant-under-test@example.com", project=incident.project
        ),
        user_conversation_id=SLACK_USER_ID,
    )
    session.commit()

    client = slack_client("wrong-person-under-test@example.com")
    context, calls = run(
        session,
        payload={"user_id": SLACK_USER_ID},
        subject=SubjectMetadata(type="incident", id=incident.id, organization_slug="default"),
        client=client,
    )

    assert calls == [1]
    assert context["user"].email == participant.individual.email
    client.users_info.assert_not_called()


def test_a_non_participant_is_identified_from_their_slack_profile(session, incident):
    """Given the caller does not participate, when identifying, then Slack's profile email is used."""
    context, calls = run(
        session,
        payload={"user_id": SLACK_USER_ID},
        subject=SubjectMetadata(type="incident", id=incident.id, organization_slug="default"),
        client=slack_client("outsider-under-test@example.com"),
    )

    assert calls == [1]
    assert context["user"].email == "outsider-under-test@example.com"


def test_a_slack_profile_without_an_email_is_refused(session):
    """Given a Slack profile with no email, when identifying, then the request is refused.

    Email is the only identifier Dispatch joins users on, so there is nothing to
    fall back to.
    """
    with pytest.raises(ContextError):
        run(session, payload={"user_id": SLACK_USER_ID}, client=slack_client(profile_email=None))


def test_a_bot_never_reaches_the_listener(session):
    """Given the requester is a bot, when identifying, then the chain stops without a user.

    Bots have no Dispatch account, and letting one through would create a user
    record for it on the spot.
    """
    context, calls = run(
        session,
        payload={"user_id": SLACK_USER_ID},
        client=slack_client(is_bot=True),
    )

    assert calls == [], "a bot reached the listener"
    assert "user" not in context
    context["ack"].assert_called_once()


def test_a_case_participant_is_identified_from_the_case(session, case):
    """Given the subject is a case, when identifying, then the case's participants are searched.

    Cases and incidents keep participants in separate tables; searching the
    wrong one finds nobody and sends every case request to Slack instead.
    """
    from tests.factories import IndividualContactFactory, ParticipantFactory

    participant = ParticipantFactory(
        individual=IndividualContactFactory(
            email="case-participant-under-test@example.com", project=case.project
        ),
        user_conversation_id=SLACK_USER_ID,
    )
    participant.case_id = case.id
    session.commit()

    client = slack_client("wrong-person-under-test@example.com")
    context, calls = run(
        session,
        payload={"user_id": SLACK_USER_ID},
        subject=SubjectMetadata(type="case", id=case.id, organization_slug="default"),
        client=client,
    )

    assert calls == [1]
    assert context["user"].email == participant.individual.email
    client.users_info.assert_not_called()


def test_a_request_arriving_without_a_session_opens_one_for_its_organization(session):
    """Given no session was opened yet, when identifying, then one opens on the request's tenant.

    `user_middleware` runs before `db_middleware` in some chains, so it has to
    open its own -- and on the requesting organization, not the default.
    """
    import dispatch.plugins.dispatch_slack.middleware as slack_middleware
    from tests.factories import OrganizationFactory

    tenant = OrganizationFactory()
    tenant.slug = "not_the_default_org"
    session.commit()

    slugs = []
    real = slack_middleware.refetch_db_session

    def record(slug):
        slugs.append(slug)
        return real("default")

    context = BoltContext(
        {"subject": SubjectMetadata(organization_slug="not_the_default_org"), "ack": MagicMock()}
    )
    calls = []
    with patch.object(slack_middleware, "refetch_db_session", record):
        slack_middleware.user_middleware(
            body={},
            client=slack_client("someone-under-test@example.com"),
            context=context,
            db_session=None,
            request=BoltRequest(body={}, mode="socket_mode"),
            next=lambda: calls.append(1),
            payload={"user_id": SLACK_USER_ID},
        )

    assert calls == [1]
    assert slugs == ["not_the_default_org"]

"""Resolving which incident or case a Slack channel belongs to.

`resolve_context_from_conversation` is the lookup every message, reaction and
command depends on: it walks the organizations, finds the conversation row for a
channel, and hands back both the subject and a session on that organization's
schema. Everything downstream -- authorization included -- trusts the subject it
produces, so resolving to the wrong one, or to a stale session, is not a
cosmetic failure.
"""

from unittest.mock import patch

import pytest
from slack_bolt import BoltContext

from dispatch.organization import service as organization_service
from dispatch.organization.models import Organization
from dispatch.plugins.dispatch_slack.exceptions import CommandError, ContextError
from dispatch.plugins.dispatch_slack.middleware import (
    command_context_middleware,
    optional_command_context_middleware,
    resolve_context_from_conversation,
)
from dispatch.plugins.dispatch_slack.models import SubjectMetadata
from tests.factories import ConversationFactory

COMMAND = "/dispatch-list-participants"


@pytest.fixture
def only_the_default_organization(session):
    """Restrict the resolver to the one organization that has a real schema.

    The resolver returns the first organization whose conversation matches, and
    `organization_service.get_all` is unordered. `ProjectFactory` creates an
    organization row per project while the suite has only the default schema, so
    without this the slug a test resolves under is down to row order.
    """
    default = session.query(Organization).filter(Organization.slug == "default").one()
    with patch.object(organization_service, "get_all", return_value=[default]):
        yield default


@pytest.fixture
def case_channel(session, case):
    """A case whose Slack conversation is reachable by channel id."""
    conversation = ConversationFactory()
    conversation.case_id = case.id
    session.commit()
    return conversation


def make_context(channel_id):
    return BoltContext({"channel_id": channel_id})


def test_an_incident_channel_resolves_to_its_incident(
    session, incident, only_the_default_organization
):
    """Given a channel holding an incident, when resolving, then that incident is the subject."""
    resolved = resolve_context_from_conversation(channel_id=incident.conversation.channel_id)

    assert resolved is not None, "an incident's own channel did not resolve"
    assert resolved.subject.type == "incident"
    assert resolved.subject.id == str(incident.id)
    assert resolved.subject.project_id == str(incident.project_id)
    assert resolved.subject.organization_slug == only_the_default_organization.slug


def test_a_case_channel_resolves_to_its_case(
    session, case, case_channel, only_the_default_organization
):
    """Given a channel holding a case, when resolving, then the subject is a case, not an incident.

    The two share one conversation table, and the subject type chosen here picks
    which service every downstream lookup goes to.
    """
    resolved = resolve_context_from_conversation(channel_id=case_channel.channel_id)

    assert resolved is not None, "a case's own channel did not resolve"
    assert resolved.subject.type == "case"
    assert resolved.subject.id == str(case.id)


def test_an_unknown_channel_resolves_to_nothing(session, incident, only_the_default_organization):
    """Given a channel no conversation claims, when resolving, then nothing comes back.

    Callers branch on None to tell the user they are in the wrong channel, so a
    raise here would surface as an unhandled error instead.
    """
    assert resolve_context_from_conversation(channel_id="C-never-seen") is None


def test_the_resolved_session_is_usable(session, incident, only_the_default_organization):
    """Given a resolved conversation, when the caller uses the session it returns, then it works.

    The session is handed to listeners through the Bolt context and is the only
    one they get, so it has to still be able to run a query.
    """
    resolved = resolve_context_from_conversation(channel_id=incident.conversation.channel_id)

    from dispatch.incident import service as incident_service

    found = incident_service.get(db_session=resolved.db_session, incident_id=incident.id)
    assert found is not None, "the session handed to the listener could not read its own subject"


def test_a_command_run_outside_any_incident_channel_is_refused(
    session, incident, only_the_default_organization
):
    """Given an unresolvable channel, when a command runs, then it is refused by name."""
    called = []
    with pytest.raises(ContextError) as exc_info:
        command_context_middleware(
            context=make_context("C-never-seen"),
            payload={"command": COMMAND},
            next=lambda: called.append(1),
        )

    assert not called
    assert COMMAND in str(exc_info.value), "the user is not told which command was refused"


def test_an_incident_command_run_in_a_case_channel_is_refused(
    session, case, case_channel, only_the_default_organization
):
    """Given a case channel, when an incident-only command runs, then it is refused as a mismatch.

    Distinct from an unresolvable channel: the context resolved fine, it is just
    the wrong kind, and the user is told which kind they are in.
    """
    called = []
    with pytest.raises(CommandError) as exc_info:
        command_context_middleware(
            context=make_context(case_channel.channel_id),
            payload={"command": COMMAND, "channel_name": "some-case"},
            next=lambda: called.append(1),
        )

    assert not called
    assert "case" in str(exc_info.value)


def test_a_command_in_a_matching_channel_proceeds(session, incident, only_the_default_organization):
    """Given an incident channel, when an incident command runs, then it proceeds with the subject."""
    context = make_context(incident.conversation.channel_id)
    called = []

    command_context_middleware(
        context=context,
        payload={"command": COMMAND},
        next=lambda: called.append(1),
    )

    assert called == [1]
    assert context["subject"].id == str(incident.id)
    assert context["db_session"] is not None


def test_an_optional_command_keeps_the_default_subject_when_nothing_resolves(
    session, incident, only_the_default_organization
):
    """Given an unresolvable channel, when an anywhere-command runs, then it still proceeds.

    Unlike `command_context_middleware` this one runs outside incident channels
    on purpose, so the default subject installed earlier has to survive.
    """
    default_subject = SubjectMetadata(organization_slug="default")
    context = BoltContext({"channel_id": "C-never-seen", "subject": default_subject})
    called = []

    optional_command_context_middleware(context=context, next=lambda: called.append(1))

    assert called == [1]
    assert context["subject"] is default_subject


def test_an_optional_command_adopts_the_conversation_it_was_run_in(
    session, incident, only_the_default_organization
):
    """Given a resolvable channel, when an anywhere-command runs, then it adopts that subject.

    It also replaces the session, so the command reads the tenant it was run in
    rather than the default organization's.
    """
    context = BoltContext(
        {
            "channel_id": incident.conversation.channel_id,
            "subject": SubjectMetadata(organization_slug="default"),
        }
    )
    called = []

    optional_command_context_middleware(context=context, next=lambda: called.append(1))

    assert called == [1]
    assert context["subject"].id == str(incident.id)
    assert context["db_session"] is not None

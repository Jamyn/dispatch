"""Shared setup for driving the Slack modal handlers.

The handlers are written for Bolt's injecting dispatcher, so they cannot simply
be called -- their signatures differ in both argument set and order, and they
read some inputs off the Bolt context rather than taking them as arguments.
These fixtures build a plausible invocation and hand back the view the handler
tried to open.
"""

import inspect
from unittest.mock import MagicMock

import pytest
from slack_bolt import BoltContext

from dispatch.plugins.dispatch_slack.models import SubjectMetadata
from tests.factories import (
    CaseFactory,
    CasePriorityFactory,
    CaseSeverityFactory,
    CaseTypeFactory,
    IncidentFactory,
    IncidentPriorityFactory,
    IncidentSeverityFactory,
    IncidentTypeFactory,
    ParticipantFactory,
    ProjectFactory,
    ServiceFactory,
)


def call_handler(handler, **available):
    """Invoke a handler with only the arguments it declares."""
    wanted = inspect.getfullargspec(inspect.unwrap(handler)).args
    missing = [a for a in wanted if a not in available]
    assert not missing, f"{handler.__name__} needs un-stubbed args: {missing}"
    return handler(**{a: available[a] for a in wanted})


@pytest.fixture
def incident_with_related_records(session):
    """An incident whose type, severity and priority belong to its own project.

    The select builders look these up by project and return None when a project
    has none, which blockkit rejects as a block. The default IncidentFactory
    gives each sub-object its own project, so the lookups come back empty.
    """
    project = ProjectFactory(display_name="Test Project")
    incident = IncidentFactory(
        project=project,
        incident_type=IncidentTypeFactory(project=project),
        incident_priority=IncidentPriorityFactory(project=project),
        incident_severity=IncidentSeverityFactory(project=project),
        # handle_update_participant_command builds a participant select, which
        # is likewise None when the incident has nobody on it.
        participants=[ParticipantFactory()],
    )
    # handle_engage_oncall_command aborts with CommandError unless the project
    # has at least one oncall service.
    ServiceFactory(project=project, is_active=True)
    return incident


@pytest.fixture
def case_with_related_records(session):
    """A case whose related records all belong to its own project."""
    project = ProjectFactory(display_name="Test Project")
    # Escalation offers incident types and priorities from the same project.
    IncidentTypeFactory(project=project)
    IncidentPriorityFactory(project=project)
    return CaseFactory(
        project=project,
        case_type=CaseTypeFactory(project=project),
        case_priority=CasePriorityFactory(project=project),
        case_severity=CaseSeverityFactory(project=project),
        assignee=ParticipantFactory(),
        status="New",
    )


def _drive(handler, subject_type, subject, session):
    client = MagicMock()
    # Handlers resolve people through Slack; a bare MagicMock is not a str.
    client.users_lookupByEmail.return_value = {"user": {"id": "U999"}}
    client.chat_getPermalink.return_value = {"permalink": "https://example.com/p"}

    # BoltContext, not a plain dict: some handlers use attribute access.
    context = BoltContext(
        {
            "subject": SubjectMetadata(
                id=str(subject.id),
                type=subject_type,
                organization_slug="default",
                project_id=str(subject.project.id),
                channel_id="C123",
            ),
            "channel_id": "C123",
            "user_id": "U123",
            "db_session": session,
            "config": MagicMock(),
        }
    )

    call_handler(
        handler,
        ack=MagicMock(),
        body={
            "trigger_id": "T123",
            "user": {"id": "U123"},
            "view": {"id": "V123"},
            "text": "",
            "channel": {"id": "C123"},
            "message": {"ts": "1.0", "text": "a message"},
        },
        client=client,
        context=context,
        db_session=session,
    )

    assert client.views_open.called, f"{handler.__name__} never opened a modal"
    return client.views_open.call_args.kwargs["view"]


@pytest.fixture
def build_incident_modal(session, incident_with_related_records):
    """Run an incident modal handler and return the view it opened."""

    def build(handler):
        return _drive(handler, "incident", incident_with_related_records, session)

    return build


@pytest.fixture
def build_case_modal(session, case_with_related_records):
    """Run a case modal handler and return the view it opened."""

    def build(handler):
        return _drive(handler, "case", case_with_related_records, session)

    return build

"""Build coverage for the incident Slack modal handlers.

blockkit validates when a component tree is built, not when it is constructed,
so these handlers can only be proven to produce valid Block Kit by running them.
Each test drives a handler with a mocked Slack client and asserts on the view
payload it hands to the Slack API.
"""

import inspect
from unittest.mock import MagicMock

import pytest

from dispatch.plugins.dispatch_slack.incident.interactive import (
    handle_add_timeline_event_command,
    handle_assign_role_command,
    handle_engage_oncall_command,
    handle_report_executive_command,
    handle_update_incident_command,
    handle_update_participant_command,
)
from dispatch.plugins.dispatch_slack.models import SubjectMetadata
from tests.factories import (
    IncidentFactory,
    IncidentPriorityFactory,
    IncidentSeverityFactory,
    IncidentTypeFactory,
    ParticipantFactory,
    ProjectFactory,
    ServiceFactory,
)


@pytest.fixture
def incident(session):
    """An incident whose type, severity and priority belong to its own project.

    The select builders look these up by project and return None when a project
    has none, which blockkit rejects as a block. The default IncidentFactory
    gives each sub-object its own project, so the lookups come back empty.
    """
    project = ProjectFactory(display_name="Test Project")
    return IncidentFactory(
        project=project,
        incident_type=IncidentTypeFactory(project=project),
        incident_priority=IncidentPriorityFactory(project=project),
        incident_severity=IncidentSeverityFactory(project=project),
        # handle_update_participant_command builds a participant select, which
        # is likewise None when the incident has nobody on it.
        participants=[ParticipantFactory()],
    )


MODAL_HANDLERS = [
    handle_update_incident_command,
    handle_add_timeline_event_command,
    handle_assign_role_command,
    handle_update_participant_command,
    handle_engage_oncall_command,
    handle_report_executive_command,
]


def call_handler(handler, **available):
    """Invoke a handler with only the arguments it declares.

    The handlers are written for Bolt's injecting dispatcher, so their
    signatures differ in both argument set and order.
    """
    wanted = inspect.getfullargspec(inspect.unwrap(handler)).args
    missing = [a for a in wanted if a not in available]
    assert not missing, f"{handler.__name__} needs un-stubbed args: {missing}"
    return handler(**{a: available[a] for a in wanted})


@pytest.mark.parametrize("handler", MODAL_HANDLERS, ids=lambda h: h.__name__)
def test_modal_handler_builds_a_valid_view(handler, session, incident):
    client = MagicMock()
    # handle_engage_oncall_command aborts with CommandError unless the project
    # has at least one oncall service.
    ServiceFactory(project=incident.project, is_active=True)
    context = {
        "subject": SubjectMetadata(
            id=str(incident.id),
            type="incident",
            organization_slug="default",
            project_id=str(incident.project.id),
            channel_id="C123",
        ),
        "channel_id": "C123",
        "user_id": "U123",
        # Several handlers reach for these off the Bolt context rather than
        # taking them as arguments.
        "db_session": session,
        "config": MagicMock(),
    }

    call_handler(
        handler,
        ack=MagicMock(),
        body={
            "trigger_id": "T123",
            "user": {"id": "U123"},
            "view": {"id": "V123"},
            "text": "",
        },
        client=client,
        context=context,
        db_session=session,
    )

    assert client.views_open.called, "handler never opened a modal"
    view = client.views_open.call_args.kwargs["view"]
    assert view["type"] == "modal"
    assert view["blocks"], "modal built with no blocks"

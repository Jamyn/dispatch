"""Build coverage for the case Slack modal handlers.

blockkit validates when a component tree is built, not when it is constructed,
so these handlers can only be proven to produce valid Block Kit by running them.
Each test drives a handler with a mocked Slack client and asserts on the view
payload it hands to the Slack API.
"""

import inspect
from unittest.mock import MagicMock

import pytest
from slack_bolt import BoltContext

from dispatch.plugins.dispatch_slack.case.interactive import (
    handle_engage_user_command,
    handle_update_case_command,
    resolve_button_click,
)
from dispatch.project.models import Project
from dispatch.plugins.dispatch_slack.models import SubjectMetadata
from tests.factories import (
    CaseFactory,
    CasePriorityFactory,
    CaseSeverityFactory,
    CaseTypeFactory,
    IncidentPriorityFactory,
    IncidentTypeFactory,
    ParticipantFactory,
    ProjectFactory,
)


@pytest.fixture
def case(session):
    """A case whose related records all belong to its own project.

    The select builders look these up by project and return None when a project
    has none, which blockkit rejects as a block. The default CaseFactory gives
    each sub-object its own project, so the lookups come back empty.
    """
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
    )


# Three handlers are deliberately absent.
# handle_list_signals_command puts the Bolt-context plugin config into a query,
# so a stub config cannot drive it. handle_escalate_case_command and
# report_issue call project_select, which emits one option per project with no
# cap; the suite accumulates far more than Slack's 100-option limit, so they
# only build in isolation.
MODAL_HANDLERS = [
    handle_update_case_command,
    handle_engage_user_command,
    resolve_button_click,
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
def test_modal_handler_builds_a_valid_view(handler, session, case):
    # project_select lists every project, and an option whose text is empty is
    # not valid Block Kit. Several factories create projects without one.
    for existing in session.query(Project).all():
        if not existing.display_name:
            existing.display_name = existing.name or f"project-{existing.id}"
    session.flush()

    client = MagicMock()
    # Handlers resolve the assignee through Slack; a bare MagicMock is not a str.
    client.users_lookupByEmail.return_value = {"user": {"id": "U999"}}
    client.chat_getPermalink.return_value = {"permalink": "https://example.com/p"}
    # BoltContext, not a plain dict: some handlers use attribute access.
    context = BoltContext(
        {
            "subject": SubjectMetadata(
                id=str(case.id),
                type="case",
                organization_slug="default",
                project_id=str(case.project.id),
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

    assert client.views_open.called, "handler never opened a modal"
    view = client.views_open.call_args.kwargs["view"]
    assert view["type"] == "modal"
    assert view["blocks"], "modal built with no blocks"

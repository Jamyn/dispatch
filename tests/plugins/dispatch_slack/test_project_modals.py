"""Every modal that offers a project, built at project counts either side of
Slack's 100-option limit.

#86 was not a bug in the selector alone -- it made four incident and case
workflows impossible to open. These build each one for real, so a regression
shows up as the modal it actually breaks.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from slack_sdk.web import WebClient

from tests.factories import IncidentTypeFactory

from dispatch.plugins.dispatch_slack.case.enums import CaseEscalateActions, CaseReportActions
from dispatch.plugins.dispatch_slack.case.interactive import (
    escalate_button_click,
    handle_escalate_case_command,
    report_issue,
)
from dispatch.plugins.dispatch_slack.config import MAX_SELECT_OPTIONS
from dispatch.plugins.dispatch_slack.fields import DefaultBlockIds
from dispatch.plugins.dispatch_slack.incident.enums import (
    IncidentReportActions,
    IncidentUpdateActions,
)
from dispatch.plugins.dispatch_slack.incident.interactive import (
    handle_report_incident_command,
    report_incident,
)

# Either side of Slack's limit, plus a deployment far past it.
PROJECT_COUNTS = [1, 99, MAX_SELECT_OPTIONS, MAX_SELECT_OPTIONS + 1, 500]

CASE_HANDLERS = [handle_escalate_case_command, escalate_button_click, report_issue]
INCIDENT_HANDLERS = [handle_report_incident_command, report_incident]


@pytest.fixture
def escalatable_case(session, case_with_related_records):
    """Give the case type an incident type from the case's own project.

    ``escalate_button_click`` preselects it, and a select rejects an
    initial_option that is not among its options -- which the default factory
    graph produces, because it gives the incident type a project of its own.
    """
    case = case_with_related_records
    case.case_type.incident_type = IncidentTypeFactory(project=case.project)
    session.commit()
    return case


def expected_mode(count: int) -> str:
    """Which select a given project count must produce -- not merely one of them."""
    return "static_select" if count <= MAX_SELECT_OPTIONS else "external_select"


def project_block(view: dict) -> dict:
    blocks = [b for b in view["blocks"] if b.get("block_id") == DefaultBlockIds.project_select]
    assert blocks, "modal has no project select"
    return blocks[0]


@pytest.mark.parametrize("count", PROJECT_COUNTS)
@pytest.mark.parametrize("handler", CASE_HANDLERS, ids=lambda h: h.__name__)
def test_case_modals_open_at_any_project_count(
    session, escalatable_case, only_projects, build_case_modal, handler, count
):
    only_projects(count, keep=[escalatable_case.project])

    view = build_case_modal(handler)

    assert view["type"] == "modal"
    assert project_block(view)["element"]["type"] == expected_mode(count)


@pytest.mark.parametrize("count", PROJECT_COUNTS)
@pytest.mark.parametrize("handler", INCIDENT_HANDLERS, ids=lambda h: h.__name__)
def test_incident_modals_open_at_any_project_count(
    session, incident_with_related_records, only_projects, build_incident_modal, handler, count
):
    only_projects(count, keep=[incident_with_related_records.project])

    view = build_incident_modal(handler)

    assert view["type"] == "modal"
    assert project_block(view)["element"]["type"] == expected_mode(count)


@pytest.mark.parametrize("handler", CASE_HANDLERS, ids=lambda h: h.__name__)
def test_case_modals_open_when_the_project_has_no_display_name(
    session, escalatable_case, only_projects, build_case_modal, handler
):
    """An empty display_name is the column default, and Slack rejects empty
    option text -- including in the initial_option the escalate modals set."""
    project = escalatable_case.project
    project.display_name = ""
    only_projects(3, keep=[project])

    view = build_case_modal(handler)
    element = project_block(view)["element"]

    labels = [o["text"]["text"] for o in element["options"]]
    assert project.name in labels, "the project fell out of its own selector"
    if "initial_option" in element:
        assert element["initial_option"]["text"]["text"] == project.name


def test_escalate_preselects_the_cases_project_past_the_limit(
    session, escalatable_case, only_projects, build_case_modal
):
    project = escalatable_case.project
    only_projects(MAX_SELECT_OPTIONS + 1, keep=[project])

    view = build_case_modal(handle_escalate_case_command)
    element = project_block(view)["element"]

    assert element["type"] == "external_select"
    assert element["initial_option"]["value"] == str(project.id)


def test_report_issue_selection_reaches_the_case_report_handler(
    session,
    case_with_related_records,
    only_projects,
    dispatch_interaction,
    single_default_organization,
):
    """The whole point of the selector: a picked project drives the next step.

    Runs the real block_actions payload Slack sends when a project is chosen and
    checks the modal that comes back, at a project count that forces the
    external select. This action's middleware resolve the database session
    before the subject, so it lands on the default organization -- hence the
    fixture that leaves exactly one.
    """
    project = case_with_related_records.project
    case_type = case_with_related_records.case_type
    only_projects(MAX_SELECT_OPTIONS + 1, keep=[project])

    selected = {
        "text": {"type": "plain_text", "text": project.display_name},
        "value": str(project.id),
    }
    body = {
        "type": "block_actions",
        "team": {"id": "T123"},
        "user": {"id": "U123"},
        "api_app_id": "A123",
        "trigger_id": "TRIGGER",
        "container": {"type": "view", "view_id": "V123"},
        "view": {
            "id": "V123",
            "hash": "1.0",
            "type": "modal",
            "private_metadata": json.dumps(
                {
                    "type": "case",
                    "id": str(case_with_related_records.id),
                    "organization_slug": "default",
                    "project_id": str(project.id),
                    "channel_id": "C123",
                }
            ),
            "state": {
                "values": {
                    DefaultBlockIds.project_select: {
                        CaseReportActions.project_select: {
                            "type": "external_select",
                            "selected_option": selected,
                        }
                    }
                }
            },
        },
        "actions": [
            {
                "type": "external_select",
                "action_id": CaseReportActions.project_select,
                "block_id": DefaultBlockIds.project_select,
                "selected_option": selected,
                "action_ts": "1.0",
            }
        ],
    }

    views_update = MagicMock(return_value={"view": {"id": "V123"}, "trigger_id": "TRIGGER"})
    with patch.object(WebClient, "views_update", views_update):
        response = dispatch_interaction(body)

    assert response.status == 200, response.body
    assert views_update.called, "the project selection re-rendered nothing"

    view = views_update.call_args.kwargs["view"]
    assert project_block(view)["element"]["initial_option"]["value"] == str(project.id)

    case_types = [
        b for b in view["blocks"] if b.get("block_id") == DefaultBlockIds.case_type_select
    ]
    assert case_types, "the selected project's case types were not offered"
    assert str(case_type.id) in {o["value"] for o in case_types[0]["element"]["options"]}, (
        "the case types offered do not belong to the selected project"
    )


def selection_payload(action_id: str, subject: dict, project) -> dict:
    """The block_actions payload Slack posts when a project is chosen."""
    selected = {
        "text": {"type": "plain_text", "text": project.display_name or project.name},
        "value": str(project.id),
    }
    return {
        "type": "block_actions",
        "team": {"id": "T123"},
        "user": {"id": "U123"},
        "api_app_id": "A123",
        "trigger_id": "TRIGGER",
        "container": {"type": "view", "view_id": "V123"},
        "view": {
            "id": "V123",
            "hash": "1.0",
            "type": "modal",
            "private_metadata": json.dumps(subject),
            "state": {
                "values": {
                    DefaultBlockIds.project_select: {
                        action_id: {"type": "external_select", "selected_option": selected}
                    }
                }
            },
        },
        "actions": [
            {
                "type": "external_select",
                "action_id": action_id,
                "block_id": DefaultBlockIds.project_select,
                "selected_option": selected,
                "action_ts": "1.0",
            }
        ],
    }


CASE_SELECT_ACTION_IDS = [CaseEscalateActions.project_select, CaseReportActions.project_select]
INCIDENT_SELECT_ACTION_IDS = [
    IncidentReportActions.project_select,
    IncidentUpdateActions.project_select,
]


def assert_selection_survives(dispatch_interaction, body, project):
    views_update = MagicMock(return_value={"view": {"id": "V123"}, "trigger_id": "TRIGGER"})
    with patch.object(WebClient, "views_update", views_update):
        response = dispatch_interaction(body)

    assert response.status == 200, response.body
    assert views_update.called, "the project selection re-rendered nothing"

    element = project_block(views_update.call_args.kwargs["view"])["element"]
    assert "initial_option" in element, "the re-rendered modal lost the selection"
    assert element["initial_option"]["value"] == str(project.id)


@pytest.mark.parametrize("action_id", CASE_SELECT_ACTION_IDS, ids=str)
def test_a_chosen_project_survives_the_case_re_render(
    session,
    escalatable_case,
    only_projects,
    dispatch_interaction,
    single_default_organization,
    action_id,
):
    """Past the limit the menu is a search, so a lost selection costs a re-search."""
    project = escalatable_case.project
    only_projects(MAX_SELECT_OPTIONS + 1, keep=[project])

    subject = {
        "type": "case",
        "id": str(escalatable_case.id),
        "organization_slug": "default",
        "project_id": str(project.id),
        "channel_id": "C123",
    }

    assert_selection_survives(
        dispatch_interaction, selection_payload(action_id, subject, project), project
    )


@pytest.mark.parametrize("action_id", INCIDENT_SELECT_ACTION_IDS, ids=str)
def test_a_chosen_project_survives_the_incident_re_render(
    session,
    incident_with_related_records,
    only_projects,
    dispatch_interaction,
    single_default_organization,
    action_id,
):
    project = incident_with_related_records.project
    only_projects(MAX_SELECT_OPTIONS + 1, keep=[project])

    subject = {
        "type": "incident",
        "id": str(incident_with_related_records.id),
        "organization_slug": "default",
        "project_id": str(project.id),
        "channel_id": "C123",
    }

    assert_selection_survives(
        dispatch_interaction, selection_payload(action_id, subject, project), project
    )


def test_report_incident_files_against_the_selected_project(
    session, only_projects, dispatch_interaction, single_default_organization
):
    """The selection has to survive submission, not just re-rendering.

    `display_name` is separately editable in the web UI, so it routinely differs
    from `name`. Resolving the submitted project from the option's text sends
    the incident to whatever `get_by_name_or_default` falls back to -- the
    default project -- with nothing said to the reporter.
    """
    from dispatch.plugins.dispatch_slack.incident.interactive import (
        handle_report_incident_submission_event,
    )

    default_project, target = only_projects(
        display_names=["", "A Friendly Display Name"],
    )
    # get_by_name_or_default falls back to whichever project is the default.
    default_project.default = True
    target.default = False
    session.commit()

    assert target.display_name != target.name, "the test needs the two to differ"

    form_data = {
        DefaultBlockIds.title_input: "a title",
        DefaultBlockIds.description_input: "a description",
        DefaultBlockIds.project_select: {
            "name": target.display_name,
            "value": str(target.id),
        },
    }

    created = {}

    def capture(*, db_session, incident_in):
        created["project"] = incident_in.project
        raise RuntimeError("stop after the project is resolved")

    client = MagicMock()
    client.views_update.return_value = {"view": {"id": "V123"}, "trigger_id": "TRIGGER"}

    with patch(
        "dispatch.plugins.dispatch_slack.incident.interactive.incident_service.create",
        side_effect=capture,
    ):
        with pytest.raises(RuntimeError, match="stop after"):
            handle_report_incident_submission_event(
                ack=MagicMock(),
                body={"view": {"id": "V123"}, "trigger_id": "TRIGGER"},
                client=client,
                db_session=session,
                form_data=form_data,
                user=MagicMock(email="reporter@example.com"),
            )

    assert created["project"].name == target.name, (
        "the incident was filed against the wrong project"
    )


def test_report_issue_files_against_the_selected_project(
    session, only_projects, dispatch_interaction, single_default_organization
):
    """Same failure as above, on the case side: the select drove the cascading
    re-render and was then dropped at submit, so `CaseCreate.project` was None
    and `get_by_name_or_default` filed every case against the default project
    (#142). The case type, severity and priority are all resolved inside
    whatever project wins, so this decides more than the case's home.
    """
    from dispatch.plugins.dispatch_slack.case.interactive import (
        handle_report_submission_event,
    )

    default_project, target = only_projects(display_names=["", "A Friendly Display Name"])
    default_project.default = True
    target.default = False
    session.commit()

    assert target.display_name != target.name, "the test needs the two to differ"

    form_data = {
        DefaultBlockIds.title_input: "a title",
        DefaultBlockIds.description_input: "a description",
        DefaultBlockIds.project_select: {
            "name": target.display_name,
            "value": str(target.id),
        },
        # The handler matches this block id by prefix, not equality.
        f"{DefaultBlockIds.case_assignee_select}-0": {"name": "user", "value": "U456"},
    }

    created = {}

    def capture(*, db_session, case_in, current_user):
        created["project"] = case_in.project
        raise RuntimeError("stop after the project is resolved")

    client = MagicMock()
    client.users_info.return_value = {"user": {"profile": {"email": "assignee@example.com"}}}
    client.views_update.return_value = {"view": {"id": "V123"}, "trigger_id": "TRIGGER"}

    with patch(
        "dispatch.plugins.dispatch_slack.case.interactive.case_service.create",
        side_effect=capture,
    ):
        with pytest.raises(RuntimeError, match="stop after"):
            handle_report_submission_event(
                ack=MagicMock(),
                body={"view": {"id": "V123"}, "trigger_id": "TRIGGER"},
                context=MagicMock(),
                client=client,
                db_session=session,
                form_data=form_data,
                user=MagicMock(email="reporter@example.com"),
            )

    assert created["project"] is not None, "the case was filed with no project at all"
    assert created["project"].name == target.name, "the case was filed against the wrong project"

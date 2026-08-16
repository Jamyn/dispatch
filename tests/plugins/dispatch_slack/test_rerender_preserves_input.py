"""Text the reporter has already typed must survive a modal re-render (#144).

Choosing a project in "Report Incident" or "Open a Case" re-renders the modal
so the type/severity/priority/assignee selects can be scoped to that project.
Rebuilt without an ``initial_value``, the title and description inputs came
back blank and whatever had been typed was gone with no warning -- and "Open a
Case" lost them a second time when the case type was then changed.

These drive the real Bolt app with real block_actions payloads and assert on
the view that came back, so a regression shows up as the modal the reporter
actually sees.
"""

import json
from unittest.mock import MagicMock, patch

from slack_sdk.web import WebClient

from dispatch.plugins.dispatch_slack.case.enums import CaseReportActions
from dispatch.plugins.dispatch_slack.fields import DefaultActionIds, DefaultBlockIds
from dispatch.plugins.dispatch_slack.incident.enums import IncidentReportActions

# The payload shape lives in one place so a change to what Slack posts does not
# have to be mirrored here.
from tests.plugins.dispatch_slack.test_project_modals import selection_payload

TYPED_TITLE = "database is returning 500s"
TYPED_DESCRIPTION = "started around 14:00, roughly one request in ten"


def with_typed_text(payload: dict, title: str, description: str) -> dict:
    """Add the two plain-text inputs to a payload's view state, as Slack does."""
    payload["view"]["state"]["values"].update(
        {
            DefaultBlockIds.title_input: {
                DefaultActionIds.title_input: {"type": "plain_text_input", "value": title}
            },
            DefaultBlockIds.description_input: {
                DefaultActionIds.description_input: {
                    "type": "plain_text_input",
                    "value": description,
                }
            },
        }
    )
    return payload


def rerendered_view(dispatch_interaction, payload: dict) -> dict:
    views_update = MagicMock(return_value={"view": {"id": "V123"}, "trigger_id": "TRIGGER"})
    with patch.object(WebClient, "views_update", views_update):
        response = dispatch_interaction(payload)

    assert response.status == 200, response.body
    assert views_update.called, "the selection re-rendered nothing"
    return views_update.call_args.kwargs["view"]


def initial_values(view: dict) -> dict[str, str | None]:
    """The ``initial_value`` of the title and description inputs, by block id."""
    wanted = {DefaultBlockIds.title_input, DefaultBlockIds.description_input}
    return {
        block["block_id"]: block["element"].get("initial_value")
        for block in view["blocks"]
        if block.get("block_id") in wanted
    }


def assert_text_survived(view: dict) -> None:
    values = initial_values(view)
    assert values.get(DefaultBlockIds.title_input) == TYPED_TITLE, (
        "the re-rendered modal lost the typed title"
    )
    assert values.get(DefaultBlockIds.description_input) == TYPED_DESCRIPTION, (
        "the re-rendered modal lost the typed description"
    )


def test_report_incident_keeps_typed_text_when_a_project_is_chosen(
    session, incident_with_related_records, dispatch_interaction, single_default_organization
):
    project = incident_with_related_records.project
    subject = {"type": "incident", "organization_slug": "default", "channel_id": "C123"}

    payload = with_typed_text(
        selection_payload(IncidentReportActions.project_select, subject, project),
        TYPED_TITLE,
        TYPED_DESCRIPTION,
    )

    assert_text_survived(rerendered_view(dispatch_interaction, payload))


def test_open_a_case_keeps_typed_text_when_a_project_is_chosen(
    session, case_with_related_records, dispatch_interaction, single_default_organization
):
    project = case_with_related_records.project
    subject = {"type": "case", "organization_slug": "default", "channel_id": "C123"}

    payload = with_typed_text(
        selection_payload(CaseReportActions.project_select, subject, project),
        TYPED_TITLE,
        TYPED_DESCRIPTION,
    )

    assert_text_survived(rerendered_view(dispatch_interaction, payload))


def test_open_a_case_keeps_typed_text_when_the_case_type_is_changed(
    session, case_with_related_records, dispatch_interaction, single_default_organization
):
    """The second re-render: Open a Case dropped the text again here."""
    project = case_with_related_records.project
    case_type = case_with_related_records.case_type

    subject = {"type": "case", "organization_slug": "default", "channel_id": "C123"}
    payload = with_typed_text(
        selection_payload(CaseReportActions.project_select, subject, project),
        TYPED_TITLE,
        TYPED_DESCRIPTION,
    )
    # Re-aim it at the case type select: same view state, a different action.
    payload["view"]["state"]["values"][DefaultBlockIds.case_type_select] = {
        CaseReportActions.case_type_select: {
            "type": "static_select",
            "selected_option": {
                "text": {"type": "plain_text", "text": case_type.name},
                "value": str(case_type.id),
            },
        }
    }
    payload["actions"] = [
        {
            "type": "static_select",
            "action_id": CaseReportActions.case_type_select,
            "block_id": DefaultBlockIds.case_type_select,
            "selected_option": {
                "text": {"type": "plain_text", "text": case_type.name},
                "value": str(case_type.id),
            },
            "action_ts": "1.0",
        }
    ]

    assert_text_survived(rerendered_view(dispatch_interaction, payload))


def test_an_untouched_input_re_renders_without_an_initial_value(
    session, incident_with_related_records, dispatch_interaction, single_default_organization
):
    """Slack rejects an empty ``initial_value``, so "" must become None."""
    project = incident_with_related_records.project
    subject = {"type": "incident", "organization_slug": "default", "channel_id": "C123"}

    payload = with_typed_text(
        selection_payload(IncidentReportActions.project_select, subject, project), "", ""
    )

    view = rerendered_view(dispatch_interaction, payload)

    assert initial_values(view) == {
        DefaultBlockIds.title_input: None,
        DefaultBlockIds.description_input: None,
    }, json.dumps(view["blocks"])

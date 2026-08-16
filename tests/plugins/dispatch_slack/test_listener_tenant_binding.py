"""Every listener must bind its session to the request's organization (#140).

``db_middleware`` reads ``context["subject"].organization_slug`` and falls back
to the *default* organization when no subject is set yet;
``action_context_middleware`` is what sets that subject, from the view's
``private_metadata``. Listed the other way round, ``db_middleware`` runs first,
sees no subject, and opens a session on the default organization's schema --
so a user in any other tenant has the request served against the wrong
database. Three "Open a Case" listeners had them reversed.

The assertion is the slug ``db_middleware`` opened its session with, which is
the decision the ordering actually controls. Asserting on a query result
instead would need two populated schemas and would still pass if the fallback
happened to name the same organization.
"""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from slack_sdk.web import WebClient

import dispatch.plugins.dispatch_slack.middleware as slack_middleware
from dispatch.plugins.dispatch_slack.case.enums import CaseReportActions
from dispatch.plugins.dispatch_slack.fields import DefaultActionIds, DefaultBlockIds
from dispatch.plugins.dispatch_slack.incident.enums import IncidentReportActions

# A slug that is deliberately not the default, so a fallback to the default
# organization cannot coincidentally produce the right answer.
TENANT_SLUG = "not_the_default_org"


@pytest.fixture
def session_slugs(session):
    """Record the organization slug every ``db_middleware`` session opens on.

    The real session is still handed back, bound to the suite's schema, so the
    listener runs to completion and a failure is the slug rather than a crash
    somewhere downstream.
    """
    slugs: list[str] = []
    real = slack_middleware.get_organization_session

    @contextmanager
    def record(slug: str):
        slugs.append(slug)
        with real("default") as db_session:
            yield db_session

    with patch.object(slack_middleware, "get_organization_session", record):
        yield slugs


def selection_payload(action_id: str, subject_type: str, project_id: int) -> dict:
    """The block_actions payload Slack posts when a project is chosen."""
    selected = {
        "text": {"type": "plain_text", "text": "A Project"},
        "value": str(project_id),
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
            "private_metadata": json.dumps(
                {
                    "type": subject_type,
                    "organization_slug": TENANT_SLUG,
                    "channel_id": "C123",
                    "project_id": str(project_id),
                }
            ),
            "state": {
                "values": {
                    DefaultBlockIds.project_select: {
                        action_id: {"type": "external_select", "selected_option": selected}
                    },
                    DefaultBlockIds.title_input: {
                        DefaultActionIds.title_input: {"type": "plain_text_input", "value": "t"}
                    },
                    DefaultBlockIds.description_input: {
                        DefaultActionIds.description_input: {
                            "type": "plain_text_input",
                            "value": "d",
                        }
                    },
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


def run(dispatch_interaction, payload: dict) -> None:
    views_update = MagicMock(return_value={"view": {"id": "V123"}, "trigger_id": "TRIGGER"})
    with patch.object(WebClient, "views_update", views_update):
        response = dispatch_interaction(payload)

    assert response.status == 200, response.body


def test_open_a_case_project_select_binds_to_the_requesting_organization(
    session, case_with_related_records, dispatch_interaction, session_slugs
):
    payload = selection_payload(
        CaseReportActions.project_select, "case", case_with_related_records.project.id
    )

    run(dispatch_interaction, payload)

    assert session_slugs == [TENANT_SLUG], (
        "the session was opened against the wrong organization's schema"
    )


def test_open_a_case_case_type_select_binds_to_the_requesting_organization(
    session, case_with_related_records, dispatch_interaction, session_slugs
):
    case_type = case_with_related_records.case_type
    payload = selection_payload(
        CaseReportActions.project_select, "case", case_with_related_records.project.id
    )
    selected = {
        "text": {"type": "plain_text", "text": case_type.name},
        "value": str(case_type.id),
    }
    payload["view"]["state"]["values"][DefaultBlockIds.case_type_select] = {
        CaseReportActions.case_type_select: {"type": "static_select", "selected_option": selected}
    }
    payload["actions"] = [
        {
            "type": "static_select",
            "action_id": CaseReportActions.case_type_select,
            "block_id": DefaultBlockIds.case_type_select,
            "selected_option": selected,
            "action_ts": "1.0",
        }
    ]

    run(dispatch_interaction, payload)

    assert session_slugs == [TENANT_SLUG], (
        "the session was opened against the wrong organization's schema"
    )


def test_report_incident_project_select_already_binds_correctly(
    session, incident_with_related_records, dispatch_interaction, session_slugs
):
    """The incident side was already ordered correctly; this pins it there."""
    payload = selection_payload(
        IncidentReportActions.project_select, "incident", incident_with_related_records.project.id
    )

    run(dispatch_interaction, payload)

    assert session_slugs == [TENANT_SLUG]


def test_open_a_case_submission_binds_to_the_requesting_organization(
    session, case_with_related_records, dispatch_interaction, session_slugs
):
    """The one that matters most: this is where the case is actually written."""
    project = case_with_related_records.project
    payload = selection_payload(CaseReportActions.project_select, "case", project.id)
    payload["type"] = "view_submission"
    payload["view"]["callback_id"] = CaseReportActions.submit
    payload["view"]["state"]["values"][f"{DefaultBlockIds.case_assignee_select}-0"] = {
        CaseReportActions.assignee_select: {"type": "users_select", "selected_user": "U456"}
    }
    del payload["actions"]

    users_info = MagicMock(
        return_value={"user": {"profile": {"email": "someone@example.com"}, "is_bot": False}}
    )
    with (
        patch.object(WebClient, "users_info", users_info),
        patch(
            "dispatch.plugins.dispatch_slack.case.interactive.case_service.create",
            return_value=MagicMock(id=1),
        ),
        patch("dispatch.plugins.dispatch_slack.case.interactive.case_flows.case_new_create_flow"),
        # The user lookup would otherwise create a real row for a tenant this
        # suite has no schema for; which slug db_middleware bound to is the
        # only thing under test here.
        patch.object(
            slack_middleware.user_service,
            "get_or_create",
            return_value=MagicMock(email="reporter@example.com"),
        ),
        patch("dispatch.plugins.dispatch_slack.case.interactive.send_success_modal"),
    ):
        run(dispatch_interaction, payload)

    assert session_slugs == [TENANT_SLUG], (
        "the case was written against the wrong organization's schema"
    )

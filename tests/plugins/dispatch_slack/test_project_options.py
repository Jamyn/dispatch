"""Coverage for the project type-ahead behind the external project select.

Past Slack's 100-option limit the select carries no options of its own and asks
for them over the block_suggestion route instead (#86). The bug #86 describes
lives at the Slack boundary, so these drive the real Bolt app with real
block_suggestion payloads rather than calling the listener directly.
"""

import json
from unittest.mock import patch

import pytest

# Deliberately the production wiring, not `.options` directly: endpoints.py is
# the only thing that registers the listener in a running deployment, so
# importing the module under test here would let that import be deleted with
# the suite still green.
import dispatch.plugins.dispatch_slack.endpoints  # noqa: F401
from dispatch.plugins.dispatch_slack.case.enums import CaseEscalateActions, CaseReportActions
from dispatch.plugins.dispatch_slack.config import MAX_SELECT_OPTIONS
from dispatch.plugins.dispatch_slack.fields import DefaultActionIds
from dispatch.plugins.dispatch_slack.incident.enums import (
    IncidentReportActions,
    IncidentUpdateActions,
)

PROJECT_SELECT_ACTION_IDS = [
    DefaultActionIds.project_select,
    IncidentUpdateActions.project_select,
    IncidentReportActions.project_select,
    CaseEscalateActions.project_select,
    CaseReportActions.project_select,
]


def suggestion_payload(query: str = "", action_id: str = DefaultActionIds.project_select) -> dict:
    """A block_suggestion payload of the shape Slack posts to /slack/menu."""
    return {
        "type": "block_suggestion",
        "team": {"id": "T123", "domain": "example"},
        "user": {"id": "U123", "name": "someone"},
        "api_app_id": "A123",
        "token": "verification-token",
        "container": {"type": "view", "view_id": "V123"},
        "action_id": action_id,
        "block_id": "project-select",
        "value": query,
        "view": {
            "id": "V123",
            "type": "modal",
            "private_metadata": json.dumps(
                {"type": "case", "organization_slug": "default", "channel_id": "C123"}
            ),
            "state": {"values": {}},
        },
    }


@pytest.fixture
def load_response(dispatch_interaction):
    """Run a block_suggestion through Bolt and return the body it answered with."""

    def load(query: str = "", action_id: str = DefaultActionIds.project_select) -> dict:
        response = dispatch_interaction(suggestion_payload(query, action_id))

        assert response.status == 200, response.body
        return json.loads(response.body)

    return load


@pytest.fixture
def load_options(load_response):
    """The options offered, however they were carried.

    A truncated answer comes back as a single labelled `option_group` rather
    than a bare option list (#146); to everything asserting on which projects
    were offered, the two are the same answer.
    """

    def load(query: str = "", action_id: str = DefaultActionIds.project_select) -> list[dict]:
        body = load_response(query, action_id)
        if "option_groups" in body:
            (group,) = body["option_groups"]
            return group["options"]
        return body["options"]

    return load


def test_an_empty_query_returns_every_project_it_can(session, only_projects, load_options):
    projects = only_projects(5)

    options = load_options()

    assert {o["value"] for o in options} == {str(p.id) for p in projects}


def test_options_are_bounded_by_slacks_limit(session, only_projects, load_options):
    """1000 projects, one query: Slack accepts at most 100 options back."""
    only_projects(1000)

    assert len(load_options()) == MAX_SELECT_OPTIONS


def test_a_query_matching_more_than_slack_allows_is_still_bounded(
    session, only_projects, load_options
):
    only_projects(display_names=[f"security-{i:04d}" for i in range(500)])

    assert len(load_options("security")) == MAX_SELECT_OPTIONS


def test_a_query_narrows_to_matching_projects(session, only_projects, load_options):
    only_projects(display_names=["Security Engineering", "Payments", "Security Ops"])

    labels = [o["text"]["text"] for o in load_options("sec")]

    assert labels == ["Security Engineering", "Security Ops"]


def test_search_is_case_insensitive(session, only_projects, load_options):
    only_projects(display_names=["Security Engineering", "Payments"])

    assert load_options("SEC") == load_options("sec")


def test_a_project_is_findable_by_name_as_well_as_display_name(
    session, only_projects, load_options
):
    projects = only_projects(display_names=["Totally Different"])

    options = load_options(projects[0].name)

    assert [o["value"] for o in options] == [str(projects[0].id)]


def test_no_matches_returns_an_empty_option_set(session, only_projects, load_options):
    only_projects(display_names=["Payments"])

    assert load_options("nothing-matches-this") == []


def test_no_projects_returns_an_empty_option_set(session, only_projects, load_options):
    only_projects(0)

    assert load_options() == []


def test_disabled_projects_are_not_offered(session, only_projects, load_options):
    projects = only_projects(display_names=["Security One", "Security Two"])
    projects[0].enabled = False
    session.commit()

    assert [o["value"] for o in load_options("security")] == [str(projects[1].id)]


def test_option_values_are_project_ids_not_names(session, only_projects, load_options):
    projects = only_projects(display_names=["Security", "Security"])

    options = load_options("security")

    assert [o["text"]["text"] for o in options] == ["Security", "Security"]
    assert {o["value"] for o in options} == {str(p.id) for p in projects}


def test_an_empty_display_name_falls_back_to_the_project_name(session, only_projects, load_options):
    projects = only_projects(display_names=[""])

    options = load_options(projects[0].name)

    assert [o["text"]["text"] for o in options] == [projects[0].name]


def test_a_long_display_name_is_truncated_to_slacks_limit(session, only_projects, load_options):
    only_projects(display_names=["y" * 200])

    assert load_options("yyy")[0]["text"]["text"] == "y" * 75


def test_results_are_ordered_by_label_case_insensitively(session, only_projects, load_options):
    only_projects(display_names=["zulu match", "Alpha match", "mike match"])

    labels = [o["text"]["text"] for o in load_options("match")]

    assert labels == ["Alpha match", "mike match", "zulu match"]


def test_repeating_a_query_returns_the_same_order(session, only_projects, load_options):
    only_projects(60)

    assert load_options() == load_options()


def test_a_wildcard_in_the_query_is_matched_literally(session, only_projects, load_options):
    """An unescaped `%` would match every project instead of none."""
    only_projects(display_names=["Payments", "Security"])

    assert load_options("%") == []


@pytest.mark.parametrize("action_id", PROJECT_SELECT_ACTION_IDS, ids=str)
def test_every_project_select_action_id_is_served(session, only_projects, load_options, action_id):
    """A select whose action id has no options handler opens to an empty menu."""
    projects = only_projects(2)

    options = load_options(action_id=action_id)

    assert {o["value"] for o in options} == {str(p.id) for p in projects}


def test_a_failed_lookup_answers_with_no_options_rather_than_an_error(
    session, only_projects, load_options
):
    """Slack shows the user an empty menu; it must not see a 500 or a traceback."""
    only_projects(3)

    with patch(
        "dispatch.project.service.get_all_enabled", side_effect=RuntimeError("database is down")
    ):
        assert load_options() == []


# --- relevance and truncation (#146) -----------------------------------------


def test_an_exact_match_survives_a_flood_of_substring_matches(session, only_projects, load_options):
    """The bug: typing a project's whole name and not seeing it.

    200 projects whose names merely contain "sec" fill the limit alphabetically
    and push the one actually called Security out of the response entirely.
    """
    only_projects(display_names=["Security", *(f"aaa-sec-{i:03d}" for i in range(200))])

    labels = [o["text"]["text"] for o in load_options("sec")]

    assert labels[0] == "Security", labels[:5]


def test_a_prefix_match_outranks_a_substring_match(session, only_projects, load_options):
    only_projects(display_names=["aaa-contains-sec", "Security Engineering"])

    labels = [o["text"]["text"] for o in load_options("sec")]

    assert labels == ["Security Engineering", "aaa-contains-sec"]


def test_within_a_tier_results_are_still_alphabetical(session, only_projects, load_options):
    only_projects(display_names=["sec-zulu", "sec-alpha", "sec-mike"])

    labels = [o["text"]["text"] for o in load_options("sec")]

    assert labels == ["sec-alpha", "sec-mike", "sec-zulu"]


def test_a_multi_word_query_matches_in_any_order(session, only_projects, load_options):
    """`security eng` is the same request whatever sits between the words."""
    only_projects(display_names=["Security Platform Engineering", "Payments"])

    labels = [o["text"]["text"] for o in load_options("security eng")]

    assert labels == ["Security Platform Engineering"]


def test_every_word_of_a_multi_word_query_must_match(session, only_projects, load_options):
    only_projects(display_names=["Security Engineering", "Security Ops"])

    labels = [o["text"]["text"] for o in load_options("security ops")]

    assert labels == ["Security Ops"]


def test_a_wildcard_in_one_word_of_a_query_is_matched_literally(
    session, only_projects, load_options
):
    only_projects(display_names=["Security Engineering"])

    assert load_options("security %") == []


def test_a_full_answer_is_not_labelled_as_truncated(session, only_projects, load_response):
    """Exactly the limit is a complete answer, not a truncated one."""
    only_projects(MAX_SELECT_OPTIONS)

    body = load_response()

    assert "option_groups" not in body, body
    assert len(body["options"]) == MAX_SELECT_OPTIONS


def test_a_truncated_answer_says_so(session, only_projects, load_response):
    only_projects(MAX_SELECT_OPTIONS + 1)

    body = load_response()

    assert "options" not in body, body
    (group,) = body["option_groups"]
    assert len(group["options"]) == MAX_SELECT_OPTIONS
    assert str(MAX_SELECT_OPTIONS) in group["label"]["text"], group["label"]

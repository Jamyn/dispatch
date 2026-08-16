"""Regression coverage for the project select behind every incident/case modal.

Slack caps a static select at 100 options, so a deployment with more enabled
projects than that could not open any modal built through ``project_select``
(#86). These tests drive the real builder against real rows either side of that
boundary and assert on the Block Kit payload it produces, not merely that it
does not raise.
"""

import pytest
from blockkit.core import FieldValidationError

from dispatch.plugins.dispatch_slack.config import MAX_SELECT_OPTIONS
from dispatch.plugins.dispatch_slack.fields import (
    DefaultActionIds,
    DefaultBlockIds,
    project_select,
)
from dispatch.project.models import Project


def build(db_session, **kwargs) -> dict | None:
    """Build the project select the way a modal does."""
    block = project_select(db_session=db_session, **kwargs)
    return None if block is None else block.build()


def element(view_block: dict) -> dict:
    return view_block["element"]


# One count either side of the boundary, for the properties that must hold in
# both modes.
BOTH_MODES = [2, MAX_SELECT_OPTIONS + 1]


def test_no_projects_yields_no_block(session, only_projects):
    only_projects(0)

    assert build(session) is None


def test_one_project_yields_a_single_static_option(session, only_projects):
    only_projects(1)

    block = build(session)

    assert element(block)["type"] == "static_select"
    assert len(element(block)["options"]) == 1


@pytest.mark.parametrize("count", [1, 99, MAX_SELECT_OPTIONS])
def test_static_select_up_to_the_slack_limit(session, only_projects, count):
    """At or below Slack's cap the options stay embedded, as they always were."""
    only_projects(count)

    block = build(session)

    assert element(block)["type"] == "static_select"
    assert len(element(block)["options"]) == count


@pytest.mark.parametrize("count", [MAX_SELECT_OPTIONS + 1, 500, 1000])
def test_external_select_past_the_slack_limit(session, only_projects, count):
    """The original #86 failure: more projects than a static select can hold."""
    only_projects(count)

    block = build(session)

    assert element(block)["type"] == "external_select"
    assert "options" not in element(block), "the whole project list is still embedded"


def test_the_original_919_project_failure(session, only_projects):
    """The count that tripped the limit when #86 was found."""
    only_projects(919)

    view_block = build(session)

    assert element(view_block)["type"] == "external_select"


def test_one_option_past_the_limit_is_what_a_static_select_rejects():
    """Why the mode switches where it does -- the constraint being designed around."""
    from dispatch.plugins.dispatch_slack.fields import static_select_block

    options = [{"text": f"p{i}", "value": str(i)} for i in range(MAX_SELECT_OPTIONS + 1)]

    with pytest.raises(FieldValidationError):
        static_select_block(options=options, placeholder="Select Project", label="Project").build()


@pytest.mark.parametrize("count", BOTH_MODES)
def test_block_and_action_ids_are_unchanged_in_either_mode(session, only_projects, count):
    """Every caller reads state back by these ids; they must not move."""
    only_projects(count)

    block = build(session)

    assert block["block_id"] == DefaultBlockIds.project_select
    assert element(block)["action_id"] == DefaultActionIds.project_select


@pytest.mark.parametrize("count", BOTH_MODES)
def test_dispatch_action_survives_in_either_mode(session, only_projects, count):
    """Selecting a project re-renders the modal; that needs dispatch_action."""
    only_projects(count)

    block = build(session, dispatch_action=True)

    assert block["dispatch_action"] is True


def test_option_values_are_project_ids(session, only_projects):
    projects = only_projects(3)

    options = element(build(session))["options"]

    assert {o["value"] for o in options} == {str(p.id) for p in projects}
    # Slack drops an option with a non-string value without saying so.
    assert all(isinstance(o["value"], str) for o in options)


@pytest.mark.parametrize("count", BOTH_MODES)
def test_initial_option_is_preserved_in_either_mode(session, only_projects, count):
    projects = only_projects(count)
    chosen = projects[0]

    block = build(session, initial_option={"text": chosen.display_name, "value": chosen.id})

    assert element(block)["initial_option"]["value"] == str(chosen.id)


def test_disabled_projects_are_excluded(session, only_projects):
    projects = only_projects(4)
    projects[0].enabled = False
    projects[1].enabled = False
    session.commit()

    options = element(build(session))["options"]

    assert {o["value"] for o in options} == {str(p.id) for p in projects[2:]}


def test_options_are_ordered_by_label_case_insensitively(session, only_projects):
    only_projects(display_names=["zulu", "Alpha", "mike"])

    labels = [o["text"]["text"] for o in element(build(session))["options"]]

    assert labels == ["Alpha", "mike", "zulu"]


def test_ordering_is_stable_across_repeated_builds(session, only_projects):
    only_projects(50)

    first = [o["value"] for o in element(build(session))["options"]]
    session.expire_all()
    second = [o["value"] for o in element(build(session))["options"]]

    assert first == second


def test_duplicate_display_names_keep_distinct_values(session, only_projects):
    projects = only_projects(2, display_names=["Security", "Security"])

    options = element(build(session))["options"]

    assert [o["text"]["text"] for o in options] == ["Security", "Security"]
    assert {o["value"] for o in options} == {str(p.id) for p in projects}


def test_an_empty_display_name_falls_back_to_the_project_name(session, only_projects):
    """``display_name`` defaults to '' at the column level, and Slack rejects
    empty option text -- so the fallback is what keeps the modal openable."""
    only_projects(1, display_names=[""])
    project = session.query(Project).filter(Project.enabled.is_(True)).one()

    options = element(build(session))["options"]

    assert options[0]["text"]["text"] == project.name


def test_a_long_display_name_is_truncated_to_slacks_limit(session, only_projects):
    only_projects(1, display_names=["x" * 200])

    options = element(build(session))["options"]

    assert options[0]["text"]["text"] == "x" * 75


def test_a_project_with_no_name_at_all_is_still_selectable(session, only_projects):
    """`name` is nullable and `display_name` defaults to '', so a label has to
    come from somewhere -- an option with empty text is rejected outright."""
    project = only_projects(1)[0]
    project.name = None
    project.display_name = ""
    session.commit()

    options = element(build(session))["options"]

    assert [o["text"]["text"] for o in options] == [f"Project {project.id}"]
    assert [o["value"] for o in options] == [str(project.id)]

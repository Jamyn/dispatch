"""Coverage for the tag type-ahead behind the tag multi-select (#141).

`search_filter_sort_paginate` defaults to 5 results per page, and the handler
did not override it, so a project with more than five matching tags was
unsearchable past the fifth alphabetically -- Slack allows up to 100 options
in a block_suggestion response. These drive the real Bolt app with real
block_suggestion payloads, same as ``test_project_options.py``, so a
regression at the Slack boundary -- not just in the handler's own arguments --
would be caught.
"""

import json

import pytest

# Deliberately the production wiring: app.py is what registers this listener
# in a running deployment (`from . import options`), so importing options.py
# directly here would let that import be deleted with the suite still green.
import dispatch.plugins.dispatch_slack.endpoints  # noqa: F401
from dispatch.plugins.dispatch_slack.config import MAX_SELECT_OPTIONS
from dispatch.plugins.dispatch_slack.fields import DefaultActionIds
from tests.factories import ProjectFactory, TagFactory, TagTypeFactory


def suggestion_payload(query: str, project_id: int) -> dict:
    """A block_suggestion payload of the shape Slack posts to /slack/menu."""
    return {
        "type": "block_suggestion",
        "team": {"id": "T123", "domain": "example"},
        "user": {"id": "U123", "name": "someone"},
        "api_app_id": "A123",
        "token": "verification-token",
        "container": {"type": "view", "view_id": "V123"},
        "action_id": DefaultActionIds.tags_multi_select,
        "block_id": "tag-multi-select",
        "value": query,
        "view": {
            "id": "V123",
            "type": "modal",
            "private_metadata": json.dumps(
                {
                    "type": "incident",
                    "organization_slug": "default",
                    "channel_id": "C123",
                    "project_id": str(project_id),
                }
            ),
            "state": {"values": {}},
        },
    }


@pytest.fixture
def load_options(dispatch_interaction):
    """Run a block_suggestion through Bolt and return the options it answered with."""

    def load(query: str, project_id: int) -> list[dict]:
        response = dispatch_interaction(suggestion_payload(query, project_id))

        assert response.status == 200, response.body
        return json.loads(response.body)["options"]

    return load


def test_an_empty_query_returns_every_tag_in_the_project(session, load_options):
    project = ProjectFactory()
    tags = [TagFactory(project=project) for _ in range(3)]
    session.commit()

    options = load_options("", project.id)

    assert {o["value"] for o in options} == {str(t.id) for t in tags}


def test_options_are_bounded_by_slacks_limit(session, load_options):
    """A project with more than 100 tags: Slack accepts at most 100 options back."""
    project = ProjectFactory()
    for _ in range(150):
        TagFactory(project=project)
    session.commit()

    assert len(load_options("", project.id)) == MAX_SELECT_OPTIONS


def test_more_than_five_matching_tags_are_all_offered(session, load_options):
    """The bug: a sixth-and-later alphabetical match used to be unreachable."""
    project = ProjectFactory()
    tags = [TagFactory(project=project, name=f"needle-{i:02d}") for i in range(8)]
    session.commit()

    options = load_options("needle", project.id)

    assert {o["value"] for o in options} == {str(t.id) for t in tags}


def test_a_query_narrows_to_matching_tags(session, load_options):
    project = ProjectFactory()
    TagFactory(project=project, name="security-incident")
    TagFactory(project=project, name="payments")
    session.commit()

    options = load_options("security", project.id)

    assert len(options) == 1
    assert "security-incident" in options[0]["text"]["text"]


def test_tags_from_other_projects_are_not_offered(session, load_options):
    project = ProjectFactory()
    other = ProjectFactory()
    tag = TagFactory(project=project, name="only-here")
    TagFactory(project=other, name="not-here")
    session.commit()

    options = load_options("", project.id)

    assert [o["value"] for o in options] == [str(tag.id)]


def test_a_type_qualified_query_still_narrows_by_the_name_half(session, load_options):
    """`type/name` searches on the name half; the type half is inert (#165)."""
    project = ProjectFactory()
    tag_type = TagTypeFactory(project=project, name="incident-type")
    wanted = TagFactory(project=project, tag_type=tag_type, name="alpha-one")
    TagFactory(project=project, tag_type=tag_type, name="unrelated")
    session.commit()

    options = load_options("incident-type/alpha", project.id)

    assert [o["value"] for o in options] == [str(wanted.id)]


def test_a_query_with_more_than_one_slash_is_acked_with_nothing(
    session, dispatch_interaction, load_options
):
    project = ProjectFactory()
    TagFactory(project=project, name="alpha")
    session.commit()

    response = dispatch_interaction(suggestion_payload("a/b/c", project.id))

    # A bare ack() carries no options at all, which Slack renders as an empty
    # menu -- the same thing the user sees for a query that matches nothing.
    assert response.status == 200
    assert not response.body


def test_no_matches_returns_an_empty_option_set(session, load_options):
    project = ProjectFactory()
    TagFactory(project=project, name="alpha")
    session.commit()

    assert load_options("nothing-matches-this", project.id) == []

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


def test_a_type_qualified_query_narrows_by_the_name_half(session, load_options):
    """`type/name` searches on the name half."""
    project = ProjectFactory()
    tag_type = TagTypeFactory(project=project, name="incident-type")
    wanted = TagFactory(project=project, tag_type=tag_type, name="alpha-one")
    TagFactory(project=project, tag_type=tag_type, name="unrelated")
    session.commit()

    options = load_options("incident-type/alpha", project.id)

    assert [o["value"] for o in options] == [str(wanted.id)]


def test_a_type_qualified_query_excludes_matching_names_of_other_types(session, load_options):
    """The bug: the type half narrowed nothing, so every type's `alpha*` came back.

    A `TagType` clause makes sqlalchemy-filters auto-join tag_type through
    project rather than through tag.tag_type_id, so a name that matched any
    type in the project left every tag in it eligible (#165).
    """
    project = ProjectFactory()
    wanted_type = TagTypeFactory(project=project, name="incident-type")
    other_type = TagTypeFactory(project=project, name="service")
    wanted = TagFactory(project=project, tag_type=wanted_type, name="alpha-one")
    TagFactory(project=project, tag_type=other_type, name="alpha-two")
    session.commit()

    options = load_options("incident-type/alpha", project.id)

    assert [o["value"] for o in options] == [str(wanted.id)]


def test_a_tag_type_name_that_is_only_a_prefix_resolves_to_nothing(session, dispatch_interaction):
    """Resolution is by exact name: a prefix of a real type is not that type.

    No exact match exists here, so a substring or case-insensitive lookup would
    resolve `incident-type` to `incident-type-legacy` and offer its tags.
    """
    project = ProjectFactory()
    longer = TagTypeFactory(project=project, name="incident-type-legacy")
    TagFactory(project=project, tag_type=longer, name="alpha-two")
    session.commit()

    response = dispatch_interaction(suggestion_payload("incident-type/alpha", project.id))

    assert response.status == 200
    assert not response.body


def test_a_type_qualified_query_offers_every_tag_of_that_type(session, load_options):
    """With no name half, the type alone scopes the menu.

    `type/` is a real mid-typing state, reached on the keystroke after the
    slash, so the type must still scope the menu with nothing to search on.
    """
    project = ProjectFactory()
    wanted_type = TagTypeFactory(project=project, name="incident-type")
    TagTypeFactory(project=project, name="service")
    tags = [TagFactory(project=project, tag_type=wanted_type, name=f"a-{i}") for i in range(3)]
    TagFactory(project=project, name="elsewhere")
    session.commit()

    options = load_options("incident-type/", project.id)

    assert sorted(o["value"] for o in options) == sorted(str(t.id) for t in tags)


def test_an_unknown_tag_type_is_acked_with_nothing(session, dispatch_interaction):
    """No such type: the same empty menu a query matching nothing produces."""
    project = ProjectFactory()
    tag_type = TagTypeFactory(project=project, name="incident-type")
    TagFactory(project=project, tag_type=tag_type, name="alpha-one")
    session.commit()

    response = dispatch_interaction(suggestion_payload("no-such-type/alpha", project.id))

    assert response.status == 200
    assert not response.body


def test_a_tag_type_from_another_project_is_not_honoured(session, dispatch_interaction):
    """Type names are resolved within the subject project, not globally."""
    project = ProjectFactory()
    other = ProjectFactory()
    TagTypeFactory(project=other, name="incident-type")
    TagFactory(project=project, name="alpha-one")
    session.commit()

    response = dispatch_interaction(suggestion_payload("incident-type/alpha", project.id))

    assert response.status == 200
    assert not response.body


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

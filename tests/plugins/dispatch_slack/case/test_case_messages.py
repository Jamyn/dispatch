"""Build-time coverage for the case Slack message builders.

blockkit validates at build() time rather than construction, so a component
tree that constructs cleanly can still raise when it is turned into JSON.
These tests call the builders and assert on the built payload.
"""

from dispatch.plugins.dispatch_slack.case.messages import (
    create_case_channel_migration_message,
    create_case_message,
    create_case_thread_migration_message,
    create_case_user_not_in_slack_workspace_message,
)
from tests.factories import CaseFactory, ParticipantFactory


def test_create_case_message_builds(session):
    # create_case_message dereferences assignee.individual, which the plain
    # case fixture leaves unset.
    case = CaseFactory(assignee=ParticipantFactory())

    blocks = create_case_message(case=case, channel_id="C12345")

    assert blocks
    assert all("type" in block for block in blocks)
    assert case.title in str(blocks)


def test_create_case_thread_migration_message_builds():
    blocks = create_case_thread_migration_message(channel_weblink="https://example.com/c")

    context = blocks[0]
    assert context["type"] == "context"
    # Raw strings are no longer coerced, so the element must carry its own type.
    assert context["elements"][0]["type"] == "mrkdwn"
    assert "https://example.com/c" in context["elements"][0]["text"]


def test_create_case_channel_migration_message_builds():
    blocks = create_case_channel_migration_message(thread_weblink="https://example.com/t")

    context = blocks[0]
    assert context["type"] == "context"
    assert context["elements"][0]["type"] == "mrkdwn"
    assert "https://example.com/t" in context["elements"][0]["text"]


def test_create_case_user_not_in_slack_workspace_message_builds():
    blocks = create_case_user_not_in_slack_workspace_message(user_email="nobody@example.com")

    context = blocks[0]
    assert context["type"] == "context"
    assert context["elements"][0]["type"] == "mrkdwn"
    assert "nobody@example.com" in context["elements"][0]["text"]

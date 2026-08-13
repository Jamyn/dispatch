"""Send the real Slack builders' output to a real Slack workspace.

Everything else in the suite asserts that blockkit *builds* a payload. Only
Slack can say whether it accepts one, and the two disagree: blockkit validates
against its own model of Block Kit, which is not the API. This suite closes
that gap for the surfaces the blockkit 2.x migration rewrote.

Skipped unless a workspace is configured, so it is inert locally and in CI by
default.

Configuration
-------------
``DISPATCH_SLACK_TEST_BOT_TOKEN``   ``xoxb-...`` bot token. Required.
``DISPATCH_SLACK_TEST_CHANNEL_ID``  ``C...`` channel to post to. Required.
``DISPATCH_SLACK_TEST_USER_ID``     ``U...`` whose App Home to publish to.
                                    Only the modal tests need it.

Locally, export them or add them to ``docker/.env``, which is already sourced
before running pytest. In CI they come from repository secrets -- see the
``Run pytest`` step in ``.github/workflows/python-ci.yml``.

Creating the app
----------------
https://api.slack.com/apps -> Create New App -> From a manifest, against a
**test** workspace, then Install to Workspace and copy the bot token::

    {
      "display_information": {"name": "Dispatch Blockkit Test"},
      "features": {
        "bot_user": {"display_name": "Dispatch Blockkit Test"},
        "app_home": {"home_tab_enabled": true, "messages_tab_enabled": false}
      },
      "oauth_config": {"scopes": {"bot": ["chat:write", "chat:write.public"]}},
      "settings": {"socket_mode_enabled": false}
    }

``chat:write.public`` lets the bot post to a public channel without being
invited; a private channel needs ``/invite`` first. Channel ID is at the bottom
of the channel's About tab, member ID is under your profile's ``...`` menu.

Why App Home rather than modals
-------------------------------
``views.open`` needs a ``trigger_id``, which only a live user interaction
produces -- that would mean socket mode, an app-level token and a registered
slash command. ``views.publish`` puts the same blocks through the same
server-side validation with none of that, so the modal tests assert on blocks
rather than on the modal envelope (whose title/submit limits blockkit already
enforces locally, and which this migration did not touch).

What this cannot check
----------------------
Rendering, which is client-side. The case action buttons are the surface to
eyeball: ``:mag: Triage``, ``:white_check_mark: Resolve``, ``:pencil: Edit``,
``:slack: Create Channel``, ``:fire: Escalate``.

Those five are the only ``plain_text`` objects in any payload here that carry
an emoji shortcode, which matters because blockkit 2 dropped the
``emoji: true`` field that 1.9.2 emitted and ``emoji`` applies to no other text
type. Verified against a real workspace 2026-08-12: Slack renders the
shortcodes as glyphs with the field absent, so the omission is inert and the
labels need no ``emoji=True``. Re-check here if that ever appears to regress.

This posts real messages and does not delete them. Point it at a throwaway
channel.
"""

import os

import pytest

from dispatch.plugins.dispatch_slack.case.interactive import (
    handle_engage_user_command,
    handle_update_case_command,
)
from dispatch.plugins.dispatch_slack.case.messages import (
    create_case_channel_migration_message,
    create_case_message,
    create_case_thread_migration_message,
)
from dispatch.plugins.dispatch_slack.incident.interactive import (
    handle_add_timeline_event_command,
    handle_assign_role_command,
    handle_engage_oncall_command,
    handle_report_executive_command,
    handle_update_incident_command,
    handle_update_participant_command,
)
from dispatch.plugins.dispatch_slack.incident.messages import (
    create_incident_channel_escalate_message,
)

BOT_TOKEN = os.environ.get("DISPATCH_SLACK_TEST_BOT_TOKEN")
CHANNEL_ID = os.environ.get("DISPATCH_SLACK_TEST_CHANNEL_ID")
USER_ID = os.environ.get("DISPATCH_SLACK_TEST_USER_ID")

pytestmark = pytest.mark.skipif(
    not (BOT_TOKEN and CHANNEL_ID),
    reason="needs DISPATCH_SLACK_TEST_BOT_TOKEN and DISPATCH_SLACK_TEST_CHANNEL_ID",
)


@pytest.fixture(scope="module")
def slack():
    from slack_sdk import WebClient

    return WebClient(token=BOT_TOKEN)


def post(slack, name, blocks):
    """Post blocks and return Slack's response.

    slack_sdk raises SlackApiError on rejection, and its message names the
    offending block, so no assertion adds anything to the failure.
    """
    return slack.chat_postMessage(channel=CHANNEL_ID, blocks=blocks, text=f"[{name}]")


def publish(slack, name, blocks):
    """Publish blocks to App Home, the trigger_id-free path to view validation."""
    return slack.views_publish(
        user_id=USER_ID,
        view={
            "type": "home",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": name[:150]}},
                {"type": "divider"},
                *blocks,
            ],
        },
    )


def test_case_message_is_accepted(slack, case_with_related_records):
    """The case message carries the five shortcode button labels."""
    blocks = create_case_message(case=case_with_related_records, channel_id=CHANNEL_ID)

    assert post(slack, "case", blocks)["ok"]


def test_link_messages_are_accepted(slack):
    """`<url|text>` markup only renders inside mrkdwn, which the migration set explicitly."""
    for name, blocks in (
        (
            "case_thread_migration",
            create_case_thread_migration_message(channel_weblink="https://example.com/channel"),
        ),
        (
            "case_channel_migration",
            create_case_channel_migration_message(thread_weblink="https://example.com/thread"),
        ),
        ("incident_escalate", create_incident_channel_escalate_message()),
    ):
        assert post(slack, name, blocks)["ok"]


INCIDENT_MODAL_HANDLERS = [
    handle_update_incident_command,
    handle_add_timeline_event_command,
    handle_assign_role_command,
    handle_update_participant_command,
    handle_engage_oncall_command,
    handle_report_executive_command,
]

CASE_MODAL_HANDLERS = [handle_update_case_command, handle_engage_user_command]

needs_user_id = pytest.mark.skipif(not USER_ID, reason="needs DISPATCH_SLACK_TEST_USER_ID")


@needs_user_id
@pytest.mark.parametrize("handler", INCIDENT_MODAL_HANDLERS, ids=lambda h: h.__name__)
def test_incident_modal_is_accepted(slack, handler, build_incident_modal):
    assert publish(slack, handler.__name__, build_incident_modal(handler)["blocks"])["ok"]


@needs_user_id
@pytest.mark.parametrize("handler", CASE_MODAL_HANDLERS, ids=lambda h: h.__name__)
def test_case_modal_is_accepted(slack, handler, build_case_modal):
    assert publish(slack, handler.__name__, build_case_modal(handler)["blocks"])["ok"]

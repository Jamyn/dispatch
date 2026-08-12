from dispatch.plugins.dispatch_slack.incident.messages import (
    create_incident_channel_escalate_message,
)


def test_create_incident_channel_escalate_message_builds():
    blocks = create_incident_channel_escalate_message()

    context = blocks[0]
    assert context["type"] == "context"
    # Raw strings are no longer coerced, so the element must carry its own type.
    assert context["elements"][0]["type"] == "mrkdwn"
    assert blocks[1]["type"] == "divider"

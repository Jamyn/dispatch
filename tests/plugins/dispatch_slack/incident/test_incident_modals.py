"""Build coverage for the incident Slack modal handlers.

blockkit validates when a component tree is built, not when it is constructed,
so these handlers can only be proven to produce valid Block Kit by running
them. Setup lives in ../conftest.py; test_slack_live.py runs the same handlers
against a real workspace when one is configured.
"""

import pytest

from dispatch.plugins.dispatch_slack.incident.interactive import (
    handle_add_timeline_event_command,
    handle_assign_role_command,
    handle_engage_oncall_command,
    handle_report_executive_command,
    handle_update_incident_command,
    handle_update_participant_command,
)

MODAL_HANDLERS = [
    handle_update_incident_command,
    handle_add_timeline_event_command,
    handle_assign_role_command,
    handle_update_participant_command,
    handle_engage_oncall_command,
    handle_report_executive_command,
]


@pytest.mark.parametrize("handler", MODAL_HANDLERS, ids=lambda h: h.__name__)
def test_modal_handler_builds_a_valid_view(handler, build_incident_modal):
    view = build_incident_modal(handler)

    assert view["type"] == "modal"
    assert view["blocks"], "modal built with no blocks"

"""Build coverage for the case Slack modal handlers.

blockkit validates when a component tree is built, not when it is constructed,
so these handlers can only be proven to produce valid Block Kit by running
them. Setup lives in ../conftest.py; test_slack_live.py runs the same handlers
against a real workspace when one is configured.
"""

import pytest

from dispatch.plugins.dispatch_slack.case.interactive import (
    handle_engage_user_command,
    handle_escalate_case_command,
    handle_update_case_command,
    report_issue,
    resolve_button_click,
)

# handle_list_signals_command is deliberately absent: it puts the Bolt-context
# plugin config into a query, so a stub config cannot drive it.
MODAL_HANDLERS = [
    handle_update_case_command,
    handle_engage_user_command,
    resolve_button_click,
    # These two build a project select, which the suite's accumulated projects
    # used to push past Slack's 100-option limit -- so they only built in
    # isolation, and only when they ran early enough (#86). Their behaviour
    # either side of that limit is covered in ../test_project_modals.py.
    handle_escalate_case_command,
    report_issue,
]


@pytest.mark.parametrize("handler", MODAL_HANDLERS, ids=lambda h: h.__name__)
def test_modal_handler_builds_a_valid_view(handler, build_case_modal):
    view = build_case_modal(handler)

    assert view["type"] == "modal"
    assert view["blocks"], "modal built with no blocks"

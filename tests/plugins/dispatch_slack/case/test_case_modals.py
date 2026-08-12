"""Build coverage for the case Slack modal handlers.

blockkit validates when a component tree is built, not when it is constructed,
so these handlers can only be proven to produce valid Block Kit by running
them. Setup lives in ../conftest.py; test_slack_live.py runs the same handlers
against a real workspace when one is configured.
"""

import pytest

from dispatch.plugins.dispatch_slack.case.interactive import (
    handle_engage_user_command,
    handle_update_case_command,
    resolve_button_click,
)

# Three handlers are deliberately absent.
# handle_list_signals_command puts the Bolt-context plugin config into a query,
# so a stub config cannot drive it. handle_escalate_case_command and
# report_issue call project_select, which emits one option per project with no
# cap; the suite accumulates far more than Slack's 100-option limit, so they
# only build in isolation.
MODAL_HANDLERS = [
    handle_update_case_command,
    handle_engage_user_command,
    resolve_button_click,
]


@pytest.mark.parametrize("handler", MODAL_HANDLERS, ids=lambda h: h.__name__)
def test_modal_handler_builds_a_valid_view(handler, build_case_modal):
    view = build_case_modal(handler)

    assert view["type"] == "modal"
    assert view["blocks"], "modal built with no blocks"

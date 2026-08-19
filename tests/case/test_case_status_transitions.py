"""Which flows run when a case changes status.

`case_status_transition_flow_dispatcher` is the case lifecycle state machine. It
does no work itself; it decides which of the status flows run, and in what
order. Two rules are carried entirely by that routing:

* reopening a closed case reactivates it *before* anything else, or its
  participants are never brought back, and
* jumping straight past triage still runs the triage flow, so a case escalated
  or closed directly out of `new` is not left without the work triage does.

The flows themselves reach out to Slack and the ticketing plugins, so they are
stood in for here -- what is under test is the routing, and the order.
"""

from unittest.mock import patch

import pytest

import dispatch.case.flows as case_flows
from dispatch.case.enums import CaseStatus

FLOWS = (
    "case_active_status_flow",
    "case_triage_status_flow",
    "case_escalated_status_flow",
    "case_stable_status_flow",
    "case_closed_status_flow",
)


@pytest.fixture
def flows_run(session):
    """Record which status flows the dispatcher runs, in order."""
    calls = []
    patches = [
        patch.object(case_flows, name, side_effect=lambda *a, _n=name, **k: calls.append(_n))
        for name in FLOWS
    ]
    for p in patches:
        p.start()
    yield calls
    for p in patches:
        p.stop()


def transition(case, previous, current, session):
    case_flows.case_status_transition_flow_dispatcher(
        case=case,
        current_status=current,
        previous_status=previous,
        organization_slug="default",
        db_session=session,
    )


@pytest.mark.parametrize(
    "destination",
    [CaseStatus.new, CaseStatus.triage, CaseStatus.escalated, CaseStatus.stable],
)
def test_reopening_a_closed_case_reactivates_it_first(session, case, flows_run, destination):
    """Given a closed case, when it is reopened, then reactivation runs before anything else.

    Reactivation is what brings the participants back. Running it second, or not
    at all, leaves the case open with nobody on it.
    """
    transition(case, CaseStatus.closed, destination, session)

    assert flows_run, f"reopening to {destination} ran no flow at all"
    assert flows_run[0] == "case_active_status_flow", (
        f"reopening to {destination} ran {flows_run} -- reactivation was not first"
    )


@pytest.mark.parametrize(
    "destination,expected_last",
    [
        (CaseStatus.escalated, "case_escalated_status_flow"),
        (CaseStatus.closed, "case_closed_status_flow"),
        (CaseStatus.stable, "case_stable_status_flow"),
    ],
)
def test_leaving_new_without_triaging_still_triages(
    session, case, flows_run, destination, expected_last
):
    """Given a new case, when it skips ahead, then triage runs before the destination.

    A case taken straight from `new` to escalated, closed or stable would
    otherwise never have the triage flow applied to it.
    """
    transition(case, CaseStatus.new, destination, session)

    assert flows_run == ["case_triage_status_flow", expected_last]


def test_escalating_an_already_triaged_case_does_not_triage_it_again(session, case, flows_run):
    """Given a triaged case, when it escalates, then only escalation runs.

    The discriminating case for the rule above: triage is added because it was
    skipped, not on every escalation.
    """
    transition(case, CaseStatus.triage, CaseStatus.escalated, session)

    assert flows_run == ["case_escalated_status_flow"]


def test_a_transition_with_nothing_to_do_runs_nothing(session, case, flows_run):
    """Given a status that needs no work, when it transitions, then no flow runs.

    `triage -> new` is explicitly a no-op; running a flow here would repeat work
    the case has already had.
    """
    transition(case, CaseStatus.triage, CaseStatus.new, session)

    assert flows_run == []

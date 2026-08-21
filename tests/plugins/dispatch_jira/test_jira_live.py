"""Drive the Jira ticket plugin against a real Jira instance.

The plugin had no tests of any kind -- unit or live -- across eight methods
and every incident, case and task ticket path. It is also built to hide its
own failures: `create` catches every exception and returns a synthetic
"jira-error-ticket" pointing back at Dispatch's UI, so a broken configuration
produces something that reads like success. Only a real instance can say
whether a ticket was actually filed, which is why this suite exists before a
fake does.

Skipped unless an instance is configured, so it is inert locally and in CI by
default. The two platforms are independent: configure either and its half runs.

Configuration
-------------
Cloud::

    DISPATCH_JIRA_TEST_CLOUD_URL         https://<site>.atlassian.net
    DISPATCH_JIRA_TEST_CLOUD_USERNAME    Atlassian account email
    DISPATCH_JIRA_TEST_CLOUD_PASSWORD    API token, not the password
    DISPATCH_JIRA_TEST_CLOUD_PROJECT     project key or id, e.g. KAN
    DISPATCH_JIRA_TEST_CLOUD_ISSUE_TYPE  issue type name, e.g. Task

Server/Data Center uses DISPATCH_JIRA_TEST_SERVER_* with the same five names.

Point this at a **throwaway** project. Every test files real issues, and they
are deleted afterwards -- including when a test fails, best effort.

What this covers that a fake cannot
-----------------------------------
- Jira accepts the issue payload the plugin builds, rather than the plugin
  silently substituting a fallback ticket.
- `groupuserpicker` resolves a commander by email address at all. Atlassian
  restricted user search for privacy, and when it returns nothing the plugin
  quietly assigns the Dispatch service account instead.
- The transition named by an incident status exists, or is skipped silently.
"""

import os
import uuid

import pytest
from jira.exceptions import JIRAError

from dispatch.plugins.dispatch_jira.plugin import (
    JiraConfiguration,
    JiraTicketPlugin,
    create_client,
)

SETTINGS = ("url", "username", "password", "project", "issue_type")


def _settings(platform: str) -> dict:
    prefix = f"DISPATCH_JIRA_TEST_{platform.upper()}_"
    return {name: os.environ.get(prefix + name.upper()) for name in SETTINGS}


class Live:
    """One configured instance, and the issues the test filed on it."""

    def __init__(self, platform, settings):
        self.platform = platform
        self.settings = settings
        self.configuration = JiraConfiguration(
            api_url=settings["url"],
            browser_url=settings["url"],
            hosting_type=platform,
            username=settings["username"],
            password=settings["password"],
            default_project_id=settings["project"],
            default_issue_type_name=settings["issue_type"],
        )
        self.plugin = JiraTicketPlugin()
        self.plugin.configuration = self.configuration
        self.client = create_client(self.configuration)
        self.created = []

    @property
    def email(self) -> str:
        return self.settings["username"]

    def title(self, what: str) -> str:
        return f"Dispatch test {what} {uuid.uuid4().hex[:12]}"

    def track(self, ticket: dict) -> dict:
        """Asserts a real issue came back, not the plugin's own stand-in.

        `create` logs the true error and returns this instead, so a bare
        assertion on the resource id would report a passing-looking string.
        """
        assert ticket is not None, "the plugin returned nothing"
        assert ticket.get("resource_type") != "jira-error-ticket", (
            "the plugin fell back to a synthetic ticket; the real error is in the log"
        )
        self.created.append(ticket["resource_id"])
        return ticket

    def issue(self, key: str):
        return self.client.issue(key)

    def cleanup(self):
        for key in reversed(self.created):
            try:
                self.client.issue(key).delete()
            except Exception as exception:  # noqa: BLE001 - teardown is best effort
                print(f"could not delete {key}: {exception}")


@pytest.fixture(params=["cloud", "server"])
def live(request):
    platform = request.param
    settings = _settings(platform)
    missing = sorted(name for name, value in settings.items() if not value)
    if missing:
        prefix = f"DISPATCH_JIRA_TEST_{platform.upper()}_"
        pytest.skip(f"needs {', '.join(prefix + name.upper() for name in missing)}")

    instance = Live(platform, settings)
    try:
        yield instance
    finally:
        instance.cleanup()


# -- filing tickets ---------------------------------------------------------


def test_creating_an_incident_ticket_files_a_real_issue(live):
    ticket = live.track(
        live.plugin.create(
            incident_id=1,
            title=live.title("incident"),
            commander_email=live.email,
            reporter_email=live.email,
            db_session=None,
        )
    )

    issue = live.issue(ticket["resource_id"])
    assert issue.fields.project.key == live.settings["project"]
    assert issue.fields.issuetype.name == live.settings["issue_type"]


def test_the_incident_ticket_carries_the_title_it_was_given(live):
    title = live.title("incident")

    ticket = live.track(
        live.plugin.create(
            incident_id=1,
            title=title,
            commander_email=live.email,
            reporter_email=live.email,
            db_session=None,
        )
    )

    assert live.issue(ticket["resource_id"]).fields.summary == title


def test_the_commander_is_assigned_the_issue(live):
    """When `groupuserpicker` cannot resolve the address, the plugin assigns
    the account Dispatch authenticates as and says nothing. Here the commander
    is that account, so this asserts resolution happened at all."""
    ticket = live.track(
        live.plugin.create(
            incident_id=1,
            title=live.title("incident"),
            commander_email=live.email,
            reporter_email=live.email,
            db_session=None,
        )
    )

    issue = live.issue(ticket["resource_id"])
    assert issue.fields.assignee is not None
    assert issue.fields.reporter is not None


def test_a_case_ticket_files_a_real_issue(live):
    ticket = live.track(
        live.plugin.create_case_ticket(
            case_id=1,
            title=live.title("case"),
            assignee_email=live.email,
            db_session=None,
        )
    )

    assert live.issue(ticket["resource_id"]).fields.project.key == live.settings["project"]


def test_a_task_ticket_files_a_real_issue(live):
    ticket = live.track(
        live.plugin.create_task_ticket(
            task_id=1,
            title=live.title("task"),
            assignee_email=live.email,
            reporter_email=live.email,
            db_session=None,
        )
    )

    assert live.issue(ticket["resource_id"]).fields.project.key == live.settings["project"]


# -- the weblink Dispatch stores --------------------------------------------


def test_the_weblink_is_a_url_a_browser_can_open(live):
    """It is persisted as the ticket's link and shown to responders, and it is
    built by concatenation onto `browser_url` -- which pydantic gives a
    trailing slash when its path is empty."""
    ticket = live.track(
        live.plugin.create(
            incident_id=1,
            title=live.title("incident"),
            commander_email=live.email,
            reporter_email=live.email,
            db_session=None,
        )
    )

    weblink = ticket["weblink"]
    assert weblink.endswith(f"/browse/{ticket['resource_id']}")
    assert "//browse/" not in weblink, f"doubled slash in {weblink}"
    assert weblink.startswith(str(live.configuration.browser_url).rstrip("/"))


# -- updating and closing ---------------------------------------------------


def test_update_rewrites_the_issue(live):
    ticket = live.track(
        live.plugin.create(
            incident_id=1,
            title=live.title("incident"),
            commander_email=live.email,
            reporter_email=live.email,
            db_session=None,
        )
    )
    new_title = live.title("renamed")

    live.plugin.update(
        ticket_id=ticket["resource_id"],
        title=new_title,
        description="A description Dispatch wrote.",
        incident_type="Denial of Service",
        incident_severity="High",
        incident_priority="High",
        status="",
        commander_email=live.email,
        reporter_email=live.email,
        conversation_weblink="https://example.com/conversation",
        document_weblink="https://example.com/document",
        storage_weblink="https://example.com/storage",
        conference_weblink="https://example.com/conference",
        dispatch_weblink="https://example.com/dispatch",
        cost=0.0,
    )

    issue = live.issue(ticket["resource_id"])
    assert issue.fields.summary == new_title
    assert "A description Dispatch wrote." in issue.fields.description


def test_update_moves_the_issue_when_the_status_names_a_transition(live):
    """`status` is passed straight through as a transition name, so it only
    moves the issue when Dispatch's status happens to match one Jira offers."""
    ticket = live.track(
        live.plugin.create(
            incident_id=1,
            title=live.title("incident"),
            commander_email=live.email,
            reporter_email=live.email,
            db_session=None,
        )
    )
    issue = live.issue(ticket["resource_id"])
    transition = live.client.transitions(issue)[-1]["name"]

    live.plugin.update(
        ticket_id=ticket["resource_id"],
        title=live.title("incident"),
        description="d",
        incident_type="t",
        incident_severity="s",
        incident_priority="p",
        status=transition,
        commander_email=live.email,
        reporter_email=live.email,
        conversation_weblink="",
        document_weblink="",
        storage_weblink="",
        conference_weblink="",
        dispatch_weblink="",
        cost=0.0,
    )

    assert live.issue(ticket["resource_id"]).fields.status.name != issue.fields.status.name


def test_an_unmatched_status_leaves_the_issue_where_it_was(live):
    """Dispatch's own statuses are Active/Stable/Closed, which no default Jira
    workflow offers, so the transition loop finds nothing and moves on."""
    ticket = live.track(
        live.plugin.create(
            incident_id=1,
            title=live.title("incident"),
            commander_email=live.email,
            reporter_email=live.email,
            db_session=None,
        )
    )
    before = live.issue(ticket["resource_id"]).fields.status.name

    live.plugin.update(
        ticket_id=ticket["resource_id"],
        title=live.title("incident"),
        description="d",
        incident_type="t",
        incident_severity="s",
        incident_priority="p",
        status="No Such Transition",
        commander_email=live.email,
        reporter_email=live.email,
        conversation_weblink="",
        document_weblink="",
        storage_weblink="",
        conference_weblink="",
        dispatch_weblink="",
        cost=0.0,
    )

    assert live.issue(ticket["resource_id"]).fields.status.name == before


def test_delete_removes_the_issue(live):
    ticket = live.plugin.create(
        incident_id=1,
        title=live.title("incident"),
        commander_email=live.email,
        reporter_email=live.email,
        db_session=None,
    )
    assert ticket.get("resource_type") != "jira-error-ticket"

    live.plugin.delete(ticket["resource_id"])

    with pytest.raises(JIRAError) as failure:
        live.issue(ticket["resource_id"])
    assert failure.value.status_code == 404

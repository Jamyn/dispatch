"""What the Jira ticket plugin sends, against the in-memory instance.

The plugin returns a synthetic ticket when anything goes wrong, so a test that
only inspects the return value passes against a plugin that never reached Jira.
These assert on the request Jira would have received.
"""

from dispatch.plugins.dispatch_jira.plugin import JiraTicketPlugin
from tests.plugins.dispatch_jira.fake_jira import (
    COMMANDER_ACCOUNT_ID,
    COMMANDER_EMAIL,
    COMMANDER_NAME,
    DISPATCH_ACCOUNT_ID,
    ISSUE_TYPE,
    PROJECT_ID,
    PROJECT_KEY,
    URL,
    USERNAME,
    build_configuration,
)

UNKNOWN_EMAIL = "nobody@example.com"


def configured(hosting_type="cloud", **overrides):
    instance = JiraTicketPlugin()
    instance.configuration = build_configuration(hosting_type, **overrides)
    return instance


def incident_ticket(plugin, **overrides):
    arguments = {
        "incident_id": 1,
        "title": "Dispatch Incident",
        "commander_email": COMMANDER_EMAIL,
        "reporter_email": COMMANDER_EMAIL,
        "db_session": None,
    }
    arguments.update(overrides)
    return plugin.create(**arguments)


def update_arguments(**overrides):
    arguments = {
        "title": "Dispatch Incident",
        "description": "what happened",
        "incident_type": "Denial of Service",
        "incident_severity": "High",
        "incident_priority": "High",
        "status": "",
        "commander_email": COMMANDER_EMAIL,
        "reporter_email": COMMANDER_EMAIL,
        "conversation_weblink": "https://example.com/conversation",
        "document_weblink": "https://example.com/document",
        "storage_weblink": "https://example.com/storage",
        "conference_weblink": "https://example.com/conference",
        "dispatch_weblink": "https://example.com/dispatch",
        "cost": 0.0,
    }
    arguments.update(overrides)
    return arguments


# -- filing an issue --------------------------------------------------------


def test_create_files_an_issue_in_the_configured_project(jira, plugin):
    ticket = incident_ticket(plugin)

    fields = jira.last("POST").body["fields"]
    assert fields["project"] == {"key": PROJECT_KEY}
    assert fields["issuetype"] == {"name": ISSUE_TYPE}
    assert fields["summary"] == "Dispatch Incident"
    assert ticket["resource_id"] in jira.issues


def test_a_numeric_project_is_sent_as_an_id_not_a_key(jira):
    """Jira accepts either, but not the wrong field name for the value."""
    plugin = configured(default_project_id=PROJECT_ID)

    incident_ticket(plugin)

    assert jira.last("POST").body["fields"]["project"] == {"id": PROJECT_ID}


def test_the_title_loses_the_newlines_a_summary_cannot_hold(jira, plugin):
    incident_ticket(plugin, title="Line one\nline two")

    assert jira.last("POST").body["fields"]["summary"] == "Line oneline two"


def test_plugin_metadata_overrides_the_configured_project_and_type(jira, plugin):
    metadata = {
        "metadata": [
            {"key": "project_id", "value": "OPS"},
            {"key": "issue_type_name", "value": "Bug"},
        ]
    }

    incident_ticket(plugin, incident_type_plugin_metadata=metadata)

    fields = jira.last("POST").body["fields"]
    assert fields["project"] == {"key": "OPS"}
    assert fields["issuetype"] == {"name": "Bug"}


def test_the_ticket_carries_the_key_and_a_weblink(jira, plugin):
    ticket = incident_ticket(plugin)

    assert ticket["resource_id"].startswith(f"{PROJECT_KEY}-")
    assert ticket["weblink"] == f"{URL}/browse/{ticket['resource_id']}"


# -- who the issue is assigned to -------------------------------------------


def test_cloud_assigns_the_commander_by_account_id(jira, plugin):
    incident_ticket(plugin)

    fields = jira.last("POST").body["fields"]
    assert fields["assignee"] == {"id": COMMANDER_ACCOUNT_ID}
    assert fields["reporter"] == {"id": COMMANDER_ACCOUNT_ID}


def test_cloud_assigns_the_dispatch_account_when_the_commander_is_unknown(jira, plugin):
    """Atlassian restricts user search, so an address Jira will not resolve is
    the normal case rather than the exceptional one -- and the plugin says
    nothing when it substitutes its own account."""
    incident_ticket(plugin, commander_email=UNKNOWN_EMAIL, reporter_email=UNKNOWN_EMAIL)

    assert jira.last("POST").body["fields"]["assignee"] == {"id": DISPATCH_ACCOUNT_ID}


def test_a_separate_reporter_is_resolved_separately(jira, plugin):
    incident_ticket(plugin, commander_email=COMMANDER_EMAIL, reporter_email=UNKNOWN_EMAIL)

    fields = jira.last("POST").body["fields"]
    assert fields["assignee"] == {"id": COMMANDER_ACCOUNT_ID}
    assert fields["reporter"] == {"id": DISPATCH_ACCOUNT_ID}


def test_server_assigns_by_username_rather_than_account_id(jira):
    """v1 Jira has no account ids; the two deployments need different fields
    for the same person."""
    plugin = configured("server")

    incident_ticket(plugin)

    assert jira.last("POST").body["fields"]["assignee"] == {"name": COMMANDER_NAME}


def test_server_falls_back_to_the_configured_username_verbatim(jira):
    """Note what it falls back to: `configuration.username` as written, which
    is an email address in every other use. Server usernames are not email
    addresses, so this only resolves where the two happen to match."""
    plugin = configured("server")

    incident_ticket(plugin, commander_email=UNKNOWN_EMAIL, reporter_email=UNKNOWN_EMAIL)

    assert jira.last("POST").body["fields"]["assignee"] == {"name": USERNAME}


# -- updating ---------------------------------------------------------------


def test_update_rewrites_the_issue_fields(jira, plugin):
    ticket = incident_ticket(plugin)

    plugin.update(ticket_id=ticket["resource_id"], **update_arguments(title="Renamed"))

    assert jira.issue(ticket["resource_id"]).fields["summary"] == "Renamed"


def test_update_transitions_when_the_status_names_one(jira, plugin):
    ticket = incident_ticket(plugin)

    plugin.update(ticket_id=ticket["resource_id"], **update_arguments(status="Done"))

    assert jira.issue(ticket["resource_id"]).status == "Done"


def test_update_leaves_the_issue_alone_when_the_status_names_nothing(jira, plugin):
    """Dispatch's statuses are Active/Stable/Closed, which no default Jira
    workflow offers, so the transition loop finds nothing."""
    ticket = incident_ticket(plugin)

    plugin.update(ticket_id=ticket["resource_id"], **update_arguments(status="Stable"))

    assert jira.issue(ticket["resource_id"]).status == "To Do"


def test_update_returns_a_link_matching_the_one_create_returned(jira, plugin):
    ticket = incident_ticket(plugin)

    data = plugin.update(ticket_id=ticket["resource_id"], **update_arguments())

    assert data["link"] == ticket["weblink"]


# -- metadata and deletion --------------------------------------------------


def test_update_metadata_refuses_a_project_it_does_not_belong_to(jira, plugin):
    """Guards against rewriting an issue in another project; it logs and
    returns rather than raising."""
    ticket = incident_ticket(plugin)
    before = len(jira.requests)

    plugin.update_metadata(
        ticket_id=ticket["resource_id"],
        metadata={"metadata": [{"key": "project_id", "value": "OTHER"}]},
    )

    assert all(request.method != "PUT" for request in jira.requests[before:])


def test_delete_removes_the_issue(jira, plugin):
    ticket = incident_ticket(plugin)

    plugin.delete(ticket["resource_id"])

    assert ticket["resource_id"] not in jira.issues


# -- cases and tasks --------------------------------------------------------


def test_a_case_ticket_is_filed_in_the_configured_project(jira, plugin):
    ticket = plugin.create_case_ticket(
        case_id=1, title="Dispatch Case", assignee_email=COMMANDER_EMAIL, db_session=None
    )

    assert jira.last("POST").body["fields"]["project"] == {"key": PROJECT_KEY}
    assert ticket["resource_id"] in jira.issues


def test_a_task_ticket_is_filed_in_the_configured_project(jira, plugin):
    ticket = plugin.create_task_ticket(
        task_id=1,
        title="Dispatch Task",
        assignee_email=COMMANDER_EMAIL,
        reporter_email=COMMANDER_EMAIL,
        db_session=None,
    )

    assert jira.last("POST").body["fields"]["project"] == {"key": PROJECT_KEY}
    assert ticket["resource_id"] in jira.issues

"""The URLs the Jira plugin builds by hand.

Everything else about a ticket comes back from the `jira` client, but the
weblink Dispatch persists and shows to responders is concatenated here, onto a
pydantic AnyHttpUrl that carries a trailing slash whenever its path is empty.
The live suite proves Jira accepts the ticket; this proves the link is one a
browser can open, and it runs without an instance.
"""

from types import SimpleNamespace

import pytest

from dispatch.plugins.dispatch_jira.plugin import JiraConfiguration, create, site_url, update


def configuration(browser_url: str) -> JiraConfiguration:
    return JiraConfiguration(
        api_url="https://jira.example.com",
        browser_url=browser_url,
        hosting_type="cloud",
        username="dispatch@example.com",
        password="not-a-real-api-token",
        default_project_id="INC",
        default_issue_type_name="Task",
    )


@pytest.fixture
def issue():
    return SimpleNamespace(key="INC-42", update=lambda **kwargs: None)


def test_the_weblink_is_not_doubled_at_the_root(issue):
    """AnyHttpUrl stores `https://host/` when the path is empty, so plain
    concatenation produced `https://host//browse/INC-42`."""
    client = SimpleNamespace(create_issue=lambda fields: issue)

    ticket = create(configuration("https://jira.example.com"), client, {})

    assert ticket["weblink"] == "https://jira.example.com/browse/INC-42"


def test_the_weblink_keeps_an_instance_under_a_context_path(issue):
    client = SimpleNamespace(create_issue=lambda fields: issue)

    ticket = create(configuration("https://example.com/jira"), client, {})

    assert ticket["weblink"] == "https://example.com/jira/browse/INC-42"


def test_update_builds_the_same_link(issue):
    """`update` returns it under a different key, from its own copy of the
    expression -- so it drifts from `create` unless both are covered."""
    client = SimpleNamespace(transitions=lambda i: [])

    data = update(configuration("https://jira.example.com"), client, issue, {}, None)

    assert data["link"] == "https://jira.example.com/browse/INC-42"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://jira.example.com/", "https://jira.example.com"),
        ("https://jira.example.com", "https://jira.example.com"),
        ("https://example.com/jira/", "https://example.com/jira"),
    ],
)
def test_site_url_trims_only_the_trailing_slash(url, expected):
    assert site_url(url) == expected

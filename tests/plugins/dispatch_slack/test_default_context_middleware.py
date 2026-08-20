"""What a listener gets before anything has identified the request.

`subject_middleware` and `db_middleware` decide which organization a request is
served against, and `configuration_middleware` decides which Slack workspace
configuration it is served with. All three fall back to a default, and a fallback
that fires when it should not is how a tenant's request ends up reading somebody
else's organization (#140).
"""

from unittest.mock import patch

import pytest
from slack_bolt import BoltContext

import dispatch.plugins.dispatch_slack.middleware as slack_middleware
from dispatch.plugins.dispatch_slack.config import SlackConversationConfiguration
from dispatch.plugins.dispatch_slack.middleware import (
    configuration_middleware,
    db_middleware,
    subject_middleware,
)
from dispatch.plugins.dispatch_slack.models import SubjectMetadata
from dispatch.project.models import Project
from tests.factories import PluginFactory, PluginInstanceFactory, ProjectFactory

TENANT_SLUG = "not_the_default_org"


@pytest.fixture
def opened_slugs(session):
    """Record the organization every session is opened for, handing back a real one.

    The slug asked for is the decision these middlewares actually make; the
    suite has only the default organization's schema, so the session itself has
    to stay the real one or the listener cannot run at all.
    """
    slugs: list[str] = []
    real = slack_middleware.refetch_db_session

    def record(slug: str):
        slugs.append(slug)
        return real("default")

    with patch.object(slack_middleware, "refetch_db_session", record):
        yield slugs


@pytest.fixture
def default_project(session):
    """Make exactly one project the default, restoring the rest afterwards.

    `ProjectFactory` leaves `default` False but the suite's other rows do not
    all agree, and `project_service.get_default` raises on more than one.
    """
    was = {p.id: p.default for p in session.query(Project).all()}
    for project in session.query(Project).all():
        project.default = False
    project = ProjectFactory(default=True)
    session.commit()

    yield project

    for existing in session.query(Project).all():
        existing.default = was.get(existing.id, False)
    session.commit()


def test_a_request_with_no_subject_yet_gets_the_default_organization(
    session, single_default_organization
):
    """Given nothing has identified the request, when the subject is set, then it defaults."""
    context = BoltContext({})
    called = []

    subject_middleware(context=context, next=lambda: called.append(1))

    assert called == [1]
    assert context["subject"].organization_slug == "default"


def test_an_organization_already_on_the_request_is_left_alone(session, single_default_organization):
    """Given the request already names a tenant, when the subject is set, then it survives.

    Overwriting it with the default is exactly how a tenant's request gets
    served against the default organization instead of its own.
    """
    subject = SubjectMetadata(organization_slug=TENANT_SLUG)
    context = BoltContext({"subject": subject})

    subject_middleware(context=context, next=lambda: None)

    assert context["subject"] is subject
    assert context["subject"].organization_slug == TENANT_SLUG


def test_the_session_falls_back_to_the_default_organization(
    session, opened_slugs, single_default_organization
):
    """Given no subject yet, when the session is opened, then the default organization is used."""
    context = BoltContext({})

    db_middleware(context=context, next=lambda: None)

    assert opened_slugs == ["default"]
    assert context["subject"].organization_slug == "default"


def test_a_configuration_already_resolved_is_not_looked_up_again(session):
    """Given the request already carries a configuration, when configuring, then it is kept.

    Re-resolving would replace a tenant's configuration with the default
    project's, and it runs on every request.
    """
    config = SlackConversationConfiguration(
        api_bot_token="xoxb-already-resolved-not-real",
        signing_secret="already-resolved-not-real",
        socket_mode_app_token="xapp-already-resolved-not-real",
        app_user_slug="already-resolved-bot",
    )
    context = BoltContext({"config": config})
    called = []

    configuration_middleware(context=context, next=lambda: called.append(1))

    assert called == [1]
    assert context["config"] is config


def test_the_active_conversation_plugin_supplies_the_configuration(session, default_project):
    """Given an enabled conversation plugin, when configuring, then its settings are handed on.

    This is where a listener gets the bot token it answers Slack with, so the
    wrong instance here means answering the wrong workspace.
    """
    instance = PluginInstanceFactory(
        project=default_project,
        plugin=PluginFactory(slug="slack-conversation", type="conversation"),
        enabled=True,
    )
    # `configuration` is a hybrid property that encrypts on assignment, so it
    # cannot be passed to the constructor.
    instance.configuration = SlackConversationConfiguration(
        api_bot_token="xoxb-from-the-plugin-not-real",
        signing_secret="from-the-plugin-not-real",
        socket_mode_app_token="xapp-from-the-plugin-not-real",
        app_user_slug="from-the-plugin-bot",
    )
    session.add(instance)
    session.commit()

    context = BoltContext({"db_session": session})
    called = []

    configuration_middleware(context=context, next=lambda: called.append(1))

    assert called == [1]
    assert context["config"].api_bot_token.get_secret_value() == "xoxb-from-the-plugin-not-real"


def test_configuring_a_request_with_no_session_opens_one(
    session, opened_slugs, default_project, single_default_organization
):
    """Given no session was opened yet, when configuring, then one opens on the default.

    `configuration_middleware` runs ahead of `db_middleware` in some chains and
    still has to reach the plugin table.
    """
    instance = PluginInstanceFactory(
        project=default_project,
        plugin=PluginFactory(slug="slack-conversation", type="conversation"),
        enabled=True,
    )
    instance.configuration = SlackConversationConfiguration(
        api_bot_token="xoxb-no-session-not-real",
        signing_secret="no-session-not-real",
        socket_mode_app_token="xapp-no-session-not-real",
        app_user_slug="no-session-bot",
    )
    session.add(instance)
    session.commit()

    context = BoltContext({})
    called = []

    configuration_middleware(context=context, next=lambda: called.append(1))

    assert called == [1]
    assert opened_slugs == ["default"]
    assert context["config"].api_bot_token.get_secret_value() == "xoxb-no-session-not-real"

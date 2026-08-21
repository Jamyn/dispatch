"""Reading the subject and the form back out of a Slack payload.

Slack hands state back as opaque strings -- a button's `value`, a view's
`private_metadata`, a submitted view's `state.values` -- and these middlewares
are what turn them into the subject a listener acts on and the form data it acts
with. A misparse here does not fail loudly; it acts on the wrong thing, or drops
what the user typed.
"""

import json

import pytest
from slack_bolt import BoltContext

from dispatch.plugins.dispatch_slack.exceptions import ContextError
from dispatch.plugins.dispatch_slack.middleware import (
    action_context_middleware,
    button_context_middleware,
    engagement_button_context_middleware,
    message_context_middleware,
    modal_submit_middleware,
    reaction_context_middleware,
    select_context_middleware,
)
from dispatch.plugins.dispatch_slack.models import FormMetadata, SubjectMetadata


def run(middleware, **kwargs):
    """Invokes a context middleware, returning its context and whether it proceeded."""
    context = kwargs.pop("context", None) or BoltContext({})
    called = []
    middleware(context=context, next=lambda: called.append(1), **kwargs)
    return context, called


def test_a_button_carries_its_subject_as_json(session):
    """Given a button whose value is subject JSON, when reading it, then the subject is restored."""
    value = SubjectMetadata(
        type="case", id="42", organization_slug="acme", project_id="7"
    ).model_dump_json()

    context, called = run(button_context_middleware, payload={"value": value})

    assert called == [1]
    assert context["subject"].type == "case"
    assert context["subject"].id == "42"
    assert context["subject"].organization_slug == "acme"


def test_a_legacy_button_still_names_its_incident(session):
    """Given a button from before subjects were JSON, when reading it, then it still resolves.

    Old messages stay in Slack indefinitely, so their `slug-id` values keep
    arriving long after the format changed.
    """
    context, called = run(button_context_middleware, payload={"value": "acme-42"})

    assert called == [1]
    assert context["subject"].organization_slug == "acme"
    assert context["subject"].id == "42"
    assert context["subject"].type == "Incident"


def test_a_modal_action_restores_the_subject_from_its_view(session):
    """Given a view carrying subject metadata, when acting on it, then that subject is used."""
    body = {
        "view": {
            "private_metadata": json.dumps(
                {"type": "incident", "id": "9", "organization_slug": "acme"}
            )
        }
    }

    context, called = run(action_context_middleware, body=body)

    assert called == [1]
    assert type(context["subject"]) is SubjectMetadata
    assert context["subject"].id == "9"


def test_a_modal_action_keeps_the_form_the_user_had_filled_in(session):
    """Given a view carrying form data, when acting on it, then the form comes back with it.

    Re-rendering a modal reads this back; losing it blanks what the user typed
    every time a select changes.
    """
    body = {
        "view": {
            "private_metadata": json.dumps(
                {
                    "type": "incident",
                    "id": "9",
                    "organization_slug": "acme",
                    "form_data": {"title": "a partly filled title"},
                }
            )
        }
    }

    context, called = run(action_context_middleware, body=body)

    assert called == [1]
    assert type(context["subject"]) is FormMetadata
    assert context["subject"].form_data["title"] == "a partly filled title"


def test_an_engagement_button_carries_the_signal_it_answers(session):
    """Given a signal engagement button, when reading it, then the instance and engagement survive.

    Approving an engagement acts on exactly these two ids; the subject alone
    cannot identify what was approved.
    """
    value = json.dumps(
        {
            "id": "5362",
            "type": "case",
            "organization_slug": "default",
            "project_id": "1",
            "channel_id": "C04KJP0BLUT",
            "signal_instance_id": "21077511-c53c-4d10-a2eb-998a5c972e09",
            "engagement_id": 5,
            "user": "someone-under-test@example.com",
        }
    )

    context, called = run(engagement_button_context_middleware, payload={"value": value})

    assert called == [1]
    assert context["subject"].signal_instance_id == "21077511-c53c-4d10-a2eb-998a5c972e09"
    assert context["subject"].engagement_id == 5


def test_a_selection_names_the_incident_it_was_made_for(session):
    """Given a selected option, when reading it, then the incident it belongs to is the subject."""
    payload = {"selected_option": {"value": "acme-42-something-else"}}

    context, called = run(select_context_middleware, payload=payload)

    assert called == [1]
    assert context["subject"].organization_slug == "acme"
    assert context["subject"].id == "42"


def test_selecting_nothing_leaves_the_subject_alone(session):
    """Given no selection was made, when reading it, then no subject is invented."""
    context, called = run(select_context_middleware, payload={})

    assert called == [1]
    assert "subject" not in context


def test_a_message_outside_any_incident_channel_is_refused(session, incident):
    """Given a channel no conversation claims, when handling a message, then it is refused.

    Proceeding would run the listener with no subject at all.
    """
    context = BoltContext({"channel_id": "C-never-seen"})
    called = []

    with pytest.raises(ContextError):
        message_context_middleware(
            request=type("R", (), {"body": {}, "context": BoltContext({})})(),
            payload={},
            context=context,
            next=lambda: called.append(1),
        )

    assert not called


def test_a_submitted_select_keeps_both_its_label_and_its_value(session):
    """Given a select, when the form is parsed, then the label and value are both kept.

    The value identifies the record and the label is what gets written into
    incident timelines, so dropping either loses information.
    """
    body = _view_state(
        {
            "incident_type": {
                "incident_type_select": {
                    "selected_option": {"text": {"text": "Denial of Service"}, "value": "3"}
                }
            }
        }
    )

    context, called = run(modal_submit_middleware, body=body)

    assert called == [1]
    assert context["form_data"]["incident_type"] == {"name": "Denial of Service", "value": "3"}


def test_a_submitted_multi_select_keeps_every_choice(session):
    """Given several options chosen, when the form is parsed, then all of them are kept."""
    body = _view_state(
        {
            "tags": {
                "tag_multi_select": {
                    "selected_options": [
                        {"text": {"text": "ExampleTag"}, "value": "1"},
                        {"text": {"text": "OtherTag"}, "value": "2"},
                    ]
                }
            }
        }
    )

    context, called = run(modal_submit_middleware, body=body)

    assert called == [1]
    assert context["form_data"]["tags"] == [
        {"name": "ExampleTag", "value": "1"},
        {"name": "OtherTag", "value": "2"},
    ]


def test_an_empty_multi_select_contributes_nothing(session):
    """Given a multi-select left empty, when the form is parsed, then no entry is made for it.

    An empty list and an absent key mean different things to the handlers that
    read this, so an empty select must not look like a choice of nothing.
    """
    body = _view_state({"tags": {"tag_multi_select": {"selected_options": []}}})

    context, called = run(modal_submit_middleware, body=body)

    assert called == [1]
    assert "tags" not in context["form_data"]


def test_a_submitted_date_and_text_come_through_as_typed(session):
    """Given a date picker and a text input, when the form is parsed, then both are kept as-is."""
    body = _view_state(
        {
            "resolved_at": {"date_picker": {"selected_date": "2026-08-19"}},
            "title": {"title_input": {"value": "a typed title"}},
        }
    )

    context, called = run(modal_submit_middleware, body=body)

    assert called == [1]
    assert context["form_data"]["resolved_at"] == "2026-08-19"
    assert context["form_data"]["title"] == "a typed title"


def _view_state(values: dict) -> dict:
    """The shape Slack posts a submitted view's inputs in."""
    return {"view": {"state": {"values": values}}}


def _request(body=None):
    """A Bolt request stub carrying only what `is_bot` reads."""
    return type("R", (), {"body": body or {}, "context": BoltContext({})})()


def test_a_message_in_an_incident_channel_carries_that_incident(session, incident):
    """Given a message in an incident channel, when handling it, then the incident is the subject."""
    context = BoltContext({"channel_id": incident.conversation.channel_id})
    called = []

    message_context_middleware(
        request=_request(),
        payload={},
        context=context,
        next=lambda: called.append(1),
    )

    assert called == [1]
    assert context["subject"].id == str(incident.id)
    assert context["db_session"] is not None


def test_a_message_from_slackbot_is_never_handled(session, incident):
    """Given a message posted by a bot, when handling it, then the listener is not reached.

    Dispatch posts into its own incident channels; treating those as user
    messages is how a bot ends up answering itself.
    """
    context = BoltContext({"channel_id": incident.conversation.channel_id, "ack": lambda: None})
    called = []

    message_context_middleware(
        request=_request({"event": {"user": "USLACKBOT"}}),
        payload={},
        context=context,
        next=lambda: called.append(1),
    )

    assert called == [], "a bot message reached the listener"


def test_a_reaction_carries_the_incident_it_was_added_in(session, incident):
    """Given a reaction in an incident channel, when handling it, then the incident is the subject.

    Reactions drive timeline entries, which are written against this subject.
    """
    context = BoltContext({"channel_id": incident.conversation.channel_id})
    called = []

    reaction_context_middleware(context=context, next=lambda: called.append(1))

    assert called == [1]
    assert context["subject"].id == str(incident.id)

"""Options-load handlers for external selects: project and tag.

Slack asks for an external select's options over the block_suggestion route
rather than reading them out of the view, which is what lets these selects
carry more options than a static select's 100-option limit allows (#86).
Requests arrive at the ``/slack/menu`` endpoint, which verifies the signature
and picks the organization before anything here runs.

Slack will not send them anywhere unless the app's *Options Load URL* is set,
which is a separate field from the Request URL -- see
docs/administration/settings/plugins/configuring-slack.

Both the incident and case flows share one tag select action id, so one
handler here serves both, same as the project select.
"""

import logging
import re

from slack_bolt import Ack, BoltContext
from sqlalchemy.orm import Session

from dispatch.database.service import search_filter_sort_paginate
from dispatch.project import service as project_service

from .bolt import listeners
from .config import MAX_SELECT_OPTIONS
from .fields import DefaultActionIds, project_option
from .middleware import action_context_middleware, db_middleware

log = logging.getLogger(__name__)

# Every project select's action id ends in "project-select" -- the shared
# default plus one per flow. Matching on the suffix rather than listing them
# means a new call site with its own action id is served too; without a
# matching handler its select would open to a permanently empty menu. Bolt
# matches with `search`, so the leading boundary has to be explicit or an
# unrelated "subproject-select" would be served this list as well.
PROJECT_SELECT_ACTION_ID_PATTERN = re.compile(r"(^|-)project-select$")


@listeners.options(
    PROJECT_SELECT_ACTION_ID_PATTERN, middleware=[action_context_middleware, db_middleware]
)
def handle_project_search_action(
    ack: Ack, payload: dict, context: BoltContext, db_session: Session
) -> None:
    """Serves the project type-ahead.

    ``db_middleware`` binds the session to the organization the modal was
    opened in, so this offers exactly what the static select would have -- every
    enabled project in that organization's schema, which is the only place the
    query can reach.
    """
    try:
        projects = project_service.get_all_enabled(
            db_session=db_session,
            query_str=payload.get("value"),
            limit=MAX_SELECT_OPTIONS,
        )
    except Exception:
        # Slack renders whatever comes back; an error here would leave the user
        # staring at a spinner, and the detail belongs in the log, not in a menu.
        # The slug is what tells an operator which tenant is broken.
        log.exception(
            "Unable to load project options for the Slack project select. Organization: %s",
            context["subject"].organization_slug,
        )
        return ack(options=[])

    options = []
    for project in projects:
        option = project_option(project)
        options.append(
            {
                "text": {"type": "plain_text", "text": option["text"]},
                # NOTE: slack doesn't accept int's as values (fails silently)
                "value": option["value"],
            }
        )

    ack(options=options)


@listeners.options(
    DefaultActionIds.tags_multi_select, middleware=[action_context_middleware, db_middleware]
)
def handle_tag_search_action(
    ack: Ack, payload: dict, context: BoltContext, db_session: Session
) -> None:
    """Serves the tag type-ahead.

    Bounded by ``MAX_SELECT_OPTIONS`` like the project select above -- left at
    ``search_filter_sort_paginate``'s default of 5, a project with more than
    five matching tags was unsearchable past the fifth alphabetically, with no
    indication to the user that more existed (#141).
    """
    query_str = payload["value"]

    filter_spec = {
        "and": [
            {
                "model": "Project",
                "op": "==",
                "field": "id",
                "value": int(context["subject"].project_id),
            }
        ]
    }

    if "/" in query_str:
        # first check to make sure there's only one slash
        if query_str.count("/") > 1:
            ack()
            return

        tag_type, query_str = query_str.split("/")
        filter_spec["and"].append(
            {"model": "TagType", "op": "==", "field": "name", "value": tag_type}
        )

    tags = search_filter_sort_paginate(
        db_session=db_session,
        model="Tag",
        query_str=query_str,
        filter_spec=filter_spec,
        items_per_page=MAX_SELECT_OPTIONS,
    )

    options = []
    for t in tags["items"]:
        options.append(
            {
                "text": {"type": "plain_text", "text": f"{t.tag_type.name}/{t.name}"},
                "value": str(t.id),  # NOTE: slack doesn't accept int's as values (fails silently)
            }
        )

    ack(options=options)

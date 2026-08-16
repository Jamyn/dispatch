from http import HTTPStatus
import json

from fastapi import APIRouter, HTTPException, Depends
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse
from slack_sdk.signature import SignatureVerifier
from sqlalchemy import true
from starlette.requests import Request, Headers

from dispatch.database.core import refetch_db_session
from dispatch.plugin.models import Plugin, PluginInstance

from .app import get_app
from .config import SlackConversationConfiguration
from .handler import SlackRequestHandler
from .messaging import get_incident_conversation_command_message

router = APIRouter()


async def get_body(request: Request):
    return await request.body()


async def parse_request(request: Request):
    request_body_form = await request.form()
    try:
        request = json.loads(request_body_form.get("payload"))
    except Exception:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST, detail=[{"msg": "Bad Request"}]
        ) from None
    return request


def is_current_configuration(
    body: bytes, headers: Headers, plugin_instance: PluginInstance
) -> bool:
    """Uses the signing secret to determine which configuration to use."""

    verifier = SignatureVerifier(
        signing_secret=plugin_instance.configuration.signing_secret.get_secret_value()
    )

    return verifier.is_valid_request(body, headers)


def get_request_handler(
    request: Request, body: bytes, organization: str
) -> tuple[SlackRequestHandler, SlackConversationConfiguration]:
    """Creates a slack request handler for use by the api."""
    session = refetch_db_session(organization)
    try:
        plugin_instances: list[PluginInstance] = (
            session.query(PluginInstance)
            .join(Plugin)
            .filter(PluginInstance.enabled == true(), Plugin.slug == "slack-conversation")
            .all()
        )
        for p in plugin_instances:
            if is_current_configuration(body=body, headers=request.headers, plugin_instance=p):
                return SlackRequestHandler(get_app(organization, p)), p.configuration
    finally:
        session.close()

    raise HTTPException(
        status_code=HTTPStatus.FORBIDDEN.value, detail=[{"msg": "Invalid request signature"}]
    )


@router.post(
    "/slack/event",
)
async def slack_events(request: Request, organization: str, body: bytes = Depends(get_body)):
    """Handle all incoming Slack events."""

    handler, _ = get_request_handler(request=request, body=body, organization=organization)
    try:
        body_json = json.loads(body)
        # if we're getting the url verification request,
        # handle it synchronously so that slack api verification works
        if body_json.get("type") == "url_verification":
            return handler.handle(req=request, body=body)
    except json.JSONDecodeError:
        pass

    # otherwise, handle it asynchronously
    task = BackgroundTask(handler.handle, req=request, body=body)
    return JSONResponse(
        background=task,
        content=HTTPStatus.OK.phrase,
        status_code=HTTPStatus.OK,
    )


@router.post(
    "/slack/command",
)
async def slack_commands(organization: str, request: Request, body: bytes = Depends(get_body)):
    """Handle all incoming Slack commands."""
    # We build the background task
    handler, configuration = get_request_handler(
        request=request, body=body, organization=organization
    )
    task = BackgroundTask(
        handler.handle,
        req=request,
        body=body,
    )

    # We get the name of command that was run
    request_body_form = await request.form()
    command = request_body_form._dict.get("command")
    message = get_incident_conversation_command_message(
        config=configuration, command_string=command
    )
    return JSONResponse(
        background=task,
        content=message,
        status_code=HTTPStatus.OK,
    )


@router.post(
    "/slack/action",
)
async def slack_actions(request: Request, organization: str, body: bytes = Depends(get_body)):
    """Handle all incoming Slack actions."""
    handler, _ = get_request_handler(request=request, body=body, organization=organization)
    return handler.handle(req=request, body=body)


@router.post(
    "/slack/menu",
)
def slack_menus(request: Request, organization: str, body: bytes = Depends(get_body)):
    """Handle all incoming Slack menus.

    Deliberately not `async`. `handler.handle` is synchronous all the way down
    and Bolt polls for the listener's ack in a sleep loop, so awaiting it here
    would block the event loop -- for every keystroke, now that the project
    select loads its options through this route.
    """
    handler, _ = get_request_handler(request=request, body=body, organization=organization)
    return handler.handle(req=request, body=body)

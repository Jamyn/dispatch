"""Construction and caching of per-configuration Bolt apps.

One App per Slack configuration, rather than one process-global App
reconfigured per request. The global App had to have its token and
configuration overwritten before every dispatch, and Bolt reads the token when
it builds the request's client -- so any two requests in flight together could
see each other's tenant state.

This lives apart from `endpoints.py` so that both the HTTP routes and the
socket-mode CLI build their apps the same way; it cannot live in `bolt.py`
because the `configure` functions import the registry from there.
"""

import hashlib
import json
import threading
from typing import Callable

from slack_bolt import App, BoltContext
from slack_bolt.listener.listener_completion_handler import CustomListenerCompletionHandler

from dispatch.plugin.models import PluginInstance

from .bolt import listeners
from .case.interactive import configure as case_configure
from .config import SlackConversationConfiguration
from .feedback.interactive import configure as feedback_configure
from .incident.interactive import configure as incident_configure
from .middleware import finalize_listener_db_session_on_success
from .workflow import configure as workflow_configure

# Imported for its registrations alone. Every module holding listeners has to
# be imported before the first app is built, or that app is missing them and
# the registry -- closed by the first build -- rejects the late import. The
# other listener modules arrive via the `configure` imports above; this one has
# no configure function, so nothing else would pull it in, and socket mode
# would come up with no project type-ahead.
from . import options  # noqa: F401,E402


def configuration_digest(configuration: SlackConversationConfiguration) -> str:
    """Fingerprints every tenant-specific value an App is built from.

    Used as the cache generation, so a rotated token or a renamed command
    yields a different digest and the App built from the superseded values is
    replaced rather than reused. Secrets are revealed only to be hashed -- the
    digest is what is retained, never the values.

    Nothing here binds the revealed values to a local that outlives the
    expression: Sentry is initialised without `include_local_variables=False`,
    so it ships frame locals with any exception, and a name holding every
    secret in one plaintext blob would be reported verbatim. The recursion
    below still holds fragments while it runs, which is why it does no work
    that can raise beyond the type checks.
    """

    def reveal(value):
        # Duck-typed rather than `isinstance(value, SecretStr)`: SecretBytes and
        # Secret[str] are not SecretStr subclasses, so a field declared with one
        # of those would survive as a mask and two different secrets would
        # digest alike -- a rotation nobody could see.
        if hasattr(value, "get_secret_value"):
            return value.get_secret_value()
        if isinstance(value, dict):
            return {k: reveal(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [reveal(v) for v in value]
        if isinstance(value, (set, frozenset)):
            # Sorted, because set iteration order is not stable across
            # processes -- an unsorted set would digest differently in each
            # worker and rebuild the App on every request.
            return sorted(reveal(v) for v in value)
        return value

    # `model_dump()` in python mode, deliberately: `mode="json"` would render
    # every secret as its mask before `reveal` could unwrap it, and the digest
    # would then be blind to exactly the rotations it exists to notice.
    return hashlib.sha256(
        json.dumps(reveal(configuration.model_dump()), sort_keys=True, default=str).encode()
    ).hexdigest()


def build_app(configuration: SlackConversationConfiguration) -> App:
    """Builds an App that owns its tenant state instead of borrowing it.

    Token and signing secret are passed to the constructor rather than assigned
    afterwards, so nothing about this App's identity is writable once it
    exists. `request_verification_enabled` stays off because signatures are
    verified before this is reached -- over HTTP that is
    `endpoints.is_current_configuration`, which is also how the organization is
    identified, so it cannot be deferred to Bolt.
    """
    app = App(
        token=configuration.api_bot_token.get_secret_value(),
        signing_secret=configuration.signing_secret.get_secret_value(),
        request_verification_enabled=False,
        token_verification_enabled=False,
    )

    # The configuration this App was verified against, put on the request
    # before any listener middleware runs; `middleware.configuration_middleware`
    # would otherwise re-derive one from the *default* organization's default
    # project and hand organization B a full configuration -- bot token and
    # signing secret included -- belonging to organization A. Global middleware
    # runs after `handler.to_bolt_request`, so a caller cannot pre-empt this
    # with `addition_context_properties["config"]`.
    @app.use
    def seed_configuration(context: BoltContext, next: Callable) -> None:
        context["config"] = configuration
        next()

    listeners.apply(app)
    # Only these depend on the configuration; they name their commands after
    # it. Running them once per App rather than once per request is also what
    # stops the listener list growing without bound.
    case_configure(app, configuration)
    feedback_configure(app, configuration)
    incident_configure(app, configuration)
    workflow_configure(app, configuration)
    # Closes any `context["db_session"]` a listener middleware (e.g.
    # `db_middleware`) opened, once Bolt reports the listener itself actually
    # finished -- see `middleware.finalize_listener_db_session_on_success` for
    # why this can't be a plain `with` block in the middleware. The failure
    # counterpart lives in `bolt.app_error_handler`, since Bolt routes a raised
    # listener *or* listener-middleware exception through the error handler,
    # not this completion hook.
    app.listener_runner.listener_completion_handler = CustomListenerCompletionHandler(
        logger=app.logger, func=finalize_listener_db_session_on_success
    )
    return app


# One App per plugin instance, replaced when that instance's configuration
# changes. Bounded by the number of Slack plugin instances, and per process --
# each worker builds its own, which is correct because an App holds no state
# that has to be shared between them.
#
# The organization is part of the key because `plugin_instance` is a
# per-organization table -- every tenant schema has its own id sequence, so ids
# repeat across organizations and alone would not identify an instance.
_apps: dict[tuple[str, int], tuple[str, App]] = {}
_apps_lock = threading.Lock()


def get_app(organization: str, plugin_instance: PluginInstance) -> App:
    """Returns the App for an already-validated configuration."""
    key = (organization, plugin_instance.id)
    digest = configuration_digest(plugin_instance.configuration)

    with _apps_lock:
        cached = _apps.get(key)
        if cached is not None and cached[0] == digest:
            return cached[1]

    # Built outside the lock: registering ~92 listeners is pure CPU, but
    # holding the lock across it would serialize every tenant behind whichever
    # one happens to be cold.
    app = build_app(plugin_instance.configuration)

    with _apps_lock:
        cached = _apps.get(key)
        if cached is not None and cached[0] == digest:
            # Another thread built the same App first; discard this one so all
            # callers share a single instance per configuration.
            return cached[1]
        _apps[key] = (digest, app)
        return app

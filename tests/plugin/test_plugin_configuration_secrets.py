"""A rejected plugin configuration must not republish what was submitted.

Plugin configuration is stored as an encrypted blob and validated with pydantic.
A pydantic ``missing`` error embeds the *entire* submitted input as
``input_value=``, and that exception travels two ways that both reach a reader:

- ``PluginInstance.configuration``'s getter logs it on every read, which every
  deployment hits after a schema change until the plugin is reconfigured;
- the setter's error reaches ``ExceptionMiddleware``, which logs it and, for a
  bare ``ValidationError``, returns ``e.errors()`` -- input included -- in the
  API response body.

Both are exercised here against a real plugin schema carrying a ``SecretStr``.

Assertions on the secret compare a precomputed bool: a failing
``assert secret not in text`` renders both operands into the pytest report,
which would publish the value the test exists to protect.
"""

import pytest
from pydantic import ValidationError

from dispatch.exceptions import InvalidConfigurationError
from dispatch.plugin.models import redacted_error

# Short on purpose: pydantic elides the middle of a long input repr, so a long
# secret can be hidden by accident. That truncation is incidental, not a control.
SECRET = "hunter2"

STALE_ZOOM_CONFIG = {
    "api_user_id": "ops@example.com",
    "api_key": "an-old-jwt-key",
    "api_secret": SECRET,
}


def _zoom_validation_error() -> ValidationError:
    from dispatch.plugins.dispatch_zoom.config import ZoomConfiguration

    with pytest.raises(ValidationError) as excinfo:
        ZoomConfiguration.model_validate(STALE_ZOOM_CONFIG)
    return excinfo.value


def test_pydantic_really_does_embed_the_submitted_input():
    """Precondition. If this ever fails, the redaction below is moot."""
    embedded = SECRET in str(_zoom_validation_error())

    assert embedded, "pydantic stopped embedding input_value; revisit redacted_error"


def test_the_redacted_form_drops_every_submitted_value():
    redacted = redacted_error(_zoom_validation_error())

    leaked = SECRET in redacted
    assert not leaked, "the redacted error still carries the submitted secret"

    key_leaked = "an-old-jwt-key" in redacted
    assert not key_leaked, "the redacted error still carries the submitted key"


def test_the_redacted_form_still_names_the_failing_fields():
    """Redaction must not cost the operator the diagnosis."""
    redacted = redacted_error(_zoom_validation_error())

    assert "ValidationError" in redacted
    for field in ("account_id", "client_id", "client_secret"):
        assert field in redacted


def test_a_non_pydantic_exception_is_reduced_to_its_type():
    reduced = redacted_error(ValueError(f"boom carrying {SECRET}"))

    leaked = SECRET in reduced
    assert not leaked
    assert reduced == "ValueError"


def test_redacted_error_never_raises_on_an_odd_exception():
    """It runs inside an exception handler; raising there would mask the cause."""

    class Awkward(Exception):
        def errors(self):
            raise RuntimeError("no errors for you")

    assert redacted_error(Awkward()) == "Awkward"

    class NotCallable(Exception):
        errors = "not a method"

    assert redacted_error(NotCallable()) == "NotCallable"


def test_setting_an_invalid_configuration_raises_without_the_values(session):
    """The setter's exception reaches the API response body, so it must be clean.

    ``InvalidConfigurationError`` is a ``ValueError``, which the application's
    ExceptionMiddleware answers with a generic 422 -- unlike a bare
    ``ValidationError``, whose handler returns ``e.errors()`` verbatim.
    """
    from dispatch.plugin.models import Plugin, PluginInstance

    plugin = Plugin(
        title="Zoom Plugin - Conference Management",
        slug="zoom-conference",
        type="conference",
        description="test",
        version="0.1.0",
    )
    instance = PluginInstance(plugin=plugin)

    with pytest.raises(InvalidConfigurationError) as excinfo:
        instance.configuration = STALE_ZOOM_CONFIG

    message = str(excinfo.value)
    leaked = SECRET in message
    assert not leaked, "the rejected configuration was echoed back"
    assert "client_secret" in message, "the operator still needs the failing fields"

"""The Zoom plugin's Server-to-Server OAuth configuration (issue #70).

Plugin configuration is stored per instance as encrypted JSON and read back
through `configuration_schema.parse_raw`, whose caller swallows a parse failure
and returns None with only a warning. A schema that accepts the wrong shape
therefore fails silently at runtime rather than at configuration time, which is
why the required fields are pinned here.
"""

import pytest
from pydantic import SecretStr, ValidationError

from tests.plugins.dispatch_zoom.conftest import ACCOUNT_ID, CLIENT_ID, CLIENT_SECRET, API_USER_ID


def build(**overrides):
    from dispatch.plugins.dispatch_zoom.config import ZoomConfiguration

    kwargs = {
        "api_user_id": API_USER_ID,
        "account_id": ACCOUNT_ID,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    kwargs.update(overrides)
    return ZoomConfiguration(**{k: v for k, v in kwargs.items() if v is not _OMIT})


_OMIT = object()


def test_a_complete_configuration_is_accepted():
    assert build()


@pytest.mark.parametrize("field", ["account_id", "client_id", "client_secret"])
def test_every_oauth_credential_is_required(field):
    with pytest.raises(ValidationError):
        build(**{field: _OMIT})


def test_the_api_user_id_is_still_required():
    """Meetings are still created under a specific user."""
    with pytest.raises(ValidationError):
        build(api_user_id=_OMIT)


def test_the_client_secret_is_a_secret():
    """A plain str would render in logs, API responses and the plugin UI."""
    configuration = build()

    assert isinstance(configuration.client_secret, SecretStr)
    assert configuration.client_secret.get_secret_value() == CLIENT_SECRET


def test_the_client_secret_does_not_appear_in_the_repr():
    assert CLIENT_SECRET not in repr(build())


def test_the_client_secret_does_not_appear_in_the_model_dump():
    """`model_dump` feeds the plugin UI; only the encoder may reveal it."""
    assert CLIENT_SECRET not in str(build().model_dump())


def test_the_retired_jwt_credentials_are_gone():
    """Regression guard for issue #70.

    Leaving `api_key`/`api_secret` on the schema would let a deployment keep
    configuring credentials that cannot authenticate against Zoom.
    """
    from dispatch.plugins.dispatch_zoom.config import ZoomConfiguration

    assert "api_key" not in ZoomConfiguration.model_fields
    assert "api_secret" not in ZoomConfiguration.model_fields


def test_a_stale_jwt_configuration_is_rejected():
    """An instance still holding the old credentials must not parse as valid.

    Note *why* it is rejected: the model inherits pydantic's default
    `extra="ignore"`, so `api_key`/`api_secret` are dropped silently and the
    error comes entirely from the three OAuth fields being absent. The old keys
    are not themselves detected -- if the new fields ever gained defaults, this
    stale shape would validate and the plugin would come up credential-less.
    That is what this pins.
    """
    from dispatch.plugins.dispatch_zoom.config import ZoomConfiguration

    with pytest.raises(ValidationError) as excinfo:
        ZoomConfiguration.model_validate(
            {
                "api_user_id": API_USER_ID,
                "api_key": "an-old-key",
                "api_secret": "an-old-secret",
            }
        )

    missing = {".".join(str(p) for p in e["loc"]) for e in excinfo.value.errors()}
    assert missing == {"account_id", "client_id", "client_secret"}


def test_a_stale_configuration_error_does_not_carry_the_old_secret():
    """`PluginInstance.configuration` logs this exception on every read.

    Pydantic renders `input_value=` into `str(e)` and keeps the tail of the
    repr, so an old `api_secret` stored last survives truncation. The plugin
    model redacts it; this pins the property the redaction depends on.
    """
    from dispatch.plugin.models import redacted_error
    from dispatch.plugins.dispatch_zoom.config import ZoomConfiguration

    # Short on purpose. Pydantic elides the middle of a long input repr, which
    # would hide a long secret by accident -- that truncation is incidental, not
    # a control, and a short secret is exactly the case it does not cover.
    secret = "hunter2"

    with pytest.raises(ValidationError) as excinfo:
        ZoomConfiguration.model_validate(
            {"api_user_id": API_USER_ID, "api_key": "an-old-key", "api_secret": secret}
        )

    # Precomputed bools: neither assertion may print the value on failure.
    embedded = secret in str(excinfo.value)
    assert embedded, "pydantic stopped embedding the input; the redaction may be moot"

    leaked = secret in redacted_error(excinfo.value)
    assert not leaked, "the redacted form still carries the stored secret"


def test_the_default_duration_is_unchanged():
    """Not part of this migration; a silent change would alter every bridge."""
    assert build().default_duration_minutes == 1440

"""Configuration contract for the Microsoft Teams conference plugin."""

import pytest
from pydantic import SecretStr, ValidationError

from dispatch.plugins.dispatch_microsoft_teams.conference.config import (
    MicrosoftTeamsConfiguration,
)

from tests.plugins.dispatch_microsoft_teams.graph_fake import (
    AUTHORITY,
    CLIENT_ID,
    SECRET,
    USER_ID,
)

REQUIRED = {
    "authority": AUTHORITY,
    "client_id": CLIENT_ID,
    "secret": SECRET,
    "user_id": USER_ID,
}


def test_the_required_fields_are_enough():
    configuration = MicrosoftTeamsConfiguration(**REQUIRED)

    assert configuration.client_id == CLIENT_ID
    assert configuration.user_id == USER_ID


@pytest.mark.parametrize("missing", sorted(REQUIRED))
def test_every_required_field_is_required(missing):
    """A half-configured plugin must fail at configuration time, not at 3am."""
    incomplete = {k: v for k, v in REQUIRED.items() if k != missing}

    with pytest.raises(ValidationError) as excinfo:
        MicrosoftTeamsConfiguration(**incomplete)

    assert missing in str(excinfo.value)


def test_the_defaults_match_the_zoom_plugin():
    configuration = MicrosoftTeamsConfiguration(**REQUIRED)

    assert configuration.default_duration_minutes == 1440
    assert configuration.allow_auto_recording is False
    assert configuration.require_passcode is True


def test_the_secret_is_held_as_a_secret():
    configuration = MicrosoftTeamsConfiguration(**REQUIRED)

    assert isinstance(configuration.secret, SecretStr)
    assert configuration.secret.get_secret_value() == SECRET


def test_the_secret_does_not_appear_in_the_repr():
    """The configuration is logged and rendered in the admin UI."""
    configuration = MicrosoftTeamsConfiguration(**REQUIRED)

    assert SECRET not in repr(configuration)
    assert SECRET not in str(configuration)
    assert SECRET not in str(configuration.model_dump())


def test_a_non_numeric_duration_is_rejected():
    with pytest.raises(ValidationError):
        MicrosoftTeamsConfiguration(**REQUIRED, default_duration_minutes="a while")


@pytest.mark.parametrize("duration", [0, -60])
def test_a_non_positive_duration_is_rejected(duration):
    """Otherwise endDateTime lands at or before startDateTime and Graph 400s."""
    with pytest.raises(ValidationError):
        MicrosoftTeamsConfiguration(**REQUIRED, default_duration_minutes=duration)


def test_the_secret_is_masked_in_the_json_the_admin_ui_stores():
    """`model_dump_json` is the path plugin configuration is persisted through."""
    configuration = MicrosoftTeamsConfiguration(**REQUIRED)

    assert SECRET not in configuration.model_dump_json()


def test_the_zoom_plugin_declares_the_same_shared_options():
    """Parity check on the configuration surface, not just on behaviour."""
    from dispatch.plugins.dispatch_zoom.config import ZoomConfiguration

    shared = {"default_duration_minutes"}
    assert shared <= set(ZoomConfiguration.model_fields)
    assert shared <= set(MicrosoftTeamsConfiguration.model_fields)

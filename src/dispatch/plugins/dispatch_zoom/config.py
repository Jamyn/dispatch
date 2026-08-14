from pydantic import Field, SecretStr

from dispatch.config import BaseConfigurationModel


class ZoomConfiguration(BaseConfigurationModel):
    """Zoom configuration description."""

    api_user_id: str = Field(
        title="Zoom API User Id",
        description="Email or user ID that meetings are created on behalf of.",
    )
    account_id: str = Field(
        title="Account ID",
        description=(
            "Account ID of the Server-to-Server OAuth app, from its App Credentials page."
        ),
    )
    client_id: str = Field(
        title="Client ID",
        description="Client ID of the Server-to-Server OAuth app.",
    )
    client_secret: SecretStr = Field(
        title="Client Secret",
        description=(
            "Client secret of the Server-to-Server OAuth app. Treat this as a credential: "
            "anyone holding it can act on the account through the app's scopes."
        ),
    )
    # Zoom rejects a duration above 1440. Bounded here so a too-large value is
    # refused at configuration time rather than on every meeting creation.
    default_duration_minutes: int = Field(
        default=1440,  # 1 day, which is also Zoom's maximum
        ge=1,
        le=1440,
        title="Default Meeting Duration (Minutes)",
        description="Default duration in minutes for conference meetings. Defaults to 1440 minutes (1 day), which is also Zoom's maximum.",
    )

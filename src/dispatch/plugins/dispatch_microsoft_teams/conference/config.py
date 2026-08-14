from pydantic import Field, SecretStr

from dispatch.config import BaseConfigurationModel


class MicrosoftTeamsConfiguration(BaseConfigurationModel):
    """MS teams configuration details."""

    authority: str = Field(
        title="MS team Authority URL",
        description="Following format https://login.microsoftonline.com/Enter_the_Tenant_Id_Here.",
    )
    client_id: str = Field(
        title="client id",
        description="It is the Application (client) ID for the application you registered.",
    )
    secret: SecretStr = Field(
        title="Azure Client Secret", description="This is the client secret created via Azure AD."
    )
    allow_auto_recording: bool = Field(
        False,
        title="Allow Auto Recording",
        description="Enable if you would like to record the meetings by default.",
    )
    user_id: str = Field(
        title="User id",
        description=(
            "Object ID of the user the application creates meetings on behalf of. "
            "A tenant application access policy must grant the application access to this user."
        ),
    )
    default_duration_minutes: int = Field(
        default=1440,  # 1 day
        ge=1,
        title="Default Meeting Duration (Minutes)",
        description=(
            "Default duration in minutes for conference meetings. Defaults to 1440 minutes "
            "(1 day). The join link stays usable until the meeting expires, 60 days after it ends."
        ),
    )
    require_passcode: bool = Field(
        default=True,
        title="Require a Meeting Passcode",
        description=(
            "Require a passcode when joining by meeting ID. Dispatch shows the generated "
            "passcode alongside the conference link. Disable to leave the meeting without one."
        ),
    )

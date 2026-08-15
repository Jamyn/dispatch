from pydantic import Field, SecretStr

from dispatch.config import BaseConfigurationModel


class AnthropicConfiguration(BaseConfigurationModel):
    """Anthropic (Claude) configuration description."""

    api_key: SecretStr = Field(title="API Key", description="Your secret Anthropic API key.")
    # `model`, not `chat_completion_model`: `dispatch.ai.service.get_genai_model`
    # reads `configuration.model`, and the ArtificialIntelligencePlugin base
    # class names it as part of the contract.
    model: str = Field(
        "claude-opus-5",
        title="Model",
        description=(
            "Available models can be found at "
            "https://docs.claude.com/en/docs/about-claude/models. Defaults to the most "
            "capable model; a smaller one (for example claude-haiku-4-5) costs less per "
            "request."
        ),
    )
    system_message: str = Field(
        "You are a helpful assistant.",
        title="System Message",
        description="The system message to help set the behavior of the assistant.",
    )
    # Anthropic requires `max_tokens` on every request -- unlike OpenAI, where it
    # is optional -- so this is a required API input rather than a knob added for
    # parity. It caps the reply, and billing is on tokens actually generated, so
    # a generous ceiling costs nothing and a tight one truncates: a truncated
    # structured reply is invalid JSON and fails validation outright.
    # The ceiling is the anthropic SDK's, not the API's. For a non-streaming
    # request the SDK estimates `3600 * max_tokens / 128000` seconds and raises a
    # bare `ValueError` -- not an `AnthropicError` -- above ten minutes, so
    # anything over 21333 fails before a request is ever sent. Bounding the field
    # keeps that out of the settings form; the plugin also catches it, because
    # the legacy `claude-opus-4-0`/`4-1` carry a lower cap of their own (8192).
    max_tokens: int = Field(
        8192,
        ge=1,
        le=21333,
        title="Maximum Output Tokens",
        description=(
            "The maximum number of tokens Claude may generate in a single reply, including "
            "any reasoning on models that reason by default. Replies that hit this limit are "
            "rejected rather than stored half-written. The default is the largest value every "
            "current and legacy Claude model accepts."
        ),
    )

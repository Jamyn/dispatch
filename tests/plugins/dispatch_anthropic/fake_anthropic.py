"""An in-memory Anthropic API, for driving the real plugin against (issue #79).

The fake is installed at the httpx2 transport, below the anthropic SDK, so the
plugin's own client construction and the SDK's own request building, structured
output schema generation and response parsing all run for real against it.
Replacing ``client.messages`` with a mock instead would assert only that the
plugin called something -- and issue #75, the defect this plugin exists not to
repeat, is precisely a case where every mocked assertion passed while no real
request could ever be built.

httpx2, not httpx: anthropic 1.x builds its requests on httpx2 and rejects an
httpx client from its constructor outright, so a fake mounted on httpx would not
even construct.

Only the plugin's ``Anthropic`` name is patched, and only to hand the real
client a transport. Everything the plugin passes to that constructor -- the API
key above all -- still travels the real code path and lands in a request this
fixture can assert on.
"""

import json

import httpx2
from anthropic import Anthropic  # noqa: F401  (re-exported for the fixtures)

# Obviously fake, and never a real-looking credential.
API_KEY = "sk-ant-not-a-real-key-tests-only"

# Deliberately not the schema default ("claude-opus-5"): a plugin that ignored
# the stored configuration and fell back to the default would still pass a test
# written against the default.
CONFIGURED_MODEL = "claude-not-a-real-model-20260101"

# Likewise distinguishable from the schema defaults.
CONFIGURED_SYSTEM_MESSAGE = "You are the configured default assistant."
CONFIGURED_MAX_TOKENS = 4321


def build_configuration(**overrides):
    """An AnthropicConfiguration as the plugin really receives it, secret and all."""
    from dispatch.plugins.dispatch_anthropic.config import AnthropicConfiguration

    values = {
        "api_key": API_KEY,
        "model": CONFIGURED_MODEL,
        "system_message": CONFIGURED_SYSTEM_MESSAGE,
        "max_tokens": CONFIGURED_MAX_TOKENS,
    }
    values.update(overrides)
    return AnthropicConfiguration(**values)


class FakeAnthropicAPI:
    """Records what reached the wire and decides what comes back.

    ``requests`` holds one entry per HTTP request the SDK actually issued, so a
    test asserts on the request Anthropic would have received rather than on the
    arguments of an intercepted method call.
    """

    def __init__(self):
        self.requests = []
        self._handler = None
        self.respond_with_text("A default assistant reply.")

    # -- what comes back ---------------------------------------------------

    def respond_with_text(self, content: str):
        """An ordinary assistant reply carrying ``content`` in one text block."""
        self.respond_with_blocks([{"type": "text", "text": content}])

    def respond_with_blocks(self, blocks: list[dict], stop_reason: str = "end_turn"):
        """An arbitrary content-block list -- several text blocks, a thinking
        block, a tool_use block, or none at all."""
        self._handler = lambda request: httpx2.Response(
            200, json=self._message(request, blocks, stop_reason=stop_reason)
        )

    def respond_with_object(self, obj):
        """A structured reply. ``obj`` is serialized as the text block, which is
        how Anthropic returns json_schema output and what the SDK parses back."""
        self.respond_with_text(json.dumps(obj))

    def respond_with_refusal(self, category: str = "cyber"):
        """The model declined. Anthropic sends stop_reason ``refusal`` plus a
        ``stop_details`` object naming the policy category; ``content`` is
        empty, and ``messages.parse`` reports ``parsed_output`` as None."""
        self._handler = lambda request: httpx2.Response(
            200,
            json=self._message(
                request,
                [],
                stop_reason="refusal",
                stop_details={
                    "type": "refusal",
                    "category": category,
                    "explanation": "declined by a safety classifier",
                },
            ),
        )

    def respond_with_truncation(self, content: str):
        """A reply cut off at ``max_tokens``: partial content, stop_reason
        ``max_tokens``. Anthropic returns this as a 200, not an error."""
        self.respond_with_blocks([{"type": "text", "text": content}], stop_reason="max_tokens")

    def respond_with_status(
        self, status: int, message: str = "boom", error_type: str = "api_error"
    ):
        """An API-level failure, in Anthropic's error envelope.

        ``message`` defaults to something harmless, but Anthropic's real 401
        body quotes the submitted API key back inside it -- see
        ``respond_with_key_echo``, which is the shape that matters.
        """
        self._handler = lambda request: httpx2.Response(
            status,
            json={"type": "error", "error": {"type": error_type, "message": message}},
            headers={"request-id": "req_test_0001"},
        )

    def respond_with_undocumented_stop_reason(self, stop_reason: str):
        """A `stop_reason` outside Anthropic's documented set. The SDK
        constructs its response models rather than validating them, so a
        `Literal`-typed field accepts this."""
        self.respond_with_blocks([{"type": "text", "text": "hi"}], stop_reason=stop_reason)

    def respond_with_undocumented_refusal_category(self, category: str):
        """Likewise for the refusal category."""
        self.respond_with_refusal(category=category)

    def respond_with_non_envelope(self, status: int = 502):
        """A body that is not Anthropic's error envelope at all -- what a proxy
        or gateway in front of the API returns. `APIStatusError.type` is None."""
        self._handler = lambda request: httpx2.Response(
            status, text="<html><body>502 Bad Gateway</body></html>"
        )

    def respond_with_connection_error(self):
        """The request never reaches a server. The SDK raises
        `APIConnectionError`, which is an `AnthropicError` but not an
        `APIStatusError`, so it has no status, type or request id."""

        def raise_connect_error(request):
            raise httpx2.ConnectError("connection refused", request=request)

        self._handler = raise_connect_error

    def respond_with_key_echo(self):
        """Anthropic's real 401: the submitted API key, quoted back in the body.

        Verified against anthropic 0.122.0 -- ``APIStatusError`` renders the
        body verbatim in ``str(e)`` (``_exceptions.py`` builds
        ``f"Error code: {status} - {body}"``), so anything that interpolates the
        exception publishes the key.
        """
        self.respond_with_status(
            401,
            f"invalid x-api-key: {API_KEY}",
            error_type="authentication_error",
        )

    # -- what was sent -----------------------------------------------------

    @property
    def request(self):
        """The single request issued, asserting there was exactly one."""
        assert len(self.requests) == 1, f"expected exactly one request, got {len(self.requests)}"
        return self.requests[0]

    # -- plumbing ----------------------------------------------------------

    def _message(self, request, blocks, stop_reason="end_turn", stop_details=None):
        body = json.loads(request.content)
        return {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": body["model"],
            "content": blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "stop_details": stop_details,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(
            {
                "url": str(request.url),
                "api_key": request.headers.get("x-api-key"),
                "body": json.loads(request.content),
            }
        )
        return self._handler(request)

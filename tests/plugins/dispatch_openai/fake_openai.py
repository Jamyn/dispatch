"""An in-memory OpenAI API, for driving the real plugin against (issue #75).

The fake is installed at the httpx2 transport, below the openai SDK, so the
plugin's own client construction and the SDK's own request building, structured
output schema generation and response parsing all run for real against it.
Replacing ``client.chat.completions`` with a mock instead would assert only that
the plugin called something -- and issue #75 is precisely a case where every
mocked assertion passed while no real request could ever be built.

httpx2, not httpx: openai 3.x builds its requests on httpx2 and only accepts an
httpx client through a legacy escape hatch the SDK documents as temporary. A
fake mounted on that hatch would exercise a stack production no longer uses.
The anthropic fake is on httpx2 too, since anthropic 1.x.

Only the plugin's ``OpenAI`` name is patched, and only to hand the real client a
transport. Everything the plugin passes to that constructor -- the API key above
all -- still travels the real code path and lands in a request this fixture can
assert on.
"""

import json

import httpx2
from openai import OpenAI  # noqa: F401  (re-exported for the fixtures)

# Obviously fake, and never a real-looking credential.
API_KEY = "sk-not-a-real-key-tests-only"

# Deliberately not the schema default ("gpt-4o"): a plugin that ignored the
# stored configuration and fell back to the default would still pass a test
# written against the default.
CONFIGURED_MODEL = "gpt-4.1-mini-not-a-real-model"

# Likewise distinguishable from the schema default.
CONFIGURED_SYSTEM_MESSAGE = "You are the configured default assistant."


def build_configuration(**overrides):
    """An OpenAIConfiguration as the plugin really receives it, secret and all."""
    from dispatch.plugins.dispatch_openai.config import OpenAIConfiguration

    values = {
        "api_key": API_KEY,
        "model": CONFIGURED_MODEL,
        "system_message": CONFIGURED_SYSTEM_MESSAGE,
    }
    values.update(overrides)
    return OpenAIConfiguration(**values)


class FakeOpenAIAPI:
    """Records what reached the wire and decides what comes back.

    ``requests`` holds one entry per HTTP request the SDK actually issued, so a
    test asserts on the request OpenAI would have received rather than on the
    arguments of an intercepted method call.
    """

    def __init__(self):
        self.requests = []
        self._handler = None
        self.respond_with_text("A default assistant reply.")

    # -- what comes back ---------------------------------------------------

    def respond_with_text(self, content: str):
        """An ordinary assistant reply carrying ``content``."""
        self._handler = lambda request: httpx2.Response(
            200, json=self._completion(request, content=content)
        )

    def respond_with_object(self, obj):
        """A structured reply. ``obj`` is serialized as the message content, which
        is how OpenAI returns json_schema output and what the SDK parses back."""
        self._handler = lambda request: httpx2.Response(
            200, json=self._completion(request, content=json.dumps(obj))
        )

    def respond_with_refusal(self, refusal: str):
        """The model declined. OpenAI sends null content plus a refusal string."""
        self._handler = lambda request: httpx2.Response(
            200, json=self._completion(request, content=None, refusal=refusal)
        )

    def respond_with_malformed_content(self, content: str):
        """Content that is not the JSON the requested schema promised."""
        self._handler = lambda request: httpx2.Response(
            200, json=self._completion(request, content=content)
        )

    def respond_with_truncation(self, content: str):
        """A reply cut off at the token limit: partial content, finish_reason
        ``length``. ``chat.completions.create`` reports this and returns the
        fragment; only ``parse`` raises for it."""
        self._handler = lambda request: httpx2.Response(
            200, json=self._completion(request, content=content, finish_reason="length")
        )

    def respond_with_status(self, status: int, message: str = "boom", code: str | None = None):
        """An API-level failure, in OpenAI's error envelope.

        ``message`` defaults to something harmless, but OpenAI's real 401 body
        quotes the submitted API key back inside it -- see
        ``respond_with_key_echo``, which is the shape that matters.
        """
        self._handler = lambda request: httpx2.Response(
            status,
            json={"error": {"message": message, "type": "invalid_request_error", "code": code}},
        )

    def respond_with_key_echo(self):
        """OpenAI's real 401: the submitted API key, quoted back in the body.

        Verified against openai 3.1.0 -- ``APIStatusError`` renders the body
        verbatim in ``str(e)`` (``_base_client.py``: ``f"Error code: {status} -
        {body}"``), so anything that interpolates the exception publishes the
        key.
        """
        self.respond_with_status(
            401,
            f"Incorrect API key provided: {API_KEY}. You can find your API key at "
            "https://platform.openai.com/account/api-keys.",
            code="invalid_api_key",
        )

    # -- what was sent -----------------------------------------------------

    @property
    def request(self):
        """The single request issued, asserting there was exactly one."""
        assert len(self.requests) == 1, f"expected exactly one request, got {len(self.requests)}"
        return self.requests[0]

    # -- plumbing ----------------------------------------------------------

    def _completion(self, request, content, refusal=None, finish_reason="stop"):
        body = json.loads(request.content)
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": body["model"],
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": content, "refusal": refusal},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(
            {
                "url": str(request.url),
                "authorization": request.headers.get("authorization"),
                "body": json.loads(request.content),
            }
        )
        return self._handler(request)

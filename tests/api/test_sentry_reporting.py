"""Sentry reporting is wired through sentry-sdk's own middleware and scope API.

sentry-asgi (unreleased since 2019) and sentry_sdk.configure_scope() both drove
the Hub API that sentry-sdk documents for removal, so the deprecation test below
is what keeps either from coming back (#76).
"""

import warnings
from types import SimpleNamespace

import pytest
import sentry_sdk
from fastapi.testclient import TestClient
from sentry_sdk.integrations.asgi import SentryAsgiMiddleware
from sentry_sdk.transport import Transport

import dispatch.extensions
from dispatch.main import api, app


class SentryProbeError(Exception):
    """Raised by the probe route so the captured event is unmistakable."""


class _CapturingTransport(Transport):
    """Collects events instead of posting them, so no DSN is ever contacted."""

    def __init__(self):
        super().__init__()
        self.events = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.type == "event":
                self.events.append(item.payload.json)


@pytest.fixture
def sentry_probe(monkeypatch):
    """configure_extensions() against a capturing transport, callable on demand.

    Integrations are stripped: excepthook, atexit and stdlib patch global state
    that would outlive the test, and none of them are what is under test here.
    """
    transport = _CapturingTransport()
    real_init = sentry_sdk.init
    previous_client = sentry_sdk.get_global_scope().client

    def init(**kwargs):
        kwargs.update(
            transport=transport,
            integrations=[],
            default_integrations=False,
            auto_enabling_integrations=False,
        )
        return real_init(**kwargs)

    monkeypatch.setattr(sentry_sdk, "init", init)
    monkeypatch.setattr(dispatch.extensions, "SENTRY_DSN", "https://probe@o0.ingest.sentry.io/0")
    monkeypatch.setattr(dispatch.extensions, "ENV_TAGS", {"probe_tag": "probe-value"})

    try:
        yield SimpleNamespace(
            configure=dispatch.extensions.configure_extensions, events=transport.events
        )
    finally:
        # The tags land on the process-wide isolation scope, and the client is
        # global; neither is undone by monkeypatch. Restoring the client rather
        # than re-initialising matters: a DSN-less Client still reports itself
        # active, so every later test would run the full event pipeline.
        sentry_sdk.get_isolation_scope().remove_tag("probe_tag")
        sentry_sdk.get_global_scope().set_client(previous_client)


@pytest.fixture
def probe_route():
    """A route on the real API app that raises, removed again afterwards."""
    path = "/_sentry_probe"

    @api.get(path)
    def _boom():
        raise SentryProbeError("probe")

    route = api.router.routes[-1]
    try:
        yield path
    finally:
        api.router.routes.remove(route)


def test_sentry_middleware_is_the_sdks_own():
    assert any(m.cls is SentryAsgiMiddleware for m in api.user_middleware)


def test_captured_exception_carries_request_context_and_env_tags(
    session, sentry_probe, probe_route
):
    sentry_probe.configure()

    response = TestClient(api).get(f"{probe_route}?probe=1", headers={"X-Api-Key": "shhh"})
    assert response.status_code == 500

    events = [
        e
        for e in sentry_probe.events
        if any(
            v.get("type") == "SentryProbeError" for v in e.get("exception", {}).get("values", [])
        )
    ]
    assert events, sentry_probe.events

    event = events[0]
    assert event["tags"]["probe_tag"] == "probe-value"
    assert event["request"]["url"].endswith(probe_route)
    assert event["request"]["query_string"] == "probe=1"
    assert event["request"]["method"] == "GET"

    # send_default_pii is off, so the SDK withholds the client IP and redacts
    # sensitive headers. sentry-asgi sent both unconditionally.
    assert "env" not in event["request"]
    assert event["request"]["headers"]["x-api-key"] != "shhh"


def test_reporting_path_raises_no_sentry_deprecation_warning(session, sentry_probe, probe_route):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        sentry_probe.configure()
        TestClient(api).get(probe_route)

    offenders = [
        f"{w.category.__name__} at {w.filename}:{w.lineno}: {w.message}"
        for w in caught
        if issubclass(w.category, DeprecationWarning)
        and "sentry" in f"{w.message} {w.filename}".lower()
    ]
    assert not offenders, offenders


def test_captured_exception_url_does_not_repeat_the_mount_prefix(
    session, sentry_probe, probe_route
):
    """Requests reach the API through app.mount("/api/v1"), not the api app directly.

    Starlette >= 0.33 leaves the mount prefix in both scope["path"] and
    scope["root_path"], so a middleware that concatenates them reports
    /api/v1/api/v1/... and Sentry's URL grouping splits on it (#298).
    """
    sentry_probe.configure()

    response = TestClient(app).get(f"/api/v1{probe_route}")
    assert response.status_code == 500

    events = [
        e
        for e in sentry_probe.events
        if any(
            v.get("type") == "SentryProbeError" for v in e.get("exception", {}).get("values", [])
        )
    ]
    assert events, sentry_probe.events

    url = events[0]["request"]["url"]
    assert url == f"http://testserver/api/v1{probe_route}", url

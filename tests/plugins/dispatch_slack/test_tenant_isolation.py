"""Tenant isolation for the Slack Bolt app.

Every Slack request used to be served by one process-global Bolt ``App`` whose
token and configuration were overwritten per request. That was only ever safe
because all four routes ran on the event loop with nothing suspending between
the write and the dispatch that read it back. `/slack/menu` becoming a
non-async endpoint broke that: Starlette runs non-async endpoints in a
threadpool, so the write and the read now interleave with other requests for
real.

These tests force that interleaving with barriers rather than hoping for it
under load, and assert at the boundary that matters -- the token on the
``WebClient`` Bolt builds for the request, which is what a listener would
actually call Slack with.
"""

import threading

import pytest
from slack_bolt.request import BoltRequest

from dispatch.plugins.dispatch_slack import app as slack_app
from dispatch.plugins.dispatch_slack.config import SlackConversationConfiguration

ORG_A_TOKEN = "xoxb-org-a-token-not-real"
ORG_B_TOKEN = "xoxb-org-b-token-not-real"


def make_configuration(prefix: str, token: str) -> SlackConversationConfiguration:
    """A configuration whose every tenant-visible value is distinct."""
    return SlackConversationConfiguration(
        api_bot_token=token,
        signing_secret=f"{prefix}-signing-secret-not-real",
        socket_mode_app_token=f"xapp-{prefix}-not-real",
        app_user_slug=f"{prefix}-bot",
        slack_command_list_signals=f"/{prefix}-list-signals",
        slack_command_create_case=f"/{prefix}-create-case",
    )


class FakePluginInstance:
    """Stands in for the row `get_request_handler` selects after verifying the
    request signature. Only `id` and `configuration` are reached from there.

    `plugin_instance` is a per-organization table, so `id` is only unique
    within one tenant schema -- which is why the tests below give two different
    organizations the same instance id.
    """

    def __init__(self, instance_id: int, configuration: SlackConversationConfiguration):
        self.id = instance_id
        self.configuration = configuration


@pytest.fixture
def isolated_app_cache(monkeypatch):
    """Give each test its own App cache, so ordering cannot leak between them."""
    monkeypatch.setattr(slack_app, "_apps", {})
    monkeypatch.setattr(slack_app, "_apps_lock", threading.Lock())


def dispatch_time_token(app) -> str:
    """The token Bolt would really call Slack with for a request on this app.

    Asserting on ``app._token`` would miss a later mutation; Bolt reads the
    token when it builds the request's client, which is what this reproduces.
    """
    request = BoltRequest(body="payload=%7B%7D", headers={}, mode="http")
    app._init_context(request)
    return request.context["client"].token


@pytest.fixture
def two_tenants():
    # Deliberately the same id: each tenant schema has its own sequence, so
    # this is the normal case rather than a contrived one.
    return (
        FakePluginInstance(1, make_configuration("orga", ORG_A_TOKEN)),
        FakePluginInstance(1, make_configuration("orgb", ORG_B_TOKEN)),
    )


def test_each_configuration_gets_its_own_app(isolated_app_cache, two_tenants):
    """Two tenants must not be served by one App."""
    org_a, org_b = two_tenants

    app_a = slack_app.get_app("org-a", org_a)
    app_b = slack_app.get_app("org-b", org_b)

    assert app_a is not app_b
    assert dispatch_time_token(app_a) == ORG_A_TOKEN
    assert dispatch_time_token(app_b) == ORG_B_TOKEN


def test_two_organizations_sharing_an_instance_id_do_not_share_an_app(
    isolated_app_cache, two_tenants
):
    """`plugin_instance.id` alone is not an identity.

    The table lives in each organization's own schema, so every tenant has an
    instance with id 1. Keying the cache on the id alone made two organizations
    collide on one entry -- they would evict each other on every request,
    rebuilding the App each time, and any two tenants whose configurations
    happened to digest alike would have been handed the same App outright.
    """
    org_a, org_b = two_tenants
    assert org_a.id == org_b.id, "the fixture no longer exercises the collision"

    app_a = slack_app.get_app("org-a", org_a)
    app_b = slack_app.get_app("org-b", org_b)

    assert app_a is not app_b
    # Both must still be cached: neither displaced the other.
    assert slack_app.get_app("org-a", org_a) is app_a
    assert slack_app.get_app("org-b", org_b) is app_b
    assert dispatch_time_token(app_a) == ORG_A_TOKEN
    assert dispatch_time_token(app_b) == ORG_B_TOKEN


def test_the_same_configuration_reuses_one_app(isolated_app_cache, two_tenants):
    """Reuse is the point of the cache; only isolation between tenants matters."""
    org_a, _ = two_tenants

    assert slack_app.get_app("org-a", org_a) is slack_app.get_app("org-a", org_a)


def test_building_an_app_does_not_grow_a_shared_listener_list(isolated_app_cache, two_tenants):
    """The per-request `configure()` calls appended to the global app forever.

    Bolt's registration appends rather than replaces, so configuring on every
    request grew one shared list without bound -- a leak, and a dispatch scan
    that got longer for the life of the process.
    """
    org_a, org_b = two_tenants

    app_a = slack_app.get_app("org-a", org_a)
    listener_count = len(app_a._listeners)

    # Absolute, not just self-consistent: an `apply()` that bound nothing would
    # leave every App at zero and satisfy a comparison against itself. The
    # registry's own entries plus the commands `configure()` adds is the floor.
    assert listener_count > len(slack_app.listeners._registrations)

    for _ in range(25):
        slack_app.build_app(org_a.configuration)
        slack_app.build_app(org_b.configuration)

    assert len(app_a._listeners) == listener_count
    assert len(slack_app.get_app("org-b", org_b)._listeners) == listener_count


def test_rotating_a_token_retires_the_cached_app(isolated_app_cache, two_tenants):
    """A cache that pinned the old credential would keep using a dead token."""
    org_a, _ = two_tenants

    before = slack_app.get_app("org-a", org_a)
    assert dispatch_time_token(before) == ORG_A_TOKEN

    org_a.configuration = make_configuration("orga", "xoxb-org-a-rotated-not-real")
    after = slack_app.get_app("org-a", org_a)

    assert after is not before
    assert dispatch_time_token(after) == "xoxb-org-a-rotated-not-real"


def test_renaming_a_command_retires_the_cached_app(isolated_app_cache, two_tenants):
    """Configuration identity is more than the token.

    Commands are registered from the configuration, so an App built before a
    rename would keep answering the old command and ignore the new one.
    """
    org_a, _ = two_tenants

    before = slack_app.get_app("org-a", org_a)
    rotated = make_configuration("orga", ORG_A_TOKEN)
    rotated.slack_command_create_case = "/orga-report-issue"
    org_a.configuration = rotated

    assert slack_app.get_app("org-a", org_a) is not before


def run_interleaved(first, second):
    """Run two callables so that each has acted before either finishes.

    Both threads publish their tenant state, meet at the barrier, and only then
    read back what they will act with. Under the old shared app the second
    write had certainly landed before the first thread read -- which is exactly
    the crossover being guarded against, made deterministic instead of timed.
    """
    barrier = threading.Barrier(2)
    results = {}
    apps = {}
    errors = []

    def run(name, work):
        try:
            app = work()
            barrier.wait(timeout=10)
            # Both the token and the object: asserting only on tokens cannot
            # tell one shared App from two equivalent ones, which is the
            # difference the cache's double-checked insert exists to make.
            apps[name] = app
            results[name] = dispatch_time_token(app)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            try:
                barrier.abort()
            except Exception:  # noqa: BLE001, S110
                pass

    threads = [
        # Daemon so a listener that never returns fails the test on the join
        # timeout instead of holding the interpreter open past it.
        threading.Thread(target=run, args=("first", first), daemon=True),
        threading.Thread(target=run, args=("second", second), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, errors
    return results, apps


def test_concurrent_requests_for_two_tenants_stay_isolated(isolated_app_cache, two_tenants):
    """menu/menu: the interleaving the threadpool made reachable."""
    org_a, org_b = two_tenants

    results, apps = run_interleaved(
        lambda: slack_app.get_app("org-a", org_a), lambda: slack_app.get_app("org-b", org_b)
    )

    assert results == {"first": ORG_A_TOKEN, "second": ORG_B_TOKEN}
    assert apps["first"] is not apps["second"]


def test_concurrent_requests_stay_isolated_in_either_order(isolated_app_cache, two_tenants):
    """The reverse pairing, since one order could pass by luck of who wrote last."""
    org_a, org_b = two_tenants

    results, apps = run_interleaved(
        lambda: slack_app.get_app("org-b", org_b), lambda: slack_app.get_app("org-a", org_a)
    )

    assert results == {"first": ORG_B_TOKEN, "second": ORG_A_TOKEN}
    assert apps["first"] is not apps["second"]


def test_concurrent_requests_for_one_tenant_share_an_app(
    isolated_app_cache, two_tenants, monkeypatch
):
    """Same-tenant concurrency must stay correct and still reuse the cache.

    Both threads are held *inside* `build_app` until the other arrives, so the
    double-checked insert is genuinely contended. Racing them around `get_app`
    instead leaves it to the scheduler whether both ever build at once -- the
    difference between testing the contention and hoping for it. Whichever
    thread loses the insert must adopt the winner's App rather than installing
    a second one.
    """
    org_a, _ = two_tenants

    building = threading.Barrier(2)
    real_build_app = slack_app.build_app

    def build_in_lockstep(configuration):
        # Neither caller can install before both have missed the cache: the
        # first to arrive is parked here until the second does.
        building.wait(timeout=10)
        return real_build_app(configuration)

    monkeypatch.setattr(slack_app, "build_app", build_in_lockstep)

    results, apps = run_interleaved(
        lambda: slack_app.get_app("org-a", org_a), lambda: slack_app.get_app("org-a", org_a)
    )

    assert results == {"first": ORG_A_TOKEN, "second": ORG_A_TOKEN}
    # Identity, not just equal tokens: both threads built, so only the
    # double-checked insert makes the loser adopt the winner's App. Comparing
    # tokens alone passes just as happily when two equivalent Apps are handed
    # out, which is the thing this is here to detect.
    assert apps["first"] is apps["second"]

    monkeypatch.setattr(slack_app, "build_app", real_build_app)
    # Exactly one App survived the race, and it is the one still handed out.
    assert len(slack_app._apps) == 1
    assert slack_app.get_app("org-a", org_a) is apps["first"]


def test_a_failure_for_one_tenant_leaves_the_other_untouched(isolated_app_cache, two_tenants):
    """One tenant's bad configuration must not disturb another's cached App."""
    org_a, org_b = two_tenants
    app_b = slack_app.get_app("org-b", org_b)

    broken = FakePluginInstance(99, make_configuration("broken", "xoxb-broken-not-real"))
    broken.configuration = None  # digesting this raises

    with pytest.raises(AttributeError):
        slack_app.get_app("broken-org", broken)

    assert slack_app.get_app("org-b", org_b) is app_b
    assert dispatch_time_token(app_b) == ORG_B_TOKEN


def test_the_previous_shared_app_design_crosses_tenants():
    """Why there is an App per configuration rather than one reconfigured app.

    This is a demonstration, not a guard: it exercises a model of the previous
    design rather than any shipped code, and with both writes forced ahead of
    both reads it cannot fail. It earns its place by making the crossover
    reproducible -- the old code contained no visible sharing, only an
    assignment and, much later, a read, which is why it survived review the
    first time. Deleting it loses the only executable statement of what the
    per-configuration App is defending against.
    """

    class ReconfiguredPerRequest:
        """`app._token = ...` in get_request_handler, read by `_init_context`."""

        token: str | None = None

    shared = ReconfiguredPerRequest()
    barrier = threading.Barrier(2)
    observed = {}

    def serve(name: str, token: str) -> None:
        shared.token = token  # get_request_handler mutates the shared app
        barrier.wait(timeout=10)  # a request for the other tenant lands here
        observed[name] = shared.token  # Bolt reads the token at dispatch time

    threads = [
        threading.Thread(target=serve, args=("a", ORG_A_TOKEN)),
        threading.Thread(target=serve, args=("b", ORG_B_TOKEN)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert observed["a"] != ORG_A_TOKEN or observed["b"] != ORG_B_TOKEN, (
        "the modelled shared-app design did not cross tenants; if it can no "
        "longer do so, this test has stopped describing the original defect"
    )


def test_the_shared_registry_holds_no_tenant_state(two_tenants):
    """The registry is process-global on purpose, so it must stay tenant-free.

    Replacing a mutable global App with a mutable global registry would have
    moved the bug rather than fixed it. What it records is the listeners that
    are identical for every organization; the configuration-derived ones are
    registered on each App instead.
    """
    from dispatch.plugins.dispatch_slack.bolt import listeners

    org_a, org_b = two_tenants
    before = len(listeners._registrations)

    slack_app.build_app(org_a.configuration)
    slack_app.build_app(org_b.configuration)

    assert len(listeners._registrations) == before

    recorded = repr(listeners._registrations)
    assert ORG_A_TOKEN not in recorded
    assert ORG_B_TOKEN not in recorded
    assert "orga" not in recorded
    assert "orgb" not in recorded

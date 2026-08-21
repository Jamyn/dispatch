"""OpenID Connect id token validation, exercised with Microsoft Entra ID tokens.

Dispatch authenticates against Entra through the generic PKCE provider, so
these are tests of that provider -- but written against the claims, issuer and
key handling a real Entra tenant produces, because the ways this integration
can be wrong are specific to what Entra actually mints.

The load-bearing case is `test_a_token_from_another_entra_tenant_is_rejected`.
Entra serves overlapping signing keys across every tenant (the same `kid` is
published at /common and at an individual tenant's endpoint), so a valid
signature proves the token came from Microsoft, not that it came from *your*
directory. Only the issuer check makes that distinction.
"""

from datetime import datetime, timedelta

import pytest
import requests
from fastapi import HTTPException
from jose import jwt
from starlette.requests import Request

import dispatch.plugins.dispatch_core.plugin as core_plugin
from dispatch.plugins.dispatch_core.plugin import PKCEAuthProviderPlugin

TENANT_ID = "aaaabbbb-0000-cccc-1111-dddd2222eeee"
OTHER_TENANT_ID = "99990000-1111-2222-3333-444455556666"
CLIENT_ID = "11112222-bbbb-3333-cccc-4444dddd5555"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
JWKS_URL = f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"
UPN = "ada@contoso.onmicrosoft.com"


def entra_claims(**overrides) -> dict:
    """A v2.0 id token as the Entra /token endpoint issues it.

    Deliberately complete: `aio`, `rh` and `uti` are opaque claims Dispatch
    must ignore, and `at_hash` is absent because Entra emits it only from
    /authorize, never from the code-for-token exchange this flow uses.
    """
    now = datetime.utcnow()
    claims = {
        "aud": CLIENT_ID,
        "iss": ISSUER,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "aio": "AXQAi/8TAAAA6ptu6Ge3s0Dw==",
        "name": "Ada Lovelace",
        "oid": "77778888-eeee-9999-ffff-0000aaaa1111",
        "preferred_username": UPN,
        "rh": "0.AR0AptQ.",
        "sub": "8sKcMPmVJTHRr0NcXTaLZOsRUgAJoZ2FbtQ0lDBmzXQ",
        "tid": TENANT_ID,
        "uti": "bMxUYPMuTUCkAB2rCwUAAA",
        "ver": "2.0",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


class FakeJwksEndpoint:
    """Stands in for login.microsoftonline.com/.../discovery/v2.0/keys."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.calls = 0
        self.error = None

    def get(self, url, timeout=None):
        self.calls += 1
        if self.error:
            raise self.error
        endpoint = self

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"keys": endpoint.keys}

        return Response()


@pytest.fixture
def entra(monkeypatch, signing_keys):
    """A configured, single-tenant Entra deployment with its JWKS served."""
    endpoint = FakeJwksEndpoint([signing_keys["current"][1]])
    monkeypatch.setattr(core_plugin.requests, "get", endpoint.get)
    monkeypatch.setattr(core_plugin, "DISPATCH_AUTHENTICATION_PROVIDER_PKCE_JWKS", JWKS_URL)
    monkeypatch.setattr(core_plugin, "DISPATCH_AUTHENTICATION_PROVIDER_PKCE_ISSUER", ISSUER)
    monkeypatch.setattr(core_plugin, "DISPATCH_JWT_AUDIENCE", CLIENT_ID)
    monkeypatch.setattr(core_plugin, "DISPATCH_JWT_EMAIL_OVERRIDE", "preferred_username")
    core_plugin.jwks_cache.clear()
    yield endpoint
    core_plugin.jwks_cache.clear()


def id_token(claims, signing_keys, key="current", kid=None) -> str:
    pem, entry = signing_keys[key]
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid or entry["kid"]})


def bearer(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/default/incidents",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "query_string": b"",
        }
    )


def assert_rejected(token):
    with pytest.raises(HTTPException) as exc:
        PKCEAuthProviderPlugin().get_current_user(bearer(token))
    assert exc.value.status_code == 401


# --- the success path ----------------------------------------------------


def test_an_entra_id_token_identifies_its_user(entra, signing_keys):
    """A well-formed v2.0 id token from the configured tenant authenticates."""
    token = id_token(entra_claims(), signing_keys)

    assert PKCEAuthProviderPlugin().get_current_user(bearer(token)) == UPN


def test_an_entra_id_token_carries_no_at_hash_to_verify(entra, signing_keys):
    """Entra emits at_hash only from /authorize, so the code flow needs no opt-out.

    DISPATCH_PKCE_DONT_VERIFY_AT_HASH exists for providers that do send one.
    Pinned here because recommending it for Entra would be advising operators
    to switch off a check for a claim their tokens never contain.
    """
    assert "at_hash" not in entra_claims()

    token = id_token(entra_claims(), signing_keys)
    assert PKCEAuthProviderPlugin().get_current_user(bearer(token)) == UPN


# --- issuer: the tenant boundary -----------------------------------------


def test_a_token_from_another_entra_tenant_is_rejected(entra, signing_keys):
    """Signed by the very key this deployment trusts, but issued elsewhere.

    This is not hypothetical: Microsoft signs every tenant's tokens from a
    shared key set, so the token below verifies. If the app registration is
    ever switched to multitenant -- or a second registration names the same
    client id -- the audience check stops isolating tenants too, and the
    issuer is all that is left between a stranger's directory and a Dispatch
    session that auto-provisions on first sight.
    """
    foreign = entra_claims(
        iss=f"https://login.microsoftonline.com/{OTHER_TENANT_ID}/v2.0",
        tid=OTHER_TENANT_ID,
        preferred_username="attacker@evil.onmicrosoft.com",
    )

    assert_rejected(id_token(foreign, signing_keys))


def test_a_token_from_the_common_endpoint_is_rejected(entra, signing_keys):
    """`/common` never appears in an `iss`; a token claiming it is not ours."""
    assert_rejected(
        id_token(entra_claims(iss="https://login.microsoftonline.com/common/v2.0"), signing_keys)
    )


def test_a_token_with_no_issuer_is_rejected(entra, signing_keys):
    """An expected issuer must be a requirement, not a comparison that is skipped."""
    assert_rejected(id_token(entra_claims(iss=None), signing_keys))


def test_without_a_configured_issuer_any_tenant_is_accepted(
    entra, signing_keys, monkeypatch, caplog
):
    """The backwards-compatible default, pinned so the exposure stays visible.

    Existing PKCE deployments predate this setting and must keep working, so
    an unset issuer cannot start rejecting tokens. What it must not do is be
    silent: config.py warns at startup, and this is what that warning is about.
    """
    monkeypatch.setattr(core_plugin, "DISPATCH_AUTHENTICATION_PROVIDER_PKCE_ISSUER", None)
    foreign = entra_claims(
        iss=f"https://login.microsoftonline.com/{OTHER_TENANT_ID}/v2.0", tid=OTHER_TENANT_ID
    )

    assert PKCEAuthProviderPlugin().get_current_user(bearer(id_token(foreign, signing_keys))) == UPN


# --- audience ------------------------------------------------------------


def test_a_token_issued_for_a_different_application_is_rejected(entra, signing_keys):
    """A token for another app in the same tenant is not a Dispatch credential."""
    assert_rejected(
        id_token(entra_claims(aud="00000003-0000-0000-c000-000000000000"), signing_keys)
    )


def test_a_token_is_rejected_when_no_audience_is_configured(entra, signing_keys, monkeypatch):
    """Unset audience must fail closed rather than skip the check.

    Every Entra id token carries `aud`, and python-jose rejects a present
    audience that nothing expects -- so a deployment that forgets
    DISPATCH_JWT_AUDIENCE gets 401s, not an unchecked audience.
    """
    monkeypatch.setattr(core_plugin, "DISPATCH_JWT_AUDIENCE", None)

    assert_rejected(id_token(entra_claims(), signing_keys))


# --- signature -----------------------------------------------------------


def test_a_token_signed_by_a_key_entra_does_not_publish_is_rejected(entra, signing_keys):
    """A key of the attacker's own, announced under a published kid."""
    forged = id_token(entra_claims(), signing_keys, key="next", kid="current-key")

    assert_rejected(forged)


# --- lifetime and clock skew ---------------------------------------------


def test_an_expired_id_token_is_rejected(entra, signing_keys):
    now = datetime.utcnow()
    expired = entra_claims(exp=int((now - timedelta(minutes=10)).timestamp()))

    assert_rejected(id_token(expired, signing_keys))


def test_an_id_token_with_no_expiry_is_rejected(entra, signing_keys):
    """OpenID Connect requires `exp`; without it the token never stops working."""
    assert_rejected(id_token(entra_claims(exp=None), signing_keys))


def test_a_token_within_the_configured_clock_skew_is_accepted(entra, signing_keys, monkeypatch):
    """Entra stamps `nbf` at issue time, so an unsynced host would 401 everyone."""
    monkeypatch.setattr(core_plugin, "DISPATCH_AUTHENTICATION_PROVIDER_PKCE_LEEWAY_SECONDS", 60)
    ahead = datetime.utcnow() + timedelta(seconds=30)
    skewed = entra_claims(iat=int(ahead.timestamp()), nbf=int(ahead.timestamp()))

    assert PKCEAuthProviderPlugin().get_current_user(bearer(id_token(skewed, signing_keys))) == UPN


def test_a_token_beyond_the_configured_clock_skew_is_rejected(entra, signing_keys, monkeypatch):
    """Leeway is a tolerance, not a way to switch expiry off."""
    monkeypatch.setattr(core_plugin, "DISPATCH_AUTHENTICATION_PROVIDER_PKCE_LEEWAY_SECONDS", 60)
    long_expired = entra_claims(
        exp=int((datetime.utcnow() - timedelta(minutes=30)).timestamp()),
    )

    assert_rejected(id_token(long_expired, signing_keys))


# --- which claim carries the identity ------------------------------------


def test_the_configured_claim_wins_over_the_email_claim(entra, signing_keys):
    """`preferred_username` is the UPN; `email` is whatever the user set.

    Microsoft documents `email` as user-mutable and not guaranteed correct.
    A deployment that names preferred_username must get the UPN even when both
    claims are present and they disagree.
    """
    token = id_token(entra_claims(email="ada.personal@example.com"), signing_keys)

    assert PKCEAuthProviderPlugin().get_current_user(bearer(token)) == UPN


def test_the_email_claim_is_used_when_no_override_is_configured(entra, signing_keys, monkeypatch):
    monkeypatch.setattr(core_plugin, "DISPATCH_JWT_EMAIL_OVERRIDE", None)
    token = id_token(entra_claims(email="ada@contoso.com"), signing_keys)

    assert PKCEAuthProviderPlugin().get_current_user(bearer(token)) == "ada@contoso.com"


def test_a_token_missing_the_configured_identity_claim_is_rejected(entra, signing_keys):
    """The common Entra misconfiguration, and it must 401 rather than 500.

    `email` is not emitted for managed users unless the optional claim or the
    email scope is configured, so pointing DISPATCH_JWT_EMAIL_OVERRIDE at a
    claim the tenant does not issue is the first thing an operator gets wrong.
    """
    assert_rejected(id_token(entra_claims(preferred_username=None), signing_keys))


def test_a_token_whose_identity_claim_is_empty_is_rejected(entra, signing_keys):
    """An empty string would otherwise become a Dispatch user with no email."""
    assert_rejected(id_token(entra_claims(preferred_username=""), signing_keys))


# --- malformed credentials must not 500 ----------------------------------


@pytest.mark.parametrize(
    "token",
    ["opaque-session-token", "a.b.c", "...", "ZXlKaGJHY2lPaUpTVXpJMU5pSjk"],
    ids=["opaque", "three-empty-segments", "dots", "base64-but-not-a-jwt"],
)
def test_a_bearer_credential_that_is_not_a_jwt_is_rejected(entra, token):
    """These once reached an unguarded b64decode/json.loads: an unauthenticated 500."""
    assert_rejected(token)


def test_a_token_with_no_kid_is_rejected(entra, signing_keys):
    pem, _ = signing_keys["current"]
    token = jwt.encode(entra_claims(), pem, algorithm="RS256")

    assert_rejected(token)


@pytest.mark.parametrize(
    "headers",
    [{}, {"authorization": "Bearer "}, {"authorization": "Basic dXNlcjpwYXNz"}],
    ids=["absent", "empty-token", "wrong-scheme"],
)
def test_an_unusable_authorization_header_is_rejected(entra, headers):
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/default/incidents",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
            "query_string": b"",
        }
    )

    with pytest.raises(HTTPException) as exc:
        PKCEAuthProviderPlugin().get_current_user(request)
    assert exc.value.status_code == 401


def test_an_unconfigured_provider_rejects_rather_than_erroring(monkeypatch, signing_keys):
    """No JWKS url means requests.get(None): a 500 on every request."""
    monkeypatch.setattr(core_plugin, "DISPATCH_AUTHENTICATION_PROVIDER_PKCE_JWKS", None)

    assert_rejected(id_token(entra_claims(), signing_keys))


# --- key handling: caching, rotation, provider outages -------------------


def test_the_key_set_is_not_refetched_for_every_request(entra, signing_keys):
    """Uncached, each authenticated API call blocks on login.microsoftonline.com."""
    token = id_token(entra_claims(), signing_keys)

    for _ in range(5):
        PKCEAuthProviderPlugin().get_current_user(bearer(token))

    assert entra.calls == 1


def test_a_rotated_signing_key_is_picked_up_without_waiting_for_the_cache(entra, signing_keys):
    """Entra rotates keys on its own schedule, mid-TTL.

    A cache that only refreshes on expiry would 401 every request signed by the
    new key until the TTL ran out, so an unknown kid has to force one refetch.
    """
    PKCEAuthProviderPlugin().get_current_user(bearer(id_token(entra_claims(), signing_keys)))
    assert entra.calls == 1

    entra.keys = [signing_keys["current"][1], signing_keys["next"][1]]
    rotated = id_token(entra_claims(), signing_keys, key="next")

    assert PKCEAuthProviderPlugin().get_current_user(bearer(rotated)) == UPN
    assert entra.calls == 2


def test_an_unknown_kid_is_rejected_once_the_key_set_has_been_refreshed(entra, signing_keys):
    """The refresh is an attempt to find the key, not a way to accept without one."""
    assert_rejected(id_token(entra_claims(), signing_keys, key="next", kid="never-published"))


def test_repeated_unknown_kids_do_not_refetch_the_key_set_each_time(entra, signing_keys):
    """Otherwise a junk `kid` turns every request into a call to the provider.

    Two fetches, not one: the first request populates an empty cache, and its
    unknown kid earns the one refresh that could have found a rotated key. The
    four after it are inside the refresh floor and take no network at all.
    """
    for _ in range(5):
        assert_rejected(id_token(entra_claims(), signing_keys, kid="never-published"))

    assert entra.calls == 2


def test_authentication_survives_a_provider_outage_once_keys_are_cached(entra, signing_keys):
    """A login.microsoftonline.com blip must not log out every signed-in user."""
    token = id_token(entra_claims(), signing_keys)
    PKCEAuthProviderPlugin().get_current_user(bearer(token))

    entra.error = requests.ConnectionError("login.microsoftonline.com unreachable")
    core_plugin.jwks_cache._entries[JWKS_URL]["fetched_at"] -= 10_000

    assert PKCEAuthProviderPlugin().get_current_user(bearer(token)) == UPN


def test_an_unreachable_key_endpoint_with_nothing_cached_is_rejected(entra, signing_keys):
    """Fail closed, and as a 401 rather than an unhandled requests exception."""
    entra.error = requests.ConnectionError("login.microsoftonline.com unreachable")

    assert_rejected(id_token(entra_claims(), signing_keys))


def test_a_key_endpoint_error_response_with_nothing_cached_is_rejected(entra, signing_keys):
    entra.error = requests.HTTPError("503 Service Unavailable")

    assert_rejected(id_token(entra_claims(), signing_keys))


# --- from an id token to a Dispatch user ---------------------------------


@pytest.fixture
def entra_login(entra, signing_keys, session, monkeypatch):
    """Drive the whole chain: id token -> PKCE provider -> DispatchUser row.

    Uses the "default" organization init_database seeds, because auth.service
    resolves the default project with one_or_none() -- a second default in the
    same schema makes it raise rather than choose.
    """
    from dispatch.auth import service as auth_service
    from dispatch.plugins.base import register

    register(PKCEAuthProviderPlugin)
    monkeypatch.setattr(
        auth_service, "DISPATCH_AUTHENTICATION_PROVIDER_SLUG", "dispatch-auth-provider-pkce"
    )

    def _login(**claim_overrides):
        request = bearer(id_token(entra_claims(**claim_overrides), signing_keys))
        request.state.db = session
        request.state.organization = "default"
        return auth_service.get_current_user(request)

    return _login


def test_signing_in_through_entra_provisions_a_dispatch_user(entra_login, session):
    """First sight of a tenant member creates the account, with no admin step."""
    from dispatch.auth import service as auth_service
    from dispatch.enums import UserRoles

    assert auth_service.get_by_email(db_session=session, email=UPN) is None

    user = entra_login()

    assert user.email == UPN
    assert user.get_organization_role("default") == UserRoles.member


def test_a_second_sign_in_reuses_the_existing_account(entra_login, session):
    """Identity is keyed on the email claim, so the same UPN is the same user."""
    first = entra_login()
    session.commit()

    second = entra_login()

    assert second.id == first.id


def test_an_entra_user_matching_an_existing_dispatch_account_is_linked_to_it(entra_login, session):
    """Account linking is by email, including for accounts created another way.

    Worth pinning because it is the security consequence of that choice: an
    account that predates Entra -- a basic-auth owner, say -- is adopted by
    whoever the directory hands that address to, with the role it already had.
    Restricting the issuer to one tenant is what makes that safe, since only
    that tenant can then assert the address.
    """
    from dispatch.auth import service as auth_service
    from dispatch.auth.models import UserRegister
    from dispatch.enums import UserRoles

    existing = auth_service.create(
        db_session=session,
        organization="default",
        user_in=UserRegister(email=UPN),
    )
    auth_service.create_or_update_organization_role(
        db_session=session,
        user=existing,
        role_in=_owner_of_default(session),
    )
    session.commit()

    user = entra_login()

    assert user.id == existing.id
    assert user.get_organization_role("default") == UserRoles.owner


def _owner_of_default(session):
    from dispatch.auth.models import UserOrganization
    from dispatch.enums import UserRoles
    from dispatch.organization import service as organization_service
    from dispatch.organization.models import OrganizationRead

    organization = organization_service.get_by_slug(db_session=session, slug="default")
    return UserOrganization(
        organization=OrganizationRead.from_orm(organization), role=UserRoles.owner
    )


def test_a_rejected_token_never_reaches_user_provisioning(
    entra_login, entra, session, signing_keys
):
    """A 401 has to stop before the row is created, not after.

    auth.service provisions on whatever email the provider returns, so a token
    the provider should have rejected would otherwise become an account.
    """
    from dispatch.auth import service as auth_service

    with pytest.raises(HTTPException) as exc:
        entra_login(iss=f"https://login.microsoftonline.com/{OTHER_TENANT_ID}/v2.0")

    assert exc.value.status_code == 401
    assert auth_service.get_by_email(db_session=session, email=UPN) is None


# --- the at_hash opt-out -------------------------------------------------


def test_a_token_carrying_an_at_hash_is_rejected_by_default(entra, signing_keys):
    """Nothing here holds the access token, so an at_hash cannot be checked.

    Entra never sends one from /token, but providers that do would 401 -- which
    is what DISPATCH_PKCE_DONT_VERIFY_AT_HASH exists to let an operator accept.
    """
    assert_rejected(id_token(entra_claims(at_hash="uJiMMlv4pHhAJPGRKkPTUw"), signing_keys))


def test_the_at_hash_check_can_be_turned_off(entra, signing_keys, monkeypatch):
    monkeypatch.setattr(core_plugin, "DISPATCH_PKCE_DONT_VERIFY_AT_HASH", True)
    token = id_token(entra_claims(at_hash="uJiMMlv4pHhAJPGRKkPTUw"), signing_keys)

    assert PKCEAuthProviderPlugin().get_current_user(bearer(token)) == UPN


def test_setting_the_at_hash_opt_out_to_false_does_not_turn_the_check_off(monkeypatch):
    """It is read as a bool, not as a non-empty string.

    Uncast, `DISPATCH_PKCE_DONT_VERIFY_AT_HASH=false` is the string "false" --
    truthy -- so an operator writing the value that means "keep verifying"
    switched verification off instead, with nothing to indicate it.
    """
    import importlib

    from dispatch import config

    monkeypatch.setenv("DISPATCH_PKCE_DONT_VERIFY_AT_HASH", "false")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.DISPATCH_PKCE_DONT_VERIFY_AT_HASH is False
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_a_token_with_no_audience_is_rejected_when_one_is_expected(entra, signing_keys):
    """An expected audience has to be a requirement, not a check that is skipped.

    python-jose passes a token that simply omits `aud`, so requiring the claim
    has to be stated separately from requiring it to match.
    """
    assert_rejected(id_token(entra_claims(aud=None), signing_keys))


def test_an_identity_claim_that_is_not_a_string_is_rejected(entra, signing_keys):
    """Whatever this claim holds becomes a Dispatch account, so it must be text."""
    token = id_token(entra_claims(preferred_username=["ada@contoso.com"]), signing_keys)

    assert_rejected(token)

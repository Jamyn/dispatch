"""
.. module: dispatch.plugins.dispatch_core.plugin
    :platform: Unix
    :copyright: (c) 2019 by Netflix Inc., see AUTHORS for more
    :license: Apache, see LICENSE for more details.
"""

import base64
import json
import logging
import time
from uuid import UUID
from typing import Literal
import requests
from cachetools import cached, TTLCache
from fastapi import HTTPException
from fastapi.security.utils import get_authorization_scheme_param
from jose import JWTError, jwt
from jose.exceptions import JWKError
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.status import HTTP_401_UNAUTHORIZED

from dispatch.auth.models import DispatchUser, MfaChallenge, MfaChallengeStatus, MfaPayload
from dispatch.case import service as case_service
from dispatch.config import (
    DISPATCH_AUTHENTICATION_PROVIDER_AWS_ALB_ARN,
    DISPATCH_AUTHENTICATION_PROVIDER_AWS_ALB_EMAIL_CLAIM,
    DISPATCH_AUTHENTICATION_PROVIDER_AWS_ALB_PUBLIC_KEY_CACHE_SECONDS,
    DISPATCH_AUTHENTICATION_PROVIDER_HEADER_NAME,
    DISPATCH_AUTHENTICATION_PROVIDER_PKCE_ISSUER,
    DISPATCH_AUTHENTICATION_PROVIDER_PKCE_JWKS,
    DISPATCH_AUTHENTICATION_PROVIDER_PKCE_JWKS_CACHE_SECONDS,
    DISPATCH_AUTHENTICATION_PROVIDER_PKCE_JWKS_TIMEOUT_SECONDS,
    DISPATCH_AUTHENTICATION_PROVIDER_PKCE_LEEWAY_SECONDS,
    DISPATCH_JWT_ALG,
    DISPATCH_JWT_AUDIENCE,
    DISPATCH_JWT_EMAIL_OVERRIDE,
    DISPATCH_JWT_SECRET,
    DISPATCH_PKCE_DONT_VERIFY_AT_HASH,
    DISPATCH_UI_URL,
)
from dispatch.database.core import Base
from dispatch.incident import service as incident_service
from dispatch.individual import service as individual_service
from dispatch.individual.models import IndividualContact, IndividualContactRead
from dispatch.plugin import service as plugin_service
from dispatch.plugins import dispatch_core as dispatch_plugin
from dispatch.plugins.bases import (
    AuthenticationProviderPlugin,
    ContactPlugin,
    MultiFactorAuthenticationPlugin,
    ParticipantPlugin,
    TicketPlugin,
)
from dispatch.plugins.dispatch_core.config import DispatchTicketConfiguration
from dispatch.plugins.dispatch_core.exceptions import (
    ActionMismatchError,
    ExpiredChallengeError,
    InvalidChallengeError,
    InvalidChallengeStateError,
    UserMismatchError,
)
from dispatch.plugins.dispatch_core.service import create_resource_id
from dispatch.project import service as project_service
from dispatch.route import service as route_service
from dispatch.service import service as service_service
from dispatch.service.models import Service, ServiceRead
from dispatch.team import service as team_service
from dispatch.team.models import TeamContact, TeamContactRead

log = logging.getLogger(__name__)


class BasicAuthProviderPlugin(AuthenticationProviderPlugin):
    title = "Dispatch Plugin - Basic Authentication Provider"
    slug = "dispatch-auth-provider-basic"
    description = "Generic basic authentication provider."
    version = dispatch_plugin.__version__

    author = "Netflix"
    author_url = "https://github.com/netflix/dispatch.git"
    configuration_schema = None

    def get_current_user(self, request: Request, **kwargs):
        authorization: str = request.headers.get("Authorization")
        scheme, param = get_authorization_scheme_param(authorization)
        # `param` is the parsed token; re-splitting the header IndexErrors on a
        # bearer scheme with an empty token, i.e. an unauthenticated 500.
        if not authorization or scheme.lower() != "bearer" or not param:
            log.warning(
                f"Malformed authorization header. Scheme: {scheme} Authorization: {authorization}"
            )
            return

        try:
            # algorithms is required: unset, python-jose honours whatever the
            # token declares, which is the opening for algorithm confusion.
            data = jwt.decode(param, DISPATCH_JWT_SECRET, algorithms=[DISPATCH_JWT_ALG])
        except (JWKError, JWTError):
            raise HTTPException(
                status_code=HTTP_401_UNAUTHORIZED,
                detail=[{"msg": "Could not validate credentials"}],
            ) from None
        return data["email"]


# Floor between repeated fetches of the same key set, whether an unknown kid
# asked for one or a previous fetch failed. Key rotation still lands within this
# window; a token replaying junk kids cannot amplify past it.
JWKS_MIN_REFRESH_INTERVAL_SECONDS = 30

_NEVER = float("-inf")


class JwksUnavailableError(Exception):
    """The identity provider's signing keys could not be retrieved."""


class JwksCache:
    """Caches an OIDC provider's signing keys, refreshing on an unknown `kid`.

    Every authenticated request decodes a token, so an uncached fetch here puts
    a synchronous call to the identity provider on Dispatch's hottest path.
    Refreshing on an unknown kid, and not only on expiry, is what keeps a
    rotated signing key from 401ing every request until the TTL runs out.

    Each entry carries three stamps because each throttles a different thing:
    `fetched_at` ages the keys, `missed_at` bounds how often an unknown kid can
    force a refetch, and `failed_at` bounds retries while the provider is down.
    Collapsing them into one stamp makes the successful fetch that populates the
    cache throttle the rotation refetch that should follow it.
    """

    def __init__(self):
        self._entries: dict[str, dict] = {}

    def clear(self) -> None:
        self._entries.clear()

    def get_key(self, url: str, kid: str, *, ttl: int, timeout: int) -> dict | None:
        """Returns the published key with this kid, or None if there is no such key."""
        now = time.monotonic()
        entry = self._entries.get(url)
        if entry is None:
            entry = self._fetch(url, None, now, timeout, missed=False)

        # Never longer than the TTL: a short TTL is an explicit request for
        # fresher keys and must not be held back by the refresh floor.
        refresh_interval = min(JWKS_MIN_REFRESH_INTERVAL_SECONDS, ttl)
        retry_allowed = now - entry["failed_at"] >= refresh_interval

        if retry_allowed and now - entry["fetched_at"] >= ttl:
            entry = self._fetch(url, entry, now, timeout, missed=False)
        elif (
            retry_allowed
            and kid not in entry["keys"]
            and now - entry["missed_at"] >= refresh_interval
        ):
            entry = self._fetch(url, entry, now, timeout, missed=True)

        return entry["keys"].get(kid)

    def _fetch(self, url: str, entry: dict | None, now: float, timeout: int, *, missed: bool):
        missed_at = now if missed else (entry["missed_at"] if entry else _NEVER)
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            keys = {key["kid"]: key for key in response.json()["keys"] if key.get("kid")}
        except (requests.RequestException, AttributeError, KeyError, TypeError, ValueError) as err:
            if entry is None:
                raise JwksUnavailableError(f"Unable to fetch signing keys from {url}") from err
            # Serving the keys already held beats failing every request for the
            # length of the provider's outage. The successful-fetch stamp is
            # deliberately not moved, so the next retry past the floor tries
            # again rather than settling on stale keys.
            log.warning("Unable to refresh signing keys from %s, using cached keys: %s", url, err)
            entry["failed_at"] = now
            entry["missed_at"] = missed_at
            return entry

        entry = {"keys": keys, "fetched_at": now, "missed_at": missed_at, "failed_at": _NEVER}
        self._entries[url] = entry
        return entry


jwks_cache = JwksCache()


class PKCEAuthProviderPlugin(AuthenticationProviderPlugin):
    title = "Dispatch Plugin - PKCE Authentication Provider"
    slug = "dispatch-auth-provider-pkce"
    description = "Generic OpenID Connect (PKCE) authentication provider."
    version = dispatch_plugin.__version__

    author = "Netflix"
    author_url = "https://github.com/netflix/dispatch.git"
    configuration_schema = None

    def get_current_user(self, request: Request, **kwargs):
        credentials_exception = HTTPException(
            status_code=HTTP_401_UNAUTHORIZED, detail=[{"msg": "Could not validate credentials"}]
        )

        if not DISPATCH_AUTHENTICATION_PROVIDER_PKCE_JWKS:
            log.error(
                "Unable to authenticate. DISPATCH_AUTHENTICATION_PROVIDER_PKCE_JWKS is not set."
            )
            raise credentials_exception

        authorization: str = request.headers.get(
            "Authorization", request.headers.get("authorization")
        )
        scheme, token = get_authorization_scheme_param(authorization)
        # `token` is the parsed parameter; re-splitting the header IndexErrors
        # on a bearer scheme with an empty token, i.e. an unauthenticated 500.
        if not authorization or scheme.lower() != "bearer" or not token:
            log.warning("Unable to authenticate. Malformed authorization header.")
            raise credentials_exception

        try:
            # The library parser, not a hand-rolled base64 decode: the header is
            # base64url and attacker-supplied, so anything that is not a JWT has
            # to come back as a rejection rather than an unhandled 500.
            kid = jwt.get_unverified_header(token).get("kid")
        except (JWTError, JWKError) as err:
            log.warning("Unable to authenticate. Unreadable token header: %s", err)
            raise credentials_exception from err

        if not kid:
            log.warning("Unable to authenticate. Token header carries no kid.")
            raise credentials_exception

        try:
            key = jwks_cache.get_key(
                DISPATCH_AUTHENTICATION_PROVIDER_PKCE_JWKS,
                kid,
                ttl=DISPATCH_AUTHENTICATION_PROVIDER_PKCE_JWKS_CACHE_SECONDS,
                timeout=DISPATCH_AUTHENTICATION_PROVIDER_PKCE_JWKS_TIMEOUT_SECONDS,
            )
        except JwksUnavailableError as err:
            log.error("Unable to authenticate. %s", err)
            raise credentials_exception from err

        if key is None:
            log.warning("Unable to authenticate. No JWKS key matches the token's kid: %s", kid)
            raise credentials_exception

        # The key's own alg, else the asymmetric families a JWKS can serve.
        # Never HMAC -- that is what lets a token ask for the public key to be
        # verified against as a shared secret.
        algorithms = (
            [key["alg"]]
            if key.get("alg")
            else ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]
        )

        # require_exp: an id token without one never expires, and OpenID Connect
        # makes the claim mandatory, so its absence is a malformed token rather
        # than a token that opted out of expiry.
        jwt_opts = {
            "leeway": DISPATCH_AUTHENTICATION_PROVIDER_PKCE_LEEWAY_SECONDS,
            "require_exp": True,
        }
        if DISPATCH_JWT_AUDIENCE:
            # python-jose skips the audience check entirely when the token has
            # no aud, so expecting one has to be stated as a requirement too.
            jwt_opts["require_aud"] = True
        if DISPATCH_PKCE_DONT_VERIFY_AT_HASH:
            jwt_opts["verify_at_hash"] = False

        try:
            # audience/issuer are passed only when configured, but neither is
            # fail-open: python-jose rejects a token whose aud is present while
            # none is expected, and rejects a missing iss once one is expected.
            data = jwt.decode(
                token,
                key,
                algorithms=algorithms,
                audience=DISPATCH_JWT_AUDIENCE,
                issuer=DISPATCH_AUTHENTICATION_PROVIDER_PKCE_ISSUER,
                options=jwt_opts,
            )
        except (JWTError, JWKError) as err:
            log.warning("Unable to authenticate. Token rejected: %s", err)
            raise credentials_exception from err

        # Support overriding where email is returned in the id token.
        email_claim = DISPATCH_JWT_EMAIL_OVERRIDE or "email"
        email = data.get(email_claim)
        # isinstance, not just truthiness: this value becomes a user identity,
        # and a claim that is a list or an object would otherwise be carried all
        # the way to the pydantic model that provisions the account.
        if not isinstance(email, str) or not email:
            log.warning(
                "Unable to authenticate. Token carries no usable '%s' claim. Set "
                "DISPATCH_JWT_EMAIL_OVERRIDE to the claim your provider issues.",
                email_claim,
            )
            raise credentials_exception

        return email


class HeaderAuthProviderPlugin(AuthenticationProviderPlugin):
    title = "Dispatch Plugin - HTTP Header Authentication Provider"
    slug = "dispatch-auth-provider-header"
    description = "Authenticate users based on HTTP request header."
    version = dispatch_plugin.__version__

    author = "Filippo Giunchedi"
    author_url = "https://github.com/filippog"
    configuration_schema = None

    def get_current_user(self, request: Request, **kwargs):
        value: str = request.headers.get(DISPATCH_AUTHENTICATION_PROVIDER_HEADER_NAME)
        if not value:
            log.error(
                f"Unable to authenticate. Header {DISPATCH_AUTHENTICATION_PROVIDER_HEADER_NAME} not found."
            )
            raise HTTPException(status_code=HTTP_401_UNAUTHORIZED)
        return value


class AwsAlbAuthProviderPlugin(AuthenticationProviderPlugin):
    title = "Dispatch Plugin - AWS ALB Authentication Provider"
    slug = "dispatch-auth-provider-aws-alb"
    description = "AWS Application Load Balancer authentication provider."
    version = dispatch_plugin.__version__

    author = "ManyPets"
    author_url = "https://manypets.com/"
    configuration_schema = None

    @cached(
        cache=TTLCache(
            maxsize=1024, ttl=DISPATCH_AUTHENTICATION_PROVIDER_AWS_ALB_PUBLIC_KEY_CACHE_SECONDS
        )
    )
    def get_public_key(self, kid: str, region: str):
        log.debug("Cache miss. Requesting key from AWS endpoint.")
        url = f"https://public-keys.auth.elb.{region}.amazonaws.com/{kid}"
        req = requests.get(url)
        return req.text

    def get_current_user(self, request: Request, **kwargs):
        credentials_exception = HTTPException(
            status_code=HTTP_401_UNAUTHORIZED, detail=[{"msg": "Could not validate credentials"}]
        )

        encoded_jwt: str = request.headers.get("x-amzn-oidc-data")
        if not encoded_jwt:
            log.error("Unable to authenticate. Header x-amzn-oidc-data not found.")
            raise credentials_exception

        log.debug(f"Header x-amzn-oidc-data header received: {encoded_jwt}")

        # Validate the signer
        jwt_headers = encoded_jwt.split(".")[0]
        decoded_jwt_headers = base64.b64decode(jwt_headers)
        decoded_json = json.loads(decoded_jwt_headers)
        received_alb_arn = decoded_json["signer"]

        if received_alb_arn != DISPATCH_AUTHENTICATION_PROVIDER_AWS_ALB_ARN:
            log.error(
                f"Unable to authenticate. ALB ARN {received_alb_arn} does not match expected ARN {DISPATCH_AUTHENTICATION_PROVIDER_AWS_ALB_ARN}"
            )
            raise credentials_exception

        # Get the key id from JWT headers (the kid field)
        kid = decoded_json["kid"]

        # Get the region from the ARN
        region = DISPATCH_AUTHENTICATION_PROVIDER_AWS_ALB_ARN.split(":")[3]

        # Get the public key from regional endpoint
        log.debug(f"Getting public key for kid {kid} in region {region}.")
        pub_key = self.get_public_key(kid, region)

        # Get the payload
        log.debug(f"Decoding {encoded_jwt} with public key {pub_key}.")
        payload = jwt.decode(encoded_jwt, pub_key, algorithms=["ES256"])

        return payload[DISPATCH_AUTHENTICATION_PROVIDER_AWS_ALB_EMAIL_CLAIM]


class DispatchTicketPlugin(TicketPlugin):
    title = "Dispatch Plugin - Ticket Management"
    slug = "dispatch-ticket"
    description = "Uses Dispatch itself to create a ticket."
    version = dispatch_plugin.__version__

    author = "Netflix"
    author_url = "https://github.com/netflix/dispatch.git"

    def __init__(self):
        self.configuration_schema = DispatchTicketConfiguration

    def create(
        self,
        incident_id: int,
        title: str,
        commander_email: str,
        reporter_email: str,
        plugin_metadata: dict,
        db_session=None,
    ):
        """Creates a Dispatch incident ticket."""
        incident = incident_service.get(db_session=db_session, incident_id=incident_id)

        if self.configuration and self.configuration.use_incident_name:
            resource_id = create_resource_id(f"{incident.project.slug}-{title}-{incident.id}")
        else:
            resource_id = f"dispatch-{incident.project.organization.slug}-{incident.project.slug}-{incident.id}"

        return {
            "resource_id": resource_id,
            "weblink": f"{DISPATCH_UI_URL}/{incident.project.organization.name}/incidents/{resource_id}?project={incident.project.name}",
            "resource_type": "dispatch-internal-ticket",
        }

    def update(
        self,
        ticket_id: str,
        title: str,
        description: str,
        incident_type: str,
        incident_severity: str,
        incident_priority: str,
        status: str,
        commander_email: str,
        reporter_email: str,
        conversation_weblink: str,
        document_weblink: str,
        storage_weblink: str,
        conference_weblink: str,
        dispatch_weblink: str,
        cost: float,
        incident_type_plugin_metadata: dict = None,
    ):
        """Updates a Dispatch incident ticket."""
        return

    def delete(
        self,
        ticket_id: str,
    ):
        """Deletes a Dispatch ticket."""
        return

    def create_case_ticket(
        self,
        case_id: int,
        title: str,
        assignee_email: str,
        # reporter: str,
        case_type_plugin_metadata: dict,
        db_session=None,
    ):
        """Creates a Dispatch case ticket."""
        case = case_service.get(db_session=db_session, case_id=case_id)

        resource_id = f"dispatch-{case.project.organization.slug}-{case.project.slug}-{case.id}"

        return {
            "resource_id": resource_id,
            "weblink": f"{DISPATCH_UI_URL}/{case.project.organization.name}/cases/{resource_id}?project={case.project.name}",
            "resource_type": "dispatch-internal-ticket",
        }

    def update_metadata(
        self,
        ticket_id: str,
        metadata: dict,
    ):
        """Updates the metadata of a Dispatch ticket."""
        return

    def update_case_ticket(
        self,
        ticket_id: str,
        title: str,
        description: str,
        resolution: str,
        case_type: str,
        case_severity: str,
        case_priority: str,
        status: str,
        assignee_email: str,
        # reporter_email: str,
        document_weblink: str,
        storage_weblink: str,
        dispatch_weblink: str,
        case_type_plugin_metadata: dict = None,
    ):
        """Updates a Dispatch case ticket."""
        return

    def create_task_ticket(
        self,
        task_id: int,
        title: str,
        assignee_email: str,
        reporter_email: str,
        incident_ticket_key: str = None,
        task_plugin_metadata: dict = None,
        db_session=None,
    ):
        """Creates a Dispatch task ticket."""
        return {
            "resource_id": "",
            "weblink": "https://dispatch.example.com",
        }


class DispatchMfaPlugin(MultiFactorAuthenticationPlugin):
    title = "Dispatch Plugin - Multi Factor Authentication"
    slug = "dispatch-auth-mfa"
    description = "Uses dispatch itself to validate external requests."
    version = dispatch_plugin.__version__

    author = "Netflix"
    author_url = "https://github.com/netflix/dispatch.git"
    configuration_schema = None

    def wait_for_challenge(
        self,
        challenge_id: UUID,
        db_session: Session,
        timeout: int = 300,
    ) -> MfaChallengeStatus:
        """Waits for a multi-factor authentication challenge."""
        start_time = time.time()

        while time.time() - start_time < timeout:
            db_session.expire_all()
            challenge = db_session.query(MfaChallenge).filter_by(challenge_id=challenge_id).first()

            if not challenge:
                log.error(f"Challenge not found: {challenge_id}")
                raise Exception("Challenge not found.")

            if challenge.status == MfaChallengeStatus.APPROVED:
                return MfaChallengeStatus.APPROVED
            elif challenge.status == MfaChallengeStatus.DENIED:
                raise Exception("Challenge denied.")

            time.sleep(1)

        # Timeout reached
        log.warning(f"Timeout reached for challenge: {challenge_id}")

        # Update the challenge status to EXPIRED if it times out
        challenge = db_session.query(MfaChallenge).filter_by(challenge_id=challenge_id).first()
        if challenge:
            log.info(f"Updating challenge {challenge_id} status to EXPIRED")
            challenge.status = MfaChallengeStatus.EXPIRED
            db_session.commit()
        else:
            log.error(f"Challenge not found when trying to expire: {challenge_id}")

        return MfaChallengeStatus.EXPIRED

    def create_mfa_challenge(
        self,
        action: str,
        current_user: DispatchUser,
        db_session: Session,
        project_id: int,
    ) -> tuple[MfaChallenge, str]:
        """Creates a multi-factor authentication challenge."""
        project = project_service.get(db_session=db_session, project_id=project_id)

        challenge = MfaChallenge(
            action=action,
            dispatch_user_id=current_user.id,
            valid=True,
        )
        db_session.add(challenge)
        db_session.commit()

        org_slug = project.organization.slug if project.organization else "default"

        challenge_url = f"{DISPATCH_UI_URL}/{org_slug}/mfa?project_id={project_id}&challenge_id={challenge.challenge_id}&action={action}"
        return challenge, challenge_url

    def validate_mfa_token(
        self,
        payload: MfaPayload,
        current_user: DispatchUser,
        db_session: Session,
    ) -> Literal[MfaChallengeStatus.APPROVED]:
        """Validates a multi-factor authentication token."""
        challenge: MfaChallenge | None = (
            db_session.query(MfaChallenge)
            .filter_by(challenge_id=payload.challenge_id)
            .one_or_none()
        )

        if not challenge:
            raise InvalidChallengeError("Invalid challenge ID")
        if challenge.dispatch_user_id != current_user.id:
            raise UserMismatchError(
                f"Challenge does not belong to the current user: {current_user.email}"
            )
        if challenge.action != payload.action:
            raise ActionMismatchError("Action mismatch")
        if not challenge.valid:
            raise ExpiredChallengeError("Challenge is no longer valid")
        if challenge.status == MfaChallengeStatus.APPROVED:
            # Challenge has already been approved
            return challenge.status
        if challenge.status != MfaChallengeStatus.PENDING:
            raise InvalidChallengeStateError(f"Challenge is in invalid state: {challenge.status}")

        challenge.status = MfaChallengeStatus.APPROVED
        db_session.add(challenge)
        db_session.commit()

        return challenge.status

    def send_push_notification(self, items, **kwargs):
        # Implement this method if needed
        raise NotImplementedError

    def validate_mfa(self, items, **kwargs):
        # Implement this method if needed
        raise NotImplementedError


class DispatchContactPlugin(ContactPlugin):
    title = "Dispatch Plugin - Contact plugin"
    slug = "dispatch-contact"
    description = "Uses dispatch itself to fetch incident participants contact info."
    version = dispatch_plugin.__version__

    author = "Netflix"
    author_url = "https://github.com/netflix/dispatch.git"
    configuration_schema = None

    def get(self, email, db_session=None):
        individual = individual_service.get_by_email_and_project(
            db_session=db_session, email=email, project_id=self.project_id
        )
        if individual is None:
            return {"email": email, "fullname": email}

        data = individual.dict()
        data["fullname"] = data["name"]
        return data


class DispatchParticipantResolverPlugin(ParticipantPlugin):
    title = "Dispatch Plugin - Participant Resolver"
    slug = "dispatch-participant-resolver"
    description = "Uses dispatch itself to resolve incident participants."
    version = dispatch_plugin.__version__

    author = "Netflix"
    author_url = "https://github.com/netflix/dispatch.git"
    configuration_schema = None

    def get(
        self,
        project_id: int,
        class_instance: Base,
        db_session=None,
    ):
        """Fetches participants from Dispatch."""
        models = [
            (IndividualContact, IndividualContactRead),
            (Service, ServiceRead),
            (TeamContact, TeamContactRead),
        ]
        recommendation = route_service.get(
            db_session=db_session,
            project_id=project_id,
            class_instance=class_instance,
            models=models,
        )

        log.debug(f"Recommendation: {recommendation}")

        individual_contacts = []
        team_contacts = []
        for match in recommendation.matches:
            if match.resource_type == TeamContact.__name__:
                team = team_service.get_or_create(
                    db_session=db_session,
                    email=match.resource_state["email"],
                    project=class_instance.project,
                )
                team_contacts.append(team)

            if match.resource_type == IndividualContact.__name__:
                individual = individual_service.get_or_create(
                    db_session=db_session,
                    email=match.resource_state["email"],
                    project=class_instance.project,
                )

                individual_contacts.append((individual, None))

            # we need to do more work when we have a service
            if match.resource_type == Service.__name__:
                plugin_instance = plugin_service.get_active_instance_by_slug(
                    db_session=db_session,
                    slug=match.resource_state["type"],
                    project_id=project_id,
                )

                if plugin_instance:
                    if plugin_instance.enabled:
                        log.debug(
                            f"Resolving service contact. ServiceContact: {match.resource_state}"
                        )
                        # ensure that service is enabled
                        service = service_service.get_by_external_id_and_project_id(
                            db_session=db_session,
                            external_id=match.resource_state["external_id"],
                            project_id=project_id,
                        )
                        if service.is_active:
                            individual_email = plugin_instance.instance.get(
                                match.resource_state["external_id"]
                            )

                            individual = individual_service.get_or_create(
                                db_session=db_session,
                                email=individual_email,
                                project=class_instance.project,
                            )

                            individual_contacts.append((individual, match.resource_state["id"]))
                    else:
                        log.warning(
                            f"Skipping service contact. Service: {match.resource_state['name']} Reason: Associated service plugin not enabled."
                        )
                else:
                    log.warning(
                        f"Skipping service contact. Service: {match.resource_state['name']} Reason: Associated service plugin not found."
                    )

        db_session.commit()
        return individual_contacts, team_contacts

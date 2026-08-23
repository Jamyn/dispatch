"""Construction contract for the Duo Auth API client.

duo-client verifies against its own bundled CA file rather than the system
trust store, and 5.7.0 added a ``disable_ca_pinning`` constructor option that
swaps in the system store instead. These tests guard Dispatch's call site --
that it keeps taking duo-client's pinned default and never overrides the
trust material -- not the contents of the bundle, which is the library's.
"""

import duo_client
from duo_client.auth import Auth

from dispatch.plugins.dispatch_duo.config import DuoConfiguration
from dispatch.plugins.dispatch_duo.service import create_duo_auth_client

# Obviously fake, and never a real-looking credential.
INTEGRATION_KEY = "DITEST0000000000TEST"
INTEGRATION_SECRET_KEY = "test-integration-secret-key"
HOST = "api-test.duosecurity.com"


def _client() -> Auth:
    return create_duo_auth_client(
        DuoConfiguration(
            integration_key=INTEGRATION_KEY,
            integration_secret_key=INTEGRATION_SECRET_KEY,
            host=HOST,
        )
    )


def test_create_duo_auth_client_unwraps_secrets():
    client = _client()

    assert isinstance(client, Auth)
    assert client.host == HOST
    # SecretStr must be unwrapped, or the client signs with "**********".
    assert client.ikey == INTEGRATION_KEY
    assert client.skey == INTEGRATION_SECRET_KEY


def test_ca_certs_is_left_at_the_duo_default():
    """No custom path and no 'HTTP'/'DISABLE' sentinel, which drop TLS entirely."""
    assert _client().ca_certs == duo_client.client.DEFAULT_CA_CERTS


def test_ca_pinning_is_not_disabled():
    # Absent on releases before 5.7.0, where pinning cannot be turned off.
    assert getattr(_client(), "disable_ca_pinning", False) is False

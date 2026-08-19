"""Password handling and token minting on the user model.

DispatchUser.password is the credential the basic auth provider checks at
login, and DispatchUser.token is what it hands back. These are small functions
with an outsized blast radius, and nothing else in the suite exercises them.
"""

from datetime import datetime, timedelta

import pytest
from jose import jwt
from pydantic import ValidationError

from dispatch.auth.models import (
    AdminPasswordReset,
    DispatchUser,
    UserPasswordUpdate,
    UserRegister,
    generate_password,
    hash_password,
)
from dispatch.config import DISPATCH_JWT_ALG, DISPATCH_JWT_EXP, DISPATCH_JWT_SECRET


# --- Password storage -----------------------------------------------------


def test_a_password_is_stored_hashed_and_never_in_the_clear():
    """The stored value must not be recoverable from the database alone."""
    user = DispatchUser(email="ada@example.com")
    user.set_password("correct horse battery")

    assert user.password != b"correct horse battery"
    assert b"correct horse battery" not in user.password
    assert user.password.startswith(b"$2b$")


def test_verify_password_accepts_the_right_password_and_rejects_others():
    """The whole point of the hash: exact match, nothing near-miss."""
    user = DispatchUser(email="ada@example.com")
    user.set_password("correct horse battery")

    assert user.verify_password("correct horse battery")
    assert not user.verify_password("Correct horse battery")
    assert not user.verify_password("correct horse batter")
    assert not user.verify_password("")


def test_verify_password_is_false_when_the_account_has_no_password_set():
    """A user row with a null password must not authenticate anyone.

    Both operands are guarded because bcrypt.checkpw raises on a None hash --
    an exception here would surface as a 500 on the login route rather than a
    clean rejection.
    """
    user = DispatchUser(email="ada@example.com")

    assert not user.verify_password("anything")


def test_the_same_password_hashes_differently_for_two_users():
    """A per-user salt is what stops one cracked hash unlocking the rest."""
    a, b = DispatchUser(email="a@example.com"), DispatchUser(email="b@example.com")
    a.set_password("same password")
    b.set_password("same password")

    assert a.password != b.password
    assert a.verify_password("same password")
    assert b.verify_password("same password")


def test_setting_an_empty_password_is_refused():
    """An empty password would otherwise hash and then verify successfully."""
    user = DispatchUser(email="ada@example.com")

    with pytest.raises(ValueError):
        user.set_password("")


def test_an_auto_provisioned_account_cannot_be_logged_into():
    """auth.service auto-provisions users as UserRegister(email=...) alone.

    Pydantic v2 does not run a mode="before" validator against a field's
    default, so the password validator does NOT fire on that call and the
    account is stored with an empty credential rather than the random hashed
    one the validator intends. What must hold either way is that no password
    authenticates such an account -- verify_password guards both operands.
    """
    auto_provisioned = UserRegister(email="ada@example.com")

    user = DispatchUser(email="ada@example.com", password=bytes(auto_provisioned.password, "utf-8"))

    assert not user.verify_password("")
    assert not user.verify_password(auto_provisioned.password)
    assert not user.verify_password("guess")


def test_registering_with_an_explicit_password_field_hashes_it():
    """When the field is supplied the validator does run, even if it is empty."""
    generated = UserRegister(email="ada@example.com", password="")

    assert generated.password.startswith("$2b$")


def test_registering_with_a_password_stores_its_hash_not_the_password():
    """The value that reaches the model is already hashed, not the plaintext."""
    registered = UserRegister(email="ada@example.com", password="hunter2hunter2")

    assert registered.password != "hunter2hunter2"

    user = DispatchUser(email="ada@example.com", password=registered.password.encode())
    assert user.verify_password("hunter2hunter2")


def test_generated_passwords_meet_the_complexity_they_promise():
    """generate_password loops until its own rules pass; check they hold."""
    for _ in range(25):
        password = generate_password()
        assert len(password) == 10
        assert any(c.islower() for c in password)
        assert any(c.isupper() for c in password)
        assert sum(c.isdigit() for c in password) >= 3


def test_two_generated_passwords_differ():
    """A constant here would hand every auto-provisioned user the same key."""
    assert len({generate_password() for _ in range(20)}) > 1


# --- Password policy ------------------------------------------------------


@pytest.mark.parametrize(
    "password,reason",
    [
        ("Ab1cdef", "seven characters"),
        ("Abcdefgh", "no digit"),
        ("abcdefg1", "no uppercase"),
        ("ABCDEFG1", "no lowercase"),
        ("", "empty"),
    ],
)
@pytest.mark.parametrize("model", [UserPasswordUpdate, AdminPasswordReset])
def test_password_policy_rejects_weak_new_passwords(model, password, reason):
    """Length, digit and mixed case are enforced on both change paths.

    Parametrised over both models because the two carry independent copies of
    the same validator -- a fix applied to one has silently missed the other.
    """
    payload = {"new_password": password}
    if model is UserPasswordUpdate:
        payload["current_password"] = "whatever"

    with pytest.raises(ValidationError):
        model(**payload)


@pytest.mark.parametrize("model", [UserPasswordUpdate, AdminPasswordReset])
def test_password_policy_accepts_a_password_that_meets_every_rule(model):
    """The rules must leave a reasonable password usable."""
    payload = {"new_password": "Str0ngEnough"}
    if model is UserPasswordUpdate:
        payload["current_password"] = "whatever"

    assert model(**payload).new_password == "Str0ngEnough"


def test_changing_a_password_requires_the_current_one():
    """Without this an authenticated session could take over the account."""
    with pytest.raises(ValidationError):
        UserPasswordUpdate(current_password="", new_password="Str0ngEnough")


# --- Token minting --------------------------------------------------------


def test_the_minted_token_identifies_the_user_and_verifies_against_our_secret():
    """DispatchUser.token is the credential the API accepts; it must round-trip."""
    user = DispatchUser(email="ada@example.com", password=hash_password("x"))

    claims = jwt.decode(user.token, DISPATCH_JWT_SECRET, algorithms=[DISPATCH_JWT_ALG])

    assert claims["email"] == "ada@example.com"


def test_the_minted_token_expires_within_the_configured_window():
    """An exp far past DISPATCH_JWT_EXP would make revocation meaningless."""
    user = DispatchUser(email="ada@example.com", password=hash_password("x"))
    before = datetime.utcnow()

    claims = jwt.decode(user.token, DISPATCH_JWT_SECRET, algorithms=[DISPATCH_JWT_ALG])
    expires_at = datetime.utcfromtimestamp(claims["exp"])

    assert before < expires_at <= before + timedelta(seconds=DISPATCH_JWT_EXP + 5)


def test_a_token_does_not_carry_the_password_hash():
    """JWT payloads are readable by anyone holding the token."""
    user = DispatchUser(email="ada@example.com", password=hash_password("hunter2"))

    claims = jwt.decode(user.token, DISPATCH_JWT_SECRET, algorithms=[DISPATCH_JWT_ALG])

    assert set(claims) == {"email", "exp"}

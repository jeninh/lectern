"""Passkey (WebAuthn) registration and authentication on top of py_webauthn."""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from lectern.config import Settings
from lectern.models import Passkey, User

RP_NAME = "Lectern"
CHALLENGE_TTL = 300


@dataclass
class PendingChallenge:
    challenge: bytes
    user_id: int | None
    expires: float


class ChallengeStore:
    """Short-lived server-side challenge store keyed by an opaque id kept in a cookie."""

    def __init__(self) -> None:
        self._items: dict[str, PendingChallenge] = {}
        self._lock = threading.Lock()

    def put(self, challenge: bytes, user_id: int | None) -> str:
        key = secrets.token_urlsafe(24)
        with self._lock:
            self._prune()
            self._items[key] = PendingChallenge(
                challenge, user_id, time.monotonic() + CHALLENGE_TTL
            )
        return key

    def pop(self, key: str | None) -> PendingChallenge | None:
        if not key:
            return None
        with self._lock:
            item = self._items.pop(key, None)
        if item is None or item.expires < time.monotonic():
            return None
        return item

    def _prune(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self._items.items() if v.expires < now]:
            self._items.pop(k, None)


def registration_options(
    settings: Settings, user: User, existing: list[Passkey]
) -> tuple[str, bytes]:
    opts = generate_registration_options(
        rp_id=settings.rp_id,
        rp_name=RP_NAME,
        user_id=user.webauthn_user_handle,
        user_name=user.username,
        user_display_name=user.display_name or user.username,
        exclude_credentials=[PublicKeyCredentialDescriptor(id=p.credential_id) for p in existing],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    return options_to_json(opts), opts.challenge


def verify_registration(settings: Settings, body: dict, challenge: bytes):
    return verify_registration_response(
        credential=body,
        expected_challenge=challenge,
        expected_rp_id=settings.rp_id,
        expected_origin=settings.base_url,
        require_user_verification=False,
    )


def authentication_options(settings: Settings, allow: list[Passkey] | None) -> tuple[str, bytes]:
    opts = generate_authentication_options(
        rp_id=settings.rp_id,
        allow_credentials=[PublicKeyCredentialDescriptor(id=p.credential_id) for p in allow]
        if allow
        else None,
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return options_to_json(opts), opts.challenge


def verify_authentication(settings: Settings, body: dict, challenge: bytes, passkey: Passkey):
    return verify_authentication_response(
        credential=body,
        expected_challenge=challenge,
        expected_rp_id=settings.rp_id,
        expected_origin=settings.base_url,
        credential_public_key=passkey.public_key,
        credential_current_sign_count=passkey.sign_count,
        require_user_verification=False,
    )


def credential_id_from_body(body: dict) -> bytes | None:
    """Extract the raw credential id from a browser PublicKeyCredential JSON."""
    raw = body.get("rawId") or body.get("id")
    if not isinstance(raw, str):
        return None
    from webauthn.helpers import base64url_to_bytes

    try:
        return base64url_to_bytes(raw)
    except Exception:  # noqa: BLE001 - any malformed input is just "no credential"
        return None


def dumps(obj) -> str:
    return json.dumps(obj)

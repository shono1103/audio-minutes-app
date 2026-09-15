"""py_webauthn の薄い wrapper。RP ID・origin は設定から取り、検証器を自作しない。"""

from __future__ import annotations

import json
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from minutes_api.config import get_settings
from minutes_api.models import User, WebAuthnCredential


def registration_options(user: User, existing: list[WebAuthnCredential]) -> tuple[dict[str, Any], str]:
    settings = get_settings()
    options = generate_registration_options(
        rp_id=settings.rp_id,
        rp_name="audio-minutes",
        user_id=user.id.bytes,
        user_name=user.email,
        user_display_name=user.email,
        exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(cred.credential_id)) for cred in existing],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED, user_verification=UserVerificationRequirement.PREFERRED
        ),
    )
    return json.loads(options_to_json(options)), bytes_to_base64url(options.challenge)


def verify_registration(credential: dict[str, Any], challenge_b64: str) -> tuple[str, str, int, list[str]]:
    settings = get_settings()
    verification = verify_registration_response(
        credential=credential,
        expected_challenge=base64url_to_bytes(challenge_b64),
        expected_rp_id=settings.rp_id,
        expected_origin=settings.allowed_origins,
        require_user_verification=False,
    )
    transports = [str(item) for item in (credential.get("response", {}).get("transports") or [])]
    return (
        bytes_to_base64url(verification.credential_id),
        bytes_to_base64url(verification.credential_public_key),
        verification.sign_count,
        transports,
    )


def authentication_options(allow: list[WebAuthnCredential] | None = None) -> tuple[dict[str, Any], str]:
    settings = get_settings()
    options = generate_authentication_options(
        rp_id=settings.rp_id,
        allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(cred.credential_id)) for cred in (allow or [])],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    return json.loads(options_to_json(options)), bytes_to_base64url(options.challenge)


def verify_authentication(credential: dict[str, Any], challenge_b64: str, stored: WebAuthnCredential) -> int:
    settings = get_settings()
    verification = verify_authentication_response(
        credential=credential,
        expected_challenge=base64url_to_bytes(challenge_b64),
        expected_rp_id=settings.rp_id,
        expected_origin=settings.allowed_origins,
        credential_public_key=base64url_to_bytes(stored.public_key),
        credential_current_sign_count=stored.sign_count,
        require_user_verification=False,
    )
    return verification.new_sign_count

"""トークン生成・ハッシュ・パスワード方針。暗号は既存ライブラリだけを使う (ADR-0001)。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()  # argon2id 既定


def now() -> datetime:
    return datetime.now(UTC)


def after(seconds: int) -> datetime:
    return now() + timedelta(seconds=seconds)


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except Exception:
        return False


# 頻出パスワードの一部。実運用では大きな辞書へ差し替えられるよう別ファイル化してよい。
COMMON_PASSWORDS = {
    "password", "password1", "password123", "passwordpassword", "123456789012345", "qwertyuiopasdfg",
    "letmeinletmein1", "iloveyouiloveyou", "adminadminadmin", "welcome123456789", "changemechangeme",
    "1234567890123456", "abcdefghijklmnop", "qwertyqwertyqwerty", "passw0rdpassw0rd", "aaaaaaaaaaaaaaa",
    "audio-minutes-app", "audiominutes123", "minutesminutes1", "correcthorsebattery",
}


def password_problems(password: str, email: str | None = None) -> list[str]:
    problems: list[str] = []
    if len(password) < 15:
        problems.append("パスワードは 15 文字以上にしてください")
    if len(password) > 256:
        problems.append("パスワードが長すぎます")
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or re.fullmatch(r"(.)\1{9,}", lowered) or re.fullmatch(r"(?:0123456789|1234567890|abcdefghij)+.*", lowered):
        problems.append("よく使われるパスワードは利用できません")
    if email:
        local = email.split("@", 1)[0].lower()
        if len(local) >= 4 and local in lowered:
            problems.append("メールアドレスを含むパスワードは利用できません")
    return problems


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_recovery_codes(count: int = 8) -> list[str]:
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(5)
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes

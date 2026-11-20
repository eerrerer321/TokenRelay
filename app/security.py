"""密碼雜湊、token 產生。"""

import hashlib
import hmac
import secrets

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    """scrypt：刻意很慢的雜湊，讓資料庫外洩時也很難暴力破解密碼。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex), n=int(n), r=int(r), p=int(p), dklen=len(digest_hex) // 2
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


# 帳號不存在時也拿它跑一次比對，讓「帳號存在」和「不存在」的回應時間差不多，避免被用時間差猜帳號。
DUMMY_PASSWORD_HASH = hash_password("dummy-password-for-timing")


def sha256_hex(value: str) -> str:
    """API key / session token 本身是高強度亂數，用快速雜湊就夠（不需要 scrypt）。"""
    return hashlib.sha256(value.encode()).hexdigest()


def new_api_key() -> str:
    return "sk-" + secrets.token_urlsafe(32)


def new_session_token() -> str:
    return "sess-" + secrets.token_urlsafe(32)

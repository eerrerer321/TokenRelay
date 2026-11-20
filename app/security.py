"""密碼雜湊、token 產生、登入失敗次數限制。"""

import hashlib
import hmac
import math
import secrets
import time
from collections import deque

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1

# 網頁（使用者網頁與後台）的安全標頭。只加在網頁檔案上：/docs 會用到 inline script，套上去會壞掉
WEB_SECURITY_HEADERS = {
    # CSP：瀏覽器只准執行這些來源的程式。就算有人成功把 <script> 塞進頁面，也不會被執行
    "Content-Security-Policy": (
        "default-src 'none'; "
        "script-src 'self' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src 'self' https://cdn.jsdelivr.net; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",  # 不讓瀏覽器猜檔案類型（避免把上傳的文字當程式執行）
    "X-Frame-Options": "DENY",  # 不准被別的網站用 iframe 嵌入（防點擊劫持）
    "Referrer-Policy": "no-referrer",
}


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


class LoginGuard:
    """登入、註冊失敗太多次就暫時擋住，防止一直猜密碼或邀請碼。

    以「IP」和「email」分別計數：同一個 key 在 window 秒內失敗 max_failures 次，就擋到最早那幾次失敗過期為止。
    以 email 計數可以擋「換很多 IP 猜同一個帳號」；代價是別人故意打錯，也能讓這個帳號暫時登不進去。
    存在記憶體裡，只適用單一程序（同攻防清單 C），重啟伺服器就歸零。
    """

    PRUNE_AT = 10_000  # 記錄的 key 超過這個數量時，順手清掉已過期的

    def __init__(self, max_failures: int, window_seconds: float, clock=time.monotonic):
        self.max_failures, self.window, self.clock = max_failures, window_seconds, clock
        self._failures: dict[str, deque[float]] = {}

    def _recent(self, key: str) -> deque[float] | None:
        """只留 window 內的失敗紀錄；全部過期就整個刪掉。"""
        failures = self._failures.get(key)
        if failures is None:
            return None
        cutoff = self.clock() - self.window
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            del self._failures[key]
            return None
        return failures

    def retry_after(self, *keys: str) -> int:
        """還要等幾秒才能再試；0 = 現在可以試。"""
        wait = 0.0
        for key in keys:
            failures = self._recent(key)
            if failures is not None and len(failures) >= self.max_failures:
                # 失敗次數要降到上限以下才放行：等倒數第 max_failures 次失敗過期
                wait = max(wait, failures[-self.max_failures] + self.window - self.clock())
        return math.ceil(wait)

    def record_failure(self, *keys: str) -> None:
        if len(self._failures) >= self.PRUNE_AT:
            for key in list(self._failures):
                self._recent(key)
        now = self.clock()
        for key in keys:
            self._failures.setdefault(key, deque()).append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)

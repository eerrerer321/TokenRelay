"""設定：全部集中在專案根目錄的 config.ini（範本是 config.example.ini）。

格式是 INI：[區段] 底下寫「名稱 = 值」，# 開頭的整行是註解。值留空 = 用預設值。
名稱或區段打錯字會在啟動時直接報錯，不會被默默忽略（例如把 receive_address 打成 recieve_address）。
上游與模型只存在這個檔案裡，不進資料庫：改完要重啟伺服器才生效。
"""

import configparser
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator

from app.billing import usd_to_micro

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
CONFIG_PATH = Path("config.ini")


class ConfigError(Exception):
    """設定檔有問題。訊息會指出是哪個區段的哪個名稱。"""


class TokenConfig(BaseModel):
    """[token:USDT] 這類區段：收哪個代幣。"""

    model_config = ConfigDict(frozen=True)

    symbol: str
    contract: str  # 合約地址，存成小寫
    decimals: int = Field(default=18, ge=0, le=36)  # BSC 上的 USDT/USDC 是 18

    @field_validator("contract")
    @classmethod
    def _check_contract(cls, value: str) -> str:
        if not ADDRESS_RE.match(value):
            raise ValueError("合約地址格式錯誤（應該是 0x 開頭、後面接 40 個 0-9 或 a-f）")
        return value.lower()


HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")  # HTTP 標頭名稱允許的字元
HEADER_VALUE_RE = re.compile(r"^[\x20-\x7e]*$")  # 只收可見的 ASCII：中文或換行要等到送出時才失敗，那時已經預扣了
# 站方自己會設定的標頭：驗證、內容格式、連線控制。讓設定檔蓋掉的話，上游 key 可能送錯地方或請求格式壞掉
RESERVED_HEADERS = frozenset({"authorization", "x-api-key", "anthropic-version", "content-type", "content-length",
                              "host", "transfer-encoding", "connection"})


class UpstreamConfig(BaseModel):
    """[upstream:名稱] 區段：一個 OpenAI 相容的上游。"""

    name: str
    base_url: str  # 例：https://api.deepseek.com/v1
    api_key: str = ""  # 不需要 key 的本機上游可以留空
    responses: bool = False  # 上游有 OpenAI 的 /responses（Codex CLI 用）才開
    # 上游的 Anthropic 格式入口（Claude Code 用的 /v1/messages），例如 https://api.deepseek.com/anthropic；留空 = 不支援
    anthropic_base_url: str = ""
    # Anthropic 入口怎麼帶 key：x-api-key（Anthropic 官方）或 bearer（Authorization: Bearer，例如 opencode Go）
    anthropic_auth: Literal["x-api-key", "bearer"] = "x-api-key"
    # 每個請求固定附加的標頭，設定檔裡寫成一行 JSON，例如 {"User-Agent": "my-relay/1.0"}
    headers: dict[str, str] = {}
    # 上游要「對話 ID」時填標頭名稱（例如 opencode Go 的 x-opencode-session）；留空 = 不送
    session_header: str = ""

    @field_validator("headers", mode="before")
    @classmethod
    def _parse_headers(cls, value):
        if not isinstance(value, str):
            return value
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError('要寫成一行 JSON，例如 {"User-Agent": "my-relay/1.0"}') from None
        if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
            raise ValueError('要寫成一行 JSON，名稱和值都是字串，例如 {"User-Agent": "my-relay/1.0"}')
        return value

    @field_validator("headers")
    @classmethod
    def _check_headers(cls, value: dict[str, str]) -> dict[str, str]:
        for name, header_value in value.items():
            if not HEADER_NAME_RE.match(name):
                raise ValueError(f"標頭名稱格式錯誤：{name!r}")
            if name.lower() in RESERVED_HEADERS:
                raise ValueError(f"{name} 由站方自己設定，不能在這裡改")
            if not HEADER_VALUE_RE.match(header_value):
                raise ValueError(f"標頭 {name} 的值只能是英數字和一般符號（不能有中文或換行）")
        return value

    @field_validator("session_header")
    @classmethod
    def _check_session_header(cls, value: str) -> str:
        if value and not HEADER_NAME_RE.match(value):
            raise ValueError(f"標頭名稱格式錯誤：{value!r}")
        if value.lower() in RESERVED_HEADERS:
            raise ValueError(f"{value} 由站方自己設定，不能當對話 ID 的標頭")
        return value

    @model_validator(mode="after")
    def _check_session_not_in_headers(self) -> "UpstreamConfig":
        if self.session_header and self.session_header.lower() in {name.lower() for name in self.headers}:
            raise ValueError(f"headers 裡不能再寫 {self.session_header}（它是 session_header，每個請求會自動產生）")
        return self

    @field_validator("base_url", "anthropic_base_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        if value == "":
            return value  # anthropic_base_url 可以留空；base_url 留空會被「必填」擋下
        if not value.startswith(("http://", "https://")):
            raise ValueError("必須以 http:// 或 https:// 開頭")
        try:
            # 例如 port 寫成文字：要在啟動時擋下。等到有人呼叫才失敗的話，預扣已經扣下去了
            httpx.URL(value)
        except httpx.InvalidURL as exc:
            raise ValueError(f"網址格式錯誤：{exc}") from None
        return value


class ModelConfig(BaseModel):
    """[model:名稱] 區段：對外開放的模型與售價。沒列在設定檔裡的模型一律拒絕。"""

    name: str  # 使用者在工具裡選的模型名稱
    upstream: str  # 對應哪個 [upstream:名稱]
    upstream_model: str  # 上游實際的模型名稱
    # chat：對話（chat/completions、responses、messages）；embedding：文字轉向量；image：產生圖片；speech：文字轉語音
    kind: Literal["chat", "embedding", "image", "speech"] = "chat"
    # 每 1M 輸入 token 幾 micro-USD（speech 是每 1M 個字元）。chat、embedding、speech 必填
    input_price_micro: int | None = Field(default=None, ge=0)
    output_price_micro: int | None = Field(default=None, ge=0)  # 每 1M 輸出 token 幾 micro-USD。chat 必填
    image_price_micro: int | None = Field(default=None, ge=0)  # 每張圖幾 micro-USD。image 必填
    # 上游回報「快取命中」的輸入 token 改用這個價格；None = 不打折，照輸入價收
    cached_input_price_micro: int | None = Field(default=None, ge=0)
    # 上游的進價：只給後台算利潤、按倍數訂價用，不影響收費
    cost_input_price_micro: int | None = Field(default=None, ge=0)
    cost_output_price_micro: int | None = Field(default=None, ge=0)
    cost_cached_input_price_micro: int | None = Field(default=None, ge=0)
    enabled: bool = True  # false = 暫時下架：使用者看不到、也呼叫不到
    max_output_tokens: int = Field(default=4096, gt=0)  # 必須是正數：負數會讓預扣變成「先送額度」
    max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    # 一張圖片在這個模型「最多」算幾個輸入 token；0 = 不接受圖片。各家算法差很多，要查上游文件填
    image_tokens: int = Field(default=0, ge=0)

    @field_validator("cached_input_price_micro")
    @classmethod
    def _check_cached_price(cls, value: int | None, info: ValidationInfo) -> int | None:
        # 預扣按一般輸入價估「最壞情況」；快取價比較貴的話，預扣就不再是上限
        input_price = info.data.get("input_price_micro")
        if value is not None and input_price is not None and value > input_price:
            raise ValueError("不可高於 input_price")
        return value

    @model_validator(mode="after")
    def _check_required_prices(self) -> "ModelConfig":
        required = {"chat": ("input_price", "output_price"), "embedding": ("input_price",),
                    "speech": ("input_price",), "image": ("image_price",)}[self.kind]
        for key in required:
            if getattr(self, f"{key}_micro") is None:
                # 少填價格就等於免費：一定要擋下來
                raise ValueError(f"{key}：必填" + ("" if self.kind == "chat" else f"（kind = {self.kind}）"))
        return self


class Settings(BaseModel):
    database_url: str = "sqlite+aiosqlite:///./dev.db"
    invite_code: str = ""  # 註冊用邀請碼；留空 = 關閉註冊
    public_url: str = ""  # 網站對外的網址，例如 https://relay.example.com（後台產生重設密碼連結用）
    session_ttl_hours: int = 24 * 7
    max_body_bytes: int = 1_000_000  # 單一請求內容上限，也間接限制了預扣金額
    max_concurrent_per_user: int = 4
    max_keys_per_user: int = 20
    upstream_timeout_seconds: float = 300
    # 同一個 email 或 IP，在 login_lock_minutes 分鐘內登入（或註冊）失敗幾次就暫停嘗試
    login_max_failures: int = Field(default=5, gt=0)
    login_lock_minutes: int = Field(default=15, gt=0)

    # ---- 正式啟動（python -m app.serve）----
    server_host: str = "0.0.0.0"  # 對外 HTTPS 綁在哪個網路介面；0.0.0.0 = 所有
    server_https_port: int = Field(default=443, ge=0, le=65535)  # 0 = 不開對外入口
    server_ssl_certfile: str = ""  # win-acme 產生的 xxx-chain.pem（憑證 + 中繼憑證）
    server_ssl_keyfile: str = ""  # win-acme 產生的 xxx-key.pem（私鑰，不要外流）
    server_local_port: int = Field(default=8000, gt=0, le=65535)  # 本機後台入口，只綁 127.0.0.1

    # ---- 鏈上儲值（方案 B：單一收款地址 + 金額尾數）----
    chain_name: str = "bsc-testnet"
    chain_rpc_url: str = ""
    chain_confirmations: int = 15
    chain_poll_seconds: float = 10
    chain_max_block_range: int = 500
    chain_start_block: int | None = None  # 第一次啟動從哪個區塊開始掃；留空 = 從當下開始
    receive_address: str = ""  # 你自己錢包的收款地址（只需要地址，不需要私鑰）
    tokens: dict[str, TokenConfig] = {}
    deposit_ttl_minutes: int = 30
    deposit_grace_minutes: int = 30  # 訂單過期後再多等多久才真的作廢（給晚到的轉帳）
    deposit_min_usd: int = 1
    deposit_max_usd: int = 1000
    max_pending_deposits: int = 3

    # ---- 上游與模型 ----
    upstreams: dict[str, UpstreamConfig] = {}
    models: dict[str, ModelConfig] = {}

    @model_validator(mode="after")
    def _check_model_upstreams(self) -> "Settings":
        for model in self.models.values():
            if model.upstream not in self.upstreams:
                raise ValueError(f"模型 {model.name} 指定的上游 {model.upstream} 不存在"
                                 f"（要有一個 [upstream:{model.upstream}] 區段）")
        return self

    @property
    def chain_enabled(self) -> bool:
        return bool(self.chain_rpc_url and self.receive_address and self.tokens)

    def token_configs(self) -> dict[str, TokenConfig]:
        return self.tokens

    def open_models(self) -> dict[str, ModelConfig]:
        """開放給使用者的模型（enabled = true）。"""
        return {name: m for name, m in self.models.items() if m.enabled}


# ---------------------------------------------------------------- 讀取 config.ini

# [區段] 裡的名稱 → Settings 的欄位
_SECTION_FIELDS = {
    "site": {
        "database_url": "database_url",
        "invite_code": "invite_code",
        "public_url": "public_url",
        "session_ttl_hours": "session_ttl_hours",
        "max_body_bytes": "max_body_bytes",
        "max_concurrent_per_user": "max_concurrent_per_user",
        "max_keys_per_user": "max_keys_per_user",
        "upstream_timeout_seconds": "upstream_timeout_seconds",
        "login_max_failures": "login_max_failures",
        "login_lock_minutes": "login_lock_minutes",
    },
    "server": {
        "host": "server_host",
        "https_port": "server_https_port",
        "ssl_certfile": "server_ssl_certfile",
        "ssl_keyfile": "server_ssl_keyfile",
        "local_port": "server_local_port",
    },
    "chain": {
        "name": "chain_name",
        "rpc_url": "chain_rpc_url",
        "receive_address": "receive_address",
        "confirmations": "chain_confirmations",
        "poll_seconds": "chain_poll_seconds",
        "max_block_range": "chain_max_block_range",
        "start_block": "chain_start_block",
    },
    "deposit": {
        "ttl_minutes": "deposit_ttl_minutes",
        "grace_minutes": "deposit_grace_minutes",
        "min_usd": "deposit_min_usd",
        "max_usd": "deposit_max_usd",
        "max_pending": "max_pending_deposits",
    },
}
_FIELD_TO_KEY = {field: (section, key) for section, keys in _SECTION_FIELDS.items() for key, field in keys.items()}
_TOKEN_KEYS = {"contract", "decimals"}
_UPSTREAM_KEYS = {"base_url", "api_key", "responses", "anthropic_base_url", "anthropic_auth", "headers",
                  "session_header"}
_PRICE_KEYS = ("input_price", "output_price", "cached_input_price", "image_price",
               "cost_input_price", "cost_output_price", "cost_cached_input_price")
_MODEL_KEYS = {"upstream", "upstream_model", *_PRICE_KEYS, "max_output_tokens", "max_tokens_field", "image_tokens",
               "enabled", "kind"}
_MODEL_KEY_NAMES = {f"{key}_micro": key for key in _PRICE_KEYS}


def _error_text(error: dict) -> str:
    """把 pydantic 的英文錯誤換成中文。"""
    kind, ctx = error["type"], error.get("ctx", {})
    if kind == "missing":
        return "必填"
    if kind == "value_error":
        return str(ctx.get("error", error["msg"]))
    if kind in ("int_parsing", "int_from_float"):
        return "必須是整數"
    if kind == "float_parsing":
        return "必須是數字"
    if kind == "greater_than":
        return f"必須大於 {ctx['gt']}"
    if kind == "greater_than_equal":
        return f"不可小於 {ctx['ge']}"
    if kind == "less_than_equal":
        return f"不可大於 {ctx['le']}"
    if kind == "bool_parsing":
        return "只能是 true 或 false"
    if kind == "literal_error":
        return f"只能是 {ctx['expected']}"
    return error["msg"]


def _read_section(parser: configparser.ConfigParser, section: str, allowed, source: str) -> dict[str, str]:
    values = {}
    for key, value in parser[section].items():
        if key not in allowed:
            raise ConfigError(f"{source} [{section}] 不認得的名稱：{key}（是不是打錯字？）")
        value = value.strip()
        if value:  # 留空 = 用預設值
            values[key] = value
    return values


def _build(cls, section: str, source: str, key_names: dict | None = None, **values):
    try:
        return cls(**values)
    except ValidationError as exc:
        error = exc.errors()[0]
        field = str(error["loc"][0]) if error["loc"] else ""
        key = (key_names or {}).get(field, field)
        where = f"{key}：" if key else ""  # 整體檢查（例如必填價格）的錯誤訊息自己會帶名稱
        raise ConfigError(f"{source} [{section}] {where}{_error_text(error)}") from None


def parse_config(parser: configparser.ConfigParser, source: str = "config.ini") -> Settings:
    if parser.defaults():
        raise ConfigError(f"{source} 不支援 [DEFAULT] 區段")
    fields: dict = {}
    tokens: dict[str, TokenConfig] = {}
    upstreams: dict[str, UpstreamConfig] = {}
    models: dict[str, ModelConfig] = {}

    for section in parser.sections():
        kind, colon, name = (part.strip() for part in section.partition(":"))
        if not colon and kind in _SECTION_FIELDS:
            for key, value in _read_section(parser, section, _SECTION_FIELDS[kind], source).items():
                fields[_SECTION_FIELDS[kind][key]] = value
        elif colon and name and kind == "token":
            values = _read_section(parser, section, _TOKEN_KEYS, source)
            tokens[name.upper()] = _build(TokenConfig, section, source, symbol=name.upper(), **values)
        elif colon and name and kind == "upstream":
            values = _read_section(parser, section, _UPSTREAM_KEYS, source)
            upstreams[name] = _build(UpstreamConfig, section, source, name=name, **values)
        elif colon and name and kind == "model":
            values = _read_section(parser, section, _MODEL_KEYS, source)
            for key in _PRICE_KEYS:
                if key in values:
                    try:
                        values[f"{key}_micro"] = usd_to_micro(values.pop(key))
                    except ValueError as exc:
                        raise ConfigError(f"{source} [{section}] {key}：{exc}") from None
            values.setdefault("upstream_model", name)
            models[name] = _build(ModelConfig, section, source, _MODEL_KEY_NAMES, name=name, **values)
        else:
            raise ConfigError(f"{source} 不認得的區段：[{section}]")

    try:
        return Settings(**fields, tokens=tokens, upstreams=upstreams, models=models)
    except ValidationError as exc:
        error = exc.errors()[0]
        field = str(error["loc"][0]) if error["loc"] else ""
        if field in _FIELD_TO_KEY:
            section, key = _FIELD_TO_KEY[field]
            raise ConfigError(f"{source} [{section}] {key}：{_error_text(error)}") from None
        raise ConfigError(f"{source}：{_error_text(error)}") from None


def load_settings(path: Path = CONFIG_PATH) -> Settings:
    if not path.exists():
        raise ConfigError(f"找不到設定檔 {path}：請把 config.example.ini 複製成 config.ini 再填值")
    try:
        text = path.read_text(encoding="utf-8-sig")  # utf-8-sig：Windows 記事本存檔時加的 BOM 也能讀
    except UnicodeDecodeError:
        raise ConfigError(f"{path} 不是 UTF-8 編碼：請用 UTF-8 重新存檔") from None
    parser = configparser.ConfigParser(interpolation=None)  # 不把 % 當特殊符號：上游 key 裡可能有 %
    try:
        parser.read_string(text, source=str(path))
    except configparser.Error as exc:
        raise ConfigError(f"{path} 格式錯誤：{exc}") from None
    return parse_config(parser, str(path))


@lru_cache
def get_settings() -> Settings:
    return load_settings()

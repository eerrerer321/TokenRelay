"""設定檔 config.ini：範本本身要能直接讀；打錯字、填錯值要在啟動時就報錯，並指出是哪個區段的哪個名稱。"""

from pathlib import Path

import pytest

from app.config import ConfigError, load_settings

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.ini"
UPSTREAM = "[upstream:up]\nbase_url = https://api.example.com/v1\napi_key = sk-test\n"
MODEL = "[model:m]\nupstream = up\ninput_price = 0.3\noutput_price = 1.2\n"
VALID = UPSTREAM + MODEL  # 最後一個區段是 [model:m]：後面直接接的設定會算在它底下


def write(tmp_path: Path, text: str, encoding: str = "utf-8") -> Path:
    path = tmp_path / "config.ini"
    path.write_text(text, encoding=encoding)
    return path


def test_example_config_loads():
    """範本一定要能直接讀。收款地址還沒填，所以儲值不會啟用。"""
    settings = load_settings(EXAMPLE)
    model = settings.models["deepseek-chat"]
    assert (model.upstream, model.input_price_micro, model.output_price_micro) == ("deepseek", 300_000, 1_200_000)
    assert settings.upstreams["deepseek"].base_url == "https://api.deepseek.com/v1"
    assert settings.chain_rpc_url.startswith("https://") and set(settings.tokens) == {"USDT", "USDC"}
    assert not settings.chain_enabled


def test_values_are_mapped_and_blank_means_default(tmp_path):
    settings = load_settings(write(tmp_path, "[site]\ninvite_code = abc\n" + VALID + (
        "[chain]\nrpc_url = https://rpc.example\nreceive_address = 0x" + "ab" * 20 + "\nstart_block =\n"
        "[token:usdt]\ncontract = 0x" + "01" * 20 + "\n"
        "[deposit]\nmax_pending = 5\n"
    )))
    assert settings.invite_code == "abc"
    assert settings.models["m"].upstream_model == "m"  # 沒填 → 跟區段名稱一樣
    assert settings.models["m"].max_output_tokens == 4096  # 沒填 → 預設值
    assert settings.chain_start_block is None  # 留空 → 預設值
    assert settings.max_pending_deposits == 5
    assert settings.tokens["USDT"].decimals == 18 and settings.tokens["USDT"].contract == "0x" + "01" * 20
    assert settings.chain_enabled


def test_utf8_bom_is_accepted(tmp_path):
    """Windows 記事本存 UTF-8 時，可能在檔案開頭加 BOM。"""
    assert load_settings(write(tmp_path, "[site]\ninvite_code = abc\n" + VALID, "utf-8-sig")).invite_code == "abc"


@pytest.mark.parametrize("text, expected", [
    # 打錯字：不能默默忽略（不然收款地址打錯字，儲值就悄悄關掉了）
    ("[site]\ninvte_code = x\n" + VALID, "[site] 不認得的名稱：invte_code"),
    ("[chian]\nrpc_url = x\n" + VALID, "不認得的區段：[chian]"),
    ("[DEFAULT]\nx = 1\n" + VALID, "不支援 [DEFAULT]"),
    ("[site]\ninvite_code = a\ninvite_code = b\n" + VALID, "格式錯誤"),
    # 模型指到不存在的上游
    (UPSTREAM + MODEL.replace("upstream = up", "upstream = nope"), "上游 nope 不存在"),
    # 上游網址：在啟動時擋下，不要等到有人呼叫、預扣已經扣下去才失敗
    (UPSTREAM.replace("https://api.example.com/v1", "http://api.example.com:abc/v1") + MODEL,
     "[upstream:up] base_url：網址格式錯誤"),
    (UPSTREAM.replace("https://", "ftp://") + MODEL, "[upstream:up] base_url：必須以 http:// 或 https:// 開頭"),
    # 輸出上限是預扣的基礎：0 或負數會讓預扣失真（負數 = 先送額度）
    (VALID + "max_output_tokens = 0\n", "[model:m] max_output_tokens：必須大於 0"),
    (VALID + "max_output_tokens = -5\n", "[model:m] max_output_tokens：必須大於 0"),
    (VALID + "image_tokens = -1\n", "[model:m] image_tokens：不可小於 0"),
    (VALID + "max_tokens_field = max_token\n", "[model:m] max_tokens_field：只能是"),
    # 價格
    (UPSTREAM + MODEL.replace("input_price = 0.3", "input_price = -1"), "[model:m] input_price：不可小於 0"),
    (UPSTREAM + MODEL.replace("input_price = 0.3", "input_price = 0.0000001"), "[model:m] input_price：最多 6 位小數"),
    (UPSTREAM + MODEL.replace("input_price = 0.3", "input_price = Infinity"), "[model:m] input_price：不是數字"),
    (UPSTREAM + MODEL.replace("input_price = 0.3", "input_price = abc"), "[model:m] input_price：不是數字"),
    (UPSTREAM + MODEL.replace("output_price = 1.2\n", ""), "[model:m] output_price：必填"),
    # 其他型別
    ("[site]\nmax_keys_per_user = many\n" + VALID, "[site] max_keys_per_user：必須是整數"),
    (VALID + "[token:USDT]\ncontract = 0x123\n", "[token:USDT] contract：合約地址格式錯誤"),
], ids=["typo-key", "typo-section", "default-section", "duplicate-key", "unknown-upstream", "bad-port",
        "bad-scheme", "zero-output", "negative-output", "negative-image", "bad-field-choice", "negative-price",
        "too-precise-price", "infinite-price", "text-price", "missing-price", "text-int", "bad-contract"])
def test_mistakes_are_reported_with_location(tmp_path, text, expected):
    with pytest.raises(ConfigError) as exc:
        load_settings(write(tmp_path, text))
    assert expected in str(exc.value)


def test_missing_file_says_how_to_create_it(tmp_path):
    with pytest.raises(ConfigError, match="config.example.ini"):
        load_settings(tmp_path / "config.ini")


def test_non_utf8_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="UTF-8"):
        load_settings(write(tmp_path, "[site]\ninvite_code = 中文\n", "big5"))

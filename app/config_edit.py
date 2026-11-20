"""後台改設定用：只改 config.ini 裡指定的那幾行，其他內容（包含註解）原樣保留。

流程：讀檔 → 在文字上改 → 用跟啟動時相同的規則檢查整份設定 → 沒問題才寫回（先寫暫存檔再取代，
寫到一半當機也不會留下半份檔案）→ 上一版存成 config.ini.bak。
"""

import configparser
import os
import re
from pathlib import Path

from app.config import ConfigError, Settings, parse_config

SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]*)\]\s*$")
KEY_RE = re.compile(r"^(?P<key>[^#;\s=:][^=:]*?)\s*[=:]")
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,99}$")


def _normalize(section: str) -> str:
    kind, colon, name = (part.strip() for part in section.partition(":"))
    return f"{kind}{colon}{name}"


def _sections(lines: list[str]) -> dict[str, tuple[int, int]]:
    """區段名稱 → (標題那一行, 最後一個「名稱 = 值」那一行)。區段結尾的註解通常屬於下一個區段，不算進來。"""
    result, current, last = {}, None, 0
    for i, line in enumerate(lines):
        match = SECTION_RE.match(line)
        if match:
            if current is not None:
                result[current] = (result[current][0], last)
            current, last = _normalize(match["name"]), i
            result[current] = (i, i)
        elif current is not None and KEY_RE.match(line):
            last = i
    if current is not None:
        result[current] = (result[current][0], last)
    return result


def _clean(value: str) -> str:
    # 換行會變成新的一行設定，等於可以偷塞任意區段進設定檔，一律拒絕
    if "\n" in value or "\r" in value:
        raise ConfigError("值不能包含換行")
    return value.strip()


def set_values(text: str, section: str, values: dict[str, str]) -> str:
    """改某個區段的幾個名稱；原本沒有這一行就加在區段最後。值給空字串 = 用預設值。"""
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    ranges = _sections(lines)
    if _normalize(section) not in ranges:
        raise ConfigError(f"找不到區段 [{section}]")
    start, end = ranges[_normalize(section)]
    for key, value in values.items():
        new_line = f"{key} = {_clean(value)}".rstrip() + newline
        for i in range(start + 1, end + 1):
            match = KEY_RE.match(lines[i])
            if match and match["key"].strip().lower() == key:
                lines[i] = new_line
                break
        else:
            if not lines[end].endswith(("\n", "\r")):
                lines[end] += newline
            end += 1
            lines.insert(end, new_line)
    return "".join(lines)


def add_section(text: str, section: str, values: dict[str, str], *, after_kind: str = "model") -> str:
    """新增區段，放在最後一個同類區段（例如最後一個 [model:...]）後面；沒有同類區段就加在檔案最後。"""
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    ranges = _sections(lines)
    if _normalize(section) in ranges:
        raise ConfigError(f"區段 [{section}] 已經存在")
    same_kind = [end for name, (_, end) in ranges.items() if name.startswith(after_kind + ":")]
    at = max(same_kind) + 1 if same_kind else len(lines)
    if at > 0 and not lines[at - 1].endswith(("\n", "\r")):
        lines[at - 1] += newline
    block = [newline, f"[{section}]{newline}"]
    block += [f"{key} = {_clean(value)}".rstrip() + newline for key, value in values.items()]
    lines[at:at] = block
    return "".join(lines)


def remove_section(text: str, section: str) -> str:
    lines = text.splitlines(keepends=True)
    ranges = _sections(lines)
    if _normalize(section) not in ranges:
        raise ConfigError(f"找不到區段 [{section}]")
    start, end = ranges[_normalize(section)]
    if start > 0 and not lines[start - 1].strip():
        start -= 1  # 連同前面那行空白一起拿掉
    del lines[start:end + 1]
    return "".join(lines)


def check_text(text: str, source: str) -> Settings:
    """跟啟動時一樣的檢查。有錯就丟 ConfigError，訊息會指出哪個區段的哪個名稱。"""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text, source=source)
    except configparser.Error as exc:
        raise ConfigError(f"格式錯誤：{exc}") from None
    return parse_config(parser, source)


def read_config(path: Path) -> str:
    # 不用 read_text：它會把 Windows 的換行 \r\n 變成 \n，寫回去時整份檔案的換行都會被改掉
    return path.read_bytes().decode("utf-8-sig")


def write_config(path: Path, text: str) -> Settings:
    """檢查通過才寫回；回傳新的設定。"""
    settings = check_text(text, str(path))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    backup = path.with_name(path.name + ".bak")
    backup.write_bytes(path.read_bytes())
    os.replace(tmp, path)
    return settings

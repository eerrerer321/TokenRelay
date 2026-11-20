"""站長用的命令列工具（刻意不做管理網頁：沒有管理 API，就沒有管理 API 可以被攻擊）。

上游與模型價格寫在 config.ini，不用指令新增。
用法：python -m app.cli <指令> --help
"""

import argparse
import asyncio
import json

import httpx
from sqlalchemy import select

from app.billing import change_balance, format_usd, usd_to_micro
from app.chain import ChainWatcher
from app.config import ConfigError, get_settings
from app.db import create_tables, make_engine, make_sessionmaker
from app.deposits import assign_transfer
from app.models import ChainTransfer, User


def usd_arg(text: str) -> int:
    try:
        return usd_to_micro(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


async def cmd_list_models(_args, settings, _sm):
    if not settings.models:
        print("config.ini 裡還沒有任何 [model:名稱] 區段")
    for model in sorted(settings.models.values(), key=lambda m: m.name):
        print(f"{model.name:30} 上游={model.upstream}/{model.upstream_model}  "
              f"輸入 ${format_usd(model.input_price_micro)}/1M  輸出 ${format_usd(model.output_price_micro)}/1M  "
              f"輸出上限 {model.max_output_tokens}  "
              f"圖片 {f'每張最多 {model.image_tokens} tokens' if model.image_tokens else '不接受'}")


async def cmd_credit(args, _settings, sm):
    async with sm.begin() as session:
        user = (await session.execute(select(User).where(User.email == args.email.lower()))).scalar_one_or_none()
        if user is None:
            raise SystemExit(f"找不到使用者 {args.email}")
        await change_balance(session, user.id, args.usd, "adjust", args.note[:64])
    print(f"已調整 {args.email} 餘額 {format_usd(args.usd)} 美元")


async def cmd_list_unmatched(_args, _settings, sm):
    async with sm() as session:
        rows = (await session.execute(
            select(ChainTransfer).where(ChainTransfer.status == "unmatched").order_by(ChainTransfer.id)
        )).scalars().all()
    if not rows:
        print("沒有待處理的轉帳")
    for t in rows:
        print(f"#{t.id} {t.token} raw={t.amount_raw} from={t.from_address} tx={t.tx_hash} 原因={t.note}")


async def cmd_assign_transfer(args, settings, sm):
    try:
        amount = await assign_transfer(sm, settings, args.id, args.email)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    print(f"已把轉帳 #{args.id} 入帳給 {args.email}：{amount} 美元")


async def cmd_poll_chain(_args, settings, sm):
    if not settings.chain_enabled:
        raise SystemExit("儲值未啟用：config.ini 的 [chain] rpc_url、receive_address 和至少一個 [token:...] 都要填")
    async with httpx.AsyncClient(timeout=20) as http:
        result = await ChainWatcher(settings, sm, http).poll_once()
    print(json.dumps(result, ensure_ascii=False) if result else "沒有新的已確認區塊")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="AI 模型轉發站管理工具")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("list-models", help="列出 config.ini 裡的模型與價格")
    p.set_defaults(func=cmd_list_models)

    p = sub.add_parser("credit", help="手動調整使用者餘額（可為負數）")
    p.add_argument("--email", required=True)
    p.add_argument("--usd", required=True, type=usd_arg)
    p.add_argument("--note", default="admin")
    p.set_defaults(func=cmd_credit)

    p = sub.add_parser("list-unmatched", help="列出對不上訂單的鏈上轉帳")
    p.set_defaults(func=cmd_list_unmatched)

    p = sub.add_parser("assign-transfer", help="把一筆對不上的轉帳人工入帳給某個使用者")
    p.add_argument("--id", required=True, type=int)
    p.add_argument("--email", required=True)
    p.set_defaults(func=cmd_assign_transfer)

    p = sub.add_parser("poll-chain", help="手動掃一次鏈（除錯用）")
    p.set_defaults(func=cmd_poll_chain)
    return parser


async def _run(args) -> None:
    try:
        settings = get_settings()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None
    engine = make_engine(settings.database_url)
    try:
        await create_tables(engine)
        await args.func(args, settings, make_sessionmaker(engine))
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    main()

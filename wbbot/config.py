import os
from dataclasses import dataclass, field
from typing import Mapping

from dotenv import load_dotenv

MAX_ACCOUNTS = 9


@dataclass(frozen=True)
class AccountConfig:
    id: str
    name: str
    wb_token: str
    seller_name: str = ""
    shipping_point_id: int | None = None
    shipping_type: str = "selfShipping"
    shipping_days: int = 0


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_id: int
    db_path: str
    accounts: list[AccountConfig] = field(default_factory=list)


def _account(env: Mapping[str, str], n: int) -> AccountConfig | None:
    """Аккаунт №1 читается из WB_TOKEN, SELLER_NAME…; №2 и далее — с суффиксом _2, _3…"""
    sfx = "" if n == 1 else f"_{n}"
    token = env.get(f"WB_TOKEN{sfx}")
    if not token:
        return None
    point = env.get(f"SHIPPING_POINT_ID{sfx}")
    return AccountConfig(
        id=str(n),
        name=env.get(f"WB_NAME{sfx}") or f"Аккаунт {n}",
        wb_token=token,
        seller_name=env.get(f"SELLER_NAME{sfx}", ""),
        shipping_point_id=int(point) if point else None,
        shipping_type=env.get(f"SHIPPING_TYPE{sfx}") or "selfShipping",
        shipping_days=int(env.get(f"SHIPPING_DAYS{sfx}") or 0),
    )


def load_accounts(env: Mapping[str, str]) -> list[AccountConfig]:
    accounts = [a for n in range(1, MAX_ACCOUNTS + 1) if (a := _account(env, n))]
    if not accounts:
        raise KeyError("WB_TOKEN")
    return accounts


def load_config(env: Mapping[str, str] | None = None) -> Config:
    if env is None:
        load_dotenv()
        env = os.environ
    return Config(
        bot_token=env["BOT_TOKEN"],
        admin_id=int(env["ADMIN_ID"]),
        db_path=env.get("DB_PATH", "wbbot.db"),
        accounts=load_accounts(env),
    )

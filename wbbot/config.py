import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_id: int
    wb_token: str
    db_path: str
    seller_name: str = ""
    shipping_point_id: int | None = None
    shipping_type: str = "selfShipping"
    shipping_days: int = 0


def load_config() -> Config:
    load_dotenv()
    return Config(
        bot_token=os.environ["BOT_TOKEN"],
        admin_id=int(os.environ["ADMIN_ID"]),
        wb_token=os.environ["WB_TOKEN"],
        db_path=os.environ.get("DB_PATH", "wbbot.db"),
        seller_name=os.environ.get("SELLER_NAME", ""),
        shipping_point_id=int(os.environ["SHIPPING_POINT_ID"]) if os.environ.get("SHIPPING_POINT_ID") else None,
        shipping_type=os.environ.get("SHIPPING_TYPE", "selfShipping"),
        shipping_days=int(os.environ.get("SHIPPING_DAYS", "0")),
    )

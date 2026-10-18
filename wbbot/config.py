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


def load_config() -> Config:
    load_dotenv()
    return Config(
        bot_token=os.environ["BOT_TOKEN"],
        admin_id=int(os.environ["ADMIN_ID"]),
        wb_token=os.environ["WB_TOKEN"],
        db_path=os.environ.get("DB_PATH", "wbbot.db"),
        seller_name=os.environ.get("SELLER_NAME", ""),
    )

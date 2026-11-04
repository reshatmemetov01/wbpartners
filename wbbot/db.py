import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    order_id  INTEGER PRIMARY KEY,
    article   TEXT NOT NULL,
    sku       TEXT NOT NULL DEFAULT '',
    supply_id TEXT,
    added     INTEGER NOT NULL DEFAULT 0,
    trbx_id   TEXT,
    account   TEXT NOT NULL DEFAULT '1'
);
CREATE TABLE IF NOT EXISTS stores (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id             INTEGER NOT NULL,
    name              TEXT NOT NULL,
    token             TEXT NOT NULL,   -- зашифрован
    token_hash        TEXT NOT NULL,
    seller_name       TEXT NOT NULL DEFAULT '',
    shipping_point_id INTEGER,
    shipping_type     TEXT NOT NULL DEFAULT 'selfShipping',
    shipping_days     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (tg_id, token_hash)
);
"""


class Db:
    def __init__(self, path: str):
        self._c = sqlite3.connect(path)
        self._c.row_factory = sqlite3.Row
        self._c.executescript(SCHEMA)
        cols = {r["name"] for r in self._c.execute("PRAGMA table_info(items)")}
        if "account" not in cols:  # база от версии с одним аккаунтом
            self._c.execute("ALTER TABLE items ADD COLUMN account TEXT NOT NULL DEFAULT '1'")
            self._c.commit()

    def get(self, order_id: int) -> sqlite3.Row | None:
        return self._c.execute("SELECT * FROM items WHERE order_id=?", (order_id,)).fetchone()

    def ensure(self, order_id: int, article: str, sku: str, account: str = "1") -> None:
        self._c.execute(
            "INSERT OR IGNORE INTO items(order_id, article, sku, account) VALUES (?,?,?,?)",
            (order_id, article, sku, account),
        )
        self._c.commit()

    def set_supply(self, order_id: int, supply_id: str) -> None:
        self._update(order_id, "supply_id", supply_id)

    def reset_supply(self, order_id: int) -> None:
        self._c.execute(
            "UPDATE items SET supply_id=NULL, added=0, trbx_id=NULL WHERE order_id=?", (order_id,)
        )
        self._c.commit()

    def set_added(self, order_id: int) -> None:
        self._update(order_id, "added", 1)

    def set_trbx(self, order_id: int, trbx_id: str) -> None:
        self._update(order_id, "trbx_id", trbx_id)

    def _update(self, order_id: int, col: str, val) -> None:
        self._c.execute(f"UPDATE items SET {col}=? WHERE order_id=?", (val, order_id))
        self._c.commit()

    def remove(self, order_ids: list[int]) -> None:
        self._c.executemany("DELETE FROM items WHERE order_id=?", [(i,) for i in order_ids])
        self._c.commit()

    def assembling(self, account: str | None = None) -> list[sqlite3.Row]:
        """Заказы, полностью перенесённые на сборку (поставка + заказ + грузоместо).

        account — только заказы этого аккаунта; None — всех.
        """
        sql = "SELECT * FROM items WHERE added=1 AND trbx_id IS NOT NULL"
        args: tuple = ()
        if account is not None:
            sql, args = sql + " AND account=?", (account,)
        return self._c.execute(sql + " ORDER BY order_id", args).fetchall()

    # ---- магазины (аккаунты WB пользователей бота) ----

    def add_store(
        self,
        tg_id: int,
        name: str,
        token_enc: str,
        token_hash: str,
        seller_name: str = "",
        store_id: int | None = None,
        shipping_point_id: int | None = None,
        shipping_type: str = "selfShipping",
        shipping_days: int = 0,
    ) -> int:
        cur = self._c.execute(
            "INSERT INTO stores(id, tg_id, name, token, token_hash, seller_name,"
            " shipping_point_id, shipping_type, shipping_days) VALUES (?,?,?,?,?,?,?,?,?)",
            (store_id, tg_id, name, token_enc, token_hash, seller_name,
             shipping_point_id, shipping_type, shipping_days),
        )
        self._c.commit()
        return cur.lastrowid

    def stores(self, tg_id: int) -> list[sqlite3.Row]:
        return self._c.execute("SELECT * FROM stores WHERE tg_id=? ORDER BY id", (tg_id,)).fetchall()

    def store_count(self, tg_id: int) -> int:
        return self._c.execute("SELECT COUNT(*) FROM stores WHERE tg_id=?", (tg_id,)).fetchone()[0]

    def has_store_token(self, tg_id: int, token_hash: str) -> bool:
        return self._c.execute(
            "SELECT 1 FROM stores WHERE tg_id=? AND token_hash=?", (tg_id, token_hash)
        ).fetchone() is not None

    def any_stores(self) -> bool:
        return self._c.execute("SELECT 1 FROM stores LIMIT 1").fetchone() is not None

    def set_seller_name(self, store_id: int, name: str) -> None:
        self._c.execute("UPDATE stores SET seller_name=? WHERE id=?", (name, store_id))
        self._c.commit()

    def delete_store(self, store_id: int, tg_id: int) -> bool:
        """Удаляет магазин (вместе с токеном) и его заказы из списка «На сборке» бота."""
        cur = self._c.execute("DELETE FROM stores WHERE id=? AND tg_id=?", (store_id, tg_id))
        if cur.rowcount:
            self._c.execute("DELETE FROM items WHERE account=?", (str(store_id),))
        self._c.commit()
        return bool(cur.rowcount)

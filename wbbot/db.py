import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    order_id  INTEGER PRIMARY KEY,
    article   TEXT NOT NULL,
    sku       TEXT NOT NULL DEFAULT '',
    supply_id TEXT,
    added     INTEGER NOT NULL DEFAULT 0,
    trbx_id   TEXT
);
"""


class Db:
    def __init__(self, path: str):
        self._c = sqlite3.connect(path)
        self._c.row_factory = sqlite3.Row
        self._c.executescript(SCHEMA)

    def get(self, order_id: int) -> sqlite3.Row | None:
        return self._c.execute("SELECT * FROM items WHERE order_id=?", (order_id,)).fetchone()

    def ensure(self, order_id: int, article: str, sku: str) -> None:
        self._c.execute(
            "INSERT OR IGNORE INTO items(order_id, article, sku) VALUES (?,?,?)",
            (order_id, article, sku),
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

    def assembling(self) -> list[sqlite3.Row]:
        """Заказы, полностью перенесённые на сборку (поставка + заказ + грузоместо)."""
        return self._c.execute(
            "SELECT * FROM items WHERE added=1 AND trbx_id IS NOT NULL ORDER BY order_id"
        ).fetchall()

import pytest

from wbbot.bot import Bind, build_router, looks_like_token
from wbbot.config import AccountConfig, Config, load_config
from wbbot.crypto import Crypto, load_crypto, token_hash
from wbbot.db import Db
from wbbot.service import seed_legacy_stores
from wbbot.wb import WBError
from cryptography.fernet import Fernet

TOKEN_A = "aaa." + "x" * 70 + ".bbb"
TOKEN_B = "ccc." + "y" * 70 + ".ddd"


class FakeWB:
    def __init__(self, token, bad_status=None, content=True):
        self.token, self.bad_status, self.content, self.closed = token, bad_status, content, False

    async def ping(self):
        if self.bad_status:
            raise WBError("bad", self.bad_status)

    async def has_content_access(self):
        return self.content

    async def seller_name(self):
        return f"ИП {self.token[:3]}"

    async def new_orders(self):
        return [{"id": 1, "article": f"ART-{self.token[:3]}"}]

    async def aclose(self):
        self.closed = True


class User:
    def __init__(self, uid):
        self.id = uid


class Msg:
    def __init__(self, uid, text=""):
        self.from_user, self.text, self.out, self.deleted = User(uid), text, [], False

    async def answer(self, text, reply_markup=None):
        self.out.append(text)

    async def edit_text(self, text, reply_markup=None):
        self.out.append(text)

    async def delete(self):
        self.deleted = True


class CB:
    def __init__(self, uid, data):
        self.from_user, self.data, self.message, self.alerts = User(uid), data, Msg(uid), []

    async def answer(self, text=None, show_alert=False):
        if text and show_alert:
            self.alerts.append(text)


class State:
    def __init__(self):
        self.state = None

    async def set_state(self, s):
        self.state = s

    async def clear(self):
        self.state = None


def handler(router, obs, name):
    return next(h.callback for h in getattr(router, obs).handlers if h.callback.__name__ == name)


def setup(factory=None, max_stores=5):
    db, crypto = Db(":memory:"), Crypto(Fernet.generate_key())
    made = []

    def f(token):
        made.append(FakeWB(token))
        return made[-1]

    router, _ = build_router(Config("b", ":memory:", max_stores=max_stores), db, crypto, factory or f)
    return router, db, crypto, made


def test_crypto_roundtrip_and_key_file(tmp_path):
    c = Crypto(Fernet.generate_key())
    assert c.decrypt(c.encrypt("секрет")) == "секрет" and c.encrypt("секрет") != "секрет"
    db_path = str(tmp_path / "x.db")
    k1 = load_crypto({}, db_path)
    k2 = load_crypto({}, db_path)  # второй запуск читает тот же ключ
    assert k2.decrypt(k1.encrypt("t")) == "t"
    assert (tmp_path / "secret.key").stat().st_mode & 0o777 == 0o600


def test_looks_like_token():
    assert looks_like_token(TOKEN_A)
    assert not looks_like_token("привет") and not looks_like_token("a.b.c")


def test_config_without_admin_and_tokens():
    cfg = load_config({"BOT_TOKEN": "b"})
    assert cfg.admin_id is None and cfg.accounts == [] and cfg.max_stores == 5


def test_seed_legacy_keeps_account_ids_and_runs_once():
    db, crypto = Db(":memory:"), Crypto(Fernet.generate_key())
    accs = [AccountConfig("1", "A", "t1"), AccountConfig("2", "B", "t2", shipping_point_id=9)]
    assert seed_legacy_stores(db, crypto, 42, accs) == 2
    rows = db.stores(42)
    assert [r["id"] for r in rows] == [1, 2] and rows[1]["shipping_point_id"] == 9
    assert crypto.decrypt(rows[0]["token"]) == "t1"
    assert seed_legacy_stores(db, crypto, 42, accs) == 0  # магазины уже есть
    assert seed_legacy_stores(Db(":memory:"), crypto, None, accs) == 0  # без владельца


@pytest.mark.asyncio
async def test_bind_store_deletes_message_encrypts_token():
    router, db, crypto, made = setup()
    state, m = State(), Msg(7, TOKEN_A)
    await handler(router, "message", "bind_token")(m, state)
    assert m.deleted and "привязан" in m.out[0]
    (row,) = db.stores(7)
    assert row["name"] == "ИП aaa" and row["token"] != TOKEN_A and crypto.decrypt(row["token"]) == TOKEN_A
    # повторная привязка того же токена
    m2 = Msg(7, TOKEN_A)
    await handler(router, "message", "bind_token")(m2, state)
    assert "уже привязан" in m2.out[0] and db.store_count(7) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status,phrase", [(401, "не принял"), (403, "Маркетплейс")])
async def test_bind_rejects_bad_tokens(status, phrase):
    router, db, _, _ = setup(lambda t: FakeWB(t, bad_status=status))
    m = Msg(7, TOKEN_A)
    await handler(router, "message", "bind_token")(m, State())
    assert phrase in m.out[-1] and db.store_count(7) == 0


@pytest.mark.asyncio
async def test_bind_warns_without_content_category_and_rejects_garbage():
    router, db, _, _ = setup(lambda t: FakeWB(t, content=False))
    m = Msg(7, TOKEN_A)
    await handler(router, "message", "bind_token")(m, State())
    assert "Контент" in m.out[-1] and db.store_count(7) == 1
    m = Msg(7, "просто текст")
    await handler(router, "message", "bind_token")(m, State())
    assert "не похоже на токен" in m.out[0]


@pytest.mark.asyncio
async def test_store_limit():
    router, db, _, _ = setup(max_stores=1)
    await handler(router, "message", "bind_token")(Msg(7, TOKEN_A), State())
    m = Msg(7, TOKEN_B)
    await handler(router, "message", "bind_token")(m, State())
    assert "не больше 1" in m.out[0] and db.store_count(7) == 1


@pytest.mark.asyncio
async def test_users_are_isolated():
    router, db, crypto, made = setup()
    await handler(router, "message", "bind_token")(Msg(1, TOKEN_A), State())
    await handler(router, "message", "bind_token")(Msg(2, TOKEN_B), State())
    sid_a = str(db.stores(1)[0]["id"])
    # пользователь 2 не видит чужие заказы
    cb = CB(2, "orders")
    await handler(router, "callback_query", "orders")(cb)
    assert "ART-ccc" in cb.message.out[0] and "ART-aaa" not in cb.message.out[0]
    # и не может ни открыть, ни перенести, ни отвязать чужой магазин
    cb = CB(2, f"move_all:{sid_a}")
    await handler(router, "callback_query", "move_all")(cb)
    assert cb.alerts and not cb.message.out
    cb = CB(2, f"del_yes:{sid_a}")
    await handler(router, "callback_query", "del_yes")(cb)
    assert "не найден" in cb.message.out[0] and db.store_count(1) == 1
    # без магазинов — подсказка привязать
    m = Msg(3)
    await handler(router, "message", "assembling")(m, State())
    assert "Сначала привяжите" in m.out[0]


@pytest.mark.asyncio
async def test_unbind_removes_token_items_and_closes_client():
    router, db, _, made = setup()
    await handler(router, "message", "bind_token")(Msg(1, TOKEN_A), State())
    sid = db.stores(1)[0]["id"]
    db.ensure(5, "A", "1", str(sid))
    cb = CB(1, f"del_yes:{sid}")
    await handler(router, "callback_query", "del_yes")(cb)
    assert db.store_count(1) == 0 and db.get(5) is None and made[0].closed

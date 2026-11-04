# WB FBS Telegram-бот

Кнопки бота:
- **Мой магазин** — привязка/отвязка магазинов по токену WB и кнопка «Заказы (FBS)».
- **Заказы (FBS)** — новые FBS-заказы; «Перенести все на сборку» или перенос одного заказа из его карточки.
  Для каждого заказа создаётся поставка с именем = артикул, в неё кладётся заказ и создаётся 1 грузоместо.
- **На сборке** — поставки с заказами; «Получить стикеры» присылает PDF (58×40 мм):
  на каждую поставку 3 страницы — QR заказа, штрихкод товара, QR грузоместа.

Автоматически ничего не создаётся — только по кнопкам.

## Публичный бот и магазины
Бот открыт для всех. Пользователь сам привязывает свои магазины: «Мой магазин» → «Привязать магазин»
→ отправляет API-токен WB (категории «Маркетплейс» и «Контент»). Сообщение с токеном бот удаляет из чата,
токен хранится в базе зашифрованным (Fernet). Каждый пользователь видит и меняет только свои магазины,
максимум — `MAX_STORES` (по умолчанию 5). Отвязка («Мой магазин» → «Отвязать магазин») стирает токен.

**Ключ шифрования.** Берётся из `ENCRYPTION_KEY` или из файла `secret.key` рядом с базой (создаётся сам;
в Docker это `data/secret.key`). Храните ключ вместе с базой и делайте их бэкап: потеряете ключ — пользователям
придётся привязать магазины заново.

**Миграция.** Аккаунты из `.env` (`WB_TOKEN`, `WB_TOKEN_2`…) при первом запуске переносятся в базу
пользователю `ADMIN_ID`, если магазинов там ещё нет; заказы «На сборке» из прошлых версий остаются на месте.
После этого токены из `.env` можно удалить.

## Настройка
```
cp .env.example .env   # достаточно BOT_TOKEN; магазины привязываются в самом боте
```

## Запуск в Docker (рекомендуется)
```
mkdir -p data              # здесь хранится база (переживает перезапуски)
cp .env.example .env && nano .env
docker compose up -d --build
docker compose logs -f     # логи; выход Ctrl+C (бот продолжит работать)
```
Остановить: `docker compose down`. Обновить: `git pull && docker compose up -d --build`.
Бот сам поднимается после перезагрузки сервера (`restart: unless-stopped`).

## Запуск на сервере без Docker (через Termius)
```
git clone <repo> && cd wbpartners
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt  # для тестов: requirements-dev.txt
cp .env.example .env && nano .env
.venv/bin/python -m wbbot
```
Постоянная работа — systemd, `/etc/systemd/system/wbbot.service`:
```
[Unit]
Description=WB bot
After=network.target

[Service]
WorkingDirectory=/root/wbpartners
ExecStart=/root/wbpartners/.venv/bin/python -m wbbot
Restart=always

[Install]
WantedBy=multi-user.target
```
`systemctl enable --now wbbot`, логи: `journalctl -u wbbot -f`.

## Важно
Пути методов WB лежат в `wbbot/wb.py` (константы вверху) и написаны по памяти —
сверьте с https://dev.wildberries.ru/docs/openapi/orders-fbs перед боевым запуском.
Начните с одного заказа.

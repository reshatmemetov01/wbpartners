# WB FBS Telegram-бот

Кнопки бота:
- **Заказы** — новые FBS-заказы; «Перенести все на сборку» или перенос одного заказа из его карточки.
  Для каждого заказа создаётся поставка с именем = артикул, в неё кладётся заказ и создаётся 1 грузоместо.
- **На сборке** — поставки с заказами; «Получить стикеры» присылает PDF (58×40 мм):
  на каждую поставку 3 страницы — QR заказа, штрихкод товара, QR грузоместа.

Автоматически ничего не создаётся — только по кнопкам.

## Несколько аккаунтов WB
Первый аккаунт — переменные `WB_TOKEN`, `WB_NAME`, `SELLER_NAME`, `SHIPPING_POINT_ID`…
Второй и далее — те же имена с суффиксом `_2`, `_3` (до `_9`), см. `.env.example`.
Бот показывает заказы и поставки по аккаунтам; «Перенести все» и «Передать в доставку» работают
отдельно для каждого аккаунта; стикеры тоже получаются отдельным PDF на каждый аккаунт. Старая база (с одним аккаунтом)
обновляется автоматически — её заказы относятся к аккаунту №1.

## Настройка
```
cp .env.example .env   # BOT_TOKEN, ADMIN_ID, WB_TOKEN (категория «Маркетплейс»)
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

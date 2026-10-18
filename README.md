# WB FBS Telegram-бот

Кнопки бота:
- **Заказы** — новые FBS-заказы; «Перенести все на сборку» или перенос одного заказа из его карточки.
  Для каждого заказа создаётся поставка с именем = артикул, в неё кладётся заказ и создаётся 1 грузоместо.
- **На сборке** — поставки с заказами; «Получить стикеры» присылает PDF (58×40 мм):
  на каждую поставку 3 страницы — QR заказа, штрихкод товара, QR грузоместа.

Автоматически ничего не создаётся — только по кнопкам.

## Настройка
```
cp .env.example .env   # BOT_TOKEN, ADMIN_ID, WB_TOKEN (категория «Маркетплейс»)
```

## Запуск на сервере (через Termius)
```
git clone <repo> && cd wbpartners
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
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

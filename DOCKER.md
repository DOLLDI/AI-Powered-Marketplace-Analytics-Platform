# Docker

Проект рассчитан на production-запуск через Docker Compose.

## Состав

```text
proxyapi-bots       FastAPI, Telegram/VK bots, billing, admin, SmartCheck
proxyapi-postgres   PostgreSQL 16
```

Данные:

```text
postgres_data       volume PostgreSQL
./data              runtime-файлы приложения: SQLite fallback, WB cache, error log
```

## Первый запуск

```bash
cp .env.example .env
cp .env.prompts.example .env.prompts
nano .env
docker compose up -d --build
docker compose logs -f proxyapi-bots
```

Проверка:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

## Обновление проекта

```bash
docker compose down
docker compose up -d --build
docker compose logs -f proxyapi-bots
```

Если менялись только переменные окружения:

```bash
docker compose up -d --force-recreate proxyapi-bots
```

## Переменные Docker Compose

Compose читает `.env` и `.env.prompts`. Если промпты не нужно переопределять, все равно создайте `.env.prompts` из `.env.prompts.example`, потому что он указан в `docker-compose.yml`.

Ключевые значения:

```env
DB_BACKEND=postgres
DATABASE_URL=postgresql://proxyapi:proxyapi@postgres:5432/proxyapi
APP_BASE_URL=http://localhost:8000
PUBLIC_WEBHOOK_BASE_URL=https://your-domain.ru
AUTO_SET_WEBHOOKS=true
TELEGRAM_DELIVERY_MODE=webhook
VK_AUTO_SET_CALLBACK=true
START_CLOUDFLARED=false
```

Для постоянного production-домена лучше выключить Quick Tunnel:

```env
START_CLOUDFLARED=false
PUBLIC_WEBHOOK_BASE_URL=https://your-domain.ru
```

Quick Tunnel можно использовать для временной демонстрации:

```env
START_CLOUDFLARED=true
PUBLIC_WEBHOOK_BASE_URL=
TUNNEL_WAIT_SECONDS=120
```

## Webhook URL

Telegram:

```text
https://your-domain.ru/telegram/webhook
```

VK:

```text
https://your-domain.ru/vk/webhook
```

Billing:

```text
https://your-domain.ru/billing/webhook
```

Robokassa:

```text
https://your-domain.ru/billing/robokassa/result
https://your-domain.ru/billing/robokassa/success
https://your-domain.ru/billing/robokassa/fail
```

## Логи

```bash
docker compose logs -f proxyapi-bots
docker compose logs -f postgres
```

Лог ошибок приложения:

```text
/app/data/proxyapi_errors.log
```

На хосте:

```text
./data/proxyapi_errors.log
```

## Бэкап PostgreSQL

```bash
docker compose exec postgres pg_dump -U proxyapi proxyapi > backup.sql
```

Восстановление:

```bash
cat backup.sql | docker compose exec -T postgres psql -U proxyapi proxyapi
```

## Очистка WB cache

```bash
rm -rf data/wb_cache
docker compose restart proxyapi-bots
```

На Windows PowerShell:

```powershell
Remove-Item -Recurse -Force .\data\wb_cache
docker compose restart proxyapi-bots
```

## Production checklist

- `ADMIN_TOKEN` заменен на сильный секрет.
- `BILLING_SECRET` заменен на сильный секрет.
- `PROXYAPI_KEY` заполнен.
- `REQUIRE_SECURE_CONFIG=true`.
- `PUBLIC_WEBHOOK_BASE_URL` указывает на HTTPS-домен.
- Реальная оплата настроена по `PAYMENTS_SETUP.md`.
- PostgreSQL вынесен в volume или управляемую базу.
- Настроен регулярный бэкап базы.
- `.env`, `.env.local`, `.env.bak` не передаются заказчику как часть исходников.

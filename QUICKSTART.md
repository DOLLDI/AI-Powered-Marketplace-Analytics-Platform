# Быстрый старт

Этот файл нужен, чтобы поднять проект без чтения всей документации.

## 1. Подготовить `.env`

```powershell
copy .env.example .env
```

Заполните минимум:

```env
TELEGRAM_BOT_TOKEN=
VK_GROUP_TOKEN=
VK_CONFIRMATION_TOKEN=
VK_GROUP_ID=
PROXYAPI_URL=
PROXYAPI_KEY=
ADMIN_TOKEN=
ADMIN_IDS=
```

Для production:

```env
APP_ENV=production
REQUIRE_SECURE_CONFIG=true
PUBLIC_WEBHOOK_BASE_URL=https://your-domain.ru
DB_BACKEND=postgres
DATABASE_URL=postgresql://proxyapi:proxyapi@postgres:5432/proxyapi
```

## 2. Настроить промпты

Проект читает `.env.prompts` после `.env`. Если файл есть, значения из него переопределяют стандартные промпты.

Для Docker Compose файл нужен как env-file, поэтому создайте его из примера:

```powershell
copy .env.prompts.example .env.prompts
```

Основные переменные:

```env
TELEGRAM_SYSTEM_PROMPT=
VK_SYSTEM_PROMPT=
WB_SUMMARY_PROMPT=
```

Для SmartCheck особенно важен `WB_SUMMARY_PROMPT`: он задает стиль, формат ответа и правила блока про накрутку отзывов.

## 3. Запуск через Docker

```bash
cp .env.prompts.example .env.prompts
docker compose up -d --build
docker compose logs -f proxyapi-bots
```

Проверить состояние:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

## 4. Telegram

Для production/webhook:

```env
TELEGRAM_DELIVERY_MODE=webhook
AUTO_SET_WEBHOOKS=true
PUBLIC_WEBHOOK_BASE_URL=https://your-domain.ru
```

Для локальной проверки без webhook:

```env
TELEGRAM_DELIVERY_MODE=polling
AUTO_SET_WEBHOOKS=false
```

## 5. VK

Минимальные настройки:

```env
VK_GROUP_TOKEN=
VK_CONFIRMATION_TOKEN=
VK_GROUP_ID=
VK_CALLBACK_SECRET=
VK_AUTO_SET_CALLBACK=true
VK_LONGPOLL_ENABLED=true
VK_LONGPOLL_WAIT_SECONDS=25
```

Webhook URL:

```text
https://your-domain.ru/vk/webhook
```

Если автонастройка VK недоступна, сервис выводит в логах URL, confirmation string и secret key для ручной вставки в настройках сообщества.

## 6. Wildberries parsing

Лимит отзывов задается в `.env`:

```env
WB_MAX_REVIEWS_TOTAL=10000
```

Примеры:

```env
WB_MAX_REVIEWS_TOTAL=500
WB_MAX_REVIEWS_TOTAL=10000
WB_MAX_REVIEWS_TOTAL=all
```

Для тяжелых товаров:

```env
WB_MAX_REVIEWS_TOTAL=all
WB_MAX_PARSE_SECONDS=180
WB_CACHE_TTL_SECONDS=21600
WB_MAX_CONCURRENT_ANALYSES=4
```

После изменения `.env` перезапустите сервис:

```bash
docker compose up -d --build
```

## 7. Админ-панель

Открыть:

```text
https://your-domain.ru/admin/login
```

Войти по:

- `admin_id` из `ADMIN_IDS`;
- `ADMIN_TOKEN` из `.env`.

В админке доступны пользователи, платежи, ручные начисления, блокировки, чеки, логи и рассылка.

## 8. Проверка перед сдачей

Минимальный smoke-test:

1. `/health` и `/ready` возвращают OK.
2. Telegram отвечает на `/start`.
3. VK отвечает на новое сообщение.
4. WB-ссылка возвращает SmartCheck-ответ.
5. Админка открывается и показывает пользователей.
6. Рассылка отправляется в Telegram и VK.
7. Платеж в выбранном провайдере создает запись и начисляет тариф.
8. В логах нет traceback после тестового сценария.

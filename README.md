# ProxyApiBots / SmartCheck

SaaS-сервис с Telegram и VK-ботами для анализа товаров Wildberries через ProxyAPI/OpenAI. Пользователь отправляет ссылку или артикул WB, сервис собирает карточку товара, отзывы, считает локальные метрики риска и возвращает короткий вердикт SmartCheck.

## Скриншоты

![Telegram bot flow](screenshots/Screenshot%202026-10-08%20223704.png)

![SmartCheck result](screenshots/Screenshot%202026-10-08%20223720.png)

![Price comparison](screenshots/Screenshot%202026-10-08%20223738.png)

![Admin and billing screens](screenshots/Screenshot%202026-10-08%20224024.png)

## Что умеет проект

- Telegram-бот и VK-бот с единым ядром обработки запросов.
- VK получает сообщения через Callback API и дополнительный Long Poll fallback.
- Анализ товаров Wildberries по ссылке или артикулу.
- Сравнение цены WB с похожими товарами на Ozon и Яндекс Маркете.
- Настраиваемый лимит отзывов через `.env`: число, `all`, `все`, `0` или `-1`.
- Умный SmartCheck-вердикт: `РЕКОМЕНДУЕМ`, `РИСКОВАННО`, `НЕ РЕКОМЕНДУЕМ`.
- Отдельная проверка признаков накрутки отзывов.
- Профили риска по категориям: одежда/белье, электроника, beauty/health, общий профиль.
- Учет рейтинга, доли негатива, доли серьезных проблем, логистики, размеров и аномалий в отзывах.
- Кэширование WB-анализа и сравнения цен, ограничение параллельных проверок, таймауты и задержки между запросами.
- Платежи: sandbox, YooKassa, Robokassa.
- Опциональная фискализация через MyTax.
- Тарифы, баланс проверок, PRO-подписка, реферальная система.
- Админ-панель с пользователями, платежами, ручным подтверждением, выдачей проверок/PRO и рассылкой.
- Rich-рассылка в Telegram и VK: текст, картинка, кнопка.
- SQLite для локального запуска и PostgreSQL для production.
- Автонастройка webhook для Telegram/VK при наличии публичного URL.

## Как работает SmartCheck

1. Пользователь отправляет ссылку WB или артикул.
2. Сервис получает карточку товара: название, бренд, цену, рейтинг, количество отзывов, категорию.
3. Отзывы загружаются пачками. Лимит задается `WB_MAX_REVIEWS_TOTAL`.
4. Отзывы нормализуются, дубли и слишком слабые тексты не ломают статистику.
5. Код считает метрики:
   - средний рейтинг по отзывам;
   - долю негатива;
   - долю всех проблем;
   - долю серьезных проблем;
   - размерные, логистические и качественные риски;
   - признаки подозрительной активности в отзывах.
6. Математика SmartCheck выбирает рекомендуемый вердикт.
7. AI получает уже подготовленный summary-pack и объясняет результат простым языком.
8. Финальный ответ дополнительно нормализуется кодом: первая строка всегда содержит итоговый вердикт.

## Сравнение цен WB/Ozon/Яндекс Маркет

При WB-ссылке или артикуле сервис дополнительно сравнивает цену исходного товара с похожими товарами на Ozon и Яндекс Маркете:

1. Берет название, бренд, категорию, ссылку и текущую цену WB.
2. Формирует поисковый запрос с учетом модели, бренда, категории и ключевых слов.
3. Ищет товары на Ozon и Яндекс Маркете.
4. Нормализует названия и цены, считает похожесть и отсекает слабые совпадения.
5. Показывает отдельный блок Ozon, отдельный блок Яндекс Маркета, разницу в рублях/процентах и итог по самому выгодному варианту.

Если одна площадка временно недоступна или не отдала надежные цены, пользователь получает частичный результат по доступным данным. Для Ozon используется каскад источников: внешний provider при наличии, Ozon API/HTML, браузерный fallback через Playwright, затем резервный поиск по Brave/Bing/DuckDuckGo. Сервис использует только результаты со ссылкой и ценой, без подстановки выдуманных цен.

История и кэш хранятся в таблицах `price_compare_history` и `price_compare_cache`. Статистика доступна в Telegram-админке, `/admin/stats` и веб-панели `/admin/panel`.

Полный отчет по альтернативам не отправляется автоматически. После анализа WB бот показывает отдельную кнопку сравнения, а рекомендация учитывает цену, риск и надежность данных Ozon/Яндекс Маркета. Формула описана в `MARKETPLACE_RISK.md`.

## Формат ответа пользователю

Ответ намеренно короткий и прикладной:

- `SmartCheck: РЕКОМЕНДУЕМ / РИСКОВАННО / НЕ РЕКОМЕНДУЕМ`
- `Коротко`
- `Проверка накрутки отзывов`
- `Сильные стороны`
- `На что обратить внимание`
- `Итог`

Важно: пункт про накрутку не должен превращать хороший товар в подозрительный без сильных сигналов. Короткие отзывы и микроповторы сами по себе считаются слабым техническим сигналом, а не доказательством накрутки.

## Основные переменные окружения

Файл `.env` создается из `.env.example`.

```env
TELEGRAM_BOT_TOKEN=
VK_GROUP_TOKEN=
VK_CONFIRMATION_TOKEN=
VK_GROUP_ID=
VK_LONGPOLL_ENABLED=true
PROXYAPI_URL=
PROXYAPI_KEY=
ADMIN_TOKEN=
ADMIN_IDS=
DB_BACKEND=postgres
DATABASE_URL=postgresql://proxyapi:proxyapi@postgres:5432/proxyapi
PAYMENT_PROVIDER=sandbox
WB_MAX_REVIEWS_TOTAL=10000
WB_CACHE_TTL_SECONDS=21600
WB_MAX_PARSE_SECONDS=45
PRICE_COMPARE_ENABLED=true
PRICE_COMPARE_CACHE_TTL_SECONDS=21600
PRICE_COMPARE_BROWSER_FALLBACK_ENABLED=true
PRICE_COMPARE_EXTERNAL_SEARCH_URL=
PRICE_COMPARE_SEARCH_ENGINE_FALLBACK_ENABLED=true
PRICE_COMPARE_MAX_RESULTS_PER_MARKETPLACE=3
PRICE_COMPARE_MIN_SIMILARITY=58
```

Для production обязательно заменить все секреты, выставить `REQUIRE_SECURE_CONFIG=true` и указать публичный домен в `PUBLIC_WEBHOOK_BASE_URL`.

## Настройка количества отзывов WB

```env
# взять максимум 500 отзывов
WB_MAX_REVIEWS_TOTAL=500

# взять максимум 10000 отзывов
WB_MAX_REVIEWS_TOTAL=10000

# пытаться загрузить все доступные отзывы до таймаута WB_MAX_PARSE_SECONDS
WB_MAX_REVIEWS_TOTAL=all
```

Для тяжелых товаров с 10-20 тыс. отзывов увеличьте:

```env
WB_MAX_REVIEWS_TOTAL=all
WB_MAX_PARSE_SECONDS=180
WB_BATCH_SIZE=100
WB_REQUEST_DELAY_SECONDS=0.35
```

## Локальный запуск

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Для Telegram polling:

```env
TELEGRAM_DELIVERY_MODE=polling
AUTO_SET_WEBHOOKS=false
```

## Docker-запуск

```bash
cp .env.example .env
cp .env.prompts.example .env.prompts
docker compose up -d --build
docker compose logs -f proxyapi-bots
```

Проверка:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

## Админ-панель

URL:

```text
/admin/login
```

Доступ:

- `ADMIN_TOKEN` - секретный токен админки.
- `ADMIN_IDS` - список Telegram ID администраторов через запятую.

Возможности:

- статистика по платформам;
- список пользователей;
- блокировка и разблокировка;
- выдача проверок;
- выдача PRO;
- просмотр платежей;
- ручное подтверждение платежа;
- повторная отправка чека;
- rich-рассылка в Telegram и VK;
- просмотр хвоста error-log.

## Структура проекта

```text
app/
  main.py                    # FastAPI app, lifecycle, webhook setup, workers
  routers/                   # HTTP endpoints: Telegram, VK, billing, admin
  platforms/
    telegram/                # Telegram bot, handlers, broadcast
    vk/                      # VK bot, handlers, broadcast, VK API helpers
  services/
    wb.py                    # Wildberries parser and SmartCheck analytics
    prompts.py               # AI prompts
    db.py                    # SQLite/PostgreSQL access layer
    billing.py               # payments and entitlements
    fiscal.py                # optional receipt retry worker
    rate_limit.py            # antiflood
    config.py                # environment settings
  shared/
    types.py
```

## Production notes

- Использовать PostgreSQL, не SQLite.
- Не хранить реальные секреты в репозитории.
- Включить `REQUIRE_SECURE_CONFIG=true`.
- Реальную оплату подключать по [PAYMENTS_SETUP.md](PAYMENTS_SETUP.md).
- Следить за размером `data/wb_cache` и логов.

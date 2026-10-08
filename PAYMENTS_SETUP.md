# Настройка реальной оплаты

Инструкция описывает именно текущую реализацию проекта ProxyApiBots / SmartCheck.

Поддерживаемые провайдеры:

- `sandbox` - тестовая оплата внутри проекта.
- `yookassa` - реальная оплата через ЮKassa.
- `robokassa` - реальная оплата через Robokassa.

## Как устроена оплата в проекте

Пользователь открывает оплату из бота. Бот генерирует ссылку:

```text
https://your-domain.ru/billing/checkout?token=...
```

Дальше проект:

1. Проверяет checkout-token.
2. Создает pending-платеж в базе.
3. Показывает страницу принятия условий: Публичная оферта, Политика конфиденциальности и дисклеймер.
4. После подтверждения отправляет пользователя на страницу провайдера.
5. Ждет серверный callback от провайдера.
6. Проверяет подпись или статус платежа.
7. Начисляет тариф.
8. Отправляет пользователю уведомление.
9. Создает чек в локальном режиме или через MyTax, если он настроен.

Тарифы сейчас заданы в `app/services/billing.py`:

```text
one_time  99 ₽   5 проверок
pro       349 ₽  30 проверок на 30 дней
```

## Общие требования

Перед включением реальной оплаты нужен постоянный HTTPS-домен.

Не используйте Cloudflare Quick Tunnel или временные `loca.lt` URL для production-оплаты. Провайдеры платежей должны ходить на стабильный адрес.

Минимальные production-настройки:

```env
APP_ENV=production
REQUIRE_SECURE_CONFIG=true
APP_BASE_URL=https://your-domain.ru
PUBLIC_WEBHOOK_BASE_URL=https://your-domain.ru
BILLING_SECRET=long-random-secret
DB_BACKEND=postgres
DATABASE_URL=postgresql://proxyapi:proxyapi@postgres:5432/proxyapi
```

После изменения `.env`:

```bash
docker compose up -d --force-recreate proxyapi-bots
```

Проверка:

```bash
curl https://your-domain.ru/health
curl https://your-domain.ru/ready
```

## Вариант 1. ЮKassa

ЮKassa в этом проекте работает через API payments:

- проект создает платеж через `https://api.yookassa.ru/v3/payments`;
- пользователь переходит на `confirmation_url`;
- ЮKassa отправляет webhook на `/billing/webhook`;
- проект дополнительно проверяет платеж запросом к API ЮKassa.

Официальная документация:

- HTTP-уведомления ЮKassa: https://yookassa.ru/docs/support/merchant/payments/http-notifications
- Webhooks API ЮKassa: https://yookassa.ru/developers/using-api/webhooks
- Payment process и `metadata`: https://yookassa.ru/developers/payment-acceptance/getting-started/payment-process

### 1. Настроить `.env`

```env
PAYMENT_PROVIDER=yookassa
YOOKASSA_SHOP_ID=your_shop_id
YOOKASSA_SECRET_KEY=your_secret_key
YOOKASSA_RETURN_URL=https://your-domain.ru/billing/return
APP_BASE_URL=https://your-domain.ru
PUBLIC_WEBHOOK_BASE_URL=https://your-domain.ru
BILLING_SECRET=long-random-secret
```

`YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY` берутся в личном кабинете ЮKassa.

### 2. Настроить webhook в ЮKassa

В личном кабинете ЮKassa откройте:

```text
Интеграция -> HTTP-уведомления
```

URL уведомлений:

```text
https://your-domain.ru/billing/webhook
```

Событие:

```text
payment.succeeded
```

Адрес должен быть HTTPS. По документации ЮKassa URL уведомлений - это адрес вашей системы, куда приходят события, и сайт должен быть защищен SSL-сертификатом.

### 3. Перезапустить проект

```bash
docker compose up -d --force-recreate proxyapi-bots
docker compose logs -f proxyapi-bots
```

### 4. Проверить оплату

1. Откройте бота.
2. Перейдите в профиль.
3. Нажмите покупку `5 проверок` или `PRO`.
4. Оплатите через ЮKassa.
5. Вернитесь в бота и откройте профиль.

Ожидаемый результат:

- в базе появился платеж со статусом `paid`;
- баланс проверок или PRO обновился;
- пользователь получил уведомление;
- в админке платеж виден в списке.

### 5. Частые ошибки ЮKassa

`YooKassa checkout validation error`

Проверьте:

```env
YOOKASSA_SHOP_ID=
YOOKASSA_SECRET_KEY=
```

`Invalid YooKassa webhook`

Проект получил webhook, но проверка через API ЮKassa не подтвердила платеж. Обычно причины:

- webhook пришел не от того магазина;
- в payload нет ожидаемого payment id;
- metadata не содержит `external_payment_id`;
- указаны неверные `SHOP_ID` или `SECRET_KEY`.

Платеж прошел, но тариф не начислился:

- проверьте `/admin/panel`;
- найдите платеж по external payment id;
- посмотрите `ERROR_LOG_PATH`;
- при необходимости подтвердите платеж вручную в админке.

## Вариант 2. Robokassa

Robokassa в этом проекте работает через redirect на платежную форму:

- проект формирует URL `https://auth.robokassa.ru/Merchant/Index.aspx`;
- в запросе передает `MerchantLogin`, `OutSum`, `InvId`, `SignatureValue` и `Shp_*`;
- Robokassa отправляет серверный Result URL на `/billing/robokassa/result`;
- проект проверяет `SignatureValue` через `ROBOKASSA_PASSWORD2`;
- после успешной проверки начисляет тариф.

Официальная документация:

- Уведомления и переадресация Robokassa: https://docs.robokassa.ru/ru/notifications-and-redirects
- Интерфейс оплаты и подпись `SignatureValue`: https://docs.robokassa.ru/ru/pay-interface

### 1. Настроить `.env`

```env
PAYMENT_PROVIDER=robokassa
ROBOKASSA_MERCHANT_LOGIN=your_merchant_login
ROBOKASSA_PASSWORD1=your_password_1
ROBOKASSA_PASSWORD2=your_password_2
ROBOKASSA_PASSWORD3=your_password_3
ROBOKASSA_TEST_MODE=false
APP_BASE_URL=https://your-domain.ru
PUBLIC_WEBHOOK_BASE_URL=https://your-domain.ru
BILLING_SECRET=long-random-secret
```

Для тестового режима:

```env
ROBOKASSA_TEST_MODE=true
```

Для реальных списаний:

```env
ROBOKASSA_TEST_MODE=false
```

### 2. Настроить URLs в кабинете Robokassa

В технических настройках магазина укажите:

```text
Result URL:
https://your-domain.ru/billing/robokassa/result
Method: POST
```

```text
Success URL:
https://your-domain.ru/billing/robokassa/success
Method: GET
```

```text
Fail URL:
https://your-domain.ru/billing/robokassa/fail
Method: GET
```

### 3. Настроить подписи

В кабинете Robokassa должны совпадать:

```env
ROBOKASSA_PASSWORD1
ROBOKASSA_PASSWORD2
```

`ROBOKASSA_PASSWORD3` можно сохранить в `.env`, но текущая интеграция его не использует: создание платежа подписывается `Password1`, а Result URL проверяется через `Password2`.

Проект формирует платежную подпись так:

```text
MerchantLogin:OutSum:InvId:Password1:Shp_external_payment_id=...:Shp_plan_code=...:Shp_platform=...:Shp_user_id=...
```

Result callback проверяется так:

```text
OutSum:InvId:Password2:Shp_external_payment_id=...:Shp_plan_code=...:Shp_platform=...:Shp_user_id=...
```

Параметры `Shp_*` сортируются по алфавиту. Это совпадает с правилом Robokassa: пользовательские параметры `Shp_*` участвуют в подписи и сортируются строго по алфавиту.

### 4. Перезапустить проект

```bash
docker compose up -d --force-recreate proxyapi-bots
docker compose logs -f proxyapi-bots
```

### 5. Проверить оплату

1. Откройте профиль в Telegram или VK.
2. Нажмите покупку.
3. Проверьте, что открылась форма Robokassa.
4. Проведите тестовый или реальный платеж.
5. После Result URL проект должен вернуть:

```text
OK<InvId>
```

6. Проверьте профиль пользователя в боте.

Ожидаемый результат:

- платеж начислил проверки или PRO;
- платеж отображается в админке;
- пользователь получил уведомление;
- в логах нет `Invalid Robokassa signature`.

### 6. Частые ошибки Robokassa

`Invalid Robokassa signature`

Проверьте:

- `ROBOKASSA_PASSWORD1`;
- `ROBOKASSA_PASSWORD2`;
- метод подписи в кабинете Robokassa;
- что Result URL получает все `Shp_*` параметры;
- что `OutSum` и `InvId` приходят без изменения.

Платеж есть в Robokassa, но тариф не начислился:

- проверьте, что `Result URL` именно POST;
- проверьте URL `/billing/robokassa/result`;
- проверьте лог `ERROR_LOG_PATH`;
- найдите платеж в админке;
- при необходимости подтвердите платеж вручную.

Форма оплаты открывается в тестовом режиме:

```env
ROBOKASSA_TEST_MODE=false
```

После изменения обязательно пересоздать контейнер.

## Фискализация и чеки

Проект поддерживает опциональную отправку чеков через MyTax.

Если MyTax не настроен, проект создает локальный чек:

```text
/billing/receipt?payment_id=...
```

Для MyTax:

```env
MYTAX_API_URL=https://...
MYTAX_API_TOKEN=...
MYTAX_SELLER_INN=...
FISCAL_RETRY_INTERVAL_SECONDS=300
FISCAL_MAX_ATTEMPTS=5
```

Если отправка чека не удалась, worker будет повторять попытки.

В админке есть ручная повторная отправка чека.

## Админка и ручное восстановление

Админка:

```text
https://your-domain.ru/admin/login
```

Что можно сделать:

- найти платеж;
- посмотреть пользователя;
- вручную подтвердить платеж;
- повторить отправку чека;
- выдать проверки;
- выдать PRO.

Ручное подтверждение нужно только при сбое callback от провайдера.

## Проверка после подключения

Минимальный чеклист:

1. `PAYMENT_PROVIDER` выбран правильно.
2. `APP_BASE_URL` указывает на публичный HTTPS-домен.
3. `/health` и `/ready` открываются снаружи.
4. В боте открывается ссылка оплаты.
5. Платеж создается в базе как pending.
6. Провайдер отправляет callback.
7. Платеж становится paid.
8. Пользователь получает лимит или PRO.
9. Пользователь получает уведомление.
10. Чек создан локально или отправлен через MyTax.

## Команды диагностики

Логи приложения:

```bash
docker compose logs -f proxyapi-bots
```

Проверка health:

```bash
curl https://your-domain.ru/health
curl https://your-domain.ru/ready
```

Поиск ошибок:

```bash
docker compose logs --tail=300 proxyapi-bots | grep -Ei "payment|billing|yookassa|robokassa|receipt|error|exception|traceback"
```

Проверка env внутри контейнера без вывода секретов:

```bash
docker compose exec -T proxyapi-bots sh -lc 'printf "PAYMENT_PROVIDER=$PAYMENT_PROVIDER\nAPP_BASE_URL=$APP_BASE_URL\nPUBLIC_WEBHOOK_BASE_URL=$PUBLIC_WEBHOOK_BASE_URL\n"'
```

## Что нельзя делать в production

- Не оставлять `PAYMENT_PROVIDER=sandbox`.
- Не оставлять `BILLING_SECRET=change-me-billing-secret`.
- Не использовать временный tunnel URL для платежных callback.
- Не передавать заказчику заполненный `.env`.
- Не включать `ROBOKASSA_TEST_MODE=true` для реальных списаний.
- Не менять тарифы в коде без проверки начислений и webhook.

## Быстрое переключение провайдера

ЮKassa:

```env
PAYMENT_PROVIDER=yookassa
YOOKASSA_SHOP_ID=...
YOOKASSA_SECRET_KEY=...
YOOKASSA_RETURN_URL=https://your-domain.ru/billing/return
```

Robokassa:

```env
PAYMENT_PROVIDER=robokassa
ROBOKASSA_MERCHANT_LOGIN=...
ROBOKASSA_PASSWORD1=...
ROBOKASSA_PASSWORD2=...
ROBOKASSA_TEST_MODE=false
```

После любого переключения:

```bash
docker compose up -d --force-recreate proxyapi-bots
docker compose logs -f proxyapi-bots
```

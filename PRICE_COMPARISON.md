# SmartCheck: сравнение цен WB/Ozon/Яндекс Маркет

Модуль доступен только пользователям с активной подпиской SmartCheck PRO. После обычного анализа WB подписчик получает предложение запустить сравнение отдельной кнопкой. Для Free и пакета 5 проверок поиск по маркетплейсам не запускается: бот показывает аккуратное описание возможности и кнопку оформления PRO.

Служебные тестовые аккаунты из `TELEGRAM_FREE_TESTER_IDS` имеют доступ для приемки без оплаты. Это внутреннее исключение и не меняет тариф пользователей.

## Что делает модуль

1. Извлекает артикул WB из ссылки или текста.
2. Получает карточку WB: название, бренд, цену, ссылку, фото, рейтинг, отзывы и категорию.
3. Формирует поисковый запрос по бренду, названию, модели, категории и важным словам.
4. Ищет похожие товары на Ozon и Яндекс Маркете.
5. Нормализует цены и названия.
6. Считает похожесть товаров.
7. Отсекает товары без цены/ссылки и слабые совпадения.
8. Считает разницу с WB в рублях и процентах.
9. Выводит лучшие варианты отдельно по Ozon и Яндекс Маркету.
10. Показывает итог: где товар выгоднее и насколько.

## Частичные результаты

Если одна площадка не отвечает, бот все равно показывает данные по второй площадке. Это штатный сценарий.

Ozon часто отдает 403 на серверные поисковые запросы. В этом случае SmartCheck не выдумывает цены, а пишет, что площадка временно не дала надежные данные. Для стабильного Ozon-парсинга на проде может потребоваться легальный API/партнерский источник данных или прокси с корректной географией.

## Настройки `.env`

```env
PRICE_COMPARE_ENABLED=true
PRICE_COMPARE_CACHE_TTL_SECONDS=21600
PRICE_COMPARE_TIMEOUT_SECONDS=20
PRICE_COMPARE_HTTP_RETRIES=2
PRICE_COMPARE_BROWSER_FALLBACK_ENABLED=true
PRICE_COMPARE_BROWSER_TIMEOUT_SECONDS=15
PRICE_COMPARE_EXTERNAL_SEARCH_URL=
PRICE_COMPARE_EXTERNAL_SEARCH_KEY=
PRICE_COMPARE_BRAVE_SEARCH_API_KEY=
PRICE_COMPARE_SERPAPI_KEY=
PRICE_COMPARE_SEARCH_ENGINE_FALLBACK_ENABLED=true
PRICE_COMPARE_MAX_RESULTS_PER_MARKETPLACE=3
PRICE_COMPARE_MIN_SIMILARITY=58
PRICE_COMPARE_SEARCH_RESULTS_LIMIT=12
PRICE_COMPARE_OZON_ENABLED=true
PRICE_COMPARE_YANDEX_ENABLED=true
```

## Внешний provider для production

Для стабильного production-поиска, особенно по Ozon, можно подключить отдельный источник данных через:

```env
PRICE_COMPARE_EXTERNAL_SEARCH_URL=https://example.com/search
PRICE_COMPARE_EXTERNAL_SEARCH_KEY=secret
```

Если в URL есть `{marketplace}` и `{query}`, SmartCheck выполнит GET:

```text
https://example.com/search?marketplace={marketplace}&q={query}
```

Если плейсхолдеров нет, SmartCheck выполнит POST:

```json
{
  "marketplace": "ozon",
  "query": "чехол iphone 13",
  "limit": 12
}
```

Ожидаемый ответ:

```json
{
  "items": [
    {
      "marketplace": "ozon",
      "title": "Название товара",
      "price": 990,
      "old_price": 1290,
      "url": "https://www.ozon.ru/product/...",
      "image_url": "https://...",
      "rating": 4.8,
      "review_count": 120,
      "brand": "Brand",
      "category": "Категория"
    }
  ]
}
```

Внешний provider используется первым. Если он не задан или не вернул товары, SmartCheck переходит к прямому HTTP/API, затем к браузерному fallback через Playwright и резервному поиску по Brave/Bing/DuckDuckGo. В резервных поисковых источниках используются только результаты, где есть ссылка на товар и цена; цены не придумываются.

Для production без зависимости от HTML маркетплейса рекомендуется задать хотя бы один управляемый поисковый источник:

```env
PRICE_COMPARE_BRAVE_SEARCH_API_KEY=
PRICE_COMPARE_SERPAPI_KEY=
```

Порядок источников:

1. `PRICE_COMPARE_EXTERNAL_SEARCH_URL` - внутренний/платный агрегатор, если он есть.
2. Brave Search API.
3. SerpAPI.
4. Прямой API/HTML площадки.
5. Playwright browser fallback.
6. Резервные поисковые HTML-выдачи Brave/Bing/DuckDuckGo.

Состояние источников видно в `/admin/stats` и в веб-панели `/admin/panel`.

## Хранение данных

Создаются таблицы:

- `price_compare_cache` - кэш поисковых и итоговых результатов.
- `price_compare_history` - история запросов, найденные товары, лучший вариант, статус и время обработки.

Статистика доступна в:

- Telegram-админке через кнопку `Статистика`;
- HTTP endpoint `/admin/stats`;
- веб-панели `/admin/panel`.

## Критерии проверки

Проверить можно ссылкой WB или артикулом. Успешный ответ должен содержать:

- цену исходного товара WB;
- блок Ozon;
- блок Яндекс Маркета;
- ссылки на найденные товары;
- разницу в рублях и процентах;
- уровень совпадения;
- итоговый вывод по самому выгодному варианту.

## Автоматическая приемка

Локальные тесты не обращаются к платным API и не зависят от доступности маркетплейсов:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Они проверяют входные данные, запрос, релевантность, фильтрацию, расчет цен, partial/error-сценарии, кэш, историю, статистику и формат сообщений. Результаты последней production-проверки находятся в `TZ3_ACCEPTANCE_CHECK.md`.

Дополнительно тестируется entitlement: пакет проверок не открывает сравнение, активный PRO открывает, истекший PRO автоматически закрывает доступ, а запрос без PRO блокируется до обращения к Ozon/Яндекс Маркету.

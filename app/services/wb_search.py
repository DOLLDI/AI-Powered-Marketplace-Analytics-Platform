import httpx

SEARCH_URL = "https://search.wb.ru/exactmatch/ru/common/v5/search"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://www.wildberries.ru/",
}


def _safe_get_first_product(data: dict) -> dict | None:
    try:
        products = data["data"]["products"]
        if isinstance(products, list) and products:
            return products[0]
    except Exception:
        pass
    return None


async def search_wb_article(query: str) -> dict | None:

    params = {
        "appType": 1,
        "curr": "rub",
        "dest": -1257786,
        "resultset": "catalog",
        "sort": "popular",
        "page": 1,
        "query": query,
    }

    async with httpx.AsyncClient(timeout=15.0, headers=HEADERS) as client:
        try:
            r = await client.get(SEARCH_URL, params=params)
        except Exception:
            return None

        if r.status_code != 200:
            return None

        try:
            data = r.json()
        except Exception:
            return None

        product = _safe_get_first_product(data)

        if not product:
            return None

        nm_id = product.get("id") or product.get("nmId")

        if not nm_id:
            return None

        return {
            "nmId": nm_id,
            "title": product.get("name"),
            "brand": product.get("brand"),
            "rating": product.get("rating"),
            "feedbacks": product.get("feedbacks"),
            "price": product.get("priceU"),
            "sale_price": product.get("salePriceU"),
            "image": product.get("img"),
        }
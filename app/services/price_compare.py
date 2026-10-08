import asyncio
import hashlib
import io
import json
import re
import time
from dataclasses import asdict, dataclass, field, fields as dataclass_fields
from html import escape
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
try:
    from PIL import Image, UnidentifiedImageError
except ImportError: 
    Image = None
    UnidentifiedImageError = OSError

from app.services.config import settings
from app.services.db import (
    get_price_compare_cache,
    has_active_pro_access,
    record_price_compare_history,
    set_price_compare_cache,
)
from app.services.error_logger import log_api_error
from app.services.logger import logger
from app.services.product_matcher import (
    TYPE_QUERY_TERMS,
    build_identity,
    build_precise_query,
    match_identities,
    tokenize as matcher_tokens,
)
from app.services.wb import WBProduct, extract_wb_article, fetch_wb_product


PRICE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}

STOPWORDS = {
    "для", "под", "над", "или", "без", "при", "про", "это", "как", "что",
    "на", "в", "во", "с", "со", "из", "от", "до", "по", "и", "а", "но",
    "the", "and", "with", "for", "new", "original", "sale", "wildberries",
    "wb", "товар", "набор", "комплект", "штука", "шт", "цвет", "размер",
    "женский", "женская", "женские", "мужской", "мужская", "мужские",
    "детский", "детская", "детские", "универсальный", "оригинальный",
    "новый", "новая", "новые", "подарок", "акция", "скидка",
}

TOKEN_ALIASES = {
    "chehol": "чехол",
    "chehly": "чехол",
    "chehla": "чехол",
    "chekhly": "чехол",
    "chekhol": "чехол",
    "case": "чехол",
    "cover": "чехол",
    "bumper": "бампер",
    "kozhanyy": "кожаный",
    "kozhanyi": "кожаный",
    "kozhanyj": "кожаный",
    "leather": "кожаный",
    "iphone": "iphone",
    "айфон": "iphone",
    "айфона": "iphone",
    "айфону": "iphone",
    "айфоне": "iphone",
    "смартфоны": "смартфон",
    "smartfon": "смартфон",
    "smartphone": "смартфон",
    "smartphones": "смартфон",
    "telefon": "телефон",
    "phone": "телефон",
    "phones": "телефон",
}

TOKEN_PREFIX_ALIASES = (
    ("chehol", "чехол"),
    ("cheh", "чехол"),
    ("chekh", "чехол"),
    ("kozh", "кожаный"),
)

PRODUCT_TYPE_GROUPS = {
    "phone_case": {"чехол", "бампер", "накладка", "кейс"},
    "phone": {"смартфон", "телефон"},
}


MARKETPLACE_TITLES = {
    "wb": "Wildberries",
    "ozon": "Ozon",
    "yandex": "Яндекс Маркет",
}


class PriceCompareError(Exception):
    pass


class PriceCompareProRequired(PriceCompareError):
    pass


@dataclass
class ComparableProduct:
    marketplace: str
    title: str
    price: float | None
    url: str
    old_price: float | None = None
    image_url: str = ""
    rating: float | None = None
    review_count: int | None = None
    category: str = ""
    brand: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketplaceRiskAssessment:
    score: int
    confidence: int
    level: str
    signals: list[str] = field(default_factory=list)
    basis: str = ""


@dataclass
class PriceComparisonItem:
    product: ComparableProduct
    similarity: int
    similarity_label: str
    price_diff: float
    price_diff_percent: float
    relation: str
    risk: MarketplaceRiskAssessment | None = None


@dataclass
class PriceRiskDecision:
    code: str
    title: str
    explanation: str
    wb_risk_score: int
    recommended_marketplace: str = "wb"
    recommended_url: str = ""
    recommended_title: str = ""


@dataclass
class MarketplaceComparison:
    marketplace: str
    items: list[PriceComparisonItem] = field(default_factory=list)
    raw_count: int = 0
    filtered_count: int = 0
    from_cache: bool = False
    error: str = ""


@dataclass
class PriceComparisonResult:
    wb_product: WBProduct
    wb_price: float
    search_query: str
    ozon: MarketplaceComparison
    yandex: MarketplaceComparison
    best_marketplace: str
    best_price: float
    best_url: str
    best_title: str
    status: str
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    from_cache: bool = False
    risk_decision: PriceRiskDecision | None = None


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value)
    match = re.search(r"(\d[\d\s\u00a0]{0,10})(?:[,.](\d{1,2}))?", text)
    if not match:
        return None
    number = match.group(1).replace(" ", "").replace("\u00a0", "")
    fraction = match.group(2) or ""
    try:
        parsed = float(f"{number}.{fraction}" if fraction else number)
    except ValueError:
        return None
    if parsed <= 0:
        return None
    return round(parsed, 2)


def _format_price(value: float | None) -> str:
    if value is None:
        return "нет цены"
    if float(value).is_integer():
        return f"{int(value):,}".replace(",", " ") + " ₽"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _normalize_text(value: str) -> str:
    value = (value or "").lower().replace("ё", "е")
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[^0-9a-zа-я+\-\s]", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _tokens(value: str) -> list[str]:
    normalized = _normalize_text(value)
    result = []
    for raw_token in normalized.split():
        for token in re.split(r"[-+]+", raw_token):
            token = token.strip("-+")
            if len(token) < 2 and not token.isdigit():
                continue
            if token in STOPWORDS:
                continue
            result.append(_normalize_token(token))
    return result


def _normalize_token(token: str) -> str:
    token = (token or "").lower().strip("-+")
    if token in TOKEN_ALIASES:
        return TOKEN_ALIASES[token]
    if token.startswith("чехл") or token.startswith("чехол"):
        return "чехол"
    if token.startswith("кожан"):
        return "кожаный"
    if token.startswith("смартфон"):
        return "смартфон"
    if token.startswith("smartfon"):
        return "смартфон"
    if token.startswith("телефон"):
        return "телефон"
    for prefix, alias in TOKEN_PREFIX_ALIASES:
        if token.startswith(prefix):
            return alias
    return token


def _product_type_groups(tokens: set[str]) -> set[str]:
    return {
        group
        for group, group_tokens in PRODUCT_TYPE_GROUPS.items()
        if tokens & group_tokens
    }


def _has_product_type_conflict(source_tokens: set[str], candidate_tokens: set[str]) -> bool:
    source_groups = _product_type_groups(source_tokens)
    candidate_groups = _product_type_groups(candidate_tokens)
    if "phone_case" in source_groups and "phone_case" not in candidate_groups and "phone" in candidate_groups:
        return True
    if "phone" in source_groups and "phone_case" in candidate_groups and "phone" not in candidate_groups:
        return True
    return False


def _marketplace_product_url_ok(url: str, marketplace: str) -> bool:
    lowered = (url or "").lower()
    if marketplace == "ozon":
        return "ozon.ru" in lowered and "/product/" in lowered and not any(
            marker in lowered for marker in ("/question", "/questions", "/reviews", "/otzyvy")
        )
    if marketplace == "yandex":
        return "market.yandex" in lowered and ("/card/" in lowered or "/product/" in lowered)
    return True


def _model_tokens(value: str) -> set[str]:
    normalized = _normalize_text(value)
    models = set(re.findall(r"\b[a-zа-я]{1,8}[- ]?\d{1,5}[a-zа-я0-9-]*\b", normalized, re.IGNORECASE))
    models.update(re.findall(r"\b\d{1,5}[a-zа-я]{1,6}\b", normalized, re.IGNORECASE))
    models.update(re.findall(r"\b\d{2,5}\b", normalized))
    cleaned = set()
    for item in models:
        model = item.replace(" ", "").replace("-", "")
        if re.match(r"^(?:и|в|на|для|с)\d+$", model):
            continue
        cleaned.add(model)
    return cleaned


def _clean_title(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    value = re.sub(r"^[{\"'\s]+|[}\"'\s]+$", "", value)
    return value[:220].strip()


def _is_bad_title(value: str) -> bool:
    lowered = (value or "").lower()
    return (
        not value
        or len(value) < 5
        or value.startswith("{")
        or "widgets" in lowered
        or "@marketfront" in lowered
        or "serplayoutitem" in lowered
    )


def _canonical_url(url: str, base_url: str) -> str:
    full = urljoin(base_url, url)
    parsed = urlparse(full)
    return parsed._replace(query="", fragment="").geturl()


def _cache_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _safe_get_cache(cache_key: str) -> dict | None:
    try:
        return await get_price_compare_cache(cache_key)
    except Exception as error:
        log_api_error(f"Price compare cache read failed: key={cache_key}, error={error}")
        return None


async def _safe_set_cache(cache_key: str, payload: dict, ttl_seconds: int) -> None:
    try:
        await set_price_compare_cache(cache_key, payload, ttl_seconds)
    except Exception as error:
        log_api_error(f"Price compare cache write failed: key={cache_key}, error={error}")


def _safe_json_loads(value: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _extract_price_from_text(text: str) -> float | None:
    if not text:
        return None
    candidates = []
    for match in re.finditer(
        r"(?:от\s*)?(\d{1,3}(?:[\s\u00a0]\d{3})+|\d{2,7})(?:[,.]\d{1,2})?\s*(?:₽|руб)",
        text,
        flags=re.IGNORECASE,
    ):
        price = _as_float(match.group(0))
        if price and 10 <= price <= 10_000_000:
            candidates.append(price)
    if not candidates:
        return None
    return min(candidates)


def _extract_old_price(text: str, current_price: float | None) -> float | None:
    if current_price is None:
        return None
    prices = []
    for match in re.finditer(
        r"(\d{1,3}(?:[\s\u00a0]\d{3})+|\d{2,7})(?:[,.]\d{1,2})?\s*(?:₽|руб)",
        text or "",
        flags=re.IGNORECASE,
    ):
        price = _as_float(match.group(0))
        if price and price > current_price:
            prices.append(price)
    return min(prices) if prices else None


def _extract_rating_reviews_from_text(text: str) -> tuple[float | None, int | None]:
    normalized = " ".join((text or "").split())
    review_match = re.search(
        r"(\d[\d\s\u00a0]{0,8})\s*(?:отзыв(?:а|ов)?|оцен(?:ка|ки|ок))",
        normalized,
        flags=re.IGNORECASE,
    )
    review_count = _parse_review_count(review_match.group(1)) if review_match else None
    rating = None
    rating_patterns = (
        r"(?:рейтинг|оценка)\s*[:—-]?\s*([1-5](?:[.,]\d{1,2})?)",
        r"([1-5][.,]\d{1,2})\s*(?:★|⭐)",
    )
    for pattern in rating_patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            rating = _as_float(match.group(1))
            break
    if rating is None and review_match:
        prefix = normalized[max(0, review_match.start() - 35):review_match.start()]
        nearby = re.search(r"([1-5][.,]\d{1,2})\D{0,20}$", prefix)
        if nearby:
            rating = _as_float(nearby.group(1))
    if rating is not None and not (0 < rating <= 5):
        rating = None
    return rating, review_count


def _iter_json_objects(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_objects(child)


def _json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _first_json_text(item: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = _first_json_text(value, keys)
            if nested:
                return nested
    return ""


def _first_json_price(item: dict) -> float | None:
    price_keys = (
        "price",
        "finalPrice",
        "currentPrice",
        "cardPrice",
        "salePrice",
        "priceWithDiscount",
        "minPrice",
        "value",
    )
    for key in price_keys:
        if key not in item:
            continue
        value = item.get(key)
        if isinstance(value, dict):
            price = _first_json_price(value)
        else:
            price = _as_float(value)
        if price:
            return price
    return None


def _first_json_url(item: dict, marketplace: str) -> str:
    url_keys = ("url", "link", "href", "action", "deeplink")
    base_url = "https://www.ozon.ru" if marketplace == "ozon" else "https://market.yandex.ru"
    for key in url_keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            candidate = value.strip()
            if marketplace == "ozon" and ("ozon.ru" in candidate or "/product/" in candidate):
                return _canonical_url(candidate, base_url)
            if marketplace == "yandex" and ("market.yandex.ru" in candidate or "/card/" in candidate or "/product/" in candidate):
                return _canonical_url(candidate, base_url)
        if isinstance(value, dict):
            nested = _first_json_url(value, marketplace)
            if nested:
                return nested
    return ""


def _extract_products_from_json(payload: Any, marketplace: str) -> list[ComparableProduct]:
    products: list[ComparableProduct] = []
    seen = set()
    for item in _iter_json_objects(payload):
        title = _clean_title(
            _first_json_text(
                item,
                ("title", "name", "productName", "text", "header", "semanticTitle", "label"),
            )
        )
        if _is_bad_title(title):
            continue
        url = _first_json_url(item, marketplace)
        if not url or url in seen or not _marketplace_product_url_ok(url, marketplace):
            continue
        price = _first_json_price(item)
        if not price:
            compact_text = " ".join(_json_text(value) for value in item.values())
            price = _extract_price_from_text(compact_text)
        if not price:
            continue
        image_url = _first_json_text(item, ("image", "imageUrl", "picture", "cover", "src"))
        rating = _as_float(item.get("rating") or item.get("reviewRating"))
        review_count_raw = item.get("reviews") or item.get("reviewCount") or item.get("feedbacks")
        review_count = None
        try:
            review_count = int(review_count_raw) if review_count_raw not in (None, "") else None
        except (TypeError, ValueError):
            review_count = None
        seen.add(url)
        products.append(
            ComparableProduct(
                marketplace=marketplace,
                title=title,
                price=price,
                url=url,
                image_url=image_url if str(image_url).startswith("http") else "",
                rating=rating,
                review_count=review_count,
                category=_first_json_text(item, ("category", "categoryName", "type")),
                brand=_first_json_text(item, ("brand", "brandName")),
                raw={"source": "json"},
            )
        )
        if len(products) >= int(settings.PRICE_COMPARE_SEARCH_RESULTS_LIMIT or 12):
            break
    return products


def _extract_external_products(payload: Any, marketplace: str) -> list[ComparableProduct]:
    if isinstance(payload, dict):
        items = payload.get("items") or payload.get("results") or payload.get("products") or []
    else:
        items = payload
    products: list[ComparableProduct] = []
    if not isinstance(items, list):
        return []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_marketplace = str(item.get("marketplace") or marketplace).lower()
        if item_marketplace and item_marketplace != marketplace:
            continue
        title = _clean_title(str(item.get("title") or item.get("name") or ""))
        url = str(item.get("url") or item.get("link") or "").strip()
        price = _as_float(item.get("price") or item.get("sale_price") or item.get("current_price"))
        if _is_bad_title(title) or not url or not price:
            continue
        url = _canonical_url(url, "https://www.ozon.ru" if marketplace == "ozon" else "https://market.yandex.ru")
        if not _marketplace_product_url_ok(url, marketplace):
            continue
        old_price = _as_float(item.get("old_price") or item.get("original_price"))
        review_count = item.get("review_count") or item.get("reviews")
        try:
            review_count = int(review_count) if review_count not in (None, "") else None
        except (TypeError, ValueError):
            review_count = None
        products.append(
            ComparableProduct(
                marketplace=marketplace,
                title=title,
                price=price,
                url=url,
                old_price=old_price,
                image_url=str(item.get("image_url") or item.get("image") or ""),
                rating=_as_float(item.get("rating")),
                review_count=review_count,
                category=str(item.get("category") or ""),
                brand=str(item.get("brand") or ""),
                raw={"source": "external"},
            )
        )
        if len(products) >= int(settings.PRICE_COMPARE_SEARCH_RESULTS_LIMIT or 12):
            break
    return products


async def search_external_marketplace(marketplace: str, query: str) -> list[ComparableProduct]:
    endpoint = (settings.PRICE_COMPARE_EXTERNAL_SEARCH_URL or "").strip()
    if not endpoint:
        return []

    headers = {"Accept": "application/json"}
    if settings.PRICE_COMPARE_EXTERNAL_SEARCH_KEY:
        headers["Authorization"] = f"Bearer {settings.PRICE_COMPARE_EXTERNAL_SEARCH_KEY}"
        headers["X-API-Key"] = settings.PRICE_COMPARE_EXTERNAL_SEARCH_KEY

    timeout = float(settings.PRICE_COMPARE_TIMEOUT_SECONDS or 20)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        if "{query}" in endpoint or "{marketplace}" in endpoint:
            url = endpoint.format(marketplace=quote_plus(marketplace), query=quote_plus(query))
            response = await client.get(url, headers=headers)
        else:
            response = await client.post(
                endpoint,
                headers=headers,
                json={"marketplace": marketplace, "query": query, "limit": int(settings.PRICE_COMPARE_SEARCH_RESULTS_LIMIT or 12)},
            )
        response.raise_for_status()
        payload = response.json()
    return _extract_external_products(payload, marketplace)


def _extract_search_api_products(payload: Any, marketplace: str, source: str) -> list[ComparableProduct]:
    if not isinstance(payload, dict):
        return []

    if source == "brave_api":
        items = ((payload.get("web") or {}).get("results") or [])
    elif source == "serpapi":
        items = payload.get("organic_results") or payload.get("shopping_results") or []
    else:
        items = []

    products: list[ComparableProduct] = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _clean_title(str(item.get("title") or item.get("name") or ""))
        url = str(item.get("url") or item.get("link") or "").strip()
        snippet = " ".join(
            str(part or "")
            for part in (
                item.get("description"),
                item.get("snippet"),
                item.get("price"),
                item.get("extracted_price"),
            )
        )
        if marketplace == "ozon" and "ozon.ru" not in url.lower():
            continue
        if marketplace == "yandex" and "market.yandex" not in url.lower():
            continue
        price = _as_float(item.get("extracted_price")) or _extract_price_from_text(snippet)
        if _is_bad_title(title) or not url or not price:
            continue
        canonical_url = _canonical_url(url, "https://www.ozon.ru" if marketplace == "ozon" else "https://market.yandex.ru")
        if canonical_url in seen or not _marketplace_product_url_ok(canonical_url, marketplace):
            continue
        seen.add(canonical_url)
        rating, review_count = _extract_rating_reviews_from_text(snippet)
        products.append(
            ComparableProduct(
                marketplace=marketplace,
                title=title,
                price=price,
                url=canonical_url,
                image_url=str(item.get("thumbnail") or item.get("image") or ""),
                rating=rating,
                review_count=review_count,
                raw={"source": source},
            )
        )
        if len(products) >= int(settings.PRICE_COMPARE_SEARCH_RESULTS_LIMIT or 12):
            break
    return products


async def search_via_brave_api(marketplace: str, query: str) -> list[ComparableProduct]:
    api_key = (settings.PRICE_COMPARE_BRAVE_SEARCH_API_KEY or "").strip()
    if not api_key:
        return []
    site = "ozon.ru/product" if marketplace == "ozon" else "market.yandex.ru"
    search_query = f"site:{site} {query} ₽"
    params = {
        "q": search_query,
        "count": min(20, int(settings.PRICE_COMPARE_SEARCH_RESULTS_LIMIT or 12)),
        "country": "ru",
        "search_lang": "ru",
    }
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
    }
    async with httpx.AsyncClient(timeout=float(settings.PRICE_COMPARE_TIMEOUT_SECONDS or 20)) as client:
        response = await client.get("https://api.search.brave.com/res/v1/web/search", params=params, headers=headers)
        response.raise_for_status()
        return _extract_search_api_products(response.json(), marketplace, "brave_api")


async def search_via_serpapi(marketplace: str, query: str) -> list[ComparableProduct]:
    api_key = (settings.PRICE_COMPARE_SERPAPI_KEY or "").strip()
    if not api_key:
        return []
    site = "ozon.ru/product" if marketplace == "ozon" else "market.yandex.ru"
    params = {
        "engine": "google",
        "q": f"site:{site} {query} ₽",
        "hl": "ru",
        "gl": "ru",
        "api_key": api_key,
        "num": min(20, int(settings.PRICE_COMPARE_SEARCH_RESULTS_LIMIT or 12)),
    }
    async with httpx.AsyncClient(timeout=float(settings.PRICE_COMPARE_TIMEOUT_SECONDS or 20)) as client:
        response = await client.get("https://serpapi.com/search.json", params=params)
        response.raise_for_status()
        return _extract_search_api_products(response.json(), marketplace, "serpapi")


async def search_production_search_apis(marketplace: str, query: str) -> tuple[list[ComparableProduct], list[str]]:
    errors: list[str] = []
    for name, searcher in (("brave_api", search_via_brave_api), ("serpapi", search_via_serpapi)):
        try:
            products = await searcher(marketplace, query)
            if products:
                return products, errors
        except Exception as error:
            errors.append(f"{marketplace} {name}: {error}")
    return [], errors


def build_search_query(product: WBProduct) -> str:
    identity = build_identity(
        product.title,
        product.brand,
        " ".join(part for part in (product.subject_name, product.category_name) if part),
        product.description,
        product.attributes,
    )
    precise_query = build_precise_query(identity)
    if precise_query:
        return precise_query

    source_parts = [product.brand, product.title, product.subject_name, product.category_name, product.description]
    source = " ".join(part for part in source_parts if part)
    title_tokens = _tokens(product.title)
    subject_tokens = _tokens(" ".join(part for part in (product.subject_name, product.category_name) if part))
    models = sorted(_model_tokens(source), key=lambda item: (len(item), item))
    base_tokens = set(brand_tokens := _tokens(product.brand)) | set(title_tokens) | set(models)
    description_tokens = []
    for token in _tokens(product.description):
        if len(token) < 4 or token in base_tokens or token in description_tokens:
            continue
        description_tokens.append(token)
    description_tokens.sort(key=lambda token: (not bool(re.search(r"[a-z0-9]", token)), len(token)))

    selected: list[str] = []
    for token in brand_tokens + title_tokens + models + description_tokens[:2] + subject_tokens:
        if token not in selected:
            selected.append(token)
        if len(selected) >= 10:
            break

    if not selected:
        selected = _tokens(product.title)[:8]
    return " ".join(selected).strip() or product.title[:120]


def build_search_queries(product: WBProduct) -> list[str]:
    primary = build_search_query(product)
    identity = build_identity(
        product.title,
        product.brand,
        " ".join(part for part in (product.subject_name, product.category_name) if part),
        product.description,
        product.attributes,
    )
    relaxed_parts = matcher_tokens(identity.brand)
    relaxed_parts += matcher_tokens(TYPE_QUERY_TERMS.get(identity.product_type, ""))
    relaxed_parts += sorted(identity.models) + sorted(identity.model_numbers) + sorted(identity.qualifiers)
    relaxed_parts += matcher_tokens(product.title)
    selected: list[str] = []
    for token in relaxed_parts:
        if token and token not in selected:
            selected.append(token)
        if len(selected) >= 8:
            break
    relaxed = " ".join(selected)
    return [query for index, query in enumerate((primary, relaxed)) if query and query not in (primary, relaxed)[:index]]


def _match_candidate(source: WBProduct, candidate: ComparableProduct, require_variant_evidence: bool = True):
    source_identity = build_identity(
        source.title,
        source.brand,
        " ".join(part for part in (source.subject_name, source.category_name) if part),
        source.description,
        source.attributes,
    )
    candidate_identity = build_identity(
        candidate.title,
        candidate.brand,
        candidate.category,
        attributes=candidate.raw.get("attributes") if isinstance(candidate.raw, dict) else None,
    )
    visual_similarity = candidate.raw.get("visual_similarity") if isinstance(candidate.raw, dict) else None
    return match_identities(
        source_identity,
        candidate_identity,
        visual_similarity=visual_similarity,
        min_score=int(settings.PRICE_COMPARE_MIN_SIMILARITY or 58),
        require_variant_evidence=require_variant_evidence,
    )


def _prefilter_candidate(source: WBProduct, candidate: ComparableProduct) -> bool:
    source_identity = build_identity(
        source.title,
        source.brand,
        " ".join(part for part in (source.subject_name, source.category_name) if part),
        source.description,
        source.attributes,
    )
    candidate_identity = build_identity(candidate.title, candidate.brand, candidate.category)
    match = match_identities(
        source_identity,
        candidate_identity,
        min_score=32,
        require_variant_evidence=False,
        allow_missing_evidence=True,
    )
    return match.score >= 32 and not match.reject_reasons


def _score_similarity(source: WBProduct, candidate: ComparableProduct) -> int:
    return _match_candidate(source, candidate).score


def _similarity_label(score: int) -> str:
    if score >= 75:
        return "высокое"
    if score >= 55:
        return "хорошее"
    if score >= settings.PRICE_COMPARE_MIN_SIMILARITY:
        return "среднее"
    return "низкое"


def _diff_relation(diff: float) -> str:
    if abs(diff) < 1:
        return "примерно как WB"
    return "дешевле WB" if diff < 0 else "дороже WB"


def _to_item(source: WBProduct, wb_price: float, candidate: ComparableProduct) -> PriceComparisonItem | None:
    if not candidate.title or not candidate.url or candidate.price is None:
        return None
    if not _marketplace_product_url_ok(candidate.url, candidate.marketplace):
        return None
    match = _match_candidate(source, candidate)
    candidate.raw["match"] = {
        "accepted": match.accepted,
        "level": match.level,
        "reasons": match.reject_reasons,
        "evidence": match.evidence,
        "components": match.components,
    }
    if not match.accepted:
        return None
    diff = round(candidate.price - wb_price, 2)
    percent = round(diff / wb_price * 100, 1) if wb_price else 0.0
    return PriceComparisonItem(
        product=candidate,
        similarity=match.score,
        similarity_label=match.level,
        price_diff=diff,
        price_diff_percent=percent,
        relation=_diff_relation(diff),
    )


def _sort_items(items: list[PriceComparisonItem]) -> list[PriceComparisonItem]:
    return sorted(
        items,
        key=lambda item: (
            -item.similarity,
            item.product.price if item.product.price is not None else 10_000_000,
            -(item.product.rating or 0),
            -(item.product.review_count or 0),
            item.price_diff,
        ),
    )


def _merge_candidates(*groups: list[ComparableProduct]) -> list[ComparableProduct]:
    merged: list[ComparableProduct] = []
    seen: set[str] = set()
    for group in groups:
        for candidate in group:
            key = candidate.url or f"{candidate.marketplace}:{candidate.title}:{candidate.price}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(candidate)
    return merged[: int(settings.PRICE_COMPARE_SEARCH_RESULTS_LIMIT or 12) * 2]


def _is_json_ld_product(item: dict) -> bool:
    item_type = item.get("@type")
    types = item_type if isinstance(item_type, list) else [item_type]
    return any(str(value or "").lower() == "product" for value in types)


def _extract_page_product_metadata(html: str) -> dict[str, Any]:
    soup = BeautifulSoup(html or "", "html.parser")
    product = None
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        payload = _safe_json_loads(script.string or script.get_text("", strip=True))
        for item in _iter_json_objects(payload):
            if _is_json_ld_product(item):
                product = item
                break
        if product:
            break
    if not product:
        return {}

    brand_value = product.get("brand")
    if isinstance(brand_value, dict):
        brand = _first_json_text(brand_value, ("name", "brand"))
    else:
        brand = _json_text(brand_value)

    image_value = product.get("image")
    if isinstance(image_value, list):
        image_value = next((value for value in image_value if isinstance(value, str)), "")
    elif isinstance(image_value, dict):
        image_value = _first_json_text(image_value, ("url", "contentUrl"))

    aggregate = product.get("aggregateRating") if isinstance(product.get("aggregateRating"), dict) else {}
    offers = product.get("offers")
    if isinstance(offers, list):
        offers = next((offer for offer in offers if isinstance(offer, dict)), {})
    if not isinstance(offers, dict):
        offers = {}
    attributes = {
        key: _json_text(product.get(key))
        for key in ("sku", "mpn", "model", "color", "size", "material")
        if _json_text(product.get(key))
    }
    return {
        "title": _clean_title(_json_text(product.get("name"))),
        "brand": _clean_title(brand),
        "category": _clean_title(_json_text(product.get("category"))),
        "image_url": _json_text(image_value),
        "price": _first_json_price(offers),
        "rating": _as_float(aggregate.get("ratingValue")),
        "review_count": _parse_review_count(
            aggregate.get("reviewCount") or aggregate.get("ratingCount")
        ),
        "attributes": attributes,
    }


def _apply_candidate_metadata(candidate: ComparableProduct, metadata: dict[str, Any]) -> None:
    if metadata.get("title") and not _is_bad_title(metadata["title"]):
        candidate.title = metadata["title"]
    if metadata.get("brand"):
        candidate.brand = metadata["brand"]
    if metadata.get("category"):
        candidate.category = metadata["category"]
    if metadata.get("image_url", "").startswith("http"):
        candidate.image_url = metadata["image_url"]
    if metadata.get("rating"):
        candidate.rating = metadata["rating"]
    if metadata.get("review_count") is not None:
        candidate.review_count = metadata["review_count"]
    if metadata.get("price") and candidate.price is None:
        candidate.price = metadata["price"]
    candidate.raw["attributes"] = metadata.get("attributes") or {}
    candidate.raw["metadata_enriched"] = True


async def _enrich_candidate_page(client: httpx.AsyncClient, candidate: ComparableProduct) -> None:
    if not _marketplace_product_url_ok(candidate.url, candidate.marketplace):
        return
    cache_key = "price_metadata:v1:" + _cache_hash(candidate.url)
    cached = await _safe_get_cache(cache_key)
    if cached:
        if cached.get("found") and isinstance(cached.get("metadata"), dict):
            _apply_candidate_metadata(candidate, cached["metadata"])
            candidate.raw["metadata_cache"] = True
        else:
            candidate.raw["metadata_error"] = cached.get("error") or "metadata unavailable"
        return
    try:
        html = await _fetch_html(client, candidate.url)
        metadata = _extract_page_product_metadata(html)
    except Exception as error:
        candidate.raw["metadata_error"] = str(error)[:160]
        await _safe_set_cache(
            cache_key,
            {"found": False, "error": candidate.raw["metadata_error"]},
            int(settings.PRICE_COMPARE_METADATA_NEGATIVE_CACHE_TTL_SECONDS or 3600),
        )
        return
    if not metadata:
        candidate.raw["metadata_error"] = "structured product metadata not found"
        await _safe_set_cache(
            cache_key,
            {"found": False, "error": candidate.raw["metadata_error"]},
            int(settings.PRICE_COMPARE_METADATA_NEGATIVE_CACHE_TTL_SECONDS or 3600),
        )
        return
    _apply_candidate_metadata(candidate, metadata)
    await _safe_set_cache(
        cache_key,
        {"found": True, "metadata": metadata},
        int(settings.PRICE_COMPARE_METADATA_CACHE_TTL_SECONDS or 86400),
    )


async def _enrich_candidate_pages(source: WBProduct, candidates: list[ComparableProduct]) -> None:
    if not settings.PRICE_COMPARE_METADATA_ENRICHMENT_ENABLED:
        return
    selected = [
        candidate
        for candidate in candidates
        if candidate.raw.get("source") and _prefilter_candidate(source, candidate)
    ][: int(settings.PRICE_COMPARE_METADATA_MAX_CANDIDATES or 8)]
    if not selected:
        return
    timeout = httpx.Timeout(float(settings.PRICE_COMPARE_METADATA_TIMEOUT_SECONDS or 8))
    limits = httpx.Limits(max_connections=len(selected), max_keepalive_connections=4)
    async with httpx.AsyncClient(timeout=timeout, headers=PRICE_HEADERS, follow_redirects=True, limits=limits) as client:
        await asyncio.gather(*(_enrich_candidate_page(client, candidate) for candidate in selected))


async def _load_market_cache(marketplace: str, query: str) -> list[ComparableProduct] | None:
    cache_key = "price_search:v11:" + marketplace + ":" + _cache_hash(query)
    cached = await _safe_get_cache(cache_key)
    if not cached:
        return None
    products = []
    for item in cached.get("items") or []:
        try:
            products.append(ComparableProduct(**item))
        except TypeError:
            continue
    return products


async def _store_market_cache(marketplace: str, query: str, products: list[ComparableProduct]):
    cache_key = "price_search:v11:" + marketplace + ":" + _cache_hash(query)
    await _safe_set_cache(
        cache_key,
        {"items": [asdict(product) for product in products]},
        int(settings.PRICE_COMPARE_CACHE_TTL_SECONDS or 21600),
    )


async def _fetch_html(client: httpx.AsyncClient, url: str, referer: str = "") -> str:
    headers = dict(PRICE_HEADERS)
    if referer:
        headers["Referer"] = referer
    attempts = max(1, int(settings.PRICE_COMPARE_HTTP_RETRIES or 1))
    last_error = None
    for attempt in range(attempts):
        try:
            response = await client.get(url, headers=headers)
            if response.status_code in {403, 429, 503}:
                raise PriceCompareError(f"source returned status {response.status_code}")
            response.raise_for_status()
            return response.text
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts:
                await asyncio.sleep(0.35 + attempt * 0.35)
    raise PriceCompareError(str(last_error) if last_error else "source request failed")


async def _fetch_html_with_browser(url: str, referer: str = "") -> str:
    if not settings.PRICE_COMPARE_BROWSER_FALLBACK_ENABLED:
        raise PriceCompareError("browser fallback disabled")
    try:
        from playwright.async_api import async_playwright
    except Exception as error:
        raise PriceCompareError(f"browser fallback unavailable: {error}") from error

    timeout_ms = max(5, int(settings.PRICE_COMPARE_BROWSER_TIMEOUT_SECONDS or 15)) * 1000
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            locale="ru-RU",
            user_agent=PRICE_HEADERS["User-Agent"],
            viewport={"width": 1365, "height": 768},
            extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"},
        )
        page = await context.new_page()
        try:
            if referer:
                await page.goto(referer, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 6000))
            except Exception:
                pass
            await page.wait_for_timeout(1200)
            content = await page.content()
            if "incidentId" in content and "supportURL" in content:
                raise PriceCompareError("browser received anti-bot page")
            return content
        finally:
            await context.close()
            await browser.close()


def _parse_market_like_cards(
    html: str,
    marketplace: str,
    base_url: str,
    link_markers: tuple[str, ...],
) -> list[ComparableProduct]:
    soup = BeautifulSoup(html, "html.parser")
    products: list[ComparableProduct] = []
    seen_urls = set()

    for link in soup.find_all("a", href=True):
        href = str(link.get("href") or "")
        if not any(marker in href for marker in link_markers):
            continue
        url = _canonical_url(href, base_url)
        if url in seen_urls or not _marketplace_product_url_ok(url, marketplace):
            continue

        title = _clean_title(link.get_text(" ", strip=True))
        if _is_bad_title(title):
            title_attr = link.get("title") or link.get("aria-label") or ""
            title = _clean_title(str(title_attr))

        node = link
        container_text = ""
        for _ in range(7):
            if node is None:
                break
            text = node.get_text(" ", strip=True)
            if text and ("₽" in text or "руб" in text.lower()):
                container_text = text
                break
            node = node.parent

        if _is_bad_title(title) and container_text:
            title = _clean_title(container_text.split("₽", 1)[0])
        if _is_bad_title(title):
            continue

        price = _extract_price_from_text(container_text)
        if price is None:
            continue
        rating, review_count = _extract_rating_reviews_from_text(container_text)
        image_url = ""
        image = node.find("img") if node and hasattr(node, "find") else None
        if image:
            raw_image = image.get("src") or image.get("data-src") or image.get("data-original") or ""
            if raw_image:
                image_url = urljoin(base_url, str(raw_image))

        seen_urls.add(url)
        products.append(
            ComparableProduct(
                marketplace=marketplace,
                title=title,
                price=price,
                old_price=_extract_old_price(container_text, price),
                url=url,
                image_url=image_url,
                rating=rating,
                review_count=review_count,
                raw={"source": "html_card"},
            )
        )
        if len(products) >= int(settings.PRICE_COMPARE_SEARCH_RESULTS_LIMIT or 12):
            break

    return products


def _extract_yandex_embedded_offers(html: str) -> list[ComparableProduct]:
    products: list[ComparableProduct] = []
    seen_urls = set()
    for match in re.finditer(r'"direct":"(?P<url>//market\.yandex\.ru/(?:product|card)/[^"]+)".{0,1600}?"price"\s*:\s*(?P<price>\d{2,9})', html):
        url = "https:" + match.group("url").encode("utf-8").decode("unicode_escape")
        url = _canonical_url(url, "https://market.yandex.ru")
        if url in seen_urls:
            continue
        window = html[max(0, match.start() - 2000):match.end() + 2000]
        title_match = re.search(r'"title"\s*:\s*"([^"]{5,220})"', window)
        title = _clean_title(title_match.group(1).encode("utf-8").decode("unicode_escape") if title_match else "")
        price = _as_float(match.group("price"))
        if title and price:
            seen_urls.add(url)
            products.append(ComparableProduct("yandex", title, price, url, raw={"source": "embedded"}))
    return products


async def search_yandex_market(query: str) -> tuple[list[ComparableProduct], bool, str]:
    cached = await _load_market_cache("yandex", query)
    if cached is not None:
        return cached, True, ""

    errors = []
    try:
        products = await search_external_marketplace("yandex", query)
        if products:
            await _store_market_cache("yandex", query, products)
            return products, False, ""
    except Exception as error:
        errors.append(f"Yandex external: {error}")

    api_products, api_errors = await search_production_search_apis("yandex", query)
    errors.extend(api_errors)
    if api_products:
        await _store_market_cache("yandex", query, api_products)
        return api_products, False, "; ".join(errors)

    url = f"https://market.yandex.ru/search?text={quote_plus(query)}"
    html = ""
    async with httpx.AsyncClient(timeout=float(settings.PRICE_COMPARE_TIMEOUT_SECONDS or 20), follow_redirects=True) as client:
        try:
            html = await _fetch_html(client, url, referer="https://market.yandex.ru/")
        except Exception as error:
            errors.append(f"Yandex direct: {error}")
            try:
                html = await _fetch_html_with_browser(url, referer="https://market.yandex.ru/")
            except Exception as browser_error:
                errors.append(f"Yandex browser: {browser_error}")
                return [], False, "; ".join(errors)

    products = _parse_market_like_cards(
        html,
        "yandex",
        "https://market.yandex.ru",
        ("/card/", "/product/"),
    )
    embedded = _extract_yandex_embedded_offers(html)
    json_payload = _safe_json_loads(html)
    json_products = _extract_products_from_json(json_payload, "yandex") if json_payload is not None else []
    seen = {product.url for product in products}
    for product in embedded + json_products:
        if product.url not in seen:
            products.append(product)
            seen.add(product.url)

    products = products[: int(settings.PRICE_COMPARE_SEARCH_RESULTS_LIMIT or 12)]
    await _store_market_cache("yandex", query, products)
    return products, False, "; ".join(errors)


def _unwrap_duckduckgo_url(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if params.get("uddg"):
        return unquote(params["uddg"][0])
    return url


def _parse_duckduckgo_ozon(html: str) -> list[ComparableProduct]:
    soup = BeautifulSoup(html, "html.parser")
    products: list[ComparableProduct] = []
    seen_urls = set()
    for link in soup.select("a.result__a, a[href]"):
        href = _unwrap_duckduckgo_url(str(link.get("href") or ""))
        if "ozon.ru" not in href:
            continue
        title = _clean_title(link.get_text(" ", strip=True))
        if not title or len(title) < 5:
            continue
        container = link
        for _ in range(4):
            if container and container.parent:
                container = container.parent
        text = container.get_text(" ", strip=True) if container else title
        price = _extract_price_from_text(text)
        if not price:
            continue
        rating, review_count = _extract_rating_reviews_from_text(text)
        url = _canonical_url(href, "https://www.ozon.ru")
        if url in seen_urls or not _marketplace_product_url_ok(url, "ozon"):
            continue
        seen_urls.add(url)
        products.append(
            ComparableProduct(
                marketplace="ozon",
                title=title,
                price=price,
                url=url,
                rating=rating,
                review_count=review_count,
                raw={"source": "duckduckgo"},
            )
        )
        if len(products) >= int(settings.PRICE_COMPARE_SEARCH_RESULTS_LIMIT or 12):
            break
    return products


def _extract_search_result_url(raw_url: str) -> str:
    raw_url = _unwrap_duckduckgo_url(raw_url)
    parsed = urlparse(raw_url)
    params = parse_qs(parsed.query)
    for key in ("url", "u", "r"):
        if params.get(key):
            return unquote(params[key][0])
    return raw_url


def _parse_search_engine_ozon(html: str, engine: str) -> list[ComparableProduct]:
    soup = BeautifulSoup(html, "html.parser")
    products: list[ComparableProduct] = []
    seen_urls = set()
    selectors = [
        'div[data-type="web"]',
        "div.snippet",
        "li.b_algo",
        "div.result",
        "article",
    ]
    nodes = []
    for selector in selectors:
        nodes.extend(soup.select(selector))
    if not nodes:
        nodes = soup.find_all("a", href=True)

    for node in nodes:
        link = node.find("a", href=True) if hasattr(node, "find") else node
        if not link:
            continue
        href = _extract_search_result_url(str(link.get("href") or ""))
        text = " ".join(node.get_text(" ", strip=True).split()) if hasattr(node, "get_text") else ""
        title = _clean_title(link.get_text(" ", strip=True) or text)
        if "ozon.ru" not in href.lower() and "ozon" not in text.lower():
            continue
        if "/product/" not in href.lower() and "ozon.ru/product" not in text.lower():
            continue
        url = _canonical_url(href, "https://www.ozon.ru")
        if "ozon.ru" not in url.lower() or url in seen_urls or not _marketplace_product_url_ok(url, "ozon"):
            continue
        if _is_bad_title(title):
            title = _clean_title(re.sub(r"^OZON\s+", "", text, flags=re.IGNORECASE))
        price = _extract_price_from_text(text)
        if not title or not price:
            continue
        rating, review_count = _extract_rating_reviews_from_text(text)
        image_url = ""
        image = node.find("img") if hasattr(node, "find") else None
        if image:
            image_url = str(image.get("src") or image.get("data-src") or "")
            if image_url.startswith("//"):
                image_url = "https:" + image_url
        seen_urls.add(url)
        products.append(
            ComparableProduct(
                marketplace="ozon",
                title=title,
                price=price,
                url=url,
                image_url=image_url if image_url.startswith("http") else "",
                rating=rating,
                review_count=review_count,
                raw={"source": engine},
            )
        )
        if len(products) >= int(settings.PRICE_COMPARE_SEARCH_RESULTS_LIMIT or 12):
            break
    return products


async def search_ozon_via_search_engines(client: httpx.AsyncClient, query: str) -> tuple[list[ComparableProduct], list[str]]:
    if not settings.PRICE_COMPARE_SEARCH_ENGINE_FALLBACK_ENABLED:
        return [], ["search engine fallback disabled"]
    errors: list[str] = []
    searches = [
        ("brave", f"https://search.brave.com/search?q={quote_plus('site:ozon.ru/product ' + query + ' ₽')}"),
        ("bing", f"https://www.bing.com/search?q={quote_plus('site:ozon.ru/product ' + query + ' ₽')}"),
        ("duckduckgo", f"https://duckduckgo.com/html/?q={quote_plus('site:ozon.ru/product ' + query + ' ₽')}"),
    ]
    for engine, url in searches:
        try:
            html = await _fetch_html(client, url, referer="https://www.google.com/")
            products = _parse_search_engine_ozon(html, engine)
            if not products and engine == "duckduckgo":
                products = _parse_duckduckgo_ozon(html)
            if products:
                return products, errors
        except Exception as error:
            errors.append(f"Ozon {engine}: {error}")
    return [], errors


async def search_ozon(query: str) -> tuple[list[ComparableProduct], bool, str]:
    cached = await _load_market_cache("ozon", query)
    if cached is not None:
        return cached, True, ""

    products: list[ComparableProduct] = []
    errors = []
    try:
        products = await search_external_marketplace("ozon", query)
        if products:
            await _store_market_cache("ozon", query, products)
            return products, False, ""
    except Exception as error:
        errors.append(f"Ozon external: {error}")

    api_products, api_errors = await search_production_search_apis("ozon", query)
    errors.extend(api_errors)
    if api_products:
        await _store_market_cache("ozon", query, api_products)
        return api_products, False, "; ".join(errors)

    async with httpx.AsyncClient(timeout=float(settings.PRICE_COMPARE_TIMEOUT_SECONDS or 20), follow_redirects=True) as client:
        api_url = f"https://www.ozon.ru/api/composer-api.bx/page/json/v2?url={quote_plus('/search/?text=' + query)}"
        try:
            json_text = await _fetch_html(client, api_url, referer="https://www.ozon.ru/")
            payload = _safe_json_loads(json_text)
            if payload is not None:
                products = _extract_products_from_json(payload, "ozon")
        except Exception as error:
            errors.append(f"Ozon api: {error}")

        direct_url = f"https://www.ozon.ru/search/?text={quote_plus(query)}"
        if not products:
            try:
                html = await _fetch_html(client, direct_url, referer="https://www.ozon.ru/")
                payload = _safe_json_loads(html)
                if payload is not None:
                    products = _extract_products_from_json(payload, "ozon")
                if not products:
                    products = _parse_market_like_cards(
                        html,
                        "ozon",
                        "https://www.ozon.ru",
                        ("/product/",),
                    )
            except Exception as error:
                errors.append(f"Ozon direct: {error}")

        if not products:
            try:
                html = await _fetch_html_with_browser(direct_url, referer="https://www.ozon.ru/")
                payload = _safe_json_loads(html)
                if payload is not None:
                    products = _extract_products_from_json(payload, "ozon")
                if not products:
                    products = _parse_market_like_cards(
                        html,
                        "ozon",
                        "https://www.ozon.ru",
                        ("/product/",),
                    )
            except Exception as error:
                errors.append(f"Ozon browser: {error}")

        if not products:
            try:
                ddg_url = f"https://duckduckgo.com/html/?q={quote_plus('site:ozon.ru ' + query)}"
                html = await _fetch_html(client, ddg_url, referer="https://duckduckgo.com/")
                products = _parse_market_like_cards(
                    html,
                    "ozon",
                    "https://www.ozon.ru",
                    ("ozon.ru/product/", "/product/"),
                )
            except Exception as error:
                errors.append(f"Ozon fallback: {error}")

        if not products:
            search_products, search_errors = await search_ozon_via_search_engines(client, query)
            products = search_products
            errors.extend(search_errors)

        if not products:
            try:
                ddg_url = f"https://duckduckgo.com/html/?q={quote_plus('site:ozon.ru ' + query + ' ₽')}"
                html = await _fetch_html(client, ddg_url, referer="https://duckduckgo.com/")
                products = _parse_duckduckgo_ozon(html)
            except Exception as error:
                errors.append(f"Ozon snippet fallback: {error}")

    products = products[: int(settings.PRICE_COMPARE_SEARCH_RESULTS_LIMIT or 12)]
    if products:
        await _store_market_cache("ozon", query, products)
        return products, False, ""
    return [], False, "; ".join(errors) or "Ozon не вернул товары с ценами"


def _compare_marketplace(
    marketplace: str,
    source: WBProduct,
    wb_price: float,
    candidates: list[ComparableProduct],
    from_cache: bool,
    error: str = "",
) -> MarketplaceComparison:
    items = []
    for candidate in candidates:
        item = _to_item(source, wb_price, candidate)
        if item:
            items.append(item)
    items = _sort_items(items)[: int(settings.PRICE_COMPARE_MAX_RESULTS_PER_MARKETPLACE or 3)]
    return MarketplaceComparison(
        marketplace=marketplace,
        items=items,
        raw_count=len(candidates),
        filtered_count=len(items),
        from_cache=from_cache,
        error=error,
    )


def _perceptual_hash(image_bytes: bytes) -> tuple[int, tuple[float, ...]]:
    if Image is None:
        raise UnidentifiedImageError("Pillow is not installed")
    with Image.open(io.BytesIO(image_bytes)) as image:
        image = image.convert("RGB")
        grayscale = image.resize((9, 8), Image.Resampling.LANCZOS).convert("L")
        pixels = list(grayscale.getdata())
        difference_hash = 0
        for row in range(8):
            for column in range(8):
                left = pixels[row * 9 + column]
                right = pixels[row * 9 + column + 1]
                difference_hash = (difference_hash << 1) | int(left > right)
        histogram_image = image.resize((32, 32), Image.Resampling.BILINEAR)
        histogram = histogram_image.histogram()
        channel_size = 32 * 32
        normalized = tuple(value / channel_size for value in histogram)
        return difference_hash, normalized


def _visual_signature_similarity(
    source: tuple[int, tuple[float, ...]], candidate: tuple[int, tuple[float, ...]]
) -> float:
    hash_similarity = 1.0 - ((source[0] ^ candidate[0]).bit_count() / 64)
    histogram_distance = sum(abs(left - right) for left, right in zip(source[1], candidate[1])) / 6
    histogram_similarity = max(0.0, 1.0 - histogram_distance)
    return round(hash_similarity * 0.72 + histogram_similarity * 0.28, 4)


async def _fetch_visual_signature(client: httpx.AsyncClient, url: str):
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
        body = response.content
        if not body or len(body) > 6_000_000:
            return None
        return await asyncio.to_thread(_perceptual_hash, body)
    except (httpx.HTTPError, OSError, ValueError, UnidentifiedImageError):
        return None


async def _enrich_visual_similarities(product: WBProduct, candidates: list[ComparableProduct]) -> None:
    if Image is None or not settings.PRICE_COMPARE_VISUAL_MATCH_ENABLED or not product.image_url:
        return
    eligible = [
        candidate
        for candidate in candidates
        if candidate.image_url and _match_candidate(product, candidate, require_variant_evidence=False).score >= 45
    ][: int(settings.PRICE_COMPARE_VISUAL_MAX_CANDIDATES or 6)]
    if not eligible:
        return
    timeout = httpx.Timeout(float(settings.PRICE_COMPARE_VISUAL_TIMEOUT_SECONDS or 8))
    async with httpx.AsyncClient(timeout=timeout, headers=PRICE_HEADERS) as client:
        source_signature = await _fetch_visual_signature(client, product.image_url)
        if source_signature is None:
            return
        signatures = await asyncio.gather(
            *(_fetch_visual_signature(client, candidate.image_url) for candidate in eligible)
        )
    for candidate, signature in zip(eligible, signatures):
        if signature is not None:
            candidate.raw["visual_similarity"] = _visual_signature_similarity(source_signature, signature)


def _result_to_payload(result: PriceComparisonResult) -> dict:
    return asdict(result)


def _product_from_payload(payload: dict) -> WBProduct:
    data = payload.get("wb_product") or {}
    allowed = set(WBProduct.__dataclass_fields__.keys())
    return WBProduct(**{key: value for key, value in data.items() if key in allowed})


def _market_from_payload(payload: dict, marketplace: str) -> MarketplaceComparison:
    raw = payload.get(marketplace) or {}
    items = []
    for item in raw.get("items") or []:
        product_data = item.get("product") or {}
        try:
            product = ComparableProduct(**product_data)
            risk_data = item.get("risk")
            risk = MarketplaceRiskAssessment(**risk_data) if isinstance(risk_data, dict) else None
            items.append(
                PriceComparisonItem(
                    product=product,
                    similarity=int(item.get("similarity") or 0),
                    similarity_label=item.get("similarity_label") or "",
                    price_diff=float(item.get("price_diff") or 0),
                    price_diff_percent=float(item.get("price_diff_percent") or 0),
                    relation=item.get("relation") or "",
                    risk=risk,
                )
            )
        except TypeError:
            continue
    return MarketplaceComparison(
        marketplace=raw.get("marketplace") or marketplace,
        items=items,
        raw_count=int(raw.get("raw_count") or 0),
        filtered_count=int(raw.get("filtered_count") or len(items)),
        from_cache=bool(raw.get("from_cache")),
        error=raw.get("error") or "",
    )


def _result_from_payload(payload: dict) -> PriceComparisonResult:
    risk_decision_data = payload.get("risk_decision")
    risk_decision = PriceRiskDecision(**risk_decision_data) if isinstance(risk_decision_data, dict) else None
    return PriceComparisonResult(
        wb_product=_product_from_payload(payload),
        wb_price=float(payload.get("wb_price") or 0),
        search_query=payload.get("search_query") or "",
        ozon=_market_from_payload(payload, "ozon"),
        yandex=_market_from_payload(payload, "yandex"),
        best_marketplace=payload.get("best_marketplace") or "wb",
        best_price=float(payload.get("best_price") or 0),
        best_url=payload.get("best_url") or "",
        best_title=payload.get("best_title") or "",
        status=payload.get("status") or "partial",
        errors=list(payload.get("errors") or []),
        elapsed_seconds=float(payload.get("elapsed_seconds") or 0),
        from_cache=True,
        risk_decision=risk_decision,
    )


def _wb_product_from_payload(payload: dict) -> WBProduct | None:
    allowed = {field.name for field in dataclass_fields(WBProduct)}
    data = {key: value for key, value in (payload or {}).items() if key in allowed}
    if not data.get("article") or not data.get("title"):
        return None
    return WBProduct(**data)


async def _load_cached_wb_product(article: str) -> WBProduct | None:
    cached = await _safe_get_cache(f"price_wb_product:v1:{article}")
    if not cached:
        return None
    return _wb_product_from_payload(cached)


async def _store_cached_wb_product(product: WBProduct):
    await _safe_set_cache(
        f"price_wb_product:v1:{product.article}",
        asdict(product),
        int(settings.PRICE_COMPARE_CACHE_TTL_SECONDS or 21600),
    )


def _choose_best(product: WBProduct, wb_price: float, markets: list[MarketplaceComparison]) -> tuple[str, float, str, str]:
    best_marketplace = "wb"
    best_price = wb_price
    best_url = product.product_url
    best_title = product.title
    for market in markets:
        for item in market.items:
            if item.product.price is not None and item.product.price < best_price:
                best_marketplace = market.marketplace
                best_price = item.product.price
                best_url = item.product.url
                best_title = item.product.title
    return best_marketplace, best_price, best_url, best_title


async def compare_wb_product_prices(
    product: WBProduct,
    user_input: str = "",
    platform: str = "",
    user_id: str = "",
) -> PriceComparisonResult:
    if platform and user_id and not await has_active_pro_access(platform, user_id):
        raise PriceCompareProRequired(
            "Сравнение цен и рисков доступно только с активной подпиской SmartCheck PRO."
        )
    if not settings.PRICE_COMPARE_ENABLED:
        await _record_failure_history(
            platform, user_id, user_input, product, "Сравнение цен отключено настройками."
        )
        raise PriceCompareError("Сравнение цен отключено настройками.")

    started = time.monotonic()
    wb_price = product.sale_price or product.price
    if wb_price is None:
        await _record_failure_history(
            platform, user_id, user_input, product, "Не удалось получить цену Wildberries для сравнения."
        )
        raise PriceCompareError("Не удалось получить цену Wildberries для сравнения.")
    if not product.title:
        await _record_failure_history(
            platform, user_id, user_input, product, "Не удалось получить название товара Wildberries для сравнения."
        )
        raise PriceCompareError("Не удалось получить название товара Wildberries для сравнения.")

    final_cache_key = f"price_compare:v15:{product.article}:{int(wb_price * 100)}"
    cached = await _safe_get_cache(final_cache_key)
    if cached:
        result = _result_from_payload(cached)
        if platform and user_id:
            await _record_result_history(result, platform, user_id, user_input)
        return result

    queries = build_search_queries(product)
    query = queries[0]
    logger.info(
        "Price compare start: platform=%s, user_id=%s, input=%r, article=%s, query=%s",
        platform,
        user_id,
        (user_input or "")[:300],
        product.article,
        query,
    )

    async def run_ozon():
        if not settings.PRICE_COMPARE_OZON_ENABLED:
            return [], False, "Ozon disabled"
        products, from_cache, error = await search_ozon(query)
        if len(queries) > 1 and not any(_match_candidate(product, item).accepted for item in products):
            fallback, fallback_cached, fallback_error = await search_ozon(queries[1])
            products = _merge_candidates(products, fallback)
            from_cache = from_cache and fallback_cached
            error = "; ".join(part for part in (error, fallback_error) if part)
        return products, from_cache, error

    async def run_yandex():
        if not settings.PRICE_COMPARE_YANDEX_ENABLED:
            return [], False, "Yandex Market disabled"
        products, from_cache, error = await search_yandex_market(query)
        if len(queries) > 1 and not any(_match_candidate(product, item).accepted for item in products):
            fallback, fallback_cached, fallback_error = await search_yandex_market(queries[1])
            products = _merge_candidates(products, fallback)
            from_cache = from_cache and fallback_cached
            error = "; ".join(part for part in (error, fallback_error) if part)
        return products, from_cache, error

    ozon_candidates: list[ComparableProduct] = []
    yandex_candidates: list[ComparableProduct] = []
    ozon_cached = False
    yandex_cached = False
    errors: list[str] = []

    results = await asyncio.gather(run_ozon(), run_yandex(), return_exceptions=True)
    for marketplace, result in zip(("ozon", "yandex"), results):
        if isinstance(result, Exception):
            error = f"{MARKETPLACE_TITLES[marketplace]}: {result}"
            errors.append(error)
            log_api_error(f"Price compare {marketplace} error: {result}")
            continue
        candidates, from_cache, error = result
        if marketplace == "ozon":
            ozon_candidates, ozon_cached = candidates, from_cache
        else:
            yandex_candidates, yandex_cached = candidates, from_cache
        if error:
            source_error = f"{MARKETPLACE_TITLES[marketplace]}: {error}"
            errors.append(source_error)
            logger.warning("Price compare source warning: %s", source_error)

    await asyncio.gather(
        _enrich_candidate_pages(product, ozon_candidates),
        _enrich_candidate_pages(product, yandex_candidates),
    )
    await asyncio.gather(
        _enrich_visual_similarities(product, ozon_candidates),
        _enrich_visual_similarities(product, yandex_candidates),
    )

    ozon = _compare_marketplace(
        "ozon",
        product,
        wb_price,
        ozon_candidates,
        ozon_cached,
        next((error for error in errors if error.startswith("Ozon:")), ""),
    )
    yandex = _compare_marketplace(
        "yandex",
        product,
        wb_price,
        yandex_candidates,
        yandex_cached,
        next((error for error in errors if error.startswith("Яндекс Маркет:")), ""),
    )

    best_marketplace, best_price, best_url, best_title = _choose_best(product, wb_price, [ozon, yandex])
    has_any = bool(ozon.items or yandex.items)
    has_errors = bool(errors)
    status = "success" if has_any and not has_errors else "partial" if has_any else "error"
    elapsed = round(time.monotonic() - started, 2)
    result = PriceComparisonResult(
        wb_product=product,
        wb_price=wb_price,
        search_query=query,
        ozon=ozon,
        yandex=yandex,
        best_marketplace=best_marketplace,
        best_price=best_price,
        best_url=best_url,
        best_title=best_title,
        status=status,
        errors=errors,
        elapsed_seconds=elapsed,
    )
    await _safe_set_cache(
        final_cache_key,
        _result_to_payload(result),
        int(settings.PRICE_COMPARE_CACHE_TTL_SECONDS or 21600),
    )
    if platform and user_id:
        await _record_result_history(result, platform, user_id, user_input)

    logger.info(
        "Price compare complete: article=%s, status=%s, ozon=%s/%s, yandex=%s/%s, best=%s, elapsed=%s",
        product.article,
        status,
        ozon.filtered_count,
        ozon.raw_count,
        yandex.filtered_count,
        yandex.raw_count,
        best_marketplace,
        elapsed,
    )
    return result


async def compare_wb_prices(user_input: str, platform: str = "", user_id: str = "") -> PriceComparisonResult:
    article = extract_wb_article(user_input or "")
    if not article:
        await _record_failure_history(
            platform,
            user_id,
            user_input,
            None,
            "Не удалось распознать ссылку или артикул Wildberries.",
        )
        raise PriceCompareError("Не удалось распознать ссылку или артикул Wildberries.")
    logger.info(
        "Price compare input parsed: platform=%s, user_id=%s, input=%r, article=%s",
        platform,
        user_id,
        (user_input or "")[:300],
        article,
    )
    product = await _load_cached_wb_product(article)
    if product:
        logger.info("Price compare WB product cache hit: article=%s", article)
    else:
        try:
            product = await fetch_wb_product(article)
        except Exception as error:
            logger.exception("Price compare WB product fetch failed: article=%s", article)
            await _record_failure_history(
                platform,
                user_id,
                user_input,
                WBProduct(article=article, title=""),
                f"Wildberries: {error}",
            )
            raise
        await _store_cached_wb_product(product)
        logger.info("Price compare WB product fetched: article=%s, title=%s", product.article, product.title)
    return await compare_wb_product_prices(product, user_input=user_input, platform=platform, user_id=user_id)


async def _record_failure_history(
    platform: str,
    user_id: str,
    user_input: str,
    product: WBProduct | None,
    error: str,
) -> None:
    if not platform or not user_id:
        return
    try:
        await record_price_compare_history(
            platform=platform,
            user_id=user_id,
            user_input=user_input,
            article=product.article if product else "",
            wb_title=product.title if product else "",
            wb_price=(product.sale_price or product.price) if product else None,
            search_query=build_search_query(product) if product and product.title else "",
            ozon_results=[],
            yandex_results=[],
            best_marketplace="wb",
            best_price=(product.sale_price or product.price) if product else None,
            status="error",
            error=error,
            elapsed_seconds=0,
        )
    except Exception as history_error:
        log_api_error(f"Price compare failure history write failed: {history_error}")


async def _record_result_history(result: PriceComparisonResult, platform: str, user_id: str, user_input: str):
    try:
        await record_price_compare_history(
            platform=platform,
            user_id=user_id,
            user_input=user_input,
            article=result.wb_product.article,
            wb_title=result.wb_product.title,
            wb_price=result.wb_price,
            search_query=result.search_query,
            ozon_results=[asdict(item) for item in result.ozon.items],
            yandex_results=[asdict(item) for item in result.yandex.items],
            best_marketplace=result.best_marketplace,
            best_price=result.best_price,
            status=result.status,
            error="; ".join(result.errors),
            elapsed_seconds=result.elapsed_seconds,
        )
    except Exception as error:
        log_api_error(f"Price compare history write failed: {error}")


def _parse_review_count(value: Any) -> int | None:
    if value in (None, ""):
        return None
    match = re.search(r"\d[\d\s\u00a0]*", str(value))
    if not match:
        return None
    try:
        return int(match.group(0).replace(" ", "").replace("\u00a0", ""))
    except ValueError:
        return None


def _extract_reputation_from_html(html: str) -> tuple[float | None, int | None]:
    soup = BeautifulSoup(html or "", "html.parser")
    candidates: list[tuple[float, int | None]] = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        payload = _safe_json_loads(script.string or script.get_text("", strip=True))
        for item in _iter_json_objects(payload):
            aggregate = item.get("aggregateRating") if isinstance(item, dict) else None
            if not isinstance(aggregate, dict):
                continue
            rating = _as_float(aggregate.get("ratingValue") or aggregate.get("rating"))
            count = _parse_review_count(
                aggregate.get("reviewCount") or aggregate.get("ratingCount") or aggregate.get("count")
            )
            if rating is not None and 0 < rating <= 5:
                candidates.append((rating, count))

    rating_meta = soup.select_one('[itemprop="ratingValue"]')
    count_meta = soup.select_one('[itemprop="reviewCount"], [itemprop="ratingCount"]')
    if rating_meta:
        rating = _as_float(rating_meta.get("content") or rating_meta.get_text(" ", strip=True))
        count = _parse_review_count(
            (count_meta.get("content") or count_meta.get_text(" ", strip=True)) if count_meta else None
        )
        if rating is not None and 0 < rating <= 5:
            candidates.append((rating, count))

    if not candidates:
        return None, None
    return max(candidates, key=lambda item: (item[1] is not None, item[1] or 0))


async def _enrich_product_reputation(client: httpx.AsyncClient, product: ComparableProduct) -> None:
    if product.rating is not None and product.review_count is not None:
        return
    try:
        response = await client.get(product.url, headers=PRICE_HEADERS)
        response.raise_for_status()
        rating, review_count = _extract_reputation_from_html(response.text)
        if product.rating is None:
            product.rating = rating
        if product.review_count is None:
            product.review_count = review_count
        if rating is not None or review_count is not None:
            product.raw["reputation_source"] = "product_page"
    except Exception as error:
        logger.info(
            "Marketplace reputation unavailable: marketplace=%s, url=%s, error=%s",
            product.marketplace,
            product.url,
            error,
        )


def _marketplace_risk_level(score: int) -> str:
    if score <= 18:
        return "низкий"
    if score <= 34:
        return "умеренный"
    if score <= 49:
        return "повышенный"
    return "высокий"


def assess_marketplace_item_risk(item: PriceComparisonItem) -> MarketplaceRiskAssessment:
    product = item.product
    risk = 0.0
    confidence = min(35, item.similarity * 0.4)
    signals: list[str] = []

    if product.rating is None:
        risk += 8
        signals.append("рейтинг площадка не раскрыла")
    else:
        rating = max(0.0, min(5.0, float(product.rating)))
        risk += max(0.0, (4.9 - rating) * 24)
        confidence += 30
        signals.append(f"рейтинг {rating:.1f}")

    reviews = product.review_count
    if reviews is None:
        risk += 6
        signals.append("число отзывов не подтверждено")
    elif reviews >= 1000:
        risk = max(0, risk - 4)
        confidence += 25
        signals.append(f"{reviews} отзывов — устойчивая выборка")
    elif reviews >= 200:
        risk = max(0, risk - 2)
        confidence += 23
        signals.append(f"{reviews} отзывов")
    elif reviews >= 50:
        risk += 3
        confidence += 18
        signals.append(f"только {reviews} отзывов")
    elif reviews >= 10:
        risk += 8
        confidence += 12
        signals.append(f"мало отзывов: {reviews}")
    else:
        risk += 15
        confidence += 6
        signals.append(f"очень мало отзывов: {reviews}")

    if item.similarity < 50:
        risk += (50 - item.similarity) * 0.45
        signals.append(f"совпадение товара только {item.similarity}%")
    elif item.similarity >= 70:
        signals.append(f"хорошее совпадение товара: {item.similarity}%")

    savings = max(0.0, -item.price_diff_percent)
    if savings >= 70:
        risk += 14
        signals.append(f"аномально низкая цена: выгода {savings:.0f}%")
    elif savings >= 50:
        risk += 9
        signals.append(f"очень большая разница в цене: {savings:.0f}%")
    elif savings >= 35:
        risk += 4
        signals.append(f"заметная разница в цене: {savings:.0f}%")

    source = str(product.raw.get("source") or "")
    if product.raw.get("reputation_source") == "product_page" or source in {"json", "external", "html_card"}:
        confidence += 10

    score = max(0, min(100, round(risk)))
    confidence_score = max(0, min(100, round(confidence)))
    if confidence_score >= 65:
        basis = "рейтинг, объем отзывов, цена и совпадение товара"
    elif confidence_score >= 45:
        basis = "частичные данные площадки и совпадение товара"
    else:
        basis = "ограниченные данные; вывод предварительный"
    level = _marketplace_risk_level(score) if confidence_score >= 50 else "не подтвержден"
    return MarketplaceRiskAssessment(
        score=score,
        confidence=confidence_score,
        level=level,
        signals=signals[:4],
        basis=basis,
    )


def _candidate_value_score(item: PriceComparisonItem, wb_risk_score: int) -> float:
    risk = item.risk or assess_marketplace_item_risk(item)
    savings = max(0.0, -item.price_diff_percent)
    extra_risk = max(0, risk.score - wb_risk_score)
    uncertainty = max(0, 60 - risk.confidence)
    return savings + item.similarity * 0.12 - extra_risk * 1.6 - uncertainty * 0.35


def build_price_risk_decision(result: PriceComparisonResult, wb_risk_score: int) -> PriceRiskDecision:
    wb_risk = max(0, min(100, int(wb_risk_score or 0)))
    candidates = [
        item
        for item in result.ozon.items + result.yandex.items
        if item.product.price is not None and item.price_diff_percent <= -5
    ]
    if not candidates:
        return PriceRiskDecision(
            code="keep_wb_price",
            title="WB пока выглядит рациональнее",
            explanation="Надежной выгоды от 5% среди сопоставимых предложений не найдено.",
            wb_risk_score=wb_risk,
        )

    candidates.sort(key=lambda item: _candidate_value_score(item, wb_risk), reverse=True)
    best = candidates[0]
    assessment = best.risk or assess_marketplace_item_risk(best)
    best.risk = assessment
    savings = abs(best.price_diff_percent)
    marketplace = MARKETPLACE_TITLES.get(best.product.marketplace, best.product.marketplace)

    min_confidence = int(settings.MARKETPLACE_RISK_MIN_CONFIDENCE or 50)
    hard_stop = int(settings.MARKETPLACE_RISK_HARD_STOP or 45)
    max_recommend_risk = int(settings.MARKETPLACE_RISK_MAX_RECOMMEND or 35)
    max_gap = int(settings.MARKETPLACE_RISK_MAX_GAP_TO_WB or 8)
    min_savings = float(settings.MARKETPLACE_MIN_SAVINGS_PERCENT or 8)

    if assessment.confidence < min_confidence:
        return PriceRiskDecision(
            code="verify_first",
            title="Цена ниже, но данных для рекомендации мало",
            explanation=(
                f"На {marketplace} дешевле на {savings:.1f}%, однако рейтинг или объем отзывов не подтверждены. "
                "Показываю вариант, но не называю его более безопасной покупкой."
            ),
            wb_risk_score=wb_risk,
        )

    if assessment.score >= hard_stop or assessment.score >= wb_risk + 20:
        return PriceRiskDecision(
            code="keep_wb_risk",
            title="Скидка не перекрывает риск",
            explanation=(
                f"На {marketplace} дешевле на {savings:.1f}%, но оценочный риск {assessment.score}% "
                f"против {wb_risk}% у WB. Переходить только ради цены не советую."
            ),
            wb_risk_score=wb_risk,
        )

    if savings >= min_savings and assessment.score <= max_recommend_risk and assessment.score <= wb_risk + max_gap:
        return PriceRiskDecision(
            code="recommend_alternative",
            title=f"Можно рассмотреть {marketplace}",
            explanation=(
                f"Цена ниже на {savings:.1f}%, а оценочный риск {assessment.score}% "
                f"не выглядит хуже WB ({wb_risk}%)."
            ),
            wb_risk_score=wb_risk,
            recommended_marketplace=best.product.marketplace,
            recommended_url=best.product.url,
            recommended_title=best.product.title,
        )

    return PriceRiskDecision(
        code="cautious_option",
        title="Есть экономия, но без однозначного перевеса",
        explanation=(
            f"На {marketplace} дешевле на {savings:.1f}%, однако профиль риска или надежность данных уступают WB. "
            "Вариант можно изучить, но это не безусловная рекомендация."
        ),
        wb_risk_score=wb_risk,
    )


async def enrich_price_comparison_with_risk(
    result: PriceComparisonResult,
    wb_risk_score: int,
) -> PriceComparisonResult:
    items = result.ozon.items + result.yandex.items
    timeout = min(12.0, float(settings.PRICE_COMPARE_TIMEOUT_SECONDS or 20))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        await asyncio.gather(
            *(_enrich_product_reputation(client, item.product) for item in items),
            return_exceptions=True,
        )
    for item in items:
        item.risk = assess_marketplace_item_risk(item)
    result.risk_decision = build_price_risk_decision(result, wb_risk_score)
    return result


def _format_diff(item: PriceComparisonItem) -> str:
    if abs(item.price_diff) < 1:
        return "примерно как WB"
    value = _format_price(abs(item.price_diff))
    percent = abs(item.price_diff_percent)
    if item.price_diff < 0:
        return f"дешевле на {value} ({percent:.1f}%)"
    return f"дороже на {value} ({percent:.1f}%)"


def _market_block_html(market: MarketplaceComparison) -> str:
    title = MARKETPLACE_TITLES.get(market.marketplace, market.marketplace)
    icon = "🔵" if market.marketplace == "ozon" else "🟡"
    if not market.items:
        reason = "подходящих товаров с ценой не найдено"
        if market.error:
            reason = "площадка временно не дала надежные данные"
        return f"{icon} <b>{escape(title)}</b>\n└ {escape(reason, quote=False)}"

    number_icons = ("1️⃣", "2️⃣", "3️⃣")
    lines = [f"{icon} <b>{escape(title)}</b>", "━━━━━━━━━━━━"]
    for index, item in enumerate(market.items, start=1):
        product = item.product
        number = number_icons[index - 1] if index <= len(number_icons) else f"{index}."
        lines.append(
            f"{number} <a href=\"{escape(product.url, quote=True)}\"><b>{escape(product.title, quote=False)}</b></a>"
        )
        lines.append(f"💰 <b>{_format_price(product.price)}</b> · {_format_diff(item)}")
        lines.append(f"🎯 Совпадение: <b>{item.similarity}%</b> · {escape(item.similarity_label)}")
        if product.old_price and product.old_price > (product.price or 0):
            lines.append(f"🏷 Старая цена: <s>{_format_price(product.old_price)}</s>")
        if product.rating is not None:
            rating_text = f"⭐ {product.rating:.1f}"
            if product.review_count is not None:
                rating_text += f" · {product.review_count} отзывов"
            lines.append(rating_text)
        elif product.review_count is not None:
            lines.append(f"💬 {product.review_count} отзывов")
        if item.risk:
            if item.risk.confidence < int(settings.MARKETPLACE_RISK_MIN_CONFIDENCE or 50):
                lines.append(
                    f"🛡 Риск: <b>не подтвержден</b> · предварительная оценка {item.risk.score}% · "
                    f"уверенность {item.risk.confidence}%"
                )
            else:
                lines.append(
                    f"🛡 Риск: <b>{item.risk.score}%</b> ({escape(item.risk.level)}) · "
                    f"уверенность {item.risk.confidence}%"
                )
            lines.append(f"🔎 Основа: {escape(item.risk.basis, quote=False)}")
        else:
            lines.append("🛡 Риск: будет рассчитан перед рекомендацией")
        if index < len(market.items):
            lines.append("────────────")
    return "\n".join(lines)


def _verdict_text(result: PriceComparisonResult) -> str:
    if result.risk_decision:
        return f"{result.risk_decision.title}\n{result.risk_decision.explanation}"
    wb_title = MARKETPLACE_TITLES["wb"]
    best_title = MARKETPLACE_TITLES.get(result.best_marketplace, result.best_marketplace)
    all_items = result.ozon.items + result.yandex.items
    confidence = max((item.similarity for item in all_items), default=0)
    confidence_text = f" Уверенность сопоставления: {confidence}%." if confidence else ""
    if result.best_marketplace == "wb":
        if result.ozon.items or result.yandex.items:
            return f"Итог: {wb_title} сейчас выглядит выгоднее или на уровне найденных аналогов.{confidence_text}"
        return "Итог: надежных аналогов с ценами не нашлось, сравнение получилось частичным."

    diff = result.wb_price - result.best_price
    percent = round(diff / result.wb_price * 100, 1) if result.wb_price else 0
    return f"Итог: самый дешевый вариант найден на {best_title}: дешевле WB на {_format_price(diff)} ({percent:.1f}%).{confidence_text}"


def _wb_price_lines(product: WBProduct, wb_price: float) -> list[str]:
    lines = [f"Цена WB: <b>{_format_price(wb_price)}</b>"]
    old_price = product.price if product.price and product.price > wb_price else None
    if old_price:
        discount = round((old_price - wb_price) / old_price * 100)
        lines.append(f"Старая цена WB: <s>{_format_price(old_price)}</s> · скидка {discount}%")
    if product.rating is not None:
        rating_line = f"Рейтинг WB: <b>{escape(str(product.rating), quote=False)}</b>"
        if product.review_count:
            rating_line += f" · отзывов: <b>{product.review_count}</b>"
        lines.append(rating_line)
    return lines


def format_price_comparison_html(result: PriceComparisonResult) -> str:
    product = result.wb_product
    wb_risk_line = ""
    if result.risk_decision:
        wb_risk_line = f"\n🛡 Риск WB: <b>{result.risk_decision.wb_risk_score}%</b> · полный анализ отзывов"
    lines = [
        "⚖️ <b>SmartCheck: цена + риск</b>",
        "━━━━━━━━━━━━",
        "🟣 <b>Wildberries · точка отсчета</b>",
        f'<a href="{escape(product.product_url, quote=True)}"><b>{escape(product.title, quote=False)}</b></a>',
        *_wb_price_lines(product, result.wb_price),
        wb_risk_line,
        "",
        _market_block_html(result.ozon),
        "",
        _market_block_html(result.yandex),
        "",
        "🏁 <b>Очищенный вывод</b>",
        _verdict_text(result),
    ]
    if result.status == "partial" and result.errors:
        lines.append("\n<i>Часть источников ответила неполно, показал только надежные данные с ценами.</i>")
    return "\n".join(lines)


def _teaser_marketplaces(result: PriceComparisonResult) -> list[str]:
    marketplaces = []
    if result.ozon.items:
        marketplaces.append("Ozon")
    if result.yandex.items:
        marketplaces.append("Яндекс Маркете")
    return marketplaces


def format_price_comparison_teaser_html(result: PriceComparisonResult) -> str:
    marketplaces = _teaser_marketplaces(result)
    if not marketplaces:
        return ""
    place_text = " и ".join(marketplaces)
    all_items = result.ozon.items + result.yandex.items
    cheaper = [item for item in all_items if item.price_diff_percent <= -5]
    if cheaper:
        best_saving = max(abs(item.price_diff_percent) for item in cheaper)
        lead = (
            f"Нашёл сопоставимые предложения на <b>{escape(place_text)}</b>. "
            f"Предварительная выгода достигает <b>{best_saving:.1f}%</b>."
        )
    else:
        lead = f"Нашёл сопоставимые предложения на <b>{escape(place_text)}</b>."
    return (
        "🔎 <b>Есть что сравнить</b>\n\n"
        f"{lead}\n\n"
        "Дешевле — не всегда выгоднее. Перед рекомендацией SmartCheck сверит цену, рейтинг, "
        "объем отзывов, совпадение товара и риск покупки."
    )


def format_price_comparison_teaser_plain(result: PriceComparisonResult) -> str:
    return _strip_html_for_plain(format_price_comparison_teaser_html(result))


def _strip_html_for_plain(value: str) -> str:
    value = re.sub(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r"\2: \1", value, flags=re.I | re.S)
    value = re.sub(r"</?(b|strong|i|em|code|pre|u|s)>", "", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    return value.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def format_price_comparison_plain(result: PriceComparisonResult) -> str:
    return _strip_html_for_plain(format_price_comparison_html(result))


def get_price_compare_health() -> dict:
    return {
        "enabled": bool(settings.PRICE_COMPARE_ENABLED),
        "ozon_enabled": bool(settings.PRICE_COMPARE_OZON_ENABLED),
        "yandex_enabled": bool(settings.PRICE_COMPARE_YANDEX_ENABLED),
        "external_provider_configured": bool((settings.PRICE_COMPARE_EXTERNAL_SEARCH_URL or "").strip()),
        "brave_api_configured": bool((settings.PRICE_COMPARE_BRAVE_SEARCH_API_KEY or "").strip()),
        "serpapi_configured": bool((settings.PRICE_COMPARE_SERPAPI_KEY or "").strip()),
        "browser_fallback_enabled": bool(settings.PRICE_COMPARE_BROWSER_FALLBACK_ENABLED),
        "search_engine_fallback_enabled": bool(settings.PRICE_COMPARE_SEARCH_ENGINE_FALLBACK_ENABLED),
        "cache_ttl_seconds": int(settings.PRICE_COMPARE_CACHE_TTL_SECONDS or 0),
        "timeout_seconds": int(settings.PRICE_COMPARE_TIMEOUT_SECONDS or 0),
        "min_similarity": int(settings.PRICE_COMPARE_MIN_SIMILARITY or 0),
    }

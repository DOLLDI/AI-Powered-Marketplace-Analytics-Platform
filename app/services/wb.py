import re
import time
import json
import random
import hashlib
from html import escape
from dataclasses import asdict, dataclass, field, fields as dataclass_fields
from collections import Counter
from pathlib import Path
from typing import Optional
import asyncio
import httpx
import openai

from app.services.config import settings
from app.services.error_logger import log_api_error
from app.services.logger import logger
from app.services.prompts import get_wb_summary_prompt
from app.services.wb_search import search_wb_article
WB_URL_RE = re.compile(r"wildberries\.ru/catalog/(\d+)/detail\.aspx", re.IGNORECASE)
WB_ARTICLE_RE = re.compile(r"\b(\d{6,12})\b")
WB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://www.wildberries.ru/",
}

WB_TEMPORARY_UNAVAILABLE_MESSAGE = "WB временно ограничил доступ к данным, попробуйте через 15 минут."
WB_ANALYSIS_UNAVAILABLE_MESSAGE = "Не удалось обработать данные Wildberries. Попробуйте позже."
MAX_REVIEW_TEXT_CHARS = 700
LOW_VALUE_MIN_CHARS = 25
SUMMARY_REVIEW_LIMIT = 15

LOW_VALUE_PHRASES = {
    "все супер",
    "всё супер",
    "супер",
    "норм",
    "нормально",
    "спасибо",
    "ок",
    "окей",
    "рекомендую",
    "хорошо",
    "отлично",
    "класс",
    "понравилось",
}

PROBLEM_MARKERS = {
    "брак": "брак",
    "бракованный": "брак",
    "сломано": "поломка",
    "сломанный": "поломка",
    "не работает": "не работает",
    "ужас": "плохое качество",
    "плохой": "плохое качество",
    "плохая": "плохое качество",
    "тонкий": "тонкий материал",
    "тонкая": "тонкий материал",
    "маломерит": "маломерит",
    "большемерит": "большемерит",
    "запах": "запах",
    "грязный": "грязный товар",
    "грязная": "грязный товар",
    "порван": "повреждение",
    "порвана": "повреждение",
    "царапина": "повреждение",
    "не подошел": "не подошел размер",
    "не подошёл": "не подошел размер",
    "возврат": "возврат",
    "доставка": "доставка",
    "упаковка": "упаковка",
    "нет детали": "некомплект",
    "не соответствует": "не соответствует описанию",
}

POSITIVE_MARKERS = {
    "качество": "качество",
    "цена": "цена",
    "материал": "материал",
    "удобный": "удобство",
    "удобная": "удобство",
    "быстрая доставка": "быстрая доставка",
    "соответствует описанию": "соответствует описанию",
    "размер подошел": "размер подошел",
    "размер подошёл": "размер подошел",
    "хорошая упаковка": "хорошая упаковка",
}

SEVERE_PROBLEM_LABELS = {
    "брак",
    "поломка",
    "не работает",
    "повреждение",
    "не соответствует описанию",
    "некомплект",
    "плохое качество",
}

FIT_PROBLEM_LABELS = {
    "не подошел размер",
    "маломерит",
    "большемерит",
    "тонкий материал",
}

LOGISTICS_PROBLEM_LABELS = {
    "доставка",
    "упаковка",
    "возврат",
    "грязный товар",
}

_analysis_semaphore: asyncio.Semaphore | None = None
_analysis_cache: dict[str, tuple[float, "WBAnalysisResult"]] = {}

class WBError(Exception):
    pass


class WBTemporaryUnavailable(WBError):
    pass


class WBNotFound(WBError):
    pass


@dataclass
class WBReview:
    text: str
    rating: int
    created_at: str = ""
    pros: str = ""
    cons: str = ""
    likes: int = 0
    dislikes: int = 0
    useful_score: int = 0


@dataclass
class WBProduct:
    article: str
    title: str
    brand: str = ""
    root_id: str = ""
    subject_id: str = ""
    subject_name: str = ""
    category_name: str = ""
    price: Optional[float] = None
    sale_price: Optional[float] = None
    rating: Optional[float] = None
    review_count: int = 0
    image_url: str = ""
    product_url: str = ""
    description: str = ""
    attributes: dict = field(default_factory=dict)


@dataclass
class WBAnalysisResult:
    product: WBProduct
    selected_reviews: list[WBReview]
    summary_html: str
    total_reviews_loaded: int
    review_analysis: Optional["ReviewAnalysisResult"] = None
    summary_pack: str = ""


@dataclass
class ReviewStats:
    total_loaded: int
    total_after_filter: int
    avg_rating: float
    count_1: int
    count_2: int
    count_3: int
    count_4: int
    count_5: int
    negative_percent: float
    neutral_percent: float
    positive_percent: float


@dataclass
class ReviewAnomaly:
    type: str
    description: str
    severity: str


@dataclass
class ReviewAnalysisResult:
    stats: ReviewStats
    anomalies: list[ReviewAnomaly]
    top_positive_reviews: list[WBReview]
    top_negative_reviews: list[WBReview]
    top_useful_reviews: list[WBReview]
    defect_reviews: list[WBReview]
    frequent_pros: list[str]
    frequent_cons: list[str]
    risk_score: int
    risk_level: str
    fake_review_signals: list[str]
    filtered_reviews: list[WBReview] = field(default_factory=list)


@dataclass
class ReviewRiskMetrics:
    avg_rating: float
    negative_percent: float
    all_problem_percent: float
    severe_problem_percent: float
    fit_problem_percent: float
    logistics_problem_percent: float
    fake_signal_count: int
    serious_anomaly_count: int


@dataclass(frozen=True)
class ProductRiskProfile:
    code: str
    title: str
    recommend_max_risk: int
    recommend_min_rating: float
    recommend_max_negative: float
    recommend_max_severe: float
    recommend_max_all_problems: float
    risky_min_negative: float
    risky_min_severe: float
    risky_min_all_problems: float
    reject_min_negative: float
    reject_min_severe: float
    reject_min_all_problems: float


RISK_PROFILES = {
    "apparel": ProductRiskProfile(
        code="apparel",
        title="одежда/бельё",
        recommend_max_risk=22,
        recommend_min_rating=4.45,
        recommend_max_negative=12,
        recommend_max_severe=6,
        recommend_max_all_problems=28,
        risky_min_negative=14,
        risky_min_severe=8,
        risky_min_all_problems=35,
        reject_min_negative=28,
        reject_min_severe=16,
        reject_min_all_problems=45,
    ),
    "electronics": ProductRiskProfile(
        code="electronics",
        title="электроника",
        recommend_max_risk=22,
        recommend_min_rating=4.45,
        recommend_max_negative=10,
        recommend_max_severe=4,
        recommend_max_all_problems=18,
        risky_min_negative=12,
        risky_min_severe=6,
        risky_min_all_problems=24,
        reject_min_negative=25,
        reject_min_severe=12,
        reject_min_all_problems=35,
    ),
    "beauty_health": ProductRiskProfile(
        code="beauty_health",
        title="красота/здоровье",
        recommend_max_risk=22,
        recommend_min_rating=4.45,
        recommend_max_negative=10,
        recommend_max_severe=5,
        recommend_max_all_problems=22,
        risky_min_negative=12,
        risky_min_severe=7,
        risky_min_all_problems=28,
        reject_min_negative=25,
        reject_min_severe=14,
        reject_min_all_problems=40,
    ),
    "default": ProductRiskProfile(
        code="default",
        title="универсальный",
        recommend_max_risk=20,
        recommend_min_rating=4.5,
        recommend_max_negative=10,
        recommend_max_severe=4,
        recommend_max_all_problems=20,
        risky_min_negative=12,
        risky_min_severe=6,
        risky_min_all_problems=28,
        reject_min_negative=25,
        reject_min_severe=12,
        reject_min_all_problems=40,
    ),
}


async def _safe_html_parse(article: str):
    from app.services.wb_html_parser import parse_wb_product_html

    url = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
    last_error = None

    for i in range(3):
        try:
            return await asyncio.to_thread(parse_wb_product_html, url)
        except Exception as e:
            last_error = e
            await asyncio.sleep(1.5 + i)

    raise last_error


def extract_wb_article(text: str) -> Optional[str]:
    url_match = WB_URL_RE.search(text)
    if url_match:
        return url_match.group(1)
    article_match = WB_ARTICLE_RE.search(text)
    if article_match:
        return article_match.group(1)
    return None


def _parse_price(raw_value) -> Optional[float]:
    if raw_value in (None, ""):
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    if value > 1000:
        return round(value / 100.0, 2)
    return round(value, 2)


def _extract_products(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")

    if isinstance(data, dict):
        products = data.get("products")
        if isinstance(products, list):
            return [p for p in products if isinstance(p, dict)]

    products = payload.get("products")
    if isinstance(products, list):
        return [p for p in products if isinstance(p, dict)]

    return []


def _build_wb_image_url(article: str, image_number: int = 1) -> str:
    try:
        nm_id = int(article)
    except (TypeError, ValueError):
        return ""

    vol = nm_id // 100000
    part = nm_id // 1000
    basket_ranges = [
        (143, "01"),
        (287, "02"),
        (431, "03"),
        (719, "04"),
        (1007, "05"),
        (1061, "06"),
        (1115, "07"),
        (1169, "08"),
        (1313, "09"),
        (1601, "10"),
        (1655, "11"),
        (1919, "12"),
        (2045, "13"),
        (2189, "14"),
        (2405, "15"),
        (2621, "16"),
        (2837, "17"),
        (3053, "18"),
        (3269, "19"),
        (3485, "20"),
        (3701, "21"),
        (3917, "22"),
        (4133, "23"),
    ]
    basket = "24"
    for max_vol, basket_number in basket_ranges:
        if vol <= max_vol:
            basket = basket_number
            break

    return (
        f"https://basket-{basket}.wbbasket.ru/vol{vol}/part{part}/"
        f"{nm_id}/images/big/{image_number}.webp"
    )


def _select_card_size_price(sizes: list) -> dict:
    priced_sizes = []
    for size in sizes or []:
        if not isinstance(size, dict):
            continue
        price = size.get("price")
        if not isinstance(price, dict) or not (price.get("product") or price.get("basic")):
            continue
        stocks = size.get("stocks") or []
        in_stock = any(
            isinstance(stock, dict) and _safe_int(stock.get("qty"), 0) > 0
            for stock in stocks
        )
        product_price = price.get("product") or price.get("basic") or float("inf")
        priced_sizes.append((in_stock, float(product_price), price))

    if not priced_sizes:
        return {}
    available = [item for item in priced_sizes if item[0]]
    pool = available or priced_sizes
    return min(pool, key=lambda item: item[1])[2]



def _parse_card_product(product_data: dict, article: str) -> WBProduct:
    sizes = product_data.get("sizes") or []
    size_price = _select_card_size_price(sizes)
    image_url = ""
    pics = product_data.get("pics") or product_data.get("images") or []
    if isinstance(pics, list) and pics:
        first_image = pics[0]
        if isinstance(first_image, str) and first_image.startswith("http"):
            image_url = first_image
    if not image_url:
        media_files = product_data.get("mediaFiles") or []
        if isinstance(media_files, list) and media_files:
            first_media = media_files[0]
            if isinstance(first_media, str) and first_media.startswith("http"):
                image_url = first_media
    if not image_url and isinstance(pics, int) and pics > 0:
        image_url = _build_wb_image_url(str(product_data.get("id") or article))

    colors = product_data.get("colors") or []
    color_names = [
        str(item.get("name") or item.get("title") or "")
        for item in colors
        if isinstance(item, dict) and (item.get("name") or item.get("title"))
    ]
    return WBProduct(
        article=article,
        title=product_data.get("name") or f"Товар WB {article}",
        brand=product_data.get("brand") or "",
        root_id=str(product_data.get("root") or ""),
        subject_id=str(product_data.get("subjectId") or product_data.get("subjectID") or ""),
        subject_name=str(product_data.get("subjectName") or product_data.get("subject") or ""),
        category_name=str(
            product_data.get("parentName")
            or product_data.get("subjectParentName")
            or product_data.get("kindName")
            or ""
        ),
        price=_parse_price(size_price.get("basic") or product_data.get("priceU")),
        sale_price=_parse_price(size_price.get("product") or product_data.get("salePriceU")),
        rating=product_data.get("reviewRating") or product_data.get("rating"),
        review_count=int(product_data.get("feedbacks") or product_data.get("feedbackCount") or 0),
        image_url=image_url,
        product_url=f"https://www.wildberries.ru/catalog/{article}/detail.aspx",
        description=product_data.get("description") or "",
        attributes={
            "colors": color_names[0] if len(color_names) == 1 else "",
            "kind": str(product_data.get("kindName") or ""),
        },
    )


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_review_field(value: str, max_chars: int = MAX_REVIEW_TEXT_CHARS) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(cleaned) > max_chars:
        return cleaned[:max_chars].rstrip() + "..."
    return cleaned


def _parse_reviews(payload: dict) -> list[WBReview]:
    review_items = []
    if isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("feedbacks"), list):
        review_items = payload["data"]["feedbacks"]
    elif isinstance(payload.get("feedbacks"), list):
        review_items = payload["feedbacks"]

    reviews: list[WBReview] = []
    for item in review_items:
        if not isinstance(item, dict):
            continue
        text = _clean_review_field(item.get("text") or item.get("reviewText") or "")
        pros = _clean_review_field(item.get("pros") or item.get("advantages") or "")
        cons = _clean_review_field(item.get("cons") or item.get("disadvantages") or "")
        if not text and not pros and not cons:
            continue
        likes = _safe_int(
            item.get("likes")
            or item.get("like")
            or item.get("plus")
            or item.get("useful")
            or item.get("helpful")
        )
        dislikes = _safe_int(item.get("dislikes") or item.get("dislike") or item.get("minus"))
        reviews.append(
            WBReview(
                text=text,
                rating=_safe_int(item.get("productValuation") or item.get("valuation") or item.get("rating")),
                created_at=str(item.get("createdDate") or item.get("createdAt") or ""),
                pros=pros,
                cons=cons,
                likes=likes,
                dislikes=dislikes,
                useful_score=max(0, likes - dislikes),
            )
        )
    return reviews


def select_reviews_for_summary(reviews: list[WBReview]) -> list[WBReview]:
    if not reviews:
        return []
    newest = reviews[:30]
    lowest = sorted(reviews, key=lambda review: (review.rating or 0, review.created_at or ""))[:20]
    selected: list[WBReview] = []
    seen = set()
    for review in newest + lowest:
        key = (review.text, review.pros, review.cons, review.created_at)
        if key in seen:
            continue
        seen.add(key)
        selected.append(review)
    return selected


def normalize_review_text(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip().lower())
    cleaned = cleaned.replace("ё", "е")
    cleaned = re.sub(r"[^\w\sа-я-]", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _normalize_review_text(value: str) -> str:
    return normalize_review_text(value)


def _combined_review_text(review: WBReview) -> str:
    return " ".join(part for part in (review.text, review.pros, review.cons) if part).strip()


def _problem_labels(review: WBReview) -> list[str]:
    normalized = normalize_review_text(_combined_review_text(review))
    labels = []
    seen = set()
    for marker, label in PROBLEM_MARKERS.items():
        if marker.replace("ё", "е") in normalized and label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def detect_product_risk_profile(product: WBProduct) -> ProductRiskProfile:
    text = normalize_review_text(
        " ".join(
            part
            for part in (
                product.title,
                product.brand,
                product.subject_name,
                product.category_name,
                product.description,
            )
            if part
        )
    )
    apparel_markers = {
        "трус",
        "боксер",
        "белье",
        "носк",
        "футбол",
        "рубаш",
        "брюк",
        "джинс",
        "плать",
        "куртк",
        "худи",
        "одежд",
        "размер",
        "обув",
        "кроссов",
    }
    electronics_markers = {
        "колонк",
        "станция",
        "алиса",
        "смартфон",
        "телефон",
        "наушник",
        "планшет",
        "ноутбук",
        "электрон",
        "заряд",
        "кабель",
        "аккумулятор",
        "плеер",
        "диктофон",
    }
    beauty_health_markers = {
        "хлорофилл",
        "витамин",
        "бад",
        "добавк",
        "крем",
        "сыворотк",
        "шампун",
        "маск",
        "космет",
        "здоров",
        "похуд",
    }
    if any(marker in text for marker in apparel_markers):
        return RISK_PROFILES["apparel"]
    if any(marker in text for marker in electronics_markers):
        return RISK_PROFILES["electronics"]
    if any(marker in text for marker in beauty_health_markers):
        return RISK_PROFILES["beauty_health"]
    return RISK_PROFILES["default"]


def calculate_review_risk_metrics(analysis: ReviewAnalysisResult) -> ReviewRiskMetrics:
    total = max(1, analysis.stats.total_after_filter)
    all_problem_count = 0
    severe_problem_count = 0
    fit_problem_count = 0
    logistics_problem_count = 0
    for review in analysis.filtered_reviews:
        labels = set(_problem_labels(review))
        if labels:
            all_problem_count += 1
        if labels & SEVERE_PROBLEM_LABELS:
            severe_problem_count += 1
        if labels & FIT_PROBLEM_LABELS:
            fit_problem_count += 1
        if labels & LOGISTICS_PROBLEM_LABELS:
            logistics_problem_count += 1

    anomaly_types = {item.type for item in analysis.anomalies if item.severity in {"medium", "high"}}
    fake_anomaly_types = {
        "duplicates",
        "template_phrases",
        "date_spike",
        "hour_spike",
        "positive_spike",
        "positive_hour_spike",
        "empty_reviews",
        "short_reviews",
    }
    serious_anomaly_types = {"many_negative"}
    return ReviewRiskMetrics(
        avg_rating=analysis.stats.avg_rating or 0,
        negative_percent=analysis.stats.negative_percent,
        all_problem_percent=all_problem_count / total * 100,
        severe_problem_percent=severe_problem_count / total * 100,
        fit_problem_percent=fit_problem_count / total * 100,
        logistics_problem_percent=logistics_problem_count / total * 100,
        fake_signal_count=len(anomaly_types & fake_anomaly_types),
        serious_anomaly_count=len(anomaly_types & serious_anomaly_types),
    )


def _positive_labels(review: WBReview) -> list[str]:
    normalized = normalize_review_text(_combined_review_text(review))
    labels = []
    seen = set()
    for marker, label in POSITIVE_MARKERS.items():
        if marker.replace("ё", "е") in normalized and label not in seen:
            seen.add(label)
            labels.append(label)
    return labels


def is_low_value_review(review: WBReview) -> bool:
    combined = normalize_review_text(_combined_review_text(review))
    if not combined:
        return True

    has_problem = bool(_problem_labels(review))
    if review.rating <= 2 and (has_problem or len(combined) >= 10):
        return False

    if combined in LOW_VALUE_PHRASES:
        return True

    words = combined.split()
    has_details = bool(review.pros or review.cons or has_problem or _positive_labels(review))
    if len(combined) < LOW_VALUE_MIN_CHARS and not has_details:
        return True
    if len(words) <= 3 and not has_details:
        return True
    return False


def deduplicate_reviews(reviews: list[WBReview]) -> list[WBReview]:
    deduped = []
    seen_exact = set()
    seen_prefix = set()
    for review in reviews:
        normalized_text = normalize_review_text(review.text)
        normalized_pros = normalize_review_text(review.pros)
        normalized_cons = normalize_review_text(review.cons)
        exact_key = (normalized_text, normalized_pros, normalized_cons)
        prefix_source = " ".join(part for part in exact_key if part)
        prefix_key = prefix_source[:150]
        if exact_key in seen_exact:
            continue
        if len(prefix_key) >= 80 and prefix_key in seen_prefix:
            continue
        seen_exact.add(exact_key)
        if prefix_key:
            seen_prefix.add(prefix_key)
        deduped.append(review)
    return deduped


def calculate_review_score(review: WBReview) -> int:
    combined = _combined_review_text(review)
    normalized = normalize_review_text(combined)
    score = 0
    if len(normalized) > 80:
        score += 3
    if review.pros:
        score += 2
    if review.cons:
        score += 2
    if review.rating in (1, 2):
        score += 3
    if _problem_labels(review):
        score += 2
    score += min(10, max(0, review.useful_score or review.likes))
    if len(normalized) < LOW_VALUE_MIN_CHARS:
        score -= 3
    if is_low_value_review(review):
        score -= 5
    return score


def analyze_review_stats(reviews: list[WBReview], total_loaded: int | None = None) -> ReviewStats:
    total_after_filter = len(reviews)
    loaded = total_loaded if total_loaded is not None else total_after_filter
    rating_counter: Counter[int] = Counter(review.rating for review in reviews if review.rating in {1, 2, 3, 4, 5})
    rated_total = sum(rating_counter.values())
    avg_rating = 0.0
    if rated_total:
        avg_rating = round(
            sum(rating * count for rating, count in rating_counter.items()) / rated_total,
            2,
        )

    def percent(count: int) -> float:
        return round(count / rated_total * 100, 1) if rated_total else 0.0

    negative = rating_counter[1] + rating_counter[2]
    neutral = rating_counter[3]
    positive = rating_counter[4] + rating_counter[5]
    return ReviewStats(
        total_loaded=loaded,
        total_after_filter=total_after_filter,
        avg_rating=avg_rating,
        count_1=rating_counter[1],
        count_2=rating_counter[2],
        count_3=rating_counter[3],
        count_4=rating_counter[4],
        count_5=rating_counter[5],
        negative_percent=percent(negative),
        neutral_percent=percent(neutral),
        positive_percent=percent(positive),
    )


def extract_frequent_pros_cons(reviews: list[WBReview]) -> tuple[list[str], list[str]]:
    pros_counter: Counter[str] = Counter()
    cons_counter: Counter[str] = Counter()
    total = max(1, len(reviews))
    for review in reviews:
        for label in _positive_labels(review):
            pros_counter[label] += 1
        for label in _problem_labels(review):
            cons_counter[label] += 1
        if review.pros and len(normalize_review_text(review.pros)) >= 12:
            pros_counter[review.pros[:80]] += 1
        if review.cons and len(normalize_review_text(review.cons)) >= 12:
            cons_counter[review.cons[:80]] += 1

    def format_items(counter: Counter[str], raw_text_min_count: int) -> list[str]:
        items = []
        for label, count in counter.most_common():
            is_raw_text = len(label) > 30 or " " in label
            if is_raw_text and count < raw_text_min_count:
                continue
            percent = count / total * 100
            items.append(f"{label} ({count}; {percent:.1f}% от обработанных)")
            if len(items) >= 8:
                break
        return items

    raw_text_min_count = max(3, int(total * 0.01))
    return (
        format_items(pros_counter, raw_text_min_count),
        format_items(cons_counter, raw_text_min_count),
    )


def detect_review_anomalies(reviews: list[WBReview]) -> list[ReviewAnomaly]:
    anomalies: list[ReviewAnomaly] = []
    if not reviews:
        return [
            ReviewAnomaly(
                type="no_reviews",
                description="Отзывы не удалось загрузить: анализ накрутки ограничен карточкой товара.",
                severity="medium",
            )
        ]

    total = len(reviews)
    short_reviews = sum(1 for review in reviews if len(normalize_review_text(_combined_review_text(review))) < 25)
    empty_text = sum(1 for review in reviews if not normalize_review_text(_combined_review_text(review)))
    five_star = sum(1 for review in reviews if review.rating == 5)
    low_rated = sum(1 for review in reviews if review.rating in (1, 2))
    severe_complaint_labels = {
        "брак",
        "поломка",
        "не работает",
        "повреждение",
        "не соответствует описанию",
        "некомплект",
        "плохое качество",
    }
    problem_reviews = 0
    severe_problem_reviews = 0
    for review in reviews:
        labels = set(_problem_labels(review))
        if labels or review.cons:
            problem_reviews += 1
        if labels & severe_complaint_labels:
            severe_problem_reviews += 1

    short_percent = short_reviews / total * 100
    if short_percent >= 25:
        if short_percent >= 65:
            short_severity = "high"
        elif short_percent >= 45:
            short_severity = "medium"
        else:
            short_severity = "low"
        anomalies.append(
            ReviewAnomaly(
                type="short_reviews",
                description=f"{short_percent:.0f}% отзывов слишком короткие и почти не содержат деталей.",
                severity=short_severity,
            )
        )

    empty_percent = empty_text / total * 100
    if empty_percent >= 15:
        anomalies.append(
            ReviewAnomaly(
                type="empty_reviews",
                description=f"{empty_percent:.0f}% отзывов без внятного текста, плюсов или минусов.",
                severity="medium",
            )
        )

    duplicate_counter = Counter(
        normalize_review_text(_combined_review_text(review))[:150]
        for review in reviews
        if len(normalize_review_text(_combined_review_text(review))) >= 20
    )
    duplicate_threshold = max(5, int(total * 0.005))
    repeated = [
        (text, count)
        for text, count in duplicate_counter.most_common(5)
        if count >= duplicate_threshold
    ]
    if repeated:
        examples = "; ".join(f"{count}x «{text[:70]}»" for text, count in repeated[:3])
        top_count = repeated[0][1]
        top_share = top_count / total * 100
        anomalies.append(
            ReviewAnomaly(
                type="duplicates",
                description=(
                    f"Найдены повторяющиеся формулировки: {examples}. "
                    f"Максимальная доля одного повтора: {top_share:.2f}%."
                ),
                severity="high" if top_share >= 3 else "medium" if top_share >= 1 else "low",
            )
        )

    short_phrase_counter = Counter(
        normalize_review_text(_combined_review_text(review))
        for review in reviews
        if normalize_review_text(_combined_review_text(review))
        and (
            normalize_review_text(_combined_review_text(review)) in LOW_VALUE_PHRASES
            or len(normalize_review_text(_combined_review_text(review))) <= 40
        )
    )
    short_phrase_threshold = max(3, int(total * 0.05))
    repeated_short = [
        (text, count)
        for text, count in short_phrase_counter.most_common(5)
        if count >= short_phrase_threshold
    ]
    if repeated_short:
        examples = "; ".join(f"{count}x «{text[:50]}»" for text, count in repeated_short[:3])
        anomalies.append(
            ReviewAnomaly(
                type="template_phrases",
                description=f"Повторяются короткие шаблонные фразы: {examples}.",
                severity="high",
            )
        )

    date_counter: Counter[str] = Counter()
    hour_counter: Counter[str] = Counter()
    five_star_by_date: Counter[str] = Counter()
    five_star_by_hour: Counter[str] = Counter()
    has_dates = False
    for review in reviews:
        date_key = _review_date_key(review.created_at)
        if not date_key:
            continue
        has_dates = True
        date_counter[date_key] += 1
        if review.rating == 5:
            five_star_by_date[date_key] += 1
        hour_key = _review_hour_key(review.created_at)
        if hour_key:
            hour_counter[hour_key] += 1
            if review.rating == 5:
                five_star_by_hour[hour_key] += 1

    if has_dates:
        dense_threshold = max(4, int(total * 0.15))
        dense_dates = [(date, count) for date, count in date_counter.most_common(5) if count >= dense_threshold]
        if dense_dates:
            date, count = dense_dates[0]
            anomalies.append(
                ReviewAnomaly(
                    type="date_spike",
                    description=f"Зафиксирован всплеск отзывов: {count} отзывов за дату {date}.",
                    severity="high" if count >= 40 else "medium",
                )
            )
        hour_threshold = max(5, int(total * 0.08))
        dense_hours = [(hour, count) for hour, count in hour_counter.most_common(5) if count >= hour_threshold]
        if dense_hours:
            hour, count = dense_hours[0]
            anomalies.append(
                ReviewAnomaly(
                    type="hour_spike",
                    description=f"Всплеск отзывов внутри часа: {count} отзывов за {hour}:00.",
                    severity="high" if count >= 20 else "medium",
                )
            )
        positive_threshold = max(4, int(total * 0.12))
        positive_spikes = [(date, count) for date, count in five_star_by_date.most_common(5) if count >= positive_threshold]
        if positive_spikes:
            date, count = positive_spikes[0]
            anomalies.append(
                ReviewAnomaly(
                    type="positive_spike",
                    description=f"Всплеск положительных оценок: {count} отзывов 5★ за дату {date}.",
                    severity="medium",
                )
            )
        positive_hour_threshold = max(5, int(total * 0.07))
        positive_hour_spikes = [
            (hour, count)
            for hour, count in five_star_by_hour.most_common(5)
            if count >= positive_hour_threshold
        ]
        if positive_hour_spikes:
            hour, count = positive_hour_spikes[0]
            anomalies.append(
                ReviewAnomaly(
                    type="positive_hour_spike",
                    description=f"Всплеск 5★ внутри часа: {count} отзывов за {hour}:00.",
                    severity="high" if count >= 20 else "medium",
                )
            )
    else:
        anomalies.append(
            ReviewAnomaly(
                type="missing_dates",
                description="Аномалии по времени не рассчитаны: отсутствуют даты отзывов.",
                severity="low",
            )
        )

    problem_percent = problem_reviews / total * 100
    severe_problem_percent = severe_problem_reviews / total * 100
    low_rated_percent = low_rated / total * 100
    if (
        five_star / total >= 0.8
        and severe_problem_percent >= 12
        and low_rated_percent >= 10
    ):
        anomalies.append(
            ReviewAnomaly(
                type="rating_complaint_mismatch",
                description=(
                    "Распределение оценок поляризовано: высокая доля 5★ сочетается с серьёзными жалобами "
                    f"в тексте: {severe_problem_percent:.1f}% серьёзных проблем, "
                    f"{low_rated_percent:.1f}% оценок 1-2★."
                ),
                severity="high" if severe_problem_percent >= 18 or low_rated_percent >= 20 else "medium",
            )
        )
    if low_rated / total >= 0.25:
        anomalies.append(
            ReviewAnomaly(
                type="many_negative",
                description=f"{low_rated / total * 100:.0f}% отзывов имеют 1-2★.",
                severity="high",
            )
        )
    return anomalies


def calculate_risk_score(
    stats: ReviewStats,
    anomalies: list[ReviewAnomaly],
    reviews: list[WBReview],
) -> tuple[int, str]:
    total = max(1, stats.total_after_filter)
    defect_count = sum(
        1
        for review in reviews
        if set(_problem_labels(review)) & SEVERE_PROBLEM_LABELS
    )
    delivery_count = sum(
        1
        for review in reviews
        if set(_problem_labels(review)) & LOGISTICS_PROBLEM_LABELS
    )
    fake_signal_count = sum(
        1
        for anomaly in anomalies
        if anomaly.severity in {"medium", "high"}
        and anomaly.type
        in {
            "short_reviews",
            "duplicates",
            "template_phrases",
            "date_spike",
            "hour_spike",
            "positive_spike",
            "positive_hour_spike",
            "empty_reviews",
        }
    )

    risk_score = 0.0
    risk_score += stats.negative_percent * 0.4
    risk_score += min(25, defect_count / total * 100 * 0.25)
    risk_score += min(20, fake_signal_count * 6)
    risk_score += min(10, delivery_count / total * 100 * 0.1)
    if stats.avg_rating and stats.avg_rating < 4.0:
        risk_score += min(15, (4.0 - stats.avg_rating) * 10)
    risk = min(100, max(0, round(risk_score)))

    if risk <= 20:
        level = "НИЗКИЙ"
    elif risk <= 50:
        level = "СРЕДНИЙ"
    else:
        level = "ВЫСОКИЙ"
    return risk, level


def analyze_reviews_locally(reviews: list[WBReview]) -> ReviewAnalysisResult:
    deduped = deduplicate_reviews(reviews)
    filtered = [review for review in deduped if not is_low_value_review(review)]
    if not filtered and deduped:
        filtered = deduped[: min(len(deduped), 20)]

    stats_source = reviews or deduped or filtered
    stats = analyze_review_stats(stats_source, total_loaded=len(reviews))
    anomalies = detect_review_anomalies(reviews)
    frequent_pros, frequent_cons = extract_frequent_pros_cons(filtered)
    risk_score, risk_level = calculate_risk_score(stats, anomalies, filtered)

    def score_desc(review: WBReview) -> tuple[int, str]:
        return (calculate_review_score(review), review.created_at or "")

    top_negative = sorted(
        [review for review in filtered if review.rating in (1, 2) or review.cons or _problem_labels(review)],
        key=score_desc,
        reverse=True,
    )[:SUMMARY_REVIEW_LIMIT]
    top_positive = sorted(
        [review for review in filtered if review.rating in (4, 5) and not _problem_labels(review)],
        key=score_desc,
        reverse=True,
    )[:SUMMARY_REVIEW_LIMIT]
    top_useful = sorted(filtered, key=score_desc, reverse=True)[:SUMMARY_REVIEW_LIMIT]
    defect_reviews = sorted(
        [review for review in filtered if _problem_labels(review)],
        key=score_desc,
        reverse=True,
    )[:10]
    fake_signals = [
        anomaly.description
        for anomaly in anomalies
        if anomaly.severity in {"medium", "high"}
        and anomaly.type
        in {
            "short_reviews",
            "duplicates",
            "template_phrases",
            "date_spike",
            "hour_spike",
            "positive_spike",
            "positive_hour_spike",
            "empty_reviews",
        }
    ]

    return ReviewAnalysisResult(
        stats=stats,
        anomalies=anomalies,
        top_positive_reviews=top_positive,
        top_negative_reviews=top_negative,
        top_useful_reviews=top_useful,
        defect_reviews=defect_reviews,
        frequent_pros=frequent_pros,
        frequent_cons=frequent_cons,
        risk_score=risk_score,
        risk_level=risk_level,
        fake_review_signals=fake_signals,
        filtered_reviews=filtered,
    )


def _review_date_key(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 10 and re.match(r"\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    return value


def _review_hour_key(value: str) -> str:
    value = (value or "").strip()
    if len(value) >= 13 and re.match(r"\d{4}-\d{2}-\d{2}[T\s]\d{2}", value):
        return value[:13].replace("T", " ")
    return ""


def _build_review_fraud_signals(reviews: list[WBReview], product_review_count: int) -> str:
    if not reviews:
        return (
            "Отзывы для анализа не загружены. Нельзя честно проверить накрутку: "
            "в вердикте обязательно укажи, что данных мало."
        )

    text_values = []
    empty_or_tiny = 0
    rating_counter: Counter[int] = Counter()
    date_counter: Counter[str] = Counter()

    for review in reviews:
        full_text = " ".join(part for part in (review.text, review.pros, review.cons) if part).strip()
        normalized = _normalize_review_text(full_text)
        if normalized:
            text_values.append(normalized)
        if len(normalized) < 25:
            empty_or_tiny += 1
        if review.rating:
            rating_counter[review.rating] += 1
        date_key = _review_date_key(review.created_at)
        if date_key:
            date_counter[date_key] += 1

    duplicate_groups = [
        (text, count)
        for text, count in Counter(text_values).most_common(5)
        if count > 1 and len(text) >= 20
    ]
    dense_dates = [(date, count) for date, count in date_counter.most_common(5) if count >= 4]
    five_star_share = round(rating_counter.get(5, 0) / len(reviews) * 100)
    low_star_share = round(
        sum(count for rating, count in rating_counter.items() if rating <= 2) / len(reviews) * 100
    )
    tiny_share = round(empty_or_tiny / len(reviews) * 100)

    lines = [
        f"Всего отзывов в карточке: {product_review_count}; передано в ИИ для проверки: {len(reviews)}.",
        f"Распределение оценок: {dict(sorted(rating_counter.items())) or 'нет данных'}.",
        f"Доля 5★: {five_star_share}%; доля 1-2★: {low_star_share}%; пустые/очень короткие отзывы: {tiny_share}%.",
    ]

    if duplicate_groups:
        examples = "; ".join(f"{count}x «{text[:90]}»" for text, count in duplicate_groups[:3])
        lines.append(f"Повторы текста обнаружены: {examples}.")
    else:
        lines.append("Дословных повторов текста в выбранной выборке не найдено.")

    if dense_dates:
        examples = "; ".join(f"{date}: {count} отзывов" for date, count in dense_dates)
        lines.append(f"Плотные всплески по датам: {examples}.")
    else:
        lines.append("Массовых всплесков отзывов в один день в выбранной выборке не найдено.")

    lines.append("Фото отзывов в данные не переданы: не выдумывай выводы про шаблонные фото.")
    return "\n".join(lines)


def _format_price_for_pack(product: WBProduct) -> str:
    if product.sale_price is not None:
        return f"{product.sale_price} RUB"
    if product.price is not None:
        return f"{product.price} RUB"
    return "Цена не получена"


def _format_review_for_pack(review: WBReview) -> str:
    parts = [f"Оценка {review.rating}/5"]
    if review.created_at:
        parts.append(f"дата: {review.created_at}")
    if review.likes or review.dislikes:
        parts.append(f"полезность: +{review.likes}/-{review.dislikes}")
    if review.text:
        parts.append(f"текст: {review.text}")
    if review.pros:
        parts.append(f"плюсы: {review.pros}")
    if review.cons:
        parts.append(f"минусы: {review.cons}")
    return " | ".join(parts)


def _format_numbered_reviews(reviews: list[WBReview], empty_text: str) -> str:
    if not reviews:
        return f"- {empty_text}"
    return "\n".join(
        f"{index}. {_format_review_for_pack(review)}"
        for index, review in enumerate(reviews, start=1)
    )


def _count_unique_reviews(groups: list[list[WBReview]]) -> int:
    seen = set()
    for group in groups:
        for review in group:
            seen.add((review.text, review.pros, review.cons, review.created_at))
    return len(seen)


def _summary_tone_instruction(verdict: str, metrics: ReviewRiskMetrics) -> str:
    if verdict == "РЕКОМЕНДУЕМ":
        return (
            "Тон ответа: уверенно-положительный и спокойный. Не ищи подвох ради подвоха. "
            "Если сильных сигналов накрутки нет, пиши, что отзывы выглядят органично. "
            "Минусы называй нюансами и обязательно показывай масштаб: "
            f"негатив {metrics.negative_percent:.1f}%, серьёзные проблемы {metrics.severe_problem_percent:.1f}%. "
            "Не используй слова 'подозрительно', 'значительно', 'много жалоб', если метрики ниже риск-порогов."
        )
    if verdict == "РИСКОВАННО":
        return (
            "Тон ответа: осторожный, но без паники. Объясни, какие именно метрики делают покупку рискованной, "
            "и отделяй системные проблемы от обычного шума отзывов."
        )
    return (
        "Тон ответа: прямой и жёсткий. Покажи ключевые причины отказа, но не придумывай риски сверх метрик."
    )


def _fake_reviews_assessment(analysis: ReviewAnalysisResult) -> str:
    fake_types = {
        "duplicates",
        "template_phrases",
        "date_spike",
        "hour_spike",
        "positive_spike",
        "positive_hour_spike",
        "empty_reviews",
        "short_reviews",
    }
    relevant = [item for item in analysis.anomalies if item.type in fake_types]
    high = [item for item in relevant if item.severity == "high"]
    medium = [item for item in relevant if item.severity == "medium"]
    low = [item for item in relevant if item.severity == "low"]

    if high:
        level = "высокий риск"
        comment = "есть сильные аномалии, их нужно прямо назвать в ответе"
    elif len(medium) >= 2:
        level = "подозрительно"
        comment = "несколько средних сигналов совпали, отзывы стоит трактовать осторожно"
    elif medium:
        level = "слабые вопросы"
        comment = "есть один средний сигнал, но без подтверждения другими метриками это не доказательство накрутки"
    elif low:
        level = "низкий риск"
        comment = "серьёзных признаков накрутки нет; есть только слабые технические сигналы, для массовых товаров это нормальный шум"
    else:
        level = "низкий риск"
        comment = "серьёзных признаков накрутки по доступным отзывам не найдено"

    signal_lines = relevant or [
        ReviewAnomaly(
            type="organic_reviews",
            description="Явных повторов, всплесков и шаблонных аномалий не найдено.",
            severity="low",
        )
    ]
    return (
        f"Уровень: {level}\n"
        f"Комментарий: {comment}.\n"
        f"Сильных сигналов: {len(high)}; средних: {len(medium)}; слабых: {len(low)}.\n"
        "Сигналы:\n"
        + "\n".join(f"- [{item.severity}] {item.description}" for item in signal_lines)
    )


def _recommended_verdict_from_analysis(
    product: WBProduct,
    analysis: ReviewAnalysisResult,
) -> str:
    profile = detect_product_risk_profile(product)
    metrics = calculate_review_risk_metrics(analysis)
    card_rating = float(product.rating or 0)

    if (
        card_rating >= 4.7
        and analysis.risk_score <= 15
        and metrics.negative_percent <= 20
        and metrics.severe_problem_percent <= 2
        and metrics.all_problem_percent <= 8
        and metrics.serious_anomaly_count == 0
        and metrics.fake_signal_count == 0
    ):
        return "РЕКОМЕНДУЕМ"

    if (
        analysis.risk_score <= profile.recommend_max_risk
        and max(metrics.avg_rating, card_rating) >= profile.recommend_min_rating
        and metrics.negative_percent <= profile.recommend_max_negative
        and metrics.severe_problem_percent <= profile.recommend_max_severe
        and metrics.all_problem_percent <= profile.recommend_max_all_problems
        and metrics.serious_anomaly_count == 0
        and metrics.fake_signal_count == 0
    ):
        return "РЕКОМЕНДУЕМ"

    if (
        analysis.risk_score >= 60
        or metrics.serious_anomaly_count > 0
        or metrics.negative_percent >= profile.reject_min_negative
        or metrics.severe_problem_percent >= profile.reject_min_severe
        or metrics.all_problem_percent >= profile.reject_min_all_problems
        or (metrics.avg_rating and metrics.avg_rating < 3.8)
    ):
        return "НЕ РЕКОМЕНДУЕМ"
    if (
        analysis.risk_score >= 35
        or metrics.negative_percent >= profile.risky_min_negative
        or metrics.severe_problem_percent >= profile.risky_min_severe
        or metrics.all_problem_percent >= profile.risky_min_all_problems
        or (metrics.avg_rating and metrics.avg_rating < 4.3)
        or (
            analysis.risk_score >= 25
            and (
                metrics.negative_percent >= profile.recommend_max_negative
                or metrics.severe_problem_percent >= profile.recommend_max_severe
            )
        )
        or (
            metrics.fake_signal_count >= 2
            and (
                metrics.negative_percent >= max(6, profile.recommend_max_negative * 0.7)
                or metrics.severe_problem_percent >= max(3, profile.recommend_max_severe * 0.7)
            )
        )
    ):
        return "РИСКОВАННО"
    return "РЕКОМЕНДУЕМ"


def build_reviews_summary_pack(product: WBProduct, analysis: ReviewAnalysisResult) -> str:
    stats = analysis.stats
    anomaly_lines = [f"- [{item.severity}] {item.description}" for item in analysis.anomalies]
    if not anomaly_lines:
        anomaly_lines = ["- Явных аномалий по доступным отзывам не найдено."]

    profile = detect_product_risk_profile(product)
    risk_metrics = calculate_review_risk_metrics(analysis)
    recommended_verdict = _recommended_verdict_from_analysis(product, analysis)
    tone_instruction = _summary_tone_instruction(recommended_verdict, risk_metrics)
    fake_reviews_assessment = _fake_reviews_assessment(analysis)
    return (
        "ТОВАР:\n"
        f"Название: {product.title}\n"
        f"Артикул: {product.article}\n"
        f"Бренд: {product.brand or 'Не указан'}\n"
        f"Профиль риска SmartCheck: {profile.title}\n"
        f"Категория WB: {product.category_name or product.subject_name or 'Не получена'}\n"
        f"Цена: {_format_price_for_pack(product)}\n"
        f"Рейтинг WB: {product.rating or 'Нет данных'}\n"
        f"Количество отзывов в карточке: {product.review_count}\n\n"
        "СТАТИСТИКА ПО ЗАГРУЖЕННЫМ ОТЗЫВАМ:\n"
        f"Всего загружено: {stats.total_loaded}\n"
        f"Загружено из заявленных в карточке: {stats.total_loaded} из {product.review_count or 'неизвестно'}\n"
        f"Отзывы с оценками для расчёта рейтинга и риска: {stats.total_after_filter}\n"
        f"Содержательных отзывов для примеров и частых плюсов/минусов: {len(analysis.filtered_reviews)}\n"
        f"Компактных примеров отзывов в summary-pack: "
        f"{_count_unique_reviews([analysis.top_negative_reviews, analysis.top_positive_reviews, analysis.top_useful_reviews, analysis.defect_reviews])}\n"
        f"Средняя оценка по обработанным отзывам: {stats.avg_rating or 'нет данных'}\n"
        f"1★: {stats.count_1}\n"
        f"2★: {stats.count_2}\n"
        f"3★: {stats.count_3}\n"
        f"4★: {stats.count_4}\n"
        f"5★: {stats.count_5}\n"
        f"Доля негативных: {stats.negative_percent}%\n"
        f"Доля нейтральных: {stats.neutral_percent}%\n"
        f"Доля положительных: {stats.positive_percent}%\n\n"
        "АНОМАЛИИ:\n"
        + "\n".join(anomaly_lines)
        + "\n\n"
        "ЧАСТЫЕ ПЛЮСЫ:\n"
        + ("\n".join(f"- {item}" for item in analysis.frequent_pros) if analysis.frequent_pros else "- Частые плюсы не выделены.")
        + "\n\n"
        "ЧАСТЫЕ МИНУСЫ:\n"
        + ("\n".join(f"- {item}" for item in analysis.frequent_cons) if analysis.frequent_cons else "- Частые минусы не выделены.")
        + "\n\n"
        "ОЦЕНОЧНЫЙ ИНДЕКС РИСКА SMARTCHECK:\n"
        f"{analysis.risk_score}% — {analysis.risk_level}\n\n"
        "МЕТРИКИ ДЛЯ ВЕРДИКТА SMARTCHECK:\n"
        f"Профиль: {profile.title}\n"
        f"Доля негатива: {risk_metrics.negative_percent:.1f}%\n"
        f"Все проблемные сигналы: {risk_metrics.all_problem_percent:.1f}%\n"
        f"Серьёзные проблемы: {risk_metrics.severe_problem_percent:.1f}%\n"
        f"Размер/посадка: {risk_metrics.fit_problem_percent:.1f}%\n"
        f"Логистика/возврат/упаковка: {risk_metrics.logistics_problem_percent:.1f}%\n"
        f"Сильные сигналы накрутки: {risk_metrics.fake_signal_count}\n"
        f"Серьёзные аномалии: {risk_metrics.serious_anomaly_count}\n\n"
        "РЕКОМЕНДОВАННЫЙ ВЕРДИКТ ПО МАТЕМАТИКЕ SMARTCHECK:\n"
        f"{recommended_verdict}\n"
        "ИИ обязан использовать этот вердикт в первой строке ответа. Не меняй его на более строгий без "
        "сильных пропорциональных сигналов риска. Для массовых товаров с тысячами отзывов отдельные жалобы "
        "на запах, упаковку, доставку и единичные дефекты считаются нормальным шумом, если их доля низкая.\n\n"
        "ТОНАЛЬНОСТЬ ОТВЕТА SMARTCHECK:\n"
        f"{tone_instruction}\n\n"
        "ВАЖНО ДЛЯ ИНТЕРПРЕТАЦИИ РЕЙТИНГА:\n"
        "Официальный рейтинг WB из карточки считай главным ориентиром. Средняя оценка по загруженным отзывам "
        "может отличаться из-за порядка выдачи отзывов API и нужна только как дополнительный сигнал.\n\n"
        "ОЦЕНКА РИСКА ОТКАЗА/ВОЗВРАТА:\n"
        "Точные данные WB недоступны. Оценка строится только по отзывам о браке, размере, доставке и возвратах.\n\n"
        "ПРОВЕРКА НАКРУТКИ ОТЗЫВОВ:\n"
        + fake_reviews_assessment
        + "\n\n"
        "ПРИМЕРЫ НЕГАТИВНЫХ ОТЗЫВОВ:\n"
        + _format_numbered_reviews(analysis.top_negative_reviews, "Негативные содержательные отзывы не найдены.")
        + "\n\n"
        "ПРИМЕРЫ ПОЛОЖИТЕЛЬНЫХ ОТЗЫВОВ:\n"
        + _format_numbered_reviews(analysis.top_positive_reviews, "Положительные содержательные отзывы не найдены.")
        + "\n\n"
        "ПРИМЕРЫ ПОЛЕЗНЫХ ОТЗЫВОВ:\n"
        + _format_numbered_reviews(analysis.top_useful_reviews, "Полезные отзывы не выделены.")
        + "\n\n"
        "ПРИМЕРЫ ОТЗЫВОВ С ЯВНЫМ БРАКОМ/ПРОБЛЕМАМИ:\n"
        + _format_numbered_reviews(analysis.defect_reviews, "Отзывы с явным браком не найдены.")
    )


def build_summary_pack(product: WBProduct, analysis: ReviewAnalysisResult) -> str:
    return build_reviews_summary_pack(product, analysis)


def _dataclass_from_dict(cls, data: dict):
    if not isinstance(data, dict):
        return cls()
    allowed = {item.name for item in dataclass_fields(cls)}
    return cls(**{key: value for key, value in data.items() if key in allowed})


def _review_from_dict(data: dict) -> WBReview:
    return _dataclass_from_dict(WBReview, data)


def _analysis_from_dict(data: dict | None) -> ReviewAnalysisResult | None:
    if not isinstance(data, dict):
        return None
    return ReviewAnalysisResult(
        stats=_dataclass_from_dict(ReviewStats, data.get("stats") or {}),
        anomalies=[_dataclass_from_dict(ReviewAnomaly, item) for item in data.get("anomalies") or []],
        top_positive_reviews=[_review_from_dict(item) for item in data.get("top_positive_reviews") or []],
        top_negative_reviews=[_review_from_dict(item) for item in data.get("top_negative_reviews") or []],
        top_useful_reviews=[_review_from_dict(item) for item in data.get("top_useful_reviews") or []],
        defect_reviews=[_review_from_dict(item) for item in data.get("defect_reviews") or []],
        frequent_pros=list(data.get("frequent_pros") or []),
        frequent_cons=list(data.get("frequent_cons") or []),
        risk_score=int(data.get("risk_score") or 0),
        risk_level=str(data.get("risk_level") or "НИЗКИЙ"),
        fake_review_signals=list(data.get("fake_review_signals") or []),
        filtered_reviews=[_review_from_dict(item) for item in data.get("filtered_reviews") or []],
    )


def _result_from_cache_payload(payload: dict) -> WBAnalysisResult | None:
    try:
        product = _dataclass_from_dict(WBProduct, payload.get("product") or {})
        review_analysis = _analysis_from_dict(payload.get("review_analysis"))
        return WBAnalysisResult(
            product=product,
            selected_reviews=[_review_from_dict(item) for item in payload.get("selected_reviews") or []],
            summary_html=str(payload.get("summary_html") or ""),
            total_reviews_loaded=int(payload.get("total_reviews_loaded") or 0),
            review_analysis=review_analysis,
            summary_pack=str(payload.get("summary_pack") or ""),
        )
    except Exception as error:
        log_api_error(f"WB cache payload parse error: {error}")
        return None


def _cache_root_dir() -> Path:
    configured = (settings.WB_CACHE_DIR or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(settings.SQLITE_DB_PATH or "users.db").expanduser().parent / "wb_cache"


def _cache_file_path(cache_key: str) -> Path:
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    return _cache_root_dir() / f"{digest}.json"


def _read_cache_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _write_cache_file(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
    temp_path.replace(path)


async def _load_cached_analysis(cache_key: str, ttl_seconds: int) -> WBAnalysisResult | None:
    cached = _analysis_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < ttl_seconds:
        return cached[1]

    path = _cache_file_path(cache_key)
    try:
        payload = await asyncio.to_thread(_read_cache_file, path)
    except Exception as error:
        log_api_error(f"WB cache read error: key={cache_key}, error={error}")
        return None
    if not payload:
        return None
    created_at = float(payload.get("created_at") or 0)
    if time.time() - created_at > ttl_seconds:
        return None
    result = _result_from_cache_payload(payload.get("result") or {})
    if result:
        _analysis_cache[cache_key] = (time.monotonic(), result)
        logger.info("WB analysis cache hit: key=%s, article=%s", cache_key, result.product.article)
    return result


async def _store_cached_analysis(cache_key: str, result: WBAnalysisResult) -> None:
    payload = {
        "created_at": time.time(),
        "cache_key": cache_key,
        "result": asdict(result),
    }
    _analysis_cache[cache_key] = (time.monotonic(), result)
    if len(_analysis_cache) > 256:
        oldest_key = min(_analysis_cache, key=lambda key_: _analysis_cache[key_][0])
        _analysis_cache.pop(oldest_key, None)
    try:
        await asyncio.to_thread(_write_cache_file, _cache_file_path(cache_key), payload)
    except Exception as error:
        log_api_error(f"WB cache write error: key={cache_key}, error={error}")


def _build_summary_user_prompt(product: WBProduct, reviews: list[WBReview]) -> str:
    price_block = []
    if product.price is not None:
        price_block.append(f"Цена без скидки: {product.price} RUB")
    if product.sale_price is not None:
        price_block.append(f"Цена со скидкой: {product.sale_price} RUB")
    price_text = "; ".join(price_block) if price_block else "Цена не получена"

    review_lines = []
    for index, review in enumerate(reviews, start=1):
        parts = [f"{index}. Оценка: {review.rating}/5"]
        if review.text:
            parts.append(f"Текст: {review.text}")
        if review.pros:
            parts.append(f"Плюсы из отзыва: {review.pros}")
        if review.cons:
            parts.append(f"Минусы из отзыва: {review.cons}")
        if review.created_at:
            parts.append(f"Дата: {review.created_at}")
        review_lines.append(" | ".join(parts))

    fraud_signals = _build_review_fraud_signals(reviews, product.review_count)

    return (
        f"Проанализируй товар Wildberries по строгому формату SmartCheck.\n"
        f"Артикул: {product.article}\n"
        f"Название: {product.title}\n"
        f"Бренд: {product.brand or 'Не указан'}\n"
        f"Рейтинг карточки: {product.rating or 'Нет данных'}\n"
        f"Количество отзывов в карточке: {product.review_count}\n"
        f"{price_text}\n"
        f"Ссылка: {product.product_url}\n\n"
        f"Антинакруточные сигналы, рассчитанные кодом:\n{fraud_signals}\n\n"
        f"Ниже отзывы для анализа ({len(reviews)} шт.):\n"
        + "\n".join(review_lines)
    )


async def _fetch_json(
    client: httpx.AsyncClient,
    url: str,
    params: Optional[dict] = None
) -> dict:
    last_error = None

    for attempt in range(3):
        try:
            response = await client.get(
                url,
                params=params,
                headers=WB_HEADERS,
                timeout=20.0
            )

            if response.status_code in (403, 429, 498):
                raise WBTemporaryUnavailable(WB_TEMPORARY_UNAVAILABLE_MESSAGE)

            if response.status_code == 404:
                raise WBNotFound("Товар не найден в Wildberries.")

            if 500 <= response.status_code < 600:
                last_error = WBError(f"WB server error: status={response.status_code}")
                await asyncio.sleep(1.0 + attempt * 0.7)
                continue

            response.raise_for_status()

            payload = response.json()

            if not isinstance(payload, dict):
                raise WBError("WB вернул неожиданный формат данных.")

            return payload

        except (httpx.RequestError, httpx.TimeoutException) as e:
            last_error = e
            await asyncio.sleep(1.5 + attempt * 0.5)
            continue

    raise WBError(f"WB request failed after retries: {last_error}")


def _select_wb_proxy() -> str:
    proxy_config = (settings.WB_PROXY_URL or "").strip()
    if not proxy_config:
        return ""
    proxies = [item.strip() for item in proxy_config.split(",") if item.strip()]
    if not proxies:
        return ""
    if settings.WB_ENABLE_ROTATING_PROXIES and len(proxies) > 1:
        return random.choice(proxies)
    return proxies[0]


def _wb_http_client_kwargs(timeout: float = 20.0) -> dict:
    kwargs = {"timeout": timeout, "follow_redirects": True}
    proxy_url = _select_wb_proxy()
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return kwargs


async def fetch_wb_product(article: str) -> WBProduct:
    last_error = None

    nm_id = None

    try:
        search_data = await search_wb_article(article)

        if search_data and search_data.get("nmId"):
            nm_id = search_data["nmId"]

    except Exception as e:
        last_error = e
        log_api_error(f"WB search failed: {e}")

    query_id = str(nm_id) if nm_id else article

    candidate_calls = [
        (
            "https://card.wb.ru/cards/v4/detail",
            {"appType": 1, "curr": "rub", "dest": -1257786, "nm": query_id},
        ),
        (
            "https://card.wb.ru/cards/detail",
            {"appType": 1, "curr": "rub", "dest": -1257786, "nm": query_id},
        ),
        (
            "https://card.wb.ru/cards/detail",
            {"appType": 1, "curr": "rub", "dest": -1027, "nm": query_id},
        ),
        (
            "https://card.wb.ru/cards/v1/detail",
            {"appType": 1, "curr": "rub", "dest": -1257786, "nm": query_id},
        ),
    ]

    async with httpx.AsyncClient(**_wb_http_client_kwargs()) as client:
        for url, params in candidate_calls:
            try:
                payload = await _fetch_json(client, url, params=params)

                products = _extract_products(payload)

                if products:
                    return _parse_card_product(products[0], query_id)

                log_api_error(f"WB empty products: article={article}, query_id={query_id}")

            except WBTemporaryUnavailable:
                raise

            except Exception as error:
                last_error = error
                log_api_error(
                    f"WB card failed: url={url}, article={article}, query_id={query_id}, error={error}"
                )
                continue

    try:
        log_api_error(f"WB HTML fallback: article={article}, query_id={query_id}")

        product_html = await _safe_html_parse(query_id)

        return WBProduct(
            article=product_html.article,
            title=product_html.title,
            brand="",
            root_id="",
            price=product_html.price,
            sale_price=product_html.price,
            rating=product_html.rating,
            review_count=product_html.review_count,
            image_url=product_html.image_url,
            product_url=product_html.product_url,
            description=product_html.description,
        )

    except Exception as html_error:
        log_api_error(f"WB HTML fallback failed: {html_error}")

        raise WBError(WB_ANALYSIS_UNAVAILABLE_MESSAGE)

def _unique_non_empty(values: list[str]) -> list[str]:
    unique = []
    seen = set()
    for value in values:
        cleaned = str(value or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        unique.append(cleaned)
    return unique


def _get_analysis_semaphore() -> asyncio.Semaphore:
    global _analysis_semaphore
    max_concurrent = max(1, int(settings.WB_MAX_CONCURRENT_ANALYSES or 4))
    if _analysis_semaphore is None:
        _analysis_semaphore = asyncio.Semaphore(max_concurrent)
    return _analysis_semaphore


def _parse_wb_review_limit_setting(product_review_count: int | None = None) -> int:
    raw_limit = str(settings.WB_MAX_REVIEWS_TOTAL or "3000").strip().lower()
    if raw_limit in {"all", "все", "full", "unlimited", "0", "-1", "*"}:
        if product_review_count and product_review_count > 0:
            return max(1, int(product_review_count))
        return 1_000_000
    try:
        return max(1, int(raw_limit))
    except (TypeError, ValueError):
        log_api_error(f"Invalid WB_MAX_REVIEWS_TOTAL={settings.WB_MAX_REVIEWS_TOTAL!r}; fallback to 3000")
        return 3000


def _wb_cache_variant() -> str:
    review_limit = str(settings.WB_MAX_REVIEWS_TOTAL or "3000").strip().lower() or "3000"
    return re.sub(r"[^a-zа-я0-9_-]+", "_", review_limit, flags=re.IGNORECASE)


def _adaptive_review_limit(product_review_count: int | None, requested_limit: int | None = None) -> int:
    hard_limit = _parse_wb_review_limit_setting(product_review_count)
    limit = requested_limit or hard_limit
    if product_review_count:
        limit = min(limit, product_review_count)
    return min(hard_limit, max(1, limit))


async def fetch_all_wb_reviews(
    article: str,
    root_id: str = "",
    max_reviews: int | None = None,
    product_review_count: int | None = None,
) -> list[WBReview]:
    review_limit = _adaptive_review_limit(product_review_count, max_reviews)
    batch_size = min(max(1, int(settings.WB_BATCH_SIZE or 100)), review_limit)
    max_seconds = max(5, int(settings.WB_MAX_PARSE_SECONDS or 45))
    request_delay = max(0.0, float(settings.WB_REQUEST_DELAY_SECONDS or 0.35))
    candidate_calls = []
    for feedback_id in _unique_non_empty([root_id, article]):
        for host in ("feedbacks2.wb.ru", "feedbacks1.wb.ru"):
            for version in ("v2", "v1"):
                candidate_calls.append(
                    (
                        f"https://{host}/feedbacks/{version}/{feedback_id}",
                        feedback_id,
                    )
                )

    started_at = time.monotonic()
    best_reviews: list[WBReview] = []

    async with httpx.AsyncClient(**_wb_http_client_kwargs()) as client:
        for url, feedback_id in candidate_calls:
            reviews_for_endpoint: list[WBReview] = []
            skip = 0
            while len(reviews_for_endpoint) < review_limit:
                if time.monotonic() - started_at >= max_seconds:
                    log_api_error(
                        f"WB reviews parse time limit: article={article}, loaded={len(best_reviews) or len(reviews_for_endpoint)}, limit_seconds={max_seconds}"
                    )
                    return (reviews_for_endpoint or best_reviews)[:review_limit]
                try:
                    payload = await _fetch_json(
                        client,
                        url,
                        params={"take": batch_size, "skip": skip},
                    )
                    batch = _parse_reviews(payload)
                    if not batch:
                        break
                    reviews_for_endpoint.extend(batch)
                    if len(reviews_for_endpoint) > len(best_reviews):
                        best_reviews = reviews_for_endpoint[:]
                    if len(batch) < batch_size:
                        break
                    skip += batch_size
                    if request_delay:
                        await asyncio.sleep(request_delay * random.uniform(0.75, 1.35))
                except WBNotFound:
                    break
                except WBTemporaryUnavailable as error:
                    log_api_error(
                        f"WB feedback temporary unavailable: article={article}, feedback_id={feedback_id}, loaded={len(reviews_for_endpoint)}, error={error}"
                    )
                    break
                except Exception as error:
                    log_api_error(
                        f"WB feedback endpoint failed: url={url}, skip={skip}, take={batch_size}, article={article}, error={error}"
                    )
                    break
            if reviews_for_endpoint:
                return reviews_for_endpoint[:review_limit]

    try:
        from app.services.wb_html_parser import parse_wb_reviews_html
        url = f"https://www.wildberries.ru/catalog/{article}/detail.aspx"
        reviews_html = await asyncio.to_thread(parse_wb_reviews_html, url, min(review_limit, 100))
        reviews = []
        for r in reviews_html:
            reviews.append(WBReview(
                text=_clean_review_field(r.text),
                rating=r.rating,
                created_at=r.created_at,
                pros="",
                cons=""
            ))
        return reviews[:review_limit]
    except Exception as html_error:    
        log_api_error(f"WB HTML reviews parse error: {html_error}")
        return best_reviews[:review_limit]


async def fetch_wb_reviews(article: str, root_id: str = "") -> list[WBReview]:
    return await fetch_all_wb_reviews(article, root_id, max_reviews=50)


async def summarize_wb_reviews(product: WBProduct, reviews: list[WBReview]) -> str:
    client = openai.AsyncOpenAI(
        api_key=settings.PROXYAPI_KEY,
        base_url=settings.PROXYAPI_URL,
        timeout=settings.PROXYAPI_TIMEOUT_SECONDS,
    )
    response = await client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": get_wb_summary_prompt()},
            {"role": "user", "content": _build_summary_user_prompt(product, reviews)},
        ],
        stream=False,
    )
    return response.choices[0].message.content or "<b>Анализ не получен.</b>"


async def summarize_wb_reviews_from_pack(summary_pack: str) -> str:
    client = openai.AsyncOpenAI(
        api_key=settings.PROXYAPI_KEY,
        base_url=settings.PROXYAPI_URL,
        timeout=settings.PROXYAPI_TIMEOUT_SECONDS,
    )
    response = await client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": get_wb_summary_prompt()},
            {"role": "user", "content": summary_pack},
        ],
        stream=False,
    )
    summary_html = response.choices[0].message.content or "<b>Анализ не получен.</b>"
    verdict = _extract_recommended_verdict(summary_pack)
    return _enforce_summary_verdict(summary_html, verdict)


def _extract_recommended_verdict(summary_pack: str) -> str:
    marker = "РЕКОМЕНДОВАННЫЙ ВЕРДИКТ ПО МАТЕМАТИКЕ SMARTCHECK:"
    marker_index = summary_pack.find(marker)
    if marker_index < 0:
        return ""
    tail = summary_pack[marker_index + len(marker):].strip()
    first_line = tail.splitlines()[0].strip() if tail else ""
    if first_line in {"РЕКОМЕНДУЕМ", "РИСКОВАННО", "НЕ РЕКОМЕНДУЕМ"}:
        return first_line
    return ""


def _enforce_summary_verdict(summary_html: str, verdict: str) -> str:
    if verdict not in {"РЕКОМЕНДУЕМ", "РИСКОВАННО", "НЕ РЕКОМЕНДУЕМ"}:
        return summary_html
    header = f"🕵️ <b>SmartCheck: {verdict}</b>"
    pattern = re.compile(
        r"^\s*(?:🕵️\s*)?(?:<b>)?SmartCheck:\s*(?:РЕКОМЕНДУЕМ|РИСКОВАННО|НЕ РЕКОМЕНДУЕМ)(?:</b>)?",
        re.IGNORECASE,
    )
    if pattern.search(summary_html):
        summary_html = pattern.sub(header, summary_html, count=1).lstrip()
    else:
        summary_html = f"{header}\n\n{summary_html.lstrip()}"

    heading_replacements = {
        "<b>Накрутка отзывов:</b>": "<b>Проверка накрутки отзывов:</b>",
        "<b>Отзывы:</b>": "<b>Проверка накрутки отзывов:</b>",
        "Накрутка отзывов:": "Проверка накрутки отзывов:",
        "Отзывы:": "Проверка накрутки отзывов:",
    }
    for old, new in heading_replacements.items():
        summary_html = summary_html.replace(old, new)

    if verdict == "РЕКОМЕНДУЕМ":
        replacements = {
            "<b>Что реально хорошо:</b>": "<b>Сильные стороны:</b>",
            "<b>Где подвох:</b>": "<b>На что обратить внимание:</b>",
            "Где подвох:": "На что обратить внимание:",
        }
        for old, new in replacements.items():
            summary_html = summary_html.replace(old, new)
    return summary_html


def format_product_preview(product: WBProduct, loaded_reviews: int, selected_reviews: int) -> str:
    lines = [
        f"<b>{escape(str(product.title), quote=False)}</b>",
        f"Артикул: <code>{escape(str(product.article), quote=False)}</code>",
    ]
    if product.brand:
        lines.append(f"Бренд: <b>{escape(str(product.brand), quote=False)}</b>")
    if product.sale_price is not None:
        lines.append(f"Цена: <b>{product.sale_price} ₽</b>")
    elif product.price is not None:
        lines.append(f"Цена: <b>{product.price} ₽</b>")
    if product.rating is not None:
        lines.append(f"Рейтинг: <b>{product.rating}</b>")
    lines.append(f"Отзывов загружено: <b>{loaded_reviews}</b>, в анализ взято: <b>{selected_reviews}</b>")
    return "\n".join(lines)


async def analyze_wb_product(user_input: str) -> WBAnalysisResult:
    article = extract_wb_article(user_input)
    if not article:
        raise WBNotFound("Не удалось распознать артикул или ссылку Wildberries.")

    cache_ttl = max(0, int(settings.WB_CACHE_TTL_SECONDS or 0))
    cache_key = f"{article}:{time.strftime('%Y-%m-%d')}:analysis-v2:reviews-{_wb_cache_variant()}"
    if cache_ttl:
        cached = await _load_cached_analysis(cache_key, cache_ttl)
        if cached:
            return cached

    semaphore = _get_analysis_semaphore()
    started_at = time.monotonic()
    try:
        async with semaphore:
            cached = await _load_cached_analysis(cache_key, cache_ttl) if cache_ttl else None
            if cached:
                return cached

            product = await fetch_wb_product(article)
            reviews = await fetch_all_wb_reviews(
                product.article,
                product.root_id,
                product_review_count=product.review_count,
            )
            review_analysis = analyze_reviews_locally(reviews)
            summary_pack = build_summary_pack(product, review_analysis)
            summary_html = await summarize_wb_reviews_from_pack(summary_pack)

            selected_reviews_map: dict[tuple[str, str, str, str], WBReview] = {}
            for review in (
                review_analysis.top_negative_reviews
                + review_analysis.top_positive_reviews
                + review_analysis.top_useful_reviews
                + review_analysis.defect_reviews
            ):
                key = (review.text, review.pros, review.cons, review.created_at)
                selected_reviews_map.setdefault(key, review)
            sent_examples_count = len(selected_reviews_map)

            elapsed = round(time.monotonic() - started_at, 2)
            logger.info(
                "WB analysis complete: "
                f"article={product.article}, card_reviews={product.review_count}, "
                f"loaded={len(reviews)}, filtered={review_analysis.stats.total_after_filter}, "
                f"sent_examples={sent_examples_count}, risk_score={review_analysis.risk_score}, "
                f"elapsed_seconds={elapsed}"
            )

            result = WBAnalysisResult(
                product=product,
                selected_reviews=review_analysis.filtered_reviews,
                summary_html=summary_html,
                total_reviews_loaded=len(reviews),
                review_analysis=review_analysis,
                summary_pack=summary_pack,
            )
            if cache_ttl:
                await _store_cached_analysis(cache_key, result)
            return result
    except WBError:
        raise
    except openai.APIError as error:
        log_api_error(error)
        raise WBError("Не удалось получить анализ от ИИ. Попробуйте позже.") from error
    except Exception as error:
        log_api_error(error)
        raise WBError(WB_ANALYSIS_UNAVAILABLE_MESSAGE) from error

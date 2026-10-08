import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services.price_compare import (
    ComparableProduct,
    MarketplaceRiskAssessment,
    PriceCompareError,
    PriceCompareProRequired,
    PriceComparisonItem,
    PriceComparisonResult,
    MarketplaceComparison,
    _compare_marketplace,
    _extract_page_product_metadata,
    _extract_reputation_from_html,
    _prefilter_candidate,
    _safe_get_cache,
    _safe_set_cache,
    _score_similarity,
    assess_marketplace_item_risk,
    build_price_risk_decision,
    build_search_query,
    compare_wb_product_prices,
    compare_wb_prices,
    format_price_comparison_html,
    format_price_comparison_plain,
    format_price_comparison_teaser_plain,
)
from app.services.wb import WBProduct, _parse_card_product, extract_wb_article
from app.services import db as db_service
from app.platforms.vk.handlers import (
    build_price_compare_keyboard,
    build_price_compare_locked_keyboard,
    build_price_compare_pro_keyboard,
    build_start_keyboard,
    format_price_compare_locked_cta_plain,
    format_price_compare_pro_offer_plain,
)


def wb_case(**overrides) -> WBProduct:
    values = {
        "article": "443655530",
        "title": "Чехол на iPhone 13 и 14 кожаный",
        "brand": "LOVRENT",
        "subject_name": "Чехлы",
        "category_name": "Аксессуары",
        "price": 599.0,
        "sale_price": 435.0,
        "rating": 4.8,
        "review_count": 11317,
        "product_url": "https://www.wildberries.ru/catalog/443655530/detail.aspx",
        "description": "Кожаный защитный чехол с поддержкой MagSafe",
    }
    values.update(overrides)
    return WBProduct(**values)


def market_case(marketplace: str, price: float, suffix: str = "") -> ComparableProduct:
    host = "www.ozon.ru" if marketplace == "ozon" else "market.yandex.ru"
    path = "product" if marketplace == "ozon" else "card"
    return ComparableProduct(
        marketplace=marketplace,
        title=f"LOVRENT Кожаный чехол для Apple iPhone 13 и 14 MagSafe {suffix}".strip(),
        price=price,
        url=f"https://{host}/{path}/leather-case-{suffix or 'base'}/",
        rating=4.7,
        review_count=120,
        category="Чехлы",
    )


class PriceCompareUnitTests(unittest.TestCase):
    def test_vk_keyboards_have_valid_buttons_and_payload(self):
        start_keyboard = json.loads(build_start_keyboard())
        self.assertTrue(start_keyboard["buttons"])
        self.assertFalse(start_keyboard["inline"])
        self.assertFalse(start_keyboard["one_time"])
        start_actions = [button for row in start_keyboard["buttons"] for button in row]
        self.assertTrue(all(button.get("color") for button in start_actions))
        self.assertIn("help", {json.loads(button["action"]["payload"])["cmd"] for button in start_actions})
        compare_keyboard = json.loads(build_price_compare_keyboard("443655530"))
        self.assertTrue(compare_keyboard["inline"])
        action = compare_keyboard["buttons"][0][0]["action"]
        self.assertEqual(action["type"], "text")
        self.assertEqual(json.loads(action["payload"])["article"], "443655530")

        pro_keyboard = json.loads(build_price_compare_pro_keyboard("42"))
        pro_action = pro_keyboard["buttons"][0][0]["action"]
        self.assertEqual(pro_action["type"], "open_link")
        self.assertIn("billing/checkout", pro_action["link"])
        self.assertIn("Сравнение цен доступно", format_price_compare_pro_offer_plain())

        locked_keyboard = json.loads(build_price_compare_locked_keyboard("42", "443655530"))
        locked_actions = [button["action"] for row in locked_keyboard["buttons"] for button in row]
        self.assertEqual(locked_actions[0]["type"], "text")
        self.assertEqual(json.loads(locked_actions[0]["payload"])["cmd"], "price_compare")
        self.assertTrue(any(action.get("type") == "open_link" for action in locked_actions))
        self.assertIn("Оплатить PRO", format_price_compare_locked_cta_plain(True, "42"))

    def _risk_result(self, item: PriceComparisonItem) -> PriceComparisonResult:
        market = MarketplaceComparison(item.product.marketplace, items=[item], raw_count=1, filtered_count=1)
        return PriceComparisonResult(
            wb_product=wb_case(price=4000, sale_price=4000),
            wb_price=4000,
            search_query="товар",
            ozon=market if item.product.marketplace == "ozon" else MarketplaceComparison("ozon"),
            yandex=market if item.product.marketplace == "yandex" else MarketplaceComparison("yandex"),
            best_marketplace=item.product.marketplace,
            best_price=item.product.price or 0,
            best_url=item.product.url,
            best_title=item.product.title,
            status="success",
        )

    def test_low_price_with_high_risk_is_not_recommended(self):
        product = market_case("ozon", 2000)
        item = PriceComparisonItem(product, 80, "высокое", -2000, -50, "дешевле WB")
        item.risk = MarketplaceRiskAssessment(50, 90, "высокий", ["низкий рейтинг"], "полные данные")
        decision = build_price_risk_decision(self._risk_result(item), wb_risk_score=20)
        self.assertEqual(decision.code, "keep_wb_risk")
        self.assertEqual(decision.recommended_marketplace, "wb")

    def test_good_reputation_and_savings_can_be_recommended(self):
        product = market_case("yandex", 3000)
        item = PriceComparisonItem(product, 82, "высокое", -1000, -25, "дешевле WB")
        item.risk = MarketplaceRiskAssessment(12, 88, "низкий", [], "полные данные")
        decision = build_price_risk_decision(self._risk_result(item), wb_risk_score=20)
        self.assertEqual(decision.code, "recommend_alternative")
        self.assertEqual(decision.recommended_marketplace, "yandex")

    def test_missing_reputation_reduces_confidence_not_inflates_risk(self):
        product = market_case("ozon", 3000)
        product.rating = None
        product.review_count = None
        item = PriceComparisonItem(product, 65, "хорошее", -1000, -25, "дешевле WB")
        risk = assess_marketplace_item_risk(item)
        self.assertLess(risk.confidence, 50)
        self.assertLess(risk.score, 45)
        self.assertEqual(risk.level, "не подтвержден")
        item.risk = risk
        decision = build_price_risk_decision(self._risk_result(item), wb_risk_score=20)
        self.assertEqual(decision.code, "verify_first")

    def test_extracts_structured_marketplace_reputation(self):
        html = """
        <script type="application/ld+json">
        {"@type":"Product","aggregateRating":{"ratingValue":"4.8","reviewCount":"1 240"}}
        </script>
        """
        self.assertEqual(_extract_reputation_from_html(html), (4.8, 1240))

    def test_extracts_json_ld_product_metadata(self):
        html = """
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Product","name":"Nike Air Max 90 black",
         "brand":{"@type":"Brand","name":"Nike"},"sku":"AM90-BLK","color":"black",
         "image":["https://cdn.example/item.jpg"],"category":"Sneakers",
         "aggregateRating":{"ratingValue":"4.8","reviewCount":"1250"},
         "offers":{"@type":"Offer","price":"7990"}}
        </script>
        """
        metadata = _extract_page_product_metadata(html)
        self.assertEqual(metadata["brand"], "Nike")
        self.assertEqual(metadata["attributes"]["sku"], "AM90-BLK")
        self.assertEqual(metadata["attributes"]["color"], "black")
        self.assertEqual(metadata["review_count"], 1250)
        self.assertEqual(metadata["price"], 7990.0)

    def test_ignores_non_product_json_ld(self):
        html = '<script type="application/ld+json">{"@type":"BreadcrumbList","name":"Catalog"}</script>'
        self.assertEqual(_extract_page_product_metadata(html), {})

    def test_metadata_prefilter_allows_missing_brand_but_rejects_wrong_type(self):
        source = WBProduct(
            article="1",
            title="Nike Air Max 90 кроссовки мужские черные",
            brand="Nike",
            subject_name="Кроссовки",
        )
        incomplete = ComparableProduct(
            "ozon", "Air Max 90 кроссовки мужские черные", 7000,
            "https://www.ozon.ru/product/air-max-90/", raw={"source": "test"},
        )
        wrong = ComparableProduct(
            "ozon", "Nike Air Max футболка мужская черная", 2000,
            "https://www.ozon.ru/product/nike-shirt/", raw={"source": "test"},
        )
        self.assertTrue(_prefilter_candidate(source, incomplete))
        self.assertFalse(_prefilter_candidate(source, wrong))

    def test_wb_price_skips_unavailable_first_size(self):
        product = _parse_card_product(
            {
                "id": 786295569,
                "name": "Джинсы бананы широкие",
                "sizes": [
                    {"name": "42-44", "price": None, "stocks": []},
                    {
                        "name": "44",
                        "price": {"basic": 1000000, "product": 291000},
                        "stocks": [{"qty": 2}],
                    },
                ],
            },
            "786295569",
        )
        self.assertEqual(product.price, 10000.0)
        self.assertEqual(product.sale_price, 2910.0)

    def test_extracts_wb_article_from_link_and_plain_article(self):
        self.assertEqual(extract_wb_article("443655530"), "443655530")
        self.assertEqual(
            extract_wb_article("https://www.wildberries.ru/catalog/443655530/detail.aspx?targetUrl=SN"),
            "443655530",
        )
        self.assertIsNone(extract_wb_article("не товар"))

    def test_search_query_contains_product_type_model_and_brand(self):
        query = build_search_query(wb_case())
        self.assertIn("lovrent", query)
        self.assertIn("чехол", query)
        self.assertIn("iphone", query)
        self.assertIn("13", query)
        self.assertIn("magsafe", query)
        self.assertNotIn("и14", query)

    def test_similarity_keeps_case_and_rejects_phone(self):
        source = wb_case()
        similar = ComparableProduct(
            "ozon",
            "LOVRENT-kozhanyy-chehol-iphone-13-leather-case-magsafe",
            500,
            "https://www.ozon.ru/product/case/",
        )
        phone = ComparableProduct(
            "ozon",
            "smartfon-apple-iphone-13-128-gb",
            50000,
            "https://www.ozon.ru/product/phone/",
        )
        battery = ComparableProduct(
            "ozon",
            "Аккумуляторная батарея для iPhone 13",
            2000,
            "https://www.ozon.ru/product/battery/",
        )
        self.assertGreaterEqual(_score_similarity(source, similar), 35)
        self.assertEqual(_score_similarity(source, phone), 0)
        self.assertLess(_score_similarity(source, battery), 35)

    def test_filter_sort_and_price_math(self):
        source = wb_case()
        comparison = _compare_marketplace(
            "ozon",
            source,
            435.0,
            [market_case("ozon", 390, "cheap"), market_case("ozon", 500, "expensive")],
            False,
        )
        self.assertEqual(len(comparison.items), 2)
        cheap = next(item for item in comparison.items if item.product.price == 390)
        self.assertEqual(cheap.price_diff, -45.0)
        self.assertEqual(cheap.price_diff_percent, -10.3)
        self.assertEqual(cheap.relation, "дешевле WB")


class PriceCompareAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_result_best_market_and_history(self):
        source = wb_case()
        with (
            patch("app.services.price_compare.has_active_pro_access", AsyncMock(return_value=True)),
            patch("app.services.price_compare._safe_get_cache", AsyncMock(return_value=None)),
            patch("app.services.price_compare._safe_set_cache", AsyncMock()) as cache_write,
            patch("app.services.price_compare.search_ozon", AsyncMock(return_value=([market_case("ozon", 390)], False, ""))),
            patch("app.services.price_compare.search_yandex_market", AsyncMock(return_value=([market_case("yandex", 470)], False, ""))),
            patch("app.services.price_compare._record_result_history", AsyncMock()) as history_write,
        ):
            result = await compare_wb_product_prices(source, "443655530", "telegram", "42")

        self.assertEqual(result.status, "success")
        self.assertEqual(result.best_marketplace, "ozon")
        self.assertEqual(result.best_price, 390)
        self.assertEqual(result.ozon.items[0].price_diff, -45.0)
        cache_write.assert_awaited_once()
        history_write.assert_awaited_once()

    async def test_partial_result_survives_one_source_failure(self):
        with (
            patch("app.services.price_compare._safe_get_cache", AsyncMock(return_value=None)),
            patch("app.services.price_compare._safe_set_cache", AsyncMock()),
            patch("app.services.price_compare.search_ozon", AsyncMock(side_effect=RuntimeError("blocked"))),
            patch("app.services.price_compare.search_yandex_market", AsyncMock(return_value=([market_case("yandex", 420)], False, ""))),
            patch("app.services.price_compare._record_result_history", AsyncMock()),
        ):
            result = await compare_wb_product_prices(wb_case())

        self.assertEqual(result.status, "partial")
        self.assertFalse(result.ozon.items)
        self.assertTrue(result.yandex.items)
        self.assertTrue(any("Ozon" in error for error in result.errors))

    async def test_no_matching_products_returns_error_result(self):
        with (
            patch("app.services.price_compare._safe_get_cache", AsyncMock(return_value=None)),
            patch("app.services.price_compare._safe_set_cache", AsyncMock()),
            patch("app.services.price_compare.search_ozon", AsyncMock(return_value=([], False, ""))),
            patch("app.services.price_compare.search_yandex_market", AsyncMock(return_value=([], False, ""))),
        ):
            result = await compare_wb_product_prices(wb_case())

        self.assertEqual(result.status, "error")
        self.assertEqual(result.best_marketplace, "wb")
        self.assertIn("надежных аналогов", format_price_comparison_plain(result))

    async def test_invalid_input_is_reported_and_recorded(self):
        with patch("app.services.price_compare._record_failure_history", AsyncMock()) as history_write:
            with self.assertRaises(PriceCompareError):
                await compare_wb_prices("не ссылка", "telegram", "42")
        history_write.assert_awaited_once()

    async def test_missing_wb_price_is_reported_and_recorded(self):
        with (
            patch("app.services.price_compare.has_active_pro_access", AsyncMock(return_value=True)),
            patch("app.services.price_compare._record_failure_history", AsyncMock()) as history_write,
        ):
            with self.assertRaisesRegex(PriceCompareError, "цену Wildberries"):
                await compare_wb_product_prices(
                    wb_case(price=None, sale_price=None), "443655530", "telegram", "42"
                )
        history_write.assert_awaited_once()

    async def test_non_pro_user_is_blocked_before_marketplace_search(self):
        ozon_search = AsyncMock()
        with (
            patch("app.services.price_compare.has_active_pro_access", AsyncMock(return_value=False)),
            patch("app.services.price_compare.search_ozon", ozon_search),
        ):
            with self.assertRaises(PriceCompareProRequired):
                await compare_wb_product_prices(wb_case(), "443655530", "telegram", "free-user")
        ozon_search.assert_not_awaited()

    async def test_cache_failure_does_not_break_comparison(self):
        with patch(
            "app.services.price_compare.get_price_compare_cache",
            AsyncMock(side_effect=RuntimeError("db unavailable")),
        ):
            self.assertIsNone(await _safe_get_cache("key"))
        with patch(
            "app.services.price_compare.set_price_compare_cache",
            AsyncMock(side_effect=RuntimeError("db unavailable")),
        ):
            await _safe_set_cache("key", {"ok": True}, 30)

    async def test_html_and_plain_output_have_required_blocks(self):
        with (
            patch("app.services.price_compare._safe_get_cache", AsyncMock(return_value=None)),
            patch("app.services.price_compare._safe_set_cache", AsyncMock()),
            patch("app.services.price_compare.search_ozon", AsyncMock(return_value=([market_case("ozon", 390)], False, ""))),
            patch("app.services.price_compare.search_yandex_market", AsyncMock(return_value=([market_case("yandex", 470)], False, ""))),
        ):
            result = await compare_wb_product_prices(wb_case())

        html = format_price_comparison_html(result)
        plain = format_price_comparison_plain(result)
        for expected in ("цена + риск", "Ozon", "Яндекс Маркет", "Цена WB", "Очищенный вывод"):
            self.assertIn(expected, html)
            self.assertIn(expected, plain)
        self.assertIn("дешевле на", plain)
        self.assertIn("Совпадение", plain)
        self.assertIn("Есть что сравнить", format_price_comparison_teaser_plain(result))


class PriceCompareDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_cache_history_and_stats_roundtrip(self):
        old_backend = db_service.settings.DB_BACKEND
        old_path = db_service.settings.SQLITE_DB_PATH
        old_tester_ids = db_service.settings.TELEGRAM_FREE_TESTER_IDS
        with tempfile.TemporaryDirectory() as temp_dir:
            db_service.settings.DB_BACKEND = "sqlite"
            db_service.settings.SQLITE_DB_PATH = str(Path(temp_dir) / "acceptance.sqlite3")
            db_service.settings.TELEGRAM_FREE_TESTER_IDS = "7730699858"
            try:
                await db_service.init_db()
                async with db_service.connect_db() as db:
                    await db_service.execute_query(
                        db,
                        "INSERT INTO users (platform, user_id, balance_requests, free_requests_used_today) VALUES (?, ?, ?, ?)",
                        ("telegram", "7730699858", 0, 1),
                    )
                    await db.commit()

                allowed, reason, profile = await db_service.get_request_access("telegram", "7730699858")
                self.assertTrue(allowed)
                self.assertEqual(reason, "free_tester")
                self.assertTrue(profile["free_tester"])
                self.assertEqual(await db_service.consume_request_limit("telegram", "7730699858"), (True, "free_tester"))
                self.assertEqual(await db_service.consume_request_limit("telegram", "7730699858"), (True, "free_tester"))
                tester_profile = await db_service.get_user_profile("telegram", "7730699858")
                self.assertEqual(tester_profile["balance_requests"], 0)
                self.assertEqual(tester_profile["free_requests_used_today"], 0)
                self.assertTrue(await db_service.has_active_pro_access("telegram", "7730699858"))

                async with db_service.connect_db() as db:
                    await db_service.execute_query(
                        db,
                        "INSERT INTO users (platform, user_id) VALUES (?, ?)",
                        ("telegram", "package-user"),
                    )
                    await db_service.execute_query(
                        db,
                        "INSERT INTO users (platform, user_id) VALUES (?, ?)",
                        ("telegram", "pro-user"),
                    )
                    await db.commit()
                await db_service.add_request_balance("telegram", "package-user", 5)
                self.assertFalse(await db_service.has_active_pro_access("telegram", "package-user"))

                await db_service.activate_pro_subscription("telegram", "pro-user", days=30, requests=30)
                self.assertTrue(await db_service.has_active_pro_access("telegram", "pro-user"))
                async with db_service.connect_db() as db:
                    await db_service.execute_query(
                        db,
                        "UPDATE users SET pro_expires_at = ? WHERE platform = ? AND user_id = ?",
                        ("2000-01-01T00:00:00+00:00", "telegram", "pro-user"),
                    )
                    await db.commit()
                self.assertFalse(await db_service.has_active_pro_access("telegram", "pro-user"))

                async with db_service.connect_db() as db:
                    await db_service.execute_query(
                        db,
                        "UPDATE users SET blocked = 1 WHERE platform = ? AND user_id = ?",
                        ("telegram", "7730699858"),
                    )
                    await db.commit()
                allowed, reason, _ = await db_service.get_request_access("telegram", "7730699858")
                self.assertFalse(allowed)
                self.assertIn("ограничен", reason)
                self.assertFalse(await db_service.has_active_pro_access("telegram", "7730699858"))

                await db_service.set_price_compare_cache("acceptance", {"value": 42}, 60)
                self.assertEqual(
                    await db_service.get_price_compare_cache("acceptance"),
                    {"value": 42},
                )

                await db_service.record_price_compare_history(
                    platform="telegram",
                    user_id="42",
                    user_input="443655530",
                    article="443655530",
                    wb_title="Чехол",
                    wb_price=435,
                    search_query="чехол iphone 13",
                    ozon_results=[{"price_diff": -45}],
                    yandex_results=[{"price_diff": 20}],
                    best_marketplace="ozon",
                    best_price=390,
                    status="success",
                    elapsed_seconds=0.5,
                )
                stats = await db_service.get_price_compare_stats()
                self.assertEqual(stats["total"], 1)
                self.assertEqual(stats["success"], 1)
                self.assertEqual(stats["ozon_success_rate"], 100.0)
                self.assertEqual(stats["yandex_success_rate"], 100.0)
                self.assertEqual(stats["top_users"][0]["user_id"], "42")
                self.assertEqual(stats["top_products"][0]["article"], "443655530")
            finally:
                db_service.settings.DB_BACKEND = old_backend
                db_service.settings.SQLITE_DB_PATH = old_path
                db_service.settings.TELEGRAM_FREE_TESTER_IDS = old_tester_ids


if __name__ == "__main__":
    unittest.main()

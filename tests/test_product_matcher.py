import unittest

from app.services.product_matcher import build_identity, build_precise_query, match_identities


class ProductMatcherTests(unittest.TestCase):
    def match(self, source_title, candidate_title, source_brand="", candidate_brand=""):
        return match_identities(
            build_identity(source_title, source_brand),
            build_identity(candidate_title, candidate_brand),
        )

    def test_rejects_different_product_family(self):
        result = self.match(
            "\u041a\u0440\u043e\u0441\u0441\u043e\u0432\u043a\u0438 \u043c\u0443\u0436\u0441\u043a\u0438\u0435 Nike Air Max 90 \u0447\u0435\u0440\u043d\u044b\u0435",
            "\u0424\u0443\u0442\u0431\u043e\u043b\u043a\u0430 \u043c\u0443\u0436\u0441\u043a\u0430\u044f Nike Air \u0447\u0435\u0440\u043d\u0430\u044f",
            "Nike",
            "Nike",
        )
        self.assertFalse(result.accepted)
        self.assertTrue(any("footwear" in reason for reason in result.reject_reasons))

    def test_rejects_explicitly_different_brand(self):
        result = self.match(
            "\u041a\u0440\u043e\u0441\u0441\u043e\u0432\u043a\u0438 Nike Air Max 90",
            "\u041a\u0440\u043e\u0441\u0441\u043e\u0432\u043a\u0438 Adidas Air Max 90",
            "Nike",
            "Adidas",
        )
        self.assertFalse(result.accepted)
        self.assertTrue(any("\u0431\u0440\u0435\u043d\u0434" in reason for reason in result.reject_reasons))

    def test_rejects_unconfirmed_brand_even_when_compatible_model_matches(self):
        result = self.match(
            "LOVRENT \u0447\u0435\u0445\u043e\u043b \u0434\u043b\u044f iPhone 13",
            "Business Style \u0447\u0435\u0445\u043e\u043b \u0434\u043b\u044f iPhone 13",
            "LOVRENT",
        )
        self.assertFalse(result.accepted)
        self.assertTrue(any("\u0431\u0440\u0435\u043d\u0434" in reason for reason in result.reject_reasons))

    def test_rejects_different_electronics_model(self):
        result = self.match(
            "\u0427\u0435\u0445\u043e\u043b Apple \u0434\u043b\u044f iPhone 13 Pro",
            "\u0427\u0435\u0445\u043e\u043b Apple \u0434\u043b\u044f iPhone 14 Pro",
            "Apple",
            "Apple",
        )
        self.assertFalse(result.accepted)
        self.assertTrue(any("\u043c\u043e\u0434\u0435\u043b" in reason for reason in result.reject_reasons))

    def test_rejects_different_color(self):
        result = self.match(
            "\u041a\u0440\u043e\u0441\u0441\u043e\u0432\u043a\u0438 Nike Air Max 90 \u0447\u0435\u0440\u043d\u044b\u0435",
            "\u041a\u0440\u043e\u0441\u0441\u043e\u0432\u043a\u0438 Nike Air Max 90 \u0431\u0435\u043b\u044b\u0435",
            "Nike",
            "Nike",
        )
        self.assertFalse(result.accepted)
        self.assertTrue(any("\u0446\u0432\u0435\u0442" in reason for reason in result.reject_reasons))

    def test_rejects_different_audience(self):
        result = self.match(
            "\u041a\u0440\u043e\u0441\u0441\u043e\u0432\u043a\u0438 \u043c\u0443\u0436\u0441\u043a\u0438\u0435 Nike Air Max 90",
            "\u041a\u0440\u043e\u0441\u0441\u043e\u0432\u043a\u0438 \u0436\u0435\u043d\u0441\u043a\u0438\u0435 Nike Air Max 90",
            "Nike",
            "Nike",
        )
        self.assertFalse(result.accepted)

    def test_rejects_different_quantity(self):
        result = self.match(
            "\u041d\u043e\u0441\u043a\u0438 \u043c\u0443\u0436\u0441\u043a\u0438\u0435 5 \u043f\u0430\u0440",
            "\u041d\u043e\u0441\u043a\u0438 \u043c\u0443\u0436\u0441\u043a\u0438\u0435 3 \u043f\u0430\u0440\u044b",
        )
        self.assertFalse(result.accepted)

    def test_accepts_same_brand_model_color(self):
        result = self.match(
            "\u041a\u0440\u043e\u0441\u0441\u043e\u0432\u043a\u0438 \u043c\u0443\u0436\u0441\u043a\u0438\u0435 Nike Air Max 90 \u0447\u0435\u0440\u043d\u044b\u0435",
            "Nike Air Max 90 \u043c\u0443\u0436\u0441\u043a\u0438\u0435 \u043a\u0440\u043e\u0441\u0441\u043e\u0432\u043a\u0438 black",
            "Nike",
            "Nike",
        )
        self.assertTrue(result.accepted)
        self.assertGreaterEqual(result.score, 70)

    def test_precise_query_is_deterministic(self):
        identity = build_identity(
            "\u0427\u0435\u0445\u043e\u043b LOVRENT \u0434\u043b\u044f iPhone 13 MagSafe \u0447\u0435\u0440\u043d\u044b\u0439",
            "LOVRENT",
        )
        self.assertEqual(build_precise_query(identity), build_precise_query(identity))
        self.assertIn("iphone13", build_precise_query(identity))

    def test_rejects_when_model_is_missing(self):
        result = self.match(
            "Samsung Galaxy S24 Ultra \u0441\u043c\u0430\u0440\u0442\u0444\u043e\u043d",
            "Samsung Galaxy Ultra \u0441\u043c\u0430\u0440\u0442\u0444\u043e\u043d",
            "Samsung",
            "Samsung",
        )
        self.assertFalse(result.accepted)
        self.assertTrue(any("\u043c\u043e\u0434\u0435\u043b" in reason for reason in result.reject_reasons))

    def test_rejects_when_known_color_is_missing(self):
        result = self.match(
            "Nike Air Max 90 \u043a\u0440\u043e\u0441\u0441\u043e\u0432\u043a\u0438 \u0447\u0435\u0440\u043d\u044b\u0435",
            "Nike Air Max 90 \u043a\u0440\u043e\u0441\u0441\u043e\u0432\u043a\u0438",
            "Nike",
            "Nike",
        )
        self.assertFalse(result.accepted)
        self.assertTrue(any("\u0446\u0432\u0435\u0442" in reason for reason in result.reject_reasons))

    def test_rejects_when_bundle_quantity_is_missing(self):
        result = self.match(
            "\u041d\u043e\u0441\u043a\u0438 \u043c\u0443\u0436\u0441\u043a\u0438\u0435 5 \u043f\u0430\u0440",
            "\u041d\u043e\u0441\u043a\u0438 \u043c\u0443\u0436\u0441\u043a\u0438\u0435",
        )
        self.assertFalse(result.accepted)
        self.assertTrue(any("\u043a\u043e\u043c\u043f\u043b\u0435\u043a\u0442\u043d" in reason for reason in result.reject_reasons))

    def test_unknown_category_requires_strong_identity_evidence(self):
        result = self.match(
            "Acme \u043f\u0440\u043e\u0444\u0435\u0441\u0441\u0438\u043e\u043d\u0430\u043b\u044c\u043d\u044b\u0439 \u043d\u0430\u0431\u043e\u0440 \u0434\u043b\u044f \u0434\u043e\u043c\u0430",
            "Acme \u0443\u043d\u0438\u0432\u0435\u0440\u0441\u0430\u043b\u044c\u043d\u044b\u0439 \u0442\u043e\u0432\u0430\u0440 \u0434\u043b\u044f \u0434\u043e\u043c\u0430",
            "Acme",
            "Acme",
        )
        self.assertFalse(result.accepted)

    def test_underwear_is_not_mistaken_for_white_color(self):
        identity = build_identity("\u041d\u0438\u0436\u043d\u0435\u0435 \u0431\u0435\u043b\u044c\u0435 \u043c\u0443\u0436\u0441\u043a\u043e\u0435")
        self.assertNotIn("white", identity.colors)


if __name__ == "__main__":
    unittest.main()

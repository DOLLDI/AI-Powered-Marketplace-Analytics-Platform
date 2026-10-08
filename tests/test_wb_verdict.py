import unittest

from app.services.wb import (
    ReviewAnalysisResult,
    ReviewAnomaly,
    ReviewStats,
    WBProduct,
    WBReview,
    _fake_reviews_assessment,
    _recommended_verdict_from_analysis,
    detect_product_risk_profile,
    detect_review_anomalies,
)


def make_analysis(*, negative_percent=9.6, risk_score=4, anomalies=None, filtered_reviews=None):
    return ReviewAnalysisResult(
        stats=ReviewStats(
            total_loaded=2228,
            total_after_filter=2228,
            avg_rating=4.48,
            count_1=164,
            count_2=51,
            count_3=94,
            count_4=160,
            count_5=1759,
            negative_percent=negative_percent,
            neutral_percent=4.2,
            positive_percent=86.1,
        ),
        anomalies=anomalies or [],
        top_positive_reviews=[],
        top_negative_reviews=[],
        top_useful_reviews=[],
        defect_reviews=[],
        frequent_pros=[],
        frequent_cons=[],
        risk_score=risk_score,
        risk_level="НИЗКИЙ" if risk_score <= 20 else "СРЕДНИЙ",
        fake_review_signals=[],
        filtered_reviews=filtered_reviews or [],
    )


class WBVerdictRegressionTests(unittest.TestCase):
    def setUp(self):
        self.product = WBProduct(
            article="231802275",
            title="MP3 плеер Digma R3 flash 8Gb",
            brand="Digma",
            rating=4.8,
            review_count=2228,
        )

    def test_digma_like_profile_is_recommended(self):
        severe_reviews = [WBReview(text="не работает", rating=1) for _ in range(42)]
        mismatch = ReviewAnomaly(
            type="rating_complaint_mismatch",
            description="Поляризованное распределение оценок",
            severity="high",
        )
        analysis = make_analysis(anomalies=[mismatch], filtered_reviews=severe_reviews)

        self.assertEqual(_recommended_verdict_from_analysis(self.product, analysis), "РЕКОМЕНДУЕМ")
        self.assertIn("низкий риск", _fake_reviews_assessment(analysis))

    def test_genuinely_high_negative_share_is_rejected(self):
        many_negative = ReviewAnomaly(
            type="many_negative",
            description="30% отзывов имеют 1-2★",
            severity="high",
        )
        analysis = make_analysis(negative_percent=30, risk_score=45, anomalies=[many_negative])
        self.assertEqual(_recommended_verdict_from_analysis(self.product, analysis), "НЕ РЕКОМЕНДУЕМ")

    def test_normal_rating_distribution_is_not_fake_review_signal(self):
        reviews = [WBReview(text=f"хороший товар номер {index}", rating=5) for index in range(79)]
        reviews += [WBReview(text=f"не работает номер {index}", rating=1) for index in range(10)]
        reviews += [WBReview(text=f"нормальный товар номер {index}", rating=4) for index in range(11)]
        anomaly_types = {item.type for item in detect_review_anomalies(reviews)}
        self.assertNotIn("rating_complaint_mismatch", anomaly_types)

    def test_mp3_player_uses_electronics_profile(self):
        self.assertEqual(detect_product_risk_profile(self.product).code, "electronics")


if __name__ == "__main__":
    unittest.main()

import os
from dotenv import load_dotenv
from pydantic_settings import BaseSettings

load_dotenv('.env.local', override=True)
load_dotenv()
load_dotenv('.env.prompts')

class Settings(BaseSettings):
    TELEGRAM_BOT_TOKEN: str
    VK_GROUP_TOKEN: str
    VK_CONFIRMATION_TOKEN: str = ""
    VK_GROUP_ID: str = ""
    VK_CALLBACK_SECRET: str = ""
    PROXYAPI_URL: str
    PROXYAPI_KEY: str = ""
    PROXYAPI_TIMEOUT_SECONDS: int = 60
    APP_ENV: str = "production"
    REQUIRE_SECURE_CONFIG: bool = False
    ADMIN_IDS: str = ""
    TELEGRAM_FREE_TESTER_IDS: str = ""
    APP_BASE_URL: str = "http://localhost:8000"
    PUBLIC_WEBHOOK_BASE_URL: str = ""
    AUTO_SET_TELEGRAM_WEBHOOK: bool = False
    AUTO_SET_WEBHOOKS: bool = False
    TELEGRAM_DELIVERY_MODE: str = "webhook"
    TELEGRAM_WEBHOOK_DEBUG_LOG: bool = False
    START_CLOUDFLARED: bool = False
    VK_AUTO_SET_CALLBACK: bool = False
    VK_CALLBACK_SERVER_TITLE: str = "ProxyApiBots"
    VK_LONGPOLL_ENABLED: bool = True
    VK_LONGPOLL_WAIT_SECONDS: int = 25
    CLOUDFLARED_METRICS_URL: str = "http://127.0.0.1:4040/metrics"
    TUNNEL_WAIT_SECONDS: int = 120
    DB_BACKEND: str = "sqlite"
    DATABASE_URL: str = ""
    POSTGRES_DB: str = "proxyapi"
    POSTGRES_USER: str = "proxyapi"
    POSTGRES_PASSWORD: str = ""
    SQLITE_DB_PATH: str = "users.db"
    ERROR_LOG_PATH: str = "proxyapi_errors.log"
    ADMIN_TOKEN: str = "supersecret"
    BILLING_SECRET: str = "change-me-billing-secret"
    PAYMENT_PROVIDER: str = "sandbox"
    YOOKASSA_SHOP_ID: str = ""
    YOOKASSA_SECRET_KEY: str = ""
    YOOKASSA_RETURN_URL: str = ""
    ROBOKASSA_MERCHANT_LOGIN: str = ""
    ROBOKASSA_PASSWORD1: str = ""
    ROBOKASSA_PASSWORD2: str = ""
    ROBOKASSA_PASSWORD3: str = ""
    ROBOKASSA_TEST_MODE: bool = True
    MYTAX_API_URL: str = ""
    MYTAX_API_TOKEN: str = ""
    MYTAX_SELLER_INN: str = ""
    FISCAL_RETRY_INTERVAL_SECONDS: int = 300
    FISCAL_MAX_ATTEMPTS: int = 5
    PAYMENT_SIDE_EFFECT_RETRY_INTERVAL_SECONDS: int = 120
    MAINTENANCE_INTERVAL_SECONDS: int = 21600
    REFERRAL_BONUS_REQUESTS: int = 2
    ANTIFLOOD_WINDOW_SECONDS: int = 10
    ANTIFLOOD_MAX_REQUESTS: int = 3
    WB_PROXY_URL: str = ""
    WB_ENABLE_ROTATING_PROXIES: bool = False
    WB_MAX_REVIEWS_TOTAL: str = "3000"
    WB_BATCH_SIZE: int = 100
    WB_MAX_PARSE_SECONDS: int = 45
    WB_REQUEST_DELAY_SECONDS: float = 0.35
    WB_CACHE_TTL_SECONDS: int = 21600
    WB_CACHE_DIR: str = ""
    WB_MAX_CONCURRENT_ANALYSES: int = 4
    PRICE_COMPARE_ENABLED: bool = True
    PRICE_COMPARE_CACHE_TTL_SECONDS: int = 21600
    PRICE_COMPARE_TIMEOUT_SECONDS: int = 20
    PRICE_COMPARE_HTTP_RETRIES: int = 2
    PRICE_COMPARE_BROWSER_FALLBACK_ENABLED: bool = True
    PRICE_COMPARE_BROWSER_TIMEOUT_SECONDS: int = 15
    PRICE_COMPARE_EXTERNAL_SEARCH_URL: str = ""
    PRICE_COMPARE_EXTERNAL_SEARCH_KEY: str = ""
    PRICE_COMPARE_BRAVE_SEARCH_API_KEY: str = ""
    PRICE_COMPARE_SERPAPI_KEY: str = ""
    PRICE_COMPARE_SEARCH_ENGINE_FALLBACK_ENABLED: bool = True
    PRICE_COMPARE_MAX_RESULTS_PER_MARKETPLACE: int = 3
    PRICE_COMPARE_MIN_SIMILARITY: int = 58
    PRICE_COMPARE_SEARCH_RESULTS_LIMIT: int = 12
    PRICE_COMPARE_METADATA_ENRICHMENT_ENABLED: bool = True
    PRICE_COMPARE_METADATA_MAX_CANDIDATES: int = 8
    PRICE_COMPARE_METADATA_TIMEOUT_SECONDS: int = 8
    PRICE_COMPARE_METADATA_CACHE_TTL_SECONDS: int = 86400
    PRICE_COMPARE_METADATA_NEGATIVE_CACHE_TTL_SECONDS: int = 3600
    PRICE_COMPARE_VISUAL_MATCH_ENABLED: bool = True
    PRICE_COMPARE_VISUAL_MAX_CANDIDATES: int = 6
    PRICE_COMPARE_VISUAL_TIMEOUT_SECONDS: int = 8
    PRICE_COMPARE_OZON_ENABLED: bool = True
    PRICE_COMPARE_YANDEX_ENABLED: bool = True
    MARKETPLACE_RISK_MIN_CONFIDENCE: int = 50
    MARKETPLACE_RISK_MAX_RECOMMEND: int = 35
    MARKETPLACE_RISK_MAX_GAP_TO_WB: int = 8
    MARKETPLACE_RISK_HARD_STOP: int = 45
    MARKETPLACE_MIN_SAVINGS_PERCENT: float = 8.0
    TELEGRAM_SYSTEM_PROMPT: str = ""
    VK_SYSTEM_PROMPT: str = ""
    WB_SUMMARY_PROMPT: str = ""

    class Config:
        env_file = ".env"

    @property
    def admin_ids(self):
        return [x.strip() for x in self.ADMIN_IDS.split(",") if x.strip()]

    @property
    def telegram_free_tester_ids(self):
        return [x.strip() for x in self.TELEGRAM_FREE_TESTER_IDS.split(",") if x.strip()]

    @property
    def db_backend(self):
        return self.DB_BACKEND.strip().lower() or "sqlite"

settings = Settings()

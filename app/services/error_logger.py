from app.services.proxyapi_logger import proxyapi_logger

PUBLIC_TECHNICAL_ERROR_MESSAGE = "Сервис временно недоступен. Попробуйте ещё раз немного позже."
PUBLIC_WEBHOOK_ERROR_DETAIL = "Webhook processing failed"
PUBLIC_PAYMENT_PROVIDER_ERROR_DETAIL = "Payment provider is temporarily unavailable"
PUBLIC_CHECKOUT_TOKEN_ERROR_DETAIL = "Invalid checkout token"
PUBLIC_PAYMENT_REQUEST_ERROR_DETAIL = "Invalid payment request"
PUBLIC_PAYMENT_TARGET_NOT_FOUND_DETAIL = "Payment target not found"

def log_api_error(error: Exception):
    proxyapi_logger.error(f"API error: {error}")

from django.urls import path

from .views import (
    checkout_success,
    portfolio_view,
    start_core_checkout,
    stripe_webhook,
)

app_name = "billing"

urlpatterns = [
    path(
        "portfolio/",
        portfolio_view,
        name="portfolio",
    ),
    path(
        "checkout/core/",
        start_core_checkout,
        name="checkout-core",
    ),
    path(
        "checkout/success/",
        checkout_success,
        name="checkout-success",
    ),
    path(
        "webhook/stripe/",
        stripe_webhook,
        name="stripe-webhook",
    ),
]

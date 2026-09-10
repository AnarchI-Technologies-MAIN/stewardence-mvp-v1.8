from __future__ import annotations

import stripe
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .catalog import FOUNDER_LIMIT, PORTFOLIOS
from .models import BillingCustomer, Subscription


def _stripe_client() -> None:
    if not settings.STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured.")

    stripe.api_key = settings.STRIPE_SECRET_KEY


def _subscription_for_user(user_id):
    return (
        Subscription.objects.filter(
            billing_customer__user_id=user_id,
        )
        .select_related("billing_customer")
        .first()
    )


@login_required
def portfolio_view(request):
    subscription = _subscription_for_user(request.user.id)

    if subscription and subscription.grants_access:
        return redirect("organizations:workspace-selection")

    founder_claimed = Subscription.objects.filter(
        is_founder=True,
    ).count()

    founder_remaining = max(
        FOUNDER_LIMIT - founder_claimed,
        0,
    )

    return render(
        request,
        "billing/portfolio.html",
        {
            "portfolios": PORTFOLIOS,
            "founder_limit": FOUNDER_LIMIT,
            "founder_claimed": founder_claimed,
            "founder_remaining": founder_remaining,
            "founder_offer_active": founder_remaining > 0,
        },
    )


@login_required
@require_POST
def start_core_checkout(request):
    _stripe_client()

    billing_customer, _ = BillingCustomer.objects.get_or_create(
        user=request.user,
    )

    founder_offer_active = (
        Subscription.objects.filter(is_founder=True).count()
        < FOUNDER_LIMIT
    )

    price_id = settings.STRIPE_CORE_STANDARD_PRICE_ID

    if founder_offer_active:
        price_id = settings.STRIPE_CORE_FOUNDER_INTRO_PRICE_ID

    if not price_id:
        raise RuntimeError("The selected Stripe price is not configured.")

    if billing_customer.stripe_customer_id:
        stripe_customer_id = billing_customer.stripe_customer_id
    else:
        customer = stripe.Customer.create(
            email=request.user.email,
            name=request.user.get_full_name() or None,
            metadata={
                "stewardence_user_id": str(request.user.id),
            },
        )
        stripe_customer_id = customer.id
        billing_customer.stripe_customer_id = stripe_customer_id
        billing_customer.save(
            update_fields=[
                "stripe_customer_id",
                "updated_at",
            ]
        )

    success_url = request.build_absolute_uri(
        reverse("billing:checkout-success")
    )

    cancel_url = request.build_absolute_uri(
        reverse("billing:portfolio")
    )

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=stripe_customer_id,
        client_reference_id=str(request.user.id),
        line_items=[
            {
                "price": price_id,
                "quantity": 1,
            }
        ],
        success_url=f"{success_url}?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=cancel_url,
        metadata={
            "stewardence_user_id": str(request.user.id),
            "portfolio": Subscription.Portfolio.CORE,
            "founder_candidate": (
                "true" if founder_offer_active else "false"
            ),
        },
        subscription_data={
            "metadata": {
                "stewardence_user_id": str(request.user.id),
                "portfolio": Subscription.Portfolio.CORE,
                "founder_candidate": (
                    "true" if founder_offer_active else "false"
                ),
            },
        },
    )

    return redirect(session.url)


@login_required
def checkout_success(request):
    subscription = _subscription_for_user(request.user.id)

    return render(
        request,
        "billing/success.html",
        {
            "subscription": subscription,
        },
    )


@csrf_exempt
@require_POST
def stripe_webhook(request: HttpRequest) -> HttpResponse:
    _stripe_client()

    signature = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload=request.body,
            sig_header=signature,
            secret=settings.STRIPE_WEBHOOK_SECRET,
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)

    event_type = event["type"]
    obj = event["data"]["object"]

    if event_type == "checkout.session.completed":
        _handle_checkout_completed(obj)

    if event_type == "customer.subscription.updated":
        _handle_subscription_updated(obj)

    if event_type == "customer.subscription.deleted":
        _handle_subscription_deleted(obj)

    if event_type == "invoice.paid":
        _handle_invoice_paid(obj)

    if event_type == "invoice.payment_failed":
        _handle_invoice_payment_failed(obj)

    return HttpResponse(status=200)


def _handle_checkout_completed(session) -> None:
    user_id = session.get("client_reference_id")

    billing_customer = BillingCustomer.objects.filter(
        user_id=user_id,
    ).first()

    if billing_customer is None:
        return

    founder_candidate = (
        session.get("metadata", {}).get("founder_candidate") == "true"
    )

    subscription, _ = Subscription.objects.get_or_create(
        billing_customer=billing_customer,
        defaults={
            "portfolio": Subscription.Portfolio.CORE,
        },
    )

    subscription.stripe_subscription_id = session.get("subscription")
    subscription.status = Subscription.Status.ACTIVE

    if founder_candidate and not subscription.is_founder:
        next_sequence = (
            Subscription.objects.filter(is_founder=True).count() + 1
        )

        if next_sequence <= FOUNDER_LIMIT:
            subscription.is_founder = True
            subscription.founder_sequence = next_sequence
            subscription.current_price_cents = 4900

    if not subscription.is_founder:
        subscription.current_price_cents = 9900

    subscription.save()


def _handle_subscription_updated(stripe_subscription) -> None:
    subscription = Subscription.objects.filter(
        stripe_subscription_id=stripe_subscription["id"],
    ).first()

    if subscription is None:
        return

    stripe_status = stripe_subscription.get("status", "")

    status_map = {
        "active": Subscription.Status.ACTIVE,
        "trialing": Subscription.Status.ACTIVE,
        "past_due": Subscription.Status.PAST_DUE,
        "canceled": Subscription.Status.CANCELED,
    }

    mapped = status_map.get(stripe_status)

    if mapped:
        subscription.status = mapped

    subscription.cancel_at_period_end = bool(
        stripe_subscription.get("cancel_at_period_end")
    )

    if (
        subscription.status == Subscription.Status.ACTIVE
        and subscription.cancel_at_period_end
    ):
        subscription.status = Subscription.Status.CANCELING

    subscription.save(
        update_fields=[
            "status",
            "cancel_at_period_end",
            "updated_at",
        ]
    )


def _handle_subscription_deleted(stripe_subscription) -> None:
    Subscription.objects.filter(
        stripe_subscription_id=stripe_subscription["id"],
    ).update(
        status=Subscription.Status.CANCELED,
        cancel_at_period_end=False,
    )


def _handle_invoice_paid(invoice) -> None:
    subscription_id = invoice.get("subscription")

    if not subscription_id:
        return

    Subscription.objects.filter(
        stripe_subscription_id=subscription_id,
    ).exclude(
        status=Subscription.Status.CANCELING,
    ).update(
        status=Subscription.Status.ACTIVE,
    )


def _handle_invoice_payment_failed(invoice) -> None:
    subscription_id = invoice.get("subscription")

    if not subscription_id:
        return

    Subscription.objects.filter(
        stripe_subscription_id=subscription_id,
    ).update(
        status=Subscription.Status.PAST_DUE,
    )

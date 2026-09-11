from __future__ import annotations

import stripe
from django.conf import settings
from django.db import transaction
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .catalog import FOUNDER_LIMIT, PORTFOLIOS
from .founder_slots import (
    attach_founder_checkout_session,
    claim_founder_slot,
    release_expired_founder_checkout,
    release_founder_reservation,
    release_unattached_founder_reservation,
    reserve_founder_slot,
)
from .models import BillingCustomer, StripeWebhookEvent, Subscription
from .stripe_schedules import (
    ensure_founder_subscription_schedule,
    price_cents_from_stripe_subscription,
)


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

    founder_slot = reserve_founder_slot(
        billing_customer_id=billing_customer.id,
    )

    if (
        founder_slot is not None
        and founder_slot.stripe_checkout_session_id
    ):
        existing_session = stripe.checkout.Session.retrieve(
            founder_slot.stripe_checkout_session_id
        )

        existing_status = existing_session.get("status")

        if existing_status == "open":
            existing_url = existing_session.get("url")

            if not existing_url:
                raise RuntimeError(
                    "Authoritative founder Checkout Session "
                    "has no redirect URL."
                )

            return redirect(existing_url)

        if existing_status == "complete":
            return redirect(
                f"{success_url}?session_id="
                f"{founder_slot.stripe_checkout_session_id}"
            )

        if existing_status == "expired":
            released = release_expired_founder_checkout(
                slot_sequence=founder_slot.sequence,
                reservation_token=founder_slot.reservation_token,
                billing_customer_id=billing_customer.id,
                stripe_checkout_session_id=(
                    founder_slot.stripe_checkout_session_id
                ),
            )

            if not released:
                raise RuntimeError(
                    "Expired founder Checkout Session could "
                    "not release its exact reservation."
                )

            founder_slot = reserve_founder_slot(
                billing_customer_id=billing_customer.id,
            )

        if existing_status not in {
            "open",
            "complete",
            "expired",
        }:
            raise RuntimeError(
                "Authoritative founder Checkout Session has "
                f"unsupported Stripe status: {existing_status!r}"
            )

    price_id = settings.STRIPE_CORE_STANDARD_PRICE_ID

    metadata = {
        "stewardence_user_id": str(request.user.id),
        "portfolio": Subscription.Portfolio.CORE,
    }

    if founder_slot is not None:
        price_id = settings.STRIPE_CORE_FOUNDER_INTRO_PRICE_ID

        metadata.update(
            {
                "founder_slot_sequence": str(
                    founder_slot.sequence
                ),
                "founder_reservation_token": str(
                    founder_slot.reservation_token
                ),
            }
        )

    if not price_id:
        if founder_slot is not None:
            release_unattached_founder_reservation(
                slot_sequence=founder_slot.sequence,
                reservation_token=founder_slot.reservation_token,
                billing_customer_id=billing_customer.id,
            )

        raise RuntimeError(
            "The selected Stripe price is not configured."
        )

    checkout_kwargs = {
        "mode": "subscription",
        "customer": stripe_customer_id,
        "client_reference_id": str(request.user.id),
        "line_items": [
            {
                "price": price_id,
                "quantity": 1,
            }
        ],
        "success_url": (
            f"{success_url}?session_id={{CHECKOUT_SESSION_ID}}"
        ),
        "cancel_url": cancel_url,
        "metadata": metadata,
        "subscription_data": {
            "metadata": metadata,
        },
    }

    if founder_slot is not None:
        checkout_kwargs["expires_at"] = int(
            founder_slot.checkout_expires_at.timestamp()
        )

    create_kwargs = {}

    if founder_slot is not None:
        create_kwargs["idempotency_key"] = (
            "stewardence-founder-checkout:"
            f"{founder_slot.reservation_token}"
        )

    try:
        session = stripe.checkout.Session.create(
            **checkout_kwargs,
            **create_kwargs,
        )
    except Exception:
        if founder_slot is not None:
            release_unattached_founder_reservation(
                slot_sequence=founder_slot.sequence,
                reservation_token=founder_slot.reservation_token,
                billing_customer_id=billing_customer.id,
            )

        raise

    if founder_slot is not None:
        attached = attach_founder_checkout_session(
            slot_sequence=founder_slot.sequence,
            reservation_token=founder_slot.reservation_token,
            billing_customer_id=billing_customer.id,
            stripe_checkout_session_id=session.id,
            checkout_expires_at=founder_slot.checkout_expires_at,
        )

        if not attached:
            raise RuntimeError(
                "Stripe returned a Checkout Session that "
                "does not match the authoritative founder "
                "reservation generation."
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

    event_id = event.get("id")
    event_type = event.get("type")

    if not event_id or not event_type:
        return HttpResponse(status=400)

    obj = event["data"]["object"]

    with transaction.atomic():
        _receipt, created = StripeWebhookEvent.objects.get_or_create(
            stripe_event_id=event_id,
            defaults={
                "event_type": event_type,
            },
        )

        if not created:
            return HttpResponse(status=200)

        if event_type == "checkout.session.completed":
            _handle_checkout_completed(obj)

        if event_type == "checkout.session.expired":
            _handle_checkout_expired(obj)

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

    metadata = session.get("metadata", {})

    raw_slot_sequence = metadata.get(
        "founder_slot_sequence"
    )

    reservation_token = metadata.get(
        "founder_reservation_token"
    )

    founder_metadata_present = bool(
        raw_slot_sequence or reservation_token
    )

    founder_slot = None

    if founder_metadata_present:
        if not raw_slot_sequence or not reservation_token:
            raise RuntimeError(
                "Founder Checkout completion has incomplete "
                "reservation metadata."
            )

        try:
            slot_sequence = int(raw_slot_sequence)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Founder Checkout completion has invalid "
                "slot sequence."
            ) from exc

        founder_slot = claim_founder_slot(
            slot_sequence=slot_sequence,
            reservation_token=reservation_token,
            billing_customer_id=billing_customer.id,
            stripe_checkout_session_id=session.get("id"),
        )

        if founder_slot is None:
            raise RuntimeError(
                "Founder Checkout completion could not prove "
                "its authoritative reservation."
            )

    subscription, _ = Subscription.objects.get_or_create(
        billing_customer=billing_customer,
        defaults={
            "portfolio": Subscription.Portfolio.CORE,
        },
    )

    subscription.stripe_subscription_id = session.get(
        "subscription"
    )
    subscription.status = Subscription.Status.ACTIVE

    if founder_slot is not None:
        subscription.is_founder = True
        subscription.founder_sequence = founder_slot.sequence
        subscription.current_price_cents = PORTFOLIOS[
            "core"
        ]["founder_intro_cents"]

        schedule_state = ensure_founder_subscription_schedule(
            subscription=subscription,
        )

        subscription.stripe_schedule_id = schedule_state[
            "schedule_id"
        ]

        subscription.founder_intro_ends_at = schedule_state[
            "intro_ends_at"
        ]

    if founder_slot is None:
        subscription.current_price_cents = PORTFOLIOS[
            "core"
        ]["standard_cents"]

    subscription.save()


def _handle_checkout_expired(session) -> None:
    user_id = session.get("client_reference_id")

    billing_customer = BillingCustomer.objects.filter(
        user_id=user_id,
    ).first()

    if billing_customer is None:
        return

    metadata = session.get("metadata", {})

    slot_sequence = metadata.get("founder_slot_sequence")
    reservation_token = metadata.get(
        "founder_reservation_token"
    )

    if not slot_sequence or not reservation_token:
        return

    try:
        slot_sequence = int(slot_sequence)
    except (TypeError, ValueError):
        return

    release_expired_founder_checkout(
        slot_sequence=slot_sequence,
        reservation_token=reservation_token,
        billing_customer_id=billing_customer.id,
        stripe_checkout_session_id=session.get("id"),
    )


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

    stripe_price_cents = price_cents_from_stripe_subscription(
        stripe_subscription
    )

    update_fields = [
        "status",
        "cancel_at_period_end",
        "updated_at",
    ]

    if stripe_price_cents is not None:
        subscription.current_price_cents = stripe_price_cents
        update_fields.append("current_price_cents")

    stripe_schedule_id = stripe_subscription.get("schedule")

    if stripe_schedule_id:
        subscription.stripe_schedule_id = stripe_schedule_id
        update_fields.append("stripe_schedule_id")

    subscription.save(
        update_fields=update_fields
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

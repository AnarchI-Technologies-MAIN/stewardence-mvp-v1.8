from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

import stripe
from django.conf import settings

from .catalog import PORTFOLIOS


def _object_value(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)

    return getattr(obj, name, default)


def _phase_value(phase, name, default=None):
    if isinstance(phase, dict):
        return phase.get(name, default)

    return getattr(phase, name, default)


def _founder_metadata(*, subscription):
    return {
        "stewardence_founder": "true",
        "stewardence_founder_sequence": str(
            subscription.founder_sequence
        ),
        "stewardence_billing_customer_id": str(
            subscription.billing_customer_id
        ),
    }


def ensure_founder_subscription_schedule(
    *,
    subscription,
):
    """
    Attach and configure the durable founder price schedule.

    Phase 1:
        Founder intro price for six monthly billing periods.

    Phase 2:
        Founder ongoing price indefinitely.

    Both Stripe mutations use stable idempotency keys derived from the
    Stripe subscription identity. Retrying after a local transaction
    failure therefore converges on the same external schedule.
    """
    if not subscription.is_founder:
        raise RuntimeError(
            "Founder schedule requires founder entitlement."
        )

    if not subscription.founder_sequence:
        raise RuntimeError(
            "Founder schedule requires founder sequence."
        )

    stripe_subscription_id = subscription.stripe_subscription_id

    if not stripe_subscription_id:
        raise RuntimeError(
            "Founder schedule requires Stripe subscription ID."
        )

    founder_intro_price_id = (
        settings.STRIPE_CORE_FOUNDER_INTRO_PRICE_ID
    )

    founder_ongoing_price_id = (
        settings.STRIPE_CORE_FOUNDER_ONGOING_PRICE_ID
    )

    if not founder_intro_price_id:
        raise RuntimeError(
            "Founder intro Stripe price is not configured."
        )

    if not founder_ongoing_price_id:
        raise RuntimeError(
            "Founder ongoing Stripe price is not configured."
        )

    create_key = (
        "stewardence-founder-schedule-create:"
        f"{stripe_subscription_id}"
    )

    configure_key = (
        "stewardence-founder-schedule-configure-v1:"
        f"{stripe_subscription_id}"
    )

    if subscription.stripe_schedule_id:
        schedule = stripe.SubscriptionSchedule.retrieve(
            subscription.stripe_schedule_id
        )
    else:
        schedule = stripe.SubscriptionSchedule.create(
            from_subscription=stripe_subscription_id,
            idempotency_key=create_key,
        )

    schedule_id = _object_value(schedule, "id")

    if not schedule_id:
        raise RuntimeError(
            "Stripe did not return a subscription schedule ID."
        )

    current_phase = _object_value(
        schedule,
        "current_phase",
        {},
    )

    current_start = _object_value(
        current_phase,
        "start_date",
    )

    if current_start is None:
        phases = _object_value(
            schedule,
            "phases",
            [],
        )

        if phases:
            current_start = _phase_value(
                phases[0],
                "start_date",
            )

    if current_start is None:
        raise RuntimeError(
            "Stripe founder schedule has no current phase start."
        )

    metadata = _founder_metadata(
        subscription=subscription,
    )

    configured = stripe.SubscriptionSchedule.modify(
        schedule_id,
        end_behavior="release",
        proration_behavior="none",
        phases=[
            {
                "start_date": current_start,
                "items": [
                    {
                        "price": founder_intro_price_id,
                        "quantity": 1,
                    }
                ],
                "duration": {
                    "interval": "month",
                    "interval_count": PORTFOLIOS[
                        "core"
                    ]["founder_intro_months"],
                },
                "proration_behavior": "none",
                "metadata": {
                    **metadata,
                    "stewardence_founder_phase": "intro",
                },
            },
            {
                "items": [
                    {
                        "price": founder_ongoing_price_id,
                        "quantity": 1,
                    }
                ],
                "proration_behavior": "none",
                "metadata": {
                    **metadata,
                    "stewardence_founder_phase": "ongoing",
                },
            },
        ],
        idempotency_key=configure_key,
    )

    phases = _object_value(
        configured,
        "phases",
        [],
    )

    if len(phases) < 2:
        raise RuntimeError(
            "Stripe founder schedule did not return both phases."
        )

    intro_end_timestamp = _phase_value(
        phases[0],
        "end_date",
    )

    if intro_end_timestamp is None:
        raise RuntimeError(
            "Stripe founder intro phase has no end date."
        )

    intro_ends_at = datetime.fromtimestamp(
        intro_end_timestamp,
        tz=dt_timezone.utc,
    )

    return {
        "schedule_id": schedule_id,
        "intro_ends_at": intro_ends_at,
    }


def price_cents_from_stripe_subscription(
    stripe_subscription,
):
    """
    Resolve Stewardence's known Core price from Stripe's active
    subscription item.

    Unknown prices intentionally return None rather than inventing local
    billing truth.
    """
    items = _object_value(
        stripe_subscription,
        "items",
        {},
    )

    data = _object_value(
        items,
        "data",
        [],
    )

    if not data:
        return None

    first_item = data[0]

    price = _object_value(
        first_item,
        "price",
        {},
    )

    price_id = _object_value(
        price,
        "id",
    )

    price_map = {
        settings.STRIPE_CORE_FOUNDER_INTRO_PRICE_ID:
            PORTFOLIOS["core"]["founder_intro_cents"],

        settings.STRIPE_CORE_FOUNDER_ONGOING_PRICE_ID:
            PORTFOLIOS["core"]["founder_ongoing_cents"],

        settings.STRIPE_CORE_STANDARD_PRICE_ID:
            PORTFOLIOS["core"]["standard_cents"],
    }

    return price_map.get(price_id)

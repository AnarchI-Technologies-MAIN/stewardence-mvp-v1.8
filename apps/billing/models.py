from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class BillingCustomer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="billing_customer",
    )

    stripe_customer_id = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Subscription(models.Model):
    class Portfolio(models.TextChoices):
        CORE = "core", "Core"
        AUTOMATION = "automation", "Automation"
        ENTERPRISE = "enterprise", "Enterprise"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        CANCELING = "canceling", "Canceling"
        PAST_DUE = "past_due", "Past due"
        CANCELED = "canceled", "Canceled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    billing_customer = models.OneToOneField(
        BillingCustomer,
        on_delete=models.CASCADE,
        related_name="subscription",
    )

    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="subscription",
        null=True,
        blank=True,
    )

    portfolio = models.CharField(
        max_length=16,
        choices=Portfolio,
        default=Portfolio.CORE,
    )

    stripe_subscription_id = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
    )

    stripe_schedule_id = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
    )

    status = models.CharField(
        max_length=16,
        choices=Status,
        default=Status.PENDING,
    )

    is_founder = models.BooleanField(default=False)

    founder_sequence = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        unique=True,
    )

    founder_intro_ends_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    founder_entitlement_ends_on_cancel = models.BooleanField(default=True)

    current_price_cents = models.PositiveIntegerField(
        null=True,
        blank=True,
    )

    current_period_end = models.DateTimeField(
        null=True,
        blank=True,
    )

    cancel_at_period_end = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def grants_access(self) -> bool:
        return self.status in {
            self.Status.ACTIVE,
            self.Status.CANCELING,
        }

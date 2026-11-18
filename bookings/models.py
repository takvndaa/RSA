from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Lane(models.Model):
    number = models.PositiveSmallIntegerField(unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["number"]

    def __str__(self):
        return f"Lane {self.number}"


class TimeSlot(models.Model):
    start_time = models.DateTimeField(unique=True)

    class Meta:
        ordering = ["start_time"]

    @property
    def end_time(self):
        return self.start_time + timedelta(hours=1)

    def __str__(self):
        return timezone.localtime(self.start_time).strftime("%Y-%m-%d %I:%M %p")


class Reservation(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending Payment"
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="reservations")
    lane = models.ForeignKey(Lane, on_delete=models.PROTECT, related_name="reservations")
    timeslot = models.ForeignKey(TimeSlot, on_delete=models.PROTECT, related_name="reservations")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    stripe_checkout_session_id = models.CharField(max_length=255, blank=True, default="")
    # A claim is written BEFORE the network request to Stripe. A stuck claim
    # intentionally retains the lane for staff review instead of risking payment.
    checkout_creation_started_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["lane", "timeslot"], condition=~Q(status__in=["cancelled", "expired"]), name="unique_active_lane_timeslot"),
            models.UniqueConstraint(fields=["member", "timeslot"], condition=~Q(status__in=["cancelled", "expired"]), name="unique_active_member_timeslot"),
        ]

    @property
    def payment_deadline(self):
        return self.created_at + timedelta(minutes=15)

    def clean(self):
        super().clean()
        if self.status in (self.Status.CANCELLED, self.Status.EXPIRED):
            return
        if not self.member.is_superuser and self.member.approval_status != "approved":
            raise ValidationError("Only approved members can reserve lanes.")
        if not self.lane.is_active:
            raise ValidationError("This lane is unavailable.")
        local_start = timezone.localtime(self.timeslot.start_time)
        if (local_start.minute != 0 or local_start.second != 0 or local_start.microsecond != 0 or not 6 <= local_start.hour < 22):
            raise ValidationError("Reservations must start on the hour between 6 AM and 9 PM.")
        if self.timeslot.start_time < timezone.now() + timedelta(hours=1):
            raise ValidationError("Reservations must be made at least one hour in advance.")

    def __str__(self):
        return f"{self.member} - {self.lane} - {self.timeslot}"


class PaymentReview(models.Model):
    """Durable staff queue for Stripe outcomes requiring investigation."""
    reservation = models.ForeignKey(Reservation, on_delete=models.PROTECT, related_name="payment_reviews")
    reason = models.CharField(max_length=80)
    stripe_session_id = models.CharField(max_length=255, blank=True, default="")
    details = models.TextField(blank=True, default="")
    resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Reservation {self.reservation_id}: {self.reason}"

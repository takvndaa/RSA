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
        return timezone.localtime(
            self.start_time
        ).strftime("%Y-%m-%d %I:%M %p")


class Reservation(models.Model):

    class Status(models.TextChoices):
        PENDING = "pending", "Pending Payment"
        CONFIRMED = "confirmed", "Confirmed"
        CANCELLED = "cancelled", "Cancelled"

    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="reservations",
    )

    lane = models.ForeignKey(
        Lane,
        on_delete=models.PROTECT,
        related_name="reservations",
    )

    timeslot = models.ForeignKey(
        TimeSlot,
        on_delete=models.PROTECT,
        related_name="reservations",
    )

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["lane", "timeslot"],
                condition=~Q(status="cancelled"),
                name="unique_active_lane_timeslot",
            ),
            models.UniqueConstraint(
                fields=["member", "timeslot"],
                condition=~Q(status="cancelled"),
                name="unique_active_member_timeslot",
            ),
        ]

    def clean(self):
        super().clean()

        if self.status == self.Status.CANCELLED:
            return

        if not self.member.is_superuser:
            if self.member.approval_status != "approved":
                raise ValidationError(
                    "Only approved members can reserve lanes."
                )

        if not self.lane.is_active:
            raise ValidationError(
                "This lane is unavailable."
            )

        local_start = timezone.localtime(
            self.timeslot.start_time
        )

        if (
            local_start.minute != 0
            or local_start.second != 0
            or local_start.microsecond != 0
            or not 6 <= local_start.hour < 22
        ):
            raise ValidationError(
                "Reservations must start on the hour "
                "between 6 AM and 9 PM."
            )

        if (
            self.timeslot.start_time
            < timezone.now() + timedelta(hours=1)
        ):
            raise ValidationError(
                "Reservations must be made at least "
                "one hour in advance."
            )

    def __str__(self):
        return (
            f"{self.member} - "
            f"{self.lane} - "
            f"{self.timeslot}"
        )

"""Member requests to cancel paid DIIVE reservations.

This endpoint does not cancel reservations, release lanes, or issue refunds.
"""

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import CancellationRequest, Reservation


@login_required
@require_POST
def request_cancellation(request, reservation_id):
    requested_at = timezone.now()

    try:
        with transaction.atomic():
            reservation = get_object_or_404(
                Reservation.objects.select_related("timeslot"),
                pk=reservation_id,
                member=request.user,
            )

            if reservation.status != Reservation.Status.CONFIRMED:
                messages.error(
                    request,
                    "Only confirmed, paid reservations can use this request form.",
                )
                return redirect("my_reservations")

            if reservation.payment_reviews.filter(resolved=False).exists():
                messages.error(
                    request,
                    "This reservation needs payment review. "
                    "Please contact administration.",
                )
                return redirect("my_reservations")

            deadline = (
                reservation.timeslot.start_time - timedelta(hours=24)
            )

            if requested_at > deadline:
                messages.error(
                    request,
                    "The 24-hour cancellation deadline has passed. "
                    "Your reservation remains confirmed. "
                    "Please contact administration to discuss a possible exception.",
                )
                return redirect("my_reservations")

            existing_request = CancellationRequest.objects.filter(
                reservation=reservation,
                decision__in=[
                    CancellationRequest.Decision.PENDING,
                    CancellationRequest.Decision.APPROVED,
                ],
            ).exists()

            if existing_request:
                messages.info(
                    request,
                    "A cancellation request is already awaiting staff processing.",
                )
                return redirect("my_reservations")

            CancellationRequest.objects.create(
                reservation=reservation,
                requested_at=requested_at,
                member_reason=request.POST.get("reason", "").strip()[:2000],
            )

    except IntegrityError:
        messages.info(
            request,
            "A cancellation request already exists. "
            "Please contact administration if needed.",
        )
        return redirect("my_reservations")

    messages.success(
        request,
        "Cancellation request submitted for staff review. "
        "Your reservation remains confirmed until staff safely "
        "complete the cancellation and refund.",
    )

    return redirect("my_reservations")

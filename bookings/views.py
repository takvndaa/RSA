
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
import stripe
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from .models import Lane, Reservation, TimeSlot


def member_is_approved(user):
    return (
        user.is_superuser
        or user.approval_status == "approved"
    )


@login_required
def availability(request):
    today = timezone.localdate()
    selected_date = parse_date(request.GET.get("date", ""))

    if selected_date is None or selected_date < today:
        selected_date = today

    local_timezone = ZoneInfo(settings.TIME_ZONE)
    lanes = list(Lane.objects.filter(is_active=True))

    slots = []

    for hour in range(6, 22):
        local_start = datetime.combine(
            selected_date,
            time(hour=hour),
            tzinfo=local_timezone,
        )

        start = local_start.astimezone(
            timezone.get_current_timezone()
        )

        existing = Reservation.objects.filter(
            timeslot__start_time=start,
        ).exclude(
            status=Reservation.Status.CANCELLED
        )

        reserved_lanes = set(
            existing.values_list("lane_id", flat=True)
        )

        member_has_booking = (
            request.user.is_authenticated
            and existing.filter(member=request.user).exists()
        )

        slots.append({
            "hour": hour,
            "start": start,
            "available_lanes": [
                lane for lane in lanes
                if lane.id not in reserved_lanes
            ],
            "can_book": (
                member_is_approved(request.user)
                and not member_has_booking
                and start >= timezone.now() + timedelta(hours=1)
            ),
            "member_has_booking": member_has_booking,
        })

    return render(
        request,
        "bookings/availability.html",
        {
            "selected_date": selected_date,
            "slots": slots,
            "approved": member_is_approved(request.user),
        },
    )


@login_required
@require_POST
def create_reservation(request):
    if not member_is_approved(request.user):
        messages.error(
            request,
            "Your membership must be approved before booking."
        )
        return redirect("member_dashboard")

    selected_date = parse_date(request.POST.get("date", ""))

    try:
        lane_number = int(request.POST.get("lane", ""))
        hour = int(request.POST.get("hour", ""))
    except (ValueError, TypeError):
        messages.error(request, "Invalid reservation details.")
        return redirect("availability")

    if (
        selected_date is None
        or not 6 <= hour < 22
    ):
        messages.error(request, "Invalid reservation date or time.")
        return redirect("availability")

    lane = get_object_or_404(
        Lane,
        number=lane_number,
        is_active=True,
    )

    start = datetime.combine(
        selected_date,
        time(hour=hour),
        tzinfo=ZoneInfo(settings.TIME_ZONE),
    )

    try:
        with transaction.atomic():
            timeslot, _ = TimeSlot.objects.get_or_create(
                start_time=start
            )

            reservation = Reservation(
                member=request.user,
                lane=lane,
                timeslot=timeslot,
            )

            reservation.full_clean()
            reservation.save()

    except (ValidationError, IntegrityError):
        messages.error(
            request,
            "This reservation is unavailable or conflicts "
            "with another booking."
        )
        return redirect("availability")

    messages.success(
        request,
        "Reservation created. Payment is still pending."
    )
    return redirect("my_reservations")


@login_required
def my_reservations(request):
    reservations = Reservation.objects.filter(
        member=request.user
    ).select_related(
        "lane", "timeslot"
    ).order_by("-timeslot__start_time")

    return render(
        request,
        "bookings/my_reservations.html",
        {"reservations": reservations},
    )


@login_required
@require_POST
def cancel_reservation(request, reservation_id):
    with transaction.atomic():
        reservation = get_object_or_404(
            Reservation.objects.select_for_update(),
            id=reservation_id,
            member=request.user,
        )

        if reservation.status == Reservation.Status.CANCELLED:
            messages.info(
                request,
                "Reservation already cancelled."
            )

        elif reservation.timeslot.start_time < (
            timezone.now() + timedelta(hours=1)
        ):
            messages.error(
                request,
                "Reservations must be cancelled at least "
                "one hour before the start time."
            )

        else:
            reservation.status = Reservation.Status.CANCELLED
            reservation.save(update_fields=["status"])

            messages.success(
                request,
                "Reservation cancelled."
            )

    return redirect("my_reservations")


@login_required
@require_POST
def checkout_reservation(request, reservation_id):
    reservation = get_object_or_404(
        Reservation,
        id=reservation_id,
        member=request.user,
    )

    if reservation.status != Reservation.Status.PENDING:
        messages.error(
            request,
            "This reservation cannot be paid for."
        )
        return redirect("my_reservations")

    if reservation.timeslot.start_time <= timezone.now():
        messages.error(
            request,
            "This reservation has already started."
        )
        return redirect("my_reservations")

    if not settings.STRIPE_SECRET_KEY:
        messages.error(
            request,
            "Stripe is not configured."
        )
        return redirect("my_reservations")

    stripe.api_key = settings.STRIPE_SECRET_KEY

    try:
        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": 1500,
                        "product_data": {
                            "name": (
                                f"DIIVE - Lane {reservation.lane.number}"
                            ),
                        },
                    },
                    "quantity": 1,
                }
            ],
            client_reference_id=str(reservation.id),
            metadata={
                "reservation_id": str(reservation.id),
            },
            success_url=(
                settings.SITE_URL
                + reverse("my_reservations")
                + "?payment=success"
            ),
            cancel_url=(
                settings.SITE_URL
                + reverse("my_reservations")
            ),
        )

    except stripe.StripeError as error:
        print(
            "STRIPE CHECKOUT ERROR:",
            type(error).__name__,
        )

        messages.error(
            request,
            "Unable to start payment. Please try again."
        )
        return redirect("my_reservations")

    return redirect(checkout_session.url)



@csrf_exempt
@require_POST
def stripe_webhook(request):
    if not settings.STRIPE_WEBHOOK_SECRET:
        return HttpResponse("Webhook not configured", status=503)

    signature = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            request.body,
            signature,
            settings.STRIPE_WEBHOOK_SECRET,
        )
    except (ValueError, stripe.SignatureVerificationError):
        return HttpResponse(status=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"].to_dict()

        reservation_id = session.get("metadata", {}).get(
            "reservation_id"
        )

        if (
            session.get("payment_status") == "paid"
            and session.get("amount_total") == 1500
            and session.get("currency") == "usd"
            and session.get("mode") == "payment"
            and reservation_id
            and session.get("client_reference_id") == str(reservation_id)
        ):
            with transaction.atomic():
                reservation = (
                    Reservation.objects.select_for_update()
                    .filter(id=reservation_id)
                    .first()
                )

                if (
                    reservation
                    and reservation.status == Reservation.Status.PENDING
                ):
                    reservation.status = Reservation.Status.CONFIRMED
                    reservation.save(update_fields=["status"])

    return HttpResponse(status=200)

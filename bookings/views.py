
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import logging

import stripe

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Lane, Reservation, TimeSlot
from .services import expire_unpaid_reservations, flag_payment_review


logger = logging.getLogger(__name__)

PAYMENT_WINDOW = timedelta(minutes=15)


def member_is_approved(user):
    return (
        user.is_superuser
        or user.approval_status == "approved"
    )


@login_required
def availability(request):
    expire_unpaid_reservations()

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
            status__in=[
                Reservation.Status.PENDING,
                Reservation.Status.CONFIRMED,
            ],
        )

        reserved_lanes = set(
            existing.values_list("lane_id", flat=True)
        )

        member_has_booking = existing.filter(
            member=request.user
        ).exists()

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
    expire_unpaid_reservations()

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
        messages.error(
            request,
            "Invalid reservation details."
        )
        return redirect("availability")

    if selected_date is None or not 6 <= hour < 22:
        messages.error(
            request,
            "Invalid reservation date or time."
        )
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
        "Reservation created. You have 15 minutes to pay."
    )
    return redirect("my_reservations")


@login_required
def my_reservations(request):
    expire_unpaid_reservations()

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
    expire_unpaid_reservations()

    reservation = get_object_or_404(
        Reservation.objects.select_related("timeslot"),
        id=reservation_id,
        member=request.user,
    )

    if reservation.status in (
        Reservation.Status.CANCELLED,
        Reservation.Status.EXPIRED,
    ):
        messages.info(
            request,
            "This reservation is already cancelled or expired."
        )
        return redirect("my_reservations")

    if reservation.timeslot.start_time < (
        timezone.now() + timedelta(hours=1)
    ):
        messages.error(
            request,
            "Reservations must be cancelled at least "
            "one hour before the start time."
        )
        return redirect("my_reservations")

    if reservation.status == Reservation.Status.CONFIRMED:
        messages.error(
            request,
            "Paid reservations require staff assistance "
            "for cancellation and refund review."
        )
        return redirect("my_reservations")

    if reservation.checkout_creation_started_at is not None:
        messages.error(request, "Payment setup is in progress. Please try again later or contact staff.")
        return redirect("my_reservations")

    session_id = reservation.stripe_checkout_session_id

    if session_id:
        if not settings.STRIPE_SECRET_KEY:
            messages.error(
                request,
                "Payment status cannot be verified right now."
            )
            return redirect("my_reservations")

        stripe.api_key = settings.STRIPE_SECRET_KEY

        try:
            session = stripe.checkout.Session.retrieve(session_id)

            if session.status == "open":
                stripe.checkout.Session.expire(session_id)

            elif session.status == "complete":
                messages.error(
                    request,
                    "Payment has completed. Please wait for "
                    "confirmation or contact staff."
                )
                return redirect("my_reservations")

            elif session.status != "expired":
                messages.error(
                    request,
                    "Payment status is uncertain. "
                    "Please contact staff."
                )
                return redirect("my_reservations")

        except stripe.StripeError:
            logger.exception(
                "Could not close Checkout session for reservation %s",
                reservation.id,
            )
            messages.error(
                request,
                "Unable to cancel the payment session. "
                "Please try again later."
            )
            return redirect("my_reservations")

    updated = Reservation.objects.filter(
        id=reservation.id,
        status=Reservation.Status.PENDING,
        checkout_creation_started_at__isnull=True,
        stripe_checkout_session_id=session_id,
    ).update(
        status=Reservation.Status.CANCELLED
    )

    if updated:
        messages.success(request, "Reservation cancelled.")
    else:
        messages.error(
            request,
            "Reservation status changed. Please refresh the page."
        )

    return redirect("my_reservations")


@login_required
@require_POST
def checkout_reservation(request, reservation_id):
    expire_unpaid_reservations()

    reservation = get_object_or_404(
        Reservation.objects.select_related("lane", "timeslot"),
        id=reservation_id,
        member=request.user,
    )

    if reservation.status != Reservation.Status.PENDING:
        messages.error(
            request,
            "This reservation is no longer awaiting payment."
        )
        return redirect("my_reservations")

    if timezone.now() >= reservation.payment_deadline:
        expire_unpaid_reservations()

        messages.error(
            request,
            "Your 15-minute payment window has expired "
            "or requires payment review."
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

    if reservation.checkout_creation_started_at is not None:
        messages.info(request, "Payment setup is already in progress. Please try again shortly.")
        return redirect("my_reservations")

    existing_session_id = reservation.stripe_checkout_session_id

    if existing_session_id:
        try:
            existing_session = stripe.checkout.Session.retrieve(
                existing_session_id
            )
        except stripe.StripeError:
            logger.exception(
                "Could not retrieve Checkout session for reservation %s",
                reservation.id,
            )
            messages.error(
                request,
                "Unable to verify your existing payment session."
            )
            return redirect("my_reservations")

        if existing_session.status == "open":
            if timezone.now() >= reservation.payment_deadline:
                expire_unpaid_reservations()
                messages.error(
                    request,
                    "Your payment window has expired."
                )
                return redirect("my_reservations")

            if existing_session.url:
                return redirect(existing_session.url)

        messages.error(
            request,
            "This payment session is no longer available. "
            "Please wait for confirmation or contact staff."
        )
        return redirect("my_reservations")

    # SQLite-safe compare-and-set claim. No database transaction is held
    # open during the Stripe API call. Other workers cannot expire/cancel
    # a reservation carrying this claim.
    claimed_at = timezone.now()
    claimed = Reservation.objects.filter(
        id=reservation.id,
        status=Reservation.Status.PENDING,
        stripe_checkout_session_id="",
        checkout_creation_started_at__isnull=True,
        created_at__gt=claimed_at - PAYMENT_WINDOW,
    ).update(checkout_creation_started_at=claimed_at)
    if not claimed:
        messages.error(request, "Payment is already being set up or this reservation is unavailable.")
        return redirect("my_reservations")

    try:
        checkout_session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
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

    except stripe.StripeError:
        Reservation.objects.filter(
            id=reservation.id, status=Reservation.Status.PENDING,
            checkout_creation_started_at=claimed_at,
            stripe_checkout_session_id="",
        ).update(checkout_creation_started_at=None)
        logger.exception(
            "Could not create Checkout session for reservation %s",
            reservation.id,
        )
        messages.error(
            request,
            "Unable to start payment. Please try again."
        )
        return redirect("my_reservations")

    # Attach the session even if the 15-minute deadline elapsed during
    # the API call. Expiration can now close it before releasing the lane.
    updated = Reservation.objects.filter(
        id=reservation.id,
        status=Reservation.Status.PENDING,
        checkout_creation_started_at=claimed_at,
        stripe_checkout_session_id="",
    ).update(
        stripe_checkout_session_id=checkout_session.id,
        checkout_creation_started_at=None,
    )
    if not updated:
        # Do not silently release the lane if the session cannot be closed.
        try:
            stripe.checkout.Session.expire(checkout_session.id)
        except stripe.StripeError as exc:
            flag_payment_review(reservation, "unassigned_checkout_session", str(exc), checkout_session.id)
            logger.exception("Could not close unassigned Checkout session %s", checkout_session.id)
        messages.error(request, "This reservation is no longer available for payment.")
        return redirect("my_reservations")

    if timezone.now() >= reservation.payment_deadline:
        expire_unpaid_reservations()
        messages.error(request, "The payment window ended while Checkout was starting. Please review your reservation.")
        return redirect("my_reservations")
    return redirect(checkout_session.url)


@csrf_exempt
@require_POST
def stripe_webhook(request):
    if not settings.STRIPE_WEBHOOK_SECRET:
        return HttpResponse(
            "Webhook not configured",
            status=503,
        )

    signature = request.headers.get(
        "Stripe-Signature",
        "",
    )

    try:
        event = stripe.Webhook.construct_event(
            request.body,
            signature,
            settings.STRIPE_WEBHOOK_SECRET,
        )
    except (
        ValueError,
        stripe.SignatureVerificationError,
    ):
        return HttpResponse(status=400)

    if event["type"] != "checkout.session.completed":
        return HttpResponse(status=200)

    session_object = event["data"]["object"]

    session = (
        session_object.to_dict()
        if hasattr(session_object, "to_dict")
        else dict(session_object)
    )

    reservation_id = (
        session.get("metadata") or {}
    ).get("reservation_id")

    if not (
        session.get("payment_status") == "paid"
        and session.get("amount_total") == 1500
        and session.get("currency") == "usd"
        and session.get("mode") == "payment"
        and reservation_id
        and session.get("client_reference_id")
            == str(reservation_id)
        and session.get("id")
    ):
        return HttpResponse(status=200)

    try:
        reservation_id = int(reservation_id)
    except (ValueError, TypeError):
        return HttpResponse(status=200)

    event_created = event.get("created")

    if event_created is not None:
        try:
            payment_event_time = datetime.fromtimestamp(
                int(event_created),
                tz=timezone.get_current_timezone(),
            )
        except (ValueError, TypeError, OverflowError):
            payment_event_time = None
    else:
        payment_event_time = None

    with transaction.atomic():
        reservation = (
            Reservation.objects.select_for_update()
            .filter(id=reservation_id)
            .first()
        )

        if reservation is None:
            logger.error(
                "Paid Checkout session %s references missing reservation %s",
                session.get("id"),
                reservation_id,
            )
            return HttpResponse(status=200)

        if reservation.stripe_checkout_session_id != session.get("id"):
            flag_payment_review(reservation, "checkout_session_mismatch", session_id=session.get("id"))
            logger.error(
                "Checkout session mismatch for reservation %s",
                reservation_id,
            )
            return HttpResponse(status=200)

        if reservation.status == Reservation.Status.CONFIRMED:
            return HttpResponse(status=200)

        if reservation.status != Reservation.Status.PENDING:
            flag_payment_review(reservation, "payment_for_inactive_reservation", session_id=session.get("id"))
            logger.error(
                "Paid Checkout session %s belongs to reservation %s "
                "with status %s; manual payment review required",
                session.get("id"),
                reservation_id,
                reservation.status,
            )
            return HttpResponse(status=200)

        if payment_event_time is None:
            flag_payment_review(reservation, "missing_payment_timestamp", session_id=session.get("id"))
            logger.error(
                "Missing payment event timestamp for reservation %s",
                reservation_id,
            )
            return HttpResponse(status=200)

        if payment_event_time > reservation.payment_deadline:
            flag_payment_review(reservation, "late_payment", session_id=session.get("id"))
            logger.error(
                "Late payment for reservation %s; "
                "manual refund or booking review required",
                reservation_id,
            )
            return HttpResponse(status=200)

        reservation.status = Reservation.Status.CONFIRMED
        reservation.save(update_fields=["status"])

    return HttpResponse(status=200)

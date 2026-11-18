from datetime import timedelta

import stripe
from django.conf import settings
from django.utils import timezone
from .models import PaymentReview, Reservation

PAYMENT_WINDOW = timedelta(minutes=15)


def flag_payment_review(reservation, reason, details="", session_id=None):
    """Create one open review entry per reservation/reason/session combination."""
    session_id = reservation.stripe_checkout_session_id if session_id is None else session_id
    review, _ = PaymentReview.objects.get_or_create(
        reservation=reservation, reason=reason, stripe_session_id=session_id,
        resolved=False, defaults={"details": details},
    )
    return review


def verify_checkout_session_closed(session_id):
    """Return (safe_to_release, review_reason, details).

    The result of Session.expire, not the earlier retrieval, is authoritative
    when the session was open. Uncertain results retain the lane.
    Stripe API errors propagate to the caller for its existing handling.
    """
    session = stripe.checkout.Session.retrieve(session_id)
    if getattr(session, "payment_status", None) == "paid":
        return False, "paid_session_unreconciled", "Stripe reports a paid session"

    if session.status == "open":
        result = stripe.checkout.Session.expire(session_id)
        if getattr(result, "payment_status", None) == "paid":
            return False, "paid_session_unreconciled", "Stripe expiration returned a paid session"
        if (getattr(result, "status", None) == "expired"
                and getattr(result, "payment_status", None) == "unpaid"):
            return True, "", ""
        return False, "checkout_expiration_unverified", "Stripe expiration response did not confirm an expired, unpaid session"

    if session.status == "expired":
        if getattr(session, "payment_status", None) == "unpaid":
            return True, "", ""
        return False, "checkout_expiration_unverified", "Expired session has an unverified payment status"

    if session.status == "complete":
        return False, "completed_session_unreconciled", "Checkout session completed"

    return False, "unknown_session_status", str(session.status)


def expire_unpaid_reservations():
    """Safely release old unpaid lanes; retain uncertain Stripe payments."""
    cutoff = timezone.now() - PAYMENT_WINDOW
    expired_count = 0
    review_count = 0
    pending = Reservation.objects.filter(status=Reservation.Status.PENDING, created_at__lte=cutoff)
    for reservation in pending:
        if reservation.checkout_creation_started_at is not None:
            flag_payment_review(reservation, "checkout_creation_unresolved")
            review_count += 1
            continue
        if (
            not reservation.stripe_checkout_session_id
            and PaymentReview.objects.filter(
                reservation=reservation,
                reason="checkout_creation_api_error",
                resolved=False,
            ).exists()
        ):
            review_count += 1
            continue
        session_id = reservation.stripe_checkout_session_id
        if session_id:
            if not settings.STRIPE_SECRET_KEY:
                flag_payment_review(reservation, "stripe_not_configured")
                review_count += 1
                continue
            stripe.api_key = settings.STRIPE_SECRET_KEY
            try:
                safe, reason, details = verify_checkout_session_closed(session_id)
            except stripe.StripeError as exc:
                flag_payment_review(reservation, "stripe_api_error", str(exc))
                review_count += 1
                continue
            if not safe:
                flag_payment_review(reservation, reason, details, session_id=session_id)
                review_count += 1
                continue
        updated = Reservation.objects.filter(
            id=reservation.id, status=Reservation.Status.PENDING,
            checkout_creation_started_at__isnull=True,
            stripe_checkout_session_id=session_id,
        ).update(status=Reservation.Status.EXPIRED)
        expired_count += updated
    return expired_count, review_count

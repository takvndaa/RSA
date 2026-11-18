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


def expire_unpaid_reservations():
    """Safely release old unpaid lanes; retain uncertain Stripe payments."""
    cutoff = timezone.now() - PAYMENT_WINDOW
    expired_count = 0
    review_count = 0
    pending = Reservation.objects.filter(status=Reservation.Status.PENDING, created_at__lte=cutoff)
    for reservation in pending:
        # Never release a lane while Stripe session creation is in progress,
        # including if the worker died and left the claim behind.
        if reservation.checkout_creation_started_at is not None:
            flag_payment_review(reservation, "checkout_creation_unresolved")
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
                session = stripe.checkout.Session.retrieve(session_id)
                if session.status == "open":
                    stripe.checkout.Session.expire(session_id)
                elif session.status == "complete":
                    flag_payment_review(reservation, "completed_session_unreconciled")
                    review_count += 1
                    continue
                elif session.status != "expired":
                    flag_payment_review(reservation, "unknown_session_status", str(session.status))
                    review_count += 1
                    continue
            except stripe.StripeError as exc:
                flag_payment_review(reservation, "stripe_api_error", str(exc))
                review_count += 1
                continue
        updated = Reservation.objects.filter(
            id=reservation.id, status=Reservation.Status.PENDING,
            checkout_creation_started_at__isnull=True,
            stripe_checkout_session_id=session_id,
        ).update(status=Reservation.Status.EXPIRED)
        expired_count += updated
    return expired_count, review_count

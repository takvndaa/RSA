from datetime import datetime, timedelta, time
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import stripe

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Lane, PaymentReview, Reservation, TimeSlot
from .services import expire_unpaid_reservations


User = get_user_model()


class BookingTestSetup(TestCase):
    """
    Shared setup and helpers.

    This class contains no test methods, so Django will not
    run duplicate tests.
    """

    def setUp(self):
        self.member = User.objects.create_user(
            username="swimmer1",
            email="swimmer1@example.com",
            password="TestPassword123!",
            approval_status=User.ApprovalStatus.APPROVED,
        )

        self.other_member = User.objects.create_user(
            username="swimmer2",
            email="swimmer2@example.com",
            password="TestPassword123!",
            approval_status=User.ApprovalStatus.APPROVED,
        )

        self.lane1 = Lane.objects.create(number=1)
        self.lane2 = Lane.objects.create(number=2)

        self.booking_date = timezone.localdate() + timedelta(days=3)

        self.start_time = datetime.combine(
            self.booking_date,
            time(hour=10),
            tzinfo=ZoneInfo(settings.TIME_ZONE),
        )

        self.timeslot = TimeSlot.objects.create(
            start_time=self.start_time
        )

        self.client.force_login(self.member)

    def booking_data(self, lane=1, hour=10):
        return {
            "date": self.booking_date.isoformat(),
            "lane": lane,
            "hour": hour,
        }

    def make_reservation(self, minutes_ago=20, session_id=""):
        reservation = Reservation.objects.create(
            member=self.member,
            lane=self.lane1,
            timeslot=self.timeslot,
            stripe_checkout_session_id=session_id,
        )

        Reservation.objects.filter(
            id=reservation.id
        ).update(
            created_at=timezone.now() - timedelta(
                minutes=minutes_ago
            )
        )

        reservation.refresh_from_db()
        return reservation


class BookingTests(BookingTestSetup):

    def test_availability_page_loads(self):
        response = self.client.get(reverse("availability"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reserve a Swimming Lane")

    def test_approved_member_can_reserve_lane(self):
        response = self.client.post(
            reverse("create_reservation"),
            self.booking_data(),
        )

        self.assertRedirects(response, reverse("my_reservations"))

        reservation = Reservation.objects.get(member=self.member)

        self.assertEqual(reservation.lane, self.lane1)
        self.assertEqual(
            reservation.status,
            Reservation.Status.PENDING,
        )

    def test_unapproved_member_cannot_reserve(self):
        self.member.approval_status = User.ApprovalStatus.PENDING
        self.member.save()

        self.client.post(
            reverse("create_reservation"),
            self.booking_data(),
        )

        self.assertEqual(Reservation.objects.count(), 0)

    def test_same_lane_cannot_be_double_booked(self):
        Reservation.objects.create(
            member=self.other_member,
            lane=self.lane1,
            timeslot=self.timeslot,
        )

        self.client.post(
            reverse("create_reservation"),
            self.booking_data(),
        )

        self.assertEqual(Reservation.objects.count(), 1)

    def test_member_cannot_reserve_two_lanes_at_same_time(self):
        Reservation.objects.create(
            member=self.member,
            lane=self.lane1,
            timeslot=self.timeslot,
        )

        self.client.post(
            reverse("create_reservation"),
            self.booking_data(lane=2),
        )

        self.assertEqual(Reservation.objects.count(), 1)

    def test_booking_less_than_one_hour_ahead_is_rejected(self):
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)

        self.client.post(
            reverse("create_reservation"),
            {
                "date": yesterday.isoformat(),
                "lane": 1,
                "hour": 10,
            },
        )

        self.assertEqual(Reservation.objects.count(), 0)

    def test_member_can_cancel_reservation(self):
        reservation = Reservation.objects.create(
            member=self.member,
            lane=self.lane1,
            timeslot=self.timeslot,
        )

        response = self.client.post(
            reverse("cancel_reservation", args=[reservation.id])
        )

        self.assertRedirects(response, reverse("my_reservations"))

        reservation.refresh_from_db()

        self.assertEqual(
            reservation.status,
            Reservation.Status.CANCELLED,
        )

    def test_member_cannot_cancel_another_members_reservation(self):
        reservation = Reservation.objects.create(
            member=self.other_member,
            lane=self.lane1,
            timeslot=self.timeslot,
        )

        response = self.client.post(
            reverse("cancel_reservation", args=[reservation.id])
        )

        self.assertEqual(response.status_code, 404)

        reservation.refresh_from_db()

        self.assertEqual(
            reservation.status,
            Reservation.Status.PENDING,
        )

    def test_cancelled_lane_can_be_booked_again(self):
        Reservation.objects.create(
            member=self.other_member,
            lane=self.lane1,
            timeslot=self.timeslot,
            status=Reservation.Status.CANCELLED,
        )

        self.client.post(
            reverse("create_reservation"),
            self.booking_data(),
        )

        self.assertEqual(
            Reservation.objects.filter(
                status=Reservation.Status.PENDING
            ).count(),
            1,
        )

    def test_database_prevents_duplicate_active_lane_booking(self):
        Reservation.objects.create(
            member=self.member,
            lane=self.lane1,
            timeslot=self.timeslot,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Reservation.objects.create(
                    member=self.other_member,
                    lane=self.lane1,
                    timeslot=self.timeslot,
                )

    def test_database_prevents_member_booking_two_lanes(self):
        Reservation.objects.create(
            member=self.member,
            lane=self.lane1,
            timeslot=self.timeslot,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Reservation.objects.create(
                    member=self.member,
                    lane=self.lane2,
                    timeslot=self.timeslot,
                )


class PaymentExpirationTests(BookingTestSetup):
    """
    Tests payment deadlines, Stripe session expiration,
    and webhook session verification.
    """

    def test_old_pending_reservation_expires(self):
        reservation = self.make_reservation()

        expired_count, review_count = expire_unpaid_reservations()

        reservation.refresh_from_db()

        self.assertEqual(
            reservation.status,
            Reservation.Status.EXPIRED,
        )
        self.assertEqual(expired_count, 1)
        self.assertEqual(review_count, 0)

    def test_recent_pending_reservation_does_not_expire(self):
        reservation = self.make_reservation(minutes_ago=5)

        expired_count, review_count = expire_unpaid_reservations()

        reservation.refresh_from_db()

        self.assertEqual(
            reservation.status,
            Reservation.Status.PENDING,
        )
        self.assertEqual(expired_count, 0)
        self.assertEqual(review_count, 0)

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.expire")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_open_stripe_session_is_closed_before_expiration(
        self,
        mock_retrieve,
        mock_expire,
    ):
        reservation = self.make_reservation(
            session_id="cs_test_open"
        )

        mock_retrieve.return_value = SimpleNamespace(
            status="open"
        )
        mock_expire.return_value = SimpleNamespace(status="expired", payment_status="unpaid")

        expired_count, review_count = expire_unpaid_reservations()

        reservation.refresh_from_db()

        mock_retrieve.assert_called_once_with("cs_test_open")
        mock_expire.assert_called_once_with("cs_test_open")

        self.assertEqual(
            reservation.status,
            Reservation.Status.EXPIRED,
        )
        self.assertEqual(expired_count, 1)
        self.assertEqual(review_count, 0)

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_completed_stripe_session_requires_review(
        self,
        mock_retrieve,
    ):
        reservation = self.make_reservation(
            session_id="cs_test_complete"
        )

        mock_retrieve.return_value = SimpleNamespace(
            status="complete"
        )

        expired_count, review_count = expire_unpaid_reservations()

        reservation.refresh_from_db()

        self.assertEqual(
            reservation.status,
            Reservation.Status.PENDING,
        )
        self.assertEqual(expired_count, 0)
        self.assertEqual(review_count, 1)

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_stripe_api_failure_does_not_release_lane(
        self,
        mock_retrieve,
    ):
        reservation = self.make_reservation(
            session_id="cs_test_error"
        )

        mock_retrieve.side_effect = stripe.APIConnectionError(
            message="Simulated Stripe connection failure"
        )

        expired_count, review_count = expire_unpaid_reservations()

        reservation.refresh_from_db()

        self.assertEqual(
            reservation.status,
            Reservation.Status.PENDING,
        )
        self.assertEqual(expired_count, 0)
        self.assertEqual(review_count, 1)

    def test_expired_lane_can_be_booked_again(self):
        reservation = self.make_reservation()

        expire_unpaid_reservations()

        self.client.post(
            reverse("create_reservation"),
            self.booking_data(),
        )

        reservation.refresh_from_db()

        self.assertEqual(
            reservation.status,
            Reservation.Status.EXPIRED,
        )

        self.assertTrue(
            Reservation.objects.filter(
                member=self.member,
                lane=self.lane1,
                timeslot=self.timeslot,
                status=Reservation.Status.PENDING,
            ).exists()
        )

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_mock")
    @patch("bookings.views.stripe.Webhook.construct_event")
    def test_unrelated_checkout_session_cannot_confirm_reservation(
        self,
        mock_construct_event,
    ):
        reservation = self.make_reservation(
            minutes_ago=5,
            session_id="cs_test_correct",
        )

        session = stripe.checkout.Session.construct_from(
            {
                "id": "cs_test_wrong",
                "metadata": {
                    "reservation_id": str(reservation.id),
                },
                "client_reference_id": str(reservation.id),
                "payment_status": "paid",
                "amount_total": 1500,
                "currency": "usd",
                "mode": "payment",
            },
            "sk_test_mock",
        )

        mock_construct_event.return_value = {
            "type": "checkout.session.completed",
            "data": {
                "object": session,
            },
        }

        response = self.client.post(
            reverse("stripe_webhook"),
            data=b'{"test": true}',
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="mock_signature",
        )

        reservation.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            reservation.status,
            Reservation.Status.PENDING,
        )

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_mock")
    @patch("bookings.views.stripe.Webhook.construct_event")
    def test_valid_payment_confirms_reservation(
        self,
        mock_construct_event,
    ):
        reservation = self.make_reservation(
            minutes_ago=5,
            session_id="cs_test_valid",
        )

        mock_construct_event.return_value = {
            "type": "checkout.session.completed",
            "created": int(timezone.now().timestamp()),
            "data": {
                "object": stripe.checkout.Session.construct_from(
                    {
                        "id": "cs_test_valid",
                        "metadata": {
                            "reservation_id": str(reservation.id),
                        },
                        "client_reference_id": str(reservation.id),
                        "payment_status": "paid",
                        "amount_total": 1500,
                        "currency": "usd",
                        "mode": "payment",
                    },
                    "sk_test_mock",
                ),
            },
        }

        response = self.client.post(
            reverse("stripe_webhook"),
            data=b'{"test": true}',
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="mock_signature",
        )

        reservation.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            reservation.status,
            Reservation.Status.CONFIRMED,
        )

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_mock")
    @patch("bookings.views.stripe.Webhook.construct_event")
    def test_delayed_webhook_confirms_on_time_payment(
        self,
        mock_construct_event,
    ):
        reservation = self.make_reservation(
            minutes_ago=20,
            session_id="cs_test_delayed",
        )

        payment_time = reservation.created_at + timedelta(
            minutes=14
        )

        mock_construct_event.return_value = {
            "type": "checkout.session.completed",
            "created": int(payment_time.timestamp()),
            "data": {
                "object": stripe.checkout.Session.construct_from(
                    {
                        "id": "cs_test_delayed",
                        "metadata": {
                            "reservation_id": str(reservation.id),
                        },
                        "client_reference_id": str(reservation.id),
                        "payment_status": "paid",
                        "amount_total": 1500,
                        "currency": "usd",
                        "mode": "payment",
                    },
                    "sk_test_mock",
                ),
            },
        }

        response = self.client.post(
            reverse("stripe_webhook"),
            data=b'{"test": true}',
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="mock_signature",
        )

        reservation.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            reservation.status,
            Reservation.Status.CONFIRMED,
        )

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_mock")
    @patch("bookings.views.stripe.Webhook.construct_event")
    def test_late_payment_is_held_for_review(
        self,
        mock_construct_event,
    ):
        reservation = self.make_reservation(
            minutes_ago=20,
            session_id="cs_test_late",
        )

        payment_time = reservation.created_at + timedelta(
            minutes=16
        )

        mock_construct_event.return_value = {
            "type": "checkout.session.completed",
            "created": int(payment_time.timestamp()),
            "data": {
                "object": stripe.checkout.Session.construct_from(
                    {
                        "id": "cs_test_late",
                        "metadata": {
                            "reservation_id": str(reservation.id),
                        },
                        "client_reference_id": str(reservation.id),
                        "payment_status": "paid",
                        "amount_total": 1500,
                        "currency": "usd",
                        "mode": "payment",
                    },
                    "sk_test_mock",
                ),
            },
        }

        response = self.client.post(
            reverse("stripe_webhook"),
            data=b'{"test": true}',
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="mock_signature",
        )

        reservation.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            reservation.status,
            Reservation.Status.PENDING,
        )


    @override_settings(
        STRIPE_SECRET_KEY="sk_test_mock",
        SITE_URL="http://testserver",
    )
    @patch("bookings.views.stripe.checkout.Session.create")
    def test_checkout_session_id_is_saved(
        self,
        mock_create,
    ):
        reservation = self.make_reservation(
            minutes_ago=5
        )

        mock_create.return_value = SimpleNamespace(
            id="cs_test_new_checkout",
            url="https://checkout.stripe.com/test-session",
        )

        response = self.client.post(
            reverse(
                "checkout_reservation",
                args=[reservation.id],
            )
        )

        reservation.refresh_from_db()

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.url,
            "https://checkout.stripe.com/test-session",
        )
        self.assertEqual(
            reservation.stripe_checkout_session_id,
            "cs_test_new_checkout",
        )

        mock_create.assert_called_once()

    
    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.views.stripe.checkout.Session.create")
    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_existing_checkout_session_is_reused(
        self,
        mock_retrieve,
        mock_create,
    ):
        reservation = self.make_reservation(
            minutes_ago=5,
            session_id="cs_test_existing",
        )

        mock_retrieve.return_value = SimpleNamespace(
            id="cs_test_existing",
            status="open",
            url="https://checkout.stripe.com/existing-session",
        )

        response = self.client.post(
            reverse(
                "checkout_reservation",
                args=[reservation.id],
            )
        )

        reservation.refresh_from_db()

        self.assertEqual(response.status_code, 302)

        self.assertEqual(
            response.url,
            "https://checkout.stripe.com/existing-session",
        )

        self.assertEqual(
            reservation.stripe_checkout_session_id,
            "cs_test_existing",
        )

        mock_retrieve.assert_called_once_with(
            "cs_test_existing"
        )

        mock_create.assert_not_called()


    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.views.stripe.checkout.Session.expire")
    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_cancelling_reservation_closes_stripe_session(
        self,
        mock_retrieve,
        mock_expire,
    ):
        reservation = self.make_reservation(
            minutes_ago=5,
            session_id="cs_test_cancel",
        )

        mock_retrieve.return_value = SimpleNamespace(
            id="cs_test_cancel",
            status="open",
        )
        mock_expire.return_value = SimpleNamespace(status="expired", payment_status="unpaid")

        response = self.client.post(
            reverse("cancel_reservation", args=[reservation.id])
        )

        reservation.refresh_from_db()

        self.assertRedirects(response, reverse("my_reservations"))
        mock_retrieve.assert_called_once_with("cs_test_cancel")
        mock_expire.assert_called_once_with("cs_test_cancel")
        self.assertEqual(
            reservation.status,
            Reservation.Status.CANCELLED,
        )

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.views.stripe.checkout.Session.expire")
    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_stripe_failure_prevents_cancellation(
        self,
        mock_retrieve,
        mock_expire,
    ):
        reservation = self.make_reservation(
            minutes_ago=5,
            session_id="cs_test_failure",
        )

        mock_retrieve.return_value = SimpleNamespace(
            id="cs_test_failure",
            status="open",
        )
        mock_expire.side_effect = stripe.APIConnectionError(
            message="Simulated Stripe connection failure"
        )

        response = self.client.post(
            reverse("cancel_reservation", args=[reservation.id])
        )

        reservation.refresh_from_db()

        self.assertRedirects(response, reverse("my_reservations"))
        mock_retrieve.assert_called_once_with("cs_test_failure")
        mock_expire.assert_called_once_with("cs_test_failure")
        self.assertEqual(
            reservation.status,
            Reservation.Status.PENDING,
        )

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.views.stripe.checkout.Session.expire")
    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_completed_payment_session_prevents_cancellation(
        self, mock_retrieve, mock_expire,
    ):
        reservation = self.make_reservation(
            minutes_ago=5, session_id="cs_test_complete_cancel",
        )
        mock_retrieve.return_value = SimpleNamespace(status="complete")

        response = self.client.post(
            reverse("cancel_reservation", args=[reservation.id])
        )
        reservation.refresh_from_db()

        self.assertRedirects(response, reverse("my_reservations"))
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        mock_retrieve.assert_called_once_with("cs_test_complete_cancel")
        mock_expire.assert_not_called()

    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_confirmed_reservation_requires_staff_cancellation(self, mock_retrieve):
        reservation = self.make_reservation(minutes_ago=5)
        reservation.status = Reservation.Status.CONFIRMED
        reservation.save(update_fields=["status"])

        response = self.client.post(
            reverse("cancel_reservation", args=[reservation.id])
        )
        reservation.refresh_from_db()

        self.assertRedirects(response, reverse("my_reservations"))
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        mock_retrieve.assert_not_called()

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_already_expired_stripe_session_releases_lane(self, mock_retrieve):
        reservation = self.make_reservation(session_id="cs_test_already_expired")
        mock_retrieve.return_value = SimpleNamespace(status="expired", payment_status="unpaid")

        expired_count, review_count = expire_unpaid_reservations()
        reservation.refresh_from_db()

        self.assertEqual(reservation.status, Reservation.Status.EXPIRED)
        self.assertEqual((expired_count, review_count), (1, 0))

    def post_mock_payment_webhook(
        self, reservation, session_id, *, amount=1500,
        currency="usd", mode="payment", payment_status="paid",
        reference_id=None, metadata_id=None,
    ):
        """Send a signed-event mock; never contact Stripe."""
        if reference_id is None:
            reference_id = str(reservation.id)
        if metadata_id is None:
            metadata_id = str(reservation.id)
        session = stripe.checkout.Session.construct_from(
            {
                "id": session_id,
                "metadata": {"reservation_id": metadata_id},
                "client_reference_id": reference_id,
                "payment_status": payment_status,
                "amount_total": amount,
                "currency": currency,
                "mode": mode,
            },
            "sk_test_mock",
        )
        event = {
            "type": "checkout.session.completed",
            "created": int(timezone.now().timestamp()),
            "data": {"object": session},
        }
        with patch(
            "bookings.views.stripe.Webhook.construct_event",
            return_value=event,
        ):
            return self.client.post(
                reverse("stripe_webhook"),
                data=b'{"test": true}',
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="mock_signature",
            )

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_mock")
    def test_webhook_rejects_wrong_amount(self):
        reservation = self.make_reservation(
            minutes_ago=5, session_id="cs_test_wrong_amount",
        )
        response = self.post_mock_payment_webhook(
            reservation, "cs_test_wrong_amount", amount=1400,
        )
        reservation.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reservation.status, Reservation.Status.PENDING)

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_mock")
    def test_webhook_rejects_wrong_currency(self):
        reservation = self.make_reservation(
            minutes_ago=5, session_id="cs_test_wrong_currency",
        )
        response = self.post_mock_payment_webhook(
            reservation, "cs_test_wrong_currency", currency="eur",
        )
        reservation.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reservation.status, Reservation.Status.PENDING)

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_mock")
    def test_webhook_rejects_unpaid_session(self):
        reservation = self.make_reservation(
            minutes_ago=5, session_id="cs_test_unpaid",
        )
        response = self.post_mock_payment_webhook(
            reservation, "cs_test_unpaid", payment_status="unpaid",
        )
        reservation.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reservation.status, Reservation.Status.PENDING)

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_mock")
    def test_webhook_rejects_wrong_checkout_mode(self):
        reservation = self.make_reservation(
            minutes_ago=5, session_id="cs_test_wrong_mode",
        )
        response = self.post_mock_payment_webhook(
            reservation, "cs_test_wrong_mode", mode="subscription",
        )
        reservation.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reservation.status, Reservation.Status.PENDING)

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_mock")
    def test_webhook_rejects_mismatched_client_reference(self):
        reservation = self.make_reservation(
            minutes_ago=5, session_id="cs_test_wrong_reference",
        )
        response = self.post_mock_payment_webhook(
            reservation, "cs_test_wrong_reference", reference_id="999999",
        )
        reservation.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reservation.status, Reservation.Status.PENDING)

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_mock")
    def test_webhook_rejects_mismatched_metadata(self):
        reservation = self.make_reservation(
            minutes_ago=5, session_id="cs_test_wrong_metadata",
        )
        response = self.post_mock_payment_webhook(
            reservation, "cs_test_wrong_metadata", metadata_id="999999",
        )
        reservation.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reservation.status, Reservation.Status.PENDING)

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_mock")
    def test_duplicate_payment_webhook_keeps_reservation_confirmed(self):
        reservation = self.make_reservation(
            minutes_ago=5, session_id="cs_test_duplicate",
        )
        first = self.post_mock_payment_webhook(reservation, "cs_test_duplicate")
        second = self.post_mock_payment_webhook(reservation, "cs_test_duplicate")
        reservation.refresh_from_db()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        self.assertEqual(Reservation.objects.filter(pk=reservation.pk).count(), 1)

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_mock")
    def test_paid_webhook_does_not_reactivate_cancelled_reservation(self):
        reservation = self.make_reservation(
            minutes_ago=5, session_id="cs_test_cancelled_webhook",
        )
        reservation.status = Reservation.Status.CANCELLED
        reservation.save(update_fields=["status"])
        response = self.post_mock_payment_webhook(
            reservation, "cs_test_cancelled_webhook",
        )
        reservation.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reservation.status, Reservation.Status.CANCELLED)


    @override_settings(STRIPE_SECRET_KEY="sk_test_mock", SITE_URL="http://testserver")
    @patch("bookings.views.stripe.checkout.Session.create")
    def test_checkout_claim_exists_before_stripe_create(self, mock_create):
        reservation = self.make_reservation(minutes_ago=5)

        def inspect_claim(**kwargs):
            reservation.refresh_from_db()
            self.assertIsNotNone(reservation.checkout_creation_started_at)
            self.assertEqual(reservation.stripe_checkout_session_id, "")
            return SimpleNamespace(id="cs_claim_before", url="https://checkout.stripe.com/claim")

        mock_create.side_effect = inspect_claim
        response = self.client.post(reverse("checkout_reservation", args=[reservation.id]))
        self.assertEqual(response.status_code, 302)
        mock_create.assert_called_once()

    def test_expiration_skips_active_checkout_claim(self):
        reservation = self.make_reservation(minutes_ago=20)
        Reservation.objects.filter(pk=reservation.pk).update(
            checkout_creation_started_at=timezone.now()
        )
        expired, review = expire_unpaid_reservations()
        reservation.refresh_from_db()
        self.assertEqual((expired, review), (0, 1))
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertTrue(PaymentReview.objects.filter(
            reservation=reservation, reason="checkout_creation_unresolved"
        ).exists())

    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_cancellation_rejected_during_checkout_creation(self, mock_retrieve):
        reservation = self.make_reservation(minutes_ago=5)
        Reservation.objects.filter(pk=reservation.pk).update(
            checkout_creation_started_at=timezone.now()
        )
        response = self.client.post(reverse("cancel_reservation", args=[reservation.id]))
        reservation.refresh_from_db()
        self.assertRedirects(response, reverse("my_reservations"))
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        mock_retrieve.assert_not_called()

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.views.stripe.checkout.Session.create")
    def test_duplicate_checkout_request_during_claim_does_not_create_session(self, mock_create):
        reservation = self.make_reservation(minutes_ago=5)
        Reservation.objects.filter(pk=reservation.pk).update(
            checkout_creation_started_at=timezone.now()
        )
        response = self.client.post(reverse("checkout_reservation", args=[reservation.id]))
        self.assertRedirects(response, reverse("my_reservations"))
        mock_create.assert_not_called()
        self.assertEqual(Reservation.objects.count(), 1)

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock", SITE_URL="http://testserver")
    @patch("bookings.views.stripe.checkout.Session.create")
    def test_successful_checkout_clears_claim(self, mock_create):
        reservation = self.make_reservation(minutes_ago=5)
        mock_create.return_value = SimpleNamespace(
            id="cs_claim_cleared", url="https://checkout.stripe.com/claim-cleared"
        )
        response = self.client.post(reverse("checkout_reservation", args=[reservation.id]))
        reservation.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(reservation.checkout_creation_started_at)
        self.assertEqual(reservation.stripe_checkout_session_id, "cs_claim_cleared")

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock", SITE_URL="http://testserver")
    @patch("bookings.views.stripe.checkout.Session.create")
    def test_stripe_create_failure_clears_claim_for_retry(self, mock_create):
        reservation = self.make_reservation(minutes_ago=5)
        mock_create.side_effect = stripe.APIConnectionError(
            message="Simulated Checkout creation failure"
        )
        response = self.client.post(reverse("checkout_reservation", args=[reservation.id]))
        reservation.refresh_from_db()
        self.assertRedirects(response, reverse("my_reservations"))
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertIsNone(reservation.checkout_creation_started_at)
        self.assertEqual(reservation.stripe_checkout_session_id, "")

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_mock")
    def test_late_payment_creates_review_record(self):
        reservation = self.make_reservation(minutes_ago=20, session_id="cs_review_late")
        response = self.post_mock_payment_webhook(reservation, "cs_review_late")
        reservation.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertEqual(PaymentReview.objects.filter(
            reservation=reservation, reason="late_payment",
            stripe_session_id="cs_review_late", resolved=False
        ).count(), 1)

    @override_settings(STRIPE_WEBHOOK_SECRET="whsec_mock")
    def test_repeated_late_webhook_does_not_duplicate_review(self):
        reservation = self.make_reservation(minutes_ago=20, session_id="cs_review_repeat")
        first = self.post_mock_payment_webhook(reservation, "cs_review_repeat")
        second = self.post_mock_payment_webhook(reservation, "cs_review_repeat")
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(PaymentReview.objects.filter(
            reservation=reservation, reason="late_payment",
            stripe_session_id="cs_review_repeat", resolved=False
        ).count(), 1)


    def test_reservation_just_before_payment_deadline_stays_pending(self):
        reservation = self.make_reservation(minutes_ago=5)
        now = timezone.now()
        Reservation.objects.filter(pk=reservation.pk).update(
            created_at=now - timedelta(minutes=15) + timedelta(seconds=1)
        )
        with patch("bookings.services.timezone.now", return_value=now):
            counts = expire_unpaid_reservations()
        reservation.refresh_from_db()
        self.assertEqual(counts, (0, 0))
        self.assertEqual(reservation.status, Reservation.Status.PENDING)

    def test_reservation_at_exact_payment_deadline_expires(self):
        reservation = self.make_reservation(minutes_ago=5)
        now = timezone.now()
        Reservation.objects.filter(pk=reservation.pk).update(
            created_at=now - timedelta(minutes=15)
        )
        with patch("bookings.services.timezone.now", return_value=now):
            counts = expire_unpaid_reservations()
        reservation.refresh_from_db()
        self.assertEqual(counts, (1, 0))
        self.assertEqual(reservation.status, Reservation.Status.EXPIRED)

    def test_repeated_expiration_does_not_count_same_reservation_twice(self):
        reservation = self.make_reservation(minutes_ago=20)
        first = expire_unpaid_reservations()
        second = expire_unpaid_reservations()
        reservation.refresh_from_db()
        self.assertEqual(first, (1, 0))
        self.assertEqual(second, (0, 0))
        self.assertEqual(reservation.status, Reservation.Status.EXPIRED)

    def test_interrupted_checkout_claim_keeps_one_review_record(self):
        reservation = self.make_reservation(minutes_ago=20)
        Reservation.objects.filter(pk=reservation.pk).update(
            checkout_creation_started_at=timezone.now() - timedelta(minutes=30)
        )
        self.assertEqual(expire_unpaid_reservations(), (0, 1))
        self.assertEqual(expire_unpaid_reservations(), (0, 1))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertEqual(PaymentReview.objects.filter(
            reservation=reservation, reason="checkout_creation_unresolved", resolved=False
        ).count(), 1)

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_completed_checkout_creates_reconciliation_review(self, mock_retrieve):
        reservation = self.make_reservation(minutes_ago=20, session_id="cs_review_complete")
        mock_retrieve.return_value = SimpleNamespace(status="complete")
        self.assertEqual(expire_unpaid_reservations(), (0, 1))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertTrue(PaymentReview.objects.filter(
            reservation=reservation, reason="completed_session_unreconciled",
            stripe_session_id="cs_review_complete", resolved=False
        ).exists())

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_unknown_checkout_status_creates_review(self, mock_retrieve):
        reservation = self.make_reservation(minutes_ago=20, session_id="cs_review_unknown")
        mock_retrieve.return_value = SimpleNamespace(status="unexpected")
        self.assertEqual(expire_unpaid_reservations(), (0, 1))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertTrue(PaymentReview.objects.filter(
            reservation=reservation, reason="unknown_session_status",
            stripe_session_id="cs_review_unknown", resolved=False
        ).exists())

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_expiration_stripe_connection_failure_creates_review(self, mock_retrieve):
        reservation = self.make_reservation(minutes_ago=20, session_id="cs_review_api_error")
        mock_retrieve.side_effect = stripe.APIConnectionError(message="Simulated outage")
        self.assertEqual(expire_unpaid_reservations(), (0, 1))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertTrue(PaymentReview.objects.filter(
            reservation=reservation, reason="stripe_api_error",
            stripe_session_id="cs_review_api_error", resolved=False
        ).exists())

    @override_settings(STRIPE_SECRET_KEY="")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_missing_stripe_configuration_creates_review_without_api_call(self, mock_retrieve):
        reservation = self.make_reservation(minutes_ago=20, session_id="cs_review_no_key")
        self.assertEqual(expire_unpaid_reservations(), (0, 1))
        mock_retrieve.assert_not_called()
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertTrue(PaymentReview.objects.filter(
            reservation=reservation, reason="stripe_not_configured",
            stripe_session_id="cs_review_no_key", resolved=False
        ).exists())


class StripeIdempotencyTests(BookingTestSetup):
    """Focused regression tests for retry-safe Checkout creation."""

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock", SITE_URL="http://testserver")
    @patch("bookings.views.stripe.checkout.Session.create")
    def test_checkout_passes_reservation_specific_idempotency_key(self, mock_create):
        reservation = self.make_reservation(minutes_ago=5)
        mock_create.return_value = SimpleNamespace(id="cs_idem_one", url="https://checkout.stripe.com/one")
        self.client.post(reverse("checkout_reservation", args=[reservation.pk]))
        self.assertEqual(mock_create.call_args.kwargs["idempotency_key"],
                         f"diive-checkout-reservation-{reservation.pk}-v1")

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock", SITE_URL="http://testserver")
    @patch("bookings.views.stripe.checkout.Session.create")
    def test_checkout_idempotency_key_is_stable_after_connection_failure(self, mock_create):
        reservation = self.make_reservation(minutes_ago=5)
        mock_create.side_effect = [
            stripe.APIConnectionError(message="Simulated interrupted response"),
            SimpleNamespace(id="cs_retry", url="https://checkout.stripe.com/retry"),
        ]
        url = reverse("checkout_reservation", args=[reservation.pk])
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(mock_create.call_count, 2)
        keys = [call.kwargs["idempotency_key"] for call in mock_create.call_args_list]
        self.assertEqual(keys[0], keys[1])

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock", SITE_URL="http://testserver")
    @patch("bookings.views.stripe.checkout.Session.create")
    def test_checkout_idempotency_key_differs_between_reservations(self, mock_create):
        first = self.make_reservation(minutes_ago=5)
        second_slot = TimeSlot.objects.create(start_time=self.start_time + timedelta(hours=1))
        second = Reservation.objects.create(member=self.other_member, lane=self.lane2, timeslot=second_slot)
        Reservation.objects.filter(pk=second.pk).update(created_at=timezone.now() - timedelta(minutes=5))
        mock_create.side_effect = [
            SimpleNamespace(id="cs_first", url="https://checkout.stripe.com/first"),
            SimpleNamespace(id="cs_second", url="https://checkout.stripe.com/second"),
        ]
        self.client.post(reverse("checkout_reservation", args=[first.pk]))
        self.client.force_login(self.other_member)
        self.client.post(reverse("checkout_reservation", args=[second.pk]))
        self.assertNotEqual(mock_create.call_args_list[0].kwargs["idempotency_key"],
                            mock_create.call_args_list[1].kwargs["idempotency_key"])

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock", SITE_URL="http://testserver")
    @patch("bookings.views.stripe.checkout.Session.create")
    def test_checkout_keeps_reservation_metadata(self, mock_create):
        reservation = self.make_reservation(minutes_ago=5)
        mock_create.return_value = SimpleNamespace(id="cs_meta", url="https://checkout.stripe.com/meta")
        self.client.post(reverse("checkout_reservation", args=[reservation.pk]))
        kwargs = mock_create.call_args.kwargs
        self.assertEqual(kwargs["metadata"]["reservation_id"], str(reservation.pk))
        self.assertEqual(kwargs["client_reference_id"], str(reservation.pk))

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock", SITE_URL="http://testserver")
    @patch("bookings.views.stripe.checkout.Session.create")
    def test_checkout_preserves_fifteen_dollar_charge(self, mock_create):
        reservation = self.make_reservation(minutes_ago=5)
        mock_create.return_value = SimpleNamespace(id="cs_price", url="https://checkout.stripe.com/price")
        self.client.post(reverse("checkout_reservation", args=[reservation.pk]))
        price = mock_create.call_args.kwargs["line_items"][0]["price_data"]
        self.assertEqual(price["unit_amount"], 1500)
        self.assertEqual(price["currency"], "usd")

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock", SITE_URL="http://testserver")
    @patch("bookings.views.stripe.checkout.Session.create")
    def test_checkout_api_failure_creates_payment_review(self, mock_create):
        reservation = self.make_reservation(minutes_ago=5)
        mock_create.side_effect = stripe.APIConnectionError(message="Simulated interrupted response")
        self.client.post(reverse("checkout_reservation", args=[reservation.pk]))
        self.assertTrue(PaymentReview.objects.filter(
            reservation=reservation, reason="checkout_creation_api_error", resolved=False
        ).exists())

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock", SITE_URL="http://testserver")
    @patch("bookings.views.stripe.checkout.Session.create")
    def test_repeated_checkout_failures_do_not_duplicate_review(self, mock_create):
        reservation = self.make_reservation(minutes_ago=5)
        mock_create.side_effect = stripe.APIConnectionError(message="Simulated outage")
        url = reverse("checkout_reservation", args=[reservation.pk])
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(PaymentReview.objects.filter(
            reservation=reservation, reason="checkout_creation_api_error", resolved=False
        ).count(), 1)

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock", SITE_URL="http://testserver")
    @patch("bookings.views.stripe.checkout.Session.create")
    def test_retry_after_failure_attaches_session_and_clears_claim(self, mock_create):
        reservation = self.make_reservation(minutes_ago=5)
        mock_create.side_effect = [
            stripe.APIConnectionError(message="Simulated outage"),
            SimpleNamespace(id="cs_recovered", url="https://checkout.stripe.com/recovered"),
        ]
        url = reverse("checkout_reservation", args=[reservation.pk])
        self.client.post(url)
        response = self.client.post(url)
        reservation.refresh_from_db()
        self.assertEqual(response.url, "https://checkout.stripe.com/recovered")
        self.assertEqual(reservation.stripe_checkout_session_id, "cs_recovered")
        self.assertIsNone(reservation.checkout_creation_started_at)

class UncertainCheckoutReconciliationTests(BookingTestSetup):
    """Do not free lanes after an ambiguous Checkout creation response."""

    def mark_uncertain(self, reservation):
        return PaymentReview.objects.create(
            reservation=reservation,
            reason="checkout_creation_api_error",
            stripe_session_id="",
            details="Simulated interrupted response",
        )

    def test_unresolved_creation_error_prevents_expiration(self):
        reservation = self.make_reservation(minutes_ago=20)
        self.mark_uncertain(reservation)
        self.assertEqual(expire_unpaid_reservations(), (0, 1))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)

    def test_repeated_cleanup_keeps_uncertain_lane_reserved(self):
        reservation = self.make_reservation(minutes_ago=20)
        self.mark_uncertain(reservation)
        self.assertEqual(expire_unpaid_reservations(), (0, 1))
        self.assertEqual(expire_unpaid_reservations(), (0, 1))
        self.assertEqual(PaymentReview.objects.filter(
            reservation=reservation, reason="checkout_creation_api_error"
        ).count(), 1)

    def test_resolved_creation_error_allows_expiration(self):
        reservation = self.make_reservation(minutes_ago=20)
        review = self.mark_uncertain(reservation)
        review.resolved = True
        review.save(update_fields=["resolved"])
        self.assertEqual(expire_unpaid_reservations(), (1, 0))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.EXPIRED)

    def test_unrelated_review_does_not_prevent_expiration(self):
        reservation = self.make_reservation(minutes_ago=20)
        PaymentReview.objects.create(
            reservation=reservation, reason="other_reason",
            stripe_session_id="", resolved=False,
        )
        self.assertEqual(expire_unpaid_reservations(), (1, 0))

    def test_unresolved_creation_error_prevents_cancellation(self):
        reservation = self.make_reservation(minutes_ago=5)
        self.mark_uncertain(reservation)
        response = self.client.post(
            reverse("cancel_reservation", args=[reservation.pk])
        )
        self.assertRedirects(response, reverse("my_reservations"))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)

    def test_resolved_creation_error_allows_cancellation(self):
        reservation = self.make_reservation(minutes_ago=5)
        review = self.mark_uncertain(reservation)
        review.resolved = True
        review.save(update_fields=["resolved"])
        response = self.client.post(
            reverse("cancel_reservation", args=[reservation.pk])
        )
        self.assertRedirects(response, reverse("my_reservations"))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CANCELLED)

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock", SITE_URL="http://testserver")
    @patch("bookings.views.stripe.checkout.Session.create")
    def test_successful_retry_resolves_uncertain_review(self, mock_create):
        reservation = self.make_reservation(minutes_ago=5)
        review = self.mark_uncertain(reservation)
        mock_create.return_value = SimpleNamespace(
            id="cs_recovered_review",
            url="https://checkout.stripe.com/recovered-review",
        )
        response = self.client.post(
            reverse("checkout_reservation", args=[reservation.pk])
        )
        self.assertEqual(response.url, "https://checkout.stripe.com/recovered-review")
        review.refresh_from_db()
        reservation.refresh_from_db()
        self.assertTrue(review.resolved)
        self.assertEqual(reservation.stripe_checkout_session_id, "cs_recovered_review")

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock", SITE_URL="http://testserver")
    @patch("bookings.views.stripe.checkout.Session.create")
    def test_failed_retry_keeps_uncertain_review_open(self, mock_create):
        reservation = self.make_reservation(minutes_ago=5)
        review = self.mark_uncertain(reservation)
        mock_create.side_effect = stripe.APIConnectionError(
            message="Simulated repeat failure"
        )
        self.client.post(
            reverse("checkout_reservation", args=[reservation.pk])
        )
        review.refresh_from_db()
        self.assertFalse(review.resolved)
        self.assertEqual(PaymentReview.objects.filter(
            reservation=reservation, reason="checkout_creation_api_error",
            resolved=False,
        ).count(), 1)


class PaidSessionReleaseSafetyTests(BookingTestSetup):
    """Paid Stripe sessions cannot free lanes via cleanup or cancellation."""

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.expire")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_cleanup_paid_open_session_keeps_lane(self, retrieve, expire):
        reservation = self.make_reservation(session_id="cs_paid_open")
        retrieve.return_value = SimpleNamespace(status="open", payment_status="paid")
        self.assertEqual(expire_unpaid_reservations(), (0, 1))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        expire.assert_not_called()
        self.assertTrue(PaymentReview.objects.filter(
            reservation=reservation, reason="paid_session_unreconciled", resolved=False
        ).exists())

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.expire")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_cleanup_paid_expired_session_keeps_lane(self, retrieve, expire):
        reservation = self.make_reservation(session_id="cs_paid_expired")
        retrieve.return_value = SimpleNamespace(status="expired", payment_status="paid")
        self.assertEqual(expire_unpaid_reservations(), (0, 1))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        expire.assert_not_called()

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_cleanup_paid_complete_session_flags_review(self, retrieve):
        reservation = self.make_reservation(session_id="cs_paid_complete")
        retrieve.return_value = SimpleNamespace(status="complete", payment_status="paid")
        self.assertEqual(expire_unpaid_reservations(), (0, 1))
        self.assertTrue(PaymentReview.objects.filter(
            reservation=reservation, reason="paid_session_unreconciled", resolved=False
        ).exists())

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_cleanup_unpaid_expired_session_still_releases_lane(self, retrieve):
        reservation = self.make_reservation(session_id="cs_unpaid_expired")
        retrieve.return_value = SimpleNamespace(status="expired", payment_status="unpaid")
        self.assertEqual(expire_unpaid_reservations(), (1, 0))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.EXPIRED)

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.views.stripe.checkout.Session.expire")
    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_cancel_paid_open_session_keeps_lane(self, retrieve, expire):
        reservation = self.make_reservation(minutes_ago=5, session_id="cs_cancel_paid_open")
        retrieve.return_value = SimpleNamespace(status="open", payment_status="paid")
        self.client.post(reverse("cancel_reservation", args=[reservation.pk]))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        expire.assert_not_called()
        self.assertTrue(PaymentReview.objects.filter(
            reservation=reservation, reason="paid_session_unreconciled", resolved=False
        ).exists())

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.views.stripe.checkout.Session.expire")
    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_cancel_paid_expired_session_keeps_lane(self, retrieve, expire):
        reservation = self.make_reservation(minutes_ago=5, session_id="cs_cancel_paid_expired")
        retrieve.return_value = SimpleNamespace(status="expired", payment_status="paid")
        self.client.post(reverse("cancel_reservation", args=[reservation.pk]))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        expire.assert_not_called()

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.views.stripe.checkout.Session.expire")
    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_cancel_paid_complete_session_flags_review(self, retrieve, expire):
        reservation = self.make_reservation(minutes_ago=5, session_id="cs_cancel_paid_complete")
        retrieve.return_value = SimpleNamespace(status="complete", payment_status="paid")
        self.client.post(reverse("cancel_reservation", args=[reservation.pk]))
        self.assertTrue(PaymentReview.objects.filter(
            reservation=reservation, reason="paid_session_unreconciled", resolved=False
        ).exists())
        expire.assert_not_called()

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.views.stripe.checkout.Session.expire")
    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_cancel_unpaid_expired_session_still_cancels(self, retrieve, expire):
        reservation = self.make_reservation(minutes_ago=5, session_id="cs_cancel_unpaid_expired")
        retrieve.return_value = SimpleNamespace(status="expired", payment_status="unpaid")
        self.client.post(reverse("cancel_reservation", args=[reservation.pk]))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CANCELLED)
        expire.assert_not_called()


class PaymentExpirationRaceTests(BookingTestSetup):
    """Conditional SQLite updates must not overwrite intervening changes."""

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.expire")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_cleanup_does_not_overwrite_confirmation_during_retrieve(self, retrieve, expire):
        reservation = self.make_reservation(session_id="cs_race_cleanup_retrieve")

        def confirm_during_retrieve(_session_id):
            Reservation.objects.filter(pk=reservation.pk).update(status=Reservation.Status.CONFIRMED)
            return SimpleNamespace(status="open", payment_status="unpaid")

        retrieve.side_effect = confirm_during_retrieve
        expire.return_value = SimpleNamespace(status="expired", payment_status="unpaid")
        self.assertEqual(expire_unpaid_reservations(), (0, 0))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.expire")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_cleanup_does_not_overwrite_confirmation_during_expire(self, retrieve, expire):
        reservation = self.make_reservation(session_id="cs_race_cleanup_expire")
        retrieve.return_value = SimpleNamespace(status="open", payment_status="unpaid")
        expire.return_value = SimpleNamespace(status="expired", payment_status="unpaid")

        def confirm_during_expire(_session_id):
            Reservation.objects.filter(pk=reservation.pk).update(status=Reservation.Status.CONFIRMED)
            return SimpleNamespace(status="expired", payment_status="unpaid")

        expire.side_effect = confirm_during_expire
        self.assertEqual(expire_unpaid_reservations(), (0, 0))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        expire.assert_called_once_with("cs_race_cleanup_expire")

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.expire")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_cleanup_does_not_overwrite_cancellation_during_expire(self, retrieve, expire):
        reservation = self.make_reservation(session_id="cs_race_cleanup_cancel")
        retrieve.return_value = SimpleNamespace(status="open", payment_status="unpaid")
        expire.return_value = SimpleNamespace(status="expired", payment_status="unpaid")

        def cancel_during_expire(_session_id):
            Reservation.objects.filter(pk=reservation.pk).update(status=Reservation.Status.CANCELLED)
            return SimpleNamespace(status="expired", payment_status="unpaid")

        expire.side_effect = cancel_during_expire
        self.assertEqual(expire_unpaid_reservations(), (0, 0))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CANCELLED)

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.expire")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_cleanup_does_not_overwrite_new_creation_claim(self, retrieve, expire):
        reservation = self.make_reservation(session_id="cs_race_cleanup_claim")
        retrieve.return_value = SimpleNamespace(status="open", payment_status="unpaid")
        expire.return_value = SimpleNamespace(status="expired", payment_status="unpaid")
        claimed_at = timezone.now()

        def claim_during_expire(_session_id):
            Reservation.objects.filter(pk=reservation.pk).update(checkout_creation_started_at=claimed_at)
            return SimpleNamespace(status="expired", payment_status="unpaid")

        expire.side_effect = claim_during_expire
        self.assertEqual(expire_unpaid_reservations(), (0, 0))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertEqual(reservation.checkout_creation_started_at, claimed_at)

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.views.stripe.checkout.Session.expire")
    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_cancellation_does_not_overwrite_confirmation_during_retrieve(self, retrieve, expire):
        reservation = self.make_reservation(minutes_ago=5, session_id="cs_race_cancel_retrieve")

        def confirm_during_retrieve(_session_id):
            Reservation.objects.filter(pk=reservation.pk).update(status=Reservation.Status.CONFIRMED)
            return SimpleNamespace(status="open", payment_status="unpaid")

        retrieve.side_effect = confirm_during_retrieve
        self.client.post(reverse("cancel_reservation", args=[reservation.pk]))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.views.stripe.checkout.Session.expire")
    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_cancellation_does_not_overwrite_confirmation_during_expire(self, retrieve, expire):
        reservation = self.make_reservation(minutes_ago=5, session_id="cs_race_cancel_expire")
        retrieve.return_value = SimpleNamespace(status="open", payment_status="unpaid")
        expire.return_value = SimpleNamespace(status="expired", payment_status="unpaid")

        def confirm_during_expire(_session_id):
            Reservation.objects.filter(pk=reservation.pk).update(status=Reservation.Status.CONFIRMED)

        expire.side_effect = confirm_during_expire
        self.client.post(reverse("cancel_reservation", args=[reservation.pk]))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CONFIRMED)
        expire.assert_called_once_with("cs_race_cancel_expire")

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.views.stripe.checkout.Session.expire")
    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_cancellation_does_not_overwrite_new_creation_claim(self, retrieve, expire):
        reservation = self.make_reservation(minutes_ago=5, session_id="cs_race_cancel_claim")
        retrieve.return_value = SimpleNamespace(status="open", payment_status="unpaid")
        expire.return_value = SimpleNamespace(status="expired", payment_status="unpaid")
        claimed_at = timezone.now()

        def claim_during_expire(_session_id):
            Reservation.objects.filter(pk=reservation.pk).update(checkout_creation_started_at=claimed_at)

        expire.side_effect = claim_during_expire
        self.client.post(reverse("cancel_reservation", args=[reservation.pk]))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertEqual(reservation.checkout_creation_started_at, claimed_at)

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.views.stripe.checkout.Session.expire")
    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_cancellation_does_not_overwrite_changed_session_id(self, retrieve, expire):
        reservation = self.make_reservation(minutes_ago=5, session_id="cs_race_cancel_old")
        retrieve.return_value = SimpleNamespace(status="open", payment_status="unpaid")
        expire.return_value = SimpleNamespace(status="expired", payment_status="unpaid")

        def change_session_during_expire(_session_id):
            Reservation.objects.filter(pk=reservation.pk).update(stripe_checkout_session_id="cs_race_cancel_new")

        expire.side_effect = change_session_during_expire
        self.client.post(reverse("cancel_reservation", args=[reservation.pk]))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertEqual(reservation.stripe_checkout_session_id, "cs_race_cancel_new")


class StripeExpirationResponseSafetyTests(BookingTestSetup):
    """Focused safety fix: trust a verified expiration response, not stale retrieval."""

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.expire")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_cleanup_keeps_lane_when_expire_returns_paid(self, retrieve, expire):
        reservation = self.make_reservation(session_id="cs_expire_paid")
        retrieve.return_value = SimpleNamespace(status="open", payment_status="unpaid")
        expire.return_value = SimpleNamespace(status="complete", payment_status="paid")
        self.assertEqual(expire_unpaid_reservations(), (0, 1))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertTrue(PaymentReview.objects.filter(reservation=reservation, reason="paid_session_unreconciled", resolved=False).exists())

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.services.stripe.checkout.Session.expire")
    @patch("bookings.services.stripe.checkout.Session.retrieve")
    def test_cleanup_keeps_lane_when_expire_response_is_incomplete(self, retrieve, expire):
        reservation = self.make_reservation(session_id="cs_expire_unknown")
        retrieve.return_value = SimpleNamespace(status="open", payment_status="unpaid")
        expire.return_value = SimpleNamespace(status="expired")
        self.assertEqual(expire_unpaid_reservations(), (0, 1))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertTrue(PaymentReview.objects.filter(reservation=reservation, reason="checkout_expiration_unverified", resolved=False).exists())

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.views.stripe.checkout.Session.expire")
    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_cancellation_keeps_lane_when_expire_returns_paid(self, retrieve, expire):
        reservation = self.make_reservation(minutes_ago=5, session_id="cs_cancel_race_paid")
        retrieve.return_value = SimpleNamespace(status="open", payment_status="unpaid")
        expire.return_value = SimpleNamespace(status="complete", payment_status="paid")
        self.client.post(reverse("cancel_reservation", args=[reservation.pk]))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.PENDING)
        self.assertTrue(PaymentReview.objects.filter(reservation=reservation, reason="paid_session_unreconciled", resolved=False).exists())

    @override_settings(STRIPE_SECRET_KEY="sk_test_mock")
    @patch("bookings.views.stripe.checkout.Session.expire")
    @patch("bookings.views.stripe.checkout.Session.retrieve")
    def test_cancellation_allows_verified_unpaid_expiration(self, retrieve, expire):
        reservation = self.make_reservation(minutes_ago=5, session_id="cs_cancel_verified")
        retrieve.return_value = SimpleNamespace(status="open", payment_status="unpaid")
        expire.return_value = SimpleNamespace(status="expired", payment_status="unpaid")
        self.client.post(reverse("cancel_reservation", args=[reservation.pk]))
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, Reservation.Status.CANCELLED)

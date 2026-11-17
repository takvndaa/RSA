
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Lane, Reservation, TimeSlot


User = get_user_model()


class BookingTests(TestCase):

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

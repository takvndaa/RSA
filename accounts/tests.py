from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class MemberAccountTests(TestCase):

    def setUp(self):
        self.registration_data = {
            "username": "swimmer1",
            "first_name": "Alex",
            "last_name": "Rivera",
            "email": "alex@example.com",
            "phone_number": "5551234567",
            "address": "123 Main Street",
            "password1": "SecureSwimPass123!",
            "password2": "SecureSwimPass123!",
        }

    def test_registration_page_loads(self):
        response = self.client.get(reverse("register"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Join DIIVE")

    def test_member_can_register(self):
        response = self.client.post(
            reverse("register"),
            self.registration_data,
        )

        self.assertRedirects(response, reverse("login"))

        member = User.objects.get(username="swimmer1")

        self.assertEqual(member.email, "alex@example.com")
        self.assertEqual(member.first_name, "Alex")
        self.assertEqual(member.last_name, "Rivera")
        self.assertEqual(
            member.approval_status,
            User.ApprovalStatus.PENDING,
        )

    def test_password_is_hashed(self):
        self.client.post(
            reverse("register"),
            self.registration_data,
        )

        member = User.objects.get(username="swimmer1")

        self.assertNotEqual(
            member.password,
            self.registration_data["password1"],
        )
        self.assertTrue(
            member.check_password(
                self.registration_data["password1"]
            )
        )

    def test_duplicate_email_is_rejected(self):
        self.client.post(
            reverse("register"),
            self.registration_data,
        )

        duplicate = self.registration_data.copy()
        duplicate["username"] = "swimmer2"
        duplicate["email"] = "ALEX@example.com"

        response = self.client.post(
            reverse("register"),
            duplicate,
        )

        self.assertEqual(User.objects.count(), 1)
        self.assertContains(
            response,
            "An account with this email already exists.",
        )

    def test_member_can_log_in(self):
        member = User.objects.create_user(
            username="swimmer1",
            password="SecureSwimPass123!",
            approval_status=User.ApprovalStatus.APPROVED,
        )

        response = self.client.post(
            reverse("login"),
            {
                "username": "swimmer1",
                "password": "SecureSwimPass123!",
            },
        )

        self.assertRedirects(
            response,
            reverse("member_dashboard"),
        )

    def test_dashboard_requires_login(self):
        response = self.client.get(
            reverse("member_dashboard")
        )

        self.assertRedirects(
            response,
            f"{reverse('login')}?next={reverse('member_dashboard')}",
        )

    def test_pending_member_sees_approval_message(self):
        member = User.objects.create_user(
            username="swimmer1",
            password="SecureSwimPass123!",
        )

        self.client.force_login(member)

        response = self.client.get(
            reverse("member_dashboard")
        )

        self.assertContains(
            response,
            "awaiting administrator approval",
        )

    def test_approved_member_sees_approved_message(self):
        member = User.objects.create_user(
            username="swimmer1",
            password="SecureSwimPass123!",
            approval_status=User.ApprovalStatus.APPROVED,
        )

        self.client.force_login(member)

        response = self.client.get(
            reverse("member_dashboard")
        )

        self.assertContains(
            response,
            "Your membership is approved.",
        )

    def test_admin_can_approve_member(self):
        admin_user = User.objects.create_superuser(
            username="adminuser",
            email="admin@example.com",
            password="SecureAdminPass123!",
        )

        member = User.objects.create_user(
            username="swimmer1",
            password="SecureSwimPass123!",
        )

        self.client.force_login(admin_user)

        response = self.client.post(
            reverse("admin:accounts_user_changelist"),
            {
                "action": "approve_members",
                "_selected_action": [member.pk],
            },
        )

        self.assertEqual(response.status_code, 302)

        member.refresh_from_db()

        self.assertEqual(
            member.approval_status,
            User.ApprovalStatus.APPROVED,
        )
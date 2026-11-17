from django.test import TestCase
from django.urls import reverse


class HomePageTests(TestCase):

    def test_homepage_returns_successful_response(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)

    def test_homepage_displays_diive(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, "DIIVE")
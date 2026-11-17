from django.urls import path
from . import views

urlpatterns = [
    path("", views.availability, name="availability"),
    path("reserve/", views.create_reservation, name="create_reservation"),
    path("mine/", views.my_reservations, name="my_reservations"),
    path(
        "cancel/<int:reservation_id>/",
        views.cancel_reservation,
        name="cancel_reservation",
    ),
]
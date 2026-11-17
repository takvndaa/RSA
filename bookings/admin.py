from django.contrib import admin

from .models import Lane, TimeSlot, Reservation


@admin.register(Lane)
class LaneAdmin(admin.ModelAdmin):
    list_display = ("number", "is_active")
    list_editable = ("is_active",)


@admin.register(TimeSlot)
class TimeSlotAdmin(admin.ModelAdmin):
    list_display = ("start_time",)
    ordering = ("start_time",)


@admin.register(Reservation)
class ReservationAdmin(admin.ModelAdmin):
    list_display = (
        "member",
        "lane",
        "timeslot",
        "status",
        "created_at",
    )

    list_filter = ("status", "lane")
    search_fields = ("member__username", "member__email")
    readonly_fields = ("created_at",)
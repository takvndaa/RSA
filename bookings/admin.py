from django.contrib import admin

from .models import CancellationRequest, Lane, PaymentReview, Reservation, TimeSlot


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
        "id", "member", "lane", "timeslot", "status", "created_at",
    )
    list_filter = ("status", "lane")
    search_fields = ("member__username", "member__email", "stripe_checkout_session_id")
    readonly_fields = (
        "member", "lane", "timeslot", "status", "created_at",
        "stripe_checkout_session_id", "checkout_creation_started_at",
    )
    list_select_related = ("member", "lane", "timeslot")
    actions = None

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PaymentReview)
class PaymentReviewAdmin(admin.ModelAdmin):
    list_display = (
        "id", "reservation", "reason", "stripe_session_id",
        "resolved", "created_at",
    )
    list_filter = ("resolved", "reason", "created_at")
    search_fields = (
        "reservation__id", "reservation__member__username",
        "reservation__member__email", "stripe_session_id", "reason",
    )
    readonly_fields = (
        "reservation", "reason", "stripe_session_id", "details",
        "resolved", "created_at",
    )
    list_select_related = ("reservation", "reservation__member")
    actions = None

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CancellationRequest)
class CancellationRequestAdmin(admin.ModelAdmin):
    """Inspection-only queue; refund/cancellation actions require a verified workflow."""

    list_display = (
        "id", "reservation", "requested_at", "eligible_for_full_refund",
        "decision", "refund_status", "reviewed_by", "completed_at",
    )
    list_filter = (
        "decision", "eligible_for_full_refund", "refund_status", "requested_at",
    )
    search_fields = (
        "=reservation__id", "reservation__member__username",
        "reservation__member__email", "stripe_refund_id",
        "reservation__stripe_checkout_session_id",
    )
    readonly_fields = (
        "reservation", "requested_at", "eligible_for_full_refund",
        "member_reason", "decision", "reviewed_by", "reviewed_at",
        "staff_notes", "refund_status", "stripe_refund_id", "completed_at",
    )
    list_select_related = ("reservation", "reservation__member", "reviewed_by")
    ordering = ("-requested_at",)
    actions = None

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

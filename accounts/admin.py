from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User


@admin.register(User)
class MemberAdmin(UserAdmin):
    list_display = (
        "username",
        "email",
        "first_name",
        "last_name",
        "approval_status",
        "is_staff",
    )

    list_filter = (
        "approval_status",
        "is_staff",
        "is_superuser",
    )

    actions = ["approve_members", "reject_members"]

    fieldsets = UserAdmin.fieldsets + (
        (
            "DIIVE Membership",
            {
                "fields": (
                    "phone_number",
                    "address",
                    "approval_status",
                )
            },
        ),
    )

    @admin.action(description="Approve selected members")
    def approve_members(self, request, queryset):
        queryset.filter(is_superuser=False).update(
            approval_status=User.ApprovalStatus.APPROVED
        )

    @admin.action(description="Reject selected members")
    def reject_members(self, request, queryset):
        queryset.filter(is_superuser=False).update(
            approval_status=User.ApprovalStatus.REJECTED
        )
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect

from .forms import MemberRegistrationForm


def register(request):
    if request.user.is_authenticated:
        return redirect("member_dashboard")

    if request.method == "POST":
        form = MemberRegistrationForm(request.POST)

        if form.is_valid():
            form.save()
            messages.success(
                request,
                "Registration successful! Your account is awaiting approval.",
            )
            return redirect("login")
    else:
        form = MemberRegistrationForm()

    return render(request, "accounts/register.html", {"form": form})


@login_required
def member_dashboard(request):
    return render(request, "accounts/dashboard.html")
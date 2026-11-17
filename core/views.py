from django.shortcuts import render


def home(request):
    """Display the DIIVE homepage."""
    return render(request, "core/home.html")
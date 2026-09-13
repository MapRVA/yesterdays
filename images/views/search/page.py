from django.shortcuts import render


def search_page(request):
    """Display the search interface."""
    return render(request, "images/search.html", {})

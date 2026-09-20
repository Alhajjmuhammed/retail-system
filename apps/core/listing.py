"""
Paging a list the same way everywhere.

Lists used to be cut at an arbitrary 100 or 200 with nothing on the page to
say so. `paginate` returns the page and the query string to keep filters
across pages, for templates/partials/pagination.html.
"""

from django.core.paginator import Paginator

PER_PAGE = 50


def paginate(request, rows, per_page=PER_PAGE):
    page = Paginator(rows, per_page).get_page(request.GET.get("page"))
    keep = request.GET.copy()
    keep.pop("page", None)
    return {"page": page, "keep": keep.urlencode()}


def modal_or_page(request, template, context, *, title="", back=""):
    """
    A form in the modal when opened by HTMX, or as its own page otherwise.
    """
    from django.shortcuts import render

    context = {**context, "modal": bool(getattr(request, "htmx", False))}
    if context["modal"]:
        return render(request, template, context)
    return render(request, "partials/form_page.html",
                  {**context, "inner": template, "title": title, "back": back})


def close_modal(request, url):
    """After a modal form saves: close it and load `url` underneath."""
    from django.http import HttpResponse
    from django.shortcuts import redirect

    if getattr(request, "htmx", False):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)

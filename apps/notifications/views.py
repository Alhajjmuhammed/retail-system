"""What the shop has sent, and what it says."""

from django.contrib import messages as django_messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from apps.core.decorators import requires
from apps.notifications.models import Message, MessageStatus, MessageTemplate


@login_required
@requires("settings.edit")
def message_log(request):
    rows = Message.objects.all()
    from apps.core.listing import paginate

    status = request.GET.get("status", "")
    if status not in MessageStatus.values:
        status = ""
    if status:
        rows = rows.filter(status=status)
    listing = paginate(request, rows)
    page = listing["page"]
    return render(
        request,
        "notifications/messages.html",
        {
            "page": page,
            "keep": listing["keep"],
            "status": status,
            "statuses": MessageStatus.choices,
            "templates": MessageTemplate.objects.all(),
            "queued": Message.objects.filter(status=MessageStatus.QUEUED).count(),
        },
    )


@login_required
@requires("settings.edit")
def template_edit(request, pk):
    """
    What the shop actually says.

    Their own words, in their own language -- the default English is a
    starting point, not the product.
    """
    template = get_object_or_404(MessageTemplate, pk=pk)

    if request.method == "POST":
        # Cut to what the columns hold: a long paste was a 500.
        template.name = request.POST.get("name", template.name).strip()[:80] or template.name
        template.body = request.POST.get("body", template.body)[:2000]
        template.is_active = request.POST.get("is_active") == "on"
        template.save()
        django_messages.success(request, f"{template.name} saved.")
        return redirect("notifications:message_log")

    return render(
        request, "notifications/template_form.html", {"template": template}
    )

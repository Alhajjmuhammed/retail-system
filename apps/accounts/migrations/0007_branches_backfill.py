"""
Carry existing meaning across to the explicit flag, and lower-case emails.

Somebody with branch links worked only there; somebody without any worked
everywhere. Emails are lower-cased unless that would collide with another
account, which is left for a person to sort out.
"""

from django.db import migrations
from django.db.models import Exists, OuterRef


def forwards(apps, schema_editor):
    Membership = apps.get_model("accounts", "Membership")
    MembershipBranch = apps.get_model("accounts", "MembershipBranch")
    User = apps.get_model("accounts", "User")

    linked = MembershipBranch.objects.filter(membership=OuterRef("pk"))
    Membership.objects.annotate(has=Exists(linked)).filter(has=True).update(all_branches=False)

    for user in User.objects.all():
        lower = user.email.strip().lower()
        if lower != user.email and not User.objects.filter(email=lower).exclude(pk=user.pk).exists():
            user.email = lower
            user.save(update_fields=["email"])


class Migration(migrations.Migration):
    dependencies = [("accounts", "0006_branches_explicit")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

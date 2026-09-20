"""
Move receipts already uploaded out of the public media folder.

Earlier ones sat at expenses/<original file name> under MEDIA_ROOT, served to
anyone who guessed the name. Each is copied into private storage under an
unguessable name and the public copy removed.
"""

import os
import uuid

from django.db import migrations


def forwards(apps, schema_editor):
    from django.core.files.storage import FileSystemStorage

    from apps.core.storage import PrivateStorage

    public, private = FileSystemStorage(), PrivateStorage()
    Expense = apps.get_model("finance", "Expense")
    for expense in Expense.objects.exclude(attachment="").iterator():
        name = expense.attachment.name
        if private.exists(name) or not public.exists(name):
            continue
        ext = os.path.splitext(name)[1].lower()[:6]
        new = f"expenses/{expense.tenant_id}/{uuid.uuid4().hex}{ext}"
        with public.open(name, "rb") as handle:
            saved = private.save(new, handle)
        Expense.objects.filter(pk=expense.pk).update(attachment=saved)
        public.delete(name)


class Migration(migrations.Migration):
    # Files are moved one at a time and cannot be rolled back with the
    # transaction: a failure halfway used to leave rows pointing at files
    # that were already gone.
    atomic = False

    dependencies = [("finance", "0004_receipts_private_storage")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

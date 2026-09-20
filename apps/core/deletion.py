"""
Deleting things that have history.

A retail system cannot simply delete a product that has been sold or a
supplier that has been paid: the sale line points at it, and a report from
last year has to keep meaning what it meant. But a shop that typed a category
twice must be able to remove one.

So there is one rule everywhere: delete when nothing references it, archive
when something does, and say which happened.
"""

from dataclasses import dataclass

from django.db.models import ProtectedError


@dataclass(frozen=True)
class Outcome:
    deleted: bool
    archived: bool
    message: str
    blocked: bool = False

    def __bool__(self) -> bool:
        return not self.blocked


def remove_or_archive(obj, *, label=None, archive_field="is_active", blockers=()):
    """
    Remove ``obj`` if it is unused, otherwise switch it off.

    ``blockers`` are (callable, message) pairs checked first: things that must
    stop the operation outright rather than fall back to archiving, like
    removing the last owner of a shop.
    """
    label = label or str(obj)

    for check, message in blockers:
        if check():
            return Outcome(False, False, message, blocked=True)

    try:
        obj.delete()
    except ProtectedError:
        pass
    else:
        return Outcome(True, False, f"{label} deleted.")

    if hasattr(obj, archive_field):
        setattr(obj, archive_field, False)
        obj.save(update_fields=[archive_field, "updated_at"])
        return Outcome(
            False, True,
            f"{label} is used by existing records, so it has been switched off "
            "rather than deleted. Your history is unchanged.",
        )

    return Outcome(
        False, False,
        f"{label} is used by existing records and cannot be removed.",
        blocked=True,
    )

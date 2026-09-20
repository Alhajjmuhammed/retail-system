"""
Every permission a shop can tick must do something.

A checkbox in the role builder that nothing on the server checks is worse
than none: the owner believes they have restricted something they have not.
"""

import pathlib
import re

import pytest
from django.urls import reverse

from apps.core.permissions import registry

pytestmark = pytest.mark.django_db

NOT_YET_ENFORCED: set = set()

APPS = pathlib.Path(__file__).resolve().parent.parent / "apps"


def _server_side_references():
    found = set()
    literal = re.compile(r'''['"]([a-z_]+\.[a-z_]+)['"]''')
    for path in APPS.rglob("*.py"):
        if "migrations" in path.parts or path.name == "permissions.py":
            continue
        found |= set(literal.findall(path.read_text()))
    return found


def test_every_permission_is_checked_somewhere_on_the_server():
    unchecked = registry.codes() - _server_side_references() - NOT_YET_ENFORCED
    assert not unchecked, f"Permissions nothing enforces: {sorted(unchecked)}"


def test_the_catalogue_in_the_database_matches_the_code(client, db):
    from apps.accounts.models import Permission, PlatformRole, User

    assert set(Permission.objects.values_list("code", flat=True)) == registry.codes()
    boss = User.objects.create_user("b@p.test", "pw", name="B", is_platform_staff=True,
                                    platform_role=PlatformRole.objects.get(name="Super admin"))
    client.force_login(boss)
    r = client.get(reverse("platform:permissions"))
    assert r.status_code == 200 and not r.context["missing"] and not r.context["stale"]

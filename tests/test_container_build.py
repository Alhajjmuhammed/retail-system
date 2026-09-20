"""
The image has to be buildable.

`collectstatic` runs during the build and imports production settings, which
refuse to load on development defaults. The build passed only a SECRET_KEY,
and a short one at that, so the moment those guards were added the image
stopped building -- and nothing noticed, because nobody had built it. This
reads the Dockerfile and puts the values it uses through the same guard.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from config.settings.guards import check_production_config

DOCKERFILE = Path(settings.BASE_DIR) / "Dockerfile"


@pytest.fixture(scope="module")
def build_env():
    """The environment the Dockerfile gives collectstatic."""
    text = DOCKERFILE.read_text()
    run = re.search(r"RUN ((?:[^\n]*\\\n)*[^\n]*collectstatic[^\n]*)", text)
    assert run, "the Dockerfile no longer runs collectstatic during the build"
    line = run.group(1).replace("\\\n", " ")
    return dict(re.findall(r"([A-Z_]+)=(\S+)", line))


def test_the_build_can_import_production_settings(build_env):
    """Whatever the build passes must satisfy the guards, or the image fails."""
    check_production_config(
        build_env.get("SECRET_KEY", ""),
        [h for h in build_env.get("ALLOWED_HOSTS", "").split(",") if h],
        build_env.get("EMAIL_HOST", ""),
    )


def test_the_build_values_are_obviously_not_real(build_env):
    """A placeholder that looks like a real secret invites being reused."""
    assert "placeholder" in build_env.get("SECRET_KEY", "").lower()
    assert build_env.get("ALLOWED_HOSTS", "").endswith(".invalid")


def test_a_container_without_real_settings_still_refuses_to_start():
    """The build-time values must not be baked in as defaults."""
    text = DOCKERFILE.read_text()
    env_lines = re.findall(r"^ENV\s+(.+)$", text, re.MULTILINE)
    baked = " ".join(env_lines)
    for name in ("SECRET_KEY", "ALLOWED_HOSTS", "EMAIL_HOST", "DATABASE_URL"):
        assert name not in baked, f"{name} must come from the environment, not the image"

    with pytest.raises(ImproperlyConfigured):
        check_production_config("", [], "")

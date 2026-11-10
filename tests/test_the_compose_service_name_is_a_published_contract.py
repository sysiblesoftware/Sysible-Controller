"""This project's compose service name is written down in another repository.

SLOP's Administration · Software & services screen gives every product a
Services disclosure, and every card behind it is keyed by a COMPOSE SERVICE
NAME. That is what lets the updater turn a card's Restart button into
`docker compose restart <service>` with no part of the argv coming from the
request — and what lets one `compose ps` feed every card's state.

Which means this project's service name is written down in two places that
cannot import each other:

  * docker-compose.yml    — the service that actually exists
  * slop:updater/backend/apps.py — MODULES, and idp/app.py's _SKELETON_MODULES

A rename here is SILENT over there. The card keeps rendering, its state
collapses to "No container", and its buttons start refusing — on a screen whose
whole job is to tell an operator what is running.

The other half of this pair is in the SLOP repository
(tests/test_module_sections_name_real_services.py), which reads this file
wherever it can find a checkout of this project. This half is the one that fails
in the repository where the rename would actually be made.

If this project really does gain or rename a service, change BOTH.

Run: pytest tests/
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE = os.path.join(REPO, "docker-compose.yml")

# The name SLOP's Software & services screen keys this product's card by.
EXPECTED = {"controller"}


def _services() -> set[str]:
    """Top-level service names, read without a YAML dependency: the services:
    block's two-space keys, stopping at the next top-level key."""
    text = open(COMPOSE, encoding="utf-8").read()
    body = text.split("\nservices:", 1)[1]
    body = re.split(r"\n(?=[a-zA-Z])", body)[0]
    return set(re.findall(r"^  ([a-z0-9][a-z0-9_-]*):", body, re.M))


def test_the_compose_file_is_where_this_test_thinks_it_is():
    """A moved compose file would make every assertion below vacuous."""
    assert os.path.isfile(COMPOSE), (
        f"{COMPOSE} is gone. If the compose file moved, this test and SLOP's "
        f"updater/backend/apps.py both need to know where to.")


def test_the_service_name_has_not_changed_under_slop():
    got = _services()
    assert got == EXPECTED, (
        f"The Controller declares {sorted(got)}; SLOP's Software & services screen "
        f"keys its card by {sorted(EXPECTED)}. Rename it in SLOP too — in "
        f"updater/backend/apps.py (MODULES) and idp/app.py "
        f"(_SKELETON_MODULES) — or the card renders with no state and every "
        f"button on it refuses."
    )

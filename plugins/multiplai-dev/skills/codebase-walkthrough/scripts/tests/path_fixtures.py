"""Git fixture repos whose entry points `code_facts.map_paths` discovers, and the trace plans that reach the module.

Each repo is committed with `fixture_repo.make_repo` (fixed author and dates,
no global or system git config), so shas are stable and citations re-read
through `git show` as in production.

    shop/   click commands in `cli/main.py`, each calling `billing.core.charge`; the module is `billing/`
    kit/    `skills/t/SKILL.md` runs `python -m tool`; the module is the skill directory `skills/t/`
    site/   a Django app `shop/` with a webhook route, a route `mobile/` calls over HTTP, a beat task,
            and a click command outside the module; `mobile/` is the other repo
"""

from __future__ import annotations

from pathlib import Path

import fixture_repo

CLICK_NAMES = ["sync"]

BILLING_CORE = "RATE = 2\n\n\ndef charge(amount):\n    return amount * RATE\n"


def click_source(names: list[str]) -> str:
    out = ["import click", "", "from billing.core import charge", ""]
    for i, n in enumerate(names, start=1):
        out += ["", "@click.command()", f"def {n}():", f"    return charge({i})", ""]
    return "\n".join(out)


def click_repo(ws: Path, names: list[str] = CLICK_NAMES) -> Path:
    """`ws/shop`, a repo with one standalone click command per name; returns the module directory."""
    repo = ws / "shop"
    fixture_repo.make_repo(
        repo,
        {
            "README.md": "# shop\n",
            "cli/__init__.py": "",
            "cli/main.py": click_source(names),
            "billing/__init__.py": "",  # zero lines: never a changed-lines key
            "billing/core.py": BILLING_CORE,
        },
    )
    return repo / "billing"


def click_plan(names: list[str] = CLICK_NAMES) -> dict[str, list]:
    """Each command's trace: its handler, then `charge` in the module."""
    return {n: [("billing/core.py", "charge")] for n in names}


SKILL_MD = "# t\n\nRun the tool.\n\n```bash\npython -m tool\n```\n"
TOOL_MAIN = "from tool.core import charge\n\ncharge(1)\n"
SKILL_NAME = "t: tool"


def skill_repo(ws: Path) -> Path:
    """`ws/kit`, whose SKILL.md runs `python -m tool`; returns the module directory (the skill)."""
    repo = ws / "kit"
    fixture_repo.make_repo(
        repo,
        {
            "skills/t/SKILL.md": SKILL_MD,
            "skills/t/tool/__init__.py": "",
            "skills/t/tool/__main__.py": TOOL_MAIN,
            "skills/t/tool/core.py": "def charge(amount):\n    return amount * 2\n",
        },
    )
    return repo / "skills" / "t"


def skill_plan() -> dict[str, list]:
    """The skill command's trace: the SKILL.md line, then `tool/__main__.py` over the launcher edge."""
    return {SKILL_NAME: [("skills/t/tool/__main__.py", 1, 3, "tool", "charge(1)")]}


SHOP_CORE = (
    "def charge(amount):\n    total = amount * 2\n    total += 1\n    return total\n"
)
SHOP_VIEWS = (
    "from shop.core import charge\n"
    "\n"
    "\n"
    "def pay_hook(request):\n"
    "    return charge(1)\n"
    "\n"
    "\n"
    "def status(request):\n"
    "    return 'ok'\n"
)
SHOP_URLS = (
    "from django.urls import path\n"
    "\n"
    "from shop import views\n"
    "\n"
    "urlpatterns = [\n"
    "    path('hooks/pay/', views.pay_hook, name='pay_webhook'),\n"
    "    path('api/status/', views.status, name='status'),\n"
    "]\n"
)
SHOP_TASKS = (
    "from celery import shared_task\n"
    "\n"
    "from shop.core import charge\n"
    "\n"
    "\n"
    "@shared_task(name='shop.sweep')\n"
    "def sweep():\n"
    "    return charge(2)\n"
)
SETTINGS = (
    "CELERY_BEAT_SCHEDULE = {\n"
    "    'sweep': {\n"
    "        'task': 'shop.sweep',\n"
    "        'schedule': 60,\n"
    "    },\n"
    "}\n"
)
SITE_CLI = "import click\n\nfrom shop.core import charge\n\n\n@click.command()\ndef settle():\n    return charge(3)\n"
MOBILE_API = "import httpx\n\n\ndef ping():\n    return httpx.get('/api/status/')\n"

WEBHOOK = "HTTP /hooks/pay/"
CALLED = "mobile: HTTP /api/status/"
BEAT = "shop.sweep (beat)"


def web_repo(ws: Path, *, cli: bool = True) -> Path:
    """`ws/site` (the Django app `shop/`, plus a click command when `cli`) and `ws/mobile`; returns `shop/`."""
    files = {
        "manage.py": "import os\n",
        "project/__init__.py": "",
        "project/settings.py": SETTINGS,
        "project/urls.py": "from django.urls import include, path\n\nurlpatterns = [\n"
        "    path('', include('shop.urls')),\n]\n",
        "shop/__init__.py": "",
        "shop/core.py": SHOP_CORE,
        "shop/views.py": SHOP_VIEWS,
        "shop/urls.py": SHOP_URLS,
        "shop/tasks.py": SHOP_TASKS,
    }
    if cli:
        files |= {"cli/__init__.py": "", "cli/main.py": SITE_CLI}
    fixture_repo.make_repo(ws / "site", files)
    fixture_repo.make_repo(ws / "mobile", {"api.py": MOBILE_API})
    return ws / "site" / "shop"


def web_plan() -> dict[str, list]:
    """The seeds' traces into `charge`, and the click command's, whose hop 2 spans all of `charge`."""
    return {
        WEBHOOK: [("shop/core.py", "charge")],
        CALLED: [],
        BEAT: [("shop/core.py", "charge")],
        "settle": [("shop/core.py", 1, 4, "charge", "def charge(amount):")],
    }


def run_args(module: Path, ws: Path, tmp_path: Path, *extra: str) -> list[str]:
    """`run` on `module`, searching the repos under `ws`, with no docs and no LSP."""
    return [
        "run",
        str(module),
        "--trust-repo",
        "--runs-dir",
        str(tmp_path / "runs"),
        "--output",
        str(tmp_path / "out"),
        "--search-root",
        str(ws),
        "--lsp-timeout",
        "0",
        *extra,
    ]

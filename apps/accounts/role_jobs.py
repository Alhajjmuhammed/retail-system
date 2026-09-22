"""
Roles in the words a shopkeeper would use.

The permission catalogue has forty-six entries in eight modules, and each
one can carry a money ceiling, a percentage or a list of allowed options.
That is the right machinery -- it is what lets a shop say "Juma may refund,
but only up to 20,000" -- and it is the wrong first question to put to
somebody who employs three people.

So this is a second way of saying the same thing: eight jobs a person might
do, each standing for a handful of permissions. Turning one on grants them;
turning it off takes them away. The full catalogue is still there underneath
and still wins -- a role built by hand simply reads as "set by hand" here,
and nothing this file does can silently change it.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Job:
    key: str
    label: str
    text: str
    # What only this job has. All of it held means the person does this job;
    # none of it means they do not.
    core: tuple[str, ...]
    # What comes with it. Shared between jobs -- "see the product list" is
    # needed by four of them -- so it can never say which job is on, or a
    # cashier would read as half a buyer.
    also: tuple[str, ...] = ()
    # One permission in the job may carry a number the shop sets, which is
    # the whole point of "can take money off -- up to 5%".
    limit_on: str | None = None
    limit_label: str = ""

    @property
    def grants(self) -> tuple[str, ...]:
        return self.core + self.also


JOBS = [
    Job(
        "sell", "Sells at the till",
        "Open a till, ring up sales, print a receipt again.",
        core=("pos.operate", "pos.sell"),
        also=("pos.reprint", "product.view", "cashup.perform", "pos.open_item"),
    ),
    Job(
        "discount", "Can take money off a price",
        "Give a discount at the till.",
        core=("pos.discount",),
        also=("pos.price_override",),
        limit_on="pos.discount", limit_label="Most they may take off (%)",
    ),
    Job(
        "undo", "Can undo a sale",
        "Refund a customer, or void a sale rung up by mistake.",
        core=("pos.refund",),
        also=("pos.void",),
        limit_on="pos.refund", limit_label="Biggest refund they may give",
    ),
    Job(
        "phone", "Sells from a phone",
        "Build a basket and take payment away from the counter.",
        core=("pos.mobile_cart", "pos.mobile_payment"),
        also=("pos.mobile_methods", "product.view"),
        limit_on="pos.mobile_payment", limit_label="Biggest sale they may take",
    ),
    Job(
        "account", "Can let a customer take goods on account",
        "Sell on credit, and record the money when they pay.",
        core=("credit.grant",),
        also=("credit.collect", "customer.manage"),
        limit_on="credit.grant", limit_label="Most a customer may owe",
    ),
    Job(
        "stock", "Looks after stock",
        "Receive deliveries, count the shelves, move stock between branches.",
        core=("stock.receive", "stock.count"),
        also=("stock.view", "stock.transfer", "stock.batches", "stock.adjust",
              "stock.wastage", "product.view"),
        limit_on="stock.adjust", limit_label="Biggest stock correction",
    ),
    Job(
        "buying", "Buys from suppliers",
        "Raise orders, manage suppliers, record what has been paid.",
        core=("po.manage", "supplier.manage"),
        also=("product.view", "po.approve", "supplier.pay"),
        limit_on="po.approve", limit_label="Biggest order they may approve",
    ),
    Job(
        "money", "Sees the money",
        "Takings, profit, what each person sold, and approving a short drawer.",
        core=("report.sales", "report.margin"),
        also=("report.stock", "report.staff", "report.export", "product.view_cost",
              "cashup.approve", "expense.create", "expense.approve",
              "fiscal.manage", "cash.movement"),
    ),
    Job(
        "admin", "Runs the shop's settings",
        "Staff, roles, branches, tills and business settings.",
        core=("user.manage", "settings.edit"),
        also=("role.manage", "branch.manage", "register.manage", "product.manage",
              "product.set_price", "billing.manage"),
    ),
]

BY_KEY = {job.key: job for job in JOBS}

# Every permission any job speaks for.
SPOKEN_FOR = {code for job in JOBS for code in job.grants}


def read(granted: dict) -> dict:
    """
    Which jobs a role is doing, read back from the permissions it holds.

    ``granted`` maps permission code to its value (None for a plain grant).
    A job is "on" when every permission it stands for is held, "some" when
    only a few are, and off when none are. "some" is shown as set by hand,
    never quietly completed.
    """
    out = {}
    for job in JOBS:
        held = [code for code in job.core if code in granted]
        if len(held) == len(job.core):
            state = "on"
        elif held:
            state = "some"
        else:
            state = "off"
        out[job.key] = {
            "job": job, "state": state,
            "limit": granted.get(job.limit_on) if job.limit_on else None,
        }
    return out


def unspoken(granted: dict) -> list:
    """Permissions this role holds that no job on this page speaks for."""
    return sorted(code for code in granted if code not in SPOKEN_FOR)


def expand(post, available=None) -> dict:
    """
    Turn the switches back into permissions.

    Returns what the save path already understands: ``{code: {"limit": str,
    "options": [...]}}`` for everything the switched-on jobs stand for. A job
    that is off simply is not in it, and what is not in it is taken away --
    which is the whole contract of this page.

    ``available`` narrows it to permissions the plan includes, so turning on
    "Looks after stock" on a plan without batches does not quietly claim it.
    """
    from apps.core.permissions import ValueType, registry

    out = {}
    for job in JOBS:
        if post.get(f"job:{job.key}") != "on":
            continue
        for code in job.grants:
            if available is not None and code not in available:
                continue
            spec = registry.get(code)
            entry = {"limit": "", "options": []}
            # A permission that is a list of allowed options means nothing
            # granted with an empty list: the phone seller who could fill a
            # basket and then take no payment for it was exactly this.
            if spec and spec.value_type == ValueType.SET:
                entry["options"] = list(spec.options)
            out[code] = entry
        if job.limit_on and job.limit_on in out:
            raw = (post.get(f"joblimit:{job.key}") or "").strip()
            out[job.limit_on]["limit"] = raw
    return out

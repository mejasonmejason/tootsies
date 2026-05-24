"""Tiny CLI for updating order status from GitHub Actions.

Used by close-on-deploy.yml after Railway confirms a successful deploy.
Imports db.py + models.py so schema changes are picked up automatically.

Usage:
    python -m scripts.update_order_status <issue_number> <status>

Example:
    python -m scripts.update_order_status 22 served
"""

from __future__ import annotations

import asyncio
import os
import sys

from db import DB
from models import OrderStatus


async def main(issue_number: int, status: OrderStatus) -> None:
    dsn = os.environ["DATABASE_URL"]
    db = DB(dsn)
    await db.connect()
    try:
        order = await db.get_order_by_issue(issue_number)
        if order is None:
            print(f"no order found for issue #{issue_number}, skipping")
            return
        await db.update_order(order.id, status=status)
        print(f"order #{order.id} (issue #{issue_number}) -> {status.value}")
    finally:
        await db.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <issue_number> <status>")
        sys.exit(1)

    issue_num = int(sys.argv[1])
    try:
        new_status = OrderStatus(sys.argv[2])
    except ValueError:
        print(f"invalid status: {sys.argv[2]}")
        print(f"valid: {', '.join(s.value for s in OrderStatus)}")
        sys.exit(1)

    asyncio.run(main(issue_num, new_status))

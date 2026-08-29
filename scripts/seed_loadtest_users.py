"""Create isolated database users and short-lived tokens for Locust read tests.

This deliberately bypasses registration/captcha: it is a local performance
fixture, not an account-provisioning path. Accounts always use the reserved
``@loadtest.invalid`` domain and can be created repeatedly without duplicates.
The generated token file is intended for ``artifacts/`` which is gitignored.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.core.database import async_session_factory
from app.core.security import create_access_token
from app.models.db_models import User


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed isolated users for Locust protected-read tests.")
    parser.add_argument("--count", type=int, default=200, help="Number of dedicated test users (default: 200).")
    parser.add_argument("--prefix", default="loadtest-", help="Account prefix; must end with '-'.")
    parser.add_argument(
        "--output",
        default="artifacts/loadtest-users.json",
        help="Local token file written for Locust (default: artifacts/loadtest-users.json).",
    )
    return parser.parse_args()


async def _seed(*, count: int, prefix: str) -> list[dict[str, str]]:
    if not 1 <= count <= 1000:
        raise ValueError("--count 必须在 1 到 1000 之间")
    if not prefix.endswith("-") or not prefix.replace("-", "").replace("_", "").isalnum():
        raise ValueError("--prefix 只能包含字母、数字、下划线和连字符，且必须以 '-' 结尾")

    accounts = [f"{prefix}{index:04d}@loadtest.invalid" for index in range(1, count + 1)]
    async with async_session_factory() as session:
        existing = {
            user.account: user
            for user in (
                await session.execute(select(User).where(User.account.in_(accounts)))
            ).scalars()
        }
        users: list[User] = []
        for index, account in enumerate(accounts, start=1):
            user = existing.get(account)
            if user is None:
                user = User(
                    username=f"压测用户 {index:04d}",
                    account=account,
                    # This fixture never uses the password login route. Keeping a
                    # non-loginable marker avoids spending 200 Argon2 hashes here.
                    password_hash="!loadtest-fixture-no-login!",
                    role="user",
                    status="active",
                )
                session.add(user)
            users.append(user)
        await session.commit()

        # New users receive their server-side UUID only after commit.
        for user in users:
            await session.refresh(user)
        return [
            {
                "user_id": str(user.id),
                "account": user.account,
                "access_token": create_access_token(str(user.id), user.username, user.role),
            }
            for user in users
        ]


async def main() -> None:
    args = _arguments()
    entries = await _seed(count=args.count, prefix=args.prefix)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"users": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已准备 {len(entries)} 个压测账号；token 已写入 {output}（请勿提交或分享）。")


if __name__ == "__main__":
    asyncio.run(main())

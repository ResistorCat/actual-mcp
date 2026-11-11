from __future__ import annotations

import datetime
import decimal
import logging
import threading
import uuid
from typing import Any

from actual import Actual
from actual.database import Accounts, Categories, Payees, Transactions
from actual.queries import (
    create_budget,
    create_payee,
    create_transaction,
    get_accounts,
    get_accumulated_budgeted_balance,
    get_budgets,
    get_category_groups,
    get_transactions,
    set_transaction_payee,
)
from sqlalchemy import func
from sqlmodel import Session, select

from actual_mcp.config import settings

logger = logging.getLogger(__name__)


def is_valid_uuid(val: str) -> bool:
    try:
        uuid.UUID(str(val))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def resolve_account(s: Session, identifier: str | Accounts) -> Accounts:
    if isinstance(identifier, Accounts):
        return identifier
    # Check by ID first
    acct = s.exec(select(Accounts).where(Accounts.id == identifier, Accounts.tombstone == 0)).one_or_none()
    if acct:
        return acct
    # Check by exact name
    acct = s.exec(select(Accounts).where(Accounts.name == identifier, Accounts.tombstone == 0)).one_or_none()
    if acct:
        return acct
    # Check by case-insensitive name
    acct = s.exec(
        select(Accounts).where(func.lower(Accounts.name) == identifier.lower(), Accounts.tombstone == 0)
    ).first()
    if not acct:
        raise ValueError(f"Account '{identifier}' not found.")
    return acct


def resolve_category(s: Session, identifier: str | Categories | None) -> Categories | None:
    if identifier is None:
        return None
    if isinstance(identifier, Categories):
        return identifier
    # Check by ID first
    cat = s.exec(select(Categories).where(Categories.id == identifier, Categories.tombstone == 0)).one_or_none()
    if cat:
        return cat
    # Check by exact name
    cat = s.exec(select(Categories).where(Categories.name == identifier, Categories.tombstone == 0)).one_or_none()
    if cat:
        return cat
    # Check by case-insensitive name
    cat = s.exec(
        select(Categories).where(func.lower(Categories.name) == identifier.lower(), Categories.tombstone == 0)
    ).first()
    if not cat:
        raise ValueError(f"Category '{identifier}' not found.")
    return cat


def resolve_payee(s: Session, identifier: str | Payees | None) -> Payees | None:
    if identifier is None:
        return None
    if isinstance(identifier, Payees):
        return identifier
    # Check by ID first
    p = s.exec(select(Payees).where(Payees.id == identifier, Payees.tombstone == 0)).one_or_none()
    if p:
        return p
    # Check by exact name
    p = s.exec(select(Payees).where(Payees.name == identifier, Payees.tombstone == 0)).one_or_none()
    if p:
        return p
    # Check by case-insensitive name
    p = s.exec(
        select(Payees).where(func.lower(Payees.name) == identifier.lower(), Payees.tombstone == 0)
    ).first()
    if not p:
        p = create_payee(s, identifier)
    return p


def parse_date(date_str: str) -> datetime.date:
    return datetime.date.fromisoformat(date_str)


def parse_month(month_str: str | None) -> datetime.date:
    if not month_str:
        return datetime.date.today().replace(day=1)
    if len(month_str) == 7:  # YYYY-MM
        return datetime.date.fromisoformat(f"{month_str}-01")
    return datetime.date.fromisoformat(month_str).replace(day=1)


class ActualBudgetClient:
    """Thread-safe client wrapping actualpy with session caching and sync/commit handling."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._actual: Actual | None = None

    def _get_actual(self) -> Actual:
        if self._actual is not None:
            return self._actual

        if not settings.is_configured:
            raise ValueError(
                "Actual Budget credentials are not fully configured. "
                "Ensure ACTUAL_PASSWORD and ACTUAL_FILE are set."
            )

        settings.actual_data_dir.mkdir(parents=True, exist_ok=True)
        self._actual = Actual(
            base_url=settings.actual_url,
            password=settings.actual_password,
            file=settings.actual_file,
            encryption_password=settings.actual_encryption_password,
            data_dir=settings.actual_data_dir,
            cert=settings.actual_verify_ssl,
            extra_headers=settings.extra_headers,
        )
        self._actual.__enter__()
        return self._actual

    def close(self) -> None:
        with self._lock:
            if self._actual is not None:
                try:
                    self._actual.__exit__(None, None, None)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Error closing Actual session: %s", e)
                self._actual = None

    def sync(self) -> dict[str, str]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()
            return {"status": "synchronized"}

    def list_accounts(self, include_closed: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()
            accounts = get_accounts(actual.session, include_deleted=False)
            results = []
            for acct in accounts:
                if not include_closed and acct.closed:
                    continue
                results.append(
                    {
                        "id": acct.id,
                        "name": acct.name,
                        "balance": float(acct.balance),
                        "offbudget": bool(acct.offbudget),
                        "closed": bool(acct.closed),
                    }
                )
            return results

    def list_categories(self, include_income: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()
            groups = get_category_groups(actual.session, include_deleted=False)
            results = []
            for group in groups:
                if not include_income and group.is_income:
                    continue
                categories = [
                    {
                        "id": cat.id,
                        "name": cat.name,
                        "is_income": bool(cat.is_income),
                        "hidden": bool(cat.hidden),
                    }
                    for cat in (group.categories or [])
                    if not getattr(cat, "tombstone", 0)
                ]
                results.append(
                    {
                        "group_id": group.id,
                        "group_name": group.name,
                        "is_income": bool(group.is_income),
                        "hidden": bool(group.hidden),
                        "categories": categories,
                    }
                )
            return results

    def get_monthly_budget(self, month: str | None = None) -> dict[str, Any]:
        m_date = parse_month(month)
        month_str = m_date.strftime("%Y-%m")
        with self._lock:
            actual = self._get_actual()
            actual.sync()
            budgets = get_budgets(actual.session, month=m_date)
            items = []
            for b in budgets:
                cat_name = b.category.name if b.category else ""
                try:
                    available = float(get_accumulated_budgeted_balance(actual.session, m_date, b.category_id))
                except Exception:  # noqa: BLE001
                    available = float(b.get_amount() + b.balance)

                items.append(
                    {
                        "category_id": b.category_id,
                        "category_name": cat_name,
                        "budgeted": float(b.get_amount()),
                        "spent": float(b.balance),
                        "available": available,
                        "carryover": bool(b.carryover),
                    }
                )

            return {
                "month": month_str,
                "budgets": items,
            }

    def list_transactions(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        account: str | None = None,
        category: str | None = None,
        notes: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        s_date = parse_date(start_date) if start_date else None
        e_date = parse_date(end_date) if end_date else None

        with self._lock:
            actual = self._get_actual()
            actual.sync()

            resolved_acct = resolve_account(actual.session, account) if account else None
            resolved_cat = resolve_category(actual.session, category) if category else None

            txs = get_transactions(
                actual.session,
                start_date=s_date,
                end_date=e_date,
                notes=notes,
                account=resolved_acct,
                category=resolved_cat,
                include_deleted=False,
            )

            # Sort by date descending
            sorted_txs = sorted(txs, key=lambda t: t.date or 0, reverse=True)[:limit]
            results = []
            for tx in sorted_txs:
                results.append(
                    {
                        "id": tx.id,
                        "date": str(tx.get_date()),
                        "amount": float(tx.get_amount()),
                        "account_id": tx.acct,
                        "account_name": tx.account.name if tx.account else "",
                        "payee_id": tx.payee_id,
                        "payee_name": tx.payee.name if tx.payee else "",
                        "category_id": tx.category_id,
                        "category_name": tx.category.name if tx.category else "",
                        "notes": tx.notes or "",
                        "cleared": bool(tx.cleared),
                    }
                )
            return results

    def add_transaction(
        self,
        date: str,
        account: str,
        amount: float,
        payee: str | None = None,
        category: str | None = None,
        notes: str = "",
        cleared: bool = False,
    ) -> dict[str, Any]:
        tx_date = parse_date(date)
        amount_dec = decimal.Decimal(str(amount))

        with self._lock:
            actual = self._get_actual()
            actual.sync()

            acct = resolve_account(actual.session, account)
            cat_obj = resolve_category(actual.session, category) if category else None
            p_obj = resolve_payee(actual.session, payee) if payee else None

            tx = create_transaction(
                actual.session,
                date=tx_date,
                account=acct,
                payee=p_obj,
                notes=notes,
                category=cat_obj,
                amount=amount_dec,
                cleared=cleared,
            )
            actual.commit()

            return {
                "id": tx.id,
                "date": str(tx.get_date()),
                "amount": float(tx.get_amount()),
                "account_id": tx.acct,
                "account_name": acct.name,
                "payee_name": p_obj.name if p_obj else "",
                "category_name": cat_obj.name if cat_obj else "",
                "notes": tx.notes or "",
                "cleared": bool(tx.cleared),
            }

    def update_transaction(
        self,
        transaction_id: str,
        date: str | None = None,
        amount: float | None = None,
        payee: str | None = None,
        category: str | None = None,
        notes: str | None = None,
        cleared: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()

            tx = actual.session.exec(
                select(Transactions).where(Transactions.id == transaction_id, Transactions.tombstone == 0)
            ).one_or_none()
            if not tx:
                raise ValueError(f"Transaction '{transaction_id}' not found.")

            if date is not None:
                tx.set_date(parse_date(date))
            if amount is not None:
                tx.set_amount(decimal.Decimal(str(amount)))
            if payee is not None:
                p_obj = resolve_payee(actual.session, payee)
                set_transaction_payee(actual.session, tx, p_obj)
            if category is not None:
                cat_obj = resolve_category(actual.session, category)
                tx.category = cat_obj
                tx.category_id = cat_obj.id if cat_obj else None
            if notes is not None:
                tx.notes = notes
            if cleared is not None:
                tx.cleared = int(cleared)

            actual.commit()

            return {
                "id": tx.id,
                "date": str(tx.get_date()),
                "amount": float(tx.get_amount()),
                "account_id": tx.acct,
                "account_name": tx.account.name if tx.account else "",
                "payee_name": tx.payee.name if tx.payee else "",
                "category_name": tx.category.name if tx.category else "",
                "notes": tx.notes or "",
                "cleared": bool(tx.cleared),
            }

    def delete_transaction(self, transaction_id: str) -> dict[str, Any]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()

            tx = actual.session.exec(
                select(Transactions).where(Transactions.id == transaction_id, Transactions.tombstone == 0)
            ).one_or_none()
            if not tx:
                raise ValueError(f"Transaction '{transaction_id}' not found.")

            tx.delete()
            actual.commit()

            return {
                "status": "deleted",
                "transaction_id": transaction_id,
            }

    def set_category_budget(
        self,
        month: str,
        category: str,
        amount: float,
        carryover: bool | None = None,
    ) -> dict[str, Any]:
        m_date = parse_month(month)
        amount_dec = decimal.Decimal(str(amount))

        with self._lock:
            actual = self._get_actual()
            actual.sync()

            cat_obj = resolve_category(actual.session, category)
            if not cat_obj:
                raise ValueError(f"Category '{category}' not found.")

            budget = create_budget(
                actual.session,
                month=m_date,
                category=cat_obj,
                amount=amount_dec,
                carryover=carryover,
            )
            actual.commit()

            return {
                "category_id": budget.category_id,
                "category_name": cat_obj.name,
                "month": m_date.strftime("%Y-%m"),
                "budgeted": float(budget.get_amount()),
                "carryover": bool(budget.carryover),
            }


# Global singleton instance
client = ActualBudgetClient()

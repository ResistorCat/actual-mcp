from __future__ import annotations

import datetime
import decimal
import logging
import threading
import uuid
from collections import defaultdict
from typing import Any

from actual import Actual
from actual.database import (
    Accounts,
    Categories,
    CategoryGroups,
    CategoryMapping,
    PayeeMapping,
    Payees,
    Transactions,
)
from actual.queries import (
    create_account as actual_create_account,
)
from actual.queries import (
    create_budget,
    create_splits,
    create_transaction,
    create_transaction_from_ids,
    get_accounts,
    get_accumulated_budgeted_balance,
    get_budgets,
    get_category_groups,
    get_transactions,
    match_transaction,
    reconcile_transaction,
    set_transaction_payee,
)
from actual.queries import (
    create_category_group as actual_create_category_group,
)
from actual.queries import (
    create_payee as actual_create_payee,
)
from actual.queries import (
    create_transfer as actual_create_transfer,
)
from actual.queries import (
    get_payees as actual_get_payees,
)
from actual.utils.conversions import cents_to_decimal, date_to_int
from sqlalchemy import func
from sqlmodel import Session, col, select

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
    acct = s.exec(
        select(Accounts).where(Accounts.id == identifier, func.coalesce(Accounts.tombstone, 0) == 0)
    ).one_or_none()
    if acct:
        return acct
    # Check by exact name
    acct = s.exec(
        select(Accounts).where(Accounts.name == identifier, func.coalesce(Accounts.tombstone, 0) == 0)
    ).one_or_none()
    if acct:
        return acct
    # Check by case-insensitive name
    acct = s.exec(
        select(Accounts).where(
            func.lower(Accounts.name) == identifier.lower(),
            func.coalesce(Accounts.tombstone, 0) == 0,
        )
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
    cat = s.exec(
        select(Categories).where(Categories.id == identifier, func.coalesce(Categories.tombstone, 0) == 0)
    ).one_or_none()
    if cat:
        return cat
    # Check by exact name
    cat = s.exec(
        select(Categories).where(Categories.name == identifier, func.coalesce(Categories.tombstone, 0) == 0)
    ).one_or_none()
    if cat:
        return cat
    # Check by case-insensitive name
    cat = s.exec(
        select(Categories).where(
            func.lower(Categories.name) == identifier.lower(),
            func.coalesce(Categories.tombstone, 0) == 0,
        )
    ).first()
    if not cat:
        raise ValueError(f"Category '{identifier}' not found.")
    return cat


def resolve_category_group(s: Session, identifier: str | CategoryGroups) -> CategoryGroups:
    if isinstance(identifier, CategoryGroups):
        return identifier
    grp = s.exec(
        select(CategoryGroups).where(
            CategoryGroups.id == identifier,
            func.coalesce(CategoryGroups.tombstone, 0) == 0,
        )
    ).one_or_none()
    if grp:
        return grp
    grp = s.exec(
        select(CategoryGroups).where(
            CategoryGroups.name == identifier,
            func.coalesce(CategoryGroups.tombstone, 0) == 0,
        )
    ).one_or_none()
    if grp:
        return grp
    grp = s.exec(
        select(CategoryGroups).where(
            func.lower(CategoryGroups.name) == identifier.lower(),
            func.coalesce(CategoryGroups.tombstone, 0) == 0,
        )
    ).first()
    if not grp:
        raise ValueError(f"Category group '{identifier}' not found.")
    return grp


def resolve_payee(s: Session, identifier: str | Payees | None) -> Payees | None:
    if identifier is None:
        return None
    if isinstance(identifier, Payees):
        return identifier
    # Check by ID first
    p = s.exec(
        select(Payees).where(Payees.id == identifier, func.coalesce(Payees.tombstone, 0) == 0)
    ).one_or_none()
    if p:
        return p
    # Check by exact name
    p = s.exec(
        select(Payees).where(Payees.name == identifier, func.coalesce(Payees.tombstone, 0) == 0)
    ).one_or_none()
    if p:
        return p
    # Check by case-insensitive name
    p = s.exec(
        select(Payees).where(
            func.lower(Payees.name) == identifier.lower(),
            func.coalesce(Payees.tombstone, 0) == 0,
        )
    ).first()
    if not p:
        p = actual_create_payee(s, identifier)
    return p


def resolve_existing_payee(s: Session, identifier: str | Payees) -> Payees:
    if isinstance(identifier, Payees):
        return identifier
    p = s.exec(
        select(Payees).where(Payees.id == identifier, func.coalesce(Payees.tombstone, 0) == 0)
    ).one_or_none()
    if p:
        return p
    p = s.exec(
        select(Payees).where(Payees.name == identifier, func.coalesce(Payees.tombstone, 0) == 0)
    ).one_or_none()
    if p:
        return p
    p = s.exec(
        select(Payees).where(
            func.lower(Payees.name) == identifier.lower(),
            func.coalesce(Payees.tombstone, 0) == 0,
        )
    ).first()
    if not p:
        raise ValueError(f"Payee '{identifier}' not found.")
    return p


def ensure_transfer_payee(s: Session, account: Accounts) -> Payees:
    """Ensures an account has an associated transfer payee row."""
    if account.payee and not getattr(account.payee, "tombstone", 0):
        return account.payee
    existing = s.exec(
        select(Payees).where(
            Payees.transfer_acct == account.id,
            func.coalesce(Payees.tombstone, 0) == 0,
        )
    ).first()
    if existing:
        return existing
    payee = actual_create_payee(s, None)
    payee.transfer_acct = account.id
    s.add(payee)
    s.flush()
    return payee


def parse_date(date_str: str) -> datetime.date:
    return datetime.date.fromisoformat(date_str)


def parse_month(month_str: str | None) -> datetime.date:
    if not month_str:
        return datetime.date.today().replace(day=1)
    if len(month_str) == 7:  # YYYY-MM
        return datetime.date.fromisoformat(f"{month_str}-01")
    return datetime.date.fromisoformat(month_str).replace(day=1)


def _serialize_transaction(tx: Transactions, include_splits: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": tx.id,
        "date": str(tx.get_date()),
        "amount": float(tx.get_amount()),
        "account_id": tx.acct,
        "account_name": tx.account.name if tx.account else "",
        "payee_id": tx.payee_id,
        "payee_name": tx.payee.name if tx.payee and tx.payee.name else "",
        "category_id": tx.category_id,
        "category_name": tx.category.name if tx.category else "",
        "notes": tx.notes or "",
        "cleared": bool(tx.cleared),
        "reconciled": bool(tx.reconciled),
        "is_parent": bool(tx.is_parent),
        "is_child": bool(tx.is_child),
        "parent_id": tx.parent_id,
        "transferred_id": tx.transferred_id,
    }
    if include_splits and tx.is_parent:
        data["splits"] = [
            _serialize_transaction(sp, include_splits=False)
            for sp in (tx.splits or [])
            if not getattr(sp, "tombstone", 0)
        ]
    return data


def resolve_budget_file(target: str, user_files: list[Any]) -> Any:
    """Resolves target file identifier against remote budget files.

    Supports exact file_id/name/group_id, case-insensitive name, normalized slug matching,
    or automatic fallback to the single active budget on the server.
    """
    active_files = [f for f in user_files if getattr(f, "deleted", 0) == 0]
    target_clean = (target or "").strip()

    if target_clean:
        # 1. Exact match by file_id, name, or group_id
        for f in active_files:
            if f.file_id == target_clean or f.name == target_clean or f.group_id == target_clean:
                return f
        # 2. Case-insensitive match on name
        for f in active_files:
            if f.name.lower() == target_clean.lower():
                return f
        # 3. Normalized slug / substring match (e.g. 'My-Finances-af43811' matching 'My Finances')
        norm_target = target_clean.replace("-", " ").lower()
        for f in active_files:
            if f.name.lower() in norm_target or norm_target in f.name.lower():
                return f

    # 4. Fallback: if only 1 active budget exists, auto-select it
    if len(active_files) == 1:
        logger.info(
            "Auto-selecting single available budget file: '%s' (id: %s)",
            active_files[0].name,
            active_files[0].file_id,
        )
        return active_files[0]

    if not active_files:
        raise ValueError("No active budget files found on Actual server.")

    available = [f"'{f.name}' (id: {f.file_id})" for f in active_files]
    raise ValueError(
        f"Could not find budget '{target_clean}'. Available budgets on server: {available}"
    )


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
                "Ensure ACTUAL_PASSWORD is set."
            )

        settings.actual_data_dir.mkdir(parents=True, exist_ok=True)
        actual = Actual(
            base_url=settings.actual_url,
            password=settings.actual_password,
            file=None,
            encryption_password=settings.actual_encryption_password,
            data_dir=settings.actual_data_dir,
            cert=settings.actual_verify_ssl,
            extra_headers=settings.extra_headers,
        )

        remote_files = actual.list_user_files().data
        selected_file = resolve_budget_file(settings.actual_file, remote_files)
        actual.set_file(selected_file)
        actual.__enter__()
        self._actual = actual
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

    # -------------------------------------------------------------------------
    # 1. Account Management & Reconciliation
    # -------------------------------------------------------------------------

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

    def create_account(
        self,
        name: str,
        initial_balance: float = 0.0,
        off_budget: bool = False,
    ) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Account name cannot be empty.")

        with self._lock:
            actual = self._get_actual()
            actual.sync()

            existing = actual.session.exec(
                select(Accounts).where(
                    func.lower(Accounts.name) == clean_name.lower(),
                    func.coalesce(Accounts.tombstone, 0) == 0,
                )
            ).first()
            if existing:
                raise ValueError(f"Account '{clean_name}' already exists.")

            bal_dec = decimal.Decimal(str(initial_balance))
            acct = actual_create_account(
                actual.session,
                name=clean_name,
                initial_balance=bal_dec,
                off_budget=off_budget,
            )
            actual.session.flush()
            actual.commit()

            return {
                "id": acct.id,
                "name": acct.name,
                "balance": float(acct.balance),
                "offbudget": bool(acct.offbudget),
                "closed": bool(acct.closed),
            }

    def update_account(
        self,
        account: str,
        name: str | None = None,
        off_budget: bool | None = None,
        closed: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()

            acct = resolve_account(actual.session, account)
            if name is not None:
                clean_name = name.strip()
                if not clean_name:
                    raise ValueError("Account name cannot be empty.")
                acct.name = clean_name
            if off_budget is not None:
                acct.offbudget = int(off_budget)
            if closed is not None:
                acct.closed = int(closed)

            actual.session.add(acct)
            actual.session.flush()
            actual.commit()

            return {
                "id": acct.id,
                "name": acct.name,
                "balance": float(acct.balance),
                "offbudget": bool(acct.offbudget),
                "closed": bool(acct.closed),
            }

    def close_account(
        self,
        account: str,
        transfer_account: str | None = None,
        category: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()

            acct = resolve_account(actual.session, account)
            current_balance = acct.balance
            transfer_tx_id: str | None = None

            if current_balance != 0:
                if transfer_account:
                    dest = resolve_account(actual.session, transfer_account)
                    if dest.id == acct.id:
                        raise ValueError("Cannot transfer remaining balance to the same account.")
                    ensure_transfer_payee(actual.session, acct)
                    ensure_transfer_payee(actual.session, dest)

                    today = datetime.date.today()
                    if current_balance > 0:
                        src_tx, dst_tx = actual_create_transfer(
                            actual.session,
                            date=today,
                            source_account=acct,
                            dest_account=dest,
                            amount=current_balance,
                            notes=f"Closing transfer from {acct.name}",
                        )
                    else:
                        src_tx, dst_tx = actual_create_transfer(
                            actual.session,
                            date=today,
                            source_account=dest,
                            dest_account=acct,
                            amount=abs(current_balance),
                            notes=f"Closing transfer to {acct.name}",
                        )

                    if category:
                        cat_obj = resolve_category(actual.session, category)
                        if cat_obj:
                            if not acct.offbudget and dest.offbudget:
                                src_tx.category_id = cat_obj.id
                            elif acct.offbudget and not dest.offbudget:
                                dst_tx.category_id = cat_obj.id
                            else:
                                src_tx.category_id = cat_obj.id

                    transfer_tx_id = src_tx.id
                    actual.session.flush()
                elif not force:
                    raise ValueError(
                        f"Account '{acct.name}' has a non-zero balance ({float(current_balance)}). "
                        "Provide 'transfer_account' to move the remaining balance or set force=True."
                    )

            acct.closed = 1
            actual.session.add(acct)
            actual.session.flush()
            actual.commit()

            return {
                "id": acct.id,
                "name": acct.name,
                "balance": float(acct.balance),
                "offbudget": bool(acct.offbudget),
                "closed": bool(acct.closed),
                "transfer_transaction_id": transfer_tx_id,
            }

    def get_account_balance(
        self,
        account: str,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        cutoff_date = parse_date(as_of_date) if as_of_date else None

        with self._lock:
            actual = self._get_actual()
            actual.sync()

            acct = resolve_account(actual.session, account)
            query = select(Transactions).where(
                Transactions.acct == acct.id,
                func.coalesce(Transactions.is_parent, 0) == 0,
                func.coalesce(Transactions.tombstone, 0) == 0,
            )
            if cutoff_date is not None:
                query = query.where(col(Transactions.date) <= date_to_int(cutoff_date))

            txs = actual.session.exec(query).all()

            total_cents = sum(t.amount or 0 for t in txs)
            cleared_cents = sum(t.amount or 0 for t in txs if t.cleared)
            reconciled_cents = sum(t.amount or 0 for t in txs if t.reconciled)
            uncleared_cents = sum(t.amount or 0 for t in txs if not t.cleared)
            unreconciled_cleared_count = sum(1 for t in txs if t.cleared and not t.reconciled)

            return {
                "account_id": acct.id,
                "account_name": acct.name,
                "as_of_date": cutoff_date.isoformat() if cutoff_date else None,
                "balance": float(cents_to_decimal(total_cents)),
                "cleared_balance": float(cents_to_decimal(cleared_cents)),
                "reconciled_balance": float(cents_to_decimal(reconciled_cents)),
                "uncleared_balance": float(cents_to_decimal(uncleared_cents)),
                "unreconciled_cleared_count": unreconciled_cleared_count,
                "transaction_count": len(txs),
            }

    def reconcile_account(
        self,
        account: str,
        target_balance: float | None = None,
        as_of_date: str | None = None,
        create_adjustment: bool = True,
        mark_reconciled: bool = True,
    ) -> dict[str, Any]:
        cutoff_date = parse_date(as_of_date) if as_of_date else datetime.date.today()
        cutoff_int = date_to_int(cutoff_date)

        with self._lock:
            actual = self._get_actual()
            actual.sync()

            acct = resolve_account(actual.session, account)

            leaf_txs = actual.session.exec(
                select(Transactions).where(
                    Transactions.acct == acct.id,
                    func.coalesce(Transactions.is_parent, 0) == 0,
                    func.coalesce(Transactions.tombstone, 0) == 0,
                    col(Transactions.date) <= cutoff_int,
                )
            ).all()

            cleared_cents = sum(t.amount or 0 for t in leaf_txs if t.cleared)
            cleared_balance = cents_to_decimal(cleared_cents)

            diff_dec = decimal.Decimal(0)
            adjustment_tx_dict: dict[str, Any] | None = None

            if target_balance is not None:
                target_dec = decimal.Decimal(str(target_balance))
                diff_dec = target_dec - cleared_balance
                if diff_dec != 0 and create_adjustment:
                    payee_obj = resolve_payee(actual.session, "Reconciliation Balance Adjustment")
                    adj_tx = create_transaction(
                        actual.session,
                        date=cutoff_date,
                        account=acct,
                        payee=payee_obj,
                        notes=f"Reconciliation adjustment as of {cutoff_date.isoformat()}",
                        category=None,
                        amount=diff_dec,
                        cleared=True,
                    )
                    if mark_reconciled:
                        adj_tx.reconciled = 1
                    actual.session.flush()
                    adjustment_tx_dict = _serialize_transaction(adj_tx)

            reconciled_count = 0
            if mark_reconciled:
                cleared_txs = actual.session.exec(
                    select(Transactions).where(
                        Transactions.acct == acct.id,
                        func.coalesce(Transactions.tombstone, 0) == 0,
                        col(Transactions.date) <= cutoff_int,
                        Transactions.cleared == 1,
                    )
                ).all()
                for tx in cleared_txs:
                    if not tx.reconciled:
                        tx.reconciled = 1
                        actual.session.add(tx)
                        if not tx.is_parent:
                            reconciled_count += 1
                    elif adjustment_tx_dict and tx.id == adjustment_tx_dict["id"]:
                        reconciled_count += 1

                acct.last_reconciled = str(
                    int(datetime.datetime.now(datetime.UTC).timestamp() * 1000)
                )
                actual.session.add(acct)

            actual.session.flush()
            actual.commit()

            final_cleared_balance = (
                cleared_balance + diff_dec
                if (target_balance is not None and create_adjustment)
                else cleared_balance
            )

            return {
                "account_id": acct.id,
                "account_name": acct.name,
                "as_of_date": cutoff_date.isoformat(),
                "previous_cleared_balance": float(cleared_balance),
                "cleared_balance": float(final_cleared_balance),
                "target_balance": float(target_balance)
                if target_balance is not None
                else float(cleared_balance),
                "difference": float(diff_dec),
                "reconciled_count": reconciled_count,
                "adjustment_transaction": adjustment_tx_dict,
            }

    # -------------------------------------------------------------------------
    # 2. Categories, Category Groups & Payees Management
    # -------------------------------------------------------------------------

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

    def create_category_group(
        self,
        name: str,
        is_income: bool = False,
        hidden: bool = False,
    ) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Category group name cannot be empty.")

        with self._lock:
            actual = self._get_actual()
            actual.sync()

            existing = actual.session.exec(
                select(CategoryGroups).where(
                    func.lower(CategoryGroups.name) == clean_name.lower(),
                    func.coalesce(CategoryGroups.tombstone, 0) == 0,
                )
            ).first()
            if existing:
                raise ValueError(f"Category group '{clean_name}' already exists.")

            grp = actual_create_category_group(actual.session, clean_name)
            grp.is_income = int(is_income)
            grp.hidden = bool(hidden)
            actual.session.add(grp)
            actual.session.flush()
            actual.commit()

            return {
                "group_id": grp.id,
                "group_name": grp.name,
                "is_income": bool(grp.is_income),
                "hidden": bool(grp.hidden),
            }

    def update_category_group(
        self,
        group: str,
        name: str | None = None,
        hidden: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()

            grp = resolve_category_group(actual.session, group)
            if name is not None:
                clean_name = name.strip()
                if not clean_name:
                    raise ValueError("Category group name cannot be empty.")
                grp.name = clean_name
            if hidden is not None:
                grp.hidden = bool(hidden)

            actual.session.add(grp)
            actual.session.flush()
            actual.commit()

            return {
                "group_id": grp.id,
                "group_name": grp.name,
                "is_income": bool(grp.is_income),
                "hidden": bool(grp.hidden),
            }

    def delete_category_group(
        self,
        group: str,
        transfer_to_category: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()

            grp = resolve_category_group(actual.session, group)
            target_cat = (
                resolve_category(actual.session, transfer_to_category)
                if transfer_to_category
                else None
            )

            cats = actual.session.exec(
                select(Categories).where(
                    Categories.cat_group == grp.id,
                    func.coalesce(Categories.tombstone, 0) == 0,
                )
            ).all()

            for cat in cats:
                if target_cat and target_cat.id == cat.id:
                    raise ValueError("Cannot transfer transactions to a category inside the deleted group.")
                self._reassign_and_delete_category(actual.session, cat, target_cat)

            grp.delete()
            actual.session.add(grp)
            actual.session.flush()
            actual.commit()

            return {
                "status": "deleted",
                "group_id": grp.id,
                "deleted_categories_count": len(cats),
                "transferred_to_category_id": target_cat.id if target_cat else None,
            }

    def create_category(
        self,
        name: str,
        group: str | None = None,
        is_income: bool = False,
        hidden: bool = False,
    ) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Category name cannot be empty.")

        with self._lock:
            actual = self._get_actual()
            actual.sync()

            if group:
                try:
                    grp = resolve_category_group(actual.session, group)
                except ValueError:
                    grp = actual_create_category_group(actual.session, group.strip())
                    grp.is_income = int(is_income)
                    actual.session.add(grp)
                    actual.session.flush()
            else:
                grp = actual_create_category_group(actual.session, "Usual Expenses")
                actual.session.flush()

            existing = actual.session.exec(
                select(Categories).where(
                    func.lower(Categories.name) == clean_name.lower(),
                    Categories.cat_group == grp.id,
                    func.coalesce(Categories.tombstone, 0) == 0,
                )
            ).first()
            if existing:
                raise ValueError(f"Category '{clean_name}' already exists in group '{grp.name}'.")

            cat = Categories(
                id=str(uuid.uuid4()),
                name=clean_name,
                hidden=bool(hidden),
                is_income=int(is_income or bool(grp.is_income)),
                sort_order=0.0,
                cat_group=grp.id,
                tombstone=0,
            )
            mapping = CategoryMapping(id=cat.id, transfer_id=cat.id)
            actual.session.add(cat)
            actual.session.add(mapping)
            actual.session.flush()
            actual.commit()

            return {
                "id": cat.id,
                "name": cat.name,
                "group_id": grp.id,
                "group_name": grp.name,
                "is_income": bool(cat.is_income),
                "hidden": bool(cat.hidden),
            }

    def update_category(
        self,
        category: str,
        name: str | None = None,
        group: str | None = None,
        hidden: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()

            cat = resolve_category(actual.session, category)
            if not cat:
                raise ValueError(f"Category '{category}' not found.")

            if name is not None:
                clean_name = name.strip()
                if not clean_name:
                    raise ValueError("Category name cannot be empty.")
                cat.name = clean_name

            if group is not None:
                grp = resolve_category_group(actual.session, group)
                cat.cat_group = grp.id
                cat.is_income = int(bool(grp.is_income))

            if hidden is not None:
                cat.hidden = bool(hidden)

            actual.session.add(cat)
            actual.session.flush()
            actual.commit()

            grp_obj = (
                resolve_category_group(actual.session, cat.cat_group)
                if cat.cat_group
                else None
            )
            return {
                "id": cat.id,
                "name": cat.name,
                "group_id": cat.cat_group,
                "group_name": grp_obj.name if grp_obj else "",
                "is_income": bool(cat.is_income),
                "hidden": bool(cat.hidden),
            }

    def _reassign_and_delete_category(
        self,
        s: Session,
        cat: Categories,
        target_cat: Categories | None,
    ) -> int:
        txs = s.exec(
            select(Transactions).where(
                Transactions.category_id == cat.id,
                func.coalesce(Transactions.tombstone, 0) == 0,
            )
        ).all()
        for tx in txs:
            tx.category_id = target_cat.id if target_cat else None
            s.add(tx)

        if target_cat:
            mappings = s.exec(
                select(CategoryMapping).where(
                    (CategoryMapping.id == cat.id) | (CategoryMapping.transfer_id == cat.id)
                )
            ).all()
            for m in mappings:
                m.transfer_id = target_cat.id
                s.add(m)

        cat.delete()
        s.add(cat)
        return len(txs)

    def delete_category(
        self,
        category: str,
        transfer_to_category: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()

            cat = resolve_category(actual.session, category)
            if not cat:
                raise ValueError(f"Category '{category}' not found.")

            target_cat = (
                resolve_category(actual.session, transfer_to_category)
                if transfer_to_category
                else None
            )
            if target_cat and target_cat.id == cat.id:
                raise ValueError("Cannot transfer transactions to the same category being deleted.")

            reassigned_count = self._reassign_and_delete_category(actual.session, cat, target_cat)
            actual.session.flush()
            actual.commit()

            return {
                "status": "deleted",
                "category_id": cat.id,
                "reassigned_transactions": reassigned_count,
                "transferred_to_category_id": target_cat.id if target_cat else None,
            }

    def list_payees(self, include_transfers: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()

            payees = actual_get_payees(actual.session, include_deleted=False)
            results = []
            for p in payees:
                if not include_transfers and p.transfer_acct is not None:
                    continue
                results.append(
                    {
                        "id": p.id,
                        "name": p.name or "",
                        "favorite": bool(p.favorite),
                        "transfer_account_id": p.transfer_acct,
                    }
                )
            return results

    def create_payee(self, name: str, favorite: bool = False) -> dict[str, Any]:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Payee name cannot be empty.")

        with self._lock:
            actual = self._get_actual()
            actual.sync()

            existing = actual.session.exec(
                select(Payees).where(
                    func.lower(Payees.name) == clean_name.lower(),
                    func.coalesce(Payees.tombstone, 0) == 0,
                )
            ).first()
            if existing:
                raise ValueError(f"Payee '{clean_name}' already exists.")

            p = actual_create_payee(actual.session, clean_name)
            p.favorite = int(favorite)
            actual.session.add(p)
            actual.session.flush()
            actual.commit()

            return {
                "id": p.id,
                "name": p.name or "",
                "favorite": bool(p.favorite),
                "transfer_account_id": p.transfer_acct,
            }

    def update_payee(
        self,
        payee: str,
        name: str | None = None,
        favorite: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()

            p = resolve_existing_payee(actual.session, payee)
            if name is not None:
                clean_name = name.strip()
                if not clean_name:
                    raise ValueError("Payee name cannot be empty.")
                p.name = clean_name
            if favorite is not None:
                p.favorite = int(favorite)

            actual.session.add(p)
            actual.session.flush()
            actual.commit()

            return {
                "id": p.id,
                "name": p.name or "",
                "favorite": bool(p.favorite),
                "transfer_account_id": p.transfer_acct,
            }

    def delete_payee(self, payee: str) -> dict[str, Any]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()

            p = resolve_existing_payee(actual.session, payee)
            if p.transfer_acct is not None:
                raise ValueError("Cannot delete a transfer payee directly.")

            p.delete()
            actual.session.add(p)
            actual.session.flush()
            actual.commit()

            return {
                "status": "deleted",
                "payee_id": p.id,
            }

    def merge_payees(
        self,
        target_payee: str,
        source_payees: list[str],
    ) -> dict[str, Any]:
        if not source_payees:
            raise ValueError("source_payees list cannot be empty.")

        with self._lock:
            actual = self._get_actual()
            actual.sync()

            target = resolve_existing_payee(actual.session, target_payee)
            merged_ids: list[str] = []
            reassigned_tx_count = 0

            for src_identifier in source_payees:
                src = resolve_existing_payee(actual.session, src_identifier)
                if src.id == target.id:
                    continue
                if src.transfer_acct is not None:
                    raise ValueError(f"Cannot merge transfer payee '{src.id}'.")

                txs = actual.session.exec(
                    select(Transactions).where(
                        Transactions.payee_id == src.id,
                        func.coalesce(Transactions.tombstone, 0) == 0,
                    )
                ).all()
                for tx in txs:
                    tx.payee_id = target.id
                    actual.session.add(tx)
                    reassigned_tx_count += 1

                mappings = actual.session.exec(
                    select(PayeeMapping).where(
                        (PayeeMapping.id == src.id) | (PayeeMapping.target_id == src.id)
                    )
                ).all()
                for m in mappings:
                    m.target_id = target.id
                    actual.session.add(m)

                src.delete()
                actual.session.add(src)
                merged_ids.append(src.id)

            actual.session.flush()
            actual.commit()

            return {
                "target_payee_id": target.id,
                "target_payee_name": target.name,
                "merged_payee_ids": merged_ids,
                "reassigned_transactions": reassigned_tx_count,
            }

    # -------------------------------------------------------------------------
    # 3. Budgets
    # -------------------------------------------------------------------------

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
                    available = float(
                        get_accumulated_budgeted_balance(actual.session, m_date, b.category_id)
                    )
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

    # -------------------------------------------------------------------------
    # 4. Transactions, Splits, Transfers & Bulk Import
    # -------------------------------------------------------------------------

    def list_transactions(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        account: str | None = None,
        category: str | None = None,
        payee: str | None = None,
        notes: str | None = None,
        cleared: bool | None = None,
        reconciled: bool | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        s_date = parse_date(start_date) if start_date else None
        # actualpy's end_date filter is exclusive (< end_date); add 1 day to make YYYY-MM-DD inclusive
        e_date = (parse_date(end_date) + datetime.timedelta(days=1)) if end_date else None

        if offset < 0:
            raise ValueError("offset must be >= 0.")
        if limit < 1:
            raise ValueError("limit must be >= 1.")

        with self._lock:
            actual = self._get_actual()
            actual.sync()

            resolved_acct = resolve_account(actual.session, account) if account else None
            resolved_cat = resolve_category(actual.session, category) if category else None
            resolved_payee = resolve_existing_payee(actual.session, payee) if payee else None

            txs = get_transactions(
                actual.session,
                start_date=s_date,
                end_date=e_date,
                notes=notes,
                account=resolved_acct,
                category=resolved_cat,
                payee=resolved_payee,
                cleared=cleared,
                include_deleted=False,
            )

            if reconciled is not None:
                txs = [t for t in txs if bool(t.reconciled) == reconciled]

            # Sort by date descending
            sorted_txs = sorted(txs, key=lambda t: (t.date or 0, t.sort_order or 0), reverse=True)
            total_count = len(sorted_txs)

            # Compute aggregations across all matching leaf transactions
            cat_totals: dict[str | None, dict[str, Any]] = {}
            month_totals: dict[str, dict[str, Any]] = defaultdict(
                lambda: {
                    "income": decimal.Decimal(0),
                    "expenses": decimal.Decimal(0),
                    "net": decimal.Decimal(0),
                    "count": 0,
                }
            )

            for tx in sorted_txs:
                amt = tx.get_amount()
                cat_id = tx.category_id
                cat_name = tx.category.name if tx.category else "Uncategorized"
                if cat_id not in cat_totals:
                    cat_totals[cat_id] = {
                        "category_id": cat_id,
                        "category_name": cat_name,
                        "total_amount": decimal.Decimal(0),
                        "count": 0,
                    }
                cat_totals[cat_id]["total_amount"] += amt
                cat_totals[cat_id]["count"] += 1

                month_key = tx.get_date().strftime("%Y-%m")
                m_entry = month_totals[month_key]
                if amt > 0:
                    m_entry["income"] += amt
                elif amt < 0:
                    m_entry["expenses"] += amt
                m_entry["net"] += amt
                m_entry["count"] += 1

            by_category = [
                {
                    "category_id": item["category_id"],
                    "category_name": item["category_name"],
                    "total_amount": float(item["total_amount"]),
                    "count": item["count"],
                }
                for item in sorted(
                    cat_totals.values(),
                    key=lambda x: abs(x["total_amount"]),
                    reverse=True,
                )
            ]

            by_month = [
                {
                    "month": month_key,
                    "income": float(vals["income"]),
                    "expenses": float(vals["expenses"]),
                    "net": float(vals["net"]),
                    "count": vals["count"],
                }
                for month_key, vals in sorted(month_totals.items(), reverse=True)
            ]

            paginated_txs = sorted_txs[offset : offset + limit]
            results = [_serialize_transaction(tx) for tx in paginated_txs]

            return {
                "transactions": results,
                "total": total_count,
                "offset": offset,
                "limit": limit,
                "has_more": (offset + limit) < total_count,
                "by_category": by_category,
                "by_month": by_month,
            }

    def add_transaction(
        self,
        date: str,
        account: str,
        amount: float,
        payee: str | None = None,
        category: str | None = None,
        notes: str = "",
        cleared: bool = False,
        reconciled: bool = False,
        splits: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        tx_date = parse_date(date)
        amount_dec = decimal.Decimal(str(amount))

        with self._lock:
            actual = self._get_actual()
            actual.sync()

            acct = resolve_account(actual.session, account)
            p_obj = resolve_payee(actual.session, payee) if payee else None

            if splits:
                splits_sum = sum(
                    (decimal.Decimal(str(sp["amount"])) for sp in splits),
                    decimal.Decimal(0),
                )
                if amount_dec != 0 and abs(amount_dec - splits_sum) > decimal.Decimal("0.001"):
                    raise ValueError(
                        f"Transaction amount ({float(amount_dec)}) does not match "
                        f"the sum of splits ({float(splits_sum)})."
                    )

                child_txs: list[Transactions] = []
                for sp in splits:
                    sp_amount = decimal.Decimal(str(sp["amount"]))
                    sp_cat = resolve_category(actual.session, sp.get("category"))
                    sp_payee = (
                        resolve_payee(actual.session, sp["payee"])
                        if sp.get("payee")
                        else p_obj
                    )
                    sp_notes = sp.get("notes") or ""
                    child_tx = create_transaction_from_ids(
                        actual.session,
                        date=tx_date,
                        account_id=acct.id,
                        payee_id=sp_payee.id if sp_payee else None,
                        notes=sp_notes,
                        category_id=sp_cat.id if sp_cat else None,
                        amount=sp_amount,
                        cleared=cleared,
                        process_payee=False,
                    )
                    child_tx.reconciled = int(reconciled)
                    child_txs.append(child_tx)

                parent_tx = create_splits(actual.session, child_txs, notes=notes)
                parent_tx.payee_id = p_obj.id if p_obj else None
                parent_tx.cleared = int(cleared)
                parent_tx.reconciled = int(reconciled)
                actual.session.add(parent_tx)
                actual.session.flush()
                actual.commit()

                return _serialize_transaction(parent_tx, include_splits=True)

            cat_obj = resolve_category(actual.session, category) if category else None
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
            tx.reconciled = int(reconciled)
            actual.session.add(tx)
            actual.session.flush()
            actual.commit()

            return _serialize_transaction(tx)

    def update_transaction(
        self,
        transaction_id: str,
        date: str | None = None,
        account: str | None = None,
        amount: float | None = None,
        payee: str | None = None,
        category: str | None = None,
        notes: str | None = None,
        cleared: bool | None = None,
        reconciled: bool | None = None,
        splits: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()

            tx = actual.session.exec(
                select(Transactions).where(
                    Transactions.id == transaction_id,
                    func.coalesce(Transactions.tombstone, 0) == 0,
                )
            ).one_or_none()
            if not tx:
                raise ValueError(f"Transaction '{transaction_id}' not found.")

            if account is not None:
                new_acct = resolve_account(actual.session, account)
                tx.acct = new_acct.id
                if tx.is_parent:
                    children = actual.session.exec(
                        select(Transactions).where(
                            Transactions.parent_id == tx.id,
                            func.coalesce(Transactions.tombstone, 0) == 0,
                        )
                    ).all()
                    for child in children:
                        child.acct = new_acct.id
                        actual.session.add(child)

            if date is not None:
                parsed_date = parse_date(date)
                tx.set_date(parsed_date)
                if tx.is_parent:
                    children = actual.session.exec(
                        select(Transactions).where(
                            Transactions.parent_id == tx.id,
                            func.coalesce(Transactions.tombstone, 0) == 0,
                        )
                    ).all()
                    for child in children:
                        child.set_date(parsed_date)
                        actual.session.add(child)

            if amount is not None and splits is None:
                tx.set_amount(decimal.Decimal(str(amount)))

            if payee is not None:
                p_obj = resolve_payee(actual.session, payee)
                set_transaction_payee(actual.session, tx, p_obj)

            if category is not None and splits is None and not tx.is_parent:
                cat_obj = resolve_category(actual.session, category)
                tx.category = cat_obj
                tx.category_id = cat_obj.id if cat_obj else None

            if notes is not None:
                tx.notes = notes

            if cleared is not None:
                tx.cleared = int(cleared)

            if reconciled is not None:
                tx.reconciled = int(reconciled)
                if tx.is_parent:
                    children = actual.session.exec(
                        select(Transactions).where(
                            Transactions.parent_id == tx.id,
                            func.coalesce(Transactions.tombstone, 0) == 0,
                        )
                    ).all()
                    for child in children:
                        child.reconciled = int(reconciled)
                        actual.session.add(child)

            if splits is not None:
                old_children = actual.session.exec(
                    select(Transactions).where(
                        Transactions.parent_id == tx.id,
                        func.coalesce(Transactions.tombstone, 0) == 0,
                    )
                ).all()
                for old_child in old_children:
                    old_child.delete()
                    actual.session.add(old_child)

                if len(splits) == 0:
                    tx.is_parent = 0
                    if amount is not None:
                        tx.set_amount(decimal.Decimal(str(amount)))
                    if category is not None:
                        cat_obj = resolve_category(actual.session, category)
                        tx.category_id = cat_obj.id if cat_obj else None
                else:
                    splits_sum = sum(
                        (decimal.Decimal(str(sp["amount"])) for sp in splits),
                        decimal.Decimal(0),
                    )
                    if amount is not None and abs(
                        decimal.Decimal(str(amount)) - splits_sum
                    ) > decimal.Decimal("0.001"):
                        raise ValueError(
                            f"Provided amount ({amount}) does not match "
                            f"the sum of splits ({float(splits_sum)})."
                        )
                    tx.set_amount(splits_sum)
                    tx.is_parent = 1
                    tx.is_child = 0
                    tx.category_id = None

                    tx_date_val = tx.get_date()
                    for sp in splits:
                        sp_amount = decimal.Decimal(str(sp["amount"]))
                        sp_cat = resolve_category(actual.session, sp.get("category"))
                        sp_payee = (
                            resolve_payee(actual.session, sp["payee"])
                            if sp.get("payee")
                            else (tx.payee if tx.payee else None)
                        )
                        child_tx = create_transaction_from_ids(
                            actual.session,
                            date=tx_date_val,
                            account_id=tx.acct,
                            payee_id=sp_payee.id if sp_payee else tx.payee_id,
                            notes=sp.get("notes") or "",
                            category_id=sp_cat.id if sp_cat else None,
                            amount=sp_amount,
                            cleared=bool(tx.cleared),
                            process_payee=False,
                        )
                        child_tx.is_parent = 0
                        child_tx.is_child = 1
                        child_tx.parent_id = tx.id
                        child_tx.reconciled = int(bool(tx.reconciled))
                        actual.session.add(child_tx)

            # Synchronize peer transfer transaction if this is a transfer
            if tx.transferred_id:
                peer_tx = actual.session.exec(
                    select(Transactions).where(
                        Transactions.id == tx.transferred_id,
                        func.coalesce(Transactions.tombstone, 0) == 0,
                    )
                ).one_or_none()
                if peer_tx:
                    if date is not None:
                        peer_tx.set_date(tx.get_date())
                    if amount is not None:
                        peer_tx.set_amount(-tx.get_amount())
                    if notes is not None:
                        peer_tx.notes = tx.notes
                    actual.session.add(peer_tx)

            actual.session.add(tx)
            actual.session.flush()
            actual.session.refresh(tx)
            actual.commit()

            return _serialize_transaction(tx, include_splits=bool(tx.is_parent))

    def delete_transaction(self, transaction_id: str) -> dict[str, Any]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()

            tx = actual.session.exec(
                select(Transactions).where(
                    Transactions.id == transaction_id,
                    func.coalesce(Transactions.tombstone, 0) == 0,
                )
            ).one_or_none()
            if not tx:
                raise ValueError(f"Transaction '{transaction_id}' not found.")

            deleted_peer_id: str | None = None
            if tx.transferred_id:
                peer = actual.session.exec(
                    select(Transactions).where(
                        Transactions.id == tx.transferred_id,
                        func.coalesce(Transactions.tombstone, 0) == 0,
                    )
                ).one_or_none()
                if peer:
                    peer.delete()
                    actual.session.add(peer)
                    deleted_peer_id = peer.id

            tx.delete()
            actual.session.add(tx)
            actual.session.flush()
            actual.commit()

            return {
                "status": "deleted",
                "transaction_id": transaction_id,
                "deleted_transfer_peer_id": deleted_peer_id,
            }

    def add_transfer(
        self,
        date: str,
        source_account: str,
        dest_account: str,
        amount: float,
        notes: str = "",
        category: str | None = None,
        cleared: bool = False,
    ) -> dict[str, Any]:
        if amount <= 0:
            raise ValueError("Transfer amount must be a positive number.")

        tx_date = parse_date(date)
        amount_dec = decimal.Decimal(str(amount))

        with self._lock:
            actual = self._get_actual()
            actual.sync()

            source = resolve_account(actual.session, source_account)
            dest = resolve_account(actual.session, dest_account)
            if source.id == dest.id:
                raise ValueError("Source and destination accounts must be different.")

            ensure_transfer_payee(actual.session, source)
            ensure_transfer_payee(actual.session, dest)

            src_tx, dst_tx = actual_create_transfer(
                actual.session,
                date=tx_date,
                source_account=source,
                dest_account=dest,
                amount=amount_dec,
                notes=notes,
            )

            src_tx.cleared = int(cleared)
            dst_tx.cleared = int(cleared)

            if category:
                cat_obj = resolve_category(actual.session, category)
                if cat_obj:
                    if not source.offbudget and dest.offbudget:
                        src_tx.category_id = cat_obj.id
                    elif source.offbudget and not dest.offbudget:
                        dst_tx.category_id = cat_obj.id
                    else:
                        src_tx.category_id = cat_obj.id

            actual.session.add(src_tx)
            actual.session.add(dst_tx)
            actual.session.flush()
            actual.commit()

            return {
                "date": str(tx_date),
                "amount": float(amount_dec),
                "source_transaction": _serialize_transaction(src_tx),
                "dest_transaction": _serialize_transaction(dst_tx),
            }

    def import_transactions(
        self,
        account: str,
        transactions: list[dict[str, Any]],
        deduplicate: bool = True,
        update_existing: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            actual = self._get_actual()
            actual.sync()

            acct = resolve_account(actual.session, account)
            already_matched: list[Transactions] = []
            created_count = 0
            matched_count = 0
            results: list[dict[str, Any]] = []

            for item in transactions:
                tx_date = parse_date(str(item["date"]))
                amount_dec = decimal.Decimal(str(item["amount"]))
                payee_name = item.get("payee")
                cat_name = item.get("category")
                notes = item.get("notes")
                imported_id = item.get("imported_id")
                cleared_val = item.get("cleared")
                imported_payee = item.get("imported_payee")

                p_obj = resolve_payee(actual.session, payee_name) if payee_name else None
                cat_obj = resolve_category(actual.session, cat_name) if cat_name else None

                if deduplicate:
                    existing = match_transaction(
                        actual.session,
                        date=tx_date,
                        account=acct,
                        payee=p_obj,
                        amount=amount_dec,
                        imported_id=imported_id,
                        already_matched=already_matched,
                    )
                    tx = reconcile_transaction(
                        actual.session,
                        date=tx_date,
                        account=acct,
                        payee=p_obj,
                        notes=notes,
                        category=cat_obj,
                        amount=amount_dec,
                        imported_id=imported_id,
                        cleared=int(cleared_val) if cleared_val is not None else None,
                        imported_payee=imported_payee,
                        update_existing=update_existing,
                        already_matched=already_matched,
                    )
                    tx.cleared = int(bool(tx.cleared))
                    actual.session.flush()
                    already_matched.append(tx)

                    if existing is not None:
                        matched_count += 1
                        action = "matched"
                    else:
                        created_count += 1
                        action = "created"
                else:
                    tx = create_transaction(
                        actual.session,
                        date=tx_date,
                        account=acct,
                        payee=p_obj,
                        notes=notes or "",
                        category=cat_obj,
                        amount=amount_dec,
                        imported_id=imported_id,
                        cleared=bool(cleared_val) if cleared_val is not None else False,
                        imported_payee=imported_payee,
                    )
                    actual.session.flush()
                    created_count += 1
                    action = "created"

                serialized = _serialize_transaction(tx)
                serialized["import_action"] = action
                results.append(serialized)

            actual.commit()

            return {
                "account_id": acct.id,
                "account_name": acct.name,
                "total_processed": len(transactions),
                "created_count": created_count,
                "matched_count": matched_count,
                "transactions": results,
            }


# Global singleton instance
client = ActualBudgetClient()

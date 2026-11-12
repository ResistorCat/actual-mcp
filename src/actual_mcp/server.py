from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.requests import Request
from starlette.responses import JSONResponse

from actual_mcp.client import client
from actual_mcp.config import settings

logger = logging.getLogger(__name__)

# Configure authentication if MCP_API_KEY is defined
verifier = None
if settings.mcp_api_key:
    verifier = StaticTokenVerifier(
        tokens={
            settings.mcp_api_key: {
                "client_id": "actual-mcp-client",
                "scopes": ["read", "write"],
            }
        }
    )

mcp = FastMCP("Actual Budget MCP", auth=verifier)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Unauthenticated health endpoint for Coolify and monitoring probes."""
    return JSONResponse(
        {
            "status": "healthy",
            "service": "actual-mcp",
            "configured": settings.is_configured,
        }
    )


# ---------------------------------------------------------------------------
# 1. Accounts & Reconciliation Tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_accounts(include_closed: bool = False) -> list[dict[str, Any]]:
    """List all accounts (checking, savings, credit cards, investments) and their current balances.

    Args:
        include_closed: Whether to include closed accounts. Defaults to False.
    """
    return client.list_accounts(include_closed=include_closed)


@mcp.tool
def create_account(
    name: str,
    initial_balance: float = 0.0,
    off_budget: bool = False,
) -> dict[str, Any]:
    """Create a new account in Actual Budget.

    Args:
        name: Unique name for the new account.
        initial_balance: Starting balance of the account (default 0.0).
        off_budget: Whether the account is off-budget (tracking/investment) instead of on-budget. Defaults to False.
    """
    return client.create_account(
        name=name,
        initial_balance=initial_balance,
        off_budget=off_budget,
    )


@mcp.tool
def update_account(
    account: str,
    name: str | None = None,
    off_budget: bool | None = None,
    closed: bool | None = None,
) -> dict[str, Any]:
    """Update an existing account's name, off-budget status, or closed flag.

    Args:
        account: Current account name or UUID.
        name: New account name (optional).
        off_budget: Mark account as off-budget (True) or on-budget (False) (optional).
        closed: Mark account as closed (True) or reopen it (False) (optional).
    """
    return client.update_account(
        account=account,
        name=name,
        off_budget=off_budget,
        closed=closed,
    )


@mcp.tool
def close_account(
    account: str,
    transfer_account: str | None = None,
    category: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Close an account, optionally transferring any remaining non-zero balance to another account.

    Args:
        account: Account name or UUID to close.
        transfer_account: Destination account name or UUID to receive the remaining balance (required if balance != 0 unless force=True).
        category: Category name or UUID if transferring between an on-budget and an off-budget account (optional).
        force: Close the account even if its balance is non-zero without transferring (default False).
    """
    return client.close_account(
        account=account,
        transfer_account=transfer_account,
        category=category,
        force=force,
    )


@mcp.tool
def get_account_balance(
    account: str,
    as_of_date: str | None = None,
) -> dict[str, Any]:
    """Get detailed account balances (total, cleared, reconciled, uncleared) optionally as of a cutoff date.

    Args:
        account: Account name or UUID.
        as_of_date: Optional cutoff date in 'YYYY-MM-DD' format (inclusive). Defaults to all dates.
    """
    return client.get_account_balance(account=account, as_of_date=as_of_date)


@mcp.tool
def reconcile_account(
    account: str,
    target_balance: float | None = None,
    as_of_date: str | None = None,
    create_adjustment: bool = True,
    mark_reconciled: bool = True,
) -> dict[str, Any]:
    """Reconcile an account as of a specific date against an optional statement target balance.

    Args:
        account: Account name or UUID to reconcile.
        target_balance: Statement balance to reconcile against (optional).
        as_of_date: Cutoff date in 'YYYY-MM-DD' format (defaults to today).
        create_adjustment: Automatically create a balance adjustment transaction if target_balance differs from cleared balance (default True).
        mark_reconciled: Lock (reconciled=True) all cleared transactions up to as_of_date (default True).
    """
    return client.reconcile_account(
        account=account,
        target_balance=target_balance,
        as_of_date=as_of_date,
        create_adjustment=create_adjustment,
        mark_reconciled=mark_reconciled,
    )


# ---------------------------------------------------------------------------
# 2. Categories, Category Groups & Payees Tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_categories(include_income: bool = True) -> list[dict[str, Any]]:
    """List all budget categories grouped by category group.

    Args:
        include_income: Whether to include income categories. Defaults to True.
    """
    return client.list_categories(include_income=include_income)


@mcp.tool
def create_category(
    name: str,
    group: str | None = None,
    is_income: bool = False,
    hidden: bool = False,
) -> dict[str, Any]:
    """Create a new budget category inside a category group.

    Args:
        name: Name of the category.
        group: Category group name or UUID (defaults to 'Usual Expenses' or creates the group if missing).
        is_income: Whether this is an income category (default False).
        hidden: Whether the category starts hidden (default False).
    """
    return client.create_category(
        name=name,
        group=group,
        is_income=is_income,
        hidden=hidden,
    )


@mcp.tool
def update_category(
    category: str,
    name: str | None = None,
    group: str | None = None,
    hidden: bool | None = None,
) -> dict[str, Any]:
    """Rename, move to another group, or hide/unhide a budget category.

    Args:
        category: Category name or UUID to update.
        name: New category name (optional).
        group: Target category group name or UUID (optional).
        hidden: New hidden state (optional).
    """
    return client.update_category(
        category=category,
        name=name,
        group=group,
        hidden=hidden,
    )


@mcp.tool
def delete_category(
    category: str,
    transfer_to_category: str | None = None,
) -> dict[str, Any]:
    """Delete (tombstone) a category and optionally reassign its transactions to another category.

    Args:
        category: Category name or UUID to delete.
        transfer_to_category: Replacement category name or UUID for existing transactions (optional).
    """
    return client.delete_category(
        category=category,
        transfer_to_category=transfer_to_category,
    )


@mcp.tool
def create_category_group(
    name: str,
    is_income: bool = False,
    hidden: bool = False,
) -> dict[str, Any]:
    """Create a new category group.

    Args:
        name: Name of the category group.
        is_income: Whether the group holds income categories (default False).
        hidden: Whether the group is hidden (default False).
    """
    return client.create_category_group(
        name=name,
        is_income=is_income,
        hidden=hidden,
    )


@mcp.tool
def update_category_group(
    group: str,
    name: str | None = None,
    hidden: bool | None = None,
) -> dict[str, Any]:
    """Update a category group's name or hidden flag.

    Args:
        group: Category group name or UUID.
        name: New name for the category group (optional).
        hidden: Whether the group is hidden (optional).
    """
    return client.update_category_group(
        group=group,
        name=name,
        hidden=hidden,
    )


@mcp.tool
def delete_category_group(
    group: str,
    transfer_to_category: str | None = None,
) -> dict[str, Any]:
    """Delete a category group and all its categories, optionally reassigning transactions to another category.

    Args:
        group: Category group name or UUID to delete.
        transfer_to_category: Replacement category name or UUID for existing transactions (optional).
    """
    return client.delete_category_group(
        group=group,
        transfer_to_category=transfer_to_category,
    )


@mcp.tool
def get_payees(include_transfers: bool = False) -> list[dict[str, Any]]:
    """List all payees in Actual Budget.

    Args:
        include_transfers: Whether to include internal account-transfer payees. Defaults to False.
    """
    return client.list_payees(include_transfers=include_transfers)


@mcp.tool
def create_payee(name: str, favorite: bool = False) -> dict[str, Any]:
    """Create a new payee.

    Args:
        name: Unique name of the payee.
        favorite: Whether to mark the payee as favorite (default False).
    """
    return client.create_payee(name=name, favorite=favorite)


@mcp.tool
def update_payee(
    payee: str,
    name: str | None = None,
    favorite: bool | None = None,
) -> dict[str, Any]:
    """Rename a payee or toggle its favorite status.

    Args:
        payee: Payee name or UUID to update.
        name: New name for the payee (optional).
        favorite: Whether the payee is marked as favorite (optional).
    """
    return client.update_payee(payee=payee, name=name, favorite=favorite)


@mcp.tool
def delete_payee(payee: str) -> dict[str, Any]:
    """Delete (tombstone) a payee by its name or UUID.

    Args:
        payee: Payee name or UUID to delete.
    """
    return client.delete_payee(payee=payee)


@mcp.tool
def merge_payees(
    target_payee: str,
    source_payees: list[str],
) -> dict[str, Any]:
    """Merge one or more duplicate payees into a target payee, reassigning all transactions and mappings.

    Args:
        target_payee: Name or UUID of the destination payee to keep.
        source_payees: List of payee names or UUIDs to merge into target_payee and delete.
    """
    return client.merge_payees(
        target_payee=target_payee,
        source_payees=source_payees,
    )


# ---------------------------------------------------------------------------
# 3. Budget Tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_budget_month(month: str | None = None) -> dict[str, Any]:
    """Get the budget summary for a specific month, including budgeted, spent, and available balances.

    Args:
        month: Month in 'YYYY-MM' format (e.g. '2026-11'). Defaults to the current month if omitted.
    """
    return client.get_monthly_budget(month=month)


@mcp.tool
def set_budget_amount(
    month: str,
    category: str,
    amount: float,
    carryover: bool | None = None,
) -> dict[str, Any]:
    """Set or update the budgeted amount for a category in a given month.

    Args:
        month: Month in 'YYYY-MM' format (e.g. '2026-11').
        category: Category name or UUID.
        amount: Budgeted amount (positive float).
        carryover: Whether to carry over overspending/surplus to the next month (optional).
    """
    return client.set_category_budget(
        month=month,
        category=category,
        amount=amount,
        carryover=carryover,
    )


# ---------------------------------------------------------------------------
# 4. Transactions, Splits, Transfers & Bulk Import Tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_transactions(
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
    """Search, filter, paginate, and aggregate transactions by category and month.

    Args:
        start_date: Earliest date in 'YYYY-MM-DD' format (inclusive).
        end_date: Latest date in 'YYYY-MM-DD' format (inclusive).
        account: Account name or UUID to filter by.
        category: Category name or UUID to filter by.
        payee: Payee name or UUID to filter by.
        notes: Substring to search inside transaction notes.
        cleared: Filter by cleared status (True/False).
        reconciled: Filter by reconciled status (True/False).
        offset: Pagination offset (default 0).
        limit: Maximum number of transactions to return in the page (default 50).
    """
    return client.list_transactions(
        start_date=start_date,
        end_date=end_date,
        account=account,
        category=category,
        payee=payee,
        notes=notes,
        cleared=cleared,
        reconciled=reconciled,
        offset=offset,
        limit=limit,
    )


@mcp.tool
def create_transaction(
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
    """Create a new transaction (single or split) in Actual Budget and synchronize changes.

    Args:
        date: Date in 'YYYY-MM-DD' format.
        account: Account name or UUID where the transaction occurs.
        amount: Transaction amount (negative for expenses, positive for income). For split transactions, must match the sum of splits (or be 0).
        payee: Payee name (will be matched or created automatically).
        category: Category name or UUID (ignored on parent when splits are provided).
        notes: Optional description or note for the transaction.
        cleared: Whether the transaction is marked as cleared (default False).
        reconciled: Whether the transaction is locked/reconciled (default False).
        splits: Optional list of split items, each with {'amount': float, 'category': str | None, 'payee': str | None, 'notes': str | None}.
    """
    return client.add_transaction(
        date=date,
        account=account,
        amount=amount,
        payee=payee,
        category=category,
        notes=notes,
        cleared=cleared,
        reconciled=reconciled,
        splits=splits,
    )


@mcp.tool
def update_transaction(
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
    """Update fields of an existing transaction (including moving account or updating splits) and commit changes.

    Args:
        transaction_id: The UUID of the transaction to update.
        date: New date in 'YYYY-MM-DD' format (optional).
        account: New account name or UUID to move the transaction to (optional).
        amount: New amount (negative for expenses, positive for income) (optional).
        payee: New payee name (optional).
        category: New category name or UUID (optional).
        notes: New transaction note (optional).
        cleared: New cleared status (optional).
        reconciled: New reconciled/locked status (optional).
        splits: Optional list of split dicts {'amount': float, 'category': str | None, 'payee': str | None, 'notes': str | None} to replace or convert to splits (pass [] to unsplit).
    """
    return client.update_transaction(
        transaction_id=transaction_id,
        date=date,
        account=account,
        amount=amount,
        payee=payee,
        category=category,
        notes=notes,
        cleared=cleared,
        reconciled=reconciled,
        splits=splits,
    )


@mcp.tool
def delete_transaction(transaction_id: str) -> dict[str, Any]:
    """Delete (tombstone) a transaction (and its child splits or transfer peer) by its ID and commit changes.

    Args:
        transaction_id: The UUID of the transaction to delete.
    """
    return client.delete_transaction(transaction_id=transaction_id)


@mcp.tool
def create_transfer(
    date: str,
    source_account: str,
    dest_account: str,
    amount: float,
    notes: str = "",
    category: str | None = None,
    cleared: bool = False,
) -> dict[str, Any]:
    """Create a linked double-entry transfer between two accounts.

    Args:
        date: Date of the transfer in 'YYYY-MM-DD' format.
        source_account: Account name or UUID sending the funds (balance decreases).
        dest_account: Account name or UUID receiving the funds (balance increases).
        amount: Positive amount to transfer.
        notes: Optional note for the transfer.
        category: Optional category name or UUID (used when transferring between an on-budget and an off-budget account).
        cleared: Whether both sides of the transfer are marked as cleared (default False).
    """
    return client.add_transfer(
        date=date,
        source_account=source_account,
        dest_account=dest_account,
        amount=amount,
        notes=notes,
        category=category,
        cleared=cleared,
    )


@mcp.tool
def import_transactions(
    account: str,
    transactions: list[dict[str, Any]],
    deduplicate: bool = True,
    update_existing: bool = True,
) -> dict[str, Any]:
    """Bulk import a list of transactions into an account with optional deduplication.

    Args:
        account: Target account name or UUID.
        transactions: List of transaction dicts with keys: 'date' (YYYY-MM-DD), 'amount' (float), and optional 'payee', 'category', 'notes', 'imported_id', 'cleared', 'imported_payee'.
        deduplicate: Match against existing transactions by imported_id or fuzzy date/amount/payee (+/- 7 days) (default True).
        update_existing: Update matched existing transactions with the imported fields (default True).
    """
    return client.import_transactions(
        account=account,
        transactions=transactions,
        deduplicate=deduplicate,
        update_existing=update_existing,
    )


@mcp.tool
def sync_budget() -> dict[str, str]:
    """Force an immediate incremental sync with the remote Actual Budget server."""
    return client.sync()


# Expose ASGI application for Uvicorn
app = mcp.http_app()


def main() -> None:
    """CLI entrypoint to run the server."""
    if settings.transport.lower() == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn

        uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()

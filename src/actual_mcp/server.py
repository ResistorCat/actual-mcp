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


@mcp.tool
def get_accounts(include_closed: bool = False) -> list[dict[str, Any]]:
    """List all accounts (checking, savings, credit cards, investments) and their current balances.

    Args:
        include_closed: Whether to include closed accounts. Defaults to False.
    """
    return client.list_accounts(include_closed=include_closed)


@mcp.tool
def get_categories(include_income: bool = True) -> list[dict[str, Any]]:
    """List all budget categories grouped by category group.

    Args:
        include_income: Whether to include income categories. Defaults to True.
    """
    return client.list_categories(include_income=include_income)


@mcp.tool
def get_budget_month(month: str | None = None) -> dict[str, Any]:
    """Get the budget summary for a specific month, including budgeted, spent, and available balances.

    Args:
        month: Month in 'YYYY-MM' format (e.g. '2026-11'). Defaults to the current month if omitted.
    """
    return client.get_monthly_budget(month=month)


@mcp.tool
def get_transactions(
    start_date: str | None = None,
    end_date: str | None = None,
    account: str | None = None,
    category: str | None = None,
    notes: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search and filter transactions from Actual Budget.

    Args:
        start_date: Earliest date in 'YYYY-MM-DD' format.
        end_date: Latest date in 'YYYY-MM-DD' format.
        account: Account name or UUID to filter by.
        category: Category name or UUID to filter by.
        notes: Substring to search inside transaction notes.
        limit: Maximum number of transactions to return (default 50).
    """
    return client.list_transactions(
        start_date=start_date,
        end_date=end_date,
        account=account,
        category=category,
        notes=notes,
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
) -> dict[str, Any]:
    """Create a new transaction in Actual Budget and synchronize changes.

    Args:
        date: Date in 'YYYY-MM-DD' format.
        account: Account name or UUID where the transaction occurs.
        amount: Transaction amount. Negative values are expenses (e.g. -45.50), positive values are income/deposits.
        payee: Payee name (will be matched or created automatically).
        category: Category name or UUID.
        notes: Optional description or note for the transaction.
        cleared: Whether the transaction is marked as cleared (default False).
    """
    return client.add_transaction(
        date=date,
        account=account,
        amount=amount,
        payee=payee,
        category=category,
        notes=notes,
        cleared=cleared,
    )


@mcp.tool
def update_transaction(
    transaction_id: str,
    date: str | None = None,
    amount: float | None = None,
    payee: str | None = None,
    category: str | None = None,
    notes: str | None = None,
    cleared: bool | None = None,
) -> dict[str, Any]:
    """Update fields of an existing transaction by its ID and commit changes.

    Args:
        transaction_id: The UUID of the transaction to update.
        date: New date in 'YYYY-MM-DD' format (optional).
        amount: New amount (negative for expenses, positive for income) (optional).
        payee: New payee name (optional).
        category: New category name or UUID (optional).
        notes: New transaction note (optional).
        cleared: New cleared status (optional).
    """
    return client.update_transaction(
        transaction_id=transaction_id,
        date=date,
        amount=amount,
        payee=payee,
        category=category,
        notes=notes,
        cleared=cleared,
    )


@mcp.tool
def delete_transaction(transaction_id: str) -> dict[str, Any]:
    """Delete (tombstone) a transaction by its ID and commit changes.

    Args:
        transaction_id: The UUID of the transaction to delete.
    """
    return client.delete_transaction(transaction_id=transaction_id)


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

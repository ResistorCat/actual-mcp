from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from actual.database import (
    Accounts,
    Categories,
    CategoryGroups,
    Payees,
    Preferences,
    ZeroBudgetMonths,
)
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from sqlmodel import Session, SQLModel, create_engine
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from actual_mcp.client import (
    ActualBudgetClient,
    is_valid_uuid,
    resolve_account,
    resolve_budget_file,
    resolve_category,
    resolve_payee,
)
from actual_mcp.config import Settings
from actual_mcp.server import app, mcp


@pytest.fixture
def memory_db():
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def mock_client(memory_db: Session) -> ActualBudgetClient:
    mock_actual = MagicMock()
    mock_actual.session = memory_db
    mock_actual.sync = MagicMock()
    mock_actual.commit = MagicMock()

    c = ActualBudgetClient()
    c._actual = mock_actual
    return c


def test_settings_properties():
    s = Settings(
        actual_password="test-password",
        actual_file="test-file",
        cf_access_client_id="id-123",
        cf_access_client_secret="secret-456",
    )
    assert s.is_configured is True
    assert s.extra_headers == {
        "CF-Access-Client-Id": "id-123",
        "CF-Access-Client-Secret": "secret-456",
    }

    s_empty = Settings(actual_password="", actual_file="")
    assert s_empty.is_configured is False
    assert s_empty.extra_headers is None


def test_uuid_validation():
    assert is_valid_uuid("7f164b1b-bf83-4a6d-9913-df2b63467765") is True
    assert is_valid_uuid("not-a-uuid") is False
    assert is_valid_uuid("") is False


def test_resolve_entities(memory_db: Session):
    account = Accounts(id="acc-uuid-1", name="Checking", closed=0, offbudget=0, tombstone=0)
    cat_group = CategoryGroups(id="grp-1", name="Food", is_income=0, hidden=False, sort_order=0, tombstone=0)
    category = Categories(id="cat-uuid-1", name="Groceries", tombstone=0, cat_group=cat_group.id, hidden=False)
    payee = Payees(id="pay-uuid-1", name="Supermarket", tombstone=0)

    memory_db.add_all([account, cat_group, category, payee])
    memory_db.commit()

    # Test account resolution by UUID and name
    assert resolve_account(memory_db, "acc-uuid-1").id == "acc-uuid-1"
    assert resolve_account(memory_db, "Checking").id == "acc-uuid-1"
    assert resolve_account(memory_db, "checking").id == "acc-uuid-1"
    with pytest.raises(ValueError):
        resolve_account(memory_db, "Nonexistent")

    # Test category resolution
    assert resolve_category(memory_db, "cat-uuid-1").id == "cat-uuid-1"
    assert resolve_category(memory_db, "Groceries").id == "cat-uuid-1"
    assert resolve_category(memory_db, "groceries").id == "cat-uuid-1"
    assert resolve_category(memory_db, None) is None
    with pytest.raises(ValueError):
        resolve_category(memory_db, "Nonexistent")

    # Test payee resolution and auto-creation
    assert resolve_payee(memory_db, "pay-uuid-1").id == "pay-uuid-1"
    assert resolve_payee(memory_db, "Supermarket").id == "pay-uuid-1"
    assert resolve_payee(memory_db, None) is None

    new_payee = resolve_payee(memory_db, "Bakery")
    assert new_payee is not None
    assert new_payee.name == "Bakery"


def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "actual-mcp"
        assert "configured" in data


def test_auth_protection_on_endpoints():
    verifier = StaticTokenVerifier(
        tokens={
            "valid-bearer-token": {
                "client_id": "test-client",
                "scopes": ["read", "write"],
            }
        }
    )
    auth_mcp = FastMCP("Auth MCP", auth=verifier)

    @auth_mcp.custom_route("/health", methods=["GET"])
    async def custom_health(r):
        return JSONResponse({"status": "healthy"})

    auth_app = auth_mcp.http_app()

    with TestClient(auth_app) as client:
        # Public health endpoint is unauthenticated
        res_health = client.get("/health")
        assert res_health.status_code == 200

        # MCP endpoint rejects unauthorized request
        res_unauth = client.post("/mcp")
        assert res_unauth.status_code == 401

        # MCP endpoint accepts valid token
        res_auth = client.post(
            "/mcp",
            headers={"Authorization": "Bearer valid-bearer-token"},
        )
        assert res_auth.status_code != 401


@pytest.mark.asyncio
async def test_mcp_tools_registered():
    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}
    expected_tools = {
        "get_accounts",
        "create_account",
        "update_account",
        "close_account",
        "get_account_balance",
        "reconcile_account",
        "get_categories",
        "create_category",
        "update_category",
        "delete_category",
        "create_category_group",
        "update_category_group",
        "delete_category_group",
        "get_payees",
        "create_payee",
        "update_payee",
        "delete_payee",
        "merge_payees",
        "get_budget_month",
        "set_budget_amount",
        "transfer_budget",
        "get_transactions",
        "create_transaction",
        "update_transaction",
        "delete_transaction",
        "create_transfer",
        "import_transactions",
        "sync_budget",
    }
    assert expected_tools.issubset(tool_names)


def test_client_crud_pagination_and_aggregations(mock_client: ActualBudgetClient):
    c = mock_client
    c.create_account(name="Wallet", initial_balance=0.0)
    c.create_account(name="Savings", initial_balance=0.0)
    c.create_category(name="Snacks", group="Food")
    c.create_category(name="Salary", group="Income", is_income=True)

    # 1. Add transactions across months and categories
    tx1 = c.add_transaction(
        date="2026-10-15",
        account="Wallet",
        amount=1000.00,
        payee="Employer",
        category="Salary",
        notes="October paycheck",
        cleared=True,
    )
    tx2 = c.add_transaction(
        date="2026-11-10",
        account="Wallet",
        amount=-15.75,
        payee="Corner Store",
        category="Snacks",
        notes="Coffee and pastry",
        cleared=True,
    )
    tx3 = c.add_transaction(
        date="2026-11-11",
        account="Wallet",
        amount=-24.25,
        payee="Supermarket",
        category="Snacks",
        notes="Cookies",
        cleared=False,
    )

    assert tx2["amount"] == -15.75
    assert tx2["account_name"] == "Wallet"
    assert tx2["cleared"] is True

    # 2. List transactions with pagination & check aggregations
    page1 = c.list_transactions(account="Wallet", offset=0, limit=2)
    assert page1["total"] == 3
    assert page1["offset"] == 0
    assert page1["limit"] == 2
    assert page1["has_more"] is True
    assert len(page1["transactions"]) == 2

    page2 = c.list_transactions(account="Wallet", offset=2, limit=2)
    assert page2["has_more"] is False
    assert len(page2["transactions"]) == 1

    # Verify by_category totals (aggregated across all 3 matching transactions)
    by_cat = {item["category_name"]: item for item in page1["by_category"]}
    assert by_cat["Salary"]["total_amount"] == 1000.00
    assert by_cat["Salary"]["count"] == 1
    assert by_cat["Snacks"]["total_amount"] == -40.00
    assert by_cat["Snacks"]["count"] == 2

    # Verify by_month totals
    by_month = {item["month"]: item for item in page1["by_month"]}
    assert by_month["2026-10"]["income"] == 1000.00
    assert by_month["2026-10"]["expenses"] == 0.00
    assert by_month["2026-11"]["expenses"] == -40.00
    assert by_month["2026-11"]["net"] == -40.00

    # 3. Update transaction including moving to another account and marking reconciled
    updated = c.update_transaction(
        transaction_id=tx3["id"],
        account="Savings",
        amount=-30.00,
        notes="Moved to Savings",
        reconciled=True,
    )
    assert updated["amount"] == -30.00
    assert updated["account_name"] == "Savings"
    assert updated["reconciled"] is True

    # Confirm Wallet now only has 2 transactions
    wallet_res = c.list_transactions(account="Wallet")
    assert wallet_res["total"] == 2

    # 4. Delete transaction
    del_res = c.delete_transaction(transaction_id=tx1["id"])
    assert del_res["status"] == "deleted"


def test_account_lifecycle_and_close_transfer(mock_client: ActualBudgetClient):
    c = mock_client
    acct1 = c.create_account(name="Checking", initial_balance=250.0, off_budget=False)
    acct2 = c.create_account(name="Brokerage", initial_balance=0.0, off_budget=True)

    assert acct1["balance"] == 250.0
    assert acct1["offbudget"] is False
    assert acct2["offbudget"] is True

    # Duplicate account creation fails
    with pytest.raises(ValueError, match="already exists"):
        c.create_account(name="checking")

    # Update account name and off_budget status
    updated = c.update_account(account="Checking", name="Main Checking", off_budget=True)
    assert updated["name"] == "Main Checking"
    assert updated["offbudget"] is True

    # Closing account with non-zero balance without transfer_account or force raises ValueError
    with pytest.raises(ValueError, match="non-zero balance"):
        c.close_account(account="Main Checking")

    # Closing account with transfer_account moves balance to Brokerage and closes Main Checking
    closed = c.close_account(account="Main Checking", transfer_account="Brokerage")
    assert closed["closed"] is True
    assert closed["balance"] == 0.0
    assert closed["transfer_transaction_id"] is not None

    brokerage_bal = c.get_account_balance(account="Brokerage")
    assert brokerage_bal["balance"] == 250.0


def test_transfers_between_accounts(mock_client: ActualBudgetClient):
    c = mock_client
    c.create_account(name="Checking", initial_balance=500.0)
    c.create_account(name="Savings", initial_balance=100.0)

    tr = c.add_transfer(
        date="2026-11-12",
        source_account="Checking",
        dest_account="Savings",
        amount=150.0,
        notes="Monthly savings transfer",
        cleared=True,
    )
    src_tx = tr["source_transaction"]
    dst_tx = tr["dest_transaction"]

    assert src_tx["amount"] == -150.0
    assert dst_tx["amount"] == 150.0
    assert src_tx["transferred_id"] == dst_tx["id"]
    assert dst_tx["transferred_id"] == src_tx["id"]

    assert c.get_account_balance("Checking")["balance"] == 350.0
    assert c.get_account_balance("Savings")["balance"] == 250.0

    # Updating amount on source transaction updates peer transaction automatically
    c.update_transaction(transaction_id=src_tx["id"], amount=-200.0)
    assert c.get_account_balance("Checking")["balance"] == 300.0
    assert c.get_account_balance("Savings")["balance"] == 300.0

    # Deleting source transaction deletes peer transaction too
    del_res = c.delete_transaction(transaction_id=src_tx["id"])
    assert del_res["deleted_transfer_peer_id"] == dst_tx["id"]
    assert c.get_account_balance("Checking")["balance"] == 500.0
    assert c.get_account_balance("Savings")["balance"] == 100.0


def test_bulk_import_with_deduplication(mock_client: ActualBudgetClient):
    c = mock_client
    c.create_account(name="Credit Card")
    c.create_category(name="Dining", group="Food")

    # Pre-existing manual transaction without imported_id
    c.add_transaction(
        date="2026-11-05",
        account="Credit Card",
        amount=-42.50,
        payee="Bistro",
        notes="Manual entry",
        cleared=False,
    )

    batch = [
        # 1. Should fuzzy-match the existing Bistro transaction (within 7 days, same amount & payee)
        {
            "date": "2026-11-06",
            "amount": -42.50,
            "payee": "Bistro",
            "category": "Dining",
            "imported_id": "bank-tx-001",
            "cleared": True,
        },
        # 2. Twin transaction #1 on same day
        {
            "date": "2026-11-08",
            "amount": -5.00,
            "payee": "Metro",
            "imported_id": "bank-tx-002",
            "cleared": True,
        },
        # 3. Twin transaction #2 on same day (must NOT deduplicate against #1 in the same batch)
        {
            "date": "2026-11-08",
            "amount": -5.00,
            "payee": "Metro",
            "imported_id": "bank-tx-003",
            "cleared": True,
        },
    ]

    res1 = c.import_transactions(account="Credit Card", transactions=batch, deduplicate=True)
    assert res1["total_processed"] == 3
    assert res1["matched_count"] == 1
    assert res1["created_count"] == 2

    # Re-importing the exact same batch should match all 3 and create 0
    res2 = c.import_transactions(account="Credit Card", transactions=batch, deduplicate=True)
    assert res2["matched_count"] == 3
    assert res2["created_count"] == 0

    all_txs = c.list_transactions(account="Credit Card")
    assert all_txs["total"] == 3


def test_reconciliation_and_balance_as_of_date(mock_client: ActualBudgetClient):
    c = mock_client
    c.create_account(name="Bank")

    c.add_transaction(
        date="2026-11-01",
        account="Bank",
        amount=1000.0,
        payee="Deposit",
        cleared=True,
    )
    c.add_transaction(
        date="2026-11-05",
        account="Bank",
        amount=-200.0,
        payee="Rent",
        cleared=True,
    )
    c.add_transaction(
        date="2026-11-10",
        account="Bank",
        amount=-50.0,
        payee="Pending Check",
        cleared=False,
    )
    c.add_transaction(
        date="2026-11-20",
        account="Bank",
        amount=-100.0,
        payee="Future Bill",
        cleared=True,
    )

    # Check balance as of 2026-11-12 (excludes 2026-11-20 transaction)
    bal = c.get_account_balance(account="Bank", as_of_date="2026-11-12")
    assert bal["balance"] == 750.0  # 1000 - 200 - 50
    assert bal["cleared_balance"] == 800.0  # 1000 - 200
    assert bal["uncleared_balance"] == -50.0
    assert bal["reconciled_balance"] == 0.0
    assert bal["unreconciled_cleared_count"] == 2

    # Reconcile as of 2026-11-12 to a statement target balance of 785.0 (creates -15.0 adjustment)
    rec = c.reconcile_account(
        account="Bank",
        target_balance=785.0,
        as_of_date="2026-11-12",
        create_adjustment=True,
        mark_reconciled=True,
    )
    assert rec["previous_cleared_balance"] == 800.0
    assert rec["difference"] == -15.0
    assert rec["cleared_balance"] == 785.0
    assert rec["adjustment_transaction"] is not None
    assert rec["adjustment_transaction"]["amount"] == -15.0
    assert rec["adjustment_transaction"]["reconciled"] is True

    # Verify reconciled balance as of 2026-11-12 is now 785.0 and unreconciled_cleared_count is 0
    bal_after = c.get_account_balance(account="Bank", as_of_date="2026-11-12")
    assert bal_after["reconciled_balance"] == 785.0
    assert bal_after["unreconciled_cleared_count"] == 0


def test_categories_groups_and_payees_management(mock_client: ActualBudgetClient):
    c = mock_client
    c.create_account(name="Cash")

    # Category Groups CRUD
    grp = c.create_category_group(name="Housing", is_income=False)
    assert grp["group_name"] == "Housing"

    grp_upd = c.update_category_group(group=grp["group_id"], name="Home & Utilities", hidden=True)
    assert grp_upd["group_name"] == "Home & Utilities"
    assert grp_upd["hidden"] is True

    # Categories CRUD + reassignment on delete
    cat1 = c.create_category(name="Electricity", group="Home & Utilities")
    c.create_category(name="General Bills", group="Home & Utilities")

    tx = c.add_transaction(
        date="2026-11-03",
        account="Cash",
        amount=-60.0,
        payee="Power Co Grid",
        category="Electricity",
    )

    cat1_upd = c.update_category(category=cat1["id"], name="Power Bill", hidden=True)
    assert cat1_upd["name"] == "Power Bill"
    assert cat1_upd["hidden"] is True

    del_cat = c.delete_category(category="Power Bill", transfer_to_category="General Bills")
    assert del_cat["reassigned_transactions"] == 1

    txs = c.list_transactions(account="Cash")["transactions"]
    assert txs[0]["id"] == tx["id"]
    assert txs[0]["category_name"] == "General Bills"

    # Payees CRUD & Merge
    p1 = c.create_payee(name="Power Co Legacy")
    p2 = c.create_payee(name="Power Company", favorite=True)
    assert p2["favorite"] is True

    c.add_transaction(
        date="2026-11-04",
        account="Cash",
        amount=-70.0,
        payee="Power Co Legacy",
        category="General Bills",
    )

    merge_res = c.merge_payees(
        target_payee="Power Company",
        source_payees=["Power Co Grid", p1["id"]],
    )
    assert merge_res["reassigned_transactions"] == 2
    assert len(merge_res["merged_payee_ids"]) == 2

    payees = c.list_payees()
    payee_names = {p["name"] for p in payees}
    assert "Power Company" in payee_names
    assert "Power Co Legacy" not in payee_names
    assert "Power Co Grid" not in payee_names


def test_split_transactions_create_and_update(mock_client: ActualBudgetClient):
    c = mock_client
    c.create_account(name="Checking")
    c.create_category(name="Groceries", group="Living")
    c.create_category(name="Household", group="Living")
    c.create_category(name="Pharmacy", group="Living")

    # Mismatched split sum raises ValueError
    with pytest.raises(ValueError, match="does not match the sum of splits"):
        c.add_transaction(
            date="2026-11-10",
            account="Checking",
            amount=-100.0,
            payee="Target",
            splits=[
                {"amount": -60.0, "category": "Groceries"},
                {"amount": -30.0, "category": "Household"},
            ],
        )

    # Create valid split transaction
    parent = c.add_transaction(
        date="2026-11-10",
        account="Checking",
        amount=-100.0,
        payee="Target",
        notes="Weekly shopping",
        cleared=True,
        splits=[
            {"amount": -65.0, "category": "Groceries", "notes": "Food items"},
            {"amount": -35.0, "category": "Household", "notes": "Detergent"},
        ],
    )
    assert parent["is_parent"] is True
    assert parent["amount"] == -100.0
    assert len(parent["splits"]) == 2

    # Account balance reflects exact split sum (-100.0) without double counting parent
    bal = c.get_account_balance("Checking")
    assert bal["balance"] == -100.0

    # List transactions aggregates child splits accurately by category
    listed = c.list_transactions(account="Checking")
    assert listed["total"] == 2
    by_cat = {c_item["category_name"]: c_item["total_amount"] for c_item in listed["by_category"]}
    assert by_cat == {"Groceries": -65.0, "Household": -35.0}

    # Update split transaction with new 3-way split
    updated_parent = c.update_transaction(
        transaction_id=parent["id"],
        amount=-120.0,
        splits=[
            {"amount": -50.0, "category": "Groceries"},
            {"amount": -40.0, "category": "Household"},
            {"amount": -30.0, "category": "Pharmacy"},
        ],
    )
    assert updated_parent["amount"] == -120.0
    assert len(updated_parent["splits"]) == 3
    assert c.get_account_balance("Checking")["balance"] == -120.0


def test_client_unconfigured_error():
    c = ActualBudgetClient()
    c._actual = None
    with pytest.raises(ValueError, match="Actual Budget credentials are not fully configured"):
        c._get_actual()


def test_resolve_budget_file():
    from actual import RemoteFileListDTO

    f1 = RemoteFileListDTO(
        name="My Finances", fileId="uuid-1", groupId="sync-1", encryptKeyId=None, deleted=0
    )
    f2 = RemoteFileListDTO(
        name="Business", fileId="uuid-2", groupId="sync-2", encryptKeyId=None, deleted=0
    )

    assert resolve_budget_file("My Finances", [f1, f2]).name == "My Finances"
    assert resolve_budget_file("uuid-2", [f1, f2]).name == "Business"
    assert resolve_budget_file("business", [f1, f2]).name == "Business"
    assert resolve_budget_file("My-Finances-af43811", [f1, f2]).name == "My Finances"
    assert resolve_budget_file("", [f1]).name == "My Finances"
    assert resolve_budget_file("unknown-slug", [f1]).name == "My Finances"

    with pytest.raises(ValueError, match="Could not find budget 'Nonexistent'"):
        resolve_budget_file("Nonexistent", [f1, f2])

    with pytest.raises(ValueError, match="No active budget files found"):
        resolve_budget_file("any", [])


def test_monthly_budget_to_budget_income_and_carryover(mock_client: ActualBudgetClient):
    c = mock_client
    c.create_category(name="Salary", group="Income", is_income=True)
    c.create_category(name="Bonus", group="Income", is_income=True)
    c.create_category(name="Groceries", group="Living")
    c.create_category(name="Dining", group="Living")
    c.create_category(name="Emergency", group="Living")
    c.create_account(name="Checking", initial_balance=0.0)

    # Empty month returns zeros with all categories
    empty_res = c.get_monthly_budget(month="2026-09")
    assert empty_res["month"] == "2026-09"
    assert empty_res["budget_type"] == "envelope"
    assert empty_res["to_budget"] == 0.0
    assert empty_res["total_income"] == 0.0
    assert len(empty_res["budgets"]) == 3

    # Month 1 (2026-10): Receive 2000 income, budget 500 Groceries, spend 300 Groceries,
    # overspend 40 on unbudgeted Dining, and hold 200 for next month.
    c.add_transaction(
        date="2026-10-05",
        account="Checking",
        amount=2000.0,
        payee="Employer",
        category="Salary",
    )
    c.add_transaction(
        date="2026-10-12",
        account="Checking",
        amount=-300.0,
        payee="Market",
        category="Groceries",
    )
    c.add_transaction(
        date="2026-10-18",
        account="Checking",
        amount=-40.0,
        payee="Cafe",
        category="Dining",
    )
    c.set_category_budget(month="2026-10", category="Groceries", amount=500.0)
    mock_client._actual.session.add(ZeroBudgetMonths(id="2026-10", buffered=20000))
    mock_client._actual.session.flush()

    oct_res = c.get_monthly_budget(month="2026-10")
    assert oct_res["month"] == "2026-10"
    assert oct_res["total_income"] == 2000.0
    assert oct_res["available_funds"] == 2000.0
    assert oct_res["total_budgeted"] == 500.0
    assert oct_res["for_next_month"] == 200.0
    # 2000 income - 500 budgeted - 200 held for next month = 1300 to budget
    assert oct_res["to_budget"] == 1300.0
    assert oct_res["total_spent"] == -340.0
    assert oct_res["overspent"] == -40.0

    oct_income = {item["category_name"]: item["received"] for item in oct_res["income_categories"]}
    assert oct_income["Salary"] == 2000.0
    assert oct_income["Bonus"] == 0.0

    oct_cats = {item["category_name"]: item for item in oct_res["budgets"]}
    assert oct_cats["Groceries"]["available"] == 200.0
    assert oct_cats["Dining"]["budgeted"] == 0.0
    assert oct_cats["Dining"]["spent"] == -40.0
    assert oct_cats["Dining"]["available"] == -40.0

    # Month 2 (2026-11): Receive 500 Bonus, budget 400 Groceries
    c.add_transaction(
        date="2026-11-02",
        account="Checking",
        amount=500.0,
        payee="Employer",
        category="Bonus",
    )
    c.set_category_budget(month="2026-11", category="Groceries", amount=400.0)

    nov_res = c.get_monthly_budget(month="2026-11")
    assert nov_res["total_income"] == 500.0
    # from_last_month = Oct to_budget (1300) + Oct for_next_month (200) = 1500
    assert nov_res["from_last_month"] == 1500.0
    assert nov_res["available_funds"] == 2000.0
    assert nov_res["last_month_overspent"] == -40.0
    # to_budget = 2000 available_funds - 400 budgeted + (-40) last_month_overspent = 1560
    assert nov_res["to_budget"] == 1560.0

    nov_cats = {item["category_name"]: item for item in nov_res["budgets"]}
    assert nov_cats["Groceries"]["available"] == 600.0  # 200 carryover + 400 budgeted
    assert nov_cats["Dining"]["available"] == 0.0  # non-carryover negative reset


def test_monthly_budget_tracking_mode(mock_client: ActualBudgetClient):
    c = mock_client
    s = mock_client._actual.session
    s.add(Preferences(id="budgetType", value="report"))
    s.flush()

    c.create_category(name="Salary", group="Income", is_income=True)
    c.create_category(name="Rent", group="Housing")
    c.create_account(name="Checking", initial_balance=0.0)

    c.add_transaction(
        date="2026-11-01",
        account="Checking",
        amount=3000.0,
        payee="Employer",
        category="Salary",
    )
    c.add_transaction(
        date="2026-11-03",
        account="Checking",
        amount=-1200.0,
        payee="Landlord",
        category="Rent",
    )
    c.set_category_budget(month="2026-11", category="Rent", amount=1200.0)

    res = c.get_monthly_budget(month="2026-11")
    assert res["budget_type"] == "tracking"
    assert res["to_budget"] is None
    assert res["total_income"] == 3000.0
    assert res["total_spent"] == -1200.0
    assert res["overspent"] == 1800.0


def test_transfer_budget_between_categories(mock_client: ActualBudgetClient):
    c = mock_client
    c.create_category(name="Salary", group="Income", is_income=True)
    c.create_category(name="Groceries", group="Living")
    c.create_category(name="Dining", group="Living")
    c.create_category(name="Fun", group="Living")
    c.create_account(name="Checking", initial_balance=0.0)

    c.add_transaction(
        date="2026-10-01",
        account="Checking",
        amount=1000.0,
        payee="Employer",
        category="Salary",
    )
    c.set_category_budget(month="2026-10", category="Groceries", amount=300.0)
    c.set_category_budget(month="2026-11", category="Dining", amount=100.0)

    before = c.get_monthly_budget(month="2026-11")
    assert before["to_budget"] == 600.0

    # Move 75 from Groceries (which has 300 carryover, 0 budgeted in Nov) to Fun (0 budgeted)
    tr1 = c.transfer_budget(
        month="2026-11",
        source_category="Groceries",
        dest_category="Fun",
        amount=75.0,
    )
    assert tr1["month"] == "2026-11"
    assert tr1["amount"] == 75.0
    assert tr1["source_category"]["previous_budgeted"] == 0.0
    assert tr1["source_category"]["budgeted"] == -75.0
    assert tr1["source_category"]["available"] == 225.0
    assert tr1["dest_category"]["previous_budgeted"] == 0.0
    assert tr1["dest_category"]["budgeted"] == 75.0
    assert tr1["dest_category"]["available"] == 75.0
    assert tr1["to_budget"] == 600.0

    # Move 40 from Dining (100 budgeted) to Fun (75 budgeted)
    tr2 = c.transfer_budget(
        month="2026-11",
        source_category="Dining",
        dest_category="Fun",
        amount=40.0,
    )
    assert tr2["source_category"]["budgeted"] == 60.0
    assert tr2["dest_category"]["budgeted"] == 115.0
    assert tr2["to_budget"] == 600.0

    # Validation errors
    with pytest.raises(ValueError, match="positive number"):
        c.transfer_budget(month="2026-11", source_category="Dining", dest_category="Fun", amount=0.0)
    with pytest.raises(ValueError, match="positive number"):
        c.transfer_budget(month="2026-11", source_category="Dining", dest_category="Fun", amount=-10.0)
    with pytest.raises(ValueError, match="must be different"):
        c.transfer_budget(month="2026-11", source_category="Dining", dest_category="dining", amount=10.0)
    with pytest.raises(ValueError, match="income category"):
        c.transfer_budget(month="2026-11", source_category="Salary", dest_category="Fun", amount=10.0)
    with pytest.raises(ValueError, match="income category"):
        c.transfer_budget(month="2026-11", source_category="Fun", dest_category="Salary", amount=10.0)
    with pytest.raises(ValueError, match="not found"):
        c.transfer_budget(month="2026-11", source_category="Unknown", dest_category="Fun", amount=10.0)


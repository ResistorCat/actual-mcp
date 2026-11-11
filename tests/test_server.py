from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from actual.database import Accounts, Categories, CategoryGroups, Payees
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
    account = Accounts(id="acc-uuid-1", name="Checking", closed=0, offbudget=0)
    cat_group = CategoryGroups(id="grp-1", name="Food", is_income=0, hidden=False, sort_order=0)
    category = Categories(id="cat-uuid-1", name="Groceries", tombstone=0, cat_group=cat_group.id)
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
        "get_categories",
        "get_budget_month",
        "get_transactions",
        "create_transaction",
        "update_transaction",
        "delete_transaction",
        "set_budget_amount",
        "sync_budget",
    }
    assert expected_tools.issubset(tool_names)


def test_client_crud_operations(memory_db: Session):
    acct = Accounts(id="acc-test", name="Wallet", closed=0, offbudget=0)
    cat = Categories(id="cat-test", name="Snacks", tombstone=0)
    memory_db.add_all([acct, cat])
    memory_db.commit()

    # Mock Actual instance
    mock_actual = MagicMock()
    mock_actual.session = memory_db
    mock_actual.sync = MagicMock()
    mock_actual.commit = MagicMock()

    c = ActualBudgetClient()
    c._actual = mock_actual

    # 1. Add transaction
    tx_dict = c.add_transaction(
        date="2026-11-10",
        account="Wallet",
        amount=-15.75,
        payee="Corner Store",
        category="Snacks",
        notes="Coffee and pastry",
        cleared=True,
    )
    assert tx_dict["amount"] == -15.75
    assert tx_dict["account_name"] == "Wallet"
    assert tx_dict["notes"] == "Coffee and pastry"
    assert tx_dict["cleared"] is True
    assert mock_actual.commit.call_count == 1

    tx_id = tx_dict["id"]

    # 2. List transactions
    txs = c.list_transactions(account="Wallet")
    assert len(txs) == 1
    assert txs[0]["id"] == tx_id
    assert txs[0]["amount"] == -15.75

    # 3. Update transaction
    updated = c.update_transaction(
        transaction_id=tx_id,
        amount=-18.00,
        notes="Updated note",
    )
    assert updated["amount"] == -18.00
    assert updated["notes"] == "Updated note"

    # 4. Delete transaction
    del_res = c.delete_transaction(transaction_id=tx_id)
    assert del_res["status"] == "deleted"

    # Confirm deleted transactions are filtered out in listing
    remaining = c.list_transactions(account="Wallet")
    assert len(remaining) == 0


def test_client_categories_and_accounts(memory_db: Session):
    acct_active = Accounts(id="acc-act", name="Active Account", closed=0, offbudget=0)
    acct_closed = Accounts(id="acc-cls", name="Old Account", closed=1, offbudget=0)
    grp = CategoryGroups(id="grp-exp", name="Monthly Expenses", is_income=0, hidden=False, sort_order=0)
    cat = Categories(id="cat-rent", name="Rent", tombstone=0, cat_group=grp.id)

    memory_db.add_all([acct_active, acct_closed, grp, cat])
    memory_db.commit()

    mock_actual = MagicMock()
    mock_actual.session = memory_db
    mock_actual.sync = MagicMock()

    c = ActualBudgetClient()
    c._actual = mock_actual

    # Test list accounts
    accounts_active = c.list_accounts(include_closed=False)
    assert len(accounts_active) == 1
    assert accounts_active[0]["name"] == "Active Account"

    accounts_all = c.list_accounts(include_closed=True)
    assert len(accounts_all) == 2

    # Test list categories
    categories = c.list_categories(include_income=True)
    assert len(categories) >= 1
    assert any(g["group_name"] == "Monthly Expenses" for g in categories)


def test_client_unconfigured_error():
    c = ActualBudgetClient()
    c._actual = None
    # Settings default is unconfigured when password and file are empty
    with pytest.raises(ValueError, match="Actual Budget credentials are not fully configured"):
        c._get_actual()


def test_resolve_budget_file():
    from actual import RemoteFileListDTO

    f1 = RemoteFileListDTO(name="My Finances", fileId="uuid-1", groupId="sync-1", encryptKeyId=None, deleted=0)
    f2 = RemoteFileListDTO(name="Business", fileId="uuid-2", groupId="sync-2", encryptKeyId=None, deleted=0)

    # 1. Exact match by name
    assert resolve_budget_file("My Finances", [f1, f2]).name == "My Finances"
    # 2. Exact match by file_id
    assert resolve_budget_file("uuid-2", [f1, f2]).name == "Business"
    # 3. Case-insensitive match
    assert resolve_budget_file("business", [f1, f2]).name == "Business"
    # 4. Normalized slug match
    assert resolve_budget_file("My-Finances-af43811", [f1, f2]).name == "My Finances"
    # 5. Single active file auto-selection
    assert resolve_budget_file("", [f1]).name == "My Finances"
    assert resolve_budget_file("unknown-slug", [f1]).name == "My Finances"

    # 6. Not found with multiple files
    with pytest.raises(ValueError, match="Could not find budget 'Nonexistent'"):
        resolve_budget_file("Nonexistent", [f1, f2])

    # 7. No active files
    with pytest.raises(ValueError, match="No active budget files found"):
        resolve_budget_file("any", [])

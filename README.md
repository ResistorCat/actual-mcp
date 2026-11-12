# Actual Budget MCP Server (`actual-mcp`)

A lightweight, production-ready [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for [Actual Budget](https://actualbudget.org/) built in Python using **[FastMCP 4](https://gofastmcp.com/)**, **[actualpy](https://github.com/bvanelli/actualpy)**, and **[uv](https://docs.astral.sh/uv/)**.

Designed for deployment on Docker / Coolify with built-in Bearer authentication, `/health` endpoint for infrastructure monitoring, and incremental local SQLite caching.

---

## Features

- **Protocol & Transport**: Implements FastMCP 4 over **Streamable HTTP** (`/mcp`) and stdio.
- **Authentication**: Bearer token authentication via `StaticTokenVerifier` (optional via `MCP_API_KEY`).
- **Health Checks**: Public unauthenticated `/health` endpoint for Docker/Coolify/Traefik probes.
- **Zero-Latency Read Cache**: Incremental synchronization with Actual Budget server using local SQLite storage (`/data`).
- **Comprehensive Budget, Account & Transaction Tools**:
  - **Accounts & Reconciliation**:
    - `get_accounts`: List accounts (checking, savings, credit cards, investments) and their balances.
    - `create_account`: Create on-budget or off-budget accounts with an optional initial balance.
    - `update_account`: Rename accounts, toggle `off_budget`, or reopen/close accounts.
    - `close_account`: Close an account and optionally transfer any remaining balance to another account.
    - `get_account_balance`: Get total, cleared, reconciled, and uncleared balances optionally as of a cutoff date.
    - `reconcile_account`: Reconcile an account as of a date against a target statement balance with automatic adjustment and transaction locking (`reconciled=True`).
  - **Transactions, Splits, Transfers & Bulk Import**:
    - `get_transactions`: Search, filter, and paginate (`offset`, `limit`) transactions with automatic aggregations by category (`by_category`) and month (`by_month`).
    - `create_transaction`: Register single or split (`splits`) expenses/income with payee, category, cleared, and reconciled flags.
    - `update_transaction`: Update date, account, amount, payee, category, notes, cleared, reconciled, or child `splits`.
    - `delete_transaction`: Soft-delete transactions (automatically cleaning up child splits or linked transfer peers).
    - `create_transfer`: Create linked double-entry transfers between accounts.
    - `import_transactions`: Bulk import transactions with native deduplication (`imported_id` and ±7 day fuzzy matching).
  - **Categories, Category Groups & Payees**:
    - `get_categories`, `create_category`, `update_category`, `delete_category` (with optional transaction reassignment).
    - `create_category_group`, `update_category_group`, `delete_category_group`.
    - `get_payees`, `create_payee`, `update_payee`, `delete_payee`, `merge_payees`.
  - **Budget & Sync**:
    - `get_budget_month`: Monthly budget breakdown (budgeted, spent, and available balances).
    - `set_budget_amount`: Assign or update monthly budget allocations.
    - `sync_budget`: Force an immediate incremental sync.

---

## Configuration

Set the following environment variables (or define them in `.env`):

| Variable | Description | Default |
|---|---|---|
| `ACTUAL_URL` | URL of the Actual Budget server | `http://localhost:5006` |
| `ACTUAL_PASSWORD` | Password for your Actual Budget server | **Required** |
| `ACTUAL_FILE` | Budget file ID or budget file name | **Required** |
| `ACTUAL_ENCRYPTION_PASSWORD` | Optional E2E encryption password for the file | `None` |
| `ACTUAL_DATA_DIR` | Directory for local SQLite cache | `/data` (or `./data`) |
| `ACTUAL_VERIFY_SSL` | Verify SSL certificates | `true` |
| `CF_ACCESS_CLIENT_ID` | Optional: Cloudflare Access Service Token Client ID | `None` |
| `CF_ACCESS_CLIENT_SECRET` | Optional: Cloudflare Access Service Token Client Secret | `None` |
| `MCP_API_KEY` | Bearer token required for clients to access `/mcp` | `None` (open if unset) |
| `HOST` | Bind host address | `0.0.0.0` |
| `PORT` | Bind port number | `8000` |
| `TRANSPORT` | Transport mode (`http` or `stdio`) | `http` |

---

## Running with `uv`

### 1. Install dependencies
```bash
uv sync
```

### 2. Run automated tests
```bash
uv run pytest -v
uv run ruff check .
```

### 3. Run the server locally
```bash
uv run actual-mcp
```

Access the health check:
```bash
curl http://localhost:8000/health
```

---

## Docker & Coolify Deployment

### Build and Run Docker Container
```bash
docker build -t actual-mcp .
docker run -d \
  --name actual-mcp \
  -p 8000:8000 \
  -v actual_mcp_data:/data \
  -e ACTUAL_URL="http://actual-server:5006" \
  -e ACTUAL_PASSWORD="your-password" \
  -e ACTUAL_FILE="My-Finances" \
  -e MCP_API_KEY="your-secret-bearer-token" \
  actual-mcp
```

---

## Connecting MCP Clients

For remote HTTP clients (Claude Desktop, Cursor, Rocket, etc.):

- **Endpoint URL**: `https://your-mcp-domain.example.com/mcp`
- **Headers**:
  - `Authorization`: `Bearer <MCP_API_KEY>`
  - `CF-Access-Client-Id`: `<Client ID>` *(if behind Cloudflare Access)*
  - `CF-Access-Client-Secret`: `<Client Secret>` *(if behind Cloudflare Access)*

---

## License

MIT

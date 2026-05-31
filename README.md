# Wealth Tracker — Rebuilt

Replaced Streamlit + CSV files with:
- **React** frontend (nginx) — sortable tables, inline editing, charts
- **FastAPI** backend — clean REST API
- **PostgreSQL** (Docker) — persistent, queryable data

## Quick Start

```bash
docker compose up --build
```

- **App (UI):** http://localhost:3000
- **API docs:** http://localhost:8000/docs
- **PostgreSQL:** localhost:5432 (user: finance, pass: finance, db: financedb)

## Project Structure

```
.
├── docker-compose.yml
├── init-scripts/
│   └── 01_init.sql          # Schema + seed data (runs once on first boot)
├── backend/
│   ├── main.py              # FastAPI app
│   ├── requirements.txt
│   └── Dockerfile
└── frontend/
    ├── index.html           # React single-page app
    ├── nginx.conf
    └── Dockerfile
```

## Data Storage

| Data | Storage | Why |
|---|---|---|
| Transactions | PostgreSQL | ACID, queryable, safe concurrent writes |
| Starting balances | PostgreSQL | Same |
| Fixed templates | PostgreSQL | Same |
| **Bank statements** | **`data/bank_statements.csv`** | **External automation service owns this file** |

### bank_statements.csv

Your automation service writes to `./data/bank_statements.csv` on the host exactly as before — nothing changes there. The backend mounts that directory **read-only** and reads the CSV fresh on every `/statements` request, so the UI always reflects whatever the automation service last wrote.

Expected CSV columns (same as original):
```
extracted_at, email_date, bank, sender, subject, statement_balance, minimum_payment, payment_due_date, msg_id
```

If the file doesn't exist yet the Statements page shows an empty table and no error.

## Adding New Accounts

Edit `ACCOUNTS` array in `frontend/index.html`:
```js
const ACCOUNTS = ['Wealthfront', 'Chase', 'DCU', 'NewBank'];
```

## Database Access

```bash
docker exec -it finance_db psql -U finance -d financedb
```

Useful queries:
```sql
-- All transactions this month
SELECT * FROM transactions WHERE date_trunc('month', date) = date_trunc('month', CURRENT_DATE);

-- Spending by category
SELECT category, SUM(amount) FROM transactions WHERE type = 'Expense' GROUP BY category ORDER BY 2 DESC;
```

## Resetting Data

To wipe and re-seed:
```bash
docker compose down -v
docker compose up --build
```

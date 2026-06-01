from fastapi import FastAPI, HTTPException, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from datetime import date, datetime
import os
import psycopg2
from psycopg2.extras import RealDictCursor
import pandas as pd
import json
import pytz
import io

EASTERN = pytz.timezone("America/New_York")

def now_eastern():
    """Current datetime in US/Eastern time."""
    return datetime.now(EASTERN)

# Path to the bank_statements.csv kept in sync by the external automation service.
# Mount this file into the container via docker-compose volumes.
BS_CSV_PATH = os.getenv("BANK_STATEMENTS_CSV", "/data/bank_statements.csv")

app = FastAPI(title="Wealth Tracker API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://finance:finance@db:5432/financedb")

def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

# ── Models ──────────────────────────────────────────────────────────────
class Transaction(BaseModel):
    date: str           # accepted as string, parsed in endpoint
    description: str
    amount: float
    category: str
    account: str
    type: str  # "Income" | "Expense"

class TransactionUpdate(BaseModel):
    date: Optional[str] = None      # accept any string, parsed manually below
    description: Optional[str] = None
    amount: Optional[float] = None
    category: Optional[str] = None
    account: Optional[str] = None
    type: Optional[str] = None

class Balance(BaseModel):
    month: date
    account: str
    amount: float

class Template(BaseModel):
    description: str
    amount: float
    due_day: int
    category: str
    account: str
    type: str

class BankStatement(BaseModel):
    extracted_at: Optional[datetime] = None
    email_date: Optional[str] = None
    bank: str
    sender: Optional[str] = None
    subject: Optional[str] = None
    statement_balance: float
    minimum_payment: Optional[float] = None
    payment_due_date: date
    msg_id: Optional[str] = None

# ── Transactions ─────────────────────────────────────────────────────────
@app.get("/transactions")
def list_transactions(year: int = Query(None), month: int = Query(None)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if year and month:
                cur.execute("""
                    SELECT * FROM transactions
                    WHERE EXTRACT(YEAR FROM date) = %s AND EXTRACT(MONTH FROM date) = %s
                    ORDER BY date DESC
                """, (year, month))
            else:
                cur.execute("SELECT * FROM transactions ORDER BY date DESC")
            rows = cur.fetchall()
            return [dict(r) for r in rows]
    finally:
        conn.close()

@app.post("/transactions", status_code=201)
def create_transaction(tx: Transaction):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            from datetime import datetime as dt
            try:
                parsed_date = dt.strptime(tx.date, "%Y-%m-%d").date() if "-" in tx.date else dt.strptime(tx.date, "%m/%d/%Y").date()
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail=f"Invalid date format: {tx.date}")
            cur.execute("""
                INSERT INTO transactions (date, description, amount, category, account, type)
                VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
            """, (parsed_date, tx.description, tx.amount, tx.category, tx.account, tx.type))
            conn.commit()
            return dict(cur.fetchone())
    finally:
        conn.close()

@app.put("/transactions/{tx_id}")
def update_transaction(tx_id: int, tx: TransactionUpdate):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            data = tx.dict(exclude_none=True)
            if not data:
                raise HTTPException(status_code=400, detail="No fields to update")

            # Parse date string safely — accept YYYY-MM-DD or MM/DD/YYYY
            if "date" in data:
                raw = data["date"]
                try:
                    from datetime import datetime as dt
                    if "/" in str(raw):
                        data["date"] = dt.strptime(raw, "%m/%d/%Y").date()
                    else:
                        data["date"] = dt.strptime(raw, "%Y-%m-%d").date()
                except (ValueError, TypeError) as e:
                    raise HTTPException(status_code=400, detail=f"Invalid date format: {raw}")

            set_clause = ", ".join(f"{k} = %s" for k in data)
            cur.execute(
                f"UPDATE transactions SET {set_clause} WHERE id = %s RETURNING *",
                list(data.values()) + [tx_id]
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Transaction not found")
            conn.commit()
            return dict(row)
    finally:
        conn.close()

@app.delete("/transactions/{tx_id}", status_code=204)
def delete_transaction(tx_id: int):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM transactions WHERE id = %s", (tx_id,))
            conn.commit()
    finally:
        conn.close()

# ── Balances ──────────────────────────────────────────────────────────────
@app.get("/balances")
def list_balances(year: int = Query(None), month: int = Query(None)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if year and month:
                cur.execute("""
                    SELECT * FROM starting_balances
                    WHERE EXTRACT(YEAR FROM month) = %s AND EXTRACT(MONTH FROM month) = %s
                """, (year, month))
            else:
                cur.execute("SELECT * FROM starting_balances ORDER BY month DESC")
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

@app.post("/balances")
def upsert_balances(balances: List[Balance]):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            for b in balances:
                cur.execute("""
                    INSERT INTO starting_balances (month, account, amount)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (month, account) DO UPDATE SET amount = EXCLUDED.amount
                    RETURNING *
                """, (b.month, b.account, b.amount))
            conn.commit()
        return {"status": "saved"}
    finally:
        conn.close()

# ── Templates ────────────────────────────────────────────────────────────
@app.get("/templates")
def list_templates():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM fixed_templates ORDER BY due_day")
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

@app.put("/templates")
def save_templates(templates: List[Template]):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM fixed_templates")
            for t in templates:
                cur.execute("""
                    INSERT INTO fixed_templates (description, amount, due_day, category, account, type)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (t.description, t.amount, t.due_day, t.category, t.account, t.type))
            conn.commit()
        return {"status": "saved"}
    finally:
        conn.close()

@app.post("/templates/sync")
def sync_templates(year: int = Query(...), month: int = Query(...)):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM fixed_templates")
            templates = [dict(r) for r in cur.fetchall()]
            cur.execute("""
                SELECT description FROM transactions
                WHERE EXTRACT(YEAR FROM date) = %s AND EXTRACT(MONTH FROM date) = %s
            """, (year, month))
            existing = {r['description'] for r in cur.fetchall()}

            added = 0
            for t in templates:
                if t['description'] not in existing:
                    try:
                        due_date = date(year, month, int(float(t['due_day'])))
                    except (ValueError, TypeError):
                        import calendar
                        last_day = calendar.monthrange(year, month)[1]
                        due_date = date(year, month, last_day)
                    cur.execute("""
                        INSERT INTO transactions (date, description, amount, category, account, type)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (due_date, t['description'], t['amount'], t['category'], t['account'], t['type']))
                    added += 1
            conn.commit()
        return {"added": added}
    finally:
        conn.close()

# ── Bank Statements (CSV — managed by external automation service) ────────────
def _read_statements_csv() -> pd.DataFrame:
    """Read bank_statements.csv written by the external automation service.
    Returns an empty DataFrame with the expected columns if the file is missing."""
    expected_cols = [
        "extracted_at", "email_date", "bank", "sender", "subject",
        "statement_balance", "minimum_payment", "payment_due_date", "msg_id"
    ]
    if not os.path.exists(BS_CSV_PATH):
        return pd.DataFrame(columns=expected_cols)
    df = pd.read_csv(BS_CSV_PATH)
    df["payment_due_date"] = pd.to_datetime(df["payment_due_date"], errors="coerce")
    df["statement_balance"] = pd.to_numeric(df["statement_balance"], errors="coerce").fillna(0.0)
    df["minimum_payment"] = pd.to_numeric(df["minimum_payment"], errors="coerce")
    if "extracted_at" in df.columns:
        df["extracted_at"] = pd.to_datetime(df["extracted_at"], errors="coerce")
    return df

@app.get("/statements")
def list_statements(year: int = Query(None), month: int = Query(None)):
    """Return statements from the automation-managed CSV, optionally filtered to a month."""
    df = _read_statements_csv()
    if df.empty:
        return []

    if year and month:
        df = df[
            (df["payment_due_date"].dt.year == year) &
            (df["payment_due_date"].dt.month == month)
        ]
        # Keep only the most-recent entry per bank (automation may append duplicates)
        if "extracted_at" in df.columns:
            df = df.sort_values("extracted_at", ascending=False).drop_duplicates("bank")

    # Serialise dates to ISO strings for JSON
    df["payment_due_date"] = df["payment_due_date"].dt.strftime("%Y-%m-%d")
    if "extracted_at" in df.columns:
        df["extracted_at"] = df["extracted_at"].astype(str)

    return df.where(pd.notna(df), None).to_dict(orient="records")

@app.post("/statements/sync")
def sync_statements(year: int = Query(...), month: int = Query(...)):
    """Push the latest CSV statements for the given month into the transactions table."""
    df = _read_statements_csv()
    if df.empty:
        return {"synced": 0, "detail": "bank_statements.csv not found or empty"}

    # Filter to the requested month and keep latest per bank
    month_df = df[
        (df["payment_due_date"].dt.year == year) &
        (df["payment_due_date"].dt.month == month)
    ]
    if "extracted_at" in month_df.columns:
        month_df = month_df.sort_values("extracted_at", ascending=False).drop_duplicates("bank")

    if month_df.empty:
        return {"synced": 0, "detail": f"No statements found for {year}-{month:02d}"}

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            synced = 0
            for _, s in month_df.iterrows():
                due_date = s["payment_due_date"].date() if hasattr(s["payment_due_date"], "date") else s["payment_due_date"]
                cur.execute("""
                    SELECT id FROM transactions
                    WHERE EXTRACT(YEAR FROM date) = %s AND EXTRACT(MONTH FROM date) = %s
                    AND description = %s
                """, (year, month, s["bank"]))
                existing = cur.fetchone()
                if existing:
                    # Only update amount and date — never overwrite category the user has set
                    cur.execute("""
                        UPDATE transactions SET amount = %s, date = %s WHERE id = %s
                    """, (float(s["statement_balance"]), due_date, existing["id"]))
                else:
                    # New entry: leave category empty so user can set it manually
                    cur.execute("""
                        INSERT INTO transactions (date, description, amount, category, account, type)
                        VALUES (%s, %s, %s, '', 'Chase', 'Expense')
                    """, (due_date, s["bank"], float(s["statement_balance"])))
                synced += 1
            conn.commit()
        return {"synced": synced}
    finally:
        conn.close()

# ── Summary ───────────────────────────────────────────────────────────────
@app.get("/summary")
def get_summary(year: int = Query(...), month: int = Query(...)):
    conn = get_conn()
    try:
        # Use Eastern time for "today" so live/forecast splits are correct
        today_eastern = now_eastern().date()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT 
                    SUM(CASE WHEN type = 'Income' AND category NOT ILIKE '%%transfer%%' THEN amount ELSE 0 END) as income,
                    SUM(CASE WHEN type = 'Expense' AND category NOT ILIKE '%%transfer%%' THEN amount ELSE 0 END) as expenses,
                    account,
                    SUM(CASE WHEN type = 'Income'  AND date <= %s THEN amount ELSE 0 END) as past_income,
                    SUM(CASE WHEN type = 'Expense' AND date <= %s THEN amount ELSE 0 END) as past_expenses,
                    SUM(CASE WHEN type = 'Expense' AND date >  %s THEN amount ELSE 0 END) as future_expenses,
                    SUM(CASE WHEN type = 'Income'  AND date >  %s THEN amount ELSE 0 END) as future_income,
                    SUM(CASE WHEN type = 'Expense' THEN amount ELSE 0 END) as total_payments
                FROM transactions
                WHERE EXTRACT(YEAR FROM date) = %s AND EXTRACT(MONTH FROM date) = %s
                GROUP BY account
            """, (today_eastern, today_eastern, today_eastern, today_eastern, year, month))
            by_account = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT category, SUM(amount) as total
                FROM transactions
                WHERE EXTRACT(YEAR FROM date) = %s AND EXTRACT(MONTH FROM date) = %s
                AND type = 'Expense' AND category NOT ILIKE '%%transfer%%'
                GROUP BY category ORDER BY total DESC
            """, (year, month))
            by_category = [dict(r) for r in cur.fetchall()]

            return {"by_account": by_account, "by_category": by_category}
    finally:
        conn.close()

def parse_checking_csv(df: pd.DataFrame) -> List[dict]:
    # Try to find Date column
    date_col = None
    for c in df.columns:
        c_lower = str(c).lower()
        if "date" in c_lower:
            date_col = c
            break
    if date_col is None:
        date_col = df.columns[0]
        
    # Try to find Description column
    desc_col = None
    for c in df.columns:
        c_lower = str(c).lower()
        if any(x in c_lower for x in ["desc", "payee", "name", "detail", "memo"]):
            desc_col = c
            break
    if desc_col is None:
        desc_col = df.columns[1] if len(df.columns) > 1 else df.columns[0]
        
    # Try to find Amount column
    amount_col = None
    for c in df.columns:
        c_lower = str(c).lower()
        if any(x in c_lower for x in ["amount", "value", "debit", "credit", "price"]):
            amount_col = c
            break
    if amount_col is None:
        amount_col = df.columns[2] if len(df.columns) > 2 else df.columns[0]

    transactions = []
    for _, row in df.iterrows():
        try:
            raw_date = str(row[date_col]).strip()
            raw_desc = str(row[desc_col]).strip()
            raw_amt = str(row[amount_col]).strip()
            
            if pd.isna(raw_date) or pd.isna(raw_desc) or pd.isna(raw_amt):
                continue
            if raw_date == "nan" or raw_desc == "nan" or raw_amt == "nan":
                continue
            
            # Parse Date
            parsed_date = None
            for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y"]:
                try:
                    parsed_date = datetime.strptime(raw_date, fmt).date()
                    break
                except ValueError:
                    continue
            if parsed_date is None:
                try:
                    parsed_date = pd.to_datetime(raw_date).date()
                except Exception:
                    continue
            
            # Parse Amount
            clean_amt = raw_amt.replace("$", "").replace(",", "").strip()
            if clean_amt.startswith("(") and clean_amt.endswith(")"):
                amt_val = -float(clean_amt[1:-1])
            else:
                amt_val = float(clean_amt)
                
            if pd.isna(amt_val) or amt_val == 0:
                continue
                
            if amt_val < 0:
                tx_type = "Expense"
                tx_amt = abs(amt_val)
            else:
                tx_type = "Income"
                tx_amt = amt_val
                
            # Guess Category based on description
            category = ""
            desc_lower = raw_desc.lower()
            if any(x in desc_lower for x in ["netflix", "spotify", "hulu", "disney", "youtube"]):
                category = "Entertainment"
            elif any(x in desc_lower for x in ["grocer", "whole foods", "trader joe", "kroger", "supermarket", "safeway"]):
                category = "Groceries"
            elif any(x in desc_lower for x in ["restaurant", "cafe", "mcdonald", "starbucks", "dunkin", "pizza", "burger", "uber eats", "doordash"]):
                category = "Dining"
            elif any(x in desc_lower for x in ["gas", "shell", "mobil", "chevron", "exxon", "bp", "fuel", "chevron"]):
                category = "Transportation"
            elif any(x in desc_lower for x in ["utilities", "electric", "power", "water", "trash", "comcast", "verizon", "at&t", "internet"]):
                category = "Utilities"
            elif any(x in desc_lower for x in ["salary", "paycheck", "payroll", "direct deposit"]):
                category = "Income"
            elif any(x in desc_lower for x in ["transfer", "wire"]):
                category = "Transfer"
            elif any(x in desc_lower for x in ["mortgage", "rent", "hoa"]):
                category = "Housing"
            elif any(x in desc_lower for x in ["discover", "chase card", "capital one", "citi"]):
                category = "Credit Card Bill"
                
            transactions.append({
                "date": parsed_date.strftime("%Y-%m-%d"),
                "description": raw_desc,
                "amount": tx_amt,
                "type": tx_type,
                "category": category
            })
        except Exception:
            continue
            
    return transactions

def find_duplicate_transaction(tx_date, tx_desc, tx_amount, tx_type, existing_txs):
    tx_desc_lower = tx_desc.lower()
    for ext in existing_txs:
        if ext['type'] != tx_type:
            continue
        if abs(float(ext['amount']) - tx_amount) >= 0.01:
            continue
        
        ext_desc_lower = ext['description'].lower()
        if ext_desc_lower == tx_desc_lower:
            return ext
        if ext_desc_lower in tx_desc_lower or tx_desc_lower in ext_desc_lower:
            return ext
            
        common_banks = ['discover', 'capital one', 'chase', 'citi', 'amazon', 'robinhood', 'wealthfront', 'amex', 'fidelity', 'boa', 'bank of america']
        for bank in common_banks:
            if bank in ext_desc_lower and bank in tx_desc_lower:
                return ext
                
    return None

class TransactionImportList(BaseModel):
    transactions: List[Transaction]

@app.post("/transactions/upload-checking/preview")
async def preview_checking_upload(
    year: int = Query(...),
    month: int = Query(...),
    account: str = Query("Chase"),
    file: UploadFile = File(...)
):
    try:
        contents = await file.read()
        df = pd.read_csv(io.BytesIO(contents))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse CSV: {str(e)}")
        
    parsed = parse_checking_csv(df)
    
    filtered_parsed = []
    for tx in parsed:
        dt_val = datetime.strptime(tx["date"], "%Y-%m-%d")
        if dt_val.year == year and dt_val.month == month:
            tx["account"] = account
            filtered_parsed.append(tx)
            
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM transactions
                WHERE EXTRACT(YEAR FROM date) = %s AND EXTRACT(MONTH FROM date) = %s
            """, (year, month))
            existing_txs = [dict(r) for r in cur.fetchall()]
            
            for tx in filtered_parsed:
                duplicate = find_duplicate_transaction(tx["date"], tx["description"], tx["amount"], tx["type"], existing_txs)
                if duplicate:
                    tx["is_duplicate"] = True
                    tx["duplicate_reason"] = f"Matches '{duplicate['description']}' (${float(duplicate['amount']):.2f}) on {duplicate['date']}"
                else:
                    tx["is_duplicate"] = False
                    tx["duplicate_reason"] = None
    finally:
        conn.close()
        
    return filtered_parsed

@app.post("/transactions/upload-checking/import")
def import_checking_transactions(payload: TransactionImportList):
    conn = get_conn()
    imported = 0
    try:
        with conn.cursor() as cur:
            for tx in payload.transactions:
                from datetime import datetime as dt
                try:
                    parsed_date = dt.strptime(tx.date, "%Y-%m-%d").date() if "-" in tx.date else dt.strptime(tx.date, "%m/%d/%Y").date()
                except (ValueError, TypeError):
                    raise HTTPException(status_code=400, detail=f"Invalid date format: {tx.date}")
                cur.execute("""
                    INSERT INTO transactions (date, description, amount, category, account, type)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (parsed_date, tx.description, tx.amount, tx.category, tx.account, tx.type))
                imported += 1
            conn.commit()
        return {"imported": imported}
    finally:
        conn.close()

@app.get("/health")
def health():
    return {"status": "ok"}

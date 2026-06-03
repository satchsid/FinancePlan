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

def simplify_desc(desc: str) -> str:
    d = desc.lower().strip()
    if "target" in d:
        return "target"
    if "affirm" in d:
        return "affirm"
    if "brookwood" in d:
        return "hoa"
    if "freedom" in d or "mortgage" in d:
        return "mortgage"
    if "payroll" in d or "alight" in d:
        return "pay"
    if "interest" in d:
        return "interest"
    words = [w for w in d.split() if len(w) > 1]
    return words[0] if words else d

def simplify_ledger_desc(desc: str) -> str:
    d = desc.lower().strip()
    if "target" in d:
        return "target"
    if "affirm" in d:
        return "affirm"
    if "hoa" in d:
        return "hoa"
    if "mortgage" in d or "freedom" in d:
        return "mortgage"
    if "pay" in d or "salary" in d:
        return "pay"
    if "interest" in d:
        return "interest"
    words = [w for w in d.split() if len(w) > 1]
    return words[0] if words else d

def find_subset_sum(numbers, target, tolerance=0.01):
    n = len(numbers)
    def backtrack(index, current_sum, path):
        if abs(current_sum - target) <= tolerance:
            return path
        if index >= n:
            return None
        # Try including
        val, item = numbers[index]
        res = backtrack(index + 1, current_sum + val, path + [item])
        if res is not None:
            return res
        # Try excluding
        return backtrack(index + 1, current_sum, path)
    return backtrack(0, 0.0, [])

@app.post("/transactions/upload-checking")
def upload_checking(
    file: UploadFile = File(...),
    year: int = Query(...),
    month: int = Query(...),
    account: str = Query("Wealthfront"),
    commit: bool = Query(False)
):
    try:
        content = file.file.read().decode('utf-8')
        df = pd.read_csv(io.StringIO(content), index_col=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse CSV file: {str(e)}")

    df.columns = [c.strip() for c in df.columns]
    
    # Dynamically locate columns to support both Wealthfront and Chase headers
    date_col = None
    for col in df.columns:
        if "date" in col.lower():
            date_col = col
            break
            
    desc_col = None
    for col in df.columns:
        if "description" in col.lower() or "desc" in col.lower():
            desc_col = col
            break
            
    amount_col = None
    for col in df.columns:
        if "amount" in col.lower():
            amount_col = col
            break

    type_col = None
    # "Details" specifies CREDIT/DEBIT in Chase CSVs and maps directly to Deposit/Withdrawal type
    if "details" in [c.lower() for c in df.columns]:
        for col in df.columns:
            if col.lower() == "details":
                type_col = col
                break
    else:
        for col in df.columns:
            if col.lower() == "type":
                type_col = col
                break

    if not date_col or not desc_col or not amount_col:
        raise HTTPException(
            status_code=400, 
            detail=f"CSV must contain at least date, description, and amount columns. Found: {list(df.columns)}"
        )

    checking_txs = []
    import re
    for idx, row in df.iterrows():
        raw_date = row[date_col]
        desc = row[desc_col]
        tx_type = row[type_col] if type_col else ""
        try:
            amount = float(row[amount_col])
        except ValueError:
            amount = 0.0
            
        try:
            parsed_date = pd.to_datetime(raw_date).date()
        except Exception:
            raise HTTPException(status_code=400, detail=f"Invalid date format in CSV: {raw_date}")

        # Collapse multiple spaces in description (common in Chase files)
        clean_desc = re.sub(r'\s+', ' ', str(desc)).strip()

        # Map type dynamically based on string content or sign
        tx_type_lower = str(tx_type).lower() if tx_type else ""
        if "deposit" in tx_type_lower or "interest" in tx_type_lower or "credit" in tx_type_lower or amount > 0:
            mapped_type = "Income"
        else:
            mapped_type = "Expense"

        checking_txs.append({
            "id": idx,
            "date": parsed_date,
            "description": clean_desc,
            "type": mapped_type,
            "amount": amount
        })

    # Filter to selected month/year
    checking_txs = [tx for tx in checking_txs if tx['date'].year == year and tx['date'].month == month]

    import re
    patterns = [
        r'\bciti\b',
        r'\brobinhood\b',
        r'\bdiscover\b',
        r'\bamex\b',
        r'\bamazon\b',
        r'\bamz\b',
        r'\bchase\b',
        r'\bcapital one\b',
        r'\bcapitalone\b',
        r'\bbank of america\b',
        r'\bboa\b',
        r'\bapplecard\b',
        r'\bapple card\b'
    ]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM transactions
                WHERE EXTRACT(YEAR FROM date) = %s AND EXTRACT(MONTH FROM date) = %s
            """, (year, month))
            ledger_txs = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    matched_ledger_ids = set()
    matched_checking_ids = set()

    ignored_statement_txs = []
    duplicate_txs = []
    new_txs = []

    # 1. Classify credit card statement payments
    for tx in checking_txs:
        is_stmt = (tx['amount'] < 0) and any(re.search(pat, tx['description'].lower()) for pat in patterns)
        if is_stmt:
            ignored_statement_txs.append({
                "date": str(tx['date']),
                "description": tx['description'],
                "amount": tx['amount'],
                "reason": "Credit Card/Statement Autopay"
            })
            matched_checking_ids.add(tx['id'])

    # 2. Find exact matches
    for l_tx in ledger_txs:
        l_amount = float(l_tx['amount'])
        for tx in checking_txs:
            if tx['id'] in matched_checking_ids:
                continue
            if tx['type'] != l_tx['type']:
                continue
            if abs(abs(tx['amount']) - l_amount) < 0.01:
                date_diff = abs((tx['date'] - l_tx['date']).days)
                if date_diff <= 3:
                    matched_checking_ids.add(tx['id'])
                    matched_ledger_ids.add(l_tx['id'])
                    duplicate_txs.append({
                        "date": str(tx['date']),
                        "description": tx['description'],
                        "amount": tx['amount'],
                        "matched_ledger": {
                            "date": str(l_tx['date']),
                            "description": l_tx['description'],
                            "amount": float(l_tx['amount'])
                        }
                    })
                    break

    # 3. Find subset combination matches for grouped ledger entries
    for l_tx in ledger_txs:
        if l_tx['id'] in matched_ledger_ids:
            continue
        l_amount = float(l_tx['amount'])
        pool = []
        for tx in checking_txs:
            if tx['id'] in matched_checking_ids:
                continue
            date_diff = abs((tx['date'] - l_tx['date']).days)
            if date_diff <= 3:
                if simplify_desc(tx['description']) == simplify_ledger_desc(l_tx['description']):
                    val = abs(tx['amount']) if tx['type'] == l_tx['type'] else -abs(tx['amount'])
                    pool.append((val, tx))

        if pool:
            subset = find_subset_sum(pool, l_amount)
            if subset:
                for tx in subset:
                    matched_checking_ids.add(tx['id'])
                    duplicate_txs.append({
                        "date": str(tx['date']),
                        "description": tx['description'],
                        "amount": tx['amount'],
                        "matched_ledger": {
                            "date": str(l_tx['date']),
                            "description": l_tx['description'],
                            "amount": float(l_tx['amount'])
                        }
                    })
                matched_ledger_ids.add(l_tx['id'])

    # 4. Classify remaining unmatched checking transactions as "New"
    for tx in checking_txs:
        if tx['id'] not in matched_checking_ids:
            new_txs.append({
                "date": str(tx['date']),
                "description": tx['description'],
                "amount": tx['amount'],
                "type": tx['type']
            })

    # If commit is requested, save the new transactions into the database
    if commit and new_txs:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                for tx in new_txs:
                    cur.execute("""
                        INSERT INTO transactions (date, description, amount, category, account, type)
                        VALUES (%s, %s, %s, '', %s, %s)
                    """, (tx["date"], tx["description"], abs(tx["amount"]), account, tx["type"]))
                conn.commit()
        finally:
            conn.close()

    return {
        "ignored": ignored_statement_txs,
        "duplicate": duplicate_txs,
        "new": new_txs,
        "committed": commit
    }

@app.get("/health")
def health():
    return {"status": "ok"}

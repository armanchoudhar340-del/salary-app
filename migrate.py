import os
import sys
import sqlite3
import json

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SQLITE_DB = os.path.join(BASE_DIR, "salary.db")
SEED_FILE = os.path.join(BASE_DIR, "seed_data.json")

def get_source_data():
    """Extract records from SQLite database if present, otherwise seed_data.json."""
    if os.path.exists(SQLITE_DB):
        try:
            con = sqlite3.connect(SQLITE_DB)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            
            # Check columns in employees
            cols = [r[1] for r in cur.execute("PRAGMA table_info(employees)").fetchall()]
            has_joining = "joining_date" in cols
            
            if has_joining:
                emp_rows = cur.execute("SELECT id, name, monthly_salary, joining_date, created_at FROM employees ORDER BY id").fetchall()
            else:
                emp_rows = cur.execute("SELECT id, name, monthly_salary, created_at FROM employees ORDER BY id").fetchall()
                
            employees = []
            for r in emp_rows:
                d = dict(r)
                if not d.get("joining_date"):
                    d["joining_date"] = d["created_at"][:10] if d.get("created_at") else None
                employees.append(d)
                
            tx_rows = cur.execute("SELECT id, employee_id, amount, transaction_date, transaction_time, created_at FROM transactions ORDER BY id").fetchall()
            transactions = [dict(r) for r in tx_rows]
            con.close()
            return employees, transactions
        except Exception as e:
            print(f"Warning: could not read directly from SQLite ({e}), falling back to seed_data.json")

    if os.path.exists(SEED_FILE):
        with open(SEED_FILE, "r") as f:
            data = json.load(f)
            return data.get("employees", []), data.get("transactions", [])

    return [], []

def migrate_to_postgres(database_url):
    if not psycopg2:
        print("Error: psycopg2 is not installed. Run 'pip install psycopg2-binary' first.")
        sys.exit(1)

    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    print(f"Connecting to PostgreSQL database...")
    conn = psycopg2.connect(database_url)
    conn.autocommit = False
    cur = conn.cursor()

    # 1. Create tables if not exist
    print("Creating production tables if they do not exist...")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            id            SERIAL PRIMARY KEY,
            name          TEXT NOT NULL,
            monthly_salary DOUBLE PRECISION NOT NULL,
            joining_date  TEXT,
            created_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id               SERIAL PRIMARY KEY,
            employee_id      INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            amount           DOUBLE PRECISION NOT NULL,
            transaction_date TEXT NOT NULL,
            transaction_time TEXT NOT NULL,
            created_at       TEXT NOT NULL
        );
    """)
    conn.commit()

    # 2. Check existing count
    cur.execute("SELECT COUNT(*) FROM employees")
    pg_emp_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM transactions")
    pg_tx_count = cur.fetchone()[0]

    print(f"PostgreSQL currently has {pg_emp_count} employees and {pg_tx_count} transactions.")

    employees, transactions = get_source_data()
    print(f"Source data contains {len(employees)} employees and {len(transactions)} transactions.")

    if not employees and not transactions:
        print("No source records found to migrate.")
        conn.close()
        return

    # 3. Migrate employees
    migrated_emps = 0
    for emp in employees:
        cur.execute("""
            INSERT INTO employees (id, name, monthly_salary, joining_date, created_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                monthly_salary = EXCLUDED.monthly_salary,
                joining_date = EXCLUDED.joining_date,
                created_at = EXCLUDED.created_at;
        """, (emp["id"], emp["name"], emp["monthly_salary"], emp.get("joining_date"), emp["created_at"]))
        migrated_emps += 1

    # 4. Migrate transactions
    migrated_txs = 0
    for tx in transactions:
        cur.execute("""
            INSERT INTO transactions (id, employee_id, amount, transaction_date, transaction_time, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                employee_id = EXCLUDED.employee_id,
                amount = EXCLUDED.amount,
                transaction_date = EXCLUDED.transaction_date,
                transaction_time = EXCLUDED.transaction_time,
                created_at = EXCLUDED.created_at;
        """, (tx["id"], tx["employee_id"], tx["amount"], tx["transaction_date"], tx["transaction_time"], tx["created_at"]))
        migrated_txs += 1

    # 5. Fix sequence counters for future AUTOINCREMENT IDs
    cur.execute("SELECT setval(pg_get_serial_sequence('employees', 'id'), COALESCE((SELECT MAX(id) FROM employees), 1));")
    cur.execute("SELECT setval(pg_get_serial_sequence('transactions', 'id'), COALESCE((SELECT MAX(id) FROM transactions), 1));")

    conn.commit()

    # 6. Verification
    cur.execute("SELECT COUNT(*) FROM employees")
    final_emp_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM transactions")
    final_tx_count = cur.fetchone()[0]

    print(f"Migration completed successfully!")
    print(f"Total employees in PostgreSQL: {final_emp_count}")
    print(f"Total transactions in PostgreSQL: {final_tx_count}")

    conn.close()

if __name__ == "__main__":
    db_url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DATABASE_URL")
    if not db_url:
        print("Usage: python migrate.py <POSTGRES_DATABASE_URL>")
        sys.exit(1)
    migrate_to_postgres(db_url)


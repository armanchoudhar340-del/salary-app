import sqlite3
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, g

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "salary_app_secret_key_2024")

# Storage & Database configuration
# If DATABASE_URL is provided, use PostgreSQL (for Render / Production)
# Otherwise, fall back to SQLite (salary.db)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", BASE_DIR)
DATABASE = os.path.join(DATA_DIR, "salary.db")
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

ADMIN_PIN = os.environ.get("ADMIN_PIN", "1234")  # Change this PIN as needed

# --------------------------------------------------------------------------
# Database Adapter (Dual SQLite & PostgreSQL support)
# --------------------------------------------------------------------------

class RowWrapper:
    """Provides uniform dictionary and index-based access across SQLite and PostgreSQL."""
    def __init__(self, d, tuple_vals=None):
        self._d = dict(d) if isinstance(d, dict) else {}
        self._t = tuple(tuple_vals if tuple_vals is not None else self._d.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._t[key]
        return self._d[key]

    def get(self, key, default=None):
        return self._d.get(key, default)

    def keys(self):
        return self._d.keys()

    def values(self):
        return self._d.values()

    def items(self):
        return self._d.items()


class PostgresCursorWrapper:
    def __init__(self, cur):
        self.cur = cur

    def fetchone(self):
        row = self.cur.fetchone()
        if row is None:
            return None
        return RowWrapper(row, tuple(row.values()))

    def fetchall(self):
        rows = self.cur.fetchall()
        return [RowWrapper(r, tuple(r.values())) for r in rows]


class PostgresDBWrapper:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        # Convert ? placeholders to %s for psycopg2
        sql_pg = sql.replace("?", "%s")
        cur = self.conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql_pg, params)
        return PostgresCursorWrapper(cur)

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.conn.close()


def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        if DATABASE_URL and HAS_POSTGRES:
            conn = psycopg2.connect(DATABASE_URL)
            db = g._database = PostgresDBWrapper(conn)
        else:
            conn = sqlite3.connect(DATABASE)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            db = g._database = conn
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist for either PostgreSQL or SQLite."""
    with app.app_context():
        if DATABASE_URL and HAS_POSTGRES:
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS employees (
                    id              SERIAL PRIMARY KEY,
                    name            TEXT NOT NULL,
                    category        TEXT DEFAULT 'Driver',
                    monthly_salary  DOUBLE PRECISION NOT NULL,
                    joining_date    TEXT,
                    months_override INTEGER DEFAULT NULL,
                    created_at      TEXT NOT NULL
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
            conn.close()
        else:
            db = get_db()
            db.executescript("""
                CREATE TABLE IF NOT EXISTS employees (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    name            TEXT    NOT NULL,
                    category        TEXT    DEFAULT 'Driver',
                    monthly_salary  REAL    NOT NULL,
                    joining_date    TEXT,
                    months_override INTEGER DEFAULT NULL,
                    created_at      TEXT    NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    employee_id      INTEGER NOT NULL,
                    amount           REAL    NOT NULL,
                    transaction_date TEXT    NOT NULL,
                    transaction_time TEXT    NOT NULL,
                    created_at       TEXT    NOT NULL,
                    FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE
                );
            """)
            # Ensure columns exist if upgrading existing SQLite DB
            cols = [r[1] for r in db.execute("PRAGMA table_info(employees)").fetchall()]
            if "joining_date" not in cols:
                db.execute("ALTER TABLE employees ADD COLUMN joining_date TEXT")
                db.execute("UPDATE employees SET joining_date = substr(created_at, 1, 10) WHERE joining_date IS NULL")
            if "category" not in cols:
                db.execute("ALTER TABLE employees ADD COLUMN category TEXT DEFAULT 'Driver'")
                db.execute("UPDATE employees SET category = 'Driver' WHERE category IS NULL")
            if "months_override" not in cols:
                db.execute("ALTER TABLE employees ADD COLUMN months_override INTEGER DEFAULT NULL")
            db.commit()


# --------------------------------------------------------------------------
# Utility & Categories
# --------------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")

CATEGORIES = [
    {"id": "Taxi",   "name": "Taxi Wala",   "icon": "🚖", "badge_class": "cat-taxi"},
    {"id": "Driver", "name": "Driver",      "icon": "🚗", "badge_class": "cat-driver"},
    {"id": "Tempo",  "name": "Tempo Wala",  "icon": "🚚", "badge_class": "cat-tempo"},
    {"id": "TRP",    "name": "TRP Wala",    "icon": "🚛", "badge_class": "cat-trp"},
]
CATEGORY_NAMES = {c["id"]: c["name"] for c in CATEGORIES}
CATEGORY_ICONS = {c["id"]: c["icon"] for c in CATEGORIES}

def now_ist():
    return datetime.now(IST)

def format_inr(amount):
    return f"₹{amount:,.0f}"

def calculate_months(joining_date, year=None, month=None):
    """Calculates elapsed months from joining_date up to (year, month). Returns at least 1."""
    if not joining_date:
        return 1
    try:
        j_year = int(joining_date[:4])
        j_month = int(joining_date[5:7])
        j_day = int(joining_date[8:10]) if len(joining_date) >= 10 else 1

        now = now_ist()
        if year is None:
            year = now.year
        if month is None:
            month = now.month

        # If selected month is in the future relative to joining month
        if (year, month) < (j_year, j_month):
            return 1

        diff = (year - j_year) * 12 + (month - j_month)

        # If viewing current month, check if current day is before joining day
        if year == now.year and month == now.month:
            if now.day < j_day:
                diff -= 1

        return max(1, diff if diff >= 1 else 1)
    except Exception:
        return 1


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        pin = request.form.get("pin", "").strip()
        if pin == ADMIN_PIN:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        else:
            error = "Wrong PIN. Please try again."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    db = get_db()
    search = request.args.get("search", "").strip()
    active_tab = request.args.get("tab", "all").strip()
    now = now_ist()
    today_str = now.strftime("%Y-%m-%d")
    year  = int(request.args.get("year",  now.year))
    month = int(request.args.get("month", now.month))

    month_str = f"{year}-{month:02d}"

    if search:
        rows = db.execute(
            "SELECT * FROM employees WHERE LOWER(name) LIKE LOWER(?) ORDER BY LOWER(name)",
            (f"%{search}%",)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM employees ORDER BY LOWER(name)").fetchall()

    employees = []
    for emp in rows:
        j_date = emp["joining_date"] if "joining_date" in emp.keys() and emp["joining_date"] else None
        if not j_date and emp["created_at"]:
            j_date = emp["created_at"][:10]
        if not j_date:
            j_date = today_str

        is_today = (j_date == today_str)
        try:
            joining_formatted = datetime.strptime(j_date, "%Y-%m-%d").strftime("%d %b %Y")
        except Exception:
            joining_formatted = j_date

        # Calculate months
        months_override = emp["months_override"] if "months_override" in emp.keys() and emp["months_override"] else None
        if months_override and months_override >= 1:
            months_count = months_override
            months_is_auto = False
        else:
            months_count = calculate_months(j_date, year, month)
            months_is_auto = True

        total_salary = months_count * emp["monthly_salary"]

        # Advances taken up to selected month (cumulative)
        total_taken = db.execute(
            """SELECT COALESCE(SUM(amount), 0) FROM transactions
               WHERE employee_id = ?
                 AND substr(transaction_date, 1, 7) <= ?""",
            (emp["id"], month_str)
        ).fetchone()[0]

        # Advances taken in THIS month only
        month_taken = db.execute(
            """SELECT COALESCE(SUM(amount), 0) FROM transactions
               WHERE employee_id = ?
                 AND substr(transaction_date, 1, 7) = ?""",
            (emp["id"], month_str)
        ).fetchone()[0]

        category = emp["category"] if "category" in emp.keys() and emp["category"] else "Driver"
        cat_info = next((c for c in CATEGORIES if c["id"] == category), CATEGORIES[1])

        employees.append({
            "id":             emp["id"],
            "name":           emp["name"],
            "category":       category,
            "category_name":  cat_info["name"],
            "category_icon":  cat_info["icon"],
            "category_badge": cat_info["badge_class"],
            "monthly_salary": emp["monthly_salary"],
            "months_count":   months_count,
            "months_is_auto": months_is_auto,
            "total_salary":   total_salary,
            "total_taken":    total_taken,
            "month_taken":    month_taken,
            "remaining":      total_salary - total_taken,
            "joining_date":   j_date,
            "joining_formatted": joining_formatted,
            "is_today":       is_today,
        })

    # Group employees by category for 3 columns (+ TRP)
    taxi_employees   = [e for e in employees if e["category"] == "Taxi"]
    driver_employees = [e for e in employees if e["category"] == "Driver"]
    tempo_employees  = [e for e in employees if e["category"] == "Tempo"]
    trp_employees    = [e for e in employees if e["category"] == "TRP"]

    cat_counts = {
        "all":    len(employees),
        "Taxi":   len(taxi_employees),
        "Driver": len(driver_employees),
        "Tempo":  len(tempo_employees),
        "TRP":    len(trp_employees),
    }

    summary = {
        "total_employees": len(employees),
        "total_salary":    sum(e["total_salary"] for e in employees),
        "total_taken":     sum(e["total_taken"] for e in employees),
        "total_remaining": sum(e["remaining"] for e in employees),
    }

    return render_template(
        "dashboard.html",
        employees=employees,
        taxi_employees=taxi_employees,
        driver_employees=driver_employees,
        tempo_employees=tempo_employees,
        trp_employees=trp_employees,
        cat_counts=cat_counts,
        summary=summary,
        active_tab=active_tab,
        search=search,
        selected_year=year,
        selected_month=month,
        current_year=now.year,
        current_month=now.month,
        categories=CATEGORIES,
    )


# --------------------------------------------------------------------------
# Add Employee
# --------------------------------------------------------------------------

@app.route("/employee/add", methods=["GET", "POST"])
@login_required
def add_employee():
    error = None
    today_str = now_ist().strftime("%Y-%m-%d")
    selected_category = request.args.get("category", "Driver").strip()
    if selected_category not in CATEGORY_NAMES:
        selected_category = "Driver"

    if request.method == "POST":
        name   = request.form.get("name", "").strip()
        salary = request.form.get("monthly_salary", "").strip()
        category = request.form.get("category", "Driver").strip()
        joining_date = request.form.get("joining_date", "").strip() or today_str
        months = request.form.get("months_override", "").strip()
        months_override = int(months) if months and months.isdigit() else None

        if not name:
            error = "Please enter the employee name."
        elif not salary:
            error = "Please enter the monthly salary."
        else:
            try:
                salary_val = float(salary)
                if salary_val <= 0:
                    raise ValueError
            except ValueError:
                error = "Salary must be a valid number."

        if not error:
            db = get_db()
            db.execute(
                "INSERT INTO employees (name, category, monthly_salary, joining_date, months_override, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (name, category, salary_val, joining_date, months_override, now_ist().isoformat())
            )
            db.commit()
            flash(f"'{name}' ({CATEGORY_NAMES.get(category, category)}) added successfully.", "success")
            return redirect(url_for("dashboard", tab=category))

    return render_template(
        "add_employee.html",
        error=error,
        today=today_str,
        categories=CATEGORIES,
        selected_category=selected_category
    )


# --------------------------------------------------------------------------
# Edit Employee
# --------------------------------------------------------------------------

@app.route("/employee/<int:emp_id>/edit", methods=["GET", "POST"])
@login_required
def edit_employee(emp_id):
    db  = get_db()
    emp = db.execute("SELECT * FROM employees WHERE id = ?", (emp_id,)).fetchone()
    if not emp:
        flash("Employee not found.", "error")
        return redirect(url_for("dashboard"))

    today_str = now_ist().strftime("%Y-%m-%d")
    current_joining = emp["joining_date"] if "joining_date" in emp.keys() and emp["joining_date"] else None
    if not current_joining and emp["created_at"]:
        current_joining = emp["created_at"][:10]
    if not current_joining:
        current_joining = today_str

    current_category = emp["category"] if "category" in emp.keys() and emp["category"] else "Driver"
    current_months = emp["months_override"] if "months_override" in emp.keys() and emp["months_override"] else ""

    error = None
    if request.method == "POST":
        name   = request.form.get("name", "").strip()
        category = request.form.get("category", "Driver").strip()
        salary = request.form.get("monthly_salary", "").strip()
        joining_date = request.form.get("joining_date", "").strip() or current_joining
        months = request.form.get("months_override", "").strip()
        months_override = int(months) if months and months.isdigit() else None

        if not name:
            error = "Please enter the employee name."
        elif not salary:
            error = "Please enter the monthly salary."
        else:
            try:
                salary_val = float(salary)
                if salary_val <= 0:
                    raise ValueError
            except ValueError:
                error = "Salary must be a valid number."

        if not error:
            db.execute(
                "UPDATE employees SET name = ?, category = ?, monthly_salary = ?, joining_date = ?, months_override = ? WHERE id = ?",
                (name, category, salary_val, joining_date, months_override, emp_id)
            )
            db.commit()
            flash(f"'{name}' updated successfully.", "success")
            return redirect(url_for("employee_profile", emp_id=emp_id))

    return render_template(
        "edit_employee.html",
        emp=emp,
        error=error,
        current_joining=current_joining,
        current_category=current_category,
        current_months=current_months,
        categories=CATEGORIES
    )


# --------------------------------------------------------------------------
# Delete Employee
# --------------------------------------------------------------------------

@app.route("/employee/<int:emp_id>/delete", methods=["POST"])
@login_required
def delete_employee(emp_id):
    db  = get_db()
    emp = db.execute("SELECT * FROM employees WHERE id = ?", (emp_id,)).fetchone()
    if emp:
        db.execute("DELETE FROM employees WHERE id = ?", (emp_id,))
        db.commit()
        flash(f"'{emp['name']}' has been deleted.", "success")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------
# Employee Profile
# --------------------------------------------------------------------------

@app.route("/employee/<int:emp_id>")
@login_required
def employee_profile(emp_id):
    db  = get_db()
    emp = db.execute("SELECT * FROM employees WHERE id = ?", (emp_id,)).fetchone()
    if not emp:
        flash("Employee not found.", "error")
        return redirect(url_for("dashboard"))

    now = now_ist()
    today_str = now.strftime("%Y-%m-%d")
    year  = int(request.args.get("year",  now.year))
    month = int(request.args.get("month", now.month))
    month_str = f"{year}-{month:02d}"

    j_date = emp["joining_date"] if "joining_date" in emp.keys() and emp["joining_date"] else None
    if not j_date and emp["created_at"]:
        j_date = emp["created_at"][:10]
    if not j_date:
        j_date = today_str

    is_today = (j_date == today_str)
    try:
        joining_formatted = datetime.strptime(j_date, "%Y-%m-%d").strftime("%d %b %Y")
    except Exception:
        joining_formatted = j_date

    # Months calculation
    months_override = emp["months_override"] if "months_override" in emp.keys() and emp["months_override"] else None
    auto_months = calculate_months(j_date, year, month)
    if months_override and months_override >= 1:
        months_count = months_override
        months_is_auto = False
    else:
        months_count = auto_months
        months_is_auto = True

    total_salary = months_count * emp["monthly_salary"]

    txns = db.execute(
        """SELECT * FROM transactions
           WHERE employee_id = ?
             AND substr(transaction_date, 1, 7) = ?
           ORDER BY transaction_date DESC, transaction_time DESC""",
        (emp_id, month_str)
    ).fetchall()

    month_taken = sum(t["amount"] for t in txns)

    # All transactions up to selected month (cumulative)
    all_taken_row = db.execute(
        """SELECT COALESCE(SUM(amount), 0) FROM transactions
           WHERE employee_id = ?
             AND substr(transaction_date, 1, 7) <= ?""",
        (emp_id, month_str)
    ).fetchone()
    total_taken = all_taken_row[0] if all_taken_row else month_taken

    remaining = total_salary - total_taken

    category = emp["category"] if "category" in emp.keys() and emp["category"] else "Driver"
    cat_info = next((c for c in CATEGORIES if c["id"] == category), CATEGORIES[1])

    # All distinct months that have transactions for this employee
    months_with_data = db.execute(
        """SELECT DISTINCT substr(transaction_date, 1, 7) AS ym
           FROM transactions WHERE employee_id = ?
           ORDER BY ym DESC""",
        (emp_id,)
    ).fetchall()

    return render_template(
        "employee_profile.html",
        emp=emp,
        txns=txns,
        category=category,
        category_name=cat_info["name"],
        category_icon=cat_info["icon"],
        category_badge=cat_info["badge_class"],
        months_count=months_count,
        auto_months=auto_months,
        months_is_auto=months_is_auto,
        total_salary=total_salary,
        total_taken=total_taken,
        month_taken=month_taken,
        remaining=remaining,
        selected_year=year,
        selected_month=month,
        current_year=now.year,
        current_month=now.month,
        months_with_data=[r["ym"] for r in months_with_data],
        joining_date=j_date,
        joining_formatted=joining_formatted,
        is_today=is_today,
    )


# --------------------------------------------------------------------------
# Quick Months Adjuster (from Profile)
# --------------------------------------------------------------------------

@app.route("/employee/<int:emp_id>/set-months", methods=["POST"])
@login_required
def set_employee_months(emp_id):
    months = request.form.get("months", "").strip()
    db = get_db()
    if months == "auto" or not months:
        db.execute("UPDATE employees SET months_override = NULL WHERE id = ?", (emp_id,))
    else:
        try:
            m_val = int(months)
            if m_val >= 1:
                db.execute("UPDATE employees SET months_override = ? WHERE id = ?", (m_val, emp_id))
        except ValueError:
            pass
    db.commit()
    flash("Salary period updated.", "success")
    return redirect(url_for("employee_profile", emp_id=emp_id))


# --------------------------------------------------------------------------
# Add Transaction
# --------------------------------------------------------------------------

@app.route("/employee/<int:emp_id>/transaction/add", methods=["POST"])
@login_required
def add_transaction(emp_id):
    db  = get_db()
    emp = db.execute("SELECT * FROM employees WHERE id = ?", (emp_id,)).fetchone()
    if not emp:
        return jsonify({"success": False, "error": "Employee not found"}), 404

    amount_str = request.form.get("amount", "").strip()
    try:
        amount = float(amount_str)
        if amount <= 0:
            raise ValueError
    except ValueError:
        flash("Please enter a valid amount.", "error")
        return redirect(url_for("employee_profile", emp_id=emp_id))

    now      = now_ist()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%I:%M %p")   # e.g. 02:45 PM

    db.execute(
        """INSERT INTO transactions (employee_id, amount, transaction_date, transaction_time, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (emp_id, amount, date_str, time_str, now.isoformat())
    )
    db.commit()
    flash(f"₹{amount:,.0f} recorded successfully.", "success")
    return redirect(url_for("employee_profile", emp_id=emp_id))


# --------------------------------------------------------------------------
# Delete Transaction
# --------------------------------------------------------------------------

@app.route("/transaction/<int:txn_id>/delete", methods=["POST"])
@login_required
def delete_transaction(txn_id):
    db  = get_db()
    txn = db.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    if txn:
        emp_id = txn["employee_id"]
        db.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
        db.commit()
        flash("Transaction deleted.", "success")
        return redirect(url_for("employee_profile", emp_id=emp_id))
    flash("Transaction not found.", "error")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------
# API: quick summary (used for dashboard live refresh)
# --------------------------------------------------------------------------

@app.route("/api/employee/<int:emp_id>/summary")
@login_required
def api_employee_summary(emp_id):
    db        = get_db()
    now       = now_ist()
    month_str = now.strftime("%Y-%m")
    emp       = db.execute("SELECT * FROM employees WHERE id = ?", (emp_id,)).fetchone()
    if not emp:
        return jsonify({"error": "not found"}), 404
    total = db.execute(
        """SELECT COALESCE(SUM(amount),0) FROM transactions
           WHERE employee_id=? AND substr(transaction_date, 1, 7)=?""",
        (emp_id, month_str)
    ).fetchone()[0]
    return jsonify({
        "monthly_salary": emp["monthly_salary"],
        "total_taken":    total,
        "remaining":      emp["monthly_salary"] - total,
    })


# --------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------

# Always initialize DB (works both locally and on PythonAnywhere)
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print("\n" + "="*55)
    print("  Employee Salary & Advance Management App")
    print(f"  Admin PIN: {ADMIN_PIN}  (change in app.py → ADMIN_PIN)")
    print(f"  Open: http://127.0.0.1:{port}")
    print(f"  Database: {DATABASE}")
    print("="*55 + "\n")
    app.run(debug=True, host="0.0.0.0", port=port)

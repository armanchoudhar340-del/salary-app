import sqlite3
import os
import json
import io
import csv
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, g, Response, send_file

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

            # Auto-recovery if database is completely empty (e.g. fresh Render container)
            count = db.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
            if count == 0:
                backup_files = [
                    os.path.join(DATA_DIR, "auto_backup.json"),
                    os.path.join(BASE_DIR, "auto_backup.json"),
                    os.path.join(BASE_DIR, "seed_data.json")
                ]
                for b_path in backup_files:
                    if os.path.exists(b_path):
                        try:
                            with open(b_path, "r", encoding="utf-8") as f:
                                b_data = json.load(f)
                            emp_list = b_data.get("employees", [])
                            for emp in emp_list:
                                db.execute(
                                    """INSERT OR IGNORE INTO employees (id, name, category, monthly_salary, joining_date, months_override, created_at)
                                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                    (emp.get("id"), emp.get("name"), emp.get("category", "Driver"),
                                     emp.get("monthly_salary", 0), emp.get("joining_date"),
                                     emp.get("months_override"), emp.get("created_at", now_ist().isoformat()))
                                )
                            for txn in b_data.get("transactions", []):
                                db.execute(
                                    """INSERT OR IGNORE INTO transactions (id, employee_id, amount, transaction_date, transaction_time, created_at)
                                       VALUES (?, ?, ?, ?, ?, ?)""",
                                    (txn.get("id"), txn.get("employee_id"), txn.get("amount", 0),
                                     txn.get("transaction_date"), txn.get("transaction_time"), txn.get("created_at", now_ist().isoformat()))
                                )
                            db.commit()
                            print(f"[Auto-Recovery] Successfully restored {len(emp_list)} employees from {b_path}")
                            break
                        except Exception as e:
                            print(f"[Auto-Recovery Error] {e}")


def save_auto_backup():
    """Saves a JSON snapshot of the database so data can never be lost."""
    try:
        db = get_db()
        emps = [dict(r) for r in db.execute("SELECT * FROM employees ORDER BY id").fetchall()]
        txns = [dict(r) for r in db.execute("SELECT * FROM transactions ORDER BY id").fetchall()]
        data = {
            "version": "1.0",
            "saved_at": now_ist().isoformat(),
            "employees": emps,
            "transactions": txns
        }
        for path in [os.path.join(DATA_DIR, "auto_backup.json"), os.path.join(BASE_DIR, "auto_backup.json")]:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[Auto-Backup Error] {e}")


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
    """Calculates completed elapsed months from joining_date up to (year, month). Returns 0 if less than 1 month."""
    if not joining_date:
        return 0
    try:
        import calendar
        j_dt = datetime.strptime(joining_date[:10], "%Y-%m-%d").date()
        now = now_ist().date()

        if year is None:
            year = now.year
        if month is None:
            month = now.month

        # Target date for calculation
        if year == now.year and month == now.month:
            target_dt = now
        else:
            _, last_day = calendar.monthrange(year, month)
            target_dt = datetime(year, month, last_day).date()

        # If joining date is in the future relative to target date
        if target_dt < j_dt:
            return 0

        # Calculate completed months difference
        diff = (target_dt.year - j_dt.year) * 12 + (target_dt.month - j_dt.month)
        if target_dt.day < j_dt.day:
            diff -= 1

        return max(0, diff)
    except Exception:
        return 0


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
        is_future = (j_date > today_str)
        try:
            joining_formatted = datetime.strptime(j_date, "%Y-%m-%d").strftime("%d %b %Y")
        except Exception:
            joining_formatted = j_date

        # Calculate months
        months_override = emp["months_override"] if "months_override" in emp.keys() and emp["months_override"] is not None else None
        if months_override is not None and str(months_override).strip() != "":
            try:
                months_count = int(months_override)
                months_is_auto = False
            except ValueError:
                months_count = calculate_months(j_date, year, month)
                months_is_auto = True
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
            "is_future":      is_future,
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
            save_auto_backup()
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
            save_auto_backup()
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
        save_auto_backup()
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
    is_future = (j_date > today_str)
    try:
        joining_formatted = datetime.strptime(j_date, "%Y-%m-%d").strftime("%d %b %Y")
    except Exception:
        joining_formatted = j_date

    # Months calculation
    months_override = emp["months_override"] if "months_override" in emp.keys() and emp["months_override"] is not None else None
    auto_months = calculate_months(j_date, year, month)
    if months_override is not None and str(months_override).strip() != "":
        try:
            months_count = int(months_override)
            months_is_auto = False
        except ValueError:
            months_count = auto_months
            months_is_auto = True
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
        is_future=is_future,
    )


# --------------------------------------------------------------------------
# Quick Months Adjuster (from Profile)
# --------------------------------------------------------------------------

@app.route("/employee/<int:emp_id>/set-months", methods=["POST"])
@login_required
def set_employee_months(emp_id):
    months = request.form.get("months", "").strip()
    db = get_db()
    if months == "auto" or months == "":
        db.execute("UPDATE employees SET months_override = NULL WHERE id = ?", (emp_id,))
    else:
        try:
            m_val = int(months)
            if m_val >= 0:
                db.execute("UPDATE employees SET months_override = ? WHERE id = ?", (m_val, emp_id))
        except ValueError:
            pass
    db.commit()
    save_auto_backup()
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
    save_auto_backup()
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
        save_auto_backup()
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
# Reports, Backup & Emergency Recovery
# --------------------------------------------------------------------------

@app.route("/reports")
@login_required
def reports():
    db = get_db()
    now = now_ist()
    today_str = now.strftime("%Y-%m-%d")
    
    # All employees with their full ledger summaries
    rows = db.execute("SELECT * FROM employees ORDER BY category, LOWER(name)").fetchall()
    employees = []
    for emp in rows:
        j_date = emp["joining_date"] or emp["created_at"][:10] if emp["created_at"] else today_str
        is_today = (j_date == today_str)
        is_future = (j_date > today_str)
        try:
            joining_formatted = datetime.strptime(j_date, "%Y-%m-%d").strftime("%d %b %Y")
        except Exception:
            joining_formatted = j_date

        months_override = emp["months_override"] if "months_override" in emp.keys() and emp["months_override"] is not None else None
        if months_override is not None and str(months_override).strip() != "":
            try:
                months_count = int(months_override)
            except ValueError:
                months_count = calculate_months(j_date, now.year, now.month)
        else:
            months_count = calculate_months(j_date, now.year, now.month)

        total_salary = months_count * emp["monthly_salary"]
        total_taken = db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE employee_id = ?",
            (emp["id"],)
        ).fetchone()[0]

        category = emp["category"] if "category" in emp.keys() and emp["category"] else "Driver"
        cat_info = next((c for c in CATEGORIES if c["id"] == category), CATEGORIES[1])

        employees.append({
            "id": emp["id"],
            "name": emp["name"],
            "category": category,
            "category_name": cat_info["name"],
            "category_icon": cat_info["icon"],
            "category_badge": cat_info["badge_class"],
            "monthly_salary": emp["monthly_salary"],
            "months_count": months_count,
            "total_salary": total_salary,
            "total_taken": total_taken,
            "remaining": total_salary - total_taken,
            "joining_date": j_date,
            "joining_formatted": joining_formatted,
            "is_today": is_today,
            "is_future": is_future,
        })

    # Summary
    summary = {
        "total_employees": len(employees),
        "total_salary": sum(e["total_salary"] for e in employees),
        "total_taken": sum(e["total_taken"] for e in employees),
        "total_remaining": sum(e["remaining"] for e in employees),
    }

    # All transactions history
    txns = db.execute("""
        SELECT t.id, t.amount, t.transaction_date, t.transaction_time, t.created_at,
               e.name as employee_name, e.category as employee_category
        FROM transactions t
        JOIN employees e ON t.employee_id = e.id
        ORDER BY t.transaction_date DESC, t.transaction_time DESC
        LIMIT 100
    """).fetchall()

    return render_template(
        "reports.html",
        employees=employees,
        summary=summary,
        txns=txns,
        generated_at=now.strftime("%d %b %Y, %I:%M %p")
    )


@app.route("/reports/export-csv")
@login_required
def export_csv():
    """Generates and downloads a clean CSV report compatible with Excel."""
    db = get_db()
    now = now_ist()
    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow(["SALARY MANAGEMENT REPORT"])
    writer.writerow([f"Generated On: {now.strftime('%d %b %Y, %I:%M %p')}"])
    writer.writerow([])

    # Employees Table
    writer.writerow(["EMPLOYEE SALARY LEDGER"])
    writer.writerow([
        "ID", "Name", "Category", "Joining Date", "Monthly Salary (INR)",
        "Completed Months", "Total Salary Due (INR)", "Total Taken/Advances (INR)", "Remaining Balance (INR)"
    ])

    rows = db.execute("SELECT * FROM employees ORDER BY category, LOWER(name)").fetchall()
    total_sal = 0
    total_tak = 0
    total_rem = 0

    for emp in rows:
        j_date = emp["joining_date"] or (emp["created_at"][:10] if emp["created_at"] else "")
        months_override = emp["months_override"] if "months_override" in emp.keys() and emp["months_override"] is not None else None
        if months_override is not None and str(months_override).strip() != "":
            try:
                months_count = int(months_override)
            except ValueError:
                months_count = calculate_months(j_date, now.year, now.month)
        else:
            months_count = calculate_months(j_date, now.year, now.month)

        tot_sal = months_count * emp["monthly_salary"]
        tot_tak = db.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE employee_id = ?", (emp["id"],)).fetchone()[0]
        rem = tot_sal - tot_tak

        total_sal += tot_sal
        total_tak += tot_tak
        total_rem += rem

        writer.writerow([
            emp["id"],
            emp["name"],
            emp["category"] or "Driver",
            j_date,
            f"{emp['monthly_salary']:.0f}",
            months_count,
            f"{tot_sal:.0f}",
            f"{tot_tak:.0f}",
            f"{rem:.0f}"
        ])

    writer.writerow([])
    writer.writerow(["TOTALS", "", "", "", "", "", f"{total_sal:.0f}", f"{total_tak:.0f}", f"{total_rem:.0f}"])
    writer.writerow([])

    # Transactions Table
    writer.writerow(["TRANSACTIONS HISTORY"])
    writer.writerow(["Transaction ID", "Date", "Time", "Employee Name", "Category", "Amount (INR)"])
    txns = db.execute("""
        SELECT t.id, t.amount, t.transaction_date, t.transaction_time,
               e.name as employee_name, e.category as employee_category
        FROM transactions t
        JOIN employees e ON t.employee_id = e.id
        ORDER BY t.transaction_date DESC, t.transaction_time DESC
    """).fetchall()

    for t in txns:
        writer.writerow([
            t["id"],
            t["transaction_date"],
            t["transaction_time"],
            t["employee_name"],
            t["employee_category"] or "Driver",
            f"{t['amount']:.0f}"
        ])

    csv_data = output.getvalue()
    filename = f"salary_report_{now.strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/reports/download-backup")
@login_required
def download_backup():
    """Downloads a complete JSON database backup file to user's device."""
    db = get_db()
    now = now_ist()
    emps = [dict(r) for r in db.execute("SELECT * FROM employees ORDER BY id").fetchall()]
    txns = [dict(r) for r in db.execute("SELECT * FROM transactions ORDER BY id").fetchall()]

    backup = {
        "version": "1.0",
        "created_at": now.isoformat(),
        "created_formatted": now.strftime("%d %b %Y, %I:%M %p"),
        "total_employees": len(emps),
        "total_transactions": len(txns),
        "employees": emps,
        "transactions": txns
    }

    json_data = json.dumps(backup, indent=2, ensure_ascii=False)
    filename = f"salary_backup_{now.strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        json_data,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@app.route("/reports/restore", methods=["POST"])
@login_required
def restore_backup():
    """Restores database from an uploaded JSON backup file."""
    if "backup_file" not in request.files:
        flash("No file was uploaded.", "error")
        return redirect(url_for("reports"))

    file = request.files["backup_file"]
    if file.filename == "":
        flash("Please select a JSON backup file.", "error")
        return redirect(url_for("reports"))

    try:
        content = file.read().decode("utf-8")
        data = json.loads(content)

        emp_list = data.get("employees", [])
        txn_list = data.get("transactions", [])

        if not emp_list and not txn_list:
            flash("The uploaded file does not contain valid salary backup data.", "error")
            return redirect(url_for("reports"))

        db = get_db()
        emp_restored = 0
        txn_restored = 0

        for emp in emp_list:
            if not emp.get("name"):
                continue
            db.execute(
                """INSERT OR REPLACE INTO employees (id, name, category, monthly_salary, joining_date, months_override, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (emp.get("id"), emp.get("name"), emp.get("category", "Driver"),
                 emp.get("monthly_salary", 0), emp.get("joining_date"),
                 emp.get("months_override"), emp.get("created_at", now_ist().isoformat()))
            )
            emp_restored += 1

        for txn in txn_list:
            if not txn.get("employee_id"):
                continue
            db.execute(
                """INSERT OR REPLACE INTO transactions (id, employee_id, amount, transaction_date, transaction_time, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (txn.get("id"), txn.get("employee_id"), txn.get("amount", 0),
                 txn.get("transaction_date"), txn.get("transaction_time"), txn.get("created_at", now_ist().isoformat()))
            )
            txn_restored += 1

        db.commit()
        save_auto_backup()
        flash(f"Data Restored Successfully! Restored {emp_restored} employees and {txn_restored} transactions.", "success")
    except Exception as e:
        flash(f"Error restoring backup: {str(e)}", "error")

    return redirect(url_for("reports"))


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

import sqlite3
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, g

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "salary_app_secret_key_2024")

# On Railway, use /data/ volume for persistent storage
# Locally, use the app directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", BASE_DIR)
DATABASE = os.path.join(DATA_DIR, "salary.db")
ADMIN_PIN = os.environ.get("ADMIN_PIN", "1234")  # Change this PIN as needed

# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------

def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist."""
    with app.app_context():
        db = get_db()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS employees (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT    NOT NULL,
                monthly_salary REAL   NOT NULL,
                created_at    TEXT    NOT NULL
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
        db.commit()


# --------------------------------------------------------------------------
# Utility
# --------------------------------------------------------------------------

IST = ZoneInfo("Asia/Kolkata")

def now_ist():
    return datetime.now(IST)

def format_inr(amount):
    return f"₹{amount:,.0f}"


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
    now = now_ist()
    year  = int(request.args.get("year",  now.year))
    month = int(request.args.get("month", now.month))

    month_str = f"{year}-{month:02d}"

    if search:
        rows = db.execute(
            "SELECT * FROM employees WHERE name LIKE ? ORDER BY name COLLATE NOCASE",
            (f"%{search}%",)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM employees ORDER BY name COLLATE NOCASE").fetchall()

    employees = []
    for emp in rows:
        total_taken = db.execute(
            """SELECT COALESCE(SUM(amount), 0) FROM transactions
               WHERE employee_id = ?
                 AND strftime('%Y-%m', transaction_date) = ?""",
            (emp["id"], month_str)
        ).fetchone()[0]
        employees.append({
            "id":             emp["id"],
            "name":           emp["name"],
            "monthly_salary": emp["monthly_salary"],
            "total_taken":    total_taken,
            "remaining":      emp["monthly_salary"] - total_taken,
        })

    return render_template(
        "dashboard.html",
        employees=employees,
        search=search,
        selected_year=year,
        selected_month=month,
        current_year=now.year,
        current_month=now.month,
    )


# --------------------------------------------------------------------------
# Add Employee
# --------------------------------------------------------------------------

@app.route("/employee/add", methods=["GET", "POST"])
@login_required
def add_employee():
    error = None
    if request.method == "POST":
        name   = request.form.get("name", "").strip()
        salary = request.form.get("monthly_salary", "").strip()

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
                "INSERT INTO employees (name, monthly_salary, created_at) VALUES (?, ?, ?)",
                (name, salary_val, now_ist().isoformat())
            )
            db.commit()
            flash(f"'{name}' added successfully.", "success")
            return redirect(url_for("dashboard"))

    return render_template("add_employee.html", error=error)


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

    error = None
    if request.method == "POST":
        name   = request.form.get("name", "").strip()
        salary = request.form.get("monthly_salary", "").strip()

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
                "UPDATE employees SET name = ?, monthly_salary = ? WHERE id = ?",
                (name, salary_val, emp_id)
            )
            db.commit()
            flash(f"'{name}' updated successfully.", "success")
            return redirect(url_for("employee_profile", emp_id=emp_id))

    return render_template("edit_employee.html", emp=emp, error=error)


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
    year  = int(request.args.get("year",  now.year))
    month = int(request.args.get("month", now.month))
    month_str = f"{year}-{month:02d}"

    txns = db.execute(
        """SELECT * FROM transactions
           WHERE employee_id = ?
             AND strftime('%Y-%m', transaction_date) = ?
           ORDER BY transaction_date DESC, transaction_time DESC""",
        (emp_id, month_str)
    ).fetchall()

    total_taken = sum(t["amount"] for t in txns)
    remaining   = emp["monthly_salary"] - total_taken

    # All distinct months that have transactions for this employee
    months_with_data = db.execute(
        """SELECT DISTINCT strftime('%Y-%m', transaction_date) AS ym
           FROM transactions WHERE employee_id = ?
           ORDER BY ym DESC""",
        (emp_id,)
    ).fetchall()

    return render_template(
        "employee_profile.html",
        emp=emp,
        txns=txns,
        total_taken=total_taken,
        remaining=remaining,
        selected_year=year,
        selected_month=month,
        current_year=now.year,
        current_month=now.month,
        months_with_data=[r["ym"] for r in months_with_data],
    )


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
           WHERE employee_id=? AND strftime('%Y-%m',transaction_date)=?""",
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

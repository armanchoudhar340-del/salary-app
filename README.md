ha # Employee Salary & Advance Management App
# वेतन प्रबंधन

## Quick Start

```bash
cd salary-app

# 1. Create virtual environment
python3 -m venv venv

# 2. Install dependencies
venv/bin/pip install -r requirements.txt

# 3. Run the app
venv/bin/python app.py
```

Then open: **http://127.0.0.1:5001**  
Default Admin PIN: **1234**

> macOS uses port 5000 for AirPlay, so the app runs on **5001** by default.

---

## Files

```
salary-app/
├── app.py                    # Flask backend (all routes + DB logic)
├── requirements.txt          # Python dependencies (Flask only)
├── salary.db                 # SQLite database (auto-created on first run)
├── templates/
│   ├── base.html             # Shared layout
│   ├── login.html            # PIN login page
│   ├── dashboard.html        # Employee list + search + month selector
│   ├── add_employee.html     # Add new employee form
│   ├── edit_employee.html    # Edit employee form
│   └── employee_profile.html # Profile, transactions, add advance
└── static/
    └── css/
        └── style.css         # All styles
```

## Changing the Admin PIN

Open `app.py` and change line:
```python
ADMIN_PIN = "1234"
```

## Database

SQLite file `salary.db` is created automatically in the `salary-app/` folder.
All data is permanently stored there.

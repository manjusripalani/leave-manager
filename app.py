"""Employee Leave and Attendance Management System (Flask + SQLite)."""
import csv
import io
import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
DB_PATH = os.environ.get("LEAVE_DB", os.path.join(app.root_path, "leave_manager.db"))
LATE_AFTER = "09:30"  # check-in after this time is marked Late

SCHEMA = """
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','manager','employee')),
    department_id INTEGER REFERENCES departments(id),
    manager_id INTEGER REFERENCES users(id),
    join_date TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS leave_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    yearly_quota INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS leave_balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    leave_type_id INTEGER NOT NULL REFERENCES leave_types(id),
    year INTEGER NOT NULL,
    total INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, leave_type_id, year)
);
CREATE TABLE IF NOT EXISTS leave_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    leave_type_id INTEGER NOT NULL REFERENCES leave_types(id),
    from_date TEXT NOT NULL,
    to_date TEXT NOT NULL,
    days INTEGER NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'Pending'
        CHECK (status IN ('Pending','Approved','Rejected','Cancelled')),
    reviewed_by INTEGER REFERENCES users(id),
    manager_comment TEXT,
    applied_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    date TEXT NOT NULL,
    check_in TEXT,
    check_out TEXT,
    status TEXT NOT NULL CHECK (status IN ('Present','Late','Absent','On Leave')),
    UNIQUE (user_id, date)
);
CREATE TABLE IF NOT EXISTS holidays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------
# Database helpers
# --------------------------------------------------------------------------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def q(sql, args=(), one=False):
    rows = db().execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def run(sql, args=()):
    cur = db().execute(sql, args)
    db().commit()
    return cur.lastrowid


def marks(ids):
    """Placeholders for an IN (...) clause. Empty lists match nothing."""
    return ",".join("?" * len(ids)) if ids else "NULL"


def ensure_balances(user_id, year, con=None):
    """Create this year's leave balance rows for a user if they are missing."""
    con = con or db()
    for lt in con.execute("SELECT id, yearly_quota FROM leave_types").fetchall():
        con.execute(
            "INSERT OR IGNORE INTO leave_balances (user_id, leave_type_id, year, total, used) "
            "VALUES (?, ?, ?, ?, 0)",
            (user_id, lt[0], year, lt[1]),
        )
    con.commit()


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        seed(con)
    con.commit()
    con.close()


def seed(con):
    """Demo data so the app is usable on first run."""
    year = date.today().year
    join = date(year, 1, 1).isoformat()
    con.executemany("INSERT INTO departments (name) VALUES (?)",
                    [("Engineering",), ("Human Resources",), ("Sales",)])
    con.executemany("INSERT INTO leave_types (name, yearly_quota) VALUES (?, ?)",
                    [("Casual", 12), ("Sick", 10), ("Earned", 15)])
    users = [
        ("HR Admin", "admin@company.com", "admin123", "admin", 2, None),
        ("Priya Raman", "manager@company.com", "manager123", "manager", 1, 1),
        ("Arjun Kumar", "arjun@company.com", "emp123", "employee", 1, 2),
        ("Meena Iyer", "meena@company.com", "emp123", "employee", 1, 2),
    ]
    for name, email, pw, role, dept, mgr in users:
        con.execute(
            "INSERT INTO users (name, email, password_hash, role, department_id, manager_id, join_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, email, generate_password_hash(pw), role, dept, mgr, join),
        )
    for uid in range(1, 5):
        ensure_balances(uid, year, con)
    for month, day, name in [(1, 26, "Republic Day"), (5, 1, "May Day"),
                             (8, 15, "Independence Day"), (10, 2, "Gandhi Jayanti"),
                             (12, 25, "Christmas")]:
        con.execute("INSERT INTO holidays (date, name) VALUES (?, ?)",
                    (date(year, month, day).isoformat(), name))


# --------------------------------------------------------------------------
# Auth helpers
# --------------------------------------------------------------------------
def csrf_token():
    """Session-bound token rendered into every form; regenerated once per session."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def csrf_protect():
    if request.method == "POST" and not app.config.get("TESTING"):
        sent = request.form.get("csrf_token", "")
        known = session.get("csrf_token", "")
        if not known or not secrets.compare_digest(sent, known):
            flash("Your session timed out. Please try that again.", "warning")
            return redirect(request.referrer or url_for("index"))


@app.before_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        user = q("SELECT * FROM users WHERE id = ? AND is_active = 1", (uid,), one=True)
        if user:
            g.user = user
        else:
            session.clear()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user["role"] not in roles:
                flash("You don't have access to that page.", "danger")
                return redirect(url_for("index"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


@app.context_processor
def inject_globals():
    pending = 0
    if g.get("user") and g.user["role"] in ("manager", "admin"):
        clause, args = request_scope(g.user)
        pending = q(
            "SELECT COUNT(*) c FROM leave_requests lr JOIN users u ON u.id = lr.user_id "
            f"WHERE lr.status = 'Pending' AND {clause}", args, one=True)["c"]
    return {"pending_badge": pending, "today": date.today()}


@app.template_filter("fdate")
def fdate(value):
    if not value:
        return ""
    d = value if isinstance(value, date) else date.fromisoformat(value)
    return d.strftime("%d %b %Y")


@app.template_filter("days")
def days_filter(n):
    return fmt_days(n)


@app.template_filter("badge")
def badge(status):
    return {
        "Pending": "st-pending", "Approved": "st-ok", "Rejected": "st-bad",
        "Cancelled": "st-muted", "Present": "st-ok", "Late": "st-pending",
        "Absent": "st-bad", "On Leave": "st-info", "Holiday": "st-muted",
        "Weekend": "st-muted",
    }.get(status, "st-muted")


# --------------------------------------------------------------------------
# Business logic
# --------------------------------------------------------------------------
def holiday_map():
    return {r["date"]: r["name"] for r in q("SELECT date, name FROM holidays")}


def working_days(start, end, hols, half_day=False):
    """Number of Mon-Fri days between start and end (inclusive) that are not holidays.
    If half_day is set and start == end, a single working day counts as 0.5."""
    count, d = 0, start
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in hols:
            count += 1
        d += timedelta(days=1)
    if half_day and start == end and count == 1:
        return 0.5
    return count


def fmt_days(n):
    """1.0 -> '1', 0.5 -> '0.5', 2.5 -> '2.5' (no trailing .0 for whole numbers)."""
    return str(int(n)) if float(n).is_integer() else str(n)


def available_days(user_id, leave_type_id, year):
    """Balance left after used days and days held by pending requests."""
    ensure_balances(user_id, year)
    b = q("SELECT total, used FROM leave_balances WHERE user_id=? AND leave_type_id=? AND year=?",
          (user_id, leave_type_id, year), one=True)
    pending = q("SELECT COALESCE(SUM(days), 0) s FROM leave_requests "
                "WHERE user_id=? AND leave_type_id=? AND status='Pending' "
                "AND substr(from_date, 1, 4) = ?", (user_id, leave_type_id, str(year)), one=True)["s"]
    return b["total"] - b["used"] - pending


def scope_ids(user):
    """IDs of the people whose data this user is allowed to see in summaries."""
    if user["role"] == "admin":
        rows = q("SELECT id FROM users WHERE is_active = 1 AND role != 'admin'")
    elif user["role"] == "manager":
        rows = q("SELECT id FROM users WHERE is_active = 1 AND manager_id = ?", (user["id"],))
    else:
        return [user["id"]]
    return [r["id"] for r in rows]


def request_scope(user):
    """SQL fragment (alias u = requester) limiting which leave requests a reviewer sees."""
    if user["role"] == "admin":
        return "1 = 1", ()
    return "u.manager_id = ?", (user["id"],)


def month_bounds(year, month):
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def month_rows(user, year, month, hols=None):
    """One row per day of the month with a status and check-in/out times."""
    hols = hols if hols is not None else holiday_map()
    first, last = month_bounds(year, month)
    att = {r["date"]: r for r in q(
        "SELECT * FROM attendance WHERE user_id=? AND date BETWEEN ? AND ?",
        (user["id"], first.isoformat(), last.isoformat()))}
    leave_days = set()
    for lv in q("SELECT from_date, to_date FROM leave_requests WHERE user_id=? AND status='Approved' "
                "AND from_date <= ? AND to_date >= ?", (user["id"], last.isoformat(), first.isoformat())):
        d = max(date.fromisoformat(lv["from_date"]), first)
        end = min(date.fromisoformat(lv["to_date"]), last)
        while d <= end:
            if d.weekday() < 5 and d.isoformat() not in hols:
                leave_days.add(d.isoformat())
            d += timedelta(days=1)

    join = date.fromisoformat(user["join_date"])
    today = date.today()
    rows, d = [], first
    while d <= last:
        iso = d.isoformat()
        rec = att.get(iso)
        note = hols.get(iso, "")
        if rec:
            status = rec["status"]
        elif iso in leave_days:
            status = "On Leave"
        elif iso in hols:
            status = "Holiday"
        elif d.weekday() >= 5:
            status = "Weekend"
        elif d < join or d >= today:
            status = "-"
        else:
            status = "Absent"
        rows.append({"date": d, "status": status, "note": note,
                     "check_in": rec["check_in"] if rec else "",
                     "check_out": rec["check_out"] if rec else ""})
        d += timedelta(days=1)
    return rows


def month_summary(user, year, month, hols=None):
    counts = {"Present": 0, "Late": 0, "On Leave": 0, "Absent": 0}
    for r in month_rows(user, year, month, hols):
        if r["status"] in counts:
            counts[r["status"]] += 1
    return counts


def leaves_between(ids, start, end):
    return q(
        "SELECT lr.*, u.name AS employee_name, lt.name AS leave_type FROM leave_requests lr "
        "JOIN users u ON u.id = lr.user_id JOIN leave_types lt ON lt.id = lr.leave_type_id "
        f"WHERE lr.status = 'Approved' AND lr.user_id IN ({marks(ids)}) "
        "AND lr.from_date <= ? AND lr.to_date >= ? ORDER BY lr.from_date",
        (*ids, end.isoformat(), start.isoformat()))


def parse_month(text):
    try:
        y, m = text.split("-")
        y, m = int(y), int(m)
        date(y, m, 1)
        return y, m
    except Exception:
        t = date.today()
        return t.year, t.month


# --------------------------------------------------------------------------
# Login / profile
# --------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = q("SELECT * FROM users WHERE lower(email) = ? AND is_active = 1", (email,), one=True)
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session["uid"] = user["id"]
            return redirect(url_for("index"))
        flash("Email or password is incorrect.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        form = request.form.get("form")
        if form == "details":
            name = request.form.get("name", "").strip()
            if not name:
                flash("Name can't be empty.", "danger")
            else:
                run("UPDATE users SET name = ? WHERE id = ?", (name, g.user["id"]))
                flash("Profile updated.", "success")
        elif form == "password":
            current = request.form.get("current", "")
            new = request.form.get("new", "")
            if not check_password_hash(g.user["password_hash"], current):
                flash("Current password is incorrect.", "danger")
            elif len(new) < 6:
                flash("New password must be at least 6 characters.", "danger")
            elif new != request.form.get("confirm", ""):
                flash("New password and confirmation don't match.", "danger")
            else:
                run("UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(new), g.user["id"]))
                flash("Password changed.", "success")
        return redirect(url_for("profile"))
    info = q("SELECT u.*, d.name AS dept, m.name AS manager FROM users u "
             "LEFT JOIN departments d ON d.id = u.department_id "
             "LEFT JOIN users m ON m.id = u.manager_id WHERE u.id = ?", (g.user["id"],), one=True)
    return render_template("profile.html", info=info)


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    user, today = g.user, date.today()
    ids = scope_ids(user)
    ctx = {"balances": [], "att_today": None, "on_leave_today": False, "recent": []}

    if user["role"] != "admin":
        ensure_balances(user["id"], today.year)
        ctx["balances"] = q(
            "SELECT lt.name, b.total, b.used FROM leave_balances b "
            "JOIN leave_types lt ON lt.id = b.leave_type_id "
            "WHERE b.user_id = ? AND b.year = ? ORDER BY lt.id", (user["id"], today.year))
        ctx["att_today"] = q("SELECT * FROM attendance WHERE user_id=? AND date=?",
                             (user["id"], today.isoformat()), one=True)
        ctx["on_leave_today"] = bool(leaves_between([user["id"]], today, today))
        ctx["recent"] = q(
            "SELECT lr.*, lt.name AS leave_type FROM leave_requests lr "
            "JOIN leave_types lt ON lt.id = lr.leave_type_id WHERE lr.user_id = ? "
            "ORDER BY lr.applied_on DESC LIMIT 5", (user["id"],))

    # Charts and stats for the people in this user's scope
    hols = holiday_map()
    leave_chart = q(
        "SELECT lt.name, SUM(lr.days) AS d FROM leave_requests lr "
        "JOIN leave_types lt ON lt.id = lr.leave_type_id "
        f"WHERE lr.status = 'Approved' AND lr.user_id IN ({marks(ids)}) "
        "AND substr(lr.from_date, 1, 4) = ? GROUP BY lt.name ORDER BY lt.id",
        (*ids, str(today.year)))
    totals = {"Present": 0, "Late": 0, "On Leave": 0, "Absent": 0}
    for row in q(f"SELECT * FROM users WHERE id IN ({marks(ids)})", ids):
        for k, v in month_summary(row, today.year, today.month, hols).items():
            totals[k] += v

    week_start = today - timedelta(days=today.weekday())
    stats, week_leaves = {}, []
    if user["role"] in ("manager", "admin"):
        clause, args = request_scope(user)
        stats["pending"] = q("SELECT COUNT(*) c FROM leave_requests lr JOIN users u ON u.id = lr.user_id "
                             f"WHERE lr.status = 'Pending' AND {clause}", args, one=True)["c"]
        stats["people"] = len(ids)
        stats["on_leave"] = len({r["user_id"] for r in leaves_between(ids, today, today)})
        stats["present"] = q(f"SELECT COUNT(*) c FROM attendance WHERE date = ? AND user_id IN ({marks(ids)})",
                             (today.isoformat(), *ids), one=True)["c"]
        week_leaves = leaves_between(ids, week_start, week_start + timedelta(days=6))

    return render_template("dashboard.html", leave_chart=[[r["name"], r["d"]] for r in leave_chart],
                           att_chart=totals, stats=stats, week_leaves=week_leaves,
                           week_start=week_start, **ctx)


# --------------------------------------------------------------------------
# Attendance
# --------------------------------------------------------------------------
def back():
    if request.form.get("next") == "attendance":
        return redirect(url_for("attendance"))
    return redirect(url_for("index"))


@app.post("/attendance/check-in")
@role_required("employee", "manager")
def check_in():
    today = date.today()
    if leaves_between([g.user["id"]], today, today):
        flash("You're on approved leave today, so check-in is disabled.", "warning")
        return back()
    if q("SELECT 1 FROM attendance WHERE user_id=? AND date=?", (g.user["id"], today.isoformat()), one=True):
        flash("You've already checked in today.", "warning")
        return back()
    now = datetime.now().strftime("%H:%M")
    status = "Late" if now > LATE_AFTER else "Present"
    run("INSERT INTO attendance (user_id, date, check_in, status) VALUES (?, ?, ?, ?)",
        (g.user["id"], today.isoformat(), now, status))
    flash(f"Checked in at {now}" + (" (marked late)." if status == "Late" else "."), "success")
    return back()


@app.post("/attendance/check-out")
@role_required("employee", "manager")
def check_out():
    today = date.today().isoformat()
    rec = q("SELECT * FROM attendance WHERE user_id=? AND date=?", (g.user["id"], today), one=True)
    if not rec:
        flash("Check in first before checking out.", "warning")
    elif rec["check_out"]:
        flash("You've already checked out today.", "warning")
    else:
        now = datetime.now().strftime("%H:%M")
        run("UPDATE attendance SET check_out = ? WHERE id = ?", (now, rec["id"]))
        flash(f"Checked out at {now}.", "success")
    return back()


@app.route("/attendance")
@role_required("employee", "manager")
def attendance():
    year, month = parse_month(request.args.get("month", ""))
    rows = month_rows(g.user, year, month)
    summary = {"Present": 0, "Late": 0, "On Leave": 0, "Absent": 0}
    for r in rows:
        if r["status"] in summary:
            summary[r["status"]] += 1
    first = date(year, month, 1)
    prev_m = (first - timedelta(days=1)).strftime("%Y-%m")
    next_m = (month_bounds(year, month)[1] + timedelta(days=1)).strftime("%Y-%m")
    att_today = q("SELECT * FROM attendance WHERE user_id=? AND date=?",
                  (g.user["id"], date.today().isoformat()), one=True)
    return render_template("attendance.html", rows=rows, summary=summary, month_label=first.strftime("%B %Y"),
                           prev_m=prev_m, next_m=next_m, att_today=att_today)


# --------------------------------------------------------------------------
# Leave: apply, history, cancel
# --------------------------------------------------------------------------
@app.get("/api/leave-days")
@login_required
def api_leave_days():
    try:
        start = date.fromisoformat(request.args["from"])
        end = date.fromisoformat(request.args["to"])
    except (KeyError, ValueError):
        return {"days": 0}
    if end < start:
        return {"days": 0}
    half_day = request.args.get("half_day") == "1"
    return {"days": working_days(start, end, holiday_map(), half_day)}


@app.route("/leave/apply", methods=["GET", "POST"])
@role_required("employee", "manager")
def apply_leave():
    user, today = g.user, date.today()
    ensure_balances(user["id"], today.year)
    types = q("SELECT * FROM leave_types ORDER BY id")
    balances = {t["id"]: available_days(user["id"], t["id"], today.year) for t in types}

    if request.method == "POST":
        try:
            lt_id = int(request.form.get("leave_type_id", ""))
            start = date.fromisoformat(request.form.get("from_date", ""))
            end = date.fromisoformat(request.form.get("to_date", ""))
        except ValueError:
            flash("Choose a leave type and valid dates.", "danger")
            return render_template("apply_leave.html", types=types, balances=balances, form=request.form)

        reason = request.form.get("reason", "").strip()
        half_day = request.form.get("half_day") == "on"
        error = None
        days = working_days(start, end, holiday_map(), half_day)
        if lt_id not in balances:
            error = "Choose a valid leave type."
        elif end < start:
            error = "The end date can't be before the start date."
        elif start < today:
            error = "You can't apply for leave in the past."
        elif start.year != end.year:
            error = "Apply separately for each calendar year."
        elif half_day and start != end:
            error = "Half-day leave can only be applied for a single date."
        elif days == 0:
            error = "That range has no working days (weekends and holidays don't count)."
        elif not reason:
            error = "Give a short reason for the leave."
        else:
            clash = q("SELECT from_date, to_date FROM leave_requests WHERE user_id = ? "
                      "AND status IN ('Pending', 'Approved') AND from_date <= ? AND to_date >= ?",
                      (user["id"], end.isoformat(), start.isoformat()), one=True)
            if clash:
                error = (f"This overlaps a leave you already applied for "
                         f"({fdate(clash['from_date'])} to {fdate(clash['to_date'])}).")
            elif available_days(user["id"], lt_id, start.year) < days:
                error = (f"Not enough balance. You need {fmt_days(days)} day(s) but have "
                         f"{fmt_days(max(available_days(user['id'], lt_id, start.year), 0))} available.")
        if error:
            flash(error, "danger")
            return render_template("apply_leave.html", types=types, balances=balances, form=request.form)

        run("INSERT INTO leave_requests (user_id, leave_type_id, from_date, to_date, days, reason, applied_on) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user["id"], lt_id, start.isoformat(), end.isoformat(), days, reason,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        flash(f"Leave request sent for {fmt_days(days)} working day(s).", "success")
        return redirect(url_for("my_leaves"))
    return render_template("apply_leave.html", types=types, balances=balances, form={})


@app.route("/leave/mine")
@role_required("employee", "manager")
def my_leaves():
    rows = q("SELECT lr.*, lt.name AS leave_type, r.name AS reviewer FROM leave_requests lr "
             "JOIN leave_types lt ON lt.id = lr.leave_type_id "
             "LEFT JOIN users r ON r.id = lr.reviewed_by WHERE lr.user_id = ? "
             "ORDER BY lr.applied_on DESC", (g.user["id"],))
    return render_template("my_leaves.html", rows=rows, now=date.today().isoformat())


@app.post("/leave/<int:rid>/cancel")
@role_required("employee", "manager")
def cancel_leave(rid):
    r = q("SELECT * FROM leave_requests WHERE id = ? AND user_id = ?", (rid, g.user["id"]), one=True)
    if not r:
        flash("Leave request not found.", "danger")
    elif r["status"] == "Pending":
        run("UPDATE leave_requests SET status = 'Cancelled' WHERE id = ?", (rid,))
        flash("Request cancelled.", "success")
    elif r["status"] == "Approved" and date.fromisoformat(r["from_date"]) > date.today():
        year = int(r["from_date"][:4])
        run("UPDATE leave_balances SET used = used - ? WHERE user_id=? AND leave_type_id=? AND year=?",
            (r["days"], r["user_id"], r["leave_type_id"], year))
        run("UPDATE leave_requests SET status = 'Cancelled' WHERE id = ?", (rid,))
        flash(f"Approved leave cancelled. {fmt_days(r['days'])} day(s) returned to your balance.", "success")
    else:
        flash("This request can't be cancelled.", "warning")
    return redirect(url_for("my_leaves"))


# --------------------------------------------------------------------------
# Manager / admin: review requests
# --------------------------------------------------------------------------
@app.route("/requests")
@role_required("manager", "admin")
def requests_page():
    clause, args = request_scope(g.user)
    base = ("SELECT lr.*, u.name AS employee_name, lt.name AS leave_type FROM leave_requests lr "
            "JOIN users u ON u.id = lr.user_id JOIN leave_types lt ON lt.id = lr.leave_type_id ")
    pending = []
    for r in q(base + f"WHERE lr.status = 'Pending' AND {clause} ORDER BY lr.applied_on", args):
        item = dict(r)
        item["left"] = available_days(r["user_id"], r["leave_type_id"], int(r["from_date"][:4])) + r["days"]
        pending.append(item)
    history = q(base + f"WHERE lr.status != 'Pending' AND {clause} ORDER BY lr.applied_on DESC LIMIT 20", args)
    return render_template("requests.html", pending=pending, history=history)


@app.post("/requests/<int:rid>/review")
@role_required("manager", "admin")
def review(rid):
    r = q("SELECT lr.*, u.manager_id FROM leave_requests lr JOIN users u ON u.id = lr.user_id "
          "WHERE lr.id = ?", (rid,), one=True)
    if not r or r["status"] != "Pending":
        flash("That request is no longer pending.", "warning")
        return redirect(url_for("requests_page"))
    if g.user["role"] == "manager" and r["manager_id"] != g.user["id"]:
        flash("You can only review requests from your own team.", "danger")
        return redirect(url_for("requests_page"))

    action = request.form.get("action")
    comment = request.form.get("comment", "").strip()
    if action == "approve":
        year = int(r["from_date"][:4])
        ensure_balances(r["user_id"], year)
        bal = q("SELECT total, used FROM leave_balances WHERE user_id=? AND leave_type_id=? AND year=?",
                (r["user_id"], r["leave_type_id"], year), one=True)
        if bal["total"] - bal["used"] < r["days"]:
            flash("The employee doesn't have enough balance left for this request.", "danger")
            return redirect(url_for("requests_page"))
        run("UPDATE leave_balances SET used = used + ? WHERE user_id=? AND leave_type_id=? AND year=?",
            (r["days"], r["user_id"], r["leave_type_id"], year))
        status = "Approved"
    elif action == "reject":
        status = "Rejected"
    else:
        flash("Choose approve or reject.", "danger")
        return redirect(url_for("requests_page"))
    run("UPDATE leave_requests SET status = ?, reviewed_by = ?, manager_comment = ? WHERE id = ?",
        (status, g.user["id"], comment, rid))
    flash(f"Request {status.lower()}.", "success")
    return redirect(url_for("requests_page"))


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------
def report_data(user, year, month):
    ids = scope_ids(user)
    hols = holiday_map()
    people = q("SELECT u.*, d.name AS dept FROM users u LEFT JOIN departments d ON d.id = u.department_id "
               f"WHERE u.id IN ({marks(ids)}) ORDER BY u.name", ids)
    att = []
    for p in people:
        s = month_summary(p, year, month, hols)
        att.append({"name": p["name"], "dept": p["dept"] or "-", **s})

    types = q("SELECT * FROM leave_types ORDER BY id")
    used = {}
    for r in q("SELECT user_id, leave_type_id, SUM(days) AS d FROM leave_requests WHERE status = 'Approved' "
               f"AND user_id IN ({marks(ids)}) AND substr(from_date, 1, 4) = ? "
               "GROUP BY user_id, leave_type_id", (*ids, str(year))):
        used[(r["user_id"], r["leave_type_id"])] = r["d"]
    leave = []
    for p in people:
        per_type = [used.get((p["id"], t["id"]), 0) for t in types]
        leave.append({"name": p["name"], "dept": p["dept"] or "-", "per_type": per_type, "total": sum(per_type)})

    dept = {}
    for a, l in zip(att, leave):
        d = dept.setdefault(a["dept"], {"people": 0, "Present": 0, "Late": 0, "On Leave": 0, "Absent": 0, "leave": 0})
        d["people"] += 1
        for k in ("Present", "Late", "On Leave", "Absent"):
            d[k] += a[k]
        d["leave"] += l["total"]
    return att, leave, types, dept


@app.route("/reports")
@role_required("manager", "admin")
def reports():
    year, month = parse_month(request.args.get("month", ""))
    att, leave, types, dept = report_data(g.user, year, month)
    return render_template("reports.html", att=att, leave=leave, types=types, dept=dept,
                           month=f"{year:04d}-{month:02d}", month_label=date(year, month, 1).strftime("%B %Y"),
                           year=year)


@app.route("/reports/export/<kind>")
@role_required("manager", "admin")
def export_csv(kind):
    year, month = parse_month(request.args.get("month", ""))
    out = io.StringIO()
    w = csv.writer(out)
    if kind == "attendance":
        att, _, _, _ = report_data(g.user, year, month)
        w.writerow(["Employee", "Department", "Present", "Late", "On leave", "Absent"])
        for a in att:
            w.writerow([a["name"], a["dept"], a["Present"], a["Late"], a["On Leave"], a["Absent"]])
        name = f"attendance_{year}-{month:02d}.csv"
    elif kind == "leaves":
        ids = scope_ids(g.user)
        w.writerow(["Employee", "Leave type", "From", "To", "Days", "Status", "Reason", "Manager comment"])
        for r in q("SELECT lr.*, u.name AS employee_name, lt.name AS leave_type FROM leave_requests lr "
                   "JOIN users u ON u.id = lr.user_id JOIN leave_types lt ON lt.id = lr.leave_type_id "
                   f"WHERE lr.user_id IN ({marks(ids)}) AND substr(lr.from_date, 1, 4) = ? "
                   "ORDER BY lr.from_date", (*ids, str(year))):
            w.writerow([r["employee_name"], r["leave_type"], r["from_date"], r["to_date"], r["days"],
                        r["status"], r["reason"], r["manager_comment"] or ""])
        name = f"leave_requests_{year}.csv"
    else:
        flash("Unknown report.", "danger")
        return redirect(url_for("reports"))
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


# --------------------------------------------------------------------------
# Admin: employees
# --------------------------------------------------------------------------
@app.route("/admin/employees")
@role_required("admin")
def admin_employees():
    rows = q("SELECT u.*, d.name AS dept, m.name AS manager FROM users u "
             "LEFT JOIN departments d ON d.id = u.department_id "
             "LEFT JOIN users m ON m.id = u.manager_id ORDER BY u.is_active DESC, u.name")
    return render_template("admin_employees.html", rows=rows)


def employee_form_data():
    return (q("SELECT * FROM departments ORDER BY name"),
            q("SELECT id, name FROM users WHERE is_active = 1 AND role IN ('manager', 'admin') ORDER BY name"))


def save_employee(emp_id=None):
    f = request.form
    name, email = f.get("name", "").strip(), f.get("email", "").strip().lower()
    role, password = f.get("role", ""), f.get("password", "")
    dept = f.get("department_id") or None
    manager = f.get("manager_id") or None
    error = None
    try:
        date.fromisoformat(f.get("join_date", ""))
    except ValueError:
        error = "Enter a valid join date."
    if not name or not email:
        error = "Name and email are required."
    elif role not in ("admin", "manager", "employee"):
        error = "Choose a role."
    elif emp_id is None and len(password) < 6:
        error = "Password must be at least 6 characters."
    elif emp_id is not None and password and len(password) < 6:
        error = "New password must be at least 6 characters."
    elif emp_id is not None and manager and int(manager) == emp_id:
        error = "A person can't be their own manager."
    if error:
        return error

    try:
        if emp_id is None:
            new_id = run("INSERT INTO users (name, email, password_hash, role, department_id, manager_id, join_date) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (name, email, generate_password_hash(password), role, dept, manager, f["join_date"]))
            ensure_balances(new_id, date.today().year)
        else:
            run("UPDATE users SET name=?, email=?, role=?, department_id=?, manager_id=?, join_date=? WHERE id=?",
                (name, email, role, dept, manager, f["join_date"], emp_id))
            if password:
                run("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), emp_id))
    except sqlite3.IntegrityError:
        return "That email is already used by another account."
    return None


@app.route("/admin/employees/new", methods=["GET", "POST"])
@role_required("admin")
def employee_new():
    if request.method == "POST":
        error = save_employee()
        if not error:
            flash("Employee added.", "success")
            return redirect(url_for("admin_employees"))
        flash(error, "danger")
    depts, managers = employee_form_data()
    return render_template("employee_form.html", emp=None, form=request.form if request.method == "POST"
                           else {"join_date": date.today().isoformat(), "role": "employee"},
                           depts=depts, managers=managers)


@app.route("/admin/employees/<int:eid>/edit", methods=["GET", "POST"])
@role_required("admin")
def employee_edit(eid):
    emp = q("SELECT * FROM users WHERE id = ?", (eid,), one=True)
    if not emp:
        flash("Employee not found.", "danger")
        return redirect(url_for("admin_employees"))
    if request.method == "POST":
        error = save_employee(eid)
        if not error:
            flash("Employee updated.", "success")
            return redirect(url_for("admin_employees"))
        flash(error, "danger")
    depts, managers = employee_form_data()
    return render_template("employee_form.html", emp=emp, form=request.form if request.method == "POST" else dict(emp),
                           depts=depts, managers=managers)


@app.post("/admin/employees/<int:eid>/toggle")
@role_required("admin")
def employee_toggle(eid):
    if eid == g.user["id"]:
        flash("You can't deactivate your own account.", "danger")
    else:
        run("UPDATE users SET is_active = 1 - is_active WHERE id = ?", (eid,))
        flash("Account status changed.", "success")
    return redirect(url_for("admin_employees"))


# --------------------------------------------------------------------------
# Admin: departments, leave types, holidays
# --------------------------------------------------------------------------
@app.route("/admin/departments", methods=["GET", "POST"])
@role_required("admin")
def admin_departments():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Enter a department name.", "danger")
        else:
            try:
                run("INSERT INTO departments (name) VALUES (?)", (name,))
                flash("Department added.", "success")
            except sqlite3.IntegrityError:
                flash("That department already exists.", "danger")
        return redirect(url_for("admin_departments"))
    rows = q("SELECT d.*, (SELECT COUNT(*) FROM users u WHERE u.department_id = d.id) AS members "
             "FROM departments d ORDER BY d.name")
    return render_template("admin_departments.html", rows=rows)


@app.post("/admin/departments/<int:did>/delete")
@role_required("admin")
def department_delete(did):
    if q("SELECT 1 FROM users WHERE department_id = ?", (did,), one=True):
        flash("Move the employees out of this department before deleting it.", "danger")
    else:
        run("DELETE FROM departments WHERE id = ?", (did,))
        flash("Department deleted.", "success")
    return redirect(url_for("admin_departments"))


@app.route("/admin/leave-types", methods=["GET", "POST"])
@role_required("admin")
def admin_leave_types():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        try:
            quota = int(request.form.get("yearly_quota", ""))
            assert quota >= 0
        except (ValueError, AssertionError):
            flash("Enter the yearly quota as a whole number.", "danger")
            return redirect(url_for("admin_leave_types"))
        if not name:
            flash("Enter a leave type name.", "danger")
        else:
            try:
                run("INSERT INTO leave_types (name, yearly_quota) VALUES (?, ?)", (name, quota))
                for u in q("SELECT id FROM users"):
                    ensure_balances(u["id"], date.today().year)
                flash("Leave type added.", "success")
            except sqlite3.IntegrityError:
                flash("That leave type already exists.", "danger")
        return redirect(url_for("admin_leave_types"))
    return render_template("admin_leave_types.html", rows=q("SELECT * FROM leave_types ORDER BY id"))


@app.post("/admin/leave-types/<int:lid>/quota")
@role_required("admin")
def leave_type_quota(lid):
    try:
        quota = int(request.form.get("yearly_quota", ""))
        assert quota >= 0
    except (ValueError, AssertionError):
        flash("Enter the yearly quota as a whole number.", "danger")
        return redirect(url_for("admin_leave_types"))
    run("UPDATE leave_types SET yearly_quota = ? WHERE id = ?", (quota, lid))
    run("UPDATE leave_balances SET total = ? WHERE leave_type_id = ? AND year = ?",
        (quota, lid, date.today().year))
    flash("Quota updated for this year's balances.", "success")
    return redirect(url_for("admin_leave_types"))


@app.route("/admin/holidays", methods=["GET", "POST"])
@role_required("admin")
def admin_holidays():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        try:
            day = date.fromisoformat(request.form.get("date", ""))
        except ValueError:
            flash("Enter a valid date.", "danger")
            return redirect(url_for("admin_holidays"))
        if not name:
            flash("Enter a holiday name.", "danger")
        else:
            try:
                run("INSERT INTO holidays (date, name) VALUES (?, ?)", (day.isoformat(), name))
                flash("Holiday added.", "success")
            except sqlite3.IntegrityError:
                flash("There's already a holiday on that date.", "danger")
        return redirect(url_for("admin_holidays"))
    return render_template("admin_holidays.html", rows=q("SELECT * FROM holidays ORDER BY date"))


@app.post("/admin/holidays/<int:hid>/delete")
@role_required("admin")
def holiday_delete(hid):
    run("DELETE FROM holidays WHERE id = ?", (hid,))
    flash("Holiday removed.", "success")
    return redirect(url_for("admin_holidays"))


init_db()

if __name__ == "__main__":
    app.run(debug=True)

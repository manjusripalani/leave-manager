# Employee Leave and Attendance Management System

A Flask + SQLite web app where employees mark attendance and apply for leave, managers approve or reject requests, and HR admins manage people, leave rules, and reports.

## Run it

```bash
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. The database file (`leave_manager.db`) and demo data are created automatically on the first run. Delete the file to start fresh.

## Demo accounts

| Role     | Email                | Password   |
|----------|----------------------|------------|
| Admin    | admin@company.com    | admin123   |
| Manager  | manager@company.com  | manager123 |
| Employee | arjun@company.com    | emp123     |
| Employee | meena@company.com    | emp123     |

Arjun and Meena report to the manager (Priya). The manager reports to the admin.

## Demo script for your viva

1. Log in as **Arjun**: check in, then apply for 3 days of Casual leave.
2. Log in as the **manager**: open Requests, approve it with a comment.
3. Log back in as Arjun: the balance dropped and the comment shows under My leaves. Cancel the leave to see the balance come back.
4. Log in as **admin**: add an employee, add a holiday, open Reports and export a CSV.

## Features

- Login and logout with hashed passwords, role-based access (admin, manager, employee)
- Check-in and check-out, late marking after 09:30, monthly attendance view
- Leave application with automatic working-day count (skips weekends and holidays), live day counter on the form
- Validation: no past dates, no overlapping requests, no applying beyond the available balance (pending requests reserve balance)
- Approval workflow with comments, automatic balance deduction, refund on cancellation
- Dashboards with charts (attendance this month, approved leave days by type) and a "who's away this week" list
- Reports by employee and department, CSV export
- Admin pages for employees, departments, leave types and quotas, and holidays
- Profile page with password change

## Project structure

```
app.py               all routes, business logic, and the database schema
templates/           HTML pages (Jinja2 + Bootstrap 5)
static/style.css     styling
test_app.py          22 automated tests
requirements.txt
```

## Database tables

`users`, `departments`, `leave_types`, `leave_balances`, `leave_requests`, `attendance`, `holidays`. The schema is at the top of `app.py`.

## Run the tests

```bash
python -m unittest -v
```

The tests use a temporary database, so they never touch your real data. You can paste the test names into the Testing chapter of your report.

## Settings

- `LATE_AFTER` in `app.py` sets the late cut-off time.
- Set the `SECRET_KEY` environment variable before deploying anywhere public.

## Future scope

Email notifications, CSRF protection on forms, half-day leave, biometric or location-based check-in, payroll integration, and a mobile app.

## Recent updates

- **CSRF protection**: every form now carries a hidden security token checked on submit.
- **Half-day leave**: when applying for a single date, a "Half day (0.5 day)" checkbox appears.

## Deploying it online (so you can demo from a link)

**Option: Render (free tier)**
1. Create a free account at render.com and a free account at github.com if you don't have one.
2. Upload this `leave_manager` folder as a new GitHub repository (GitHub Desktop or the "upload files" button on github.com both work).
3. Add a file named `Procfile` (no extension) in the folder with this single line:
   ```
   web: gunicorn app:app
   ```
4. Add `gunicorn` to `requirements.txt` (one line, under Flask).
5. On Render: New > Web Service > connect your GitHub repo > Build command `pip install -r requirements.txt` > Start command `gunicorn app:app` > Create Web Service.
6. Render gives you a live `https://yourapp.onrender.com` link. Use that for your demo instead of localhost.

**Option: PythonAnywhere (also free, simpler for beginners)**
1. Create a free account at pythonanywhere.com.
2. Use the "Files" tab to upload the `leave_manager` folder (or use their Bash console with `git clone` if you used GitHub).
3. Go to the "Web" tab > Add a new web app > Flask > point it at `app.py`.
4. Reload the web app. Your project is now live at `yourusername.pythonanywhere.com`.

Either way, keep a laptop copy running locally as backup in case the free hosting is slow during your demo.

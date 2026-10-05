"""Automated tests. Run with:  python -m unittest -v"""
import os
import tempfile
import unittest
from datetime import date, timedelta

import app as leave_app


def next_weekday(start, weekday):
    """First date on/after start that falls on the given weekday (0 = Monday)."""
    return start + timedelta(days=(weekday - start.weekday()) % 7)


class LeaveManagerTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        leave_app.DB_PATH = self.path
        leave_app.init_db()
        leave_app.app.config["TESTING"] = True
        self.client = leave_app.app.test_client()

    def tearDown(self):
        os.remove(self.path)

    # helpers -------------------------------------------------------------
    def login(self, email, password):
        self.client.get("/logout")
        return self.client.post("/login", data={"email": email, "password": password}, follow_redirects=True)

    def as_employee(self):
        return self.login("arjun@company.com", "emp123")

    def as_manager(self):
        return self.login("manager@company.com", "manager123")

    def as_admin(self):
        return self.login("admin@company.com", "admin123")

    def apply(self, start, end, type_id=1, reason="Family function"):
        return self.client.post("/leave/apply", data={
            "leave_type_id": type_id, "from_date": start.isoformat(),
            "to_date": end.isoformat(), "reason": reason}, follow_redirects=True)

    def query(self, sql, args=()):
        con = leave_app.sqlite3.connect(self.path)
        con.row_factory = leave_app.sqlite3.Row
        rows = con.execute(sql, args).fetchall()
        con.close()
        return rows

    def clean_days(self):
        """Three consecutive weekdays in the future that avoid seeded holidays."""
        d = next_weekday(date.today() + timedelta(days=14), 0)
        holidays = {r["date"] for r in self.query("SELECT date FROM holidays")}
        while any((d + timedelta(days=i)).isoformat() in holidays for i in range(3)):
            d += timedelta(days=7)
        return d, d + timedelta(days=2)

    # authentication ------------------------------------------------------
    def test_login_and_logout(self):
        self.assertIn(b"Email or password is incorrect", self.login("admin@company.com", "wrong").data)
        self.assertIn(b"Hello, HR", self.as_admin().data)
        self.assertIn(b"Log in", self.client.get("/logout", follow_redirects=True).data)

    def test_pages_require_login(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.get("/admin/employees").status_code, 302)

    def test_role_restrictions(self):
        self.as_employee()
        self.assertIn(b"don&#39;t have access", self.client.get("/admin/employees", follow_redirects=True).data)
        self.assertIn(b"don&#39;t have access", self.client.get("/requests", follow_redirects=True).data)
        self.as_admin()
        self.assertIn(b"don&#39;t have access", self.client.get("/leave/apply", follow_redirects=True).data)

    def test_every_page_renders_for_every_role(self):
        pages = {
            "employee": ["/", "/attendance", "/leave/apply", "/leave/mine", "/profile"],
            "manager": ["/", "/attendance", "/leave/apply", "/leave/mine", "/requests", "/reports", "/profile"],
            "admin": ["/", "/requests", "/reports", "/profile", "/admin/employees", "/admin/employees/new",
                      "/admin/employees/3/edit", "/admin/departments", "/admin/leave-types", "/admin/holidays"],
        }
        for role, paths in pages.items():
            getattr(self, f"as_{role}")()
            for path in paths:
                self.assertEqual(self.client.get(path).status_code, 200, f"{role} {path}")

    # attendance ----------------------------------------------------------
    def test_check_in_and_out(self):
        self.as_employee()
        self.assertIn(b"Checked in at", self.client.post("/attendance/check-in", follow_redirects=True).data)
        self.assertIn(b"already checked in", self.client.post("/attendance/check-in", follow_redirects=True).data)
        self.assertIn(b"Checked out at", self.client.post("/attendance/check-out", follow_redirects=True).data)
        self.assertIn(b"already checked out", self.client.post("/attendance/check-out", follow_redirects=True).data)
        self.assertEqual(len(self.query("SELECT * FROM attendance")), 1)

    def test_check_out_needs_check_in(self):
        self.as_employee()
        self.assertIn(b"Check in first", self.client.post("/attendance/check-out", follow_redirects=True).data)

    def test_late_check_in(self):
        self.as_employee()
        original = leave_app.LATE_AFTER
        leave_app.LATE_AFTER = "00:00"
        try:
            self.client.post("/attendance/check-in")
        finally:
            leave_app.LATE_AFTER = original
        self.assertEqual(self.query("SELECT status FROM attendance")[0]["status"], "Late")

    # leave application ---------------------------------------------------
    def test_apply_counts_working_days_only(self):
        self.as_employee()
        start = next_weekday(date.today() + timedelta(days=21), 4)   # a Friday
        end = start + timedelta(days=3)                              # to Monday
        self.apply(start, end)
        row = self.query("SELECT days FROM leave_requests")[0]
        holidays = {r["date"] for r in self.query("SELECT date FROM holidays")}
        expected = sum(1 for i in range(4) if (start + timedelta(days=i)).weekday() < 5
                       and (start + timedelta(days=i)).isoformat() not in holidays)
        self.assertEqual(row["days"], expected)

    def test_apply_validations(self):
        self.as_employee()
        start, end = self.clean_days()
        self.assertIn(b"in the past", self.apply(date.today() - timedelta(days=3), date.today()).data)
        self.assertIn(b"can&#39;t be before", self.apply(end, start).data)
        saturday = next_weekday(date.today() + timedelta(days=14), 5)
        self.assertIn(b"no working days", self.apply(saturday, saturday + timedelta(days=1)).data)
        self.assertIn(b"Not enough balance", self.apply(start, start + timedelta(days=60)).data)
        self.assertIn(b"short reason", self.apply(start, end, reason="  ").data)
        self.assertEqual(len(self.query("SELECT * FROM leave_requests")), 0)

    def test_overlap_is_blocked(self):
        self.as_employee()
        start, end = self.clean_days()
        self.assertIn(b"Leave request sent", self.apply(start, end).data)
        self.assertIn(b"overlaps", self.apply(end, end + timedelta(days=2), type_id=2).data)
        self.assertEqual(len(self.query("SELECT * FROM leave_requests")), 1)

    def test_pending_requests_reserve_balance(self):
        self.as_employee()
        start, end = self.clean_days()
        self.assertIn(b"Leave request sent", self.apply(start, end, type_id=2).data)      # 3 of 10 Sick days
        later = end + timedelta(days=7)
        # 3 weeks of Sick leave needs far more than the 7 days still available
        self.assertIn(b"Not enough balance", self.apply(later, later + timedelta(days=20), type_id=2).data)
        self.assertEqual(len(self.query("SELECT * FROM leave_requests")), 1)

    def test_half_day_leave(self):
        self.as_employee()
        start = next_weekday(date.today() + timedelta(days=10), 1)   # a clean Tuesday
        holidays = {r["date"] for r in self.query("SELECT date FROM holidays")}
        while start.isoformat() in holidays:
            start += timedelta(days=7)
        self.assertIn(b"0.5 working day", self.client.post("/leave/apply", data={
            "leave_type_id": 1, "from_date": start.isoformat(), "to_date": start.isoformat(),
            "reason": "Doctor visit", "half_day": "on"}, follow_redirects=True).data)
        self.assertEqual(self.query("SELECT days FROM leave_requests")[0]["days"], 0.5)

    def test_half_day_rejected_for_date_range(self):
        self.as_employee()
        start, end = self.clean_days()
        self.assertIn(b"single date", self.apply(start, end).data.replace(b"", b"") if False else
                      self.client.post("/leave/apply", data={
                          "leave_type_id": 1, "from_date": start.isoformat(), "to_date": end.isoformat(),
                          "reason": "x", "half_day": "on"}, follow_redirects=True).data)
        self.assertEqual(len(self.query("SELECT * FROM leave_requests")), 0)

    def test_csrf_blocks_form_without_token(self):
        self.as_employee()
        self.app_testing_off()
        try:
            resp = self.client.post("/attendance/check-in", follow_redirects=True)
            self.assertIn(b"session timed out", resp.data)
            self.assertEqual(len(self.query("SELECT * FROM attendance")), 0)
        finally:
            leave_app.app.config["TESTING"] = True

    def app_testing_off(self):
        leave_app.app.config["TESTING"] = False

    # approval workflow ---------------------------------------------------
    def test_approval_deducts_balance_and_cancel_refunds(self):
        self.as_employee()
        start, end = self.clean_days()
        self.apply(start, end)
        req = self.query("SELECT * FROM leave_requests")[0]

        self.as_manager()
        self.assertIn(b"Arjun Kumar", self.client.get("/requests").data)
        self.assertIn(b"Request approved", self.client.post(
            f"/requests/{req['id']}/review", data={"action": "approve", "comment": "Enjoy"}, follow_redirects=True).data)
        bal = self.query("SELECT used FROM leave_balances WHERE user_id = 3 AND leave_type_id = 1 AND year = ?",
                         (start.year,))[0]
        self.assertEqual(bal["used"], req["days"])

        self.as_employee()
        self.assertIn(b"Enjoy", self.client.get("/leave/mine").data)
        self.assertIn(b"returned to your balance", self.client.post(
            f"/leave/{req['id']}/cancel", follow_redirects=True).data)
        bal = self.query("SELECT used FROM leave_balances WHERE user_id = 3 AND leave_type_id = 1 AND year = ?",
                         (start.year,))[0]
        self.assertEqual(bal["used"], 0)

    def test_rejection_keeps_balance(self):
        self.as_employee()
        start, end = self.clean_days()
        self.apply(start, end)
        rid = self.query("SELECT id FROM leave_requests")[0]["id"]
        self.as_manager()
        self.client.post(f"/requests/{rid}/review", data={"action": "reject", "comment": "Busy week"})
        self.assertEqual(self.query("SELECT status FROM leave_requests")[0]["status"], "Rejected")
        self.assertEqual(self.query("SELECT SUM(used) s FROM leave_balances")[0]["s"], 0)

    def test_cannot_review_twice(self):
        self.as_employee()
        start, end = self.clean_days()
        self.apply(start, end)
        rid = self.query("SELECT id FROM leave_requests")[0]["id"]
        self.as_manager()
        self.client.post(f"/requests/{rid}/review", data={"action": "approve"})
        self.assertIn(b"no longer pending", self.client.post(
            f"/requests/{rid}/review", data={"action": "approve"}, follow_redirects=True).data)
        self.assertEqual(self.query("SELECT used FROM leave_balances WHERE user_id = 3 AND leave_type_id = 1")[0]["used"],
                         self.query("SELECT days FROM leave_requests")[0]["days"])

    def test_manager_cannot_review_other_teams(self):
        self.as_admin()
        self.client.post("/admin/employees/new", data={
            "name": "Other Manager", "email": "other@company.com", "role": "manager",
            "department_id": "3", "manager_id": "1", "join_date": date.today().isoformat(), "password": "secret1"})
        self.client.post("/admin/employees/new", data={
            "name": "Ravi", "email": "ravi@company.com", "role": "employee", "department_id": "3",
            "manager_id": "5", "join_date": "2026-01-01", "password": "secret1"})
        self.login("ravi@company.com", "secret1")
        start, end = self.clean_days()
        self.apply(start, end)
        rid = self.query("SELECT id FROM leave_requests")[0]["id"]
        self.as_manager()                                    # Priya is not Ravi's manager
        self.assertNotIn(b"Ravi", self.client.get("/requests").data)
        self.assertIn(b"only review requests from your own team", self.client.post(
            f"/requests/{rid}/review", data={"action": "approve"}, follow_redirects=True).data)
        self.as_admin()                                      # admin can review anyone
        self.assertIn(b"Request approved", self.client.post(
            f"/requests/{rid}/review", data={"action": "approve"}, follow_redirects=True).data)

    def test_employee_cannot_cancel_someone_elses_request(self):
        self.as_employee()
        start, end = self.clean_days()
        self.apply(start, end)
        rid = self.query("SELECT id FROM leave_requests")[0]["id"]
        self.login("meena@company.com", "emp123")
        self.assertIn(b"not found", self.client.post(f"/leave/{rid}/cancel", follow_redirects=True).data)
        self.assertEqual(self.query("SELECT status FROM leave_requests")[0]["status"], "Pending")

    # reports -------------------------------------------------------------
    def test_reports_and_csv(self):
        self.as_manager()
        page = self.client.get("/reports").data
        self.assertIn(b"Arjun Kumar", page)
        self.assertIn(b"Meena Iyer", page)
        csv_att = self.client.get("/reports/export/attendance")
        self.assertEqual(csv_att.mimetype, "text/csv")
        self.assertTrue(csv_att.data.startswith(b"Employee,Department"))
        self.assertTrue(self.client.get("/reports/export/leaves").data.startswith(b"Employee,Leave type"))

    def test_attendance_month_navigation(self):
        self.as_employee()
        self.assertEqual(self.client.get("/attendance?month=2026-02").status_code, 200)
        self.assertEqual(self.client.get("/attendance?month=garbage").status_code, 200)

    # admin ---------------------------------------------------------------
    def test_admin_employee_crud(self):
        self.as_admin()
        data = {"name": "Kavya", "email": "kavya@company.com", "role": "employee", "department_id": "1",
                "manager_id": "2", "join_date": "2026-03-01", "password": "abcdef"}
        self.assertIn(b"Employee added", self.client.post("/admin/employees/new", data=data, follow_redirects=True).data)
        self.assertIn(b"already used", self.client.post("/admin/employees/new", data=data, follow_redirects=True).data)
        self.assertIn(b"at least 6", self.client.post(
            "/admin/employees/new", data={**data, "email": "x@y.com", "password": "123"}, follow_redirects=True).data)
        new_id = self.query("SELECT id FROM users WHERE email = 'kavya@company.com'")[0]["id"]
        self.assertGreater(len(self.query("SELECT * FROM leave_balances WHERE user_id = ?", (new_id,))), 0)

        self.assertIn(b"Employee updated", self.client.post(
            f"/admin/employees/{new_id}/edit", data={**data, "name": "Kavya S", "password": ""}, follow_redirects=True).data)
        self.client.post(f"/admin/employees/{new_id}/toggle")
        self.assertIn(b"Email or password is incorrect", self.login("kavya@company.com", "abcdef").data)
        self.as_admin()
        self.assertIn(b"can&#39;t deactivate your own", self.client.post("/admin/employees/1/toggle", follow_redirects=True).data)

    def test_departments_leave_types_holidays(self):
        self.as_admin()
        self.assertIn(b"Department added", self.client.post("/admin/departments", data={"name": "Finance"}, follow_redirects=True).data)
        self.assertIn(b"already exists", self.client.post("/admin/departments", data={"name": "Finance"}, follow_redirects=True).data)
        self.assertIn(b"Move the employees", self.client.post("/admin/departments/1/delete", follow_redirects=True).data)
        fid = self.query("SELECT id FROM departments WHERE name = 'Finance'")[0]["id"]
        self.assertIn(b"Department deleted", self.client.post(f"/admin/departments/{fid}/delete", follow_redirects=True).data)

        self.assertIn(b"Leave type added", self.client.post(
            "/admin/leave-types", data={"name": "Study", "yearly_quota": "5"}, follow_redirects=True).data)
        self.assertEqual(len(self.query("SELECT * FROM leave_balances WHERE leave_type_id = 4")), 4)
        self.client.post("/admin/leave-types/1/quota", data={"yearly_quota": "20"})
        self.assertEqual({r["total"] for r in self.query("SELECT total FROM leave_balances WHERE leave_type_id = 1")}, {20})

        self.assertIn(b"Holiday added", self.client.post(
            "/admin/holidays", data={"date": "2026-11-08", "name": "Diwali"}, follow_redirects=True).data)
        self.assertIn(b"already a holiday", self.client.post(
            "/admin/holidays", data={"date": "2026-11-08", "name": "Again"}, follow_redirects=True).data)

    def test_holiday_changes_leave_day_count(self):
        self.as_admin()
        start, end = self.clean_days()
        self.client.post("/admin/holidays", data={"date": (start + timedelta(days=1)).isoformat(), "name": "Local holiday"})
        self.as_employee()
        self.apply(start, end)
        self.assertEqual(self.query("SELECT days FROM leave_requests")[0]["days"], 2)

    # profile -------------------------------------------------------------
    def test_change_password(self):
        self.as_employee()
        self.assertIn(b"Current password is incorrect", self.client.post(
            "/profile", data={"form": "password", "current": "no", "new": "newpass1", "confirm": "newpass1"}, follow_redirects=True).data)
        self.assertIn(b"Password changed", self.client.post(
            "/profile", data={"form": "password", "current": "emp123", "new": "newpass1", "confirm": "newpass1"}, follow_redirects=True).data)
        self.assertIn(b"Hello, Arjun", self.login("arjun@company.com", "newpass1").data)


if __name__ == "__main__":
    unittest.main()

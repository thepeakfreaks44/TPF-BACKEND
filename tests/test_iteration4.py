"""Iteration 4 backend tests — expenses, attendance, transporters, vehicles,
trek-schedules, settings, backup, reports, extended dashboard & transport math."""
import os
import uuid
import pytest
import requests
from datetime import date, timedelta

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL")
            or open("/app/frontend/.env").read().split("REACT_APP_BACKEND_URL=")[1].splitlines()[0]).rstrip("/")
API = f"{BASE_URL}/api"

ADMIN = ("admin@peakfreaks.com", "Peak@2026")
MANAGER = ("manager@peakfreaks.com", "Manager@2026")
STAFF = ("staff@peakfreaks.com", "Staff@2026")


def _login(email, pw):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": pw}, timeout=15)
    assert r.status_code == 200, f"login {email} failed: {r.status_code} {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="module")
def admin_token():
    return _login(*ADMIN)


@pytest.fixture(scope="module")
def staff_token():
    return _login(*STAFF)


@pytest.fixture(scope="module")
def admin_h(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture(scope="module")
def staff_h(staff_token):
    return {"Authorization": f"Bearer {staff_token}"}


# ---------------- DASHBOARD extended shape ----------------
def test_dashboard_summary_shape(admin_h):
    r = requests.get(f"{API}/dashboard/summary", headers=admin_h, timeout=20)
    assert r.status_code == 200
    d = r.json()
    for k in ("today", "money", "totals"):
        assert k in d
    for k in ("pax", "package_amount", "advance", "remaining", "expenses", "salary_expense"):
        assert k in d["today"], f"missing today.{k}"
    for k in ("rental_total", "rental_income", "rental_pending", "transport_expense",
              "total_income", "total_expenses", "net_profit"):
        assert k in d["money"], f"missing money.{k}"
    for k in ("active_guides", "staff_present", "staff_absent", "active_schedules"):
        assert k in d["totals"], f"missing totals.{k}"


# ---------------- EXPENSES CRUD + RBAC ----------------
def test_expenses_crud_and_rbac(admin_h, staff_h):
    payload = {"date": date.today().isoformat(), "category": "TEST_fuel",
               "amount": 250.5, "description": "TEST_exp", "payment_mode": "cash"}
    # staff blocked
    r = requests.post(f"{API}/expenses", headers=staff_h, json=payload)
    assert r.status_code == 403
    # admin create
    r = requests.post(f"{API}/expenses", headers=admin_h, json=payload)
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    assert r.json()["amount"] == 250.5
    # list
    r = requests.get(f"{API}/expenses", headers=admin_h)
    assert r.status_code == 200
    assert any(x["id"] == eid for x in r.json())
    # update as staff -> 403
    r = requests.put(f"{API}/expenses/{eid}", headers=staff_h, json={**payload, "amount": 300})
    assert r.status_code == 403
    # update admin
    r = requests.put(f"{API}/expenses/{eid}", headers=admin_h, json={**payload, "amount": 300})
    assert r.status_code == 200 and r.json()["amount"] == 300
    # delete as staff -> 403
    r = requests.delete(f"{API}/expenses/{eid}", headers=staff_h)
    assert r.status_code == 403
    r = requests.delete(f"{API}/expenses/{eid}", headers=admin_h)
    assert r.status_code == 200


# ---------------- ATTENDANCE upsert ----------------
def test_attendance_upsert_and_decorate(admin_h):
    # ensure a staff exists
    staff_payload = {"name": f"TEST_Staff_{uuid.uuid4().hex[:6]}", "role": "Guide",
                     "salary_type": "per_day", "salary_rate": 500, "is_active": True}
    r = requests.post(f"{API}/staff", headers=admin_h, json=staff_payload)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    today = date.today().isoformat()
    try:
        # mark present
        r = requests.post(f"{API}/attendance", headers=admin_h,
                          json={"staff_id": sid, "date": today, "status": "present"})
        assert r.status_code == 200
        first_id = r.json()["id"]
        # upsert: same staff+date, different status
        r = requests.post(f"{API}/attendance", headers=admin_h,
                          json={"staff_id": sid, "date": today, "status": "absent"})
        assert r.status_code == 200
        assert r.json()["id"] == first_id, "attendance should upsert, not create new"
        assert r.json()["status"] == "absent"
        # list w/ filters + decoration
        r = requests.get(f"{API}/attendance", headers=admin_h, params={"date": today, "staff_id": sid})
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["staff_name"] == staff_payload["name"]
        assert rows[0]["staff_role"] == "Guide"
        # cleanup attendance
        requests.delete(f"{API}/attendance/{first_id}", headers=admin_h)
    finally:
        requests.delete(f"{API}/staff/{sid}", headers=admin_h)


# ---------------- TRANSPORTERS + VEHICLES ----------------
def test_transporters_and_vehicles_decorated(admin_h):
    tp = {"name": f"TEST_Transporter_{uuid.uuid4().hex[:5]}", "phone": "9999"}
    r = requests.post(f"{API}/transporters", headers=admin_h, json=tp)
    assert r.status_code == 200
    tid = r.json()["id"]
    try:
        v = {"vehicle_no": f"TEST-{uuid.uuid4().hex[:5]}", "vehicle_type": "SUV",
             "driver_name": "Ram", "transporter_id": tid, "seat_capacity": 6}
        r = requests.post(f"{API}/vehicles", headers=admin_h, json=v)
        assert r.status_code == 200
        vid = r.json()["id"]
        # GET decorates
        r = requests.get(f"{API}/vehicles", headers=admin_h)
        assert r.status_code == 200
        row = next((x for x in r.json() if x["id"] == vid), None)
        assert row is not None
        assert row.get("transporter_name") == tp["name"]
        requests.delete(f"{API}/vehicles/{vid}", headers=admin_h)
    finally:
        requests.delete(f"{API}/transporters/{tid}", headers=admin_h)


# ---------------- TREK SCHEDULES decorated ----------------
def test_trek_schedules_decorated(admin_h):
    # create staff (guide) + vehicle
    st = requests.post(f"{API}/staff", headers=admin_h,
                       json={"name": f"TEST_Guide_{uuid.uuid4().hex[:5]}", "role": "Guide",
                             "salary_type": "per_month", "salary_rate": 0, "is_active": True})
    sid = st.json()["id"]
    vh = requests.post(f"{API}/vehicles", headers=admin_h,
                       json={"vehicle_no": f"SCH-{uuid.uuid4().hex[:5]}", "vehicle_type": "Tempo"})
    vid = vh.json()["id"]
    try:
        payload = {"trek_name": "TEST_Trek", "trek_date": date.today().isoformat(),
                   "total_groups": 1, "total_pax": 5,
                   "assigned_guide_id": sid, "assigned_vehicle_id": vid,
                   "status": "scheduled"}
        r = requests.post(f"{API}/trek-schedules", headers=admin_h, json=payload)
        assert r.status_code == 200
        schid = r.json()["id"]
        r = requests.get(f"{API}/trek-schedules", headers=admin_h)
        row = next((x for x in r.json() if x["id"] == schid), None)
        assert row and row.get("guide_name") and row.get("vehicle_no")
        requests.delete(f"{API}/trek-schedules/{schid}", headers=admin_h)
    finally:
        requests.delete(f"{API}/vehicles/{vid}", headers=admin_h)
        requests.delete(f"{API}/staff/{sid}", headers=admin_h)


# ---------------- SETTINGS singleton + RBAC ----------------
def test_settings_singleton_and_rbac(admin_h, staff_h):
    r = requests.get(f"{API}/settings", headers=admin_h)
    assert r.status_code == 200
    doc = r.json()
    assert doc.get("id") == "singleton"
    assert "company_name" in doc

    # staff cannot save
    r = requests.put(f"{API}/settings", headers=staff_h, json=doc)
    assert r.status_code == 403
    # manager also blocked
    mgr = _login(*MANAGER)
    r = requests.put(f"{API}/settings", headers={"Authorization": f"Bearer {mgr}"}, json=doc)
    assert r.status_code == 403
    # admin can
    doc["tagline"] = "TEST_tag"
    r = requests.put(f"{API}/settings", headers=admin_h, json=doc)
    assert r.status_code == 200
    assert r.json()["tagline"] == "TEST_tag"


# ---------------- BACKUP export/restore ----------------
def test_backup_export_admin_only(admin_h, staff_h):
    r = requests.get(f"{API}/backup/export", headers=staff_h)
    assert r.status_code == 403
    mgr = _login(*MANAGER)
    r = requests.get(f"{API}/backup/export", headers={"Authorization": f"Bearer {mgr}"})
    assert r.status_code == 403
    r = requests.get(f"{API}/backup/export", headers=admin_h)
    assert r.status_code == 200
    data = r.json()
    assert "collections" in data
    expected = {"clients", "gear", "rentals", "transport", "staff", "salaries",
                "payments", "treks", "expenses", "attendance", "transporters",
                "vehicles", "trek_schedules", "settings"}
    assert expected.issubset(set(data["collections"].keys()))


def test_backup_restore_admin_only(admin_h, staff_h):
    r = requests.post(f"{API}/backup/restore", headers=staff_h,
                      json={"payload": {"collections": {}}, "wipe_first": False})
    assert r.status_code == 403
    # empty restore ok
    r = requests.post(f"{API}/backup/restore", headers=admin_h,
                      json={"payload": {"collections": {}}, "wipe_first": False})
    assert r.status_code == 200
    assert r.json().get("ok") is True
    assert "restored" in r.json()


# ---------------- REPORTS ----------------
def test_reports_summary(admin_h):
    start = (date.today() - timedelta(days=30)).isoformat()
    end = (date.today() + timedelta(days=1)).isoformat()
    r = requests.get(f"{API}/reports/summary", headers=admin_h, params={"start": start, "end": end})
    assert r.status_code == 200, r.text
    d = r.json()
    for k in ("bookings", "rentals", "transport", "expenses", "salaries", "totals"):
        assert k in d
    t = d["totals"]
    assert round(t["income"] - t["expenses"], 2) == round(t["net_profit"], 2)


# ---------------- CLIENT new fields ----------------
def test_client_new_fields(admin_h):
    payload = {
        "name": f"TEST_Cli_{uuid.uuid4().hex[:5]}",
        "phone": "1234", "trek_name": "T",
        "start_date": date.today().isoformat(), "end_date": date.today().isoformat(),
        "people_count": 2, "trek_amount": 1000, "paid_amount": 0,
        "payment_mode": "pending", "company_name": "TEST_Co",
        "booking_status": "confirmed"
    }
    r = requests.post(f"{API}/clients", headers=admin_h, json=payload)
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    try:
        r = requests.get(f"{API}/clients", headers=admin_h)
        row = next((x for x in r.json() if x["id"] == cid), None)
        assert row["company_name"] == "TEST_Co"
        assert row["booking_status"] == "confirmed"
    finally:
        requests.delete(f"{API}/clients/{cid}", headers=admin_h)


# ---------------- TRANSPORT rounds math ----------------
def test_transport_rounds_math(admin_h):
    payload = {
        "vehicle_no": f"TT-{uuid.uuid4().hex[:5]}",
        "vehicle_type": "SUV", "driver_name": "D",
        "transporter_name": "TP", "pickup": "A", "drop": "B",
        "pax": 4, "rounds": 3, "rate_per_round": 1000,
        "start_date": date.today().isoformat(),
        "end_date": (date.today() + timedelta(days=1)).isoformat(),
        "price_per_day": 500, "paid_amount": 0, "status": "scheduled"
    }
    r = requests.post(f"{API}/transport", headers=admin_h, json=payload)
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    try:
        # rounds * rate wins over days * price
        assert r.json()["total_amount"] == 3000
        # fallback path
        payload2 = {**payload, "vehicle_no": f"TT2-{uuid.uuid4().hex[:5]}",
                    "rounds": 0, "rate_per_round": 0, "price_per_day": 500}
        r2 = requests.post(f"{API}/transport", headers=admin_h, json=payload2)
        assert r2.status_code == 200
        # 2 days * 500 = 1000
        assert r2.json()["total_amount"] == 1000
        requests.delete(f"{API}/transport/{r2.json()['id']}", headers=admin_h)
    finally:
        requests.delete(f"{API}/transport/{tid}", headers=admin_h)

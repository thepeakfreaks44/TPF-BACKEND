"""Iteration 5 backend tests — client 360 summary, guides list/duty, transport reports,
dashboard today-bookings."""
import os
import uuid
import pytest
import requests
from datetime import date

BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL")
            or open("/app/frontend/.env").read().split("REACT_APP_BACKEND_URL=")[1].splitlines()[0]).rstrip("/")
API = f"{BASE_URL}/api"

ADMIN = ("admin@peakfreaks.com", "Peak@2026")
STAFF = ("staff@peakfreaks.com", "Staff@2026")


def _login(email, pw):
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": pw}, timeout=15)
    assert r.status_code == 200, f"login {email} failed: {r.status_code} {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="module")
def admin_h():
    return {"Authorization": f"Bearer {_login(*ADMIN)}"}


@pytest.fixture(scope="module")
def staff_h():
    return {"Authorization": f"Bearer {_login(*STAFF)}"}


# ---------- AUTH REQUIRED ----------
@pytest.mark.parametrize("path", [
    "/clients/anything/summary",
    "/guides",
    "/guides/anything/duty",
    "/transport/reports",
    "/dashboard/today-bookings",
])
def test_new_endpoints_require_auth(path):
    r = requests.get(f"{API}{path}", timeout=15)
    assert r.status_code == 401, f"{path} expected 401 got {r.status_code}"


# ---------- STAFF ROLE CAN READ ALL NEW ENDPOINTS ----------
def test_staff_can_read_guides(staff_h):
    r = requests.get(f"{API}/guides", headers=staff_h, timeout=15)
    assert r.status_code == 200


def test_staff_can_read_transport_reports(staff_h):
    r = requests.get(f"{API}/transport/reports", headers=staff_h, timeout=15)
    assert r.status_code == 200


def test_staff_can_read_today_bookings(staff_h):
    r = requests.get(f"{API}/dashboard/today-bookings", headers=staff_h, timeout=15)
    assert r.status_code == 200


# ---------- TRANSPORT REPORTS SHAPE ----------
def test_transport_reports_shape(admin_h):
    r = requests.get(f"{API}/transport/reports", headers=admin_h, timeout=20)
    assert r.status_code == 200
    d = r.json()
    for k in ("totals", "by_vehicle", "by_route", "by_driver", "by_transporter"):
        assert k in d
    for k in ("trips", "rounds", "pax", "total", "paid", "pending"):
        assert k in d["totals"]
    for grp in ("by_vehicle", "by_route", "by_driver", "by_transporter"):
        assert isinstance(d[grp], list)
        for row in d[grp]:
            for f in ("key", "trips", "rounds", "pax", "total", "paid", "pending"):
                assert f in row, f"missing {f} in {grp} row"


# ---------- TODAY BOOKINGS SHAPE ----------
def test_today_bookings_shape(admin_h):
    r = requests.get(f"{API}/dashboard/today-bookings", headers=admin_h, timeout=20)
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    for row in rows:
        for f in ("id", "name", "company_name", "phone", "trek_name", "pax",
                  "amount", "cash", "online", "paid", "balance", "status"):
            assert f in row, f"missing {f} in today-bookings row"


# ---------- GUIDES LIST DECORATION ----------
def test_guides_list_shape(admin_h):
    r = requests.get(f"{API}/guides", headers=admin_h, timeout=20)
    assert r.status_code == 200
    guides = r.json()
    assert isinstance(guides, list)
    for g in guides:
        assert (g.get("role") or "").lower() == "guide", "list must only contain Guide role"
        for f in ("total_treks", "active_treks", "completed_treks", "total_clients", "current_duty"):
            assert f in g, f"guide missing {f}"


# ---------- CREATE GUIDE + SCHEDULE, verify duty & totals ----------
@pytest.fixture(scope="module")
def created_guide(admin_h):
    payload = {
        "name": f"TEST_Guide_{uuid.uuid4().hex[:6]}",
        "phone": "9990000001",
        "role": "Guide",
        "salary_type": "per_month",
        "monthly_salary": 20000,
        "is_active": True,
    }
    r = requests.post(f"{API}/staff", headers=admin_h, json=payload, timeout=15)
    assert r.status_code in (200, 201), r.text
    gid = r.json()["id"]
    yield gid
    # cleanup
    requests.delete(f"{API}/staff/{gid}", headers=admin_h, timeout=15)


def test_guides_duty_with_schedule(admin_h, created_guide):
    gid = created_guide
    # create a scheduled trek assigned to this guide
    today = date.today().isoformat()
    sch_payload = {
        "trek_name": "TEST_Trek_I5",
        "trek_date": today,
        "assigned_guide_id": gid,
        "total_pax": 7,
        "status": "scheduled",
    }
    r = requests.post(f"{API}/trek-schedules", headers=admin_h, json=sch_payload, timeout=15)
    assert r.status_code in (200, 201), r.text
    sid = r.json()["id"]
    try:
        # /guides should show this guide with total_treks>=1 and total_clients>=7
        gl = requests.get(f"{API}/guides", headers=admin_h, timeout=15).json()
        me = next((g for g in gl if g["id"] == gid), None)
        assert me is not None, "created guide not in /guides list"
        assert me["total_treks"] >= 1
        assert me["total_clients"] >= 7
        assert me["active_treks"] >= 1
        assert me["current_duty"] is not None
        assert me["current_duty"]["id"] == sid

        # /guides/{gid}/duty
        r2 = requests.get(f"{API}/guides/{gid}/duty", headers=admin_h, timeout=15)
        assert r2.status_code == 200
        d = r2.json()
        assert d["guide"]["id"] == gid
        assert any(s["id"] == sid for s in d["schedules"])
        assert isinstance(d["salary_records"], list)
    finally:
        requests.delete(f"{API}/trek-schedules/{sid}", headers=admin_h, timeout=15)


def test_guides_duty_404(admin_h):
    r = requests.get(f"{API}/guides/nonexistent_id_xyz/duty", headers=admin_h, timeout=15)
    assert r.status_code == 404


# ---------- CLIENT 360 SUMMARY ----------
@pytest.fixture(scope="module")
def created_client(admin_h):
    payload = {
        "name": f"TEST_Client_I5_{uuid.uuid4().hex[:6]}",
        "phone": "9998887777",
        "trek_name": "Everest BC",
        "trek_amount": 50000,
        "paid_amount": 10000,
        "payment_mode": "cash",
        "people_count": 3,
        "start_date": date.today().isoformat(),
        "end_date": date.today().isoformat(),
        "company_name": "TEST_Co",
        "booking_status": "confirmed",
    }
    r = requests.post(f"{API}/clients", headers=admin_h, json=payload, timeout=15)
    assert r.status_code in (200, 201), r.text
    cid = r.json()["id"]
    yield cid, payload
    requests.delete(f"{API}/clients/{cid}", headers=admin_h, timeout=15)


def test_client_summary_shape_and_totals(admin_h, created_client):
    cid, payload = created_client
    r = requests.get(f"{API}/clients/{cid}/summary", headers=admin_h, timeout=15)
    assert r.status_code == 200
    d = r.json()
    for k in ("client", "payments", "rentals", "transports", "totals"):
        assert k in d
    for k in ("booking_amount", "booking_paid", "rental_total", "rental_paid",
              "transport_total", "transport_paid", "grand_total", "grand_paid", "grand_balance"):
        assert k in d["totals"], f"missing totals.{k}"
    assert d["client"]["id"] == cid
    assert d["totals"]["booking_amount"] == 50000
    # grand math
    t = d["totals"]
    assert round(t["grand_total"] - (t["booking_amount"] + t["rental_total"] + t["transport_total"]), 2) == 0
    assert round(t["grand_paid"] - (t["booking_paid"] + t["rental_paid"] + t["transport_paid"]), 2) == 0
    assert round(t["grand_balance"] - (t["grand_total"] - t["grand_paid"]), 2) == 0


def test_client_summary_404(admin_h):
    r = requests.get(f"{API}/clients/no_such_client/summary", headers=admin_h, timeout=15)
    assert r.status_code == 404


def test_client_summary_matches_rental_by_name(admin_h, created_client):
    """Create a rental with matching customer_name and ensure it appears in the client summary."""
    cid, payload = created_client
    rental_payload = {
        "customer_name": payload["name"],  # exact same name
        "customer_phone": payload["phone"],
        "item_name": "TEST_Item",
        "quantity": 1,
        "rate": 500,
        "days": 2,
        "total_amount": 1000,
        "paid_amount": 400,
        "start_date": date.today().isoformat(),
    }
    r = requests.post(f"{API}/rentals", headers=admin_h, json=rental_payload, timeout=15)
    if r.status_code not in (200, 201):
        pytest.skip(f"rental create not supported with this schema: {r.text}")
    rid = r.json()["id"]
    try:
        s = requests.get(f"{API}/clients/{cid}/summary", headers=admin_h, timeout=15).json()
        assert any(rr["id"] == rid for rr in s["rentals"]), "rental not matched to client by name"
        assert s["totals"]["rental_total"] >= 1000
    finally:
        requests.delete(f"{API}/rentals/{rid}", headers=admin_h, timeout=15)


def test_today_bookings_includes_created_today(admin_h, created_client):
    cid, payload = created_client
    r = requests.get(f"{API}/dashboard/today-bookings", headers=admin_h, timeout=15).json()
    row = next((x for x in r if x["id"] == cid), None)
    assert row is not None, "newly-created client should appear in today-bookings"
    # cash mode: initial 10000 must go into cash bucket
    assert row["cash"] >= 10000
    assert row["online"] == 0
    assert row["company_name"] == "TEST_Co"

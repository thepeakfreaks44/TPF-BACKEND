"""Iteration 3 tests: payments ledger + trek catalog + branded PDF."""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://peak-freaks-portal.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"

CREDS = {
    "admin":   ("admin@peakfreaks.com",   "Peak@2026"),
    "manager": ("manager@peakfreaks.com", "Manager@2026"),
    "staff":   ("staff@peakfreaks.com",   "Staff@2026"),
}


def _login(role):
    email, pw = CREDS[role]
    r = requests.post(f"{API}/auth/login", json={"email": email, "password": pw}, timeout=15)
    assert r.status_code == 200
    return r.json()["token"]


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture(scope="module")
def tokens():
    return {role: _login(role) for role in CREDS}


@pytest.fixture(scope="module")
def seed(tokens):
    admin = tokens["admin"]
    c = requests.post(f"{API}/clients", json={
        "name": "TEST_Ledger Client", "phone": "+911111",
        "trek_name": "TEST Trek", "start_date": "2026-03-01", "end_date": "2026-03-05",
        "people_count": 1, "trek_amount": 15000, "paid_amount": 5000, "payment_mode": "cash"
    }, headers=_h(admin), timeout=15).json()

    g = requests.post(f"{API}/gear", json={
        "name": "TEST_LedgerTent", "total_qty": 1, "available_qty": 1, "rent_per_day": 100
    }, headers=_h(admin), timeout=15).json()

    rn = requests.post(f"{API}/rentals", json={
        "customer_name": "TEST_LR", "gear_id": g["id"], "qty": 1,
        "rent_date": "2026-03-01", "return_date": "2026-03-02",
        "daily_rate": 100, "paid_amount": 50, "payment_mode": "cash"
    }, headers=_h(admin), timeout=15).json()

    tr = requests.post(f"{API}/transport", json={
        "vehicle_no": "TEST-LDG-01", "driver_name": "D", "route": "A-B",
        "start_date": "2026-03-01", "end_date": "2026-03-02", "price_per_day": 1000,
        "paid_amount": 200, "payment_mode": "cash", "status": "scheduled"
    }, headers=_h(admin), timeout=15).json()

    ids = {"client": c["id"], "gear": g["id"], "rental": rn["id"], "transport": tr["id"]}
    yield ids

    for path, _id in [("clients", ids["client"]), ("rentals", ids["rental"]),
                      ("transport", ids["transport"]), ("gear", ids["gear"])]:
        requests.delete(f"{API}/{path}/{_id}", headers=_h(admin))


# ---------- PAYMENTS RBAC ----------
def test_staff_cannot_create_payment(tokens, seed):
    r = requests.post(f"{API}/payments", json={
        "entity": "client", "entity_id": seed["client"], "amount": 100,
        "mode": "cash", "date": "2026-03-02"
    }, headers=_h(tokens["staff"]), timeout=15)
    assert r.status_code == 403


def test_manager_can_create_payment(tokens, seed):
    r = requests.post(f"{API}/payments", json={
        "entity": "client", "entity_id": seed["client"], "amount": 1,
        "mode": "cash", "date": "2026-03-02"
    }, headers=_h(tokens["manager"]), timeout=15)
    assert r.status_code == 200
    pid = r.json()["id"]
    # cleanup
    d = requests.delete(f"{API}/payments/{pid}", headers=_h(tokens["admin"]))
    assert d.status_code == 200


def test_staff_cannot_delete_payment(tokens, seed):
    # admin creates
    p = requests.post(f"{API}/payments", json={
        "entity": "client", "entity_id": seed["client"], "amount": 1,
        "mode": "cash", "date": "2026-03-02"
    }, headers=_h(tokens["admin"]), timeout=15).json()
    r = requests.delete(f"{API}/payments/{p['id']}", headers=_h(tokens["staff"]), timeout=15)
    assert r.status_code == 403
    requests.delete(f"{API}/payments/{p['id']}", headers=_h(tokens["admin"]))


def test_payment_invalid_parent(tokens):
    r = requests.post(f"{API}/payments", json={
        "entity": "client", "entity_id": "does-not-exist", "amount": 10,
        "mode": "cash", "date": "2026-03-02"
    }, headers=_h(tokens["admin"]), timeout=15)
    assert r.status_code == 400


# ---------- LEDGER MATH: client ----------
def test_client_ledger_math(tokens, seed):
    admin = tokens["admin"]
    cid = seed["client"]
    # add two payments 2000 + 1000
    for amt in (2000, 1000):
        r = requests.post(f"{API}/payments", json={
            "entity": "client", "entity_id": cid, "amount": amt,
            "mode": "cash", "date": "2026-03-03"
        }, headers=_h(admin), timeout=15)
        assert r.status_code == 200

    # list payments
    lst = requests.get(f"{API}/payments", params={"entity": "client", "entity_id": cid},
                       headers=_h(admin), timeout=15).json()
    assert len(lst) >= 2

    # get client
    clients = requests.get(f"{API}/clients", headers=_h(admin), timeout=15).json()
    me = next(c for c in clients if c["id"] == cid)
    assert me["initial_paid"] == 5000
    assert me["ledger_paid"] == 3000
    assert me["paid_amount"] == 8000
    assert me["balance"] == 7000

    # cleanup payments
    for p in lst:
        requests.delete(f"{API}/payments/{p['id']}", headers=_h(admin))


def test_rental_ledger_math(tokens, seed):
    admin = tokens["admin"]
    rid = seed["rental"]
    r = requests.post(f"{API}/payments", json={
        "entity": "rental", "entity_id": rid, "amount": 30,
        "mode": "cash", "date": "2026-03-03"
    }, headers=_h(admin), timeout=15)
    assert r.status_code == 200
    pid = r.json()["id"]

    rentals = requests.get(f"{API}/rentals", headers=_h(admin), timeout=15).json()
    me = next(x for x in rentals if x["id"] == rid)
    # daily_rate 100 * qty 1 * days 2 = 200 total
    assert me["total_amount"] == 200
    assert me["initial_paid"] == 50
    assert me["ledger_paid"] == 30
    assert me["paid_amount"] == 80
    assert me["balance"] == 120

    requests.delete(f"{API}/payments/{pid}", headers=_h(admin))


def test_transport_ledger_math(tokens, seed):
    admin = tokens["admin"]
    tid = seed["transport"]
    r = requests.post(f"{API}/payments", json={
        "entity": "transport", "entity_id": tid, "amount": 500,
        "mode": "upi", "date": "2026-03-03"
    }, headers=_h(admin), timeout=15).json()

    trans = requests.get(f"{API}/transport", headers=_h(admin), timeout=15).json()
    me = next(x for x in trans if x["id"] == tid)
    # 1000/day * 2 days = 2000
    assert me["total_amount"] == 2000
    assert me["initial_paid"] == 200
    assert me["ledger_paid"] == 500
    assert me["paid_amount"] == 700
    assert me["balance"] == 1300

    requests.delete(f"{API}/payments/{r['id']}", headers=_h(admin))


# ---------- CASCADE DELETE ----------
def test_delete_cascade_payments(tokens):
    admin = tokens["admin"]
    # create a fresh client
    c = requests.post(f"{API}/clients", json={
        "name": "TEST_Cascade", "phone": "1", "trek_name": "T",
        "start_date": "2026-04-01", "end_date": "2026-04-02", "people_count": 1,
        "trek_amount": 1000, "paid_amount": 0, "payment_mode": "pending"
    }, headers=_h(admin), timeout=15).json()
    cid = c["id"]
    p = requests.post(f"{API}/payments", json={
        "entity": "client", "entity_id": cid, "amount": 100,
        "mode": "cash", "date": "2026-04-01"
    }, headers=_h(admin), timeout=15).json()

    before = requests.get(f"{API}/payments", params={"entity": "client", "entity_id": cid},
                          headers=_h(admin)).json()
    assert len(before) == 1

    d = requests.delete(f"{API}/clients/{cid}", headers=_h(admin))
    assert d.status_code == 200

    after = requests.get(f"{API}/payments", params={"entity": "client", "entity_id": cid},
                         headers=_h(admin)).json()
    assert after == []


# ---------- TREKS CRUD ----------
def test_treks_crud_and_rbac(tokens):
    admin = tokens["admin"]
    staff = tokens["staff"]

    # staff cannot POST
    body = {"name": "TEST_TrekX", "region": "Uttarakhand", "duration_days": 5,
            "difficulty": "Moderate", "price_per_person": 8000, "is_active": True}
    r = requests.post(f"{API}/treks", json=body, headers=_h(staff))
    assert r.status_code == 403

    # admin creates
    r = requests.post(f"{API}/treks", json=body, headers=_h(admin))
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    assert r.json()["price_per_person"] == 8000

    # staff can GET
    r = requests.get(f"{API}/treks", headers=_h(staff))
    assert r.status_code == 200
    assert any(t["id"] == tid for t in r.json())

    # staff cannot PUT
    body["price_per_person"] = 9000
    r = requests.put(f"{API}/treks/{tid}", json=body, headers=_h(staff))
    assert r.status_code == 403

    # admin updates
    r = requests.put(f"{API}/treks/{tid}", json=body, headers=_h(admin))
    assert r.status_code == 200
    assert r.json()["price_per_person"] == 9000

    # staff cannot DELETE
    r = requests.delete(f"{API}/treks/{tid}", headers=_h(staff))
    assert r.status_code == 403

    # admin deletes
    r = requests.delete(f"{API}/treks/{tid}", headers=_h(admin))
    assert r.status_code == 200


# ---------- INVOICE reflects ledger ----------
def test_invoice_shows_ledger_sum(tokens):
    admin = tokens["admin"]
    c = requests.post(f"{API}/clients", json={
        "name": "TEST_InvLedger", "phone": "1", "trek_name": "T",
        "start_date": "2026-05-01", "end_date": "2026-05-02", "people_count": 1,
        "trek_amount": 15000, "paid_amount": 5000, "payment_mode": "cash"
    }, headers=_h(admin), timeout=15).json()
    cid = c["id"]
    requests.post(f"{API}/payments", json={
        "entity": "client", "entity_id": cid, "amount": 3000,
        "mode": "cash", "date": "2026-05-01"
    }, headers=_h(admin))

    inv = requests.get(f"{API}/invoice/client/{cid}", headers=_h(admin)).json()
    assert inv["totals"]["paid"] == 8000
    assert inv["totals"]["balance"] == 7000
    # text formatting Rs.8,000
    assert "8,000" in inv["text"]
    assert "7,000" in inv["text"]

    requests.delete(f"{API}/clients/{cid}", headers=_h(admin))


def test_pdf_branded(tokens, seed):
    admin = tokens["admin"]
    r = requests.get(f"{API}/invoice/client/{seed['client']}/pdf", headers=_h(admin), timeout=20)
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"
    assert r.headers.get("content-type", "").startswith("application/pdf")
    assert len(r.content) > 2048

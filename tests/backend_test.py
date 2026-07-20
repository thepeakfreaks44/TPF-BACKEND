"""Backend tests for Peak Freaks - iteration 2: RBAC (staff read-only) + Invoice endpoints."""
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
    assert r.status_code == 200, f"login failed for {role}: {r.status_code} {r.text}"
    return r.json()["token"]


@pytest.fixture(scope="session")
def tokens():
    return {role: _login(role) for role in CREDS}


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


# ---------- SEED helpers (admin creates test data) ----------
@pytest.fixture(scope="session")
def seed(tokens):
    admin = tokens["admin"]
    # Client
    c = requests.post(f"{API}/clients", json={
        "name": "TEST_Invoice Client", "phone": "+919999111222", "email": "t@t.com",
        "trek_name": "Kedarkantha", "start_date": "2026-02-01", "end_date": "2026-02-05",
        "people_count": 2, "trek_amount": 20000, "paid_amount": 5000, "payment_mode": "upi",
        "notes": "Test note"
    }, headers=_h(admin), timeout=15)
    assert c.status_code == 200, c.text
    client_id = c.json()["id"]

    # Gear (needed for rental)
    g = requests.post(f"{API}/gear", json={
        "name": "TEST_Tent", "category": "Shelter", "total_qty": 5, "available_qty": 5,
        "rent_per_day": 200, "deposit": 500
    }, headers=_h(admin), timeout=15)
    assert g.status_code == 200, g.text
    gear_id = g.json()["id"]

    # Rental
    rn = requests.post(f"{API}/rentals", json={
        "customer_name": "TEST_Renter", "customer_phone": "+919888777666",
        "gear_id": gear_id, "qty": 2, "rent_date": "2026-02-01", "return_date": "2026-02-03",
        "daily_rate": 200, "paid_amount": 100, "payment_mode": "cash"
    }, headers=_h(admin), timeout=15)
    assert rn.status_code == 200, rn.text
    rental_id = rn.json()["id"]

    # Transport
    tr = requests.post(f"{API}/transport", json={
        "vehicle_no": "TEST-UK07-1234", "vehicle_type": "SUV", "driver_name": "TEST_Driver",
        "driver_phone": "+919777666555", "route": "Rishikesh - Sankri",
        "start_date": "2026-02-01", "end_date": "2026-02-02",
        "price_per_day": 3000, "paid_amount": 1000, "payment_mode": "cash", "status": "scheduled"
    }, headers=_h(admin), timeout=15)
    assert tr.status_code == 200, tr.text
    transport_id = tr.json()["id"]

    ids = {"client": client_id, "gear": gear_id, "rental": rental_id, "transport": transport_id}
    yield ids

    # cleanup
    requests.delete(f"{API}/clients/{client_id}", headers=_h(admin))
    requests.delete(f"{API}/rentals/{rental_id}", headers=_h(admin))
    requests.delete(f"{API}/transport/{transport_id}", headers=_h(admin))
    requests.delete(f"{API}/gear/{gear_id}", headers=_h(admin))


# ---------- RBAC: Staff GET allowed ----------
@pytest.mark.parametrize("path", ["/clients", "/gear", "/rentals", "/transport", "/staff", "/salaries"])
def test_staff_can_get(tokens, path):
    r = requests.get(f"{API}{path}", headers=_h(tokens["staff"]), timeout=15)
    assert r.status_code == 200, f"staff GET {path} => {r.status_code}"


# ---------- RBAC: Staff cannot POST/PUT (403) ----------
STAFF_WRITE_CASES = [
    ("POST", "/clients", {"name": "x", "phone": "1", "trek_name": "t", "start_date": "2026-01-01", "end_date": "2026-01-02"}),
    ("POST", "/gear",    {"name": "TEST_x"}),
    ("POST", "/transport", {"vehicle_no": "X", "driver_name": "D", "route": "A-B", "start_date": "2026-01-01", "end_date": "2026-01-02"}),
    ("POST", "/staff",   {"name": "TEST_x"}),
    ("POST", "/salaries",{"staff_id": "nonexistent", "month": "2026-01"}),
]

@pytest.mark.parametrize("method,path,body", STAFF_WRITE_CASES)
def test_staff_write_forbidden(tokens, method, path, body):
    r = requests.request(method, f"{API}{path}", json=body, headers=_h(tokens["staff"]), timeout=15)
    assert r.status_code == 403, f"expected 403 staff {method} {path}, got {r.status_code} {r.text[:200]}"


def test_staff_rental_post_forbidden(tokens, seed):
    body = {
        "customer_name": "x", "gear_id": seed["gear"], "qty": 1,
        "rent_date": "2026-02-01", "return_date": "2026-02-02", "daily_rate": 100
    }
    r = requests.post(f"{API}/rentals", json=body, headers=_h(tokens["staff"]), timeout=15)
    assert r.status_code == 403, f"expected 403 staff POST /rentals, got {r.status_code}"


def test_staff_rental_put_forbidden(tokens, seed):
    # Update existing rental as staff should be forbidden
    body = {
        "customer_name": "TEST_Renter", "customer_phone": "+919888777666",
        "gear_id": seed["gear"], "qty": 2, "rent_date": "2026-02-01", "return_date": "2026-02-03",
        "daily_rate": 200, "paid_amount": 100, "payment_mode": "cash"
    }
    r = requests.put(f"{API}/rentals/{seed['rental']}", json=body, headers=_h(tokens["staff"]), timeout=15)
    assert r.status_code == 403, f"expected 403 staff PUT /rentals/{{id}}, got {r.status_code} - BUG: update_rental uses get_current_user instead of can_write"


def test_staff_client_put_forbidden(tokens, seed):
    body = {
        "name": "hacked", "phone": "1", "trek_name": "t",
        "start_date": "2026-01-01", "end_date": "2026-01-02", "people_count": 1,
        "trek_amount": 0, "paid_amount": 0, "payment_mode": "pending",
    }
    r = requests.put(f"{API}/clients/{seed['client']}", json=body, headers=_h(tokens["staff"]), timeout=15)
    assert r.status_code == 403


def test_staff_delete_forbidden(tokens, seed):
    r = requests.delete(f"{API}/clients/{seed['client']}", headers=_h(tokens["staff"]), timeout=15)
    assert r.status_code == 403


# ---------- Admin & Manager can write ----------
@pytest.mark.parametrize("role", ["admin", "manager"])
def test_admin_manager_can_write_client(tokens, role):
    body = {"name": f"TEST_{role}_client", "phone": "1", "trek_name": "T",
            "start_date": "2026-01-01", "end_date": "2026-01-02", "people_count": 1,
            "trek_amount": 100, "paid_amount": 0, "payment_mode": "pending"}
    r = requests.post(f"{API}/clients", json=body, headers=_h(tokens[role]), timeout=15)
    assert r.status_code == 200, f"{role} POST failed {r.status_code} {r.text}"
    cid = r.json()["id"]

    # update
    body["paid_amount"] = 50
    r2 = requests.put(f"{API}/clients/{cid}", json=body, headers=_h(tokens[role]), timeout=15)
    assert r2.status_code == 200
    assert r2.json()["balance"] == 50

    # cleanup
    requests.delete(f"{API}/clients/{cid}", headers=_h(tokens["admin"]))


# ---------- Invoice endpoints ----------
def test_invoice_requires_auth():
    r = requests.get(f"{API}/invoice/client/anything", timeout=15)
    assert r.status_code == 401


def test_invoice_client(tokens, seed):
    r = requests.get(f"{API}/invoice/client/{seed['client']}", headers=_h(tokens["admin"]), timeout=15)
    assert r.status_code == 200
    d = r.json()
    for k in ("subject", "text", "phone", "totals", "filename"):
        assert k in d, f"missing {k}"
    assert "THE PEAK FREAKS" in d["text"]
    assert "Kedarkantha" in d["text"]
    assert "TEST_Invoice Client" in d["text"]
    assert d["phone"] == "+919999111222"
    assert d["totals"]["balance"] == 15000
    # dates
    assert "2026-02-01" in d["text"] and "2026-02-05" in d["text"]


def test_invoice_rental(tokens, seed):
    r = requests.get(f"{API}/invoice/rental/{seed['rental']}", headers=_h(tokens["admin"]), timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert d["phone"] == "+919888777666"
    assert "TEST_Tent" in d["text"]  # gear_name in text
    assert "TEST_Renter" in d["text"]
    assert d["totals"]["total"] == 200 * 2 * 3  # rate*qty*days(3)


def test_invoice_transport(tokens, seed):
    r = requests.get(f"{API}/invoice/transport/{seed['transport']}", headers=_h(tokens["admin"]), timeout=15)
    assert r.status_code == 200
    d = r.json()
    assert "TEST-UK07-1234" in d["text"]
    assert "Rishikesh - Sankri" in d["text"]
    assert d["totals"]["total"] == 3000 * 2


def test_invoice_staff_can_view(tokens, seed):
    """Staff (read-only) should still view invoices."""
    r = requests.get(f"{API}/invoice/client/{seed['client']}", headers=_h(tokens["staff"]), timeout=15)
    assert r.status_code == 200


@pytest.mark.parametrize("entity_key,entity", [("client", "client"), ("rental", "rental"), ("transport", "transport")])
def test_invoice_pdf(tokens, seed, entity_key, entity):
    item_id = seed[entity_key]
    r = requests.get(f"{API}/invoice/{entity}/{item_id}/pdf", headers=_h(tokens["admin"]), timeout=20)
    assert r.status_code == 200, f"{entity} pdf {r.status_code}"
    assert r.headers.get("content-type", "").startswith("application/pdf")
    assert r.content[:4] == b"%PDF"


def test_invoice_not_found(tokens):
    r = requests.get(f"{API}/invoice/client/does-not-exist", headers=_h(tokens["admin"]), timeout=15)
    assert r.status_code == 404


def test_invoice_unknown_entity(tokens):
    r = requests.get(f"{API}/invoice/foobar/xyz", headers=_h(tokens["admin"]), timeout=15)
    assert r.status_code == 400

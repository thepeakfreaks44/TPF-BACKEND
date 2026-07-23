from dotenv import load_dotenv
from pathlib import Path

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

import os
import io
import uuid
import logging
import bcrypt
import jwt
from datetime import datetime, timezone, timedelta, date
from typing import List, Optional, Literal

from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, EmailStr, ConfigDict

# --- Reporting libs ---
from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

# --- Env / DB ---
MONGO_URL = os.environ['MONGO_URL']
DB_NAME = os.environ['DB_NAME']
JWT_SECRET = os.environ['JWT_SECRET']
JWT_ALGORITHM = "HS256"
ACCESS_TTL_MIN = 60 * 24  # 24h for ERP convenience

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

app = FastAPI(title="The Peak Freaks API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://tpf-frontend-m4p0qww6v-thepeakfreaks44s-projects.vercel.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api = APIRouter(prefix="/api")

# --- Utils ---
def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    return dt.isoformat()

def new_id() -> str:
    return str(uuid.uuid4())

def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def verify_pw(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False

def make_token(user_id: str, email: str, role: str) -> str:
    payload = {
        "sub": user_id, "email": email, "role": role,
        "exp": now_utc() + timedelta(minutes=ACCESS_TTL_MIN),
        "type": "access",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def parse_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])

async def get_current_user(request: Request) -> dict:
    token = request.cookies.get("access_token")
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(401, "Not authenticated")
    try:
        payload = parse_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")
    user = await db.users.find_one({"id": payload["sub"]})
    if not user:
        raise HTTPException(401, "User not found")
    user.pop("_id", None)
    user.pop("password_hash", None)
    return user

def require_role(*roles: str):
    async def dep(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") not in roles:
            raise HTTPException(403, "Insufficient permissions")
        return user
    return dep

# Any create/update requires admin or manager. Staff = read only.
can_write = require_role("admin", "manager")

# --- Models ---
class LoginIn(BaseModel):
    email: EmailStr
    password: str

class UserOut(BaseModel):
    id: str
    email: EmailStr
    name: str
    role: str

class UserCreateIn(BaseModel):
    email: EmailStr
    password: str
    name: str
    role: Literal["admin", "manager", "staff"] = "staff"

# Client / Trek booking
class ClientIn(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None
    company_name: Optional[str] = None
    trek_name: str
    start_date: str  # YYYY-MM-DD
    end_date: str
    people_count: int = 1
    trek_amount: float = 0.0
    paid_amount: float = 0.0
    payment_mode: Literal["cash", "online", "upi", "card", "pending"] = "pending"
    booking_status: Literal["confirmed", "pending", "cancelled", "completed"] = "confirmed"
    notes: Optional[str] = None

class GearIn(BaseModel):
    name: str
    category: str = "General"
    total_qty: int = 1
    available_qty: int = 1
    rent_per_day: float = 0.0
    deposit: float = 0.0
    notes: Optional[str] = None

class RentalIn(BaseModel):
    client_id: Optional[str] = None
    customer_name: str
    customer_phone: Optional[str] = None
    gear_id: str
    qty: int = 1
    rent_date: str  # YYYY-MM-DD
    return_date: str
    daily_rate: float
    paid_amount: float = 0.0
    payment_mode: Literal["cash", "online", "upi", "card", "pending"] = "pending"
    returned: bool = False

class TransportIn(BaseModel):
    vehicle_no: str
    vehicle_type: str = "SUV"
    driver_name: str
    driver_phone: Optional[str] = None
    transporter_name: Optional[str] = None
    transporter_phone: Optional[str] = None
    booking_id: Optional[str] = None  # link to client booking
    client_name: Optional[str] = None
    trek_name: Optional[str] = None
    pickup: Optional[str] = None
    drop: Optional[str] = None
    route: str = ""
    pax: int = 0
    rounds: int = 0
    rate_per_round: float = 0.0
    start_date: str
    end_date: str
    price_per_day: float = 0.0  # legacy — used if rounds/rate = 0
    paid_amount: float = 0.0
    payment_mode: Literal["cash", "online", "upi", "card", "pending"] = "pending"
    trek_ref: Optional[str] = None
    status: Literal["scheduled", "active", "completed", "cancelled"] = "scheduled"
    notes: Optional[str] = None

class StaffIn(BaseModel):
    name: str
    phone: Optional[str] = None
    role: str = "Guide"  # Guide, Porter, Cook, Driver, Manager
    salary_type: Literal["per_month", "per_day"] = "per_month"
    salary_rate: float = 0.0
    active_trek: Optional[str] = None
    is_active: bool = True
    joined_on: Optional[str] = None
    notes: Optional[str] = None

class SalaryIn(BaseModel):
    staff_id: str
    month: str  # YYYY-MM
    days_worked: int = 0
    bonus: float = 0.0
    deduction: float = 0.0
    paid_amount: float = 0.0
    payment_mode: Literal["cash", "online", "upi", "card", "pending"] = "pending"
    notes: Optional[str] = None

# Payment ledger — records additional part-payments against a client/rental/transport
class PaymentIn(BaseModel):
    entity: Literal["client", "rental", "transport"]
    entity_id: str
    amount: float
    mode: Literal["cash", "online", "upi", "card"] = "cash"
    date: str  # YYYY-MM-DD
    notes: Optional[str] = None

# Trek catalog — saved pricing templates
class TrekIn(BaseModel):
    name: str
    region: str = ""
    duration_days: int = 1
    difficulty: Literal["Easy", "Moderate", "Difficult", "Extreme"] = "Moderate"
    price_per_person: float = 0.0
    description: Optional[str] = None
    is_active: bool = True

# --- Startup: seed admin + defaults ---
@app.on_event("startup")
async def startup():
    await db.users.create_index("email", unique=True)
    await db.clients.create_index("created_at")
    await db.gear.create_index("name")
    await db.rentals.create_index("created_at")
    await db.transport.create_index("start_date")
    await db.staff.create_index("name")
    await db.salaries.create_index([("staff_id", 1), ("month", 1)], unique=True)
    await db.payments.create_index([("entity", 1), ("entity_id", 1)])
    await db.treks.create_index("name")

    seeds = [
        (os.environ.get("ADMIN_EMAIL", "admin@peakfreaks.com"),
         os.environ.get("ADMIN_PASSWORD", "Peak@2026"), "Admin", "admin"),
        ("manager@peakfreaks.com", "Manager@2026", "Ops Manager", "manager"),
        ("staff@peakfreaks.com", "Staff@2026", "Field Staff", "staff"),
    ]
    for email, pw, name, role in seeds:
        existing = await db.users.find_one({"email": email})
        if not existing:
            await db.users.insert_one({
                "id": new_id(), "email": email, "name": name, "role": role,
                "password_hash": hash_pw(pw), "created_at": iso(now_utc()),
            })
        elif not verify_pw(pw, existing.get("password_hash", "")):
            await db.users.update_one({"email": email}, {"$set": {"password_hash": hash_pw(pw)}})

@app.on_event("shutdown")
async def shutdown():
    client.close()

# ---------------- AUTH ----------------
@api.post("/auth/login")
async def login(inp: LoginIn, response: Response):
    email = inp.email.lower()
    user = await db.users.find_one({"email": email})
    if not user or not verify_pw(inp.password, user.get("password_hash", "")):
        raise HTTPException(401, "Invalid email or password")
    token = make_token(user["id"], user["email"], user["role"])
    response.set_cookie("access_token", token, httponly=True, secure=False,
                        samesite="lax", max_age=ACCESS_TTL_MIN*60, path="/")
    return {
        "token": token,
        "user": {"id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"]}
    }

@api.post("/auth/logout")
async def logout(response: Response, _: dict = Depends(get_current_user)):
    response.delete_cookie("access_token", path="/")
    return {"ok": True}

@api.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return {"id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"]}

@api.get("/auth/users")
async def list_users(_: dict = Depends(require_role("admin"))):
    docs = await db.users.find({}, {"_id": 0, "password_hash": 0}).to_list(500)
    return docs

@api.post("/auth/users")
async def create_user(inp: UserCreateIn, _: dict = Depends(require_role("admin"))):
    email = inp.email.lower()
    if await db.users.find_one({"email": email}):
        raise HTTPException(400, "Email already registered")
    doc = {
        "id": new_id(), "email": email, "name": inp.name, "role": inp.role,
        "password_hash": hash_pw(inp.password), "created_at": iso(now_utc()),
    }
    await db.users.insert_one(doc)
    doc.pop("_id", None); doc.pop("password_hash", None)
    return doc

@api.delete("/auth/users/{user_id}")
async def delete_user(user_id: str, current: dict = Depends(require_role("admin"))):
    if user_id == current["id"]:
        raise HTTPException(400, "You cannot delete yourself")
    res = await db.users.delete_one({"id": user_id})
    if res.deleted_count == 0:
        raise HTTPException(404, "User not found")
    return {"ok": True}

# ---------------- PAYMENT LEDGER helpers ----------------
async def _ledger_map(entity: str) -> dict:
    """entity_id -> total_amount from db.payments"""
    pipeline = [
        {"$match": {"entity": entity}},
        {"$group": {"_id": "$entity_id", "total": {"$sum": "$amount"}}},
    ]
    out = {}
    async for row in db.payments.aggregate(pipeline):
        out[row["_id"]] = float(row["total"] or 0)
    return out

async def _ledger_one(entity: str, entity_id: str) -> float:
    pipeline = [
        {"$match": {"entity": entity, "entity_id": entity_id}},
        {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
    ]
    async for row in db.payments.aggregate(pipeline):
        return float(row["total"] or 0)
    return 0.0

# ---------------- CLIENTS ----------------
def _client_totals(doc: dict, ledger: float = 0.0) -> dict:
    initial = float(doc.get("paid_amount", 0) or 0)
    doc["initial_paid"] = initial
    doc["ledger_paid"] = round(ledger, 2)
    doc["paid_amount"] = round(initial + ledger, 2)  # override with true total
    doc["balance"] = round(float(doc.get("trek_amount", 0)) - doc["paid_amount"], 2)
    return doc

@api.get("/clients")
async def list_clients(_: dict = Depends(get_current_user)):
    docs = await db.clients.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    lm = await _ledger_map("client")
    return [_client_totals(d, lm.get(d["id"], 0.0)) for d in docs]

@api.post("/clients")
async def create_client(inp: ClientIn, _: dict = Depends(can_write)):
    doc = inp.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = iso(now_utc())
    await db.clients.insert_one(doc)
    doc.pop("_id", None)
    return _client_totals(doc, 0.0)

@api.put("/clients/{cid}")
async def update_client(cid: str, inp: ClientIn, _: dict = Depends(can_write)):
    data = inp.model_dump()
    res = await db.clients.update_one({"id": cid}, {"$set": data})
    if res.matched_count == 0:
        raise HTTPException(404, "Not found")
    doc = await db.clients.find_one({"id": cid}, {"_id": 0})
    return _client_totals(doc, await _ledger_one("client", cid))

@api.delete("/clients/{cid}")
async def delete_client(cid: str, _: dict = Depends(require_role("admin", "manager"))):
    res = await db.clients.delete_one({"id": cid})
    if res.deleted_count == 0:
        raise HTTPException(404, "Not found")
    await db.payments.delete_many({"entity": "client", "entity_id": cid})
    return {"ok": True}

# ---------------- GEAR ----------------
@api.get("/gear")
async def list_gear(_: dict = Depends(get_current_user)):
    return await db.gear.find({}, {"_id": 0}).sort("name", 1).to_list(1000)

@api.post("/gear")
async def create_gear(inp: GearIn, _: dict = Depends(can_write)):
    doc = inp.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = iso(now_utc())
    await db.gear.insert_one(doc)
    doc.pop("_id", None)
    return doc

@api.put("/gear/{gid}")
async def update_gear(gid: str, inp: GearIn, _: dict = Depends(can_write)):
    res = await db.gear.update_one({"id": gid}, {"$set": inp.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(404, "Not found")
    return await db.gear.find_one({"id": gid}, {"_id": 0})

@api.delete("/gear/{gid}")
async def delete_gear(gid: str, _: dict = Depends(require_role("admin", "manager"))):
    res = await db.gear.delete_one({"id": gid})
    if res.deleted_count == 0:
        raise HTTPException(404, "Not found")
    return {"ok": True}

# ---------------- RENTALS ----------------
def _days_between(a: str, b: str) -> int:
    da = datetime.strptime(a, "%Y-%m-%d").date()
    dbb = datetime.strptime(b, "%Y-%m-%d").date()
    return max(1, (dbb - da).days + 1)

def _rental_totals(doc: dict, ledger: float = 0.0) -> dict:
    days = _days_between(doc["rent_date"], doc["return_date"])
    total = round(days * float(doc.get("daily_rate", 0)) * int(doc.get("qty", 1)), 2)
    initial = float(doc.get("paid_amount", 0) or 0)
    doc["days"] = days
    doc["total_amount"] = total
    doc["initial_paid"] = initial
    doc["ledger_paid"] = round(ledger, 2)
    doc["paid_amount"] = round(initial + ledger, 2)
    doc["balance"] = round(total - doc["paid_amount"], 2)
    return doc

@api.get("/rentals")
async def list_rentals(_: dict = Depends(get_current_user)):
    docs = await db.rentals.find({}, {"_id": 0}).sort("created_at", -1).to_list(1000)
    lm = await _ledger_map("rental")
    return [_rental_totals(d, lm.get(d["id"], 0.0)) for d in docs]

@api.post("/rentals")
async def create_rental(inp: RentalIn, _: dict = Depends(can_write)):
    gear = await db.gear.find_one({"id": inp.gear_id})
    if not gear:
        raise HTTPException(400, "Invalid gear")
    doc = inp.model_dump()
    doc["id"] = new_id()
    doc["gear_name"] = gear["name"]
    doc["created_at"] = iso(now_utc())
    await db.rentals.insert_one(doc)
    doc.pop("_id", None)
    return _rental_totals(doc, 0.0)

@api.put("/rentals/{rid}")
async def update_rental(rid: str, inp: RentalIn, _: dict = Depends(can_write)):
    gear = await db.gear.find_one({"id": inp.gear_id})
    if not gear:
        raise HTTPException(400, "Invalid gear")
    data = inp.model_dump()
    data["gear_name"] = gear["name"]
    res = await db.rentals.update_one({"id": rid}, {"$set": data})
    if res.matched_count == 0:
        raise HTTPException(404, "Not found")
    doc = await db.rentals.find_one({"id": rid}, {"_id": 0})
    return _rental_totals(doc, await _ledger_one("rental", rid))

@api.delete("/rentals/{rid}")
async def delete_rental(rid: str, _: dict = Depends(require_role("admin", "manager"))):
    res = await db.rentals.delete_one({"id": rid})
    if res.deleted_count == 0:
        raise HTTPException(404, "Not found")
    await db.payments.delete_many({"entity": "rental", "entity_id": rid})
    return {"ok": True}

# ---------------- TRANSPORT ----------------
def _transport_totals(doc: dict, ledger: float = 0.0) -> dict:
    days = _days_between(doc["start_date"], doc["end_date"])
    rounds = int(doc.get("rounds", 0) or 0)
    rate_per_round = float(doc.get("rate_per_round", 0) or 0)
    if rounds > 0 and rate_per_round > 0:
        total = round(rounds * rate_per_round, 2)
    else:
        total = round(days * float(doc.get("price_per_day", 0) or 0), 2)
    initial = float(doc.get("paid_amount", 0) or 0)
    doc["days"] = days
    doc["total_amount"] = total
    doc["initial_paid"] = initial
    doc["ledger_paid"] = round(ledger, 2)
    doc["paid_amount"] = round(initial + ledger, 2)
    doc["balance"] = round(total - doc["paid_amount"], 2)
    return doc

@api.get("/transport")
async def list_transport(_: dict = Depends(get_current_user)):
    docs = await db.transport.find({}, {"_id": 0}).sort("start_date", -1).to_list(1000)
    lm = await _ledger_map("transport")
    return [_transport_totals(d, lm.get(d["id"], 0.0)) for d in docs]

@api.post("/transport")
async def create_transport(inp: TransportIn, _: dict = Depends(can_write)):
    doc = inp.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = iso(now_utc())
    await db.transport.insert_one(doc)
    doc.pop("_id", None)
    return _transport_totals(doc, 0.0)

@api.put("/transport/{tid}")
async def update_transport(tid: str, inp: TransportIn, _: dict = Depends(can_write)):
    res = await db.transport.update_one({"id": tid}, {"$set": inp.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(404, "Not found")
    doc = await db.transport.find_one({"id": tid}, {"_id": 0})
    return _transport_totals(doc, await _ledger_one("transport", tid))

@api.delete("/transport/{tid}")
async def delete_transport(tid: str, _: dict = Depends(require_role("admin", "manager"))):
    res = await db.transport.delete_one({"id": tid})
    if res.deleted_count == 0:
        raise HTTPException(404, "Not found")
    await db.payments.delete_many({"entity": "transport", "entity_id": tid})
    return {"ok": True}

# ---------------- PAYMENT LEDGER ----------------
@api.get("/payments")
async def list_payments(entity: str, entity_id: str, _: dict = Depends(get_current_user)):
    if entity not in ("client", "rental", "transport"):
        raise HTTPException(400, "Invalid entity")
    docs = await db.payments.find({"entity": entity, "entity_id": entity_id}, {"_id": 0}).sort("date", -1).to_list(500)
    return docs

@api.post("/payments")
async def create_payment(inp: PaymentIn, _: dict = Depends(can_write)):
    # Validate parent exists
    coll = {"client": db.clients, "rental": db.rentals, "transport": db.transport}[inp.entity]
    parent = await coll.find_one({"id": inp.entity_id})
    if not parent:
        raise HTTPException(400, "Parent record not found")
    doc = inp.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = iso(now_utc())
    await db.payments.insert_one(doc)
    doc.pop("_id", None)
    return doc

@api.delete("/payments/{pid}")
async def delete_payment(pid: str, _: dict = Depends(can_write)):
    res = await db.payments.delete_one({"id": pid})
    if res.deleted_count == 0:
        raise HTTPException(404, "Not found")
    return {"ok": True}

# ---------------- TREK CATALOG ----------------
@api.get("/treks")
async def list_treks(_: dict = Depends(get_current_user)):
    return await db.treks.find({}, {"_id": 0}).sort("name", 1).to_list(500)

@api.post("/treks")
async def create_trek(inp: TrekIn, _: dict = Depends(can_write)):
    doc = inp.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = iso(now_utc())
    await db.treks.insert_one(doc)
    doc.pop("_id", None)
    return doc

@api.put("/treks/{tid}")
async def update_trek(tid: str, inp: TrekIn, _: dict = Depends(can_write)):
    res = await db.treks.update_one({"id": tid}, {"$set": inp.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(404, "Not found")
    return await db.treks.find_one({"id": tid}, {"_id": 0})

@api.delete("/treks/{tid}")
async def delete_trek(tid: str, _: dict = Depends(require_role("admin", "manager"))):
    res = await db.treks.delete_one({"id": tid})
    if res.deleted_count == 0:
        raise HTTPException(404, "Not found")
    return {"ok": True}

# ---------------- STAFF ----------------
@api.get("/staff")
async def list_staff(_: dict = Depends(get_current_user)):
    return await db.staff.find({}, {"_id": 0}).sort("name", 1).to_list(1000)

@api.post("/staff")
async def create_staff(inp: StaffIn, _: dict = Depends(can_write)):
    doc = inp.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = iso(now_utc())
    await db.staff.insert_one(doc)
    doc.pop("_id", None)
    return doc

@api.put("/staff/{sid}")
async def update_staff(sid: str, inp: StaffIn, _: dict = Depends(can_write)):
    res = await db.staff.update_one({"id": sid}, {"$set": inp.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(404, "Not found")
    return await db.staff.find_one({"id": sid}, {"_id": 0})

@api.delete("/staff/{sid}")
async def delete_staff(sid: str, _: dict = Depends(require_role("admin", "manager"))):
    res = await db.staff.delete_one({"id": sid})
    if res.deleted_count == 0:
        raise HTTPException(404, "Not found")
    return {"ok": True}

# ---------------- SALARY ----------------
def _compute_salary(staff: dict, sal: dict) -> dict:
    rate = float(staff.get("salary_rate", 0))
    days = int(sal.get("days_worked", 0))
    if staff.get("salary_type") == "per_day":
        gross = rate * days
    else:
        gross = rate  # monthly flat
    gross = round(gross + float(sal.get("bonus", 0)) - float(sal.get("deduction", 0)), 2)
    sal["gross_amount"] = gross
    sal["balance"] = round(gross - float(sal.get("paid_amount", 0)), 2)
    sal["staff_name"] = staff.get("name")
    sal["salary_type"] = staff.get("salary_type")
    sal["salary_rate"] = rate
    return sal

@api.get("/salaries")
async def list_salaries(month: Optional[str] = None, _: dict = Depends(get_current_user)):
    q = {"month": month} if month else {}
    docs = await db.salaries.find(q, {"_id": 0}).sort("month", -1).to_list(1000)
    result = []
    for d in docs:
        s = await db.staff.find_one({"id": d["staff_id"]})
        if s:
            result.append(_compute_salary(s, d))
    return result

@api.post("/salaries")
async def create_salary(inp: SalaryIn, _: dict = Depends(can_write)):
    staff = await db.staff.find_one({"id": inp.staff_id})
    if not staff:
        raise HTTPException(400, "Invalid staff")
    exists = await db.salaries.find_one({"staff_id": inp.staff_id, "month": inp.month})
    if exists:
        raise HTTPException(400, "Salary entry for this month already exists")
    doc = inp.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = iso(now_utc())
    await db.salaries.insert_one(doc)
    doc.pop("_id", None)
    return _compute_salary(staff, doc)

@api.put("/salaries/{sid}")
async def update_salary(sid: str, inp: SalaryIn, _: dict = Depends(can_write)):
    staff = await db.staff.find_one({"id": inp.staff_id})
    if not staff:
        raise HTTPException(400, "Invalid staff")
    res = await db.salaries.update_one({"id": sid}, {"$set": inp.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(404, "Not found")
    doc = await db.salaries.find_one({"id": sid}, {"_id": 0})
    return _compute_salary(staff, doc)

@api.delete("/salaries/{sid}")
async def delete_salary(sid: str, _: dict = Depends(require_role("admin", "manager"))):
    res = await db.salaries.delete_one({"id": sid})
    if res.deleted_count == 0:
        raise HTTPException(404, "Not found")
    return {"ok": True}

# ---------------- DASHBOARD ----------------
@api.get("/dashboard/summary")
async def dashboard_summary(_: dict = Depends(get_current_user)):
    today = date.today().isoformat()
    month_prefix = today[:7]

    clients = await db.clients.find({}, {"_id": 0}).to_list(10000)
    rentals = await db.rentals.find({}, {"_id": 0}).to_list(10000)
    transport = await db.transport.find({}, {"_id": 0}).to_list(10000)
    staff = await db.staff.find({}, {"_id": 0}).to_list(2000)
    expenses = await db.expenses.find({}, {"_id": 0}).to_list(10000)
    payments = await db.payments.find({}, {"_id": 0}).to_list(50000)
    salaries = await db.salaries.find({}, {"_id": 0}).to_list(5000)
    schedules = await db.trek_schedules.find({}, {"_id": 0}).to_list(2000)
    attendance_today = await db.attendance.find({"date": today}, {"_id": 0}).to_list(2000)

    def sum_paid(items, filt=None, mode=None):
        s = 0.0
        for it in items:
            if filt and not filt(it):
                continue
            if mode and it.get("payment_mode") != mode:
                continue
            s += float(it.get("paid_amount", 0) or 0)
        return round(s, 2)

    today_client_new = [c for c in clients if str(c.get("created_at", "")).startswith(today)]
    month_clients = [c for c in clients if str(c.get("created_at", "")).startswith(month_prefix)]

    def today_filter(it): return str(it.get("created_at", "")).startswith(today)
    def month_filter(it): return str(it.get("created_at", "")).startswith(month_prefix)

    # Ledger payments today
    ledger_today_cash = round(sum(float(p.get("amount", 0) or 0) for p in payments
                                  if str(p.get("date", "")) == today and p.get("mode") == "cash"), 2)
    ledger_today_online = round(sum(float(p.get("amount", 0) or 0) for p in payments
                                    if str(p.get("date", "")) == today and p.get("mode") in ("online", "upi", "card")), 2)
    ledger_month_cash = round(sum(float(p.get("amount", 0) or 0) for p in payments
                                  if str(p.get("date", "")).startswith(month_prefix) and p.get("mode") == "cash"), 2)
    ledger_month_online = round(sum(float(p.get("amount", 0) or 0) for p in payments
                                    if str(p.get("date", "")).startswith(month_prefix) and p.get("mode") in ("online", "upi", "card")), 2)

    revenue_today_cash = (
        sum_paid(clients, today_filter, "cash")
        + sum_paid(rentals, today_filter, "cash")
        + sum_paid(transport, today_filter, "cash")
        + ledger_today_cash
    )
    revenue_today_online = sum(sum_paid(x, today_filter, m) for x in [clients, rentals, transport]
                               for m in ["online", "upi", "card"]) + ledger_today_online
    revenue_today = round(revenue_today_cash + revenue_today_online, 2)

    revenue_month_cash = (
        sum_paid(clients, month_filter, "cash")
        + sum_paid(rentals, month_filter, "cash")
        + sum_paid(transport, month_filter, "cash")
        + ledger_month_cash
    )
    revenue_month_online = sum(sum_paid(x, month_filter, m) for x in [clients, rentals, transport]
                               for m in ["online", "upi", "card"]) + ledger_month_online
    revenue_month = round(revenue_month_cash + revenue_month_online, 2)

    # Today's KPIs from bookings created today
    today_pax = sum(int(c.get("people_count", 0) or 0) for c in today_client_new)
    today_package = round(sum(float(c.get("trek_amount", 0) or 0) for c in today_client_new), 2)
    today_advance = round(sum(float(c.get("paid_amount", 0) or 0) for c in today_client_new), 2)
    today_remaining = round(today_package - today_advance, 2)

    # Rental income + pending
    rental_total_all = 0.0
    for r in rentals:
        days = _days_between(r["rent_date"], r["return_date"])
        rental_total_all += days * float(r.get("daily_rate", 0) or 0) * int(r.get("qty", 1) or 1)
    rental_income = round(sum(float(r.get("paid_amount", 0) or 0) for r in rentals)
                          + sum(float(p.get("amount", 0) or 0) for p in payments if p.get("entity") == "rental"), 2)
    rental_pending = round(rental_total_all - rental_income, 2)

    # Transport expense (money going out)
    transport_expense = round(sum(float(t.get("paid_amount", 0) or 0) for t in transport)
                              + sum(float(p.get("amount", 0) or 0) for p in payments if p.get("entity") == "transport"), 2)

    # Outstanding balances
    def outstanding_clients():
        return round(sum(max(0.0, float(c.get("trek_amount", 0) or 0)
                             - float(c.get("paid_amount", 0) or 0)
                             - sum(float(p.get("amount", 0) or 0) for p in payments
                                   if p.get("entity") == "client" and p.get("entity_id") == c["id"]))
                         for c in clients), 2)
    def outstanding_rentals():
        s = 0.0
        for r in rentals:
            total = _days_between(r["rent_date"], r["return_date"]) * float(r.get("daily_rate", 0) or 0) * int(r.get("qty", 1) or 1)
            paid = float(r.get("paid_amount", 0) or 0) + sum(float(p.get("amount", 0) or 0) for p in payments
                                                             if p.get("entity") == "rental" and p.get("entity_id") == r["id"])
            s += max(0.0, total - paid)
        return round(s, 2)
    def outstanding_transport():
        s = 0.0
        for t in transport:
            rd = int(t.get("rounds", 0) or 0)
            rr = float(t.get("rate_per_round", 0) or 0)
            if rd > 0 and rr > 0:
                total = rd * rr
            else:
                total = _days_between(t["start_date"], t["end_date"]) * float(t.get("price_per_day", 0) or 0)
            paid = float(t.get("paid_amount", 0) or 0) + sum(float(p.get("amount", 0) or 0) for p in payments
                                                             if p.get("entity") == "transport" and p.get("entity_id") == t["id"])
            s += max(0.0, total - paid)
        return round(s, 2)

    active_transport = [t for t in transport if t.get("status") == "active"]
    active_schedules = [s for s in schedules if s.get("status") in ("scheduled", "active")]
    active_guides = [s for s in staff if s.get("is_active") and (s.get("role") or "").lower() == "guide"]
    active_staff = [s for s in staff if s.get("is_active")]

    # Attendance today
    present_ids = {a["staff_id"] for a in attendance_today if a.get("status") in ("present", "half_day")}
    absent_ids = {a["staff_id"] for a in attendance_today if a.get("status") == "absent"}
    total_active_staff_ids = {s["id"] for s in staff if s.get("is_active")}
    unmarked = total_active_staff_ids - present_ids - absent_ids
    staff_present = len(present_ids)
    staff_absent = len(absent_ids) + len(unmarked)  # unmarked treated as absent

    # Today's salary expense (approximate from per_day staff marked present today)
    per_day_wage_today = 0.0
    for s in staff:
        if s.get("salary_type") == "per_day" and s["id"] in present_ids:
            per_day_wage_today += float(s.get("salary_rate", 0) or 0)
    per_day_wage_today = round(per_day_wage_today, 2)

    # Today's expenses (from expenses collection)
    today_expenses = round(sum(float(e.get("amount", 0) or 0) for e in expenses if e.get("date") == today), 2)

    # Today's cash & online (uses same revenue split above)
    today_total_collection = revenue_today

    # Total income vs expenses (all-time for now)
    trek_income_all = round(sum(float(c.get("paid_amount", 0) or 0) for c in clients)
                            + sum(float(p.get("amount", 0) or 0) for p in payments if p.get("entity") == "client"), 2)
    total_income = round(trek_income_all + rental_income, 2)
    total_expenses = round(transport_expense
                           + sum(float(e.get("amount", 0) or 0) for e in expenses)
                           + sum(float(s.get("paid_amount", 0) or 0) for s in salaries), 2)
    net_profit = round(total_income - total_expenses, 2)

    trend = []
    for i in range(6, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        def f(it, day=d): return str(it.get("created_at", "")).startswith(day)
        cash_ = sum_paid(clients, f, "cash") + sum_paid(rentals, f, "cash") + sum_paid(transport, f, "cash")
        cash_ += sum(float(p.get("amount", 0) or 0) for p in payments if p.get("date") == d and p.get("mode") == "cash")
        online_ = sum(sum_paid(x, f, m) for x in [clients, rentals, transport] for m in ["online", "upi", "card"])
        online_ += sum(float(p.get("amount", 0) or 0) for p in payments if p.get("date") == d and p.get("mode") in ("online", "upi", "card"))
        trend.append({"date": d, "cash": round(cash_, 2), "online": round(online_, 2),
                      "total": round(cash_ + online_, 2)})

    return {
        "today": {
            "new_clients": len(today_client_new),
            "pax": today_pax,
            "package_amount": today_package,
            "advance": today_advance,
            "remaining": today_remaining,
            "revenue_total": revenue_today,
            "revenue_cash": revenue_today_cash,
            "revenue_online": revenue_today_online,
            "expenses": today_expenses,
            "salary_expense": per_day_wage_today,
        },
        "month": {
            "clients": len(month_clients),
            "revenue_total": revenue_month,
            "revenue_cash": revenue_month_cash,
            "revenue_online": revenue_month_online,
        },
        "totals": {
            "clients": len(clients),
            "gear_items": await db.gear.count_documents({}),
            "transport_trips": len(transport),
            "active_transport": len(active_transport),
            "active_schedules": len(active_schedules),
            "staff_total": len(staff),
            "staff_active": len(active_staff),
            "active_guides": len(active_guides),
            "staff_present": staff_present,
            "staff_absent": staff_absent,
        },
        "money": {
            "rental_total": round(rental_total_all, 2),
            "rental_income": rental_income,
            "rental_pending": rental_pending,
            "transport_expense": transport_expense,
            "total_income": total_income,
            "total_expenses": total_expenses,
            "net_profit": net_profit,
        },
        "outstanding": {
            "clients": outstanding_clients(),
            "rentals": outstanding_rentals(),
            "transport": outstanding_transport(),
        },
        "trend_7d": trend,
        "active_transport_list": active_transport[:10],
        "active_staff_list": [
            {"id": s["id"], "name": s["name"], "role": s.get("role"), "active_trek": s.get("active_trek")}
            for s in active_staff[:10]
        ],
        "active_schedules_list": active_schedules[:10],
    }

# ---------------- INVOICES ----------------
COMPANY_NAME = "THE PEAK FREAKS"

def _fmt_inr(n) -> str:
    try:
        return f"Rs.{int(round(float(n or 0))):,}"
    except Exception:
        return "Rs.0"

async def _build_invoice(entity: str, item_id: str):
    """Returns (subject, text_lines, phone, filename_slug, table_headers, table_rows, totals)."""
    if entity == "client":
        d = await db.clients.find_one({"id": item_id}, {"_id": 0})
        if not d:
            raise HTTPException(404, "Client not found")
        amount = float(d.get("trek_amount", 0))
        paid = float(d.get("paid_amount", 0)) + await _ledger_one("client", item_id)
        bal = round(amount - paid, 2)
        phone = d.get("phone") or ""
        subject = f"Booking Invoice - {d.get('trek_name')}"
        lines = [
            f"{COMPANY_NAME}",
            f"Invoice for: {d.get('name')}",
            f"Trek: {d.get('trek_name')}",
            f"Dates: {d.get('start_date')} to {d.get('end_date')}",
            f"People: {d.get('people_count')}",
            "",
            f"Trek Amount: {_fmt_inr(amount)}",
            f"Paid:        {_fmt_inr(paid)}",
            f"Balance:     {_fmt_inr(bal)}",
            f"Mode:        {str(d.get('payment_mode','')).upper()}",
        ]
        if d.get("notes"):
            lines += ["", f"Note: {d.get('notes')}"]
        rows = [["Trek Package", d.get("trek_name"), d.get("people_count"),
                 _fmt_inr(amount)]]
        headers = ["Item", "Description", "Qty", "Amount"]
        totals = {"total": amount, "paid": paid, "balance": bal}
        slug = f"invoice_client_{d.get('name','client').replace(' ','_')}"
        return subject, lines, phone, slug, headers, rows, totals

    if entity == "rental":
        d = await db.rentals.find_one({"id": item_id}, {"_id": 0})
        if not d:
            raise HTTPException(404, "Rental not found")
        days = _days_between(d["rent_date"], d["return_date"])
        rate = float(d.get("daily_rate", 0))
        qty = int(d.get("qty", 1))
        total = round(days * rate * qty, 2)
        paid = float(d.get("paid_amount", 0)) + await _ledger_one("rental", item_id)
        bal = round(total - paid, 2)
        phone = d.get("customer_phone") or ""
        subject = f"Rental Invoice - {d.get('gear_name')}"
        lines = [
            f"{COMPANY_NAME}",
            f"Rental Invoice for: {d.get('customer_name')}",
            f"Item: {d.get('gear_name')}  x {qty}",
            f"From: {d.get('rent_date')}  To: {d.get('return_date')}  ({days} days)",
            "",
            f"Rate:    {_fmt_inr(rate)}/day",
            f"Total:   {_fmt_inr(total)}",
            f"Paid:    {_fmt_inr(paid)}",
            f"Balance: {_fmt_inr(bal)}",
            f"Mode:    {str(d.get('payment_mode','')).upper()}",
        ]
        headers = ["Item", "Qty", "Days", "Rate/Day", "Amount"]
        rows = [[d.get("gear_name"), qty, days, _fmt_inr(rate), _fmt_inr(total)]]
        totals = {"total": total, "paid": paid, "balance": bal}
        slug = f"invoice_rental_{d.get('customer_name','customer').replace(' ','_')}"
        return subject, lines, phone, slug, headers, rows, totals

    if entity == "transport":
        d = await db.transport.find_one({"id": item_id}, {"_id": 0})
        if not d:
            raise HTTPException(404, "Transport not found")
        days = _days_between(d["start_date"], d["end_date"])
        rate = float(d.get("price_per_day", 0))
        total = round(days * rate, 2)
        paid = float(d.get("paid_amount", 0)) + await _ledger_one("transport", item_id)
        bal = round(total - paid, 2)
        phone = d.get("driver_phone") or ""
        subject = f"Transport Invoice - {d.get('vehicle_no')}"
        lines = [
            f"{COMPANY_NAME}",
            f"Transport Invoice",
            f"Vehicle: {d.get('vehicle_no')} ({d.get('vehicle_type')})",
            f"Driver:  {d.get('driver_name')}",
            f"Route:   {d.get('route')}",
            f"From: {d.get('start_date')}  To: {d.get('end_date')}  ({days} days)",
            "",
            f"Rate:    {_fmt_inr(rate)}/day",
            f"Total:   {_fmt_inr(total)}",
            f"Paid:    {_fmt_inr(paid)}",
            f"Balance: {_fmt_inr(bal)}",
            f"Mode:    {str(d.get('payment_mode','')).upper()}",
        ]
        headers = ["Vehicle", "Route", "Days", "Rate/Day", "Amount"]
        rows = [[d.get("vehicle_no"), d.get("route"), days, _fmt_inr(rate), _fmt_inr(total)]]
        totals = {"total": total, "paid": paid, "balance": bal}
        slug = f"invoice_transport_{d.get('vehicle_no','vehicle').replace(' ','_')}"
        return subject, lines, phone, slug, headers, rows, totals

    raise HTTPException(400, "Unknown entity")

@api.get("/invoice/{entity}/{item_id}")
async def get_invoice(entity: str, item_id: str, _: dict = Depends(get_current_user)):
    subject, lines, phone, slug, _h, _r, totals = await _build_invoice(entity, item_id)
    text = "\n".join(lines) + f"\n\nThank you for choosing {COMPANY_NAME}."
    return {"subject": subject, "text": text, "phone": phone, "totals": totals, "filename": slug}

@api.get("/invoice/{entity}/{item_id}/pdf")
async def get_invoice_pdf(entity: str, item_id: str, _: dict = Depends(get_current_user)):
    subject, lines, phone, slug, headers, rows, totals = await _build_invoice(entity, item_id)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=28, bottomMargin=28, leftMargin=32, rightMargin=32)
    styles = getSampleStyleSheet()
    elems = []

    # Branded header — obsidian bar with company name + terracotta accent
    invoice_no = item_id[:8].upper()
    header = Table([[
        Paragraph("<font color='white' size=20 face='Helvetica-Bold'>THE PEAK FREAKS</font>"
                  "<br/><font color='#D95C41' size=9 face='Helvetica'>ADVENTURE OPERATIONS &nbsp;·&nbsp; TREK · GEAR · TRANSPORT</font>",
                  styles["Normal"]),
        Paragraph(f"<font color='white' size=9>INVOICE #<b>{invoice_no}</b></font>"
                  f"<br/><font color='#999' size=8>Date: {date.today().isoformat()}</font>",
                  styles["Normal"])
    ]], colWidths=[380, 145])
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#161616")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 18),
        ("RIGHTPADDING", (0, 0), (-1, -1), 18),
        ("TOPPADDING", (0, 0), (-1, -1), 16),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 16),
    ]))
    elems.append(header)
    elems.append(Spacer(1, 4))
    # Terracotta accent bar
    accent = Table([[""]], colWidths=[531], rowHeights=[4])
    accent.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#D95C41"))]))
    elems.append(accent)
    elems.append(Spacer(1, 16))

    elems.append(Paragraph(f"<font size=13 color='#161616'><b>{subject}</b></font>", styles["Normal"]))
    elems.append(Spacer(1, 10))

    # Info block from lines (skip first 2 title lines)
    for ln in lines[2:]:
        if ln.strip():
            safe = ln.replace("  ", "&nbsp;&nbsp;")
            elems.append(Paragraph(f"<font size=10 color='#333'>{safe}</font>", styles["Normal"]))
    elems.append(Spacer(1, 14))

    t = Table([headers] + [[str(c) for c in r] for r in rows], hAlign="LEFT", colWidths=None)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#161616")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E5E5")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFAFA")]),
    ]))
    elems.append(t)
    elems.append(Spacer(1, 14))

    tot = Table([
        ["Total", _fmt_inr(totals["total"])],
        ["Paid", _fmt_inr(totals["paid"])],
        ["Balance Due", _fmt_inr(totals["balance"])],
    ], hAlign="RIGHT", colWidths=[110, 130])
    tot.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#D95C41")),
        ("TEXTCOLOR", (0, -1), (-1, -1), colors.white),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTNAME", (0, 0), (0, -2), "Helvetica-Bold"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E5E5")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E5E5")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
    ]))
    elems.append(tot)
    elems.append(Spacer(1, 28))

    # Footer
    footer = Table([[
        Paragraph("<font size=8 color='#999'>"
                  f"THE PEAK FREAKS · Adventure Operations<br/>"
                  f"Contact: {phone or '—'} · admin@peakfreaks.com"
                  "</font>", styles["Normal"]),
        Paragraph("<font size=8 color='#999'>Thank you for climbing with us.</font>", styles["Normal"]),
    ]], colWidths=[350, 175])
    footer.setStyle(TableStyle([
        ("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.HexColor("#E5E5E5")),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    elems.append(footer)

    doc.build(elems)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{slug}.pdf"'},
    )

# ---------------- EXPORTS ----------------
def _excel_response(headers: List[str], rows: List[List], filename: str):
    wb = Workbook()
    ws = wb.active
    ws.title = filename[:30]
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}.xlsx"'},
    )

def _pdf_response(title: str, headers: List[str], rows: List[List], filename: str):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=30, bottomMargin=30, leftMargin=24, rightMargin=24)
    styles = getSampleStyleSheet()
    elems = [Paragraph(f"<b>THE PEAK FREAKS</b> &nbsp;&nbsp; <font size=10 color='#666'>{title}</font>", styles["Title"]),
             Spacer(1, 10)]
    data = [headers] + rows
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#161616")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5E5E5")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFAFA")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    elems.append(t)
    doc.build(elems)
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}.pdf"'},
    )

@api.get("/export/{entity}.{fmt}")
async def export_entity(entity: str, fmt: str, _: dict = Depends(get_current_user)):
    if fmt not in ("xlsx", "pdf"):
        raise HTTPException(400, "Unsupported format")

    if entity == "clients":
        docs = await db.clients.find({}, {"_id": 0}).sort("created_at", -1).to_list(5000)
        headers = ["Name", "Phone", "Trek", "Start", "End", "People", "Amount", "Paid", "Balance", "Mode"]
        rows = [[d.get("name"), d.get("phone"), d.get("trek_name"), d.get("start_date"), d.get("end_date"),
                 d.get("people_count"), d.get("trek_amount"), d.get("paid_amount"),
                 round(float(d.get("trek_amount", 0)) - float(d.get("paid_amount", 0)), 2),
                 d.get("payment_mode")] for d in docs]
        title, fname = "Clients Report", "peakfreaks_clients"
    elif entity == "rentals":
        docs = await db.rentals.find({}, {"_id": 0}).sort("created_at", -1).to_list(5000)
        headers = ["Customer", "Gear", "Qty", "From", "To", "Days", "Rate", "Total", "Paid", "Balance", "Mode"]
        rows = []
        for d in docs:
            days = _days_between(d["rent_date"], d["return_date"])
            total = days * float(d.get("daily_rate", 0)) * int(d.get("qty", 1))
            rows.append([d.get("customer_name"), d.get("gear_name"), d.get("qty"),
                         d.get("rent_date"), d.get("return_date"), days,
                         d.get("daily_rate"), round(total, 2), d.get("paid_amount"),
                         round(total - float(d.get("paid_amount", 0)), 2), d.get("payment_mode")])
        title, fname = "Rentals Report", "peakfreaks_rentals"
    elif entity == "transport":
        docs = await db.transport.find({}, {"_id": 0}).sort("start_date", -1).to_list(5000)
        headers = ["Vehicle No", "Type", "Driver", "Route", "Start", "End", "Days", "Rate/Day", "Total", "Paid", "Balance", "Status"]
        rows = []
        for d in docs:
            days = _days_between(d["start_date"], d["end_date"])
            total = days * float(d.get("price_per_day", 0))
            rows.append([d.get("vehicle_no"), d.get("vehicle_type"), d.get("driver_name"), d.get("route"),
                         d.get("start_date"), d.get("end_date"), days, d.get("price_per_day"),
                         round(total, 2), d.get("paid_amount"),
                         round(total - float(d.get("paid_amount", 0)), 2), d.get("status")])
        title, fname = "Transport Report", "peakfreaks_transport"
    elif entity == "staff":
        docs = await db.staff.find({}, {"_id": 0}).sort("name", 1).to_list(5000)
        headers = ["Name", "Phone", "Role", "Salary Type", "Rate", "Active Trek", "Status"]
        rows = [[d.get("name"), d.get("phone"), d.get("role"), d.get("salary_type"),
                 d.get("salary_rate"), d.get("active_trek") or "-",
                 "Active" if d.get("is_active") else "Inactive"] for d in docs]
        title, fname = "Staff Report", "peakfreaks_staff"
    elif entity == "salaries":
        docs = await db.salaries.find({}, {"_id": 0}).sort("month", -1).to_list(5000)
        headers = ["Staff", "Month", "Type", "Rate", "Days", "Bonus", "Deduction", "Gross", "Paid", "Balance", "Mode"]
        rows = []
        for d in docs:
            s = await db.staff.find_one({"id": d["staff_id"]})
            if not s:
                continue
            rate = float(s.get("salary_rate", 0))
            gross = (rate * int(d.get("days_worked", 0))) if s.get("salary_type") == "per_day" else rate
            gross = round(gross + float(d.get("bonus", 0)) - float(d.get("deduction", 0)), 2)
            rows.append([s["name"], d.get("month"), s.get("salary_type"), rate, d.get("days_worked"),
                         d.get("bonus"), d.get("deduction"), gross, d.get("paid_amount"),
                         round(gross - float(d.get("paid_amount", 0)), 2), d.get("payment_mode")])
        title, fname = "Salaries Report", "peakfreaks_salaries"
    else:
        raise HTTPException(404, "Unknown entity")

    if fmt == "xlsx":
        return _excel_response(headers, rows, fname)
    return _pdf_response(title, headers, rows, fname)

# ============================================================
# EXPENSES
# ============================================================
class ExpenseIn(BaseModel):
    date: str
    category: str
    amount: float
    description: Optional[str] = None
    payment_mode: Literal["cash", "online", "upi", "card"] = "cash"

@api.get("/expenses")
async def list_expenses(_: dict = Depends(get_current_user)):
    return await db.expenses.find({}, {"_id": 0}).sort("date", -1).to_list(2000)

@api.post("/expenses")
async def create_expense(inp: ExpenseIn, _: dict = Depends(can_write)):
    doc = inp.model_dump()
    doc["id"] = new_id()
    doc["created_at"] = iso(now_utc())
    await db.expenses.insert_one(doc)
    doc.pop("_id", None)
    return doc

@api.put("/expenses/{eid}")
async def update_expense(eid: str, inp: ExpenseIn, _: dict = Depends(can_write)):
    res = await db.expenses.update_one({"id": eid}, {"$set": inp.model_dump()})
    if res.matched_count == 0:
        raise HTTPException(404, "Not found")
    return await db.expenses.find_one({"id": eid}, {"_id": 0})

@api.delete("/expenses/{eid}")
async def delete_expense(eid: str, _: dict = Depends(require_role("admin", "manager"))):
    res = await db.expenses.delete_one({"id": eid})
    if res.deleted_count == 0:
        raise HTTPException(404, "Not found")
    return {"ok": True}

# ============================================================
# ATTENDANCE
# ============================================================
class AttendanceIn(BaseModel):
    staff_id: str
    date: str
    status: Literal["present", "absent", "leave", "half_day"] = "present"
    notes: Optional[str] = None

@api.get("/attendance")
async def list_attendance(date: Optional[str] = None, staff_id: Optional[str] = None,
                          _: dict = Depends(get_current_user)):
    q = {}
    if date: q["date"] = date
    if staff_id: q["staff_id"] = staff_id
    docs = await db.attendance.find(q, {"_id": 0}).sort("date", -1).to_list(5000)
    # decorate with staff name
    ids = list({d["staff_id"] for d in docs})
    if ids:
        staff_map = {s["id"]: s async for s in db.staff.find({"id": {"$in": ids}}, {"_id": 0})}
        for d in docs:
            s = staff_map.get(d["staff_id"])
            d["staff_name"] = s.get("name") if s else "—"
            d["staff_role"] = s.get("role") if s else ""
    return docs

@api.post("/attendance")
async def upsert_attendance(inp: AttendanceIn, _: dict = Depends(can_write)):
    staff = await db.staff.find_one({"id": inp.staff_id})
    if not staff:
        raise HTTPException(400, "Invalid staff")
    doc = inp.model_dump()
    doc["created_at"] = iso(now_utc())
    existing = await db.attendance.find_one({"staff_id": inp.staff_id, "date": inp.date})
    if existing:
        await db.attendance.update_one({"_id": existing["_id"]}, {"$set": doc})
        doc["id"] = existing["id"]
    else:
        doc["id"] = new_id()
        await db.attendance.insert_one(doc)
    doc.pop("_id", None)
    doc["staff_name"] = staff.get("name")
    doc["staff_role"] = staff.get("role")
    return doc

@api.delete("/attendance/{aid}")
async def delete_attendance(aid: str, _: dict = Depends(can_write)):
    await db.attendance.delete_one({"id": aid})
    return {"ok": True}

# ============================================================
# TRANSPORTERS (vendors) & VEHICLES (registry)
# ============================================================
class TransporterIn(BaseModel):
    name: str
    phone: Optional[str] = None
    address: Optional[str] = None
    notes: Optional[str] = None

class VehicleIn(BaseModel):
    vehicle_no: str
    vehicle_type: str = "SUV"
    driver_name: Optional[str] = None
    driver_phone: Optional[str] = None
    transporter_id: Optional[str] = None
    seat_capacity: int = 6
    is_active: bool = True
    notes: Optional[str] = None

@api.get("/transporters")
async def list_transporters(_: dict = Depends(get_current_user)):
    return await db.transporters.find({}, {"_id": 0}).sort("name", 1).to_list(500)

@api.post("/transporters")
async def create_transporter(inp: TransporterIn, _: dict = Depends(can_write)):
    doc = inp.model_dump(); doc["id"] = new_id(); doc["created_at"] = iso(now_utc())
    await db.transporters.insert_one(doc); doc.pop("_id", None); return doc

@api.put("/transporters/{tid}")
async def update_transporter(tid: str, inp: TransporterIn, _: dict = Depends(can_write)):
    res = await db.transporters.update_one({"id": tid}, {"$set": inp.model_dump()})
    if res.matched_count == 0: raise HTTPException(404, "Not found")
    return await db.transporters.find_one({"id": tid}, {"_id": 0})

@api.delete("/transporters/{tid}")
async def delete_transporter(tid: str, _: dict = Depends(require_role("admin", "manager"))):
    await db.transporters.delete_one({"id": tid}); return {"ok": True}

@api.get("/vehicles")
async def list_vehicles(_: dict = Depends(get_current_user)):
    docs = await db.vehicles.find({}, {"_id": 0}).sort("vehicle_no", 1).to_list(500)
    # decorate with transporter name
    ids = list({d.get("transporter_id") for d in docs if d.get("transporter_id")})
    if ids:
        tmap = {t["id"]: t async for t in db.transporters.find({"id": {"$in": ids}}, {"_id": 0})}
        for d in docs:
            t = tmap.get(d.get("transporter_id"))
            d["transporter_name"] = t.get("name") if t else None
    return docs

@api.post("/vehicles")
async def create_vehicle(inp: VehicleIn, _: dict = Depends(can_write)):
    doc = inp.model_dump(); doc["id"] = new_id(); doc["created_at"] = iso(now_utc())
    await db.vehicles.insert_one(doc); doc.pop("_id", None); return doc

@api.put("/vehicles/{vid}")
async def update_vehicle(vid: str, inp: VehicleIn, _: dict = Depends(can_write)):
    res = await db.vehicles.update_one({"id": vid}, {"$set": inp.model_dump()})
    if res.matched_count == 0: raise HTTPException(404, "Not found")
    return await db.vehicles.find_one({"id": vid}, {"_id": 0})

@api.delete("/vehicles/{vid}")
async def delete_vehicle(vid: str, _: dict = Depends(require_role("admin", "manager"))):
    await db.vehicles.delete_one({"id": vid}); return {"ok": True}

# ============================================================
# TREK SCHEDULES (scheduled runs of treks, distinct from catalog)
# ============================================================
class TrekScheduleIn(BaseModel):
    trek_name: str
    trek_date: str
    end_date: Optional[str] = None
    total_groups: int = 1
    total_pax: int = 0
    assigned_guide_id: Optional[str] = None
    assigned_vehicle_id: Optional[str] = None
    status: Literal["scheduled", "active", "completed", "cancelled"] = "scheduled"
    notes: Optional[str] = None

@api.get("/trek-schedules")
async def list_schedules(_: dict = Depends(get_current_user)):
    docs = await db.trek_schedules.find({}, {"_id": 0}).sort("trek_date", -1).to_list(500)
    # decorate with guide + vehicle
    gids = list({d.get("assigned_guide_id") for d in docs if d.get("assigned_guide_id")})
    vids = list({d.get("assigned_vehicle_id") for d in docs if d.get("assigned_vehicle_id")})
    gmap = {s["id"]: s async for s in db.staff.find({"id": {"$in": gids}}, {"_id": 0})} if gids else {}
    vmap = {v["id"]: v async for v in db.vehicles.find({"id": {"$in": vids}}, {"_id": 0})} if vids else {}
    for d in docs:
        g = gmap.get(d.get("assigned_guide_id"))
        v = vmap.get(d.get("assigned_vehicle_id"))
        d["guide_name"] = g.get("name") if g else None
        d["vehicle_no"] = v.get("vehicle_no") if v else None
    return docs

@api.post("/trek-schedules")
async def create_schedule(inp: TrekScheduleIn, _: dict = Depends(can_write)):
    doc = inp.model_dump(); doc["id"] = new_id(); doc["created_at"] = iso(now_utc())
    await db.trek_schedules.insert_one(doc); doc.pop("_id", None); return doc

@api.put("/trek-schedules/{sid}")
async def update_schedule(sid: str, inp: TrekScheduleIn, _: dict = Depends(can_write)):
    res = await db.trek_schedules.update_one({"id": sid}, {"$set": inp.model_dump()})
    if res.matched_count == 0: raise HTTPException(404, "Not found")
    return await db.trek_schedules.find_one({"id": sid}, {"_id": 0})

@api.delete("/trek-schedules/{sid}")
async def delete_schedule(sid: str, _: dict = Depends(require_role("admin", "manager"))):
    await db.trek_schedules.delete_one({"id": sid}); return {"ok": True}

# ============================================================
# SETTINGS (single-doc — company info)
# ============================================================
class SettingsIn(BaseModel):
    company_name: str = "THE PEAK FREAKS"
    tagline: Optional[str] = "Adventure Operations · Trek · Gear · Transport"
    address: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    gstin: Optional[str] = None
    website: Optional[str] = None
    logo_data_url: Optional[str] = None  # base64 or hosted URL — user will set later
    currency_symbol: str = "₹"
    invoice_terms: Optional[str] = None

@api.get("/settings")
async def get_settings(_: dict = Depends(get_current_user)):
    doc = await db.settings.find_one({"id": "singleton"}, {"_id": 0})
    if not doc:
        # seed defaults
        default = SettingsIn().model_dump()
        default["id"] = "singleton"
        default["updated_at"] = iso(now_utc())
        await db.settings.insert_one(default)
        default.pop("_id", None)
        return default
    return doc

@api.put("/settings")
async def update_settings(inp: SettingsIn, _: dict = Depends(require_role("admin"))):
    data = inp.model_dump()
    data["id"] = "singleton"
    data["updated_at"] = iso(now_utc())
    await db.settings.update_one({"id": "singleton"}, {"$set": data}, upsert=True)
    return await db.settings.find_one({"id": "singleton"}, {"_id": 0})

# ============================================================
# BACKUP & RESTORE
# ============================================================
BACKUP_COLLECTIONS = [
    "clients", "gear", "rentals", "transport", "staff", "salaries",
    "payments", "treks", "expenses", "attendance", "transporters",
    "vehicles", "trek_schedules", "settings",
]

@api.get("/backup/export")
async def backup_export(_: dict = Depends(require_role("admin"))):
    dump = {"generated_at": iso(now_utc()), "collections": {}}
    for name in BACKUP_COLLECTIONS:
        docs = await db[name].find({}, {"_id": 0}).to_list(100000)
        dump["collections"][name] = docs
    import json as _json
    data = _json.dumps(dump, indent=2, default=str).encode()
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="peakfreaks_backup_{date.today().isoformat()}.json"'},
    )

class RestoreIn(BaseModel):
    payload: dict
    wipe_first: bool = False

@api.post("/backup/restore")
async def backup_restore(inp: RestoreIn, _: dict = Depends(require_role("admin"))):
    counts = {}
    cols = inp.payload.get("collections", {})
    if not isinstance(cols, dict):
        raise HTTPException(400, "Invalid backup payload")
    for name in BACKUP_COLLECTIONS:
        docs = cols.get(name, [])
        if not isinstance(docs, list):
            continue
        if inp.wipe_first:
            await db[name].delete_many({})
        if docs:
            # avoid _id conflicts
            for d in docs:
                d.pop("_id", None)
            try:
                await db[name].insert_many(docs, ordered=False)
            except Exception:
                # fall back to upsert on 'id'
                for d in docs:
                    if "id" in d:
                        await db[name].update_one({"id": d["id"]}, {"$set": d}, upsert=True)
        counts[name] = len(docs)
    return {"ok": True, "restored": counts}

# ============================================================
# REPORTS (aggregated summaries)
# ============================================================
def _date_range_filter(items, start, end, field="created_at"):
    def ok(x):
        val = str(x.get(field, ""))[:10]
        return start <= val <= end
    return [i for i in items if ok(i)]

@api.get("/reports/summary")
async def report_summary(start: str, end: str, _: dict = Depends(get_current_user)):
    clients = await db.clients.find({}, {"_id": 0}).to_list(10000)
    rentals = await db.rentals.find({}, {"_id": 0}).to_list(10000)
    transport = await db.transport.find({}, {"_id": 0}).to_list(10000)
    expenses = await db.expenses.find({}, {"_id": 0}).to_list(10000)
    payments = await db.payments.find({}, {"_id": 0}).to_list(50000)

    # Filter by created_at prefix (bookings) or date field (payments/expenses)
    c_in = _date_range_filter(clients, start, end, "created_at")
    r_in = _date_range_filter(rentals, start, end, "created_at")
    t_in = _date_range_filter(transport, start, end, "start_date")
    e_in = _date_range_filter(expenses, start, end, "date")
    p_in = _date_range_filter(payments, start, end, "date")

    def sum_field(items, field): return round(sum(float(i.get(field, 0) or 0) for i in items), 2)
    def sum_paid_mode(items, modes):
        return round(sum(float(i.get("paid_amount", 0) or 0) for i in items if i.get("payment_mode") in modes), 2)

    # Ledger payments in range grouped by mode + entity
    ledger_by_mode = {"cash": 0.0, "online": 0.0, "upi": 0.0, "card": 0.0}
    ledger_by_entity = {"client": 0.0, "rental": 0.0, "transport": 0.0}
    for p in p_in:
        m = p.get("mode", "cash")
        e = p.get("entity", "client")
        amt = float(p.get("amount", 0) or 0)
        if m in ledger_by_mode:
            ledger_by_mode[m] += amt
        if e in ledger_by_entity:
            ledger_by_entity[e] += amt

    total_package = sum_field(c_in, "trek_amount")
    client_initial = sum_field(c_in, "paid_amount")
    total_pax = sum(int(c.get("people_count", 0) or 0) for c in c_in)

    rental_total = 0.0
    for r in r_in:
        days = _days_between(r["rent_date"], r["return_date"])
        rental_total += days * float(r.get("daily_rate", 0) or 0) * int(r.get("qty", 1) or 1)
    rental_total = round(rental_total, 2)
    rental_initial = sum_field(r_in, "paid_amount")

    transport_total = 0.0
    for tr in t_in:
        days = _days_between(tr["start_date"], tr["end_date"])
        rd = int(tr.get("rounds", 0) or 0)
        rr = float(tr.get("rate_per_round", 0) or 0)
        if rd > 0 and rr > 0:
            transport_total += rd * rr
        else:
            transport_total += days * float(tr.get("price_per_day", 0) or 0)
    transport_total = round(transport_total, 2)
    transport_initial = sum_field(t_in, "paid_amount")

    cash_collected = (
        sum_paid_mode(c_in, ["cash"]) + sum_paid_mode(r_in, ["cash"]) + ledger_by_mode["cash"]
    )
    online_collected = (
        sum_paid_mode(c_in, ["online", "upi", "card"])
        + sum_paid_mode(r_in, ["online", "upi", "card"])
        + ledger_by_mode["online"] + ledger_by_mode["upi"] + ledger_by_mode["card"]
    )

    trek_income = client_initial + ledger_by_entity["client"]
    rental_income = rental_initial + ledger_by_entity["rental"]
    transport_expense = transport_initial + ledger_by_entity["transport"]
    other_expenses = sum_field(e_in, "amount")
    # Salary expense during range (paid_amount on salary docs; month-level record so approximate)
    salaries = await db.salaries.find({}, {"_id": 0}).to_list(10000)
    salary_expense = 0.0
    for s in salaries:
        m = s.get("month", "")
        if len(m) == 7 and start[:7] <= m <= end[:7]:
            salary_expense += float(s.get("paid_amount", 0) or 0)
    salary_expense = round(salary_expense, 2)
    total_income = round(trek_income + rental_income, 2)
    total_expenses = round(transport_expense + other_expenses + salary_expense, 2)
    net_profit = round(total_income - total_expenses, 2)

    return {
        "range": {"start": start, "end": end},
        "bookings": {
            "count": len(c_in),
            "total_pax": total_pax,
            "package_amount": total_package,
            "advance_received": round(trek_income, 2),
            "remaining": round(total_package - trek_income, 2),
            "cash": cash_collected,
            "online": online_collected,
            "total_collection": round(cash_collected + online_collected, 2),
        },
        "rentals": {
            "count": len(r_in),
            "total": rental_total,
            "collected": round(rental_income, 2),
            "pending": round(rental_total - rental_income, 2),
        },
        "transport": {
            "count": len(t_in),
            "total": transport_total,
            "paid": round(transport_expense, 2),
            "pending": round(transport_total - transport_expense, 2),
            "total_rounds": sum(int(t.get("rounds", 0) or 0) for t in t_in),
        },
        "expenses": {"total": round(other_expenses, 2), "count": len(e_in)},
        "salaries": {"paid": salary_expense},
        "totals": {
            "income": total_income,
            "expenses": total_expenses,
            "net_profit": net_profit,
        },
    }

# ---------------- CLIENT 360 (consolidated view) ----------------
@api.get("/clients/{cid}/summary")
async def client_summary(cid: str, _: dict = Depends(get_current_user)):
    client = await db.clients.find_one({"id": cid}, {"_id": 0})
    if not client:
        raise HTTPException(404, "Client not found")
    ledger_c = await _ledger_one("client", cid)
    client = _client_totals(client, ledger_c)

    payments = await db.payments.find({"entity": "client", "entity_id": cid}, {"_id": 0}).sort("date", -1).to_list(500)

    name = (client.get("name") or "").strip().lower()
    phone = (client.get("phone") or "").strip()

    lm_rental = await _ledger_map("rental")
    all_rentals = await db.rentals.find({}, {"_id": 0}).to_list(5000)
    rentals = [_rental_totals(r, lm_rental.get(r["id"], 0.0)) for r in all_rentals
               if (r.get("customer_name") or "").strip().lower() == name
               or r.get("client_id") == cid
               or (phone and (r.get("customer_phone") or "").strip() == phone)]

    lm_tp = await _ledger_map("transport")
    all_tp = await db.transport.find({}, {"_id": 0}).to_list(5000)
    transports = [_transport_totals(t, lm_tp.get(t["id"], 0.0)) for t in all_tp
                  if t.get("booking_id") == cid
                  or (t.get("client_name") or "").strip().lower() == name]

    totals = {
        "booking_amount": float(client.get("trek_amount", 0) or 0),
        "booking_paid": float(client.get("paid_amount", 0) or 0),
        "booking_balance": float(client.get("balance", 0) or 0),
        "rental_total": round(sum(float(r.get("total_amount", 0) or 0) for r in rentals), 2),
        "rental_paid": round(sum(float(r.get("paid_amount", 0) or 0) for r in rentals), 2),
        "transport_total": round(sum(float(t.get("total_amount", 0) or 0) for t in transports), 2),
        "transport_paid": round(sum(float(t.get("paid_amount", 0) or 0) for t in transports), 2),
    }
    totals["grand_total"] = round(totals["booking_amount"] + totals["rental_total"] + totals["transport_total"], 2)
    totals["grand_paid"] = round(totals["booking_paid"] + totals["rental_paid"] + totals["transport_paid"], 2)
    totals["grand_balance"] = round(totals["grand_total"] - totals["grand_paid"], 2)
    return {"client": client, "payments": payments, "rentals": rentals, "transports": transports, "totals": totals}

# ---------------- GUIDE DUTY ----------------
@api.get("/guides")
async def list_guides(_: dict = Depends(get_current_user)):
    """Staff filtered by role=Guide, decorated with duty stats."""
    staff = await db.staff.find({}, {"_id": 0}).to_list(2000)
    guides = [s for s in staff if (s.get("role") or "").lower() == "guide"]
    schedules = await db.trek_schedules.find({}, {"_id": 0}).to_list(5000)
    today = date.today().isoformat()
    for g in guides:
        my_treks = [s for s in schedules if s.get("assigned_guide_id") == g["id"]]
        g["total_treks"] = len(my_treks)
        g["active_treks"] = sum(1 for s in my_treks if s.get("status") in ("scheduled", "active"))
        g["completed_treks"] = sum(1 for s in my_treks if s.get("status") == "completed")
        g["total_clients"] = sum(int(s.get("total_pax", 0) or 0) for s in my_treks)
        cur = next((s for s in my_treks if s.get("status") == "active"), None) \
              or next((s for s in my_treks if s.get("status") == "scheduled" and s.get("trek_date", "") >= today), None)
        g["current_duty"] = cur
    return guides

@api.get("/guides/{gid}/duty")
async def guide_duty(gid: str, _: dict = Depends(get_current_user)):
    staff = await db.staff.find_one({"id": gid}, {"_id": 0})
    if not staff:
        raise HTTPException(404, "Guide not found")
    schedules = await db.trek_schedules.find({"assigned_guide_id": gid}, {"_id": 0}).sort("trek_date", -1).to_list(500)
    # decorate with vehicle
    vids = [s.get("assigned_vehicle_id") for s in schedules if s.get("assigned_vehicle_id")]
    vmap = {}
    if vids:
        async for v in db.vehicles.find({"id": {"$in": vids}}, {"_id": 0}):
            vmap[v["id"]] = v
    for s in schedules:
        v = vmap.get(s.get("assigned_vehicle_id"))
        s["vehicle_no"] = v.get("vehicle_no") if v else None
    salary_records = await db.salaries.find({"staff_id": gid}, {"_id": 0}).sort("month", -1).to_list(200)
    for sal in salary_records:
        _compute_salary(staff, sal)
    return {"guide": staff, "schedules": schedules, "salary_records": salary_records}

# ---------------- TRANSPORT REPORTS (breakdowns) ----------------
@api.get("/transport/reports")
async def transport_reports(_: dict = Depends(get_current_user)):
    trips = await db.transport.find({}, {"_id": 0}).to_list(10000)
    lm = await _ledger_map("transport")
    trips = [_transport_totals(t, lm.get(t["id"], 0.0)) for t in trips]

    def group_by(items, key):
        buckets = {}
        for it in items:
            k = it.get(key) or "—"
            b = buckets.setdefault(k, {"key": k, "trips": 0, "rounds": 0, "pax": 0,
                                       "total": 0.0, "paid": 0.0, "pending": 0.0})
            b["trips"] += 1
            b["rounds"] += int(it.get("rounds", 0) or 0)
            b["pax"] += int(it.get("pax", 0) or 0)
            b["total"] = round(b["total"] + float(it.get("total_amount", 0) or 0), 2)
            b["paid"] = round(b["paid"] + float(it.get("paid_amount", 0) or 0), 2)
            b["pending"] = round(b["pending"] + max(0.0, float(it.get("balance", 0) or 0)), 2)
        return sorted(buckets.values(), key=lambda x: x["total"], reverse=True)

    return {
        "totals": {
            "trips": len(trips),
            "rounds": sum(int(t.get("rounds", 0) or 0) for t in trips),
            "pax": sum(int(t.get("pax", 0) or 0) for t in trips),
            "total": round(sum(float(t.get("total_amount", 0) or 0) for t in trips), 2),
            "paid": round(sum(float(t.get("paid_amount", 0) or 0) for t in trips), 2),
            "pending": round(sum(max(0.0, float(t.get("balance", 0) or 0)) for t in trips), 2),
        },
        "by_vehicle": group_by(trips, "vehicle_no"),
        "by_route": group_by(trips, "route"),
        "by_driver": group_by(trips, "driver_name"),
        "by_transporter": group_by(trips, "transporter_name"),
    }

# ---------------- DAILY BOOKINGS (dashboard table) ----------------
@api.get("/dashboard/today-bookings")
async def today_bookings(_: dict = Depends(get_current_user)):
    today = date.today().isoformat()
    docs = await db.clients.find({}, {"_id": 0}).to_list(5000)
    lm = await _ledger_map("client")
    result = []
    for d in docs:
        created = str(d.get("created_at", ""))[:10]
        # Include if created today OR trek starts today
        if created != today and d.get("start_date") != today:
            continue
        c = _client_totals(dict(d), lm.get(d["id"], 0.0))
        # ledger split by mode
        payments = await db.payments.find({"entity": "client", "entity_id": d["id"]}, {"_id": 0}).to_list(500)
        ledger_cash = round(sum(float(p.get("amount", 0) or 0) for p in payments if p.get("mode") == "cash"), 2)
        ledger_online = round(sum(float(p.get("amount", 0) or 0) for p in payments if p.get("mode") in ("online", "upi", "card")), 2)
        initial = float(d.get("paid_amount", 0) or 0)  # raw stored value
        mode = d.get("payment_mode") or ""
        cash = ledger_cash + (initial if mode == "cash" else 0.0)
        online = ledger_online + (initial if mode in ("online", "upi", "card") else 0.0)
        result.append({
            "id": c["id"],
            "name": c.get("name"),
            "company_name": c.get("company_name") or "",
            "phone": c.get("phone"),
            "trek_name": c.get("trek_name"),
            "pax": c.get("people_count"),
            "amount": c.get("trek_amount"),
            "cash": round(cash, 2),
            "online": round(online, 2),
            "paid": c.get("paid_amount"),
            "balance": c.get("balance"),
            "status": c.get("booking_status") or "confirmed",
            "created_at": d.get("created_at"),
            "start_date": d.get("start_date"),
        })
    return result

# --- Wire up ---
app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Bavarium Maintenance Planner — BETA 0.3 (Cloud-ready)
# Streamlit single-file app + managers-only login + Neon Postgres submissions + Manager Review workflow
#
# Flow: Login → Vehicle → Intervals → History → Results (+ Settings + Manager Review)
#
# Key additions vs 0.2:
# - Saves full results + bulk_copy into template_submissions
# - Stores last_submission_id in session so Results can update bulk_copy + vehicle_notes
# - NEW: Manager Review screen (Pending / My / Approved / Denied) with Approve / Deny / Request Changes
# - Review actions write an append-only audit row to template_reviews
#
# Streamlit Secrets (TOML) expected:
#   [credentials.usernames.andrew]
#   name = "Andrew Gomes"
#   password = "changeme1"
#
#   [credentials.usernames.erin]
#   name = "Erin Gomes"
#   password = "changeme2"
#
#   [database]
#   url = "postgresql://....?sslmode=require"
#
# Run local: python -m streamlit run app.py

import json
import uuid
from datetime import date
from typing import Optional, Dict, Any, Tuple, List

import requests
import streamlit as st

# Optional DB (only used if secrets has [database].url)
try:
    import psycopg  # type: ignore
except Exception:
    psycopg = None  # allows local run without DB dependency installed


# -------------------------
# Page config
# -------------------------
st.set_page_config(page_title="Bavarium Maintenance Planner", layout="centered")


# -------------------------
# Constants / Defaults
# -------------------------
MAKES = ["BMW", "MINI", "Audi", "Porsche", "Mercedes-Benz", "Volkswagen", "Volvo"]

SERVICE_ITEMS = [
    "Engine Oil",
    "Brake Fluid",
    "Cabin Filter",
    "Engine Air Filter",
    "Coolant",
    "Spark Plugs",
    "Transmission / Transaxle",
    "Front Differential",
    "Rear Differential",
    "Transfer Case",
    "Fuel Filter",
    "Oxygen Sensor",
]

DEFAULT_INTERVALS = {
    "Engine Oil": {"miles": 5000, "years": 1},
    "Brake Fluid": {"years": 2},
    "Coolant": {"years": 4},
    "Transmission / Transaxle": {"miles": 75000, "years": 7},
    "Front Differential": {"miles": 75000, "years": 7},
    "Rear Differential": {"miles": 75000, "years": 7},
    "Transfer Case": {"miles": 75000, "years": 7},
}

AUTO_MILES_PER_YEAR = 10_000

MONTHS = ["01 Jan", "02 Feb", "03 Mar", "04 Apr", "05 May", "06 Jun",
          "07 Jul", "08 Aug", "09 Sep", "10 Oct", "11 Nov", "12 Dec"]
MONTH_LABEL_TO_NUM = {m: int(m.split()[0]) for m in MONTHS}
NUM_TO_MONTH_LABEL = {i: MONTHS[i - 1] for i in range(1, 13)}

# Managers-only for now (your request)
MANAGER_USERS = {"andrew", "erin"}


# -------------------------
# Simple Login + User Management via Streamlit Secrets
# -------------------------
def get_users_dict() -> dict:
    """
    Expects Streamlit secrets:
      [users]
      username = { name="...", password="...", role="manager|shop" }
    """
    if "users" not in st.secrets:
        st.error("Missing [users] in Streamlit Secrets.")
        st.stop()
    return dict(st.secrets["users"])


def require_login():
    users = get_users_dict()

    if "auth_ok" not in st.session_state:
        st.session_state.auth_ok = False
        st.session_state.auth_user = None
        st.session_state.auth_name = None
        st.session_state.auth_role = None

    if st.session_state.auth_ok:
        with st.sidebar:
            st.success(f"Logged in: {st.session_state.auth_name} ({st.session_state.auth_role})")
            if st.button("Logout"):
                st.session_state.auth_ok = False
                st.session_state.auth_user = None
                st.session_state.auth_name = None
                st.session_state.auth_role = None
                st.rerun()
        return

    st.title("Login")
    with st.form("login_form", clear_on_submit=False):
        username = st.text_input("Username").strip().lower()
        password = st.text_input("Password", type="password")
        submit = st.form_submit_button("Login")

    if submit:
        u = users.get(username)
        if u and password == str(u.get("password", "")):
            st.session_state.auth_ok = True
            st.session_state.auth_user = username
            st.session_state.auth_name = str(u.get("name", username))
            st.session_state.auth_role = str(u.get("role", "shop"))
            st.rerun()
        else:
            st.error("Incorrect username or password")

    st.info("Please log in to continue.")
    st.stop()


require_login()


def is_manager() -> bool:
    return (st.session_state.get("auth_role") or "") == "manager"

# -------------------------
# VIN helpers
# -------------------------
def normalize_vin(v: str) -> str:
    return (v or "").strip().upper()


def vin_year_from_10th(vin: str) -> Optional[int]:
    vin = normalize_vin(vin)
    if len(vin) < 10:
        return None

    c = vin[9]  # 10th char

    modern = {
        "A": 2010, "B": 2011, "C": 2012, "D": 2013, "E": 2014, "F": 2015, "G": 2016, "H": 2017,
        "J": 2018, "K": 2019, "L": 2020, "M": 2021, "N": 2022, "P": 2023, "R": 2024, "S": 2025,
        "T": 2026, "V": 2027, "W": 2028, "X": 2029, "Y": 2030,
        "1": 2001, "2": 2002, "3": 2003, "4": 2004, "5": 2005, "6": 2006, "7": 2007, "8": 2008, "9": 2009,
    }
    if c in modern:
        return modern[c]

    older = {
        "A": 1980, "B": 1981, "C": 1982, "D": 1983, "E": 1984, "F": 1985, "G": 1986, "H": 1987,
        "J": 1988, "K": 1989, "L": 1990, "M": 1991, "N": 1992, "P": 1993, "R": 1994, "S": 1995,
        "T": 1996, "V": 1997, "W": 1998, "X": 1999, "Y": 2000,
    }
    return older.get(c)


@st.cache_data(show_spinner=False)
def decode_vin_vpic(vin: str) -> dict:
    vin = normalize_vin(vin)
    if not vin:
        return {"ok": False, "error": "VIN is empty."}

    try:
        url = f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVin/{vin}?format=json"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        results = r.json().get("Results", [])

        def pick(var_name: str):
            for x in results:
                if x.get("Variable") == var_name:
                    return x.get("Value")
            return None

        return {
            "ok": True,
            "vin": vin,
            "year": pick("ModelYear"),
            "make": pick("Make"),
            "model": pick("Model"),
            "trim": pick("Trim"),
            "series": pick("Series"),
            "drive_type": pick("DriveType"),
            "fuel_type": pick("FuelTypePrimary"),
            "engine_cyl": pick("EngineCylinders"),
            "engine_disp_l": pick("DisplacementL"),
            "trans_style": pick("TransmissionStyle"),
            "trans_speeds": pick("TransmissionSpeeds"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -------------------------
# Date helpers
# -------------------------
def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(month=2, day=28, year=d.year + years)


def months_between(d1: date, d2: date) -> int:
    return (d2.year - d1.year) * 12 + (d2.month - d1.month)


# -------------------------
# Session State init
# -------------------------
def ss_init():
    if "step" not in st.session_state:
        st.session_state.step = "vehicle"

    if "vehicle" not in st.session_state:
        st.session_state.vehicle = {}

    if "intervals" not in st.session_state:
        st.session_state.intervals = {}

    if "history" not in st.session_state:
        st.session_state.history = {}

    if "results" not in st.session_state:
        st.session_state.results = None

    if "vin_decode" not in st.session_state:
        st.session_state.vin_decode = None

    # Due-soon thresholds
    if "due_soon_miles_default" not in st.session_state:
        st.session_state.due_soon_miles_default = 5000
    if "due_soon_months_default" not in st.session_state:
        st.session_state.due_soon_months_default = 6

    if "due_soon_miles_by_item" not in st.session_state:
        st.session_state.due_soon_miles_by_item = {i: st.session_state.due_soon_miles_default for i in SERVICE_ITEMS}
        st.session_state.due_soon_miles_by_item["Engine Oil"] = 1500
    if "due_soon_months_by_item" not in st.session_state:
        st.session_state.due_soon_months_by_item = {i: st.session_state.due_soon_months_default for i in SERVICE_ITEMS}

    # Bulk bullets (bulk copy box ONLY)
    if "bulk_bullets" not in st.session_state:
        st.session_state.bulk_bullets = {"due_now": "•", "due_soon": "?", "ok": "–", "na": "×"}

    # Last DB save message (shown on Results)
    if "last_db_save_msg" not in st.session_state:
        st.session_state.last_db_save_msg = None

    # Track last saved submission_id so Results can update it
    if "last_submission_id" not in st.session_state:
        st.session_state.last_submission_id = None


ss_init()


# -------------------------
# Interval / formatting helpers
# -------------------------
def interval_text(item: str) -> str:
    iv = st.session_state.intervals.get(item)
    if not iv:
        return "N/A"
    parts = []
    if iv.get("years") is not None:
        parts.append(f"{int(iv['years'])} yr")
    if iv.get("miles") is not None:
        parts.append(f"{int(iv['miles']):,} mi")
    return " / ".join(parts) if parts else "N/A"


def interval_phrase_short(iv: dict) -> str:
    parts = []
    if iv.get("years") is not None:
        parts.append(f"every {int(iv['years'])} yr")
    if iv.get("miles") is not None:
        parts.append(f"every {int(iv['miles']):,} mi")
    return " / ".join(parts) if parts else "interval not set"


def interval_phrase_bulk(iv: dict, item: str) -> str:
    years = iv.get("years")
    miles = iv.get("miles")

    parts = []
    if years is not None:
        parts.append(f"{int(years)} yr")
    if miles is not None:
        if item == "Engine Oil" and int(miles) % 1000 == 0 and int(miles) <= 15000:
            parts.append(f"{int(miles)//1000}K")
        else:
            parts.append(f"{int(miles):,} mi")

    if not parts:
        return "interval ?"

    if item == "Engine Oil":
        return "DUE " + " / ".join(parts)
    return "interval " + " / ".join(parts)


def fmt_last_done(hist: dict, vehicle: dict) -> str:
    if hist.get("not_equipped"):
        return "not equipped / not serviceable"

    if hist.get("known"):
        ld = hist.get("last_date")
        lm = hist.get("last_miles")
        if ld and lm is not None and int(lm) > 0:
            return f"last {ld.strftime('%m/%Y')} @ {int(lm):,} mi"
        if ld:
            return f"last {ld.strftime('%m/%Y')}"
        if lm is not None and int(lm) > 0:
            return f"last @ {int(lm):,} mi"
        return "history known (missing)"

    pd = vehicle.get("production_date")
    return f"no history (baseline {pd.strftime('%m/%Y')})" if pd else "no history (baseline unknown)"


def get_due_soon_miles(item: str) -> int:
    return int(st.session_state.due_soon_miles_by_item.get(item, st.session_state.due_soon_miles_default))


def get_due_soon_months(item: str) -> int:
    return int(st.session_state.due_soon_months_by_item.get(item, st.session_state.due_soon_months_default))


# -------------------------
# Core evaluation
# -------------------------
def evaluate_item(item: str, vehicle: dict, hist: dict) -> Tuple[str, str, str, str]:
    """
    Returns (status, concise_line, verbose_line, bulk_line_or_empty)
      status: due_now | due_soon | ok | na
    """
    iv = st.session_state.intervals.get(item)
    today = date.today()
    current_miles = int(vehicle["current_miles"])

    due_soon_miles = get_due_soon_miles(item)
    due_soon_months = get_due_soon_months(item)

    # Baselines
    if hist.get("known"):
        base_miles = hist.get("last_miles")
        base_date = hist.get("last_date")
    else:
        base_miles = 0
        base_date = vehicle.get("production_date")

    serviced_today = bool(hist.get("performed_this_visit", False))

    # N/A
    if hist.get("not_equipped"):
        line = f"{item} — not equipped / not serviceable"
        return ("na", line, line, "")

    # Missing interval
    if not iv:
        last_done = fmt_last_done(hist, vehicle)
        line = f"{item} — {last_done} — interval not set"
        return ("na", line, line, "")

    interval_phrase = interval_phrase_short(iv)
    last_done = "SCV’D TODAY" if serviced_today else fmt_last_done(hist, vehicle)

    miles_due = miles_soon = False
    time_due = time_soon = False

    next_due_miles_txt = None
    next_due_time_txt = None
    next_due_miles_verbose = None
    next_due_time_verbose = None

    # Miles evaluation
    if iv.get("miles") is not None and base_miles is not None and int(base_miles) > 0:
        due_at = int(base_miles) + int(iv["miles"])
        remaining = due_at - current_miles

        miles_due = current_miles >= due_at
        miles_soon = (not miles_due) and (remaining <= due_soon_miles)

        if remaining >= 0:
            next_due_miles_txt = f"next ~{due_at:,} mi"
            next_due_miles_verbose = f"miles due @ {due_at:,} (in {remaining:,})"
        else:
            next_due_miles_txt = f"due was {due_at:,} mi"
            next_due_miles_verbose = f"miles due @ {due_at:,} (over {abs(remaining):,})"

    # Time evaluation
    if iv.get("years") is not None and base_date is not None:
        due_date = add_years(base_date, int(iv["years"]))
        months_to_due = months_between(today, due_date)

        time_due = today >= due_date
        time_soon = (not time_due) and (months_to_due <= due_soon_months)

        if months_to_due >= 0:
            next_due_time_txt = f"next ~{due_date.strftime('%m/%Y')}"
            next_due_time_verbose = f"time due {due_date.strftime('%m/%Y')} (in ~{months_to_due} mo)"
        else:
            next_due_time_txt = f"due was {due_date.strftime('%m/%Y')}"
            next_due_time_verbose = f"time due {due_date.strftime('%m/%Y')} (over ~{abs(months_to_due)} mo)"

    # OR logic status
    if miles_due or time_due:
        status = "due_now"
    elif miles_soon or time_soon:
        status = "due_soon"
    else:
        status = "ok"

    # Build next-due strings
    concise_next_parts = []
    if next_due_miles_txt:
        concise_next_parts.append(next_due_miles_txt)
    if next_due_time_txt:
        concise_next_parts.append(next_due_time_txt)
    concise_next = " / ".join(concise_next_parts) if concise_next_parts else "next unknown"

    verbose_next_parts = []
    if next_due_miles_verbose:
        verbose_next_parts.append(next_due_miles_verbose)
    if next_due_time_verbose:
        verbose_next_parts.append(next_due_time_verbose)
    verbose_next = " • ".join(verbose_next_parts) if verbose_next_parts else "next unknown"

    concise_line = f"{item} — {last_done} — {interval_phrase} — {concise_next}"
    verbose_line = f"{item} — {last_done} — {interval_phrase} • {verbose_next}"

    # BULK COPY (tight 1 line; excludes N/A)
    bulk_bullets = st.session_state.bulk_bullets
    bullet = bulk_bullets.get(status, "•")

    status_txt = {"due_now": "DUE NOW", "due_soon": "DUE SOON", "ok": "OK"}[status]
    interval_bulk = interval_phrase_bulk(iv, item)
    history_bulk = last_done

    bulk_line = f"{bullet} {item} — {status_txt} {history_bulk} • {interval_bulk}"

    return (status, concise_line, verbose_line, bulk_line)


# -------------------------
# Interval auto-fill callbacks
# -------------------------
def on_years_change(item: str):
    auto_key = f"auto_miles_{item}"
    years_key = f"years_{item}"
    miles_key = f"miles_{item}"

    if not st.session_state.get(auto_key, True):
        return

    years = int(st.session_state.get(years_key, 0) or 0)
    if years > 0:
        st.session_state[miles_key] = years * AUTO_MILES_PER_YEAR


def on_miles_change(item: str):
    auto_key = f"auto_miles_{item}"
    years_key = f"years_{item}"
    miles_key = f"miles_{item}"

    miles = int(st.session_state.get(miles_key, 0) or 0)
    years = int(st.session_state.get(years_key, 0) or 0)
    auto_value = years * AUTO_MILES_PER_YEAR if years > 0 else 0

    if miles == 0:
        st.session_state[auto_key] = True
        return

    if years > 0 and miles != auto_value:
        st.session_state[auto_key] = False


# -------------------------
# DB helpers (Neon Postgres)
# -------------------------
def db_ready() -> bool:
    if "database" not in st.secrets or "url" not in st.secrets["database"]:
        return False
    if psycopg is None:
        return False
    return True


def db_url() -> str:
    return st.secrets["database"]["url"]


def db_exec(sql: str, params: Optional[Dict[str, Any]] = None, fetch: bool = False):
    with psycopg.connect(db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            if fetch:
                rows = cur.fetchall()
                cols = [d.name for d in cur.description] if cur.description else []
                return rows, cols
    return None


def save_submission_for_review(vehicle: dict, intervals: dict):
    """
    Saves a pending submission for manager review.
    - Runs for BOTH shop + managers
    - Requires full 17-char VIN
    """
    st.session_state.last_db_save_msg = None

    vin = (vehicle.get("vin") or "").strip().upper()
    if len(vin) != 17:
        st.session_state.last_db_save_msg = "Review NOT saved: full 17-character VIN required."
        return

    if "database" not in st.secrets or "url" not in st.secrets["database"]:
        st.session_state.last_db_save_msg = "Review NOT saved: missing [database].url in Streamlit Secrets."
        return

    if psycopg is None:
        st.session_state.last_db_save_msg = "Review NOT saved: psycopg not installed (check requirements.txt)."
        return

    db_url = st.secrets["database"]["url"]
    submission_id = str(uuid.uuid4())
    user = (st.session_state.get("auth_user") or "").strip().lower()

    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO template_submissions (
                      submission_id, created_by, vin, year, make, model,
                      engine_raw, trans_raw, intervals_proposed, manager_state
                    )
                    VALUES (
                      %(submission_id)s, %(created_by)s, %(vin)s, %(year)s, %(make)s, %(model)s,
                      %(engine_raw)s, %(trans_raw)s, %(intervals_proposed)s::jsonb, 'pending'
                    )
                    """,
                    {
                        "submission_id": submission_id,
                        "created_by": user,
                        "vin": vin,
                        "year": int(vehicle["year"]),
                        "make": str(vehicle["make"]),
                        "model": str(vehicle["model"]),
                        "engine_raw": str(vehicle.get("engine") or "").strip(),
                        "trans_raw": str(vehicle.get("trans") or "").strip(),
                        "intervals_proposed": json.dumps(intervals),
                    },
                )
        st.session_state.last_db_save_msg = "✅ Saved for manager review (pending)."
    except Exception as e:
        st.session_state.last_db_save_msg = f"❌ Review NOT saved: {type(e).__name__}: {e}"


def update_submission_content(submission_id: str, bulk_copy: str, vehicle_notes: str) -> str:
    if not db_ready():
        return "❌ DB not ready."
    if not submission_id:
        return "❌ No submission_id found."
    try:
        db_exec(
            """
            UPDATE template_submissions
            SET bulk_copy = %(bulk_copy)s,
                vehicle_notes = %(vehicle_notes)s,
                updated_at = now()
            WHERE submission_id = %(id)s
            """,
            {"id": submission_id, "bulk_copy": bulk_copy or "", "vehicle_notes": vehicle_notes or ""},
        )
        return "✅ Saved updates to submission."
    except Exception as e:
        return f"❌ Update failed: {type(e).__name__}: {e}"


def fetch_submissions_by_state(state: str, limit: int = 50):
    if not db_ready():
        return [], []
    out = db_exec(
        """
        SELECT submission_id, created_at, created_by, vin, year, make, model,
               manager_state, bulk_copy, vehicle_notes, reviewed_at, reviewed_by, review_notes
        FROM template_submissions
        WHERE manager_state = %(state)s
        ORDER BY created_at DESC
        LIMIT %(limit)s
        """,
        {"state": state, "limit": limit},
        fetch=True,
    )
    if not out:
        return [], []
    return out[0], out[1]


def fetch_my_recent_submissions(created_by: str, limit: int = 50):
    if not db_ready():
        return [], []
    out = db_exec(
        """
        SELECT submission_id, created_at, vin, year, make, model,
               manager_state, bulk_copy, vehicle_notes, reviewed_at, reviewed_by, review_notes
        FROM template_submissions
        WHERE created_by = %(u)s
        ORDER BY created_at DESC
        LIMIT %(limit)s
        """,
        {"u": created_by, "limit": limit},
        fetch=True,
    )
    if not out:
        return [], []
    return out[0], out[1]


def review_submission(submission_id: str, action: str, notes: str) -> str:
    """
    action: approve | deny | request_changes
    """
    if not db_ready():
        return "❌ DB not ready."
    reviewer = (st.session_state.get("auth_user") or "").strip().lower()

    new_state = {
        "approve": "approved",
        "deny": "denied",
        "request_changes": "changes_requested",
    }.get(action)

    if not new_state:
        return "❌ Invalid review action."

    try:
        # Snapshot for audit log
        out = db_exec(
            """
            SELECT bulk_copy, vehicle_notes, vin, year, make, model, created_by, created_at, manager_state
            FROM template_submissions
            WHERE submission_id = %(id)s
            """,
            {"id": submission_id},
            fetch=True,
        )
        snapshot = {}
        if out and out[0]:
            snapshot = dict(zip(out[1], out[0][0]))

        # Append-only review log (UUID generated in Python)
        db_exec(
            """
            INSERT INTO template_reviews (review_id, submission_id, reviewed_by, action, notes, snapshot)
            VALUES (%(rid)s, %(sid)s, %(by)s, %(action)s, %(notes)s, %(snap)s::jsonb)
            """,
            {
                "rid": str(uuid.uuid4()),
                "sid": submission_id,
                "by": reviewer,
                "action": action,
                "notes": notes or "",
                "snap": json.dumps(snapshot),
            },
            fetch=False,
        )

        # Update submission state — guard against double finalization (must still be pending)
        db_exec(
            """
            UPDATE template_submissions
            SET manager_state = %(state)s,
                reviewed_at = now(),
                reviewed_by = %(by)s,
                review_action = %(action)s,
                review_notes = %(notes)s,
                updated_at = now()
            WHERE submission_id = %(id)s
              AND manager_state = 'pending'
            """,
            {
                "state": new_state,
                "by": reviewer,
                "action": action,
                "notes": notes or "",
                "id": submission_id,
            },
            fetch=False,
        )

        return f"✅ {new_state.upper()}."
    except Exception as e:
        return f"❌ Review failed: {type(e).__name__}: {e}"


# -------------------------
# Sidebar navigation (minimal)
# -------------------------
with st.sidebar:
    st.markdown("### Bavarium Planner")
    st.caption(f"{st.session_state.get('auth_name','')}")

    if st.button("Vehicle Intake"):
        st.session_state.step = "vehicle"
        st.rerun()
    if st.button("Intervals"):
        if st.session_state.vehicle:
            st.session_state.step = "intervals"
            st.rerun()
    if st.button("History"):
        if st.session_state.vehicle:
            st.session_state.step = "history"
            st.rerun()
    if st.button("Results"):
        if st.session_state.results:
            st.session_state.step = "results"
            st.rerun()

    st.divider()
    if st.button("🧾 Manager Review"):
        st.session_state.step = "manager_review"
        st.rerun()

    if st.button("⚙️ Settings"):
        st.session_state.step = "settings"
        st.rerun()


# -------------------------
# SCREEN — Settings
# -------------------------

    elif st.session_state.step == "settings":
        st.title("Settings")
    
        if not is_manager():
            st.error("Managers only.")
            if st.button("← Back"):
                st.session_state.step = "vehicle"
                st.rerun()
            st.stop()

    # ---- settings content below this line ----
       
if st.session_state.step == "settings":
    st.title("Settings")
    st.caption("Per-service due-soon thresholds + bulk-copy bullets.")

    st.subheader("Global defaults")
    c1, c2 = st.columns(2)
    with c1:
        st.session_state.due_soon_miles_default = st.number_input(
            "Default due-soon miles (baseline)",
            min_value=0,
            max_value=50000,
            step=500,
            value=int(st.session_state.due_soon_miles_default),
        )
    with c2:
        st.session_state.due_soon_months_default = st.number_input(
            "Default due-soon months (baseline)",
            min_value=0,
            max_value=24,
            step=1,
            value=int(st.session_state.due_soon_months_default),
        )

    st.divider()
    st.subheader("Per-service due-soon thresholds")

    h1, h2, h3 = st.columns([3.0, 2.0, 2.0])
    h1.markdown("**Service**")
    h2.markdown("**Due-soon miles**")
    h3.markdown("**Due-soon months**")
    st.divider()

    for item in SERVICE_ITEMS:
        miles_key = f"ds_miles_{item}"
        months_key = f"ds_months_{item}"

        if miles_key not in st.session_state:
            st.session_state[miles_key] = int(
                st.session_state.due_soon_miles_by_item.get(item, st.session_state.due_soon_miles_default)
            )
        if months_key not in st.session_state:
            st.session_state[months_key] = int(
                st.session_state.due_soon_months_by_item.get(item, st.session_state.due_soon_months_default)
            )

        c1, c2, c3 = st.columns([3.0, 2.0, 2.0])
        c1.write(item)
        m = c2.number_input("", min_value=0, max_value=50000, step=500, label_visibility="collapsed", key=miles_key)
        mo = c3.number_input("", min_value=0, max_value=24, step=1, label_visibility="collapsed", key=months_key)

        st.session_state.due_soon_miles_by_item[item] = int(m)
        st.session_state.due_soon_months_by_item[item] = int(mo)

    st.divider()
    st.subheader("Bulk Copy bullets (bulk copy box ONLY)")
    st.caption("N/A items are excluded from bulk copy; bullets only affect the bulk copy box.")

    b1, b2, b3, b4 = st.columns(4)
    with b1:
        st.session_state.bulk_bullets["due_now"] = st.text_input(
            "Due Now bullet", value=st.session_state.bulk_bullets["due_now"], max_chars=3
        )
    with b2:
        st.session_state.bulk_bullets["due_soon"] = st.text_input(
            "Due Soon bullet", value=st.session_state.bulk_bullets["due_soon"], max_chars=3
        )
    with b3:
        st.session_state.bulk_bullets["ok"] = st.text_input(
            "OK bullet", value=st.session_state.bulk_bullets["ok"], max_chars=3
        )
    with b4:
        st.session_state.bulk_bullets["na"] = st.text_input(
            "N/A bullet", value=st.session_state.bulk_bullets["na"], max_chars=3
        )

    st.divider()
    if st.button("← Back to Vehicle"):
        st.session_state.step = "vehicle"
        st.rerun()

    st.stop()


# -------------------------
# SCREEN 1 — Vehicle Intake
# -------------------------
if st.session_state.step == "vehicle":
    st.title("Bavarium Maintenance Planner — BETA 0.3")
    st.caption("Flow: Vehicle → Intervals → History → Results (+ Review)")

 ##   if not is_manager():
 ##       st.warning("Managers-only mode is enabled. Please log in as a manager.")
 ##        st.stop()

    # Widget state mirrors vehicle state
    if "veh_year" not in st.session_state:
        st.session_state.veh_year = int(st.session_state.vehicle.get("year", 2021))
    if "veh_make" not in st.session_state:
        st.session_state.veh_make = st.session_state.vehicle.get("make", "BMW")
    if "veh_model" not in st.session_state:
        st.session_state.veh_model = st.session_state.vehicle.get("model", "")
    if "veh_miles" not in st.session_state:
        st.session_state.veh_miles = int(st.session_state.vehicle.get("current_miles", 50000))

    if "veh_engine" not in st.session_state:
        st.session_state.veh_engine = st.session_state.vehicle.get("engine", "")
    if "veh_trans" not in st.session_state:
        st.session_state.veh_trans = st.session_state.vehicle.get("trans", "")
    if "veh_drive" not in st.session_state:
        st.session_state.veh_drive = st.session_state.vehicle.get("drive", "")

    if "veh_prod_unknown" not in st.session_state:
        st.session_state.veh_prod_unknown = bool(st.session_state.vehicle.get("production_unknown", False))
    if "veh_prod_date" not in st.session_state:
        default_year = int(st.session_state.veh_year or date.today().year)
        st.session_state.veh_prod_date = st.session_state.vehicle.get("production_date") or date(default_year, 6, 1)

    vin_col1, vin_col2 = st.columns([3, 1])
    with vin_col1:
        vin_input = st.text_input("VIN (required for template saving)", value=st.session_state.vehicle.get("vin", ""))
    with vin_col2:
        decode_btn = st.button("Decode VIN 🔎")

    if decode_btn:
        v = normalize_vin(vin_input)
        if len(v) < 11:
            st.error("VIN looks too short. Please enter a full VIN.")
        else:
            decoded = decode_vin_vpic(v)
            st.session_state.vin_decode = decoded

            if decoded.get("ok"):
                year_val = None
                y = decoded.get("year")
                if y and str(y).isdigit():
                    year_val = int(y)
                else:
                    year_val = vin_year_from_10th(v)

                make_val = (decoded.get("make") or "").strip() or st.session_state.veh_make
                model_val = (decoded.get("model") or "").strip() or st.session_state.veh_model

                if year_val:
                    st.session_state.veh_year = int(year_val)
                    st.session_state.veh_prod_date = date(int(year_val), 6, 1)

                if make_val in MAKES:
                    st.session_state.veh_make = make_val

                st.session_state.veh_model = model_val

                cyl = decoded.get("engine_cyl")
                disp = decoded.get("engine_disp_l")
                if cyl or disp:
                    st.session_state.veh_engine = f"{cyl or '—'} cyl, {disp or '—'} L"

                trans_style = decoded.get("trans_style")
                trans_speeds = decoded.get("trans_speeds")
                if trans_style or trans_speeds:
                    st.session_state.veh_trans = f"{trans_style or '—'} {trans_speeds or ''}".strip()

                drive = decoded.get("drive_type")
                if drive:
                    st.session_state.veh_drive = drive

                st.success("VIN decoded. Fields updated below (review + adjust if needed).")
                st.rerun()
            else:
                st.error(f"VIN decode failed: {decoded.get('error', 'Unknown error')}")

    if st.session_state.vin_decode and st.session_state.vin_decode.get("ok"):
        d = st.session_state.vin_decode
        fallback_year = vin_year_from_10th(d.get("vin", ""))
        shown_year = d.get("year") or (str(fallback_year) if fallback_year else "—")

        with st.expander("VIN Decode Details (NHTSA vPIC)", expanded=False):
            left, right = st.columns(2)
            with left:
                st.write(f"**VIN:** {d.get('vin', '')}")
                st.write(f"**Year:** {shown_year}")
                st.write(f"**Make:** {d.get('make') or '—'}")
                st.write(f"**Model:** {d.get('model') or '—'}")
                st.write(f"**Trim/Series:** {(d.get('trim') or d.get('series') or '—')}")
            with right:
                cyl = d.get("engine_cyl") or "—"
                disp = d.get("engine_disp_l") or "—"
                st.write(f"**Engine:** {cyl} cyl, {disp} L")
                st.write(f"**Trans:** {d.get('trans_style') or '—'}")
                st.write(f"**Drive:** {d.get('drive_type') or '—'}")
                st.write(f"**Fuel:** {d.get('fuel_type') or '—'}")

    with st.expander("Powertrain (editable)", expanded=True):
        e1, e2, e3 = st.columns(3)
        with e1:
            st.text_input("Engine", key="veh_engine", placeholder="e.g. B58 / N55 / M274.920")
        with e2:
            st.text_input("Transmission", key="veh_trans", placeholder="e.g. ZF 8HP / Aisin / PDK")
        with e3:
            st.text_input("Drive", key="veh_drive", placeholder="RWD / AWD / FWD")

    with st.form("vehicle_form"):
        col1, col2 = st.columns(2)
        with col1:
            st.number_input("Year", min_value=1980, max_value=2035, step=1, key="veh_year")
        with col2:
            st.number_input("Current Mileage", min_value=0, max_value=500000, step=1000, key="veh_miles")

        col3, col4 = st.columns(2)
        with col3:
            st.text_input("Model (required)", key="veh_model")
        with col4:
            st.selectbox("Make", MAKES, key="veh_make")

        st.checkbox("Production date unknown", key="veh_prod_unknown")

        if not st.session_state.veh_prod_unknown:
            st.date_input("Production date (baseline for time-based services)", key="veh_prod_date")
            st.caption("NHTSA VIN decode does not provide build date. This is an estimate — adjust if known.")

        submitted = st.form_submit_button("Continue →")

    if submitted:
        if not (st.session_state.veh_model or "").strip():
            st.error("Model is required.")
            st.stop()

        prod_date = None if st.session_state.veh_prod_unknown else st.session_state.veh_prod_date

        st.session_state.vehicle = {
            "vin": normalize_vin(vin_input),
            "year": int(st.session_state.veh_year),
            "make": st.session_state.veh_make,
            "model": (st.session_state.veh_model or "").strip(),
            "current_miles": int(st.session_state.veh_miles),
            "production_date": prod_date,
            "production_unknown": bool(st.session_state.veh_prod_unknown),
            "engine": (st.session_state.veh_engine or "").strip(),
            "trans": (st.session_state.veh_trans or "").strip(),
            "drive": (st.session_state.veh_drive or "").strip(),
        }

        st.session_state.intervals = {k: dict(v) for k, v in DEFAULT_INTERVALS.items()}

        st.session_state.history = {
            item: {
                "known": True,
                "last_miles": None,
                "last_date": None,
                "not_equipped": False,
                "performed_this_visit": False,
            }
            for item in SERVICE_ITEMS
        }

        st.session_state.results = None
        st.session_state.vin_decode = None
        st.session_state.last_db_save_msg = None
        st.session_state.last_submission_id = None
        st.session_state.pop("edited_bulk_copy", None)
        st.session_state.pop("edited_vehicle_notes", None)

        # Clear interval/history widgets for fresh workflow
        for item in SERVICE_ITEMS:
            for k in [
                f"use_{item}", f"years_{item}", f"miles_{item}", f"auto_miles_{item}",
                f"{item}_known", f"{item}_hist_miles", f"{item}_hist_month", f"{item}_hist_year",
                f"{item}_ne", f"{item}_ptv"
            ]:
                st.session_state.pop(k, None)

        st.session_state.step = "intervals"
        st.rerun()


# -------------------------
# SCREEN 2 — Intervals
# -------------------------
elif st.session_state.step == "intervals":
    v = st.session_state.vehicle
    st.title("Intervals (This Vehicle)")
    st.caption(f"{v['year']} {v['make']} {v['model']} • {v['current_miles']:,} miles")

    st.info(
        "Edit intervals for this visit. One Use checkbox per line: unchecked = (N/A).\n\n"
        "Auto-miles rule: when Years is set, Miles auto-fills as Years × 10,000 (overrideable). "
        "To re-enable auto after overriding, set Miles back to 0."
    )

    st.divider()

    h1, h2, h3, h4 = st.columns([3.0, 1.0, 2.0, 2.0])
    h1.markdown("**Service Item**")
    h2.markdown("**Use**")
    h3.markdown("**Years**")
    h4.markdown("**Miles**")

    st.divider()

    for item in SERVICE_ITEMS:
        current = st.session_state.intervals.get(item, {})
        default_years = int(current.get("years") or 0)
        default_miles = int(current.get("miles") or 0)
        use_default = (default_years > 0) or (default_miles > 0)

        use_key = f"use_{item}"
        years_key = f"years_{item}"
        miles_key = f"miles_{item}"
        auto_key = f"auto_miles_{item}"

        if use_key not in st.session_state:
            st.session_state[use_key] = use_default
        if years_key not in st.session_state:
            st.session_state[years_key] = default_years
        if miles_key not in st.session_state:
            st.session_state[miles_key] = default_miles
        if auto_key not in st.session_state:
            st.session_state[auto_key] = True

        c1, c2, c3, c4 = st.columns([3.0, 1.0, 2.0, 2.0])
        c1.write(item)
        use_item = c2.checkbox("", key=use_key)

        c3.number_input(
            "",
            min_value=0,
            max_value=30,
            step=1,
            disabled=not use_item,
            label_visibility="collapsed",
            key=years_key,
            on_change=on_years_change,
            args=(item,),
        )

        c4.number_input(
            "",
            min_value=0,
            max_value=300000,
            step=1000,
            disabled=not use_item,
            label_visibility="collapsed",
            key=miles_key,
            on_change=on_miles_change,
            args=(item,),
        )

        if not use_item:
            st.session_state.intervals.pop(item, None)
            continue

        years = int(st.session_state.get(years_key, 0) or 0)
        miles = int(st.session_state.get(miles_key, 0) or 0)

        new_iv = {}
        if years > 0:
            new_iv["years"] = years
        if miles > 0:
            new_iv["miles"] = miles

        if new_iv:
            st.session_state.intervals[item] = new_iv
        else:
            st.session_state.intervals.pop(item, None)

    st.divider()
    colA, colB = st.columns(2)
    with colA:
        if st.button("← Back to Vehicle"):
            st.session_state.step = "vehicle"
            st.rerun()
    with colB:
        if st.button("Continue → Service History"):
            st.session_state.step = "history"
            st.rerun()


# -------------------------
# SCREEN 3 — Service History
# -------------------------
elif st.session_state.step == "history":
    v = st.session_state.vehicle
    st.title("Service History")
    st.caption(f"{v['year']} {v['make']} {v['model']} • {v['current_miles']:,} miles")

    st.info(
        "For each item: select Known or No history. Use Not Equipped for non-serviceable components.\n\n"
        "Dates are Month/Year (no day selection). Months are numbered for speed."
    )

    current_year = date.today().year
    year_options = list(range(current_year, 1990, -1))

    for item in SERVICE_ITEMS:
        data = st.session_state.history[item]

        with st.expander(f"{item}  —  Interval: {interval_text(item)}", expanded=False):
            col1, col2, col3, col4 = st.columns([1.1, 1.1, 1.4, 1.1])

            with col1:
                known_choice = st.radio(
                    "History",
                    ["Known", "No history"],
                    index=0 if data["known"] else 1,
                    key=f"{item}_known",
                )
                data["known"] = (known_choice == "Known")

            with col2:
                data["last_miles"] = st.number_input(
                    "Last mileage",
                    min_value=0,
                    max_value=500000,
                    step=1000,
                    value=int(data["last_miles"] or 0),
                    disabled=not data["known"],
                    key=f"{item}_hist_miles",
                )

            with col3:
                default_base = data.get("last_date") or v.get("production_date") or date.today()
                default_month_label = NUM_TO_MONTH_LABEL[default_base.month]
                default_year = default_base.year

                m_key = f"{item}_hist_month"
                y_key = f"{item}_hist_year"

                if m_key not in st.session_state:
                    st.session_state[m_key] = default_month_label
                if y_key not in st.session_state:
                    st.session_state[y_key] = default_year if default_year in year_options else year_options[0]

                mcol, ycol = st.columns([1, 1])
                with mcol:
                    month_label = st.selectbox(
                        "Month",
                        MONTHS,
                        index=MONTHS.index(st.session_state[m_key]),
                        disabled=not data["known"],
                        key=m_key,
                    )
                with ycol:
                    year_ = st.selectbox(
                        "Year",
                        year_options,
                        index=year_options.index(st.session_state[y_key]) if st.session_state[y_key] in year_options else 0,
                        disabled=not data["known"],
                        key=y_key,
                    )

                if data["known"]:
                    data["last_date"] = date(int(year_), int(MONTH_LABEL_TO_NUM[month_label]), 1)
                else:
                    data["last_date"] = None

            with col4:
                data["performed_this_visit"] = st.checkbox(
                    "SCV’D TODAY",
                    value=bool(data.get("performed_this_visit", False)),
                    key=f"{item}_ptv",
                )

            data["not_equipped"] = st.checkbox(
                "Not equipped / not serviceable",
                value=data["not_equipped"],
                key=f"{item}_ne",
            )

            if not data["known"]:
                data["last_miles"] = None
                data["last_date"] = None

    colA, colB, colC = st.columns(3)
    with colA:
        if st.button("← Back"):
            st.session_state.step = "intervals"
            st.rerun()
    with colB:
        if st.button("Edit Intervals"):
            st.session_state.step = "intervals"
            st.rerun()
    with colC:
        if st.button("Calculate Results →"):
            due_now, due_soon, ok, na = [], [], [], []
            bulk_lines = []

            for item in SERVICE_ITEMS:
                status, concise, verbose, bulk_line = evaluate_item(item, v, st.session_state.history[item])
                payload = {"item": item, "concise": concise, "verbose": verbose, "bulk": bulk_line}

                if status == "due_now":
                    due_now.append(payload)
                elif status == "due_soon":
                    due_soon.append(payload)
                elif status == "ok":
                    ok.append(payload)
                else:
                    na.append(payload)

                if bulk_line:
                    bulk_lines.append(bulk_line)

            st.session_state.results = {
                "due_now": due_now,
                "due_soon": due_soon,
                "ok": ok,
                "na": na,
                "bulk_lines": bulk_lines,
            }

    # Save submission to Neon (shop + managers → pending review, full VIN required)
    save_submission_for_review(v, st.session_state.intervals)
    
    st.session_state.step = "results"
    st.rerun()


# -------------------------
# SCREEN 4 — Results
# -------------------------
elif st.session_state.step == "results":
    v = st.session_state.vehicle
    r = st.session_state.results or {"due_now": [], "due_soon": [], "ok": [], "na": [], "bulk_lines": []}

    st.title("Results")
    st.caption(f"{v['year']} {v['make']} {v['model']} • {v['current_miles']:,} miles")

    # Show DB save result clearly (managers-only)
    if is_manager() and st.session_state.last_db_save_msg:
        msg = st.session_state.last_db_save_msg
        if msg.startswith("✅"):
            st.success(msg)
        elif msg.startswith("❌"):
            st.error(msg)
        else:
            st.warning(msg)

    top1, top2 = st.columns([2, 1])
    with top1:
        st.info("📋 Copying to DVI/RO: select the lines you want and press CTRL + C to copy.")
    with top2:
        verbose_mode = st.checkbox("Verbose details", value=False)

    def pick_line(x: dict) -> str:
        return x["verbose"] if verbose_mode else x["concise"]

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("🔴 Due Now")
        if r["due_now"]:
            for x in r["due_now"]:
                st.write(f"- {pick_line(x)}")
        else:
            st.write("_None_")

    with col2:
        st.subheader("🟡 Due Soon")
        if r["due_soon"]:
            for x in r["due_soon"]:
                st.write(f"- {pick_line(x)}")
        else:
            st.write("_None_")

    st.subheader("🟢 Not Due (history + next due shown)")
    if r["ok"]:
        for x in r["ok"]:
            st.write(f"- {pick_line(x)}")
    else:
        st.write("_None_")

    st.subheader("⚪ N/A / Needs Interval (still shown for planning)")
    if r["na"]:
        for x in r["na"]:
            st.write(f"- {pick_line(x)}")
    else:
        st.write("_None_")

    st.divider()
    st.subheader("Bulk Copy Box (Customer/RO — tight, 1 line each)")
    st.caption("Note: N/A items are automatically excluded from this box.")

    default_bulk = "\n".join(r.get("bulk_lines", []))
    edited_bulk = st.text_area("All Lines", value=default_bulk, height=260, key="edited_bulk_copy")

    st.subheader("Vehicle Notes (internal / dealer history summary)")
    st.caption("Optional: anything you want Erin/Andy to see during review.")
    notes = st.text_area("Notes", value=st.session_state.get("edited_vehicle_notes", ""), height=120, key="edited_vehicle_notes")

    if is_manager() and st.session_state.get("last_submission_id"):
        if st.button("💾 Update Saved Submission"):
            msg = update_submission_content(st.session_state.last_submission_id, edited_bulk, notes)
            (st.success if msg.startswith("✅") else st.error)(msg)
    elif is_manager():
        st.caption("No saved submission_id found for this run (did you have a full VIN?).")

    colA, colB = st.columns(2)
    with colA:
        if st.button("← Back to History"):
            st.session_state.step = "history"
            st.rerun()

    with colB:
        if st.button("Start New Vehicle"):
            st.session_state.step = "vehicle"
            st.session_state.vehicle = {}
            st.session_state.intervals = {}
            st.session_state.history = {}
            st.session_state.results = None
            st.session_state.vin_decode = None
            st.session_state.last_db_save_msg = None
            st.session_state.last_submission_id = None

            for k in ["veh_year", "veh_make", "veh_model", "veh_miles", "veh_engine", "veh_trans", "veh_drive", "veh_prod_unknown", "veh_prod_date"]:
                st.session_state.pop(k, None)

            st.session_state.pop("edited_bulk_copy", None)
            st.session_state.pop("edited_vehicle_notes", None)

            st.rerun()


# -------------------------
# SCREEN — Manager Review
# -------------------------
elif st.session_state.step == "manager_review":
    st.title("Manager Review")
    st.caption("Erin ↔ Andy review queue. Approve / Deny / Request Changes.")

    if not is_manager():
        st.warning("Managers-only.")
        st.stop()

    if not db_ready():
        st.error("DB not ready. Check Streamlit Secrets [database].url and psycopg dependency.")
        st.stop()

    me = (st.session_state.get("auth_user") or "").strip().lower()

    tab1, tab2, tab3, tab4 = st.tabs(["Pending Queue", "My Submissions", "Approved", "Denied"])

    def render_cards(rows: List[tuple], cols: List[str], allow_actions: bool):
        if not rows:
            st.write("_None_")
            return

        for row in rows:
            d = dict(zip(cols, row))
            sid = str(d.get("submission_id"))
            header = f"{d.get('year')} {d.get('make')} {d.get('model')} • VIN {d.get('vin')}"

            created_at = d.get("created_at")
            state = d.get("manager_state")
            subtitle = f"{state} — {created_at}" if created_at else f"{state}"

            with st.expander(f"{header}  —  {subtitle}", expanded=False):
                st.write(f"**Created by:** {d.get('created_by')}")
                if d.get("reviewed_by"):
                    st.write(f"**Reviewed by:** {d.get('reviewed_by')} @ {d.get('reviewed_at')}")
                if d.get("review_notes"):
                    st.write(f"**Review notes:** {d.get('review_notes')}")

                st.subheader("Bulk Copy")
                st.text_area("bulk_copy", value=d.get("bulk_copy") or "", height=160, key=f"bulk_{sid}", disabled=True)

                st.subheader("Vehicle Notes")
                st.text_area("vehicle_notes", value=d.get("vehicle_notes") or "", height=120, key=f"notes_{sid}", disabled=True)

                if allow_actions:
                    st.divider()
                    st.caption("Review action (only works if still Pending).")

                    review_notes = st.text_input("Notes / Reason", value="", key=f"rn_{sid}")
                    c1, c2, c3 = st.columns(3)

                    with c1:
                        if st.button("✅ Approve", key=f"ap_{sid}"):
                            msg = review_submission(sid, "approve", review_notes)
                            (st.success if msg.startswith("✅") else st.error)(msg)
                            st.rerun()
                    with c2:
                        if st.button("🔁 Request Changes", key=f"rc_{sid}"):
                            msg = review_submission(sid, "request_changes", review_notes)
                            (st.success if msg.startswith("✅") else st.error)(msg)
                            st.rerun()
                    with c3:
                        if st.button("❌ Deny", key=f"dn_{sid}"):
                            msg = review_submission(sid, "deny", review_notes)
                            (st.success if msg.startswith("✅") else st.error)(msg)
                            st.rerun()

    with tab1:
        rows, cols = fetch_submissions_by_state("pending", limit=50)
        render_cards(rows, cols, allow_actions=True)

    with tab2:
        rows, cols = fetch_my_recent_submissions(me, limit=50)
        render_cards(rows, cols, allow_actions=False)

    with tab3:
        rows, cols = fetch_submissions_by_state("approved", limit=50)
        render_cards(rows, cols, allow_actions=False)

    with tab4:
        rows, cols = fetch_submissions_by_state("denied", limit=50)
        render_cards(rows, cols, allow_actions=False)

    st.divider()
    if st.button("← Back to Results"):
        st.session_state.step = "results" if st.session_state.results else "vehicle"
        st.rerun()


# Footer
st.caption("Bavarium Maintenance Planner — BETA 0.3")











#!/usr/bin/env python3

import os, json, uuid, time, logging, asyncio, re
from datetime import datetime, timezone, timedelta
import requests, certifi

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler,
    TypeHandler, filters, ContextTypes,
)
from telegram.constants import ParseMode

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s │ %(levelname)s │ %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
# NOTE: no hardcoded fallback secrets here on purpose — this file is meant to
# be safe to push to a (even public) GitHub repo. All secrets must be set as
# environment variables on whatever host runs this (Railway "Variables" tab,
# systemd EnvironmentFile, .env + python-dotenv locally, etc).
BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
SB_URL     = os.getenv("SUPABASE_URL", "")
SB_ANON    = os.getenv("SUPABASE_ANON_KEY", "")
SB_SVC     = os.getenv("SUPABASE_SERVICE_KEY", "")

_missing = [n for n, v in [
    ("BOT_TOKEN", BOT_TOKEN), ("SUPABASE_URL", SB_URL),
    ("SUPABASE_ANON_KEY", SB_ANON), ("SUPABASE_SERVICE_KEY", SB_SVC),
] if not v]
if _missing:
    raise SystemExit(
        f"❌ Missing required environment variable(s): {', '.join(_missing)}\n"
        f"   Set them in your host's Variables/Secrets panel before starting the bot."
    )

AF_BASE    = "https://api2.appsflyer.com/inappevent"
BDT        = timedelta(hours=6)
ADMIN_CONTACT = "@whoiam31"
MAX_SCHED_RETRIES  = 3     # how many times a due scheduled task retries on failure before marked "error"
SCHED_POLL_SECONDS = 30    # how often the background scheduler checks for due tasks

# ─── Conversation states ───────────────────────────────────────────────────────
(
    ST_GUEST,
    ST_EMAIL, ST_PASS,
    ST_MAIN,
    ST_GAME_ACTION,
    ST_SEND_TYPE, ST_TASK_PICK, ST_TASK_NUM, ST_UID_PICK,
    ST_PURCH_EVENT, ST_PURCH_AMT, ST_PURCH_CUR, ST_PURCH_PID,
    ST_SCHED_TASK, ST_SCHED_NUM, ST_SCHED_UID, ST_SCHED_DATE, ST_SCHED_TIME,
    ST_ADMIN_MAIN, ST_ADMIN_USER_PICK,
    ST_GAME_SEARCH,
    ST_ADD_UID_NAME, ST_ADD_UID_VALUE, ST_ADD_UID_GAID,
    ST_ADMIN_BAN,
    ST_ADMIN_BROADCAST,
    ST_ADMIN_USER_SEARCH,
    ST_SCHED_EDIT_PICK, ST_SCHED_EDIT_DATE, ST_SCHED_EDIT_TIME,
    ST_ADMIN_COPY_GAME_SRC, ST_ADMIN_COPY_GAME_PICK, ST_ADMIN_COPY_GAME_DST,
    ST_ADMIN_MANAGE_GAMES_USER, ST_ADMIN_MANAGE_GAMES_ACT,
    ST_EXPLORE_SEARCH,
    ST_ADMIN_COPY_GAME_SEARCH, ST_ADMIN_COPY_GAME_MODE,
    ST_ADMIN_MG_SEARCH,
    ST_ADMIN_MG_EDIT, ST_ADMIN_MG_LIMIT, ST_ADMIN_MG_DATE, ST_ADMIN_MG_TASK_ADD,
    ST_ADMIN_MG_TASK_EDIT,
    ST_ADDGAME_EMAIL, ST_ADDGAME_DETAILS,
    ST_ADDUSER_EMAIL, ST_ADDUSER_PASSWORD,
    # ── Scheduled purchase states ────────────────────────────────────────────
    ST_SCHED_PURCH_GAME, ST_SCHED_PURCH_UID,
    ST_SCHED_PURCH_AMT,
    ST_SCHED_PURCH_DATE, ST_SCHED_PURCH_TIME,
    ST_SCHED_PURCH_SEARCH,   # game-search within purchase schedule flow
    ST_SCHED_PURCH_SAME,     # repeat purchase for same game (only asks amount+date+time)
) = range(55)


# ═══════════════════════════════════════════════════════════════════════════════
#  Supabase HTTP client
# ═══════════════════════════════════════════════════════════════════════════════
class _Sb:
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.key = key
        self._s  = requests.Session()
        self._s.verify = certifi.where()
        self._s.headers.update({
            "apikey":        key,
            "Authorization": f"Bearer {key}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        })

    def _h(self, token=None):
        """Headers using the anon apikey, optionally overriding the
        Authorization bearer with a user's access token."""
        h = dict(self._s.headers)
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def _h_svc(self):
        """Headers for SERVICE-ROLE requests (admin operations).
        IMPORTANT: both `apikey` and `Authorization` must be the service
        key — Supabase's gateway checks `apikey` independently of the
        bearer token, so setting only Authorization silently gets you
        anon-level access (empty results due to RLS), which is what was
        causing '0 users' and empty Explore Games."""
        h = dict(self._s.headers)
        h["apikey"]        = SB_SVC
        h["Authorization"] = f"Bearer {SB_SVC}"
        return h

    def login(self, email, password):
        r = self._s.post(
            f"{self.url}/auth/v1/token?grant_type=password",
            headers={"apikey": self.key, "Content-Type": "application/json"},
            json={"email": email, "password": password},
            timeout=15,
        )
        if not r.ok:
            raise Exception(r.json().get("error_description", r.text))
        d = r.json()
        return d["access_token"], d.get("refresh_token", ""), d.get("expires_in", 3600), d["user"]["id"]

    def refresh_session(self, refresh_token):
        """Exchange a refresh_token for a new access_token.

        FIX: Supabase access tokens expire (default ~1 hour). Previously the
        bot obtained a token once at login and used it forever. Every
        request made after expiry silently returned 401 and was swallowed
        (no error checking anywhere), so writes stopped persisting and
        reads silently fell back to old cached data — with the bot still
        reporting success. This method lets the bot transparently mint a
        fresh access_token before that happens.
        Returns (access_token, refresh_token, expires_in). Raises on failure
        (e.g. refresh_token itself expired/revoked — caller must ask the
        user to /start and log in again)."""
        r = self._s.post(
            f"{self.url}/auth/v1/token?grant_type=refresh_token",
            headers={"apikey": self.key, "Content-Type": "application/json"},
            json={"refresh_token": refresh_token},
            timeout=15,
        )
        if not r.ok:
            raise Exception(r.json().get("error_description", r.text))
        d = r.json()
        return d["access_token"], d.get("refresh_token", refresh_token), d.get("expires_in", 3600)

    def get_user_record(self, uid, token):
        r = self._s.get(
            f"{self.url}/rest/v1/users",
            headers=self._h(token),
            params={"select": "*", "id": f"eq.{uid}"},
            timeout=10,
        )
        rows = r.json() if r.ok else []
        return rows[0] if rows else None

    def save_games(self, uid, token, games_dict):
        self._s.patch(
            f"{self.url}/rest/v1/users",
            headers={**self._h(token), "Prefer": "return=minimal"},
            params={"id": f"eq.{uid}"},
            json={"games": json.dumps(games_dict)},
            timeout=10,
        )

    def save_scheduled(self, uid, token, tasks_list):
        self._s.patch(
            f"{self.url}/rest/v1/users",
            headers={**self._h(token), "Prefer": "return=minimal"},
            params={"id": f"eq.{uid}"},
            json={"scheduled_tasks": json.dumps(tasks_list)},
            timeout=10,
        )

    def set_telegram_id(self, uid, token, telegram_id):
        self._s.patch(
            f"{self.url}/rest/v1/users",
            headers={**self._h(token), "Prefer": "return=minimal"},
            params={"id": f"eq.{uid}"},
            json={"telegram_id": telegram_id},
            timeout=10,
        )

    def admin_create_user(self, email, password):
        """Create a brand-new auth account + matching users-table row.
        Mirrors the desktop app's `self.svc.auth.admin.create_user(...)`
        followed by an insert into the `users` table. SERVICE ROLE only —
        this hits Supabase's GoTrue Admin API directly since there's no
        Python SDK here, just raw requests."""
        r = self._s.post(
            f"{self.url}/auth/v1/admin/users",
            headers=self._h_svc(),
            json={"email": email, "password": password,
                  "email_confirm": True, "user_metadata": {}},
            timeout=15,
        )
        if not r.ok:
            try:
                msg = r.json().get("msg") or r.json().get("error_description") or r.text
            except Exception:
                msg = r.text
            if "already" in msg.lower() or r.status_code == 422:
                raise Exception("This email is already registered.")
            raise Exception(msg)
        uid = r.json()["id"]

        r2 = self._s.post(
            f"{self.url}/rest/v1/users",
            headers={**self._h_svc(), "Prefer": "return=minimal"},
            json={
                "id": uid, "email": email, "games": "{}",
                "deleted_games": "[]", "device_id": None, "banned": False,
                "is_admin": False, "last_active": None,
                "default_product_id": "emerald_pack_01",
            },
            timeout=15,
        )
        if not r2.ok:
            raise Exception(f"Auth user created but DB row failed: {r2.text}")
        return uid

    # ── Admin: list all users (SERVICE ROLE — fixed headers) ─────────────────
    def admin_list_users(self):
        r = self._s.get(
            f"{self.url}/rest/v1/users",
            headers=self._h_svc(),
            params={"select": "id,email,is_admin,banned,games,telegram_id,scheduled_tasks",
                    "order": "email"},
            timeout=15,
        )
        if not r.ok:
            log.warning("admin_list_users failed: %s %s", r.status_code, r.text[:200])
            return []
        return r.json()

    def admin_update_user(self, uid, data):
        self._s.patch(
            f"{self.url}/rest/v1/users",
            headers={**self._h_svc(), "Prefer": "return=minimal"},
            params={"id": f"eq.{uid}"},
            json=data,
            timeout=10,
        )

    def admin_get_user_by_email(self, email):
        r = self._s.get(
            f"{self.url}/rest/v1/users",
            headers=self._h_svc(),
            params={"select": "*", "email": f"ilike.*{email}*"},
            timeout=10,
        )
        return r.json() if r.ok else []

    def admin_get_user_by_id(self, uid):
        r = self._s.get(
            f"{self.url}/rest/v1/users",
            headers=self._h_svc(),
            params={"select": "*", "id": f"eq.{uid}"},
            timeout=10,
        )
        rows = r.json() if r.ok else []
        return rows[0] if rows else None

    def admin_save_games_for_uid(self, uid, games_dict):
        """Admin saves games directly to any user by uid (service key)."""
        self._s.patch(
            f"{self.url}/rest/v1/users",
            headers={**self._h_svc(), "Prefer": "return=minimal"},
            params={"id": f"eq.{uid}"},
            json={"games": json.dumps(games_dict)},
            timeout=10,
        )

    # ── Get admin user's games (for explore) — SERVICE ROLE fixed headers ────
    def get_admin_games(self):
        """Returns the games dict from the first admin user in DB."""
        r = self._s.get(
            f"{self.url}/rest/v1/users",
            headers=self._h_svc(),
            params={"select": "games", "is_admin": "eq.true", "limit": 1},
            timeout=10,
        )
        if not r.ok:
            log.warning("get_admin_games failed: %s %s", r.status_code, r.text[:200])
            return {}
        rows = r.json()
        if not rows:
            return {}
        raw = rows[0].get("games", {})
        return _pj(raw, {})


SB = _Sb(SB_URL, SB_ANON)


# ═══════════════════════════════════════════════════════════════════════════════
#  AppsFlyer helpers
# ═══════════════════════════════════════════════════════════════════════════════
def _af_time_android():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _af_time_ios():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

def send_task_event(dev_key, app_id, af_uid, gaid, event_name, platform="android"):
    url     = f"{AF_BASE}/{app_id}"
    headers = {"authentication": dev_key, "Content-Type": "application/json"}
    plat    = (platform or "android").lower()
    if plat == "ios":
        payload = {
            "appsflyer_id": af_uid, "idfa": gaid,
            "eventName": event_name, "eventValue": "{}",
            "eventTime": _af_time_ios(),
        }
    else:
        payload = {
            "appsflyer_id": af_uid, "advertising_id": gaid,
            "eventName": event_name, "eventValue": "{}",
            "app_id": app_id, "eventTime": _af_time_android(),
        }
    r = requests.post(url, headers=headers, json=payload, timeout=10)
    return r.status_code, r.text

def send_purchase_event(dev_key, app_id, af_uid, gaid, amount, currency,
                         product_id, event_name, platform="android"):
    url     = f"{AF_BASE}/{app_id}"
    headers = {"authentication": dev_key, "Content-Type": "application/json"}
    plat    = (platform or "android").lower()
    if plat == "ios":
        ev_val = json.dumps({
            "af_revenue": f"{amount:.2f}", "af_currency": currency,
            **({} if not product_id else {"af_content_id": product_id}),
        })
        payload = {
            "appsflyer_id": af_uid, "idfa": gaid,
            "eventName": event_name, "eventValue": ev_val,
            "eventTime": _af_time_ios(),
        }
    else:
        order   = f"GPA.{uuid.uuid4().hex[:16].upper()}"
        receipt = {
            "orderId": order, "packageName": app_id,
            "productId": product_id, "purchaseTime": int(time.time() * 1000),
            "purchaseState": 0, "purchaseToken": uuid.uuid4().hex,
            "quantity": 1, "acknowledged": True,
        }
        ev_val = json.dumps({
            "af_revenue": f"{amount:.2f}", "af_currency": currency,
            "af_quantity": "1", "af_content_id": product_id,
            "af_receipt_id": order, "af_receipt_data": json.dumps(receipt),
        })
        payload = {
            "appsflyer_id": af_uid, "advertising_id": gaid,
            "eventName": event_name, "eventValue": ev_val,
            "eventCurrency": currency, "app_id": app_id,
            "eventTime": _af_time_android(),
        }
    r = requests.post(url, headers=headers, json=payload, timeout=10)
    return r.status_code, r.text


# ═══════════════════════════════════════════════════════════════════════════════
#  Session / helper utilities
# ═══════════════════════════════════════════════════════════════════════════════
def sess(ctx) -> dict:
    return ctx.user_data

def is_logged_in(ctx) -> bool:
    return bool(sess(ctx).get("token"))

def is_admin(ctx) -> bool:
    return sess(ctx).get("is_admin", False)

def get_games(ctx) -> dict:
    raw = sess(ctx).get("user_record", {}).get("games", {})
    if isinstance(raw, str):
        try: return json.loads(raw)
        except: return {}
    return raw or {}

def bd_now():
    return datetime.now(timezone.utc) + BDT

def get_reset_window_start():
    now_bd = bd_now()
    reset_today = now_bd.replace(hour=16, minute=0, second=0, microsecond=0)
    if now_bd >= reset_today:
        return reset_today
    return reset_today - timedelta(days=1)

def id_label(g: dict) -> str:
    plat = (g.get("platform") or "android").lower()
    return "IDFA" if plat == "ios" else "GAID"

def _pj(raw, default):
    if not raw: return default
    if isinstance(raw, (dict, list)): return raw
    try: return json.loads(raw)
    except: return default

def _natural_key(s):
    """Sort key that orders embedded numbers numerically, so
    'completed village 2' comes before 'completed village 10'
    (plain string sort would put '10' right after '1')."""
    return [int(c) if c.isdigit() else c.lower()
            for c in re.split(r'(\d+)', s or "")]

def _task_sort_key(t):
    """Task ordering: '@'-parameterised tasks first, then natural order
    by id_name (numeric-aware)."""
    return (0 if "@" in t.get("event_name", "") else 1,
            _natural_key(t.get("id_name", "")))

def kb(rows):
    """Build an InlineKeyboardMarkup from rows of (text, data) tuples.

    Fix: entries whose `data` starts with 'url:' are rendered as REAL
    link buttons (InlineKeyboardButton(url=...)), not callback_data
    buttons. Previously every entry used callback_data even for URLs,
    so tapping "Contact Admin to Buy" fired a dead callback that no
    handler processed — the button visibly did nothing.
    """
    keyboard = []
    for row in rows:
        line = []
        for text, data in row:
            if data.startswith("url:"):
                real_url = data[4:]
                line.append(InlineKeyboardButton(text, url=real_url))
            else:
                line.append(InlineKeyboardButton(text, callback_data=data))
        keyboard.append(line)
    return InlineKeyboardMarkup(keyboard)

def back_kb(cb="menu:back"):
    return kb([[("🔙 Back", cb)]])

def _paginate(items, page, page_size):
    """Slice a list into a page. Returns (page_items, page, total_pages)."""
    total_pages = max(1, (len(items) - 1) // page_size + 1) if items else 1
    page = max(0, min(page, total_pages - 1))
    return items[page * page_size:(page + 1) * page_size], page, total_pages

def _nav_row(page, total_pages, cb_prefix):
    """Build a Prev/Next row for pagination. cb_prefix already ends right
    before the page number, e.g. 'explore:page:'."""
    row = []
    if page > 0:
        row.append((f"⬅️ Prev", f"{cb_prefix}{page-1}"))
    if page < total_pages - 1:
        row.append((f"Next ➡️", f"{cb_prefix}{page+1}"))
    return row

# Every place that lists the user's own games as tappable buttons (My Games,
# Send Event's game picker, Scheduler's New Schedule game picker) pages at
# 5 games per screen with ⬅️ Prev / Next ➡️ navigation.
GAMES_PAGE_SIZE = 5

# New Purchase Schedule game picker shows 10 games per page.
PURCH_SCHED_PAGE_SIZE = 10

# Every screen that lists a game's AF UIDs as tappable buttons (Send Task,
# Send Purchase, Scheduler's UID picker, Delete AF UID) pages at 10 per
# screen with ⬅️ Prev / Next ➡️ navigation.
UIDS_PAGE_SIZE = 10


# The Send Event "Select Task" screen pages the game's tasks at 10 per screen
# (5 rows of 2) with ⬅️ Prev / Next ➡️ navigation.
TASKS_PAGE_SIZE = 10


# ═══════════════════════════════════════════════════════════════════════════════
#  Guest Welcome (no login required) — Explore Games entry point
# ═══════════════════════════════════════════════════════════════════════════════
async def show_welcome(update: Update, ctx: ContextTypes.DEFAULT_TYPE, edit=False):
    text = (
        "👋 *Welcome to AppsFlyer Master Bot!*\n\n"
        "🌐 Browse available games without logging in, or log in to "
        "your account to send events and manage your games."
    )
    rows = [
        [("🌐 Explore Games", "guest:explore")],
        [("🔐 Login", "guest:login")],
    ]
    target_kb = kb(rows)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=target_kb)
    else:
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg:
            await msg.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=target_kb)

async def guest_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    act = q.data.split(":", 1)[1]

    if act == "explore":
        return await show_explore(update, ctx)

    if act == "login":
        await q.edit_message_text(
            "🔐 *Login*\n\nEnter your *email address*:",
            parse_mode=ParseMode.MARKDOWN)
        return ST_EMAIL

    if act == "back":
        await show_welcome(update, ctx, edit=True)
        return ST_GUEST

    return ST_GUEST


# ═══════════════════════════════════════════════════════════════════════════════
#  Global token-refresh middleware
#
#  ROOT CAUSE FIX for "works right after login, silently breaks after the
#  session has been running a while": Supabase access tokens expire
#  (commonly after 1 hour) and nothing ever refreshed them. Every write
#  (add AF UID, new schedule, delete schedule, etc.) after expiry got a
#  silent 401 and never actually saved — but the bot kept showing "✅ Done!"
#  because no response was ever checked. Every read after expiry silently
#  fell back to old in-memory cached data, so changes made elsewhere
#  (desktop app, admin panel) never appeared until the user logged out and
#  back in (which forces a fresh token + fresh fetch).
#
#  This runs in PTB group=-1, meaning it fires for EVERY update (message or
#  button tap) before the conversation handler processes it, and silently
#  refreshes the token a couple of minutes before it would actually expire.
# ═══════════════════════════════════════════════════════════════════════════════
async def _global_token_refresh_middleware(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not sess(ctx).get("token"):
        return   # not logged in — nothing to refresh
    expires_at = sess(ctx).get("token_expires_at", 0)
    if time.time() < expires_at - 120:   # still valid for more than 2 minutes
        return
    rtok = sess(ctx).get("refresh_token")
    if not rtok:
        return
    try:
        new_tok, new_rtok, new_exp = await asyncio.get_running_loop().run_in_executor(
            None, lambda: SB.refresh_session(rtok))
        sess(ctx)["token"]            = new_tok
        sess(ctx)["refresh_token"]    = new_rtok
        sess(ctx)["token_expires_at"] = time.time() + new_exp
        log.info("🔄 Refreshed session token for uid=%s", sess(ctx).get("uid"))
    except Exception as e:
        # Refresh token itself is invalid/expired/revoked. Don't crash the
        # update — let the in-flight action proceed with the old token (it
        # will fail cleanly) and surface a clear message on the next visible
        # failure rather than silently pretending everything is fine.
        log.warning("Token refresh failed for uid=%s: %s", sess(ctx).get("uid"), e)


# ═══════════════════════════════════════════════════════════════════════════════
#  Login flow
# ═══════════════════════════════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_logged_in(ctx):
        await show_main_menu(update, ctx)
        return ST_MAIN
    # Fix: previously this jumped straight into asking for an email,
    # so unauthenticated users had NO way to reach Explore Games.
    # Now we show a welcome screen with Explore / Login choices first.
    await show_welcome(update, ctx)
    return ST_GUEST

async def handle_email(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["_email"] = update.message.text.strip()
    await update.message.reply_text("🔑 Enter your *password*:", parse_mode=ParseMode.MARKDOWN)
    return ST_PASS

async def handle_password(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    email    = ctx.user_data.pop("_email", "")
    password = update.message.text.strip()
    msg = await update.message.reply_text("⏳ Logging in…")
    try:
        token, refresh_token, expires_in, uid = await asyncio.get_running_loop().run_in_executor(
            None, lambda: SB.login(email, password))
        ur = await asyncio.get_running_loop().run_in_executor(
            None, lambda: SB.get_user_record(uid, token))
    except Exception as e:
        await msg.edit_text(f"❌ Login failed: {e}")
        return ST_EMAIL

    if not ur:
        await msg.edit_text("❌ User record not found. Contact admin.")
        return ST_EMAIL
    if ur.get("banned"):
        await msg.edit_text("🚫 Your account is banned.")
        return ST_EMAIL

    is_admin_acct = ur.get("is_admin", False)
    tg_id = update.effective_user.id
    if not is_admin_acct:
        stored_tg = ur.get("telegram_id")
        if stored_tg is None:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: SB.set_telegram_id(uid, token, tg_id))
                ur["telegram_id"] = tg_id
            except Exception:
                pass
        elif int(stored_tg) != int(tg_id):
            await msg.edit_text(
                "🚫 *This account is already linked to another Telegram account.*\n\n"
                "Contact admin to reset.",
                parse_mode=ParseMode.MARKDOWN)
            return ST_EMAIL

    ur["games"]           = _pj(ur.get("games"), {})
    ur["scheduled_tasks"] = _pj(ur.get("scheduled_tasks"), [])
    ctx.user_data.update({
        "token": token,
        "refresh_token": refresh_token,
        "token_expires_at": time.time() + expires_in,
        "uid": uid,
        "is_admin": is_admin_acct, "user_record": ur})
    await msg.delete()
    await show_main_menu(update, ctx)
    return ST_MAIN

async def cmd_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in(ctx):
        await show_welcome(update, ctx)
        return ST_GUEST
    await show_main_menu(update, ctx)
    return ST_MAIN

# ── /mygames — open the My Games page directly (logged-in users) ────────────────
async def cmd_mygames(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in(ctx):
        await show_welcome(update, ctx)
        return ST_GUEST
    games = await _fetch_and_refresh_games(ctx)   # fresh — show latest desktop/admin changes
    if not games:
        await update.message.reply_text("🎮 No games assigned.\nContact admin.",
                                        reply_markup=back_kb("menu:back"))
        return ST_MAIN

    names = sorted(games)
    page_names, page, total_pages = _paginate(names, 0, GAMES_PAGE_SIZE)

    rows = [[("🔍 Search game", "search:games")]]
    for gn in page_names:
        g      = games[gn]
        plat_e = "🍎" if (g.get("platform") or "android").lower() == "ios" else "🤖"
        uids   = len(g.get("af_uids", []))
        lim    = g.get("af_uid_limit", 2)
        rows.append([(f"{plat_e} {gn}  [{uids}/{lim} UIDs]", f"game:{gn}")])
    nav = _nav_row(page, total_pages, "gpage:")
    if nav:
        rows.append(nav)
    rows.append([("🔙 Back", "menu:back")])

    header = f"🎮 *My Games* — Select a game:\n_Page {page+1}/{total_pages} · {len(names)} total_"
    await update.message.reply_text(header, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_GAME_ACTION

# ── /event — open the Send Event page directly (logged-in users) ────────────────
async def cmd_event(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in(ctx):
        await show_welcome(update, ctx)
        return ST_GUEST
    games = await _fetch_and_refresh_games(ctx)   # fresh — show latest desktop/admin changes
    if not games:
        await update.message.reply_text("❌ No games available.", reply_markup=back_kb("menu:back"))
        return ST_MAIN

    names = sorted(games)
    page_names, page, total_pages = _paginate(names, 0, GAMES_PAGE_SIZE)

    rows = [[("🔍 Search game", "search:send")]]
    for gn in page_names:
        g      = games[gn]
        plat_e = "🍎" if (g.get("platform") or "android").lower() == "ios" else "🤖"
        rows.append([(f"{plat_e} {gn}", f"snd_game:{gn}")])
    nav = _nav_row(page, total_pages, "sndpage:")
    if nav:
        rows.append(nav)
    rows.append([("🔙 Back", "menu:back")])
    header = f"📤 *Send Event* — Select a game:\n_Page {page+1}/{total_pages} · {len(names)} total_"
    await update.message.reply_text(header, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_SEND_TYPE


# ═══════════════════════════════════════════════════════════════════════════════
#  Main menu
# ═══════════════════════════════════════════════════════════════════════════════
async def show_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE, edit=False):
    ur    = sess(ctx).get("user_record", {})
    email = ur.get("email", "User")
    games = get_games(ctx)

    # Always fetch fresh scheduled tasks from DB to avoid ghost counts
    try:
        token = sess(ctx).get("token"); uid = sess(ctx).get("uid")
        if token and uid:
            ur_fresh = await asyncio.get_running_loop().run_in_executor(
                None, lambda: SB.get_user_record(uid, token))
            if ur_fresh:
                sched = _pj(ur_fresh.get("scheduled_tasks"), [])
                ur["scheduled_tasks"] = sched
                sess(ctx)["user_record"]["scheduled_tasks"] = sched
            else:
                sched = _pj(ur.get("scheduled_tasks"), [])
        else:
            sched = _pj(ur.get("scheduled_tasks"), [])
    except Exception:
        sched = _pj(ur.get("scheduled_tasks"), [])

    pending = sum(1 for t in sched if t.get("status") == "pending")

    rows = [
        [("🎮 My Games", "menu:games"),   ("📤 Send Event",  "menu:send")],
        [("⏰ Scheduler", "menu:sched"),  ("📊 Dashboard",   "menu:dash")],
        [("🌐 Explore",  "menu:explore"), ("⚙️ Settings",    "menu:settings")],
    ]
    if sess(ctx).get("is_admin"):
        rows.append([("👑 Admin Panel", "menu:admin")])
    rows.append([("🚪 Logout", "menu:logout")])

    text = (f"🏠 *Main Menu*\n👤 {email}\n"
            f"🎮 Games: {len(games)}  •  ⏰ Pending: {pending}")
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    else:
        target = update.message or (update.callback_query.message if update.callback_query else None)
        if target:
            await target.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))

async def main_menu_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    dest = q.data.split(":", 1)[1]
    if dest == "back":
        await show_main_menu(update, ctx, edit=True)
        return ST_MAIN
    if dest == "games":   return await show_games_list(update, ctx)
    if dest == "send":    return await show_send_game_select(update, ctx)
    if dest == "sched":   return await show_scheduler(update, ctx)
    if dest == "dash":    return await show_dashboard(update, ctx)
    if dest == "explore": return await show_explore(update, ctx)
    if dest == "settings":return await show_settings(update, ctx)
    if dest == "admin":   return await show_admin_menu(update, ctx)
    if dest == "logout":
        ctx.user_data.clear()
        await q.edit_message_text("👋 Logged out. Send /start to log in again.")
        return ConversationHandler.END
    return ST_MAIN


# ═══════════════════════════════════════════════════════════════════════════════
#  My Games
# ═══════════════════════════════════════════════════════════════════════════════
async def show_games_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE, page: int = 0):
    games = await _fetch_and_refresh_games(ctx)   # fresh — show latest desktop/admin changes
    q     = update.callback_query
    if not games:
        await q.edit_message_text("🎮 No games assigned.\nContact admin.",
                                  reply_markup=back_kb("menu:back"))
        return ST_MAIN

    names = sorted(games)
    page_names, page, total_pages = _paginate(names, page, GAMES_PAGE_SIZE)

    rows = [[("🔍 Search game", "search:games")]]
    for gn in page_names:
        g      = games[gn]
        plat_e = "🍎" if (g.get("platform") or "android").lower() == "ios" else "🤖"
        uids   = len(g.get("af_uids", []))
        lim    = g.get("af_uid_limit", 2)
        rows.append([(f"{plat_e} {gn}  [{uids}/{lim} UIDs]", f"game:{gn}")])
    nav = _nav_row(page, total_pages, "gpage:")
    if nav:
        rows.append(nav)
    rows.append([("🔙 Back", "menu:back")])

    header = f"🎮 *My Games* — Select a game:\n_Page {page+1}/{total_pages} · {len(names)} total_"
    await q.edit_message_text(header, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_GAME_ACTION

async def games_page_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":", 1)[1])
    return await show_games_list(update, ctx, page=page)

async def _render_game_detail(q, ctx, gn):
    games = await _fetch_and_refresh_games(ctx)   # fresh — show latest desktop/admin changes
    g     = games.get(gn)
    if not g:
        await q.edit_message_text("❌ Game not found.", reply_markup=back_kb("menu:games"))
        return ST_GAME_ACTION

    plat   = g.get("platform") or "android"
    plat_e = "🍎 iOS" if plat.lower() == "ios" else "🤖 Android"
    uids   = g.get("af_uids", [])
    tasks  = g.get("tasks", [])
    vd     = g.get("validation_date") or "Unlimited"
    acts   = "🟢 Active" if g.get("has_activity") else "⚪ Not Active"

    uid_lines = "\n".join(f"  • {af.get('name','?')}  `{af.get('uid','?')[:16]}…`"
                          for af in uids[:5]) or "  None"
    if len(uids) > 5:
        uid_lines += f"\n  … and {len(uids)-5} more"
    task_lines = "\n".join(f"  • {t.get('id_name','?')}" for t in tasks[:5]) or "  None"
    if len(tasks) > 5:
        task_lines += f"\n  … and {len(tasks)-5} more"

    text = (f"🎮 *{gn}*\n📱 Platform: {plat_e}\n📊 Status: {acts}\n"
            f"📅 Valid: {vd[:10] if len(vd)>8 else vd}\n"
            f"👥 AF UIDs: {len(uids)}/{g.get('af_uid_limit',2)}\n\n"
            f"*AF UIDs:*\n{uid_lines}\n\n*Tasks:*\n{task_lines}")
    ctx.user_data["_cur_game"] = gn
    rows = [
        [(f"➕ Add {id_label(g)} / UID", f"adduid:{gn}")],
        [(f"🗑 Delete AF UID", f"deluid:{gn}")],
        [("🔙 My Games", "menu:games")],
    ]
    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_GAME_ACTION

async def game_detail_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q  = update.callback_query
    await q.answer()
    gn = q.data.split(":", 1)[1]
    return await _render_game_detail(q, ctx, gn)


# ═══════════════════════════════════════════════════════════════════════════════
#  Game search
# ═══════════════════════════════════════════════════════════════════════════════
_SEARCH_CTXS = {
    "games": ("game:",     "menu:games", "🎮 My Games",   ST_GAME_ACTION),
    "send":  ("snd_game:", "menu:send",  "📤 Send Event", ST_SEND_TYPE),
    "sched": ("sg:",       "sched:new",  "⏰ Scheduler",   ST_SCHED_TASK),
    "purch": ("sp_game:",  "sched:newpurch", "💰 Purchase Schedule", ST_SCHED_PURCH_GAME),
}

async def search_prompt_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    origin = q.data.split(":", 1)[1]
    if origin not in _SEARCH_CTXS:
        origin = "games"
    ctx.user_data["_search_ctx"] = origin
    await q.edit_message_text("🔍 *Search*\nType part of the game name:",
                               parse_mode=ParseMode.MARKDOWN,
                               reply_markup=kb([(("\u274c Cancel", "search:cancel"),)]))
    # Route to the purchase-specific search state so the right handlers fire.
    return ST_SCHED_PURCH_SEARCH if origin == "purch" else ST_GAME_SEARCH

async def _show_search_results(reply_fn, ctx, query_text):
    origin = ctx.user_data.get("_search_ctx", "games")
    prefix, back_cb, title, next_state = _SEARCH_CTXS[origin]
    games   = get_games(ctx)
    if origin in ("sched", "purch"):
        # Expired games can't send events / purchases — filter them out.
        games = {gn: g for gn, g in games.items() if _check_game_valid(g)[0]}
    matches = sorted(gn for gn in games if query_text.lower() in gn.lower())
    # If still awaiting typed input, return the appropriate waiting state.
    search_wait_state = ST_SCHED_PURCH_SEARCH if origin == "purch" else ST_GAME_SEARCH
    if not matches:
        await reply_fn(f"🔍 No games match `{query_text}`.\nTry again:",
                       parse_mode=ParseMode.MARKDOWN,
                       reply_markup=kb([(("\u274c Cancel", "search:cancel"),)]))
        return search_wait_state
    rows = []
    for gn in matches[:25]:
        g      = games[gn]
        plat_e = "🍎" if (g.get("platform") or "android").lower() == "ios" else "🤖"
        rows.append([(f"{plat_e} {gn}", f"{prefix}{gn}")])
    rows.append([("🔍 Search Again", f"search:{origin}"), ("🔙 Back", back_cb)])
    await reply_fn(f"🔍 *Results for* `{query_text}` *in {title}:*",
                   parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return next_state

async def game_search_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    origin     = ctx.user_data.get("_search_ctx", "games")
    if not query_text:
        await update.message.reply_text("⚠️ Type at least one character:")
        return ST_SCHED_PURCH_SEARCH if origin == "purch" else ST_GAME_SEARCH
    async def reply_fn(text, **kw):
        await update.message.reply_text(text, **kw)
    return await _show_search_results(reply_fn, ctx, query_text)

async def search_cancel_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    origin = ctx.user_data.pop("_search_ctx", "games")
    if origin == "sched":
        q.data = "sched:new"
        return await sched_cb(update, ctx)
    if origin == "purch":
        q.data = "sched:newpurch"
        return await sched_cb(update, ctx)
    await q.answer()
    if origin == "games": return await show_games_list(update, ctx)
    if origin == "send":  return await show_send_game_select(update, ctx)
    await show_main_menu(update, ctx, edit=True)
    return ST_MAIN


# ═══════════════════════════════════════════════════════════════════════════════
#  Add AF UID
# ═══════════════════════════════════════════════════════════════════════════════
def _check_game_valid(g):
    vd = g.get("validation_date")
    if vd:
        try:
            exp = datetime.fromisoformat(vd.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > exp:
                return False, "❌ Game validation expired. Contact admin."
        except Exception:
            pass
    return True, ""

def _uid_daily_limit_check(g):
    limit = g.get("af_uid_limit", 2)
    reset_start     = get_reset_window_start()
    reset_start_utc = reset_start - BDT
    additions       = g.get("af_uid_additions", [])
    recent = 0
    for ts in additions:
        try:
            if datetime.fromisoformat(ts.replace("Z", "+00:00")) > reset_start_utc:
                recent += 1
        except Exception:
            continue
    next_reset = (reset_start + timedelta(days=1)).strftime("%I:%M %p")
    return recent < limit, recent, limit, next_reset

async def add_uid_start_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q  = update.callback_query
    await q.answer()
    gn = q.data.split(":", 1)[1]
    games = get_games(ctx)
    g     = games.get(gn)
    if not g:
        await q.edit_message_text("❌ Game not found.", reply_markup=back_kb("menu:games"))
        return ST_GAME_ACTION
    ok, reason = _check_game_valid(g)
    if not ok:
        await q.edit_message_text(reason, reply_markup=back_kb(f"game:{gn}"))
        return ST_GAME_ACTION
    allowed, recent, limit, next_reset = _uid_daily_limit_check(g)
    if not allowed:
        await q.edit_message_text(
            f"🚫 *Daily Limit Reached*\n\nLimit: {limit}/day\nAdded today: {recent}\n"
            f"Resets at 4 PM BDT — next: *{next_reset} BDT*",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb(f"game:{gn}"))
        return ST_GAME_ACTION
    ctx.user_data["_adduid_game"] = gn
    await q.edit_message_text(
        f"➕ *Add AF UID to {gn}*\n\nEnter a *name* for this UID (e.g. `Player1`):",
        parse_mode=ParseMode.MARKDOWN)
    return ST_ADD_UID_NAME

async def add_uid_name_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("⚠️ Name can't be empty:")
        return ST_ADD_UID_NAME
    ctx.user_data["_adduid_name"] = name
    await update.message.reply_text("🆔 Enter the *AF UID*:", parse_mode=ParseMode.MARKDOWN)
    return ST_ADD_UID_VALUE

async def add_uid_value_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid_val = update.message.text.strip()
    gn      = ctx.user_data.get("_adduid_game")
    games   = get_games(ctx)
    g       = games.get(gn, {})
    if not uid_val:
        await update.message.reply_text("⚠️ AF UID can't be empty:")
        return ST_ADD_UID_VALUE
    for af in g.get("af_uids", []):
        if af.get("uid") == uid_val and not af.get("deleted_by_user"):
            await update.message.reply_text("⚠️ UID already exists. Enter a different one:")
            return ST_ADD_UID_VALUE
    ctx.user_data["_adduid_uid"] = uid_val
    label = id_label(g)
    await update.message.reply_text(
        f"🏷 Enter the *{label}* (or `-` to skip):", parse_mode=ParseMode.MARKDOWN)
    return ST_ADD_UID_GAID

async def add_uid_gaid_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    gaid_val = update.message.text.strip()
    if gaid_val in ("-", "skip", "none", "None"): gaid_val = ""
    gn      = ctx.user_data.get("_adduid_game")
    name    = ctx.user_data.get("_adduid_name")
    uid_val = ctx.user_data.get("_adduid_uid")
    all_g   = await _fetch_and_refresh_games(ctx)   # fresh — don't clobber desktop/admin changes
    g       = all_g.get(gn, {})
    allowed, recent, limit, next_reset = _uid_daily_limit_check(g)
    if not allowed:
        for k in ("_adduid_game", "_adduid_name", "_adduid_uid"):
            ctx.user_data.pop(k, None)
        await update.message.reply_text(
            f"🚫 Daily limit reached ({recent}/{limit}). Next reset: {next_reset} BDT.")
        return ST_MAIN
    now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    g.setdefault("af_uid_additions", []).append(now_str)
    g.setdefault("af_uids", []).append(
        {"name": name, "uid": uid_val, "gaid": gaid_val, "usage": []})
    all_g[gn] = g
    sess(ctx)["user_record"]["games"] = all_g
    token = sess(ctx)["token"]; uidv = sess(ctx)["uid"]
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.save_games(uidv, token, all_g))
    for k in ("_adduid_game", "_adduid_name", "_adduid_uid"):
        ctx.user_data.pop(k, None)
    label = id_label(g)
    await update.message.reply_text(
        f"✅ *AF UID added!*\n🎮 Game: *{gn}*\n👤 Name: *{name}*\n"
        f"🆔 UID: `{uid_val[:24]}…`\n🏷 {label}: `{gaid_val or '—'}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[(f"🎮 View {gn}", f"game:{gn}"), ("🏠 Menu", "menu:back")]]))
    return ST_GAME_ACTION


# ═══════════════════════════════════════════════════════════════════════════════
#  Delete AF UID (user-facing)
#
#  Mirrors the desktop app's _user_delete_af_uid exactly:
#    • Hard-delete — the entry is fully removed from af_uids, not flagged.
#    • af_uid_additions is NEVER touched by a delete. That list is the daily
#      add-limit audit trail; if delete removed entries from it, a user
#      could add → delete → add → delete… to bypass the daily cap entirely.
#      Deleting a UID intentionally does NOT free up or change the daily
#      AF UID add limit — same as the desktop app.
# ═══════════════════════════════════════════════════════════════════════════════
async def del_uid_list_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q  = update.callback_query
    await q.answer()
    if q.data.startswith("deluidnav:"):
        # Safe parse even if the game name itself contains ':' — the page
        # number is always the last colon-separated token.
        rest       = q.data[len("deluidnav:"):]
        gn, pg_str = rest.rsplit(":", 1)
        page       = int(pg_str)
    else:
        gn   = q.data.split(":", 1)[1]     # "deluid:{gn}" — entry point, page 0
        page = 0

    games = await _fetch_and_refresh_games(ctx)   # fresh — don't act on stale data
    g     = games.get(gn)
    if not g:
        await q.edit_message_text("❌ Game not found.", reply_markup=back_kb("menu:games"))
        return ST_GAME_ACTION

    af_list_full = [af for af in g.get("af_uids", []) if not af.get("deleted_by_user")]
    if not af_list_full:
        await q.edit_message_text("ℹ️ No AF UIDs to delete for this game.",
                                  reply_markup=back_kb(f"game:{gn}"))
        return ST_GAME_ACTION

    page_list, page, total_pages = _paginate(af_list_full, page, UIDS_PAGE_SIZE)

    # Store the FULL list (not just this page) so index-based confirm still
    # works correctly regardless of which page the user is on.
    ctx.user_data["_deluid_game"] = gn
    ctx.user_data["_deluid_list"] = af_list_full

    rows = []
    base_idx = page * UIDS_PAGE_SIZE
    for offset, af in enumerate(page_list):
        i     = base_idx + offset
        label = f"🗑 {af.get('name','?')}  {af.get('uid','?')[:14]}…"
        rows.append([(label, f"deluidsel:{i}")])
    nav = _nav_row(page, total_pages, f"deluidnav:{gn}:")
    if nav:
        rows.append(nav)
    rows.append([("🔙 Back", f"game:{gn}")])

    header = (f"🗑 *Delete AF UID — {gn}*\n"
              f"_Page {page+1}/{total_pages} · {len(af_list_full)} total_\n\n"
              f"Select the AF UID to remove:")
    await q.edit_message_text(header, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_GAME_ACTION


async def del_uid_confirm_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    idx = int(q.data.split(":", 1)[1])
    af_list = ctx.user_data.get("_deluid_list", [])
    gn      = ctx.user_data.get("_deluid_game")
    if idx < 0 or idx >= len(af_list) or not gn:
        await q.edit_message_text("❌ Selection expired. Please try again.",
                                  reply_markup=back_kb("menu:games"))
        return ST_GAME_ACTION
    target = af_list[idx]
    ctx.user_data["_deluid_target"] = target

    await q.edit_message_text(
        f"⚠️ *Confirm Delete*\n\n"
        f"Delete *{target.get('name','?')}* from your account?\n"
        f"`{target.get('uid','?')}`\n\n"
        f"This cannot be undone.\n"
        f"ℹ️ _Deleting an AF UID does not reset your daily add limit._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([
            [("✅ Yes, Delete", "deluidyes"), ("❌ Cancel", f"game:{gn}")],
        ]))
    return ST_GAME_ACTION


async def del_uid_yes_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    gn     = ctx.user_data.get("_deluid_game")
    target = ctx.user_data.get("_deluid_target")
    if not gn or not target:
        await q.edit_message_text("❌ Selection expired. Please try again.",
                                  reply_markup=back_kb("menu:games"))
        return ST_GAME_ACTION

    games = await _fetch_and_refresh_games(ctx)   # fresh — don't clobber concurrent changes
    g     = games.get(gn)
    if not g:
        await q.edit_message_text("❌ Game not found.", reply_markup=back_kb("menu:games"))
        return ST_GAME_ACTION

    target_uid = target.get("uid")
    before     = len(g.get("af_uids", []))
    # Hard-delete only — af_uid_additions is intentionally left untouched.
    g["af_uids"] = [af for af in g.get("af_uids", []) if af.get("uid") != target_uid]
    removed = before - len(g["af_uids"])

    for k in ("_deluid_game", "_deluid_list", "_deluid_target"):
        ctx.user_data.pop(k, None)

    if removed == 0:
        await q.edit_message_text("ℹ️ That AF UID was already removed.",
                                  reply_markup=back_kb(f"game:{gn}"))
        return ST_GAME_ACTION

    games[gn] = g
    sess(ctx)["user_record"]["games"] = games
    token = sess(ctx)["token"]; uidv = sess(ctx)["uid"]
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.save_games(uidv, token, games))

    await q.edit_message_text(
        f"✅ *'{target.get('name','?')}' removed from your account.*\n\n"
        f"ℹ️ _Deleting AF UIDs does not reset your daily add limit._",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[(f"🎮 View {gn}", f"game:{gn}"), ("🏠 Menu", "menu:back")]]))
    return ST_GAME_ACTION


# ═══════════════════════════════════════════════════════════════════════════════
#  Send Event
# ═══════════════════════════════════════════════════════════════════════════════
async def show_send_game_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE, page: int = 0):
    all_games = await _fetch_and_refresh_games(ctx)   # fresh — show latest desktop/admin changes
    q         = update.callback_query
    # Exclude games whose validation date has expired.
    games = {gn: g for gn, g in all_games.items() if _check_game_valid(g)[0]}
    if not games:
        msg = ("❌ *All your games have expired validation dates.*\n\n"
               "Contact admin to extend validity before sending events."
               if all_games else "❌ No games available.")
        await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=back_kb("menu:back"))
        return ST_MAIN

    names = sorted(games)
    page_names, page, total_pages = _paginate(names, page, GAMES_PAGE_SIZE)

    rows = [[("🔍 Search game", "search:send")]]
    for gn in page_names:
        g      = games[gn]
        plat_e = "🍎" if (g.get("platform") or "android").lower() == "ios" else "🤖"
        rows.append([(f"{plat_e} {gn}", f"snd_game:{gn}")])
    nav = _nav_row(page, total_pages, "sndpage:")
    if nav:
        rows.append(nav)
    rows.append([("🔙 Back", "menu:back")])
    header = f"📤 *Send Event* — Select a game:\n_Page {page+1}/{total_pages} · {len(names)} total_"
    await q.edit_message_text(header, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_SEND_TYPE

async def send_games_page_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":", 1)[1])
    return await show_send_game_select(update, ctx, page=page)

async def send_game_pick_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q  = update.callback_query
    await q.answer()
    gn = q.data.split(":", 1)[1]
    games = get_games(ctx)
    g     = games.get(gn, {})
    ok, reason = _check_game_valid(g)
    if not ok:
        await q.edit_message_text(reason, reply_markup=back_kb("menu:send"))
        return ST_SEND_TYPE
    ctx.user_data["_snd_game"] = gn
    await q.edit_message_text(
        f"📤 *{gn}*\nWhat type of event?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([
            [("✅ Task Event", "snd_type:task"), ("💰 Fake Purchase", "snd_type:purchase")],
            [("🔙 Back", "menu:send")],
        ]))
    return ST_SEND_TYPE

async def send_type_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    typ = q.data.split(":", 1)[1]
    ctx.user_data["_snd_type"] = typ
    gn  = ctx.user_data.get("_snd_game")
    games = get_games(ctx)
    g   = games.get(gn, {})

    if typ == "task":
        tasks = g.get("tasks", [])
        if not tasks:
            await q.edit_message_text("❌ No tasks in this game.",
                                      reply_markup=back_kb(f"snd_game:{gn}"))
            return ST_SEND_TYPE
        return await _show_send_task_list(q, ctx, gn, page=0)
    else:
        ctx.user_data["_purch_event"] = "af_purchase"
        ctx.user_data["_purch_cur"]   = "USD"
        ctx.user_data["_purch_pid"]   = ""
        await q.edit_message_text(
            "💵 *Fake Purchase*\nEnter *amount* (e.g. `4.99`):",
            parse_mode=ParseMode.MARKDOWN)
        return ST_PURCH_AMT

async def _show_send_task_list(q, ctx, gn, page: int = 0):
    """Render one page of a game's tasks for Send Event, 10 per page,
    with ⬅️ Prev / Next ➡️ navigation."""
    games = get_games(ctx)
    g     = games.get(gn, {})
    tasks = g.get("tasks", [])
    if not tasks:
        await q.edit_message_text("❌ No tasks in this game.",
                                  reply_markup=back_kb(f"snd_game:{gn}"))
        return ST_SEND_TYPE
    tasks_s = sorted(tasks, key=_task_sort_key)
    page_tasks, page, total_pages = _paginate(tasks_s, page, TASKS_PAGE_SIZE)

    rows = [[(t["id_name"], f"snd_task:{t['id_name']}") for t in page_tasks[i:i+2]]
            for i in range(0, len(page_tasks), 2)]
    nav = _nav_row(page, total_pages, "sndtpage:")
    if nav:
        rows.append(nav)
    rows.append([("🔙 Back", f"snd_game:{gn}")])
    header = (f"✅ *Select Task:*\n"
              f"_Page {page+1}/{total_pages} · {len(tasks_s)} total_")
    await q.edit_message_text(header, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_TASK_PICK

async def send_task_page_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":", 1)[1])
    gn   = ctx.user_data.get("_snd_game")
    return await _show_send_task_list(q, ctx, gn, page=page)

async def task_pick_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query
    await q.answer()
    task_id = q.data.split(":", 1)[1]
    ctx.user_data["_snd_task_id"] = task_id
    gn      = ctx.user_data.get("_snd_game")
    games   = get_games(ctx)
    g       = games.get(gn, {})
    task    = next((t for t in g.get("tasks",[]) if t["id_name"]==task_id), None)
    if task and "@" in task.get("event_name",""):
        await q.edit_message_text(
            f"🔢 *{task_id}* — enter the level number (e.g. `20`):",
            parse_mode=ParseMode.MARKDOWN)
        return ST_TASK_NUM
    return await _show_uid_pick(update, ctx, gn, g)

async def task_num_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    num = update.message.text.strip()
    if not num.isdigit():
        await update.message.reply_text("⚠️ Whole numbers only (e.g. `20`):",
                                        parse_mode=ParseMode.MARKDOWN)
        return ST_TASK_NUM
    ctx.user_data["_snd_task_num"] = num
    gn    = ctx.user_data.get("_snd_game")
    games = get_games(ctx)
    g     = games.get(gn, {})
    return await _show_uid_pick(update, ctx, gn, g)

async def _show_uid_pick(update, ctx, gn, g, page: int = 0):
    uids = g.get("af_uids", [])
    if not uids:
        txt = "❌ No AF UIDs in this game. Add UIDs first."
        if update.callback_query:
            await update.callback_query.edit_message_text(txt)
        else:
            await update.message.reply_text(txt)
        return ST_SEND_TYPE
    page_list, page, total_pages = _paginate(uids, page, UIDS_PAGE_SIZE)
    rows = [[(f"{af.get('name','?')}  {af.get('uid','?')[:14]}…", f"snd_uid:{af['uid']}")]
            for af in page_list]
    nav = _nav_row(page, total_pages, "snduidpage:")
    if nav:
        rows.append(nav)
    rows.append([("✅ All UIDs", "snd_uid:ALL"), ("🔙 Back", "snd_type:task")])
    txt = f"👤 *Select AF UID:*\n_Page {page+1}/{total_pages} · {len(uids)} total_"
    if update.callback_query:
        await update.callback_query.edit_message_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    else:
        await update.message.reply_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_UID_PICK

async def snd_uid_page_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page  = int(q.data.split(":", 1)[1])
    gn    = ctx.user_data.get("_snd_game")
    games = await _fetch_and_refresh_games(ctx)
    g     = games.get(gn, {})
    return await _show_uid_pick(update, ctx, gn, g, page=page)

async def uid_pick_send_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    target = q.data.split(":", 1)[1]
    gn     = ctx.user_data.get("_snd_game")
    games  = await _fetch_and_refresh_games(ctx)   # fresh — don't clobber desktop/admin changes
    g      = games.get(gn, {})
    # Final validity guard — game may have expired between game-pick and UID confirmation.
    ok, reason = _check_game_valid(g)
    if not ok:
        for k in ("_snd_task_id", "_snd_task_num", "_snd_game", "_snd_type"):
            ctx.user_data.pop(k, None)
        await q.edit_message_text(reason, reply_markup=back_kb("menu:send"))
        return ST_MAIN
    task_id = ctx.user_data.get("_snd_task_id")
    task   = next((t for t in g.get("tasks",[]) if t["id_name"]==task_id), None)
    if not task:
        await q.edit_message_text("❌ Task not found.")
        return ST_MAIN
    num        = ctx.user_data.get("_snd_task_num")
    event_name = task["event_name"].replace("@", num) if num else task["event_name"]
    display    = f"{task_id} {num}" if num else task_id
    uids = g.get("af_uids", [])
    targets = uids if target == "ALL" else [af for af in uids if af.get("uid")==target]
    await q.edit_message_text(f"⏳ Sending *{display}* to {len(targets)} UID(s)…",
                               parse_mode=ParseMode.MARKDOWN)
    results = []
    any_ok  = False
    had_activity = g.get("has_activity", False)
    for af in targets:
        code, _ = await asyncio.get_running_loop().run_in_executor(
            None, lambda a=af: send_task_event(
                g["dev_key"], g["app_id"], a.get("uid",""), a.get("gaid",""),
                event_name, g.get("platform","android")))
        ok = code == 200
        if ok: any_ok = True
        results.append(f"{'✅' if ok else '❌'} `{af.get('name','?')}` → {'Done' if ok else f'Failed (HTTP {code})'}")
        if ok:
            af.setdefault("sent_tasks",[]).append(
                {"task":display,"event":event_name,"time":datetime.now(timezone.utc).isoformat()})
            g.setdefault("stats",{})
            g["stats"]["tasks"] = g["stats"].get("tasks",0)+1
            g["stats"]["last_task_event"] = display
    if any_ok and not had_activity:
        g["has_activity"] = True
    all_g = games; all_g[gn] = g
    sess(ctx)["user_record"]["games"] = all_g
    token = sess(ctx)["token"]; uid = sess(ctx)["uid"]
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.save_games(uid, token, all_g))
    for k in ("_snd_task_id","_snd_task_num","_snd_game","_snd_type"):
        ctx.user_data.pop(k, None)
    await q.edit_message_text(
        f"📤 *{display}*\n\n" + "\n".join(results),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[("📤 Send Another", "menu:send"), ("🏠 Menu", "menu:back")]]))
    return ST_MAIN


# ── Purchase flow ─────────────────────────────────────────────────────────────
# Note: event name / currency / product ID are no longer asked — they're
# defaulted (af_purchase / USD / none) and only amount + AF UID are collected.
async def purch_amt_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:    ctx.user_data["_purch_amt"] = float(update.message.text.strip())
    except:
        await update.message.reply_text("⚠️ Invalid. Enter a number like `4.99`:",
                                        parse_mode=ParseMode.MARKDOWN)
        return ST_PURCH_AMT
    gn    = ctx.user_data.get("_snd_game")
    games = get_games(ctx)
    g     = games.get(gn, {})
    return await _show_purch_uid_pick(update, ctx, gn, g)

async def _show_purch_uid_pick(update, ctx, gn, g, page: int = 0):
    uids = g.get("af_uids", [])
    if not uids:
        msg = "❌ No AF UIDs in this game."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return ST_MAIN
    page_list, page, total_pages = _paginate(uids, page, UIDS_PAGE_SIZE)
    rows = [[(f"{af.get('name','?')}  {af.get('uid','?')[:14]}…", f"pur_uid:{af['uid']}")]
            for af in page_list]
    nav = _nav_row(page, total_pages, "puruidpage:")
    if nav:
        rows.append(nav)
    rows.append([("✅ All UIDs", "pur_uid:ALL")])
    txt = f"👤 *Select AF UID:*\n_Page {page+1}/{total_pages} · {len(uids)} total_"
    if update.callback_query:
        await update.callback_query.edit_message_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    else:
        await update.message.reply_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_UID_PICK

async def pur_uid_page_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page  = int(q.data.split(":", 1)[1])
    gn    = ctx.user_data.get("_snd_game")
    games = await _fetch_and_refresh_games(ctx)
    g     = games.get(gn, {})
    return await _show_purch_uid_pick(update, ctx, gn, g, page=page)

async def uid_pick_purchase_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    target = q.data.split(":", 1)[1]
    gn     = ctx.user_data.get("_snd_game")
    games  = await _fetch_and_refresh_games(ctx)   # fresh — don't clobber desktop/admin changes
    g      = games.get(gn, {})
    event  = ctx.user_data.get("_purch_event","af_purchase")
    amount = ctx.user_data.get("_purch_amt",0.99)
    cur    = ctx.user_data.get("_purch_cur","USD")
    pid    = ctx.user_data.get("_purch_pid","")
    uids   = g.get("af_uids", [])
    targets = uids if target=="ALL" else [af for af in uids if af.get("uid")==target]
    await q.edit_message_text(
        f"⏳ Sending purchase *{event}* ${amount} {cur} to {len(targets)} UID(s)…",
        parse_mode=ParseMode.MARKDOWN)
    results = []; any_ok = False
    for af in targets:
        code, _ = await asyncio.get_running_loop().run_in_executor(
            None, lambda a=af: send_purchase_event(
                g["dev_key"], g["app_id"], a.get("uid",""), a.get("gaid",""),
                amount, cur, pid, event, g.get("platform","android")))
        ok = code == 200
        if ok: any_ok = True
        results.append(f"{'✅' if ok else '❌'} `{af.get('name','?')}` → {'Done' if ok else f'Failed (HTTP {code})'}")
        if ok:
            g.setdefault("stats",{})
            g["stats"]["purchases"] = g["stats"].get("purchases",0)+1
            g["stats"]["last_purchase_event"] = event
            # FIX: this record was never written, so purchases sent via the
            # bot never showed up in either the bot's own admin Purchase
            # History view or the desktop app's Purchase History tab (both
            # read af["usage"] entries with type == "purchase"). Field names
            # match the desktop app exactly: type, timestamp, amount,
            # currency, product_id.
            af.setdefault("usage", []).append({
                "type": "purchase",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "amount": amount,
                "currency": cur,
                "product_id": pid,
                "event": event,
            })
    if any_ok and not g.get("has_activity"):
        g["has_activity"] = True
    all_g = games; all_g[gn] = g
    sess(ctx)["user_record"]["games"] = all_g
    token = sess(ctx)["token"]; uid = sess(ctx)["uid"]
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.save_games(uid, token, all_g))
    for k in ("_purch_event","_purch_amt","_purch_cur","_purch_pid","_snd_game","_snd_type"):
        ctx.user_data.pop(k, None)
    await q.edit_message_text(
        "💰 *Purchase Sent*\n\n" + "\n".join(results),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[("📤 Send Another", "menu:send"), ("🏠 Menu", "menu:back")]]))
    return ST_MAIN


# ═══════════════════════════════════════════════════════════════════════════════
#  Scheduler — fresh from DB + delete pending + EDIT date/time of pending tasks
# ═══════════════════════════════════════════════════════════════════════════════
async def _fetch_and_refresh_tasks(ctx) -> list:
    """Always pull fresh task list from DB. Fix: previously partial
    failures could leave stale in-memory data displayed; we now always
    overwrite the in-memory cache with exactly what the DB returns
    (or an explicit empty list on failure), so no ghost/leftover
    scheduled tasks can be shown that don't actually exist server-side."""
    try:
        token = sess(ctx)["token"]
        uid   = sess(ctx)["uid"]
        ur    = await asyncio.get_running_loop().run_in_executor(
            None, lambda: SB.get_user_record(uid, token))
        if ur is not None:
            tasks = _pj(ur.get("scheduled_tasks"), [])
            sess(ctx)["user_record"]["scheduled_tasks"] = tasks
            return tasks
    except Exception:
        pass
    # On failure, do NOT trust any previously cached value — return
    # what's currently cached only as a last resort, otherwise empty.
    return _pj(sess(ctx).get("user_record", {}).get("scheduled_tasks"), [])

async def _fetch_and_refresh_games(ctx) -> dict:
    """Always pull the fresh 'games' object from DB right before any
    read-modify-write on it.

    FIX: Several bot actions (Add AF UID, Send Task, Send Purchase) used to
    read `games` from the in-memory session cache — which is a snapshot
    taken at login or at the last explicit refresh — modify one game inside
    it, then PATCH the *entire* games object back to the DB. If the desktop
    app (or the admin panel, or another bot session) had changed anything
    in `games` since that snapshot was taken (added an AF UID, added a
    task, updated stats, etc.), that whole change would be silently
    overwritten and lost the instant the bot saved its stale copy back.
    This is exactly why AF UIDs / tasks added in one place could vanish or
    fail to appear in the other. Always fetching fresh immediately before
    modifying closes that race window."""
    try:
        token = sess(ctx)["token"]
        uid   = sess(ctx)["uid"]
        ur    = await asyncio.get_running_loop().run_in_executor(
            None, lambda: SB.get_user_record(uid, token))
        if ur is not None:
            games = _pj(ur.get("games"), {})
            sess(ctx)["user_record"]["games"] = games
            return games
    except Exception:
        pass
    return _pj(sess(ctx).get("user_record", {}).get("games"), {})


async def show_scheduler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q     = update.callback_query
    tasks = await _fetch_and_refresh_tasks(ctx)
    tasks = sorted(tasks, key=lambda t: t.get("fire_at_utc",""))

    if not tasks:
        await q.edit_message_text(
            "⏰ *Scheduler*\nNo scheduled tasks yet.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([
                [("➕ New Task Schedule",     "sched:new")],
                [("💰 New Purchase Schedule", "sched:newpurch")],
                [("🔙 Back", "menu:back")],
            ]))
        return ST_SCHED_TASK

    lines = []
    for i, t in enumerate(tasks[-20:]):
        fire = t.get("fire_at_utc","")
        try:
            bd_t = (datetime.fromisoformat(fire.replace("Z","+00:00")) + BDT).strftime("%m-%d %H:%M")
        except: bd_t = fire[:16]
        st_icon = {"sent":"✅","error":"❌","retrying":"🔄","pending":"⏳"}.get(t.get("status",""), "❓")
        t_type  = t.get("type", "task")
        type_icon = "💰" if t_type == "purchase" else "✅"
        lines.append(
            f"{st_icon}{type_icon} *{t.get('task_id_name','?')}*  "
            f"`{t.get('game','?')[:14]}`  {bd_t} BDT")

    text = "⏰ *Scheduled Tasks*\n\n" + "\n".join(lines)
    await q.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([
            [("➕ New Task Schedule",     "sched:new"),
             ("💰 New Purchase Schedule", "sched:newpurch")],
            [("🗑 Clear Done", "sched:clear"),
             ("🧹 Clear ALL",  "sched:clearall")],
            [("🗑 Delete Pending", "sched:delpick"),
             ("✏️ Edit Pending",   "sched:editpick")],
            [("🔙 Back", "menu:back")],
        ]))
    return ST_SCHED_TASK

async def sched_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    act = q.data.split(":", 1)[1]

    if act == "clear":
        tasks = await _fetch_and_refresh_tasks(ctx)
        tasks[:] = [t for t in tasks if t.get("status") not in ("sent","error")]
        sess(ctx)["user_record"]["scheduled_tasks"] = tasks
        token = sess(ctx)["token"]; uid = sess(ctx)["uid"]
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: SB.save_scheduled(uid, token, tasks))
        await q.answer("✅ Cleared done tasks!", show_alert=True)
        return await show_scheduler(update, ctx)

    if act == "clearall":
        # Fix: lets a user self-service wipe any leftover/ghost schedule
        # entries (e.g. stale test data from earlier bot versions) that
        # don't correspond to anything created via the current flow.
        token = sess(ctx)["token"]; uid = sess(ctx)["uid"]
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: SB.save_scheduled(uid, token, []))
        sess(ctx)["user_record"]["scheduled_tasks"] = []
        await q.answer("🧹 All scheduled tasks cleared!", show_alert=True)
        return await show_scheduler(update, ctx)

    if act == "delpick":
        tasks = await _fetch_and_refresh_tasks(ctx)
        pending = [t for t in tasks if t.get("status") == "pending"]
        if not pending:
            await q.answer("No pending tasks to delete.", show_alert=True)
            return await show_scheduler(update, ctx)
        rows = []
        for t in pending:
            fire = t.get("fire_at_utc","")
            try:
                bd_t = (datetime.fromisoformat(fire.replace("Z","+00:00"))+BDT).strftime("%m-%d %H:%M")
            except: bd_t = fire[:16]
            label = f"🗑 {t.get('task_id_name','?')} — {t.get('game','?')[:12]} {bd_t}"
            rows.append([(label, f"sched:del:{t.get('task_id','')}")])
        rows.append([("🔙 Back", "sched:view")])
        await q.edit_message_text("🗑 *Select pending task to delete:*",
                                  parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
        return ST_SCHED_TASK

    # ── Edit pending: pick task ───────────────────────────────────────────────
    if act == "editpick":
        tasks = await _fetch_and_refresh_tasks(ctx)
        pending = [t for t in tasks if t.get("status") == "pending"]
        if not pending:
            await q.answer("No pending tasks to edit.", show_alert=True)
            return await show_scheduler(update, ctx)
        rows = []
        for t in pending:
            fire = t.get("fire_at_utc","")
            try:
                bd_t = (datetime.fromisoformat(fire.replace("Z","+00:00"))+BDT).strftime("%m-%d %H:%M")
            except: bd_t = fire[:16]
            label = f"✏️ {t.get('task_id_name','?')} — {t.get('game','?')[:10]} {bd_t}"
            rows.append([(label, f"sched:editsel:{t.get('task_id','')}")])
        rows.append([("🔙 Back", "sched:view")])
        await q.edit_message_text("✏️ *Select pending task to edit:*",
                                  parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
        return ST_SCHED_EDIT_PICK

    if act.startswith("editsel:"):
        task_id = act[8:]
        ctx.user_data["_edit_task_id"] = task_id
        await q.edit_message_text(
            "📅 Enter new *date* in BDT:\n`YYYY-MM-DD`  e.g. `2026-07-20`",
            parse_mode=ParseMode.MARKDOWN)
        return ST_SCHED_EDIT_DATE

    if act == "view":
        return await show_scheduler(update, ctx)

    if act.startswith("del:"):
        task_id_to_del = act[4:]
        tasks = await _fetch_and_refresh_tasks(ctx)
        before = len(tasks)
        tasks[:] = [t for t in tasks if t.get("task_id") != task_id_to_del]
        if len(tasks) < before:
            sess(ctx)["user_record"]["scheduled_tasks"] = tasks
            token = sess(ctx)["token"]; uid = sess(ctx)["uid"]
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: SB.save_scheduled(uid, token, tasks))
            await q.answer("✅ Task deleted!", show_alert=True)
        else:
            await q.answer("Task not found.", show_alert=True)
        return await show_scheduler(update, ctx)

    if act == "new" or act.startswith("page:"):
        all_games = get_games(ctx)
        if not all_games:
            await q.edit_message_text("❌ No games.", reply_markup=back_kb("menu:sched"))
            return ST_SCHED_TASK
        # Exclude games whose validation date has expired.
        games = {gn: g for gn, g in all_games.items() if _check_game_valid(g)[0]}
        if not games:
            await q.edit_message_text(
                "⛔ *All your games have expired validation dates.*\n\n"
                "Contact admin to extend validity before scheduling tasks.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb("menu:sched"))
            return ST_SCHED_TASK
        page = int(act.split(":", 1)[1]) if act.startswith("page:") else 0
        names = sorted(games)
        page_names, page, total_pages = _paginate(names, page, GAMES_PAGE_SIZE)

        rows = [[("🔍 Search game", "search:sched")]]
        for gn in page_names:
            g      = games[gn]
            plat_e = "🍎" if (g.get("platform","android").lower()=="ios") else "🤖"
            rows.append([(f"{plat_e} {gn}", f"sg:{gn}")])
        nav = _nav_row(page, total_pages, "sched:page:")
        if nav:
            rows.append(nav)
        rows.append([("🔙 Back", "menu:sched")])
        header = (f"⏰ *New Task Schedule* — Select game:\n"
                  f"_Page {page+1}/{total_pages} · {len(names)} total_")
        await q.edit_message_text(header, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
        return ST_SCHED_TASK

    # ── New Purchase Schedule — game picker (10/page + search) ───────────────
    if act == "newpurch" or act.startswith("ppage:"):
        all_games = get_games(ctx)
        if not all_games:
            await q.edit_message_text("❌ No games.", reply_markup=back_kb("menu:sched"))
            return ST_SCHED_TASK
        games = {gn: g for gn, g in all_games.items() if _check_game_valid(g)[0]}
        if not games:
            await q.edit_message_text(
                "⛔ *All your games have expired validation dates.*\n\n"
                "Contact admin to extend validity before scheduling purchases.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb("menu:sched"))
            return ST_SCHED_TASK
        page = int(act.split(":", 1)[1]) if act.startswith("ppage:") else 0
        names = sorted(games)
        page_names, page, total_pages = _paginate(names, page, PURCH_SCHED_PAGE_SIZE)
        rows = [[("🔍 Search game", "search:purch")]]
        for gn in page_names:
            g      = games[gn]
            plat_e = "🍎" if (g.get("platform","android").lower()=="ios") else "🤖"
            rows.append([(f"{plat_e} {gn}", f"sp_game:{gn}")])
        nav = _nav_row(page, total_pages, "sched:ppage:")
        if nav:
            rows.append(nav)
        rows.append([("🔙 Back", "menu:sched")])
        header = (f"💰 *New Purchase Schedule* — Select game:\n"
                  f"_Page {page+1}/{total_pages} · {len(names)} total_")
        await q.edit_message_text(header, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
        return ST_SCHED_PURCH_GAME

    # ── Same-game repeat purchase — handled here because sched_cb matches
    # ^sched: before sched_purch_samegame_cb in every state's handler list. ──
    if act == "purch_samegame":
        return await sched_purch_samegame_cb(update, ctx)

    return ST_SCHED_TASK


# ── Edit pending: date input ──────────────────────────────────────────────────
async def sched_edit_date_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = update.message.text.strip()
    try:
        datetime.strptime(d, "%Y-%m-%d")
        ctx.user_data["_edit_date"] = d
    except:
        await update.message.reply_text("⚠️ Invalid date. Use `YYYY-MM-DD`:",
                                        parse_mode=ParseMode.MARKDOWN)
        return ST_SCHED_EDIT_DATE
    await update.message.reply_text(
        "⏰ Enter new *time* in BDT:\n`HH:MM`  e.g. `14:30`", parse_mode=ParseMode.MARKDOWN)
    return ST_SCHED_EDIT_TIME

async def sched_edit_time_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    try:
        datetime.strptime(t, "%H:%M")
    except:
        await update.message.reply_text("⚠️ Invalid time. Use `HH:MM` (24h):",
                                        parse_mode=ParseMode.MARKDOWN)
        return ST_SCHED_EDIT_TIME

    task_id = ctx.user_data.pop("_edit_task_id", None)
    date_s  = ctx.user_data.pop("_edit_date", None)

    bdt_dt  = datetime.strptime(f"{date_s} {t}", "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone(BDT))
    utc_dt  = bdt_dt.astimezone(timezone.utc)
    new_fire = utc_dt.isoformat().replace("+00:00","Z")

    tasks = await _fetch_and_refresh_tasks(ctx)
    updated = False
    for task in tasks:
        if task.get("task_id") == task_id and task.get("status") == "pending":
            task["fire_at_utc"] = new_fire
            updated = True
            break

    if updated:
        sess(ctx)["user_record"]["scheduled_tasks"] = tasks
        token = sess(ctx)["token"]; uid = sess(ctx)["uid"]
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: SB.save_scheduled(uid, token, tasks))
        await update.message.reply_text(
            f"✅ *Schedule updated!*\n📅 New time: *{date_s} {t} BDT*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("⏰ View Scheduler", "menu:sched"), ("🏠 Menu", "menu:back")]]))
    else:
        await update.message.reply_text("❌ Task not found or already executed.")
    return ST_SCHED_TASK


async def sched_game_pick_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q  = update.callback_query
    await q.answer()
    gn = q.data.split(":", 1)[1]
    ctx.user_data["_sg_game"] = gn
    games = get_games(ctx)
    g     = games.get(gn, {})
    # Re-check validity here too, in case the game expired between showing
    # the list and this tap (e.g. an admin just expired it).
    ok, reason = _check_game_valid(g)
    if not ok:
        await q.edit_message_text(reason, reply_markup=back_kb("sched:new"))
        return ST_SCHED_TASK
    tasks = sorted(g.get("tasks",[]), key=_task_sort_key)
    if not tasks:
        await q.edit_message_text("❌ No tasks in this game.", reply_markup=back_kb("sched:new"))
        return ST_SCHED_TASK
    rows = [[(t["id_name"], f"st:{t['id_name']}") for t in tasks[i:i+2]]
            for i in range(0, len(tasks), 2)]
    rows.append([("🔙 Back", "sched:new")])
    await q.edit_message_text("✅ *Select Task:*",
                              parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_SCHED_TASK

async def sched_task_pick_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query
    await q.answer()
    task_id = q.data.split(":", 1)[1]
    ctx.user_data["_sg_task"] = task_id
    gn      = ctx.user_data.get("_sg_game")
    games   = get_games(ctx)
    g       = games.get(gn, {})
    task    = next((t for t in g.get("tasks",[]) if t["id_name"]==task_id), None)
    if task and "@" in task.get("event_name",""):
        await q.edit_message_text(
            f"🔢 *{task_id}* requires a number — enter it (e.g. `20`):",
            parse_mode=ParseMode.MARKDOWN)
        return ST_SCHED_NUM
    ctx.user_data["_sg_task_num"] = None
    return await _sched_pick_uid(update, ctx)

async def sched_num_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    num = update.message.text.strip()
    if not num.isdigit():
        await update.message.reply_text("⚠️ Whole numbers only:", parse_mode=ParseMode.MARKDOWN)
        return ST_SCHED_NUM
    ctx.user_data["_sg_task_num"] = num
    return await _sched_pick_uid(update, ctx)

async def _sched_pick_uid(update, ctx, page: int = 0):
    gn    = ctx.user_data.get("_sg_game")
    games = get_games(ctx)
    g     = games.get(gn, {})
    uids  = g.get("af_uids", [])
    if not uids:
        msg = "❌ No AF UIDs in this game."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return ST_MAIN
    page_list, page, total_pages = _paginate(uids, page, UIDS_PAGE_SIZE)
    rows = [[(f"{af.get('name','?')} {af.get('uid','?')[:12]}…", f"su:{af['uid']}")]
            for af in page_list]
    nav = _nav_row(page, total_pages, "scheduidpage:")
    if nav:
        rows.append(nav)
    txt = f"👤 *Select AF UID:*\n_Page {page+1}/{total_pages} · {len(uids)} total_"
    if update.callback_query:
        await update.callback_query.edit_message_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    else:
        await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_SCHED_UID

async def sched_uid_page_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":", 1)[1])
    return await _sched_pick_uid(update, ctx, page=page)

async def sched_uid_pick_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["_sg_uid"] = q.data.split(":", 1)[1]
    gn    = ctx.user_data.get("_sg_game")
    games = get_games(ctx)
    g     = games.get(gn, {})
    af    = next((a for a in g.get("af_uids",[]) if a.get("uid")==ctx.user_data["_sg_uid"]), {})
    ctx.user_data["_sg_gaid"] = af.get("gaid","")
    await q.edit_message_text(
        "📅 Enter date in *BDT*:\n`YYYY-MM-DD`  e.g. `2026-07-15`",
        parse_mode=ParseMode.MARKDOWN)
    return ST_SCHED_DATE

async def sched_date_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = update.message.text.strip()
    try:
        datetime.strptime(d, "%Y-%m-%d")
        ctx.user_data["_sg_date"] = d
    except:
        await update.message.reply_text("⚠️ Invalid date. Use `YYYY-MM-DD`:",
                                        parse_mode=ParseMode.MARKDOWN)
        return ST_SCHED_DATE
    await update.message.reply_text(
        "⏰ Enter time in *BDT*:\n`HH:MM`  e.g. `14:30`", parse_mode=ParseMode.MARKDOWN)
    return ST_SCHED_TIME

async def sched_time_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    try:
        datetime.strptime(t, "%H:%M")
        ctx.user_data["_sg_time"] = t
    except:
        await update.message.reply_text("⚠️ Invalid time. Use `HH:MM` (24h):",
                                        parse_mode=ParseMode.MARKDOWN)
        return ST_SCHED_TIME

    date_s  = ctx.user_data.get("_sg_date")
    gn      = ctx.user_data.get("_sg_game")
    task_id = ctx.user_data.get("_sg_task")
    num     = ctx.user_data.get("_sg_task_num")
    uid_v   = ctx.user_data.get("_sg_uid")
    gaid_v  = ctx.user_data.get("_sg_gaid")
    games   = await _fetch_and_refresh_games(ctx)
    g       = games.get(gn, {})

    # Final re-check right before writing to DB — the game may have expired
    # in the time it took to fill in date/time.
    ok, reason = _check_game_valid(g)
    if not ok:
        for k in ("_sg_game","_sg_task","_sg_task_num","_sg_uid","_sg_gaid","_sg_date","_sg_time"):
            ctx.user_data.pop(k, None)
        await update.message.reply_text(reason)
        return ST_MAIN

    task    = next((tk for tk in g.get("tasks",[]) if tk["id_name"]==task_id), {})
    ev_name = task.get("event_name","").replace("@", num) if num else task.get("event_name","")
    display = f"{task_id} {num}" if num else task_id

    bdt_dt  = datetime.strptime(f"{date_s} {t}", "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone(BDT))
    utc_dt  = bdt_dt.astimezone(timezone.utc)
    fire_at = utc_dt.isoformat().replace("+00:00","Z")

    new_task = {
        "task_id":      uuid.uuid4().hex[:8],
        "task_id_name": display,
        "event_name":   ev_name,
        "game":         gn,
        "af_uid":       uid_v,
        "gaid":         gaid_v,
        "fire_at_utc":  fire_at,
        "status":       "pending",
        "result":       "Waiting…",
        "retries":      0,
    }
    # ── FIX: previously this read the stale in-memory session cache
    # (sess(ctx)["user_record"]["scheduled_tasks"]), which could be minutes
    # old. If the background scheduler_tick (or the desktop app) had since
    # marked another task "sent"/"error" in the DB, appending to this stale
    # list and saving it back would SILENTLY REVERT that task to whatever
    # stale status it had in memory — e.g. a task that just fired and was
    # marked "sent" in the DB would be overwritten back to "pending" the
    # moment the user scheduled a new task. This is why users reported
    # "task sent successfully but bot still shows pending", and why a
    # "Clear Done" done elsewhere could reappear. Always fetch fresh
    # immediately before this read-modify-write.
    sched = await _fetch_and_refresh_tasks(ctx)
    sched.append(new_task)
    sess(ctx)["user_record"]["scheduled_tasks"] = sched
    token = sess(ctx)["token"]; uidv = sess(ctx)["uid"]
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.save_scheduled(uidv, token, sched))

    for k in ("_sg_game","_sg_task","_sg_task_num","_sg_uid","_sg_gaid","_sg_date","_sg_time"):
        ctx.user_data.pop(k, None)

    await update.message.reply_text(
        f"✅ *Scheduled!*\n\n🎮 Game: *{gn}*\n✅ Task: *{display}*\n"
        f"📅 Fire at: *{date_s} {t} BDT*\n👤 UID: `{uid_v[:16]}…`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([
            [("➕ Add Another Schedule", "sched:new")],
            [("⏰ View Scheduler", "menu:sched"), ("🏠 Menu", "menu:back")],
        ]))
    return ST_SCHED_TASK


# ═══════════════════════════════════════════════════════════════════════════════
#  Scheduler — Purchase scheduling flow
# ═══════════════════════════════════════════════════════════════════════════════
async def sched_purch_game_pick_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User picked a game for the new purchase schedule."""
    q  = update.callback_query
    await q.answer()
    gn = q.data.split(":", 1)[1]
    ctx.user_data["_sp_game"] = gn
    games = get_games(ctx)
    g     = games.get(gn, {})
    ok, reason = _check_game_valid(g)
    if not ok:
        await q.edit_message_text(reason, reply_markup=back_kb("sched:newpurch"))
        return ST_SCHED_PURCH_GAME

    uids = g.get("af_uids", [])
    if not uids:
        await q.edit_message_text("❌ No AF UIDs in this game.",
                                  reply_markup=back_kb("sched:newpurch"))
        return ST_SCHED_PURCH_GAME
    return await _sched_purch_pick_uid(update, ctx)


async def _sched_purch_pick_uid(update, ctx, page: int = 0):
    """Show AF UID picker for purchase schedule."""
    gn    = ctx.user_data.get("_sp_game")
    games = get_games(ctx)
    g     = games.get(gn, {})
    uids  = g.get("af_uids", [])
    if not uids:
        msg = "❌ No AF UIDs in this game."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.message.reply_text(msg)
        return ST_MAIN
    page_list, page, total_pages = _paginate(uids, page, UIDS_PAGE_SIZE)
    rows = [[(f"{af.get('name','?')} {af.get('uid','?')[:12]}…", f"sp_uid:{af['uid']}")]
            for af in page_list]
    nav = _nav_row(page, total_pages, "schedpurchuidpage:")
    if nav:
        rows.append(nav)
    txt = f"👤 *Select AF UID:*\n_Page {page+1}/{total_pages} · {len(uids)} total_"
    if update.callback_query:
        await update.callback_query.edit_message_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    else:
        await update.message.reply_text(
            txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_SCHED_PURCH_UID


async def sched_purch_uid_page_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    page = int(q.data.split(":", 1)[1])
    return await _sched_purch_pick_uid(update, ctx, page=page)


async def sched_purch_uid_pick_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User picked an AF UID — ask for purchase amount."""
    q = update.callback_query
    await q.answer()
    ctx.user_data["_sp_uid"] = q.data.split(":", 1)[1]
    gn    = ctx.user_data.get("_sp_game")
    games = get_games(ctx)
    g     = games.get(gn, {})
    af    = next((a for a in g.get("af_uids", []) if a.get("uid") == ctx.user_data["_sp_uid"]), {})
    ctx.user_data["_sp_gaid"] = af.get("gaid", "")
    await q.edit_message_text(
        "💵 *Purchase Amount*\nEnter amount (e.g. `4.99`):",
        parse_mode=ParseMode.MARKDOWN)
    return ST_SCHED_PURCH_AMT


async def sched_purch_amt_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Receive purchase amount — then skip directly to date (currency/product ID removed)."""
    raw = update.message.text.strip()
    try:
        amt = float(raw)
        assert amt > 0
        ctx.user_data["_sp_amt"] = amt
    except Exception:
        await update.message.reply_text("⚠️ Invalid. Enter a positive number like `4.99`:",
                                        parse_mode=ParseMode.MARKDOWN)
        return ST_SCHED_PURCH_AMT
    # Currency and Product ID are no longer asked — default to USD / no product.
    ctx.user_data["_sp_cur"] = "USD"
    ctx.user_data["_sp_pid"] = ""
    await update.message.reply_text(
        "📅 Enter *date* in BDT:\n`YYYY-MM-DD`  e.g. `2026-07-15`",
        parse_mode=ParseMode.MARKDOWN)
    return ST_SCHED_PURCH_DATE


# Currency and Product ID steps are intentionally removed from the scheduled
# purchase flow. Currency defaults to USD; product_id is left empty.
# The functions below are kept as stubs for backward compatibility with any
# in-flight session state that may still reference them.
async def sched_purch_cur_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Stub — currency picker removed; routes to date entry."""
    q = update.callback_query
    await q.answer()
    ctx.user_data["_sp_cur"] = "USD"
    ctx.user_data["_sp_pid"] = ""
    await q.edit_message_text(
        "📅 Enter *date* in BDT:\n`YYYY-MM-DD`  e.g. `2026-07-15`",
        parse_mode=ParseMode.MARKDOWN)
    return ST_SCHED_PURCH_DATE


async def sched_purch_pid_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Stub — product-ID step removed; routes to date entry."""
    ctx.user_data["_sp_pid"] = ""
    await update.message.reply_text(
        "📅 Enter *date* in BDT:\n`YYYY-MM-DD`  e.g. `2026-07-15`",
        parse_mode=ParseMode.MARKDOWN)
    return ST_SCHED_PURCH_DATE


async def sched_purch_date_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Receive scheduled date for purchase."""
    d = update.message.text.strip()
    try:
        datetime.strptime(d, "%Y-%m-%d")
        ctx.user_data["_sp_date"] = d
    except Exception:
        await update.message.reply_text("⚠️ Invalid date. Use `YYYY-MM-DD`:",
                                        parse_mode=ParseMode.MARKDOWN)
        return ST_SCHED_PURCH_DATE
    await update.message.reply_text(
        "⏰ Enter *time* in BDT:\n`HH:MM`  e.g. `14:30`",
        parse_mode=ParseMode.MARKDOWN)
    return ST_SCHED_PURCH_TIME


async def sched_purch_time_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Receive scheduled time, build the purchase task, save it."""
    t = update.message.text.strip()
    try:
        datetime.strptime(t, "%H:%M")
    except Exception:
        await update.message.reply_text("⚠️ Invalid time. Use `HH:MM` (24h):",
                                        parse_mode=ParseMode.MARKDOWN)
        return ST_SCHED_PURCH_TIME

    date_s  = ctx.user_data.get("_sp_date")
    gn      = ctx.user_data.get("_sp_game")
    uid_v   = ctx.user_data.get("_sp_uid")
    gaid_v  = ctx.user_data.get("_sp_gaid")
    amt     = ctx.user_data.get("_sp_amt", 0.99)
    cur     = ctx.user_data.get("_sp_cur", "USD")
    pid     = ctx.user_data.get("_sp_pid", "")

    games = await _fetch_and_refresh_games(ctx)
    g     = games.get(gn, {})
    ok, reason = _check_game_valid(g)
    if not ok:
        for k in ("_sp_game","_sp_uid","_sp_gaid","_sp_amt",
                  "_sp_cur","_sp_pid","_sp_date"):
            ctx.user_data.pop(k, None)
        await update.message.reply_text(reason)
        return ST_MAIN

    bdt_dt  = datetime.strptime(f"{date_s} {t}", "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone(BDT))
    utc_dt  = bdt_dt.astimezone(timezone.utc)
    fire_at = utc_dt.isoformat().replace("+00:00", "Z")

    display = f"Purchase ${amt:.2f} {cur}"
    new_task = {
        "task_id":      uuid.uuid4().hex[:8],
        "task_id_name": display,
        "type":         "purchase",
        "event_name":   "af_purchase",
        "game":         gn,
        "af_uid":       uid_v,
        "gaid":         gaid_v,
        "amount":       amt,
        "currency":     cur,
        "product_id":   pid,
        "fire_at_utc":  fire_at,
        "status":       "pending",
        "result":       "Waiting…",
        "retries":      0,
    }

    sched = await _fetch_and_refresh_tasks(ctx)
    sched.append(new_task)
    sess(ctx)["user_record"]["scheduled_tasks"] = sched
    token = sess(ctx)["token"]; uidv = sess(ctx)["uid"]
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.save_scheduled(uidv, token, sched))

    saved_gn  = gn    # keep for same-game option
    saved_uid = uid_v
    saved_gaid= gaid_v
    for k in ("_sp_uid","_sp_gaid","_sp_amt",
              "_sp_cur","_sp_pid","_sp_date"):
        ctx.user_data.pop(k, None)
    # _sp_game intentionally NOT popped here — needed for "same game" repeat option.

    await update.message.reply_text(
        f"✅ *Purchase Scheduled!*\n\n"
        f"🎮 Game: *{saved_gn}*\n"
        f"💰 Amount: *${amt:.2f}*"
        f"\n📅 Fire at: *{date_s} {t} BDT*\n"
        f"👤 UID: `{saved_uid[:16]}…`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([
            [("🔁 Another Purchase — Same Game",     "sched:purch_samegame")],
            [("💰 Another Purchase — Different Game", "sched:newpurch")],
            [("➕ Add Task Schedule",                 "sched:new")],
            [("⏰ View Scheduler", "menu:sched"), ("🏠 Menu", "menu:back")],
        ]))
    return ST_SCHED_TASK


# ═══════════════════════════════════════════════════════════════════════════════
#  Scheduler — "Same game" repeat purchase (only asks amount + date + time)
# ═══════════════════════════════════════════════════════════════════════════════
async def sched_purch_samegame_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Re-use the game+UID already in session; only ask amount → date → time."""
    q  = update.callback_query
    await q.answer()
    gn = ctx.user_data.get("_sp_game")
    if not gn:
        # _sp_game was cleared — fall back to full picker.
        q.data = "sched:newpurch"
        return await sched_cb(update, ctx)
    games = get_games(ctx)
    g     = games.get(gn, {})
    ok, reason = _check_game_valid(g)
    if not ok:
        ctx.user_data.pop("_sp_game", None)
        await q.edit_message_text(reason, reply_markup=back_kb("sched:newpurch"))
        return ST_SCHED_PURCH_GAME
    uids = g.get("af_uids", [])
    if not uids:
        ctx.user_data.pop("_sp_game", None)
        await q.edit_message_text("❌ No AF UIDs in this game.",
                                  reply_markup=back_kb("sched:newpurch"))
        return ST_SCHED_PURCH_GAME
    # Mark that we will reuse the existing UID picker (or if only one UID,
    # auto-select it).
    ctx.user_data["_sp_cur"] = "USD"
    ctx.user_data["_sp_pid"] = ""
    ctx.user_data["_sp_same_game"] = True
    return await _sched_purch_pick_uid(update, ctx)


# ═══════════════════════════════════════════════════════════════════════════════
#  Dashboard
# ═══════════════════════════════════════════════════════════════════════════════
async def show_dashboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    games  = get_games(ctx)
    sched  = _pj(sess(ctx).get("user_record",{}).get("scheduled_tasks"), [])
    total_tasks     = sum(g.get("stats",{}).get("tasks",0) for g in games.values())
    total_purchases = sum(g.get("stats",{}).get("purchases",0) for g in games.values())
    pending  = sum(1 for t in sched if t.get("status")=="pending")
    sent_sch = sum(1 for t in sched if t.get("status")=="sent")

    lines = []
    for gn, g in sorted(games.items()):
        plat_e = "🍎" if (g.get("platform","android").lower()=="ios") else "🤖"
        stats  = g.get("stats",{})
        act    = "🟢" if g.get("has_activity") else "⚪"
        lines.append(f"{act} {plat_e} *{gn}*\n   Tasks: {stats.get('tasks',0)}  "
                     f"Purchases: {stats.get('purchases',0)}")

    text = (f"📊 *Dashboard*\n\n🎮 Games: {len(games)}\n"
            f"✅ Total Tasks: {total_tasks}\n💰 Total Purchases: {total_purchases}\n"
            f"⏳ Scheduled Pending: {pending}\n📨 Scheduled Sent: {sent_sch}\n\n"
            + ("\n\n".join(lines) if lines else "_No games yet_"))
    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                              reply_markup=back_kb("menu:back"))
    return ST_MAIN


# ═══════════════════════════════════════════════════════════════════════════════
#  Explore Games — reads from admin user's games, visible to ALL (no login needed)
#  Shows: game name, platform, iOS App Store ID or Android package
#  Contact Admin to Buy button → @whoiam31
# ═══════════════════════════════════════════════════════════════════════════════
EXPLORE_PAGE_SIZE = 6

async def _render_explore(dest, ctx, page: int, edit: bool, search_query: str = None):
    """Render one page of Explore Games. `dest` is either a callback_query
    (edit=True) or a Message (edit=False, i.e. reply_text)."""
    logged_in = is_logged_in(ctx)
    back_target = "menu:back" if logged_in else "guest:back"

    try:
        admin_games = await asyncio.get_running_loop().run_in_executor(
            None, SB.get_admin_games)
    except Exception:
        admin_games = {}

    contact_btn = (f"📞 Contact Admin to Buy — {ADMIN_CONTACT}",
                   f"url:https://t.me/{ADMIN_CONTACT.lstrip('@')}")

    names = sorted(admin_games.keys())
    if search_query:
        names = [n for n in names if search_query.lower() in n.lower()]

    async def _send(text, rows):
        markup = kb(rows)
        if edit:
            await dest.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
        else:
            await dest.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)

    if not names:
        rows = []
        if not logged_in:
            rows.append([("🔐 Login", "guest:login")])
        rows.append([("🔍 Search", "explore:search")])
        if search_query:
            rows.append([("❌ Clear Search", "explore:clear")])
        rows.append([contact_btn])
        rows.append([("🔙 Back", back_target)])
        if search_query:
            text = f"🌐 *Explore Games*\n\n_No games match_ `{search_query}`_._"
        else:
            text = ("🌐 *Explore Games*\n\n_No games available right now._\n\n"
                    "Contact admin to get access to games:")
        await _send(text, rows)
        return ST_MAIN if logged_in else ST_GUEST

    page_names, page, total_pages = _paginate(names, page, EXPLORE_PAGE_SIZE)

    lines = []
    for gn in page_names:
        g = admin_games[gn]
        plat = (g.get("platform") or "android").lower()
        app_id = g.get("app_id", "")
        if plat == "ios":
            plat_label = "🍎 iOS"
            store_info = f"App Store ID: `{app_id}`" if app_id else ""
        else:
            plat_label = "🤖 Android"
            store_info = f"Package: `{app_id}`" if app_id else ""
        line = f"🎮 *{gn}*\n   📱 {plat_label}"
        if store_info:
            line += f"\n   {store_info}"
        lines.append(line)

    header = "🌐 *Explore Games*"
    if search_query:
        header += f"\n🔍 Results for `{search_query}`"
    header += f"\n_Page {page+1}/{total_pages} · {len(names)} game(s)_\n\n"
    text = header + "\n\n".join(lines)

    rows = []
    nav = _nav_row(page, total_pages, "explore:page:")
    if nav:
        rows.append(nav)
    search_row = [("🔍 Search", "explore:search")]
    if search_query:
        search_row.append(("❌ Clear", "explore:clear"))
    rows.append(search_row)
    if not logged_in:
        rows.append([("🔐 Login to Your Account", "guest:login")])
    rows.append([contact_btn])
    rows.append([("🔙 Back", back_target)])

    await _send(text, rows)
    return ST_MAIN if logged_in else ST_GUEST


async def show_explore(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    ctx.user_data.pop("_explore_search", None)
    return await _render_explore(q, ctx, page=0, edit=True)

async def explore_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    act = q.data.split(":", 1)[1]

    if act == "login":
        await q.edit_message_text(
            "🔐 *Login*\n\nEnter your *email address*:",
            parse_mode=ParseMode.MARKDOWN)
        return ST_EMAIL

    if act.startswith("page:"):
        page = int(act.split(":", 1)[1])
        return await _render_explore(q, ctx, page, edit=True,
                                     search_query=ctx.user_data.get("_explore_search"))

    if act == "search":
        await q.edit_message_text(
            "🔍 *Search Games*\n\nType part of the game name:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("❌ Cancel", "explore:cancelsearch")]]))
        return ST_EXPLORE_SEARCH

    if act == "clear":
        ctx.user_data.pop("_explore_search", None)
        return await _render_explore(q, ctx, 0, edit=True)

    if act == "cancelsearch":
        return await _render_explore(q, ctx, 0, edit=True,
                                     search_query=ctx.user_data.get("_explore_search"))

    return ST_MAIN if is_logged_in(ctx) else ST_GUEST

async def explore_search_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    if not query_text:
        await update.message.reply_text("⚠️ Type at least one character:")
        return ST_EXPLORE_SEARCH
    ctx.user_data["_explore_search"] = query_text
    return await _render_explore(update.message, ctx, 0, edit=False, search_query=query_text)


# ═══════════════════════════════════════════════════════════════════════════════
#  Settings
# ═══════════════════════════════════════════════════════════════════════════════
async def show_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q  = update.callback_query
    ur = sess(ctx).get("user_record", {})
    games = get_games(ctx)
    total_uids = sum(len(g.get("af_uids",[])) for g in games.values())
    sched = _pj(ur.get("scheduled_tasks"), [])
    text = (f"⚙️ *Settings & Account*\n\n"
            f"📧 Email: `{ur.get('email','?')}`\n"
            f"🎮 Games: {len(games)}\n👤 Total AF UIDs: {total_uids}\n"
            f"⏰ Scheduled Tasks: {len(sched)}\n\n"
            f"Tap *Refresh* to reload your data from the server.")
    await q.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([
            [("🔄 Refresh Data", "settings:refresh")],
            [("🔙 Back", "menu:back")],
        ]))
    return ST_MAIN

async def settings_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    act = q.data.split(":", 1)[1]
    if act == "refresh":
        await q.edit_message_text("⏳ Refreshing…")
        try:
            token = sess(ctx)["token"]; uid = sess(ctx)["uid"]
            ur    = await asyncio.get_running_loop().run_in_executor(
                None, lambda: SB.get_user_record(uid, token))
            if ur:
                ur["games"]           = _pj(ur.get("games"), {})
                ur["scheduled_tasks"] = _pj(ur.get("scheduled_tasks"), [])
                sess(ctx)["user_record"] = ur
        except Exception as e:
            await q.edit_message_text(f"❌ Refresh failed: {e}")
            return ST_MAIN
        await q.edit_message_text("✅ Data refreshed!", reply_markup=back_kb("menu:back"))
    return ST_MAIN


# ═══════════════════════════════════════════════════════════════════════════════
#  Admin Panel
# ═══════════════════════════════════════════════════════════════════════════════
async def show_admin_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(ctx):
        await update.callback_query.edit_message_text("🚫 Admin access required.")
        return ST_MAIN
    q = update.callback_query

    # Show real-time user count (fixed — uses correct service-role headers now)
    try:
        users = await asyncio.get_running_loop().run_in_executor(None, SB.admin_list_users)
        user_count = len(users)
    except Exception:
        user_count = 0

    await q.edit_message_text(
        f"👑 *Admin Panel*\n👥 Total Users: *{user_count}*\n\nSelect an action:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([
            [("👥 List Users",     "adm:users"),     ("📊 Global Stats",   "adm:stats")],
            [("🔍 User Details",   "adm:udetail"),   ("🚫 Ban/Unban",      "adm:ban")],
            [("📢 Broadcast",      "adm:broadcast"), ("📋 Manage Games",   "adm:mgames")],
            [("📋 Copy Game",      "adm:copygame"),  ("➕ Add Game",       "adm:addgame")],
            [("➕ Add User",       "adm:adduser")],
            [("🔙 Back",           "menu:back")],
        ]))
    return ST_ADMIN_MAIN

async def admin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    if not is_admin(ctx):
        await q.answer("🚫 No access", show_alert=True)
        return ST_MAIN
    act = q.data.split(":", 1)[1]

    # ── List all users ────────────────────────────────────────────────────────
    if act == "users":
        await q.edit_message_text("⏳ Loading users…")
        users = await asyncio.get_running_loop().run_in_executor(None, SB.admin_list_users)
        lines = []
        for u in users[:40]:
            email = u.get("email","?")
            adm   = "👑" if u.get("is_admin") else "👤"
            ban   = "🚫" if u.get("banned") else ""
            games = _pj(u.get("games"), {})
            sched = _pj(u.get("scheduled_tasks"), [])
            pending = sum(1 for t in sched if t.get("status")=="pending")
            lines.append(f"{adm}{ban} {email}\n   🎮 {len(games)} games  ⏳ {pending} pending")
        text = f"👥 *Users ({len(users)} total)*\n\n" + "\n\n".join(lines)
        await q.edit_message_text(text[:4000], parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=back_kb("adm:back"))
        return ST_ADMIN_MAIN

    # ── Global stats ──────────────────────────────────────────────────────────
    if act == "stats":
        await q.edit_message_text("⏳ Loading stats…")
        users = await asyncio.get_running_loop().run_in_executor(None, SB.admin_list_users)
        total_games = sum(len(_pj(u.get("games"),{})) for u in users)
        total_tasks = sum(
            g.get("stats",{}).get("tasks",0)
            for u in users
            for g in _pj(u.get("games"),{}).values())
        total_purch = sum(
            g.get("stats",{}).get("purchases",0)
            for u in users
            for g in _pj(u.get("games"),{}).values())
        total_uids  = sum(
            len(g.get("af_uids",[]))
            for u in users
            for g in _pj(u.get("games"),{}).values())
        admins  = sum(1 for u in users if u.get("is_admin"))
        banned  = sum(1 for u in users if u.get("banned"))
        all_sched = sum(
            len(_pj(u.get("scheduled_tasks"),[]))
            for u in users)
        await q.edit_message_text(
            f"📊 *Global Stats*\n\n"
            f"👥 Total Users: {len(users)}\n"
            f"👑 Admins: {admins}  🚫 Banned: {banned}\n"
            f"🎮 Total Games: {total_games}\n"
            f"👤 Total AF UIDs: {total_uids}\n"
            f"✅ Total Tasks Sent: {total_tasks}\n"
            f"💰 Total Purchases: {total_purch}\n"
            f"⏰ Total Scheduled: {all_sched}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb("adm:back"))
        return ST_ADMIN_MAIN

    # ── User details ──────────────────────────────────────────────────────────
    if act == "udetail":
        await q.edit_message_text(
            "🔍 *User Details*\n\nEnter the user's email (partial match works):",
            parse_mode=ParseMode.MARKDOWN)
        ctx.user_data["_adm_action"] = "udetail"
        return ST_ADMIN_USER_SEARCH

    # ── Ban/Unban ─────────────────────────────────────────────────────────────
    if act == "ban":
        await q.edit_message_text(
            "🚫 *Ban / Unban*\n\nEnter the user's email to toggle ban status:",
            parse_mode=ParseMode.MARKDOWN)
        ctx.user_data["_adm_action"] = "ban"
        return ST_ADMIN_BAN

    # ── Broadcast ─────────────────────────────────────────────────────────────
    if act == "broadcast":
        await q.edit_message_text(
            "📢 *Broadcast*\n\nEnter the message to send to all users.\n"
            "(Only users who have linked their Telegram account will receive it.)",
            parse_mode=ParseMode.MARKDOWN)
        return ST_ADMIN_BROADCAST

    # ── Manage Games — entry ─────────────────────────────────────────────────
    if act == "mgames":
        await q.edit_message_text(
            "📋 *Manage Games*\n\nEnter user email to manage their games:",
            parse_mode=ParseMode.MARKDOWN)
        ctx.user_data["_adm_action"] = "mgames"
        return ST_ADMIN_MANAGE_GAMES_USER

    # ── Copy Game — entry ────────────────────────────────────────────────────
    if act == "copygame":
        await q.edit_message_text(
            "📋 *Copy Game*\n\nEnter the *source* user email (copy game FROM):",
            parse_mode=ParseMode.MARKDOWN)
        return ST_ADMIN_COPY_GAME_SRC

    # ── Add Game to Users — entry ────────────────────────────────────────────
    if act == "addgame":
        await q.edit_message_text(
            "➕ *Add Game to Users*\n\nWho should receive this game?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([
                [("🌍 All Users", "adm:agtarget:all")],
                [("🔍 Specific User", "adm:agtarget:one")],
                [("❌ Cancel", "adm:back")],
            ]))
        return ST_ADMIN_MAIN

    if act.startswith("agtarget:"):
        target = act.split(":", 1)[1]
        if target == "all":
            ctx.user_data["_ag_target"] = "all"
            return await _show_addgame_platform(q, ctx)
        await q.edit_message_text(
            "🔍 *Add Game — Specific User*\n\nEnter the user's email:",
            parse_mode=ParseMode.MARKDOWN)
        return ST_ADDGAME_EMAIL

    if act.startswith("agpick:"):
        target_uid = act.split(":", 1)[1]
        ctx.user_data["_ag_target"] = target_uid
        return await _show_addgame_platform(q, ctx)

    if act.startswith("agplat:"):
        plat = act.split(":", 1)[1]
        ctx.user_data["_ag_platform"] = plat
        await q.edit_message_text(
            "📝 *Add Game — Details*\n\n"
            "Send the game details in this exact format, one line, "
            "separated by `|`:\n\n"
            "`GameName | DEV_KEY | APP_ID | ValidationDate(YYYY-MM-DD or -) "
            "| AF_UID_Limit(number or -) | TaskIdName(or -) | EventName(or -)`\n\n"
            "*Example:*\n"
            "`Coin Master | abc123devkey | com.example.app | - | 3 | "
            "Reach Level @ | level_@_done`\n\n"
            "_GameName, DEV_KEY, APP_ID are required. Everything else can "
            "be `-` to use defaults / skip. Use @ in Event Name as a "
            "number placeholder if needed._",
            parse_mode=ParseMode.MARKDOWN)
        return ST_ADDGAME_DETAILS

    # ── Add User — entry ─────────────────────────────────────────────────────
    if act == "adduser":
        await q.edit_message_text(
            "➕ *Add New User*\n\n📧 Enter the email for the new account:",
            parse_mode=ParseMode.MARKDOWN)
        return ST_ADDUSER_EMAIL

    # ── View a user's AF UIDs (per game, admin) ──────────────────────────────
    if act.startswith("uafuids:"):
        target_uid = act.split(":", 1)[1]
        return await _show_admin_uafuids_gamepick(q, ctx, target_uid)

    if act.startswith("uafg:"):
        rest       = act[len("uafg:"):]
        target_uid, gn = rest.split(":", 1)
        return await _show_admin_uafuids_list(q, ctx, target_uid, gn, page=0)

    if act.startswith("uafnav:"):
        rest             = act[len("uafnav:"):]
        target_uid, tail = rest.split(":", 1)
        gn, page_str     = tail.rsplit(":", 1)
        return await _show_admin_uafuids_list(q, ctx, target_uid, gn, page=int(page_str))

    # ── View a user's Task History (admin, all games, newest first) ─────────
    if act.startswith("utasks:"):
        target_uid = act.split(":", 1)[1]
        return await _show_admin_task_history(q, ctx, target_uid, page=0)

    if act.startswith("utasknav:"):
        rest = act[len("utasknav:"):]
        target_uid, page_str = rest.split(":", 1)
        return await _show_admin_task_history(q, ctx, target_uid, page=int(page_str))

    # ── View a user's Purchase History (admin, all games, newest first) ─────
    if act.startswith("upurch:"):
        target_uid = act.split(":", 1)[1]
        return await _show_admin_purchase_history(q, ctx, target_uid, page=0)

    if act.startswith("upurchnav:"):
        rest = act[len("upurchnav:"):]
        target_uid, page_str = rest.split(":", 1)
        return await _show_admin_purchase_history(q, ctx, target_uid, page=int(page_str))

    # ── Back ──────────────────────────────────────────────────────────────────
    if act == "back":
        return await show_admin_menu(update, ctx)

    # ── Inline ban/unban ──────────────────────────────────────────────────────
    if act.startswith("doban:") or act.startswith("dounban:"):
        action, uid_to_toggle = act.split(":", 1)
        new_ban = (action == "doban")
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: SB.admin_update_user(uid_to_toggle, {"banned": new_ban}))
        verb = "🚫 Banned" if new_ban else "✅ Unbanned"
        await q.answer(f"{verb}!", show_alert=True)
        return await show_admin_menu(update, ctx)

    # ── Manage Games: user was picked from a multi-match list ────────────────
    if act.startswith("mgsel:") or act.startswith("mg_toggle_pre:"):
        target_uid = act.split(":", 1)[1]
        ctx.user_data["_mg_uid"] = target_uid
        ctx.user_data.pop("_mg_search", None)
        return await _show_manage_games_for_user(q, ctx, target_uid, page=0)

    # ── Manage Games: "back to game list" from a detail/sub-panel — reuses
    #    the stored uid/page/search instead of resetting them ────────────────
    if act == "mgback":
        uid = ctx.user_data.get("_mg_uid")
        if not uid:
            await q.answer("Session lost.", show_alert=True)
            return await show_admin_menu(update, ctx)
        return await _show_manage_games_for_user(q, ctx, uid, page=ctx.user_data.get("_mg_page", 0))

    # ── Manage Games: pagination on the game list ─────────────────────────────
    if act.startswith("mgpage:"):
        uid = ctx.user_data.get("_mg_uid")
        if not uid:
            await q.answer("Session lost.", show_alert=True)
            return await show_admin_menu(update, ctx)
        page = int(act.split(":", 1)[1])
        return await _show_manage_games_for_user(q, ctx, uid, page=page)

    # ── Manage Games: search prompt ───────────────────────────────────────────
    if act == "mgsearch":
        await q.edit_message_text(
            "🔍 *Search Games*\n\nType part of the game name:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("❌ Cancel", "adm:mgclear")]]))
        return ST_ADMIN_MG_SEARCH

    if act == "mgclear":
        uid = ctx.user_data.get("_mg_uid")
        ctx.user_data.pop("_mg_search", None)
        if not uid:
            await q.answer("Session lost.", show_alert=True)
            return await show_admin_menu(update, ctx)
        return await _show_manage_games_for_user(q, ctx, uid, page=0)

    # ── Manage games: toggle (add/remove) a game for a user, by list index ───
    if act.startswith("mgt:"):
        idx = int(act.split(":", 1)[1])
        target_uid = ctx.user_data.get("_mg_uid")
        page_list  = ctx.user_data.get("_mg_list") or []
        if not target_uid or idx >= len(page_list):
            await q.answer("Session expired — reopen Manage Games.", show_alert=True)
            return await show_admin_menu(update, ctx)
        game_name = page_list[idx]
        all_users = await asyncio.get_running_loop().run_in_executor(None, SB.admin_list_users)
        target_user = next((u for u in all_users if u["id"] == target_uid), None)
        if not target_user:
            await q.answer("User not found.", show_alert=True)
            return ST_ADMIN_MAIN
        target_games = _pj(target_user.get("games"), {})
        admin_games  = await asyncio.get_running_loop().run_in_executor(None, SB.get_admin_games)
        if game_name in target_games:
            del target_games[game_name]
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: SB.admin_save_games_for_uid(target_uid, target_games))
            await q.answer(f"✅ Removed '{game_name}'", show_alert=True)
        else:
            if game_name not in admin_games:
                await q.answer(f"❌ Game '{game_name}' not in admin games.", show_alert=True)
                return ST_ADMIN_MAIN
            g_template = dict(admin_games[game_name])
            g_template["af_uids"]          = []
            g_template["af_uid_additions"] = []
            g_template["stats"]            = {"tasks": 0, "purchases": 0,
                                               "last_task_event": "", "last_purchase_event": ""}
            g_template["has_activity"]     = False
            target_games[game_name] = g_template
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: SB.admin_save_games_for_uid(target_uid, target_games))
            await q.answer(f"✅ Added '{game_name}'", show_alert=True)
        return await _show_manage_games_for_user(q, ctx, target_uid, page=ctx.user_data.get("_mg_page", 0))

    # ── Manage Games: open detail panel for one assigned game, by list index ─
    if act.startswith("mgd:"):
        idx = int(act.split(":", 1)[1])
        uid       = ctx.user_data.get("_mg_uid")
        page_list = ctx.user_data.get("_mg_list") or []
        if not uid or idx >= len(page_list):
            await q.answer("Session expired — reopen Manage Games.", show_alert=True)
            return await show_admin_menu(update, ctx)
        gn = page_list[idx]
        return await _show_mg_detail(q, ctx, uid, gn)

    # ── Manage Games: edit name / dev key / app id (stateless — uses session) ─
    if act == "mgedit":
        uid, gn = ctx.user_data.get("_mg_uid"), ctx.user_data.get("_mg_game")
        if not uid or not gn:
            await q.answer("Session lost.", show_alert=True)
            return await show_admin_menu(update, ctx)
        await q.edit_message_text(
            f"✏️ *Edit Keys — {gn}*\n\n"
            f"Send the new values in ONE message, pipe-separated:\n"
            f"`GameName|DevKey|AppID`\n\n"
            f"Example:\n`{gn}|abcd1234devkey|com.example.app`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("❌ Cancel", "adm:mgdetail")]]))
        return ST_ADMIN_MG_EDIT

    # ── Manage Games: set AF UID limit ────────────────────────────────────────
    if act == "mglimit":
        uid, gn = ctx.user_data.get("_mg_uid"), ctx.user_data.get("_mg_game")
        if not uid or not gn:
            await q.answer("Session lost.", show_alert=True)
            return await show_admin_menu(update, ctx)
        await q.edit_message_text(
            f"🔢 *Set AF UID Limit — {gn}*\n\nSend a number:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("❌ Cancel", "adm:mgdetail")]]))
        return ST_ADMIN_MG_LIMIT

    # ── Manage Games: extend / set validation date ────────────────────────────
    if act == "mgdate":
        uid, gn = ctx.user_data.get("_mg_uid"), ctx.user_data.get("_mg_game")
        if not uid or not gn:
            await q.answer("Session lost.", show_alert=True)
            return await show_admin_menu(update, ctx)
        await q.edit_message_text(
            f"📅 *Validation Date — {gn}*\n\n"
            f"Send a date as `YYYY-MM-DD`, or send `clear` for unlimited:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("❌ Cancel", "adm:mgdetail")]]))
        return ST_ADMIN_MG_DATE

    # ── Manage Games: reopen the detail panel (stateless "back") ─────────────
    if act == "mgdetail":
        uid, gn = ctx.user_data.get("_mg_uid"), ctx.user_data.get("_mg_game")
        if not uid or not gn:
            await q.answer("Session lost.", show_alert=True)
            return await show_admin_menu(update, ctx)
        return await _show_mg_detail(q, ctx, uid, gn)

    # ── Manage Games: tasks list ──────────────────────────────────────────────
    if act == "mgtasks":
        uid, gn = ctx.user_data.get("_mg_uid"), ctx.user_data.get("_mg_game")
        if not uid or not gn:
            await q.answer("Session lost.", show_alert=True)
            return await show_admin_menu(update, ctx)
        return await _show_mg_tasks(q, ctx, uid, gn)

    if act == "mgtaskadd":
        uid, gn = ctx.user_data.get("_mg_uid"), ctx.user_data.get("_mg_game")
        if not uid or not gn:
            await q.answer("Session lost.", show_alert=True)
            return await show_admin_menu(update, ctx)
        await q.edit_message_text(
            f"📝 *Add Task — {gn}*\n\n"
            f"Send in ONE message, pipe-separated:\n`TaskIdName|EventName`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("❌ Cancel", "adm:mgtasks")]]))
        return ST_ADMIN_MG_TASK_ADD

    if act.startswith("mgtaskedit:"):
        idx_s = act.split(":", 1)[1]
        uid, gn = ctx.user_data.get("_mg_uid"), ctx.user_data.get("_mg_game")
        if not uid or not gn:
            await q.answer("Session lost.", show_alert=True)
            return await show_admin_menu(update, ctx)
        target_user = await _get_target_user(uid)
        if not target_user:
            await q.answer("User not found.", show_alert=True)
            return ST_ADMIN_MAIN
        games = _pj(target_user.get("games"), {})
        g = games.get(gn)
        idx = int(idx_s)
        tasks = (g or {}).get("tasks", [])
        if g is None or not (0 <= idx < len(tasks)):
            await q.answer("Task not found.", show_alert=True)
            return await _show_mg_tasks(q, ctx, uid, gn)
        t = tasks[idx]
        ctx.user_data["_mg_task_idx"] = idx
        await q.edit_message_text(
            f"✏️ *Edit Task — {gn}*\n\n"
            f"Current: `{t.get('id_name','?')}|{t.get('event_name','?')}`\n\n"
            f"Send the new values in ONE message, pipe-separated:\n`TaskIdName|EventName`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("❌ Cancel", "adm:mgtasks")]]))
        return ST_ADMIN_MG_TASK_EDIT

    if act.startswith("mgtaskdel:"):
        idx_s = act.split(":", 1)[1]
        uid, gn = ctx.user_data.get("_mg_uid"), ctx.user_data.get("_mg_game")
        if not uid or not gn:
            await q.answer("Session lost.", show_alert=True)
            return await show_admin_menu(update, ctx)
        all_users = await asyncio.get_running_loop().run_in_executor(None, SB.admin_list_users)
        target_user = next((u for u in all_users if u["id"] == uid), None)
        if not target_user:
            await q.answer("User not found.", show_alert=True)
            return ST_ADMIN_MAIN
        target_games = _pj(target_user.get("games"), {})
        g = target_games.get(gn)
        if g is None:
            await q.answer("Game not found.", show_alert=True)
            return ST_ADMIN_MAIN
        tasks = g.get("tasks", [])
        idx = int(idx_s)
        if 0 <= idx < len(tasks):
            removed = tasks.pop(idx)
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: SB.admin_save_games_for_uid(uid, target_games))
            await q.answer(f"🗑 Removed task '{removed.get('id_name','?')}'", show_alert=True)
        return await _show_mg_tasks(q, ctx, uid, gn)

    # ── Manage Games: delete a game from the user (with confirm step) ────────
    if act == "mgdelete":
        uid, gn = ctx.user_data.get("_mg_uid"), ctx.user_data.get("_mg_game")
        if not uid or not gn:
            await q.answer("Session lost.", show_alert=True)
            return await show_admin_menu(update, ctx)
        await q.edit_message_text(
            f"🗑 *Delete Game*\n\nRemove *{gn}* from this user? This cannot be undone.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([
                [("✅ Yes, Delete", "adm:mgdeleteconfirm")],
                [("❌ Cancel", "adm:mgdetail")],
            ]))
        return ST_ADMIN_MAIN

    if act == "mgdeleteconfirm":
        uid, gn = ctx.user_data.get("_mg_uid"), ctx.user_data.get("_mg_game")
        if not uid or not gn:
            await q.answer("Session lost.", show_alert=True)
            return await show_admin_menu(update, ctx)
        all_users = await asyncio.get_running_loop().run_in_executor(None, SB.admin_list_users)
        target_user = next((u for u in all_users if u["id"] == uid), None)
        if not target_user:
            await q.answer("User not found.", show_alert=True)
            return ST_ADMIN_MAIN
        target_games = _pj(target_user.get("games"), {})
        target_games.pop(gn, None)
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: SB.admin_save_games_for_uid(uid, target_games))
        await q.answer(f"🗑 '{gn}' deleted.", show_alert=True)
        ctx.user_data.pop("_mg_game", None)
        return await _show_manage_games_for_user(q, ctx, uid, page=ctx.user_data.get("_mg_page", 0))

    # ── Copy Game: pagination on the source-game list ─────────────────────────
    if act.startswith("cgpage:"):
        page = int(act.split(":", 1)[1])
        return await _render_copy_pick(q, ctx, page, edit=True,
                                       search_query=ctx.user_data.get("_cg_search"))

    # ── Copy Game: search prompt ──────────────────────────────────────────────
    if act == "cgsearch":
        await q.edit_message_text(
            "🔍 *Search Games*\n\nType part of the game name:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("❌ Cancel", "adm:cgclear")]]))
        return ST_ADMIN_COPY_GAME_SEARCH

    if act == "cgclear":
        ctx.user_data.pop("_cg_search", None)
        return await _render_copy_pick(q, ctx, 0, edit=True)

    # ── Copy Game: a game was picked ──────────────────────────────────────────
    if act.startswith("cgpick:"):
        gn = act.split(":", 1)[1]
        ctx.user_data["_cg_game"] = gn
        await q.edit_message_text(
            f"📋 *Copy Game:* `{gn}`\n\nEnter *destination* user email (copy TO):",
            parse_mode=ParseMode.MARKDOWN)
        return ST_ADMIN_COPY_GAME_DST

    # ── Copy Game: copy-mode chosen (basic info only, or info + tasks) ───────
    if act.startswith("cgmode:"):
        mode = act.split(":", 1)[1]   # "basic" or "full"
        gn        = ctx.user_data.pop("_cg_game", None)
        src_games = ctx.user_data.pop("_cg_src_games", {})
        src_email = ctx.user_data.pop("_cg_src_email", "?")
        ctx.user_data.pop("_cg_src_uid", None)
        dst_uid   = ctx.user_data.pop("_cg_dst_uid", None)
        dst_email = ctx.user_data.pop("_cg_dst_email", "?")

        if not gn or gn not in src_games or not dst_uid:
            await q.edit_message_text("❌ Copy data lost. Start over.",
                                      reply_markup=kb([[("👑 Admin Panel", "adm:back")]]))
            return ST_ADMIN_MAIN

        all_users = await asyncio.get_running_loop().run_in_executor(None, SB.admin_list_users)
        dst_user  = next((u for u in all_users if u["id"] == dst_uid), None)
        if not dst_user:
            await q.edit_message_text("❌ Destination user not found.",
                                      reply_markup=kb([[("👑 Admin Panel", "adm:back")]]))
            return ST_ADMIN_MAIN
        dst_games = _pj(dst_user.get("games"), {})

        src_g = src_games[gn]
        if mode == "basic":
            # Only game name, dev key, app id — fresh game, no tasks/uids/settings
            g_copy = {
                "dev_key": src_g.get("dev_key", ""),
                "app_id":  src_g.get("app_id", ""),
                "platform": src_g.get("platform", "android"),
                "tasks": [],
                "af_uids": [],
                "af_uid_additions": [],
                "af_uid_limit": src_g.get("af_uid_limit", 2),
                "validation_date": src_g.get("validation_date", ""),
                "stats": {"tasks": 0, "purchases": 0,
                         "last_task_event": "", "last_purchase_event": ""},
                "has_activity": False,
            }
            mode_label = "Game Info Only (name, dev key, app id)"
        else:
            g_copy = dict(src_g)
            g_copy["af_uids"]          = []
            g_copy["af_uid_additions"] = []
            g_copy["stats"]            = {"tasks": 0, "purchases": 0,
                                          "last_task_event": "", "last_purchase_event": ""}
            g_copy["has_activity"]     = False
            mode_label = "Game Info + Tasks"

        dst_games[gn] = g_copy
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: SB.admin_save_games_for_uid(dst_uid, dst_games))

        await q.edit_message_text(
            f"✅ *Game Copied!*\n\n🎮 Game: *{gn}*\n➡️ From: `{src_email}`\n➡️ To: `{dst_email}`\n"
            f"📦 Mode: {mode_label}\n\nAF UIDs reset to empty.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("👑 Admin Menu", "adm:back")]]))
        return ST_ADMIN_MAIN

    return ST_ADMIN_MAIN


# ═══════════════════════════════════════════════════════════════════════════════
#  Admin: Manage Games — full feature parity with the desktop app's
#  ManageGamesWindow (assign/remove, edit keys, tasks, limit, validation date,
#  delete game) — with search + pagination on the game list.
# ═══════════════════════════════════════════════════════════════════════════════
MG_PAGE_SIZE = 6

async def _get_target_user(uid):
    all_users = await asyncio.get_running_loop().run_in_executor(None, SB.admin_list_users)
    return next((u for u in all_users if u["id"] == uid), None)

async def _render_manage_games(dest, ctx, uid, page, edit, search_query=None):
    target_user = await _get_target_user(uid)
    if not target_user:
        text = "❌ User not found."
        if edit:
            await dest.edit_message_text(text)
        else:
            await dest.reply_text(text)
        return ST_ADMIN_MAIN

    admin_games  = await asyncio.get_running_loop().run_in_executor(None, SB.get_admin_games)
    target_games = _pj(target_user.get("games"), {})
    email = target_user.get("email", "?")

    names = sorted(admin_games.keys())
    if search_query:
        names = [n for n in names if search_query.lower() in n.lower()]

    page_names, page, total_pages = _paginate(names, page, MG_PAGE_SIZE)

    # IMPORTANT: Telegram limits callback_data to 64 bytes. A user UUID alone
    # is 36 chars, so embedding uid+game-name in every button (as before)
    # silently broke almost every real game name — Telegram rejected the
    # button and taps did nothing. Fix: keep uid/page in session state and
    # reference games by their index on this page instead.
    ctx.user_data["_mg_uid"]  = uid
    ctx.user_data["_mg_page"] = page
    ctx.user_data["_mg_list"] = page_names

    rows = []
    for i, gn in enumerate(page_names):
        g      = admin_games.get(gn, {})
        plat_e = "🍎" if (g.get("platform") or "android").lower() == "ios" else "🤖"
        has = gn in target_games
        if has:
            rows.append([
                (f"✅ {plat_e} {gn}", f"adm:mgt:{i}"),
                ("⚙️ Manage", f"adm:mgd:{i}"),
            ])
        else:
            rows.append([(f"➕ Add {plat_e} {gn}", f"adm:mgt:{i}")])

    nav = _nav_row(page, total_pages, "adm:mgpage:")
    if nav:
        rows.append(nav)
    search_row = [("🔍 Search", "adm:mgsearch")]
    if search_query:
        search_row.append(("❌ Clear", "adm:mgclear"))
    rows.append(search_row)
    rows.append([("🔙 Admin Menu", "adm:back")])

    header = f"📋 *Manage Games for*\n`{email}`\n\n✅ = assigned (tap ⚙️ to manage)  ➕ = add"
    if search_query:
        header += f"\n🔍 Results for `{search_query}`"
    header += f"\n_Page {page+1}/{total_pages} · {len(names)} of {len(admin_games)} game(s)_"

    markup = kb(rows)
    if edit:
        await dest.edit_message_text(header, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
    else:
        await dest.reply_text(header, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
    return ST_ADMIN_MAIN


# ═══════════════════════════════════════════════════════════════════════════════
#  Admin: Add Game to Users — matches the desktop app's
#  _add_game_to_users_dialog exactly (same fields, same duplicate/suffix
#  handling, same optional-task support with @ placeholder).
# ═══════════════════════════════════════════════════════════════════════════════
async def _show_addgame_platform(q, ctx):
    rows = [
        [("🤖 Android", "adm:agplat:android"), ("🍎 iOS", "adm:agplat:ios")],
        [("❌ Cancel", "adm:back")],
    ]
    text = "📱 *Add Game — Platform*\n\nSelect the platform:"
    if hasattr(q, "edit_message_text"):
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    else:
        await q.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_ADMIN_MAIN


async def addgame_email_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    email_q = update.message.text.strip()
    users   = await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.admin_get_user_by_email(email_q))
    if not users:
        await update.message.reply_text(
            f"❌ No users found matching `{email_q}`. Try again, or /main to cancel.",
            parse_mode=ParseMode.MARKDOWN)
        return ST_ADDGAME_EMAIL
    if len(users) == 1:
        ctx.user_data["_ag_target"] = users[0]["id"]
        return await _show_addgame_platform(update.message, ctx)
    rows = [[(u["email"][:35], f"adm:agpick:{u['id']}")] for u in users[:10]]
    rows.append([("❌ Cancel", "adm:back")])
    await update.message.reply_text(
        f"Found {len(users)} matches — select one:", reply_markup=kb(rows))
    return ST_ADMIN_MAIN


async def addgame_details_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw   = update.message.text.strip()
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3:
        await update.message.reply_text(
            "⚠️ Need at least `GameName | DEV_KEY | APP_ID`. Try again:",
            parse_mode=ParseMode.MARKDOWN)
        return ST_ADDGAME_DETAILS
    while len(parts) < 7:
        parts.append("-")
    gn, dev_key, app_id, vdate, lim_s, task_id, task_ev = parts[:7]

    if not gn or not dev_key or not app_id or gn == "-" or dev_key == "-" or app_id == "-":
        await update.message.reply_text(
            "⚠️ GameName, DEV_KEY, and APP_ID are required (cannot be `-`). Try again:",
            parse_mode=ParseMode.MARKDOWN)
        return ST_ADDGAME_DETAILS

    vd_iso = ""
    if vdate and vdate != "-":
        try:
            exp = datetime.strptime(vdate, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc)
            vd_iso = exp.isoformat()
        except Exception:
            await update.message.reply_text(
                "⚠️ Invalid date. Use `YYYY-MM-DD` or `-`. Try again:",
                parse_mode=ParseMode.MARKDOWN)
            return ST_ADDGAME_DETAILS

    try:
        lim = int(lim_s) if lim_s and lim_s != "-" else 2
    except Exception:
        lim = 2

    init_tasks = []
    if task_id and task_id != "-":
        if not task_ev or task_ev == "-":
            await update.message.reply_text(
                "⚠️ Task ID Name given but Event Name is missing. Provide "
                "both, or `-` for both. Try again:", parse_mode=ParseMode.MARKDOWN)
            return ST_ADDGAME_DETAILS
        init_tasks = [{"id_name": task_id, "event_name": task_ev, "added_by_admin": True}]

    plat = ctx.user_data.get("_ag_platform", "android")
    target = ctx.user_data.get("_ag_target")

    new_game = {
        "dev_key": dev_key, "app_id": app_id, "platform": plat,
        "af_uids": [], "gaids": [], "af_uid_additions": [],
        "af_uid_limit": lim, "has_activity": False,
        "added_by_admin": True, "validation_date": vd_iso,
        "tasks": init_tasks,
        "stats": {"tasks": 0, "purchases": 0,
                  "last_task_event": "", "last_purchase_event": ""},
    }

    msg = await update.message.reply_text("⏳ Adding game…")

    def _do():
        all_users = SB.admin_list_users()
        targets = all_users if target == "all" else [u for u in all_users if u["id"] == target]
        count = 0
        for u in targets:
            try:
                games = _pj(u.get("games"), {})
                # Same name + same platform → skip (real duplicate).
                # Same name + other platform → store under suffixed key.
                same_plat_dup = any(
                    k == gn and (gg.get("platform") or "android").lower() == plat
                    for k, gg in games.items())
                if same_plat_dup:
                    continue
                game_key = gn
                if game_key in games:
                    suffix = " (iOS)" if plat == "ios" else " (Android)"
                    game_key = f"{gn}{suffix}"
                    i = 2
                    while game_key in games:
                        game_key = f"{gn}{suffix} {i}"
                        i += 1
                games[game_key] = new_game
                SB.admin_save_games_for_uid(u["id"], games)
                count += 1
            except Exception:
                pass
        return count

    count = await asyncio.get_running_loop().run_in_executor(None, _do)

    for k in ("_ag_target", "_ag_platform"):
        ctx.user_data.pop(k, None)

    await msg.edit_text(
        f"✅ *Game added to {count} user(s).*\n\n🎮 {gn}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[("👑 Admin Menu", "adm:back")]]))
    return ST_ADMIN_MAIN


# ═══════════════════════════════════════════════════════════════════════════════
#  Admin: Add User — matches the desktop app's _add_user_dialog exactly
#  (create Auth account + matching users-table row, service role only).
# ═══════════════════════════════════════════════════════════════════════════════
async def adduser_email_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    email = update.message.text.strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        await update.message.reply_text(
            "⚠️ Invalid email format. Try again, or /main to cancel:")
        return ST_ADDUSER_EMAIL
    ctx.user_data["_au_email"] = email
    await update.message.reply_text(
        "🔑 Enter a password for this user (min. 6 characters):")
    return ST_ADDUSER_PASSWORD


async def adduser_password_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    email    = ctx.user_data.get("_au_email", "")
    if len(password) < 6:
        await update.message.reply_text(
            "⚠️ Password must be at least 6 characters. Try again:")
        return ST_ADDUSER_PASSWORD

    # Delete the password message for basic hygiene (best-effort).
    try:
        await update.message.delete()
    except Exception:
        pass

    msg = await update.effective_chat.send_message("⏳ Creating user…")
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: SB.admin_create_user(email, password))
    except Exception as e:
        ctx.user_data.pop("_au_email", None)
        await msg.edit_text(
            f"❌ Failed to create user:\n{e}",
            reply_markup=kb([[("👑 Admin Menu", "adm:back")]]))
        return ST_ADMIN_MAIN

    ctx.user_data.pop("_au_email", None)
    await msg.edit_text(
        f"✅ *User created successfully!*\n\n📧 {email}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[("👑 Admin Menu", "adm:back")]]))
    return ST_ADMIN_MAIN


# ═══════════════════════════════════════════════════════════════════════════════
#  Admin: view a user's AF UIDs, Task History, and Purchase History.
#  Mirrors the desktop app's "AF UIDs & GAIDs", "Task History", and
#  "Purchase History" tabs exactly — same source data (af_uids / sent_tasks /
#  usage records), same newest-first ordering. Paginated at 10 rows/page.
# ═══════════════════════════════════════════════════════════════════════════════
async def _show_admin_uafuids_gamepick(q, ctx, target_uid):
    u = await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.admin_get_user_by_id(target_uid))
    if not u:
        await q.edit_message_text("❌ User not found.", reply_markup=back_kb("adm:back"))
        return ST_ADMIN_MAIN
    games = _pj(u.get("games"), {})
    if not games:
        await q.edit_message_text(
            f"ℹ️ *{u.get('email','?')}* has no games.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb("adm:back"))
        return ST_ADMIN_MAIN
    rows = []
    for gn, g in sorted(games.items()):
        plat_e = "🍎" if (g.get("platform") or "android").lower() == "ios" else "🤖"
        n_uids = len(g.get("af_uids", []))
        rows.append([(f"{plat_e} {gn}  ({n_uids} UIDs)", f"adm:uafg:{target_uid}:{gn}")])
    rows.append([("🔙 Admin Menu", "adm:back")])
    await q.edit_message_text(
        f"👤 *AF UIDs — {u.get('email','?')}*\n\nSelect a game:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_ADMIN_MAIN


async def _show_admin_uafuids_list(q, ctx, target_uid, gn, page=0):
    u = await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.admin_get_user_by_id(target_uid))
    if not u:
        await q.edit_message_text("❌ User not found.", reply_markup=back_kb("adm:back"))
        return ST_ADMIN_MAIN
    games = _pj(u.get("games"), {})
    g     = games.get(gn, {})
    af_list = g.get("af_uids", [])
    if not af_list:
        await q.edit_message_text(
            f"ℹ️ No AF UIDs for *{gn}*.", parse_mode=ParseMode.MARKDOWN,
            reply_markup=back_kb(f"adm:uafuids:{target_uid}"))
        return ST_ADMIN_MAIN

    page_list, page, total_pages = _paginate(af_list, page, UIDS_PAGE_SIZE)
    lines = []
    for af in page_list:
        deleted = " 🗑️_deleted by user_" if af.get("deleted_by_user") else ""
        n_events = len(af.get("sent_tasks", [])) + sum(
            1 for r in af.get("usage", []) if r.get("type") == "purchase")
        lines.append(
            f"👤 *{af.get('name','?')}*{deleted}\n"
            f"   UID: `{af.get('uid','?')}`\n"
            f"   GAID: `{af.get('gaid') or '—'}`\n"
            f"   Events: {n_events}")

    nav = _nav_row(page, total_pages, f"uafnav:{target_uid}:{gn}:")
    rows = [nav] if nav else []
    rows.append([("🔙 Games", f"adm:uafuids:{target_uid}"), ("👑 Admin Menu", "adm:back")])

    header = (f"👤 *AF UIDs — {u.get('email','?')} · {gn}*\n"
              f"_Page {page+1}/{total_pages} · {len(af_list)} total_\n\n")
    await q.edit_message_text(header + "\n\n".join(lines),
                              parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_ADMIN_MAIN


async def _show_admin_task_history(q, ctx, target_uid, page=0):
    u = await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.admin_get_user_by_id(target_uid))
    if not u:
        await q.edit_message_text("❌ User not found.", reply_markup=back_kb("adm:back"))
        return ST_ADMIN_MAIN
    games = _pj(u.get("games"), {})

    rows_data = []
    for gn, g in games.items():
        for af in g.get("af_uids", []):
            for st in af.get("sent_tasks", []):
                try:
                    t = datetime.fromisoformat(
                        str(st.get("time","")).replace("Z", "+00:00"))
                except Exception:
                    continue
                rows_data.append((t, gn, af.get("name","?"),
                                   st.get("task","?"), st.get("event","?")))
    rows_data.sort(key=lambda r: r[0], reverse=True)

    if not rows_data:
        await q.edit_message_text(
            f"ℹ️ No task history for *{u.get('email','?')}*.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb("adm:back"))
        return ST_ADMIN_MAIN

    page_list, page, total_pages = _paginate(rows_data, page, UIDS_PAGE_SIZE)
    lines = []
    for t, gn, af_name, task, event in page_list:
        bd_t = (t + BDT).strftime("%Y-%m-%d %H:%M")
        lines.append(f"⏰ `{bd_t}` BDT\n🎮 {gn}  👤 {af_name}\n✅ *{task}*")

    nav = _nav_row(page, total_pages, f"utasknav:{target_uid}:")
    rows = [nav] if nav else []
    rows.append([("👑 Admin Menu", "adm:back")])

    header = (f"✅ *Task History — {u.get('email','?')}*\n"
              f"_Page {page+1}/{total_pages} · {len(rows_data)} total_\n\n")
    await q.edit_message_text(header + "\n\n".join(lines),
                              parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_ADMIN_MAIN


async def _show_admin_purchase_history(q, ctx, target_uid, page=0):
    u = await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.admin_get_user_by_id(target_uid))
    if not u:
        await q.edit_message_text("❌ User not found.", reply_markup=back_kb("adm:back"))
        return ST_ADMIN_MAIN
    games = _pj(u.get("games"), {})

    rows_data = []
    for gn, g in games.items():
        for af in g.get("af_uids", []):
            for rec in af.get("usage", []):
                if rec.get("type") != "purchase":
                    continue
                try:
                    t = datetime.fromisoformat(
                        str(rec.get("timestamp","")).replace("Z", "+00:00"))
                except Exception:
                    continue
                rows_data.append((t, gn, af.get("name","?"),
                                   rec.get("amount","?"), rec.get("currency","USD"),
                                   rec.get("product_id","N/A")))
    rows_data.sort(key=lambda r: r[0], reverse=True)

    if not rows_data:
        await q.edit_message_text(
            f"ℹ️ No purchase history for *{u.get('email','?')}*.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb("adm:back"))
        return ST_ADMIN_MAIN

    page_list, page, total_pages = _paginate(rows_data, page, UIDS_PAGE_SIZE)
    lines = []
    for t, gn, af_name, amount, currency, product_id in page_list:
        bd_t = (t + BDT).strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"⏰ `{bd_t}` BDT\n🎮 {gn}  👤 {af_name}\n"
            f"💰 *${amount} {currency}*  🏷 {product_id}")

    nav = _nav_row(page, total_pages, f"upurchnav:{target_uid}:")
    rows = [nav] if nav else []
    rows.append([("👑 Admin Menu", "adm:back")])

    header = (f"💰 *Purchase History — {u.get('email','?')}*\n"
              f"_Page {page+1}/{total_pages} · {len(rows_data)} total_\n\n")
    await q.edit_message_text(header + "\n\n".join(lines),
                              parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_ADMIN_MAIN


async def _show_manage_games_for_user(q, ctx, uid, page=0):
    return await _render_manage_games(q, ctx, uid, page, edit=True,
                                      search_query=ctx.user_data.get("_mg_search"))

async def _show_manage_games_for_user_msg(msg, ctx, uid, page=0):
    return await _render_manage_games(msg, ctx, uid, page, edit=False,
                                      search_query=ctx.user_data.get("_mg_search"))


async def _show_mg_detail(q, ctx, uid, gn):
    target_user = await _get_target_user(uid)
    if not target_user:
        await q.edit_message_text("❌ User not found.")
        return ST_ADMIN_MAIN
    games = _pj(target_user.get("games"), {})
    g = games.get(gn)
    if not g:
        await q.answer("Game not found on this user.", show_alert=True)
        return await _show_manage_games_for_user(q, ctx, uid)

    # Keep uid/game in session so every button below can stay short
    # (well under Telegram's 64-byte callback_data limit) instead of
    # re-embedding the UUID + game name each time.
    ctx.user_data["_mg_uid"]  = uid
    ctx.user_data["_mg_game"] = gn

    plat   = (g.get("platform") or "android").lower()
    plat_e = "🍎 iOS" if plat == "ios" else "🤖 Android"
    vd     = g.get("validation_date") or ""
    now    = datetime.now(timezone.utc)
    try:
        expired = bool(vd and datetime.fromisoformat(vd.replace("Z","+00:00")) < now)
    except Exception:
        expired = False
    status = "⛔ Expired" if expired else ("🔒 Locked (has activity)" if g.get("has_activity") else "✅ Active")
    dk     = g.get("dev_key", "")
    dk_disp = (dk[:10] + "…" + dk[-6:]) if len(dk) > 18 else (dk or "—")

    text = (
        f"⚙️ *Manage — {gn}*\n`{target_user.get('email','?')}`\n\n"
        f"📱 Platform: {plat_e}\n"
        f"🔑 DEV KEY: `{dk_disp}`\n"
        f"🆔 APP ID: `{g.get('app_id','—')}`\n"
        f"👤 AF UIDs: {len(g.get('af_uids',[]))}/{g.get('af_uid_limit',2)}\n"
        f"📝 Tasks: {len(g.get('tasks',[]))}\n"
        f"📅 Valid: {vd[:10] if vd else 'Unlimited'}\n"
        f"📊 Status: {status}"
    )
    rows = [
        [("📝 Tasks", "adm:mgtasks"), ("✏️ Edit Keys", "adm:mgedit")],
        [("🔢 Set Limit", "adm:mglimit"), ("📅 Validation Date", "adm:mgdate")],
        [("🗑 Delete Game", "adm:mgdelete")],
        [("🔙 Back to Games", "adm:mgback")],
    ]
    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_ADMIN_MAIN


async def _show_mg_tasks(q, ctx, uid, gn):
    target_user = await _get_target_user(uid)
    if not target_user:
        await q.edit_message_text("❌ User not found.")
        return ST_ADMIN_MAIN
    games = _pj(target_user.get("games"), {})
    g = games.get(gn)
    if not g:
        await q.answer("Game not found on this user.", show_alert=True)
        return await _show_manage_games_for_user(q, ctx, uid)

    ctx.user_data["_mg_uid"]  = uid
    ctx.user_data["_mg_game"] = gn

    tasks = g.get("tasks", [])
    rows = []
    for i, t in enumerate(tasks[:20]):
        idn = t.get("id_name", "?")
        rows.append([
            (f"✏️ {idn}", f"adm:mgtaskedit:{i}"),
            ("🗑", f"adm:mgtaskdel:{i}"),
        ])
    rows.append([("➕ Add Task", "adm:mgtaskadd")])
    rows.append([("🔙 Back", "adm:mgdetail")])

    text = f"📝 *Tasks — {gn}*\n\nTap ✏️ to edit a task, 🗑 to delete it, or add a new one:"
    if not tasks:
        text = f"📝 *Tasks — {gn}*\n\n_No tasks yet._"
    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_ADMIN_MAIN


# ── Manage Games: user email input (step 1) ────────────────────────────────────
async def admin_manage_games_user_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    email_q = update.message.text.strip()
    users   = await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.admin_get_user_by_email(email_q))
    if not users:
        await update.message.reply_text(
            f"❌ No users found matching `{email_q}`.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("👑 Admin Panel", "adm:back")]]))
        return ST_ADMIN_MAIN
    if len(users) == 1:
        ctx.user_data["_mg_uid"] = users[0]["id"]
        ctx.user_data.pop("_mg_search", None)
        return await _show_manage_games_for_user_msg(update.message, ctx, users[0]["id"])
    rows = [[( u['email'][:35], f"adm:mgsel:{u['id']}")] for u in users[:10]]
    rows.append([("🔙 Admin Menu", "adm:back")])
    await update.message.reply_text(
        "Multiple users found. Select one:",
        reply_markup=kb(rows))
    return ST_ADMIN_MAIN


# ── Manage Games: search input ──────────────────────────────────────────────────
async def admin_mg_search_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    if not query_text:
        await update.message.reply_text("⚠️ Type at least one character:")
        return ST_ADMIN_MG_SEARCH
    ctx.user_data["_mg_search"] = query_text
    uid = ctx.user_data.get("_mg_uid")
    if not uid:
        await update.message.reply_text("❌ Session lost. Start over from Admin Panel.",
                                        reply_markup=kb([[("👑 Admin Panel", "adm:back")]]))
        return ST_ADMIN_MAIN
    return await _show_manage_games_for_user_msg(update.message, ctx, uid)


# ── Manage Games: edit keys input (GameName|DevKey|AppID) ──────────────────────
async def admin_mg_edit_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.user_data.get("_mg_uid")
    gn  = ctx.user_data.get("_mg_game")
    if not uid or not gn:
        await update.message.reply_text("❌ Session lost.", reply_markup=kb([[("👑 Admin Panel", "adm:back")]]))
        return ST_ADMIN_MAIN
    parts = [p.strip() for p in update.message.text.split("|")]
    if len(parts) != 3 or not all(parts):
        await update.message.reply_text(
            "⚠️ Send exactly 3 values separated by `|`:\n`GameName|DevKey|AppID`",
            parse_mode=ParseMode.MARKDOWN)
        return ST_ADMIN_MG_EDIT
    new_name, new_dk, new_ai = parts

    target_user = await _get_target_user(uid)
    if not target_user:
        await update.message.reply_text("❌ User not found.")
        return ST_ADMIN_MAIN
    games = _pj(target_user.get("games"), {})
    g = games.get(gn)
    if not g:
        await update.message.reply_text("❌ Game not found.")
        return ST_ADMIN_MAIN

    if new_name != gn and new_name in games:
        await update.message.reply_text(
            f"❌ A game named `{new_name}` already exists for this user.",
            parse_mode=ParseMode.MARKDOWN)
        return ST_ADMIN_MG_EDIT

    g["dev_key"] = new_dk
    g["app_id"]  = new_ai
    if new_name != gn:
        games[new_name] = g
        del games[gn]
        gn = new_name

    await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.admin_save_games_for_uid(uid, games))
    ctx.user_data["_mg_game"] = gn
    await update.message.reply_text(
        f"✅ Updated *{gn}*.", parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[("⚙️ Back to Game", "adm:mgdetail")]]))
    return ST_ADMIN_MAIN


# ── Manage Games: AF UID limit input ────────────────────────────────────────────
async def admin_mg_limit_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.user_data.get("_mg_uid")
    gn  = ctx.user_data.get("_mg_game")
    if not uid or not gn:
        await update.message.reply_text("❌ Session lost.", reply_markup=kb([[("👑 Admin Panel", "adm:back")]]))
        return ST_ADMIN_MAIN
    try:
        lim = int(update.message.text.strip())
        if lim < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Send a valid non-negative number:")
        return ST_ADMIN_MG_LIMIT

    target_user = await _get_target_user(uid)
    if not target_user:
        await update.message.reply_text("❌ User not found.")
        return ST_ADMIN_MAIN
    games = _pj(target_user.get("games"), {})
    if gn not in games:
        await update.message.reply_text("❌ Game not found.")
        return ST_ADMIN_MAIN
    games[gn]["af_uid_limit"] = lim
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.admin_save_games_for_uid(uid, games))
    await update.message.reply_text(
        f"✅ Limit for *{gn}* set to *{lim}*.", parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[("⚙️ Back to Game", "adm:mgdetail")]]))
    return ST_ADMIN_MAIN


# ── Manage Games: validation date input ─────────────────────────────────────────
async def admin_mg_date_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.user_data.get("_mg_uid")
    gn  = ctx.user_data.get("_mg_game")
    if not uid or not gn:
        await update.message.reply_text("❌ Session lost.", reply_markup=kb([[("👑 Admin Panel", "adm:back")]]))
        return ST_ADMIN_MAIN
    raw = update.message.text.strip()

    target_user = await _get_target_user(uid)
    if not target_user:
        await update.message.reply_text("❌ User not found.")
        return ST_ADMIN_MAIN
    games = _pj(target_user.get("games"), {})
    if gn not in games:
        await update.message.reply_text("❌ Game not found.")
        return ST_ADMIN_MAIN

    if raw.lower() == "clear":
        games[gn]["validation_date"] = ""
        msg = f"✅ Validation date for *{gn}* cleared (unlimited)."
    else:
        try:
            exp = datetime.strptime(raw, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc)
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid format. Send as `YYYY-MM-DD` or `clear`:",
                parse_mode=ParseMode.MARKDOWN)
            return ST_ADMIN_MG_DATE
        games[gn]["validation_date"] = exp.isoformat()
        msg = f"✅ Validation date for *{gn}* set to *{raw}*."

    await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.admin_save_games_for_uid(uid, games))
    await update.message.reply_text(
        msg, parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[("⚙️ Back to Game", "adm:mgdetail")]]))
    return ST_ADMIN_MAIN


# ── Manage Games: add task input (TaskIdName|EventName) ────────────────────────
async def admin_mg_task_add_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.user_data.get("_mg_uid")
    gn  = ctx.user_data.get("_mg_game")
    if not uid or not gn:
        await update.message.reply_text("❌ Session lost.", reply_markup=kb([[("👑 Admin Panel", "adm:back")]]))
        return ST_ADMIN_MAIN
    parts = [p.strip() for p in update.message.text.split("|")]
    if len(parts) != 2 or not all(parts):
        await update.message.reply_text(
            "⚠️ Send exactly 2 values separated by `|`:\n`TaskIdName|EventName`",
            parse_mode=ParseMode.MARKDOWN)
        return ST_ADMIN_MG_TASK_ADD
    id_name, ev_name = parts

    target_user = await _get_target_user(uid)
    if not target_user:
        await update.message.reply_text("❌ User not found.")
        return ST_ADMIN_MAIN
    games = _pj(target_user.get("games"), {})
    if gn not in games:
        await update.message.reply_text("❌ Game not found.")
        return ST_ADMIN_MAIN
    tasks = games[gn].setdefault("tasks", [])
    if any(t.get("id_name") == id_name for t in tasks):
        await update.message.reply_text(f"❌ Task `{id_name}` already exists.",
                                        parse_mode=ParseMode.MARKDOWN)
        return ST_ADMIN_MG_TASK_ADD
    tasks.append({"id_name": id_name, "event_name": ev_name, "added_by_admin": True})
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.admin_save_games_for_uid(uid, games))
    await update.message.reply_text(
        f"✅ Task `{id_name}` added to *{gn}*.", parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[("📝 Back to Tasks", "adm:mgtasks")]]))
    return ST_ADMIN_MAIN


# ── Manage Games: edit an existing task's id_name / event_name ─────────────────
async def admin_mg_task_edit_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = ctx.user_data.get("_mg_uid")
    gn  = ctx.user_data.get("_mg_game")
    idx = ctx.user_data.get("_mg_task_idx")
    if not uid or not gn or idx is None:
        await update.message.reply_text("❌ Session lost.", reply_markup=kb([[("👑 Admin Panel", "adm:back")]]))
        return ST_ADMIN_MAIN
    parts = [p.strip() for p in update.message.text.split("|")]
    if len(parts) != 2 or not all(parts):
        await update.message.reply_text(
            "⚠️ Send exactly 2 values separated by `|`:\n`TaskIdName|EventName`",
            parse_mode=ParseMode.MARKDOWN)
        return ST_ADMIN_MG_TASK_EDIT
    id_name, ev_name = parts

    target_user = await _get_target_user(uid)
    if not target_user:
        await update.message.reply_text("❌ User not found.")
        return ST_ADMIN_MAIN
    games = _pj(target_user.get("games"), {})
    if gn not in games:
        await update.message.reply_text("❌ Game not found.")
        return ST_ADMIN_MAIN
    tasks = games[gn].setdefault("tasks", [])
    if not (0 <= idx < len(tasks)):
        await update.message.reply_text("❌ Task not found (list may have changed).",
                                        reply_markup=kb([[("📝 Back to Tasks", "adm:mgtasks")]]))
        return ST_ADMIN_MAIN
    if any(i != idx and t.get("id_name") == id_name for i, t in enumerate(tasks)):
        await update.message.reply_text(f"❌ Another task named `{id_name}` already exists.",
                                        parse_mode=ParseMode.MARKDOWN)
        return ST_ADMIN_MG_TASK_EDIT

    old_name = tasks[idx].get("id_name", "?")
    tasks[idx]["id_name"]    = id_name
    tasks[idx]["event_name"] = ev_name
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.admin_save_games_for_uid(uid, games))
    ctx.user_data.pop("_mg_task_idx", None)
    await update.message.reply_text(
        f"✅ Task `{old_name}` updated → `{id_name}` on *{gn}*.", parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[("📝 Back to Tasks", "adm:mgtasks")]]))
    return ST_ADMIN_MAIN


# ═══════════════════════════════════════════════════════════════════════════════
#  Admin: Copy Game flow — search + pagination on source game list,
#  and a copy-mode choice (basic info only, or info + tasks).
# ═══════════════════════════════════════════════════════════════════════════════
CG_PAGE_SIZE = 6

async def _render_copy_pick(dest, ctx, page, edit, search_query=None):
    src_games = ctx.user_data.get("_cg_src_games", {})
    src_email = ctx.user_data.get("_cg_src_email", "?")
    names = sorted(src_games.keys())
    if search_query:
        names = [n for n in names if search_query.lower() in n.lower()]

    page_names, page, total_pages = _paginate(names, page, CG_PAGE_SIZE)
    rows = []
    for gn in page_names:
        g      = src_games.get(gn, {})
        plat_e = "🍎" if (g.get("platform") or "android").lower() == "ios" else "🤖"
        rows.append([(f"{plat_e} {gn}", f"adm:cgpick:{gn}")])

    nav = _nav_row(page, total_pages, "adm:cgpage:")
    if nav:
        rows.append(nav)
    search_row = [("🔍 Search", "adm:cgsearch")]
    if search_query:
        search_row.append(("❌ Clear", "adm:cgclear"))
    rows.append(search_row)
    rows.append([("🔙 Admin Menu", "adm:back")])

    header = f"📋 *Copy Game FROM* `{src_email}`\n\nSelect game to copy:"
    if search_query:
        header += f"\n🔍 Results for `{search_query}`"
    header += f"\n_Page {page+1}/{total_pages} · {len(names)} game(s)_"

    if not names:
        header = f"📋 *Copy Game FROM* `{src_email}`\n\n_No games match your search._"

    markup = kb(rows)
    if edit:
        await dest.edit_message_text(header, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
    else:
        await dest.reply_text(header, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
    return ST_ADMIN_COPY_GAME_PICK


async def admin_copy_game_src_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 1: receive source email."""
    email_q = update.message.text.strip()
    users   = await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.admin_get_user_by_email(email_q))
    if not users:
        await update.message.reply_text(
            f"❌ No users found matching `{email_q}`.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("👑 Admin Panel", "adm:back")]]))
        return ST_ADMIN_MAIN

    src = users[0]
    src_games = _pj(src.get("games"), {})
    if not src_games:
        await update.message.reply_text(
            f"❌ User `{src.get('email')}` has no games.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("👑 Admin Panel", "adm:back")]]))
        return ST_ADMIN_MAIN

    ctx.user_data["_cg_src_uid"]   = src["id"]
    ctx.user_data["_cg_src_email"] = src.get("email", "?")
    ctx.user_data["_cg_src_games"] = src_games
    ctx.user_data.pop("_cg_search", None)

    return await _render_copy_pick(update.message, ctx, 0, edit=False)


async def admin_cg_search_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    if not query_text:
        await update.message.reply_text("⚠️ Type at least one character:")
        return ST_ADMIN_COPY_GAME_SEARCH
    ctx.user_data["_cg_search"] = query_text
    return await _render_copy_pick(update.message, ctx, 0, edit=False, search_query=query_text)


async def admin_copy_game_dst_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Step 3: receive destination email — then ask which fields to copy."""
    email_q = update.message.text.strip()
    gn      = ctx.user_data.get("_cg_game")
    src_games = ctx.user_data.get("_cg_src_games", {})

    if not gn or gn not in src_games:
        await update.message.reply_text("❌ Game data lost. Start over.",
                                        reply_markup=kb([[("👑 Admin Panel", "adm:back")]]))
        return ST_ADMIN_MAIN

    users = await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.admin_get_user_by_email(email_q))
    if not users:
        await update.message.reply_text(
            f"❌ No users found matching `{email_q}`.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("👑 Admin Panel", "adm:back")]]))
        return ST_ADMIN_MAIN

    dst = users[0]
    ctx.user_data["_cg_dst_uid"]   = dst["id"]
    ctx.user_data["_cg_dst_email"] = dst.get("email", "?")

    await update.message.reply_text(
        f"📋 *Copy* `{gn}`\n➡️ To: `{dst.get('email','?')}`\n\n"
        f"What should be copied?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([
            [("🧩 Only Game Info (name/key/id)", "adm:cgmode:basic")],
            [("📋 Game Info + Tasks", "adm:cgmode:full")],
            [("❌ Cancel", "adm:back")],
        ]))
    return ST_ADMIN_COPY_GAME_MODE



# ── Admin: user search input ──────────────────────────────────────────────────
async def admin_user_search_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    email_q = update.message.text.strip()
    adm_act = ctx.user_data.pop("_adm_action", "udetail")
    users   = await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.admin_get_user_by_email(email_q))

    if not users:
        await update.message.reply_text(
            f"❌ No users found matching `{email_q}`.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("👑 Admin Panel", "adm:back")]]))
        return ST_ADMIN_MAIN

    lines = []
    rows  = []
    for u in users[:10]:
        email  = u.get("email","?")
        adm_   = "👑" if u.get("is_admin") else "👤"
        ban_   = "🚫 BANNED" if u.get("banned") else "✅ Active"
        games  = _pj(u.get("games"),{})
        sched  = _pj(u.get("scheduled_tasks"),[])
        pending = sum(1 for t in sched if t.get("status")=="pending")
        uid_count = sum(len(g.get("af_uids",[])) for g in games.values())
        task_count= sum(g.get("stats",{}).get("tasks",0) for g in games.values())
        tg_linked  = "✅" if u.get("telegram_id") else "❌"
        lines.append(
            f"{adm_} *{email}*  {ban_}\n"
            f"   🎮 Games: {len(games)}  👤 UIDs: {uid_count}\n"
            f"   ✅ Tasks: {task_count}  ⏳ Pending sched: {pending}\n"
            f"   🔗 Telegram linked: {tg_linked}")
        if u.get("banned"):
            rows.append([(f"✅ Unban {email[:20]}", f"adm:dounban:{u['id']}")])
        else:
            rows.append([(f"🚫 Ban {email[:20]}", f"adm:doban:{u['id']}")])
        rows.append([(f"📋 Manage Games {email[:15]}", f"adm:mg_toggle_pre:{u['id']}")])
        rows.append([
            ("👤 AF UIDs",  f"adm:uafuids:{u['id']}"),
            ("✅ Tasks",    f"adm:utasks:{u['id']}"),
            ("💰 Purchases",f"adm:upurch:{u['id']}"),
        ])

    rows.append([("🔙 Admin Menu", "adm:back")])
    await update.message.reply_text(
        "🔍 *User Details*\n\n" + "\n\n".join(lines),
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_ADMIN_MAIN

# ── Admin: ban input ──────────────────────────────────────────────────────────
async def admin_ban_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    email_q = update.message.text.strip()
    users   = await asyncio.get_running_loop().run_in_executor(
        None, lambda: SB.admin_get_user_by_email(email_q))
    if not users:
        await update.message.reply_text(
            f"❌ No users found matching `{email_q}`.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb([[("👑 Admin Panel", "adm:back")]]))
        return ST_ADMIN_MAIN
    rows = []
    lines = []
    for u in users[:5]:
        email  = u.get("email","?")
        cur_ban = u.get("banned", False)
        status  = "🚫 Currently BANNED" if cur_ban else "✅ Currently Active"
        lines.append(f"*{email}*  {status}")
        if cur_ban:
            rows.append([(f"✅ Unban {email[:20]}", f"adm:dounban:{u['id']}")])
        else:
            rows.append([(f"🚫 Ban {email[:20]}", f"adm:doban:{u['id']}")])
    rows.append([("🔙 Admin Menu", "adm:back")])
    await update.message.reply_text(
        "🚫 *Ban / Unban*\n\n" + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb(rows))
    return ST_ADMIN_MAIN

# ── Admin: broadcast input ────────────────────────────────────────────────────
async def admin_broadcast_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text.strip()
    if not message_text:
        await update.message.reply_text("⚠️ Message can't be empty. Enter the broadcast message:")
        return ST_ADMIN_BROADCAST
    await update.message.reply_text("⏳ Sending broadcast…")
    users = await asyncio.get_running_loop().run_in_executor(None, SB.admin_list_users)
    sent  = 0
    failed= 0
    for u in users:
        tg_id = u.get("telegram_id")
        if not tg_id:
            continue
        try:
            await update.get_bot().send_message(
                chat_id=int(tg_id),
                text=f"📢 *Admin Broadcast*\n\n{message_text}",
                parse_mode=ParseMode.MARKDOWN)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"📢 *Broadcast Complete*\n\n✅ Sent: {sent}\n❌ Failed/not linked: {failed}",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb([[("👑 Admin Menu", "adm:back")]]))
    return ST_ADMIN_MAIN


# ═══════════════════════════════════════════════════════════════════════════════
#  Shared callbacks & fallbacks
# ═══════════════════════════════════════════════════════════════════════════════
async def back_to_main_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if is_logged_in(ctx):
        await show_main_menu(update, ctx, edit=True)
        return ST_MAIN
    await show_welcome(update, ctx, edit=True)
    return ST_GUEST

async def unhandled_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Use the buttons to navigate. Send /start to return to the main menu.")
    return ST_MAIN if is_logged_in(ctx) else ST_GUEST

async def cmd_refresh(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_logged_in(ctx):
        await update.message.reply_text("❌ Not logged in. Send /start.")
        return
    try:
        token = sess(ctx)["token"]; uid = sess(ctx)["uid"]
        ur    = await asyncio.get_running_loop().run_in_executor(
            None, lambda: SB.get_user_record(uid, token))
        if ur:
            ur["games"]           = _pj(ur.get("games"), {})
            ur["scheduled_tasks"] = _pj(ur.get("scheduled_tasks"), [])
            sess(ctx)["user_record"] = ur
        await update.message.reply_text("✅ Data refreshed!")
    except Exception as e:
        await update.message.reply_text(f"❌ Refresh failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  Background scheduler — actually fires due scheduled tasks
#
#  IMPORTANT: previously, scheduling a task only ever wrote a "pending" row
#  to the DB. Nothing in the bot ever came back to check fire_at_utc and
#  actually send the AppsFlyer event — so every scheduled task stayed stuck
#  on "⏳ pending" forever, even though the UI said "✅ Scheduled!". This
#  engine is the missing piece: a JobQueue job that runs every
#  SCHED_POLL_SECONDS, scans every user's scheduled_tasks (service role —
#  not tied to any single logged-in session), fires anything whose
#  fire_at_utc has passed, updates its status in the DB, and DMs the user
#  the result on Telegram.
# ═══════════════════════════════════════════════════════════════════════════════
def _process_due_scheduled_tasks_sync():
    """Blocking (runs in executor). Returns [(telegram_id, message_text), ...]
    for final-state (sent/error) notifications to deliver on Telegram."""
    notifications = []
    now = datetime.now(timezone.utc)
    try:
        users = SB.admin_list_users()
    except Exception as e:
        log.warning("scheduler: failed to list users: %s", e)
        return notifications

    for u in users:
        sched = _pj(u.get("scheduled_tasks"), [])
        if not sched:
            continue
        games = _pj(u.get("games"), {})
        user_changed = False

        for t in sched:
            if t.get("status") != "pending":
                continue
            fire_s = t.get("fire_at_utc", "")
            try:
                fire_dt = datetime.fromisoformat(fire_s.replace("Z", "+00:00"))
            except Exception:
                continue
            if fire_dt > now:
                continue  # not due yet

            gn = t.get("game")
            g  = games.get(gn)
            if not g:
                t["status"] = "error"
                t["result"] = "Game no longer available on this account."
                user_changed = True
                tg_id = u.get("telegram_id")
                if tg_id:
                    notifications.append((tg_id,
                        f"❌ *Scheduled Task Failed*\n\n🎮 {gn}\n"
                        f"✅ {t.get('task_id_name','?')}\n{t['result']}"))
                continue

            # A task can be scheduled before a game expires. If it's still
            # pending once the game's validation date has passed, don't
            # attempt to fire it — mark it as an error instead.
            valid_ok, valid_reason = _check_game_valid(g)
            if not valid_ok:
                t["status"] = "error"
                t["result"] = "Game validation expired before this task could fire."
                user_changed = True
                tg_id = u.get("telegram_id")
                if tg_id:
                    notifications.append((tg_id,
                        f"❌ *Scheduled Task Failed*\n\n🎮 {gn}\n"
                        f"✅ {t.get('task_id_name','?')}\n{t['result']}"))
                continue

            task_type = t.get("type", "task")
            try:
                if task_type == "purchase":
                    amt = t.get("amount", 0.99)
                    cur = t.get("currency", "USD")
                    pid = t.get("product_id", "")
                    code, _body = send_purchase_event(
                        g.get("dev_key", ""), g.get("app_id", ""),
                        t.get("af_uid", ""), t.get("gaid", ""),
                        amt, cur, pid, t.get("event_name", "af_purchase"),
                        g.get("platform", "android"),
                    )
                else:
                    code, _body = send_task_event(
                        g.get("dev_key", ""), g.get("app_id", ""),
                        t.get("af_uid", ""), t.get("gaid", ""),
                        t.get("event_name", ""), g.get("platform", "android"),
                    )
                ok = (code == 200)
            except Exception as e:
                ok, code = False, 0

            user_changed = True
            if ok:
                t["status"]  = "sent"
                t["result"]  = "Done"
                t["sent_at"] = now.isoformat().replace("+00:00", "Z")

                af_entry = next((a for a in g.get("af_uids", [])
                                  if a.get("uid") == t.get("af_uid")), None)
                g.setdefault("stats", {})
                if task_type == "purchase":
                    # Record purchase usage & stats
                    if af_entry is not None:
                        af_entry.setdefault("usage", []).append({
                            "type": "purchase",
                            "timestamp": now.isoformat(),
                            "amount": t.get("amount", 0.99),
                            "currency": t.get("currency", "USD"),
                            "product_id": t.get("product_id", ""),
                            "event": t.get("event_name", "af_purchase"),
                        })
                    g["stats"]["purchases"] = g["stats"].get("purchases", 0) + 1
                    g["stats"]["last_purchase_event"] = t.get("task_id_name", "?")
                else:
                    # Record task usage & stats
                    if af_entry is not None:
                        af_entry.setdefault("sent_tasks", []).append({
                            "task": t.get("task_id_name", "?"),
                            "event": t.get("event_name", ""),
                            "time": now.isoformat(),
                        })
                    g["stats"]["tasks"] = g["stats"].get("tasks", 0) + 1
                    g["stats"]["last_task_event"] = t.get("task_id_name", "?")
                if not g.get("has_activity"):
                    g["has_activity"] = True
                games[gn] = g

                tg_id = u.get("telegram_id")
                if tg_id:
                    if task_type == "purchase":
                        notifications.append((tg_id,
                            f"✅ *Scheduled Purchase Sent*\n\n🎮 {gn}\n"
                            f"💰 {t.get('task_id_name','?')}\nDone"))
                    else:
                        notifications.append((tg_id,
                            f"✅ *Scheduled Task Sent*\n\n🎮 {gn}\n"
                            f"✅ {t.get('task_id_name','?')}\nDone"))
            else:
                t["retries"] = t.get("retries", 0) + 1
                if t["retries"] >= MAX_SCHED_RETRIES:
                    t["status"] = "error"
                    t["result"] = f"Failed after {MAX_SCHED_RETRIES} attempts (HTTP {code})."
                    tg_id = u.get("telegram_id")
                    if tg_id:
                        type_icon = "💰" if task_type == "purchase" else "✅"
                        notifications.append((tg_id,
                            f"❌ *Scheduled {'Purchase' if task_type == 'purchase' else 'Task'} Failed*"
                            f"\n\n🎮 {gn}\n"
                            f"{type_icon} {t.get('task_id_name','?')}\n{t['result']}"))
                else:
                    t["result"] = f"Retry {t['retries']}/{MAX_SCHED_RETRIES} — HTTP {code}, will retry."
                    # stays "pending" so the next tick retries it automatically

        if user_changed:
            try:
                SB.admin_update_user(u["id"], {
                    "scheduled_tasks": json.dumps(sched),
                    "games":           json.dumps(games),
                })
            except Exception as e:
                log.warning("scheduler: failed saving user %s: %s", u.get("id"), e)

    return notifications


async def scheduler_tick(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        notifications = await asyncio.get_running_loop().run_in_executor(
            None, _process_due_scheduled_tasks_sync)
    except Exception as e:
        log.warning("scheduler_tick failed: %s", e)
        return
    for tg_id, text in notifications:
        try:
            await ctx.bot.send_message(chat_id=tg_id, text=text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            log.warning("scheduler: failed to notify telegram_id=%s: %s", tg_id, e)


# ═══════════════════════════════════════════════════════════════════════════════
#  App setup
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    all_states_common = [
        CallbackQueryHandler(main_menu_cb,    pattern="^menu:"),
        CallbackQueryHandler(back_to_main_cb, pattern="^menu:back$"),
        CallbackQueryHandler(admin_cb,        pattern="^adm:"),
        CallbackQueryHandler(settings_cb,     pattern="^settings:"),
        CallbackQueryHandler(explore_cb,      pattern="^explore:"),
        CallbackQueryHandler(guest_cb,        pattern="^guest:"),
    ]

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start",   cmd_start),
            CommandHandler("main",    cmd_main),
            CommandHandler("mygames", cmd_mygames),
            CommandHandler("event",   cmd_event),
        ],
        states={
            # ── Guest (not logged in) welcome screen ─────────────────────────
            ST_GUEST: [
                CallbackQueryHandler(guest_cb,   pattern="^guest:"),
                CallbackQueryHandler(explore_cb, pattern="^explore:"),
            ],

            ST_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_email)],
            ST_PASS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_password)],

            ST_MAIN: all_states_common + [
                CallbackQueryHandler(game_detail_cb,     pattern="^game:"),
                CallbackQueryHandler(games_page_cb,      pattern="^gpage:"),
                CallbackQueryHandler(send_game_pick_cb,  pattern="^snd_game:"),
                CallbackQueryHandler(send_games_page_cb, pattern="^sndpage:"),
                CallbackQueryHandler(send_type_cb,       pattern="^snd_type:"),
                CallbackQueryHandler(sched_cb,           pattern="^sched:"),
                CallbackQueryHandler(search_prompt_cb,   pattern="^search:"),
                CallbackQueryHandler(del_uid_list_cb,    pattern="^deluid:"),
                CallbackQueryHandler(del_uid_list_cb,    pattern="^deluidnav:"),
                CallbackQueryHandler(del_uid_confirm_cb, pattern="^deluidsel:"),
                CallbackQueryHandler(del_uid_yes_cb,     pattern="^deluidyes"),
            ],

            ST_GAME_ACTION: all_states_common + [
                CallbackQueryHandler(game_detail_cb,    pattern="^game:"),
                CallbackQueryHandler(games_page_cb,     pattern="^gpage:"),
                CallbackQueryHandler(add_uid_start_cb,  pattern="^adduid:"),
                CallbackQueryHandler(search_prompt_cb,  pattern="^search:"),
                CallbackQueryHandler(del_uid_list_cb,    pattern="^deluid:"),
                CallbackQueryHandler(del_uid_list_cb,    pattern="^deluidnav:"),
                CallbackQueryHandler(del_uid_confirm_cb, pattern="^deluidsel:"),
                CallbackQueryHandler(del_uid_yes_cb,     pattern="^deluidyes"),
            ],

            ST_SEND_TYPE: all_states_common + [
                CallbackQueryHandler(send_game_pick_cb,  pattern="^snd_game:"),
                CallbackQueryHandler(send_games_page_cb, pattern="^sndpage:"),
                CallbackQueryHandler(send_type_cb,       pattern="^snd_type:"),
                CallbackQueryHandler(search_prompt_cb,   pattern="^search:"),
            ],

            ST_TASK_PICK: all_states_common + [
                CallbackQueryHandler(task_pick_cb,      pattern="^snd_task:"),
                CallbackQueryHandler(send_task_page_cb, pattern="^sndtpage:"),
                CallbackQueryHandler(send_game_pick_cb, pattern="^snd_game:"),
                CallbackQueryHandler(send_type_cb,      pattern="^snd_type:"),
            ],

            ST_TASK_NUM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, task_num_input),
            ],

            ST_UID_PICK: all_states_common + [
                CallbackQueryHandler(uid_pick_send_cb,     pattern="^snd_uid:"),
                CallbackQueryHandler(uid_pick_purchase_cb, pattern="^pur_uid:"),
                CallbackQueryHandler(snd_uid_page_cb,      pattern="^snduidpage:"),
                CallbackQueryHandler(pur_uid_page_cb,      pattern="^puruidpage:"),
                CallbackQueryHandler(send_type_cb,         pattern="^snd_type:"),
                CallbackQueryHandler(send_game_pick_cb,    pattern="^snd_game:"),
            ],

            ST_PURCH_AMT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, purch_amt_input)],

            ST_SCHED_TASK: all_states_common + [
                CallbackQueryHandler(sched_cb,                pattern="^sched:"),
                CallbackQueryHandler(sched_game_pick_cb,      pattern="^sg:"),
                CallbackQueryHandler(sched_task_pick_cb,      pattern="^st:"),
                CallbackQueryHandler(sched_purch_game_pick_cb,pattern="^sp_game:"),
                CallbackQueryHandler(search_prompt_cb,        pattern="^search:"),
                CallbackQueryHandler(sched_purch_samegame_cb, pattern="^sched:purch_samegame$"),
            ],

            ST_SCHED_NUM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, sched_num_input)],

            ST_SCHED_UID: all_states_common + [
                CallbackQueryHandler(sched_uid_pick_cb,  pattern="^su:"),
                CallbackQueryHandler(sched_uid_page_cb,  pattern="^scheduidpage:"),
            ],

            ST_SCHED_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, sched_date_input)],
            ST_SCHED_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sched_time_input)],

            # ── Scheduled Purchase states ────────────────────────────────────
            ST_SCHED_PURCH_GAME: all_states_common + [
                CallbackQueryHandler(sched_cb,                pattern="^sched:"),
                CallbackQueryHandler(sched_purch_game_pick_cb,pattern="^sp_game:"),
                CallbackQueryHandler(search_prompt_cb,        pattern="^search:"),
            ],
            ST_SCHED_PURCH_UID: all_states_common + [
                CallbackQueryHandler(sched_purch_uid_pick_cb,  pattern="^sp_uid:"),
                CallbackQueryHandler(sched_purch_uid_page_cb,  pattern="^schedpurchuidpage:"),
            ],
            ST_SCHED_PURCH_AMT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, sched_purch_amt_input)],
            ST_SCHED_PURCH_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, sched_purch_date_input)],
            ST_SCHED_PURCH_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sched_purch_time_input)],
            ST_SCHED_PURCH_SEARCH: [
                CallbackQueryHandler(search_cancel_cb,   pattern="^search:cancel"),
                CallbackQueryHandler(search_prompt_cb,   pattern="^search:"),
                CallbackQueryHandler(sched_cb,           pattern="^sched:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, game_search_input),
            ],
            ST_SCHED_PURCH_SAME: all_states_common + [
                CallbackQueryHandler(sched_cb,                  pattern="^sched:"),
                CallbackQueryHandler(sched_purch_game_pick_cb,  pattern="^sp_game:"),
                CallbackQueryHandler(sched_purch_samegame_cb,   pattern="^sched:purch_samegame$"),
                CallbackQueryHandler(sched_purch_uid_pick_cb,   pattern="^sp_uid:"),
                CallbackQueryHandler(sched_purch_uid_page_cb,   pattern="^schedpurchuidpage:"),
            ],

            # ── Edit pending schedule ────────────────────────────────────────
            ST_SCHED_EDIT_PICK: all_states_common + [
                CallbackQueryHandler(sched_cb, pattern="^sched:"),
            ],
            ST_SCHED_EDIT_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sched_edit_date_input),
            ],
            ST_SCHED_EDIT_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sched_edit_time_input),
            ],

            ST_ADMIN_MAIN: all_states_common,

            ST_GAME_SEARCH: [
                CallbackQueryHandler(search_cancel_cb,   pattern="^search:cancel"),
                CallbackQueryHandler(search_prompt_cb,   pattern="^search:"),
                CallbackQueryHandler(main_menu_cb,       pattern="^menu:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, game_search_input),
            ],

            # ── Explore Games search ─────────────────────────────────────────
            ST_EXPLORE_SEARCH: [
                CallbackQueryHandler(explore_cb, pattern="^explore:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, explore_search_input),
            ],

            ST_ADD_UID_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_uid_name_input)],
            ST_ADD_UID_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_uid_value_input)],
            ST_ADD_UID_GAID:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_uid_gaid_input)],

            ST_ADMIN_BAN:         [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_ban_input)],
            ST_ADMIN_BROADCAST:   [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_input)],
            ST_ADMIN_USER_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_user_search_input)],
            ST_ADDGAME_EMAIL:     [MessageHandler(filters.TEXT & ~filters.COMMAND, addgame_email_input)],
            ST_ADDGAME_DETAILS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, addgame_details_input)],
            ST_ADDUSER_EMAIL:     [MessageHandler(filters.TEXT & ~filters.COMMAND, adduser_email_input)],
            ST_ADDUSER_PASSWORD:  [MessageHandler(filters.TEXT & ~filters.COMMAND, adduser_password_input)],

            # ── Copy game flow ───────────────────────────────────────────────
            ST_ADMIN_COPY_GAME_SRC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_copy_game_src_input),
            ],
            ST_ADMIN_COPY_GAME_PICK: all_states_common,
            ST_ADMIN_COPY_GAME_SEARCH: [
                CallbackQueryHandler(admin_cb, pattern="^adm:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_cg_search_input),
            ],
            ST_ADMIN_COPY_GAME_DST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_copy_game_dst_input),
            ],
            ST_ADMIN_COPY_GAME_MODE: all_states_common,

            # ── Manage games flow ────────────────────────────────────────────
            ST_ADMIN_MANAGE_GAMES_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_manage_games_user_input),
            ],
            ST_ADMIN_MANAGE_GAMES_ACT: all_states_common,
            ST_ADMIN_MG_SEARCH: [
                CallbackQueryHandler(admin_cb, pattern="^adm:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_mg_search_input),
            ],
            ST_ADMIN_MG_EDIT: [
                CallbackQueryHandler(admin_cb, pattern="^adm:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_mg_edit_input),
            ],
            ST_ADMIN_MG_LIMIT: [
                CallbackQueryHandler(admin_cb, pattern="^adm:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_mg_limit_input),
            ],
            ST_ADMIN_MG_DATE: [
                CallbackQueryHandler(admin_cb, pattern="^adm:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_mg_date_input),
            ],
            ST_ADMIN_MG_TASK_ADD: [
                CallbackQueryHandler(admin_cb, pattern="^adm:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_mg_task_add_input),
            ],
            ST_ADMIN_MG_TASK_EDIT: [
                CallbackQueryHandler(admin_cb, pattern="^adm:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_mg_task_edit_input),
            ],
        },
        fallbacks=[
            CommandHandler("start",   cmd_start),
            CommandHandler("main",    cmd_main),
            CommandHandler("mygames", cmd_mygames),
            CommandHandler("event",   cmd_event),
            CommandHandler("refresh", cmd_refresh),
            MessageHandler(filters.TEXT & ~filters.COMMAND, unhandled_text),
        ],
        allow_reentry=True,
        per_chat=False,
        per_user=True,
        per_message=False,
    )

    # group=-1 runs before every other handler for every single update,
    # transparently keeping the Supabase session token fresh.
    app.add_handler(TypeHandler(Update, _global_token_refresh_middleware), group=-1)

    app.add_handler(conv)
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("main",    cmd_main))

    if app.job_queue is None:
        log.error(
            "❌ JobQueue is not available — scheduled tasks will be saved but "
            "NEVER actually fire. Install the job-queue extra: "
            "pip install \"python-telegram-bot[job-queue]\""
        )
    else:
        app.job_queue.run_repeating(
            scheduler_tick, interval=SCHED_POLL_SECONDS, first=10, name="scheduler_tick")
        log.info("✅ Scheduler engine started (checks every %ss).", SCHED_POLL_SECONDS)

    log.info("✅ AppsFlyer Master Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()

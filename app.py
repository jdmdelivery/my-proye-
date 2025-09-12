# app.py
# World Jewerly — Sistema modular (Dashboard, Clientes, Empeños, Pagos, Caja, Reportes, Config, Inventario, Ventas)
# Compatible con PyInstaller (guarda DB y uploads fuera del exe), abre navegador.

from __future__ import annotations
from flask import Flask, request, redirect, url_for, Response, render_template_string, send_from_directory, session
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, date
import csv
import io
import os
import sys
import smtplib
import ssl
import secrets
from email.mime.text import MIMEText
from pathlib import Path
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import threading, time, webbrowser
from functools import wraps
from urllib.parse import quote_plus

APP_BRAND = "World Jewerly"

# ===== Rutas para .exe / desarrollo =====
BASE_PATH = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APPDATA_DIR = Path.home() / "WorldJewerlyData"
APPDATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = str(APPDATA_DIR / "empenos.db")
STATIC_DIR = str(BASE_PATH / "static")
UPLOAD_DIR = APPDATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.config['UPLOAD_FOLDER'] = str(UPLOAD_DIR)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB
app.secret_key = "cambia-esta-clave-secreta-por-una-larga-y-unica"  # IMPORTANTE: cámbiala en producción

# ===== Esquema =====
SCHEMA = """
CREATE TABLE IF NOT EXISTS loans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    item_name TEXT NOT NULL,
    weight_grams REAL NOT NULL,
    customer_name TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    phone TEXT NOT NULL,
    amount REAL NOT NULL,
    interest_rate REAL NOT NULL,
    due_date TEXT NOT NULL,
    photo_path TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVO',
    redeemed_at TEXT
);
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    document TEXT NOT NULL,
    phone TEXT,
    address TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id INTEGER NOT NULL,
    paid_at TEXT NOT NULL,
    amount REAL NOT NULL,
    type TEXT NOT NULL, -- 'INTERES' o 'ABONO'
    notes TEXT,
    FOREIGN KEY(loan_id) REFERENCES loans(id)
);
CREATE TABLE IF NOT EXISTS cash_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    when_at TEXT NOT NULL,
    concept TEXT NOT NULL,
    amount REAL NOT NULL,     -- positivo ingreso / negativo egreso
    ref TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Ventas (artículos en venta / vendidos)
CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_desc TEXT NOT NULL,
    price REAL NOT NULL,
    sold_at TEXT,
    status TEXT NOT NULL DEFAULT 'EN_VENTA'
);
-- Inventario (artículos perdidos / encontrados)
CREATE TABLE IF NOT EXISTS inventory_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_desc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PERDIDO',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    pass_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS password_resets (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
"""

def get_db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

# Inicializa DB y semillas
with closing(get_db()) as conn:
    conn.executescript(SCHEMA)
    # settings por defecto
    defaults = {
        "default_interest_rate": "20",
        "default_term_days": "90",
        "recovery_email": "jdm299102@gmail.com",
        "smtp_host": "",
        "smtp_port": "587",
        "smtp_user": "",
        "smtp_pass": "",
        "renew_days": "30"
    }
    for k,v in defaults.items():
        if not conn.execute("SELECT 1 FROM settings WHERE key=?", (k,)).fetchone():
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", (k,v))
    # usuario admin por defecto
    if not conn.execute("SELECT 1 FROM users").fetchone():
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO users(username, pass_hash, role, created_at) VALUES(?,?,?,?)",
            ("admin", generate_password_hash("admin123"), "admin", now)
        )
    conn.commit()

def set_setting(key, value):
    with closing(get_db()) as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        conn.commit()

def get_setting(key, default=None):
    with closing(get_db()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return (row["value"] if row else default)

# ===== Email helper =====
def send_email(to_email:str, subject:str, html_body:str) -> bool:
    host = get_setting("smtp_host","")
    port = int(get_setting("smtp_port","587") or 587)
    user = get_setting("smtp_user","")
    pwd  = get_setting("smtp_pass","")
    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user or "no-reply@localhost"
    msg["To"] = to_email
    if host and user and pwd:
        try:
            ctx = ssl.create_default_context()
            with smtplib.SMTP(host, port, timeout=10) as s:
                s.starttls(context=ctx)
                s.login(user, pwd)
                s.send_message(msg)
            return True
        except Exception as e:
            print("== ERROR SMTP ==>", e)
            print("== CONTENIDO DEL CORREO (fallback) ==>\n", html_body)
            return False
    else:
        # Fallback: imprime en consola
        print("== SMTP NO CONFIGURADO. MOSTRANDO CORREO EN CONSOLA ==")
        print("Para:", to_email)
        print("Asunto:", subject)
        print(html_body)
        return False

# ====== Auth helpers ======
def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        p = request.path
        open_paths = ["/login", "/recover", "/reset", "/uploads", "/static", "/inicio", "/"]
        if p in open_paths or p.startswith("/uploads") or p.startswith("/static"):
            return f(*args, **kwargs)
        if not session.get("uid"):
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return wrapped

# ======= Páginas de autenticación =======
LOGIN_TPL = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{{ brand }} — Iniciar sesión</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>.glass{background:rgba(255,255,255,.08);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,.12);}</style>
</head>
<body class="min-h-screen flex items-center justify-center bg-stone-900 text-stone-100">
  <div class="w-full max-w-sm glass p-6 rounded-2xl">
    <div class="text-center mb-4">
      <div class="text-4xl">💎</div>
      <h1 class="text-2xl font-extrabold mt-1">{{ brand }}</h1>
      <p class="text-sm text-yellow-200/70 mt-1">Inicia sesión para continuar</p>
    </div>
    {% if msg %}<div class="mb-3 p-2 bg-emerald-900/40 border border-emerald-700 rounded">{{ msg }}</div>{% endif %}
    {% if error %}<div class="mb-3 p-2 bg-red-900/40 border border-red-700 rounded">{{ error }}</div>{% endif %}
    <form method="post" class="space-y-3">
      <input name="username" placeholder="Usuario" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <input name="password" type="password" placeholder="Contraseña" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <button class="w-full bg-yellow-400 text-stone-900 font-semibold px-4 py-2 rounded-xl">Entrar</button>
    </form>
    <div class="text-center mt-3">
      <a class="text-yellow-300 underline" href="{{ url_for('recover') }}">¿Olvidaste usuario o contraseña?</a>
    </div>
  </div>
</body>
</html>
"""

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username","").strip()
        p = request.form.get("password","")
        with closing(get_db()) as conn:
            row = conn.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
        if row and check_password_hash(row["pass_hash"], p):
            session["uid"] = row["id"]
            session["username"] = row["username"]
            return redirect(request.args.get("next") or url_for("dashboard"))
        else:
            return render_template_string(LOGIN_TPL, brand=APP_BRAND, error="Usuario o contraseña inválidos", msg=None)
    return render_template_string(LOGIN_TPL, brand=APP_BRAND, error=None, msg=request.args.get("msg"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ===== Recuperación de acceso =====
RECOVER_TPL = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{{ brand }} — Recuperar acceso</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>.glass{background:rgba(255,255,255,.08);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,.12);}</style>
</head>
<body class="min-h-screen flex items-center justify-center bg-stone-900 text-stone-100">
  <div class="w-full max-w-lg glass p-6 rounded-2xl">
    <h1 class="text-xl font-bold text-yellow-300 mb-3">Recuperar acceso</h1>
    <p class="text-sm text-yellow-200/80 mb-3">Se enviará un correo con tus usuarios y enlaces para restablecer la contraseña a <b>{{ email }}</b>.</p>
    {% if msg %}<div class="mb-3 p-2 bg-emerald-900/40 border border-emerald-700 rounded">{{ msg }}</div>{% endif %}
    {% if error %}<div class="mb-3 p-2 bg-red-900/40 border border-red-700 rounded">{{ error }}</div>{% endif %}
    <form method="post" class="space-y-3">
      <button class="bg-yellow-400 text-stone-900 font-semibold px-4 py-2 rounded-xl">Enviar correo de recuperación</button>
      <a href="{{ url_for('login') }}" class="px-4 py-2 rounded-xl border border-yellow-200/30">Volver</a>
    </form>
  </div>
</body>
</html>
"""

@app.route("/recover", methods=["GET","POST"])
def recover():
    email = get_setting("recovery_email", "jdm299102@gmail.com")
    if request.method == "POST":
        with closing(get_db()) as conn:
            users = conn.execute("SELECT id,username FROM users ORDER BY id").fetchall()
            if not users:
                return render_template_string(RECOVER_TPL, brand=APP_BRAND, email=email, msg=None, error="No hay usuarios registrados.")
            lines = []
            base_url = request.host_url.rstrip("/")
            for u in users:
                token = secrets.token_urlsafe(24)
                now = datetime.now()
                exp = now + timedelta(hours=1)
                conn.execute("INSERT OR REPLACE INTO password_resets(token,user_id,expires_at,created_at) VALUES (?,?,?,?)",
                             (token, u["id"], exp.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")))
                link = f"{base_url}{url_for('reset')}?token={token}&u={u['username']}"
                lines.append(f"<li><b>{u['username']}</b>: <a href='{link}' target='_blank'>{link}</a> (expira en 1 hora)</li>")
            conn.commit()
        html = f"""
        <h2>Recuperación de acceso — {APP_BRAND}</h2>
        <p>Usuarios encontrados:</p>
        <ul>{''.join(lines)}</ul>
        <p>Si no solicitaste este correo, ignóralo.</p>
        """
        ok = send_email(email, f"Recuperación — {APP_BRAND}", html)
        msg = "Correo enviado. Revisa tu bandeja (o consola si SMTP no está configurado)."
        if not ok:
            msg += " (SMTP no configurado: se imprimió el contenido en la consola.)"
        return render_template_string(RECOVER_TPL, brand=APP_BRAND, email=email, msg=msg, error=None)
    return render_template_string(RECOVER_TPL, brand=APP_BRAND, email=email, msg=None, error=None)

RESET_TPL = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{{ brand }} — Restablecer contraseña</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>.glass{background:rgba(255,255,255,.08);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,.12);}</style>
</head>
<body class="min-h-screen flex items-center justify-center bg-stone-900 text-stone-100">
  <div class="w-full max-w-sm glass p-6 rounded-2xl">
    <h1 class="text-xl font-bold text-yellow-300 mb-3">Restablecer contraseña</h1>
    <p class="text-sm text-yellow-200/80 mb-3">Usuario: <b>{{ username }}</b></p>
    {% if error %}<div class="mb-3 p-2 bg-red-900/40 border border-red-700 rounded">{{ error }}</div>{% endif %}
    <form method="post" class="space-y-3">
      <input type="hidden" name="token" value="{{ token }}"/>
      <input type="hidden" name="u" value="{{ username }}"/>
      <input name="password" type="password" placeholder="Nueva contraseña" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <input name="password2" type="password" placeholder="Repetir contraseña" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <button class="w-full bg-yellow-400 text-stone-900 font-semibold px-4 py-2 rounded-xl">Guardar</button>
    </form>
  </div>
</body>
</html>
"""

@app.route("/reset", methods=["GET","POST"])
def reset():
    if request.method == "GET":
        token = request.args.get("token","")
        username = request.args.get("u","")
        if not token or not username:
            return "Link inválido", 400
        with closing(get_db()) as conn:
            u = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            t = conn.execute("SELECT * FROM password_resets WHERE token=? AND user_id=?", (token, u["id"] if u else -1)).fetchone()
        if not u or not t:
            return "Token inválido", 400
        if datetime.strptime(t["expires_at"], "%Y-%m-%d %H:%M:%S") < datetime.now():
            return "Token expirado", 400
        return render_template_string(RESET_TPL, brand=APP_BRAND, token=token, username=username, error=None)
    # POST
    token = request.form.get("token","")
    username = request.form.get("u","")
    p1 = request.form.get("password","")
    p2 = request.form.get("password2","")
    if not token or not username or not p1 or p1!=p2:
        return render_template_string(RESET_TPL, brand=APP_BRAND, token=token, username=username, error="Datos inválidos o contraseñas no coinciden")
    with closing(get_db()) as conn:
        u = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        t = conn.execute("SELECT * FROM password_resets WHERE token=? AND user_id=?", (token, u["id"] if u else -1)).fetchone()
        if not u or not t:
            return render_template_string(RESET_TPL, brand=APP_BRAND, token=token, username=username, error="Token inválido")
        if datetime.strptime(t["expires_at"], "%Y-%m-%d %H:%M:%S") < datetime.now():
            return render_template_string(RESET_TPL, brand=APP_BRAND, token=token, username=username, error="Token expirado")
        with conn:
            conn.execute("UPDATE users SET pass_hash=? WHERE id=?", (generate_password_hash(p1), u["id"]))
            conn.execute("DELETE FROM password_resets WHERE token=?", (token,))
    return redirect(url_for("login", msg="Contraseña actualizada. Inicia sesión."))

# ====== PLANTILLA BASE ======
BASE_SHELL = """
<!doctype html>
<html lang="es" class="h-full">
<head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{{ brand }} — {{ title or '' }}</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
:root{ --gold-2:#f5d36b; --gold-3:#d97706; --gold-4:#eab308; --bg-1:#0b0b0e; --bg-2:#151521; --bg-3:#0f0f16; }
.bg-galaxy{ background: radial-gradient(1200px 600px at 10% -10%, #2b2b40 0%, transparent 60%), radial-gradient(900px 500px at 90% 10%, #1f2937 0%, transparent 60%), linear-gradient(135deg, var(--bg-1), var(--bg-2) 40%, var(--bg-3)); }
.gold-gradient{ background-image: linear-gradient(135deg, var(--gold-3), var(--gold-4), var(--gold-2)); }
.glass{ background: rgba(255,255,255,.08); backdrop-filter: blur(10px); border:1px solid rgba(255,255,255,.12); }
.thead-gold{ background: linear-gradient(90deg, #1c1917, #0b0b0e); color:#f5d36b;}
.nav a{ padding:.5rem .9rem; border-radius:.75rem; }
.nav a.active{ background:#111827; color:#fde68a; }
@media print { .no-print{display:none} body{background:white} }
</style>
</head>
<body class="min-h-screen bg-galaxy text-stone-100">
<header class="gold-gradient text-stone-900 shadow-lg">
  <div class="max-w-7xl mx-auto px-4 py-5">
    <div class="grid place-items-center gap-2">
      <div class="flex items-center gap-3">
        <div class="h-12 w-12 rounded-full bg-stone-900/90 text-yellow-300 flex items-center justify-center text-2xl">💎</div>
        <h1 class="text-3xl md:text-4xl font-extrabold tracking-tight text-stone-950">{{ brand }}</h1>
      </div>
      <nav class="nav flex flex-wrap gap-2 mt-2">
        <a href="{{ url_for('dashboard') }}" class="{{ 'active' if active=='dashboard' else '' }}">Inicio</a>
        <a href="{{ url_for('index') }}" class="{{ 'active' if active=='loans' else '' }}">Empeños</a>
        <a href="{{ url_for('clients') }}" class="{{ 'active' if active=='clients' else '' }}">Clientes</a>
        <a href="{{ url_for('cash') }}" class="{{ 'active' if active=='cash' else '' }}">Caja</a>
        <a href="{{ url_for('reports') }}" class="{{ 'active' if active=='reports' else '' }}">Reportes</a>
        <a href="{{ url_for('inventory') }}" class="{{ 'active' if active=='inventory' else '' }}">Inventario</a>
        <a href="{{ url_for('sales_page') }}" class="{{ 'active' if active=='sales' else '' }}">Ventas</a>
        <a href="{{ url_for('users_page') }}" class="{{ 'active' if active=='users' else '' }}">Usuarios</a>
        <a href="{{ url_for('settings_page') }}" class="{{ 'active' if active=='settings' else '' }}">Configuración</a>
        <a href="{{ url_for('logout') }}">Salir ({{ session.get('username') }})</a>
      </nav>
    </div>
  </div>
</header>
<main class="max-w-7xl mx-auto px-4 py-6">
  {{ body|safe }}
</main>
<footer class="text-center text-xs text-yellow-200/80 mt-4 pb-6">© {{ now.year if now else '' }} {{ brand }}</footer>
</body></html>
"""

def render_page(body_html, title="", active=""):
    now = datetime.now()
    return render_template_string(BASE_SHELL, body=body_html, brand=APP_BRAND, title=title, active=active, now=now)

# ===== Utilidades de fechas y texto =====
def parse_dt(s): return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
def parse_date(s): return datetime.strptime(s, "%Y-%m-%d").date()

def normalize_phone(raw:str) -> str:
    raw = (raw or "").strip()
    if raw.startswith('+'):
        return '+' + ''.join([c for c in raw[1:] if c.isdigit()])
    return ''.join([c for c in raw if c.isdigit()])

# === Utilidades de interés mensual y vencimientos ===
def last_interest_paid_dt(conn, loan_id:int) -> datetime|None:
    r = conn.execute("SELECT MAX(paid_at) AS last FROM payments WHERE loan_id=? AND type='INTERES'", (loan_id,)).fetchone()
    if r and r["last"]:
        try:
            return datetime.strptime(r["last"], "%Y-%m-%d %H:%M:%S")
        except:
            return None
    return None

def next_interest_due_date_raw(loan_row, last_int_dt:datetime|None, base_as_of:date|None=None) -> date:
    """Próximo interés vence = (último pago de interés o fecha de inicio) + 30 días."""
    start_dt = parse_dt(loan_row["created_at"])
    ref_dt = last_int_dt or start_dt
    candidate = (ref_dt + timedelta(days=30)).date()
    return candidate

def months_overdue_since(conn, loan_row, as_of_date:date) -> int:
    """Meses enteros vencidos de interés desde último pago o inicio."""
    last_int = last_interest_paid_dt(conn, loan_row["id"])
    start = (last_int or parse_dt(loan_row["created_at"])).date()
    if as_of_date <= start:
        return 0
    days = (as_of_date - start).days
    return max(0, days // 30)

def month_key(dt:date) -> str:
    return dt.strftime("%Y-%m")

def months_range_inclusive(y1:int,m1:int,y2:int,m2:int):
    y, m = y1, m1
    while (y < y2) or (y == y2 and m <= m2):
        yield (y, m)
        m += 1
        if m > 12: m = 1; y += 1

def months_between_inclusive(d1:date, d2:date) -> int:
    return (d2.year - d1.year) * 12 + (d2.month - d1.month) + 1

def monthly_interest(amount:float, monthly_rate_pct:float) -> float:
    return float(amount) * (float(monthly_rate_pct)/100.0)

def interest_due_as_of(loan_id:int, as_of_date:date, start_override:date|None=None) -> float:
    """Interés pendiente desde el último pago de INTERES (o inicio) hasta as_of_date."""
    with closing(get_db()) as conn:
        loan = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if not loan:
            return 0.0
        created_dt = parse_dt(loan["created_at"])
        last_int = conn.execute(
            "SELECT MAX(paid_at) AS last FROM payments WHERE loan_id=? AND type='INTERES'",
            (loan_id,)
        ).fetchone()
        if start_override is not None:
            start_dt = datetime.combine(start_override, datetime.min.time())
        elif last_int and last_int["last"]:
            start_dt = datetime.strptime(last_int["last"], "%Y-%m-%d %H:%M:%S")
        else:
            start_dt = created_dt

        end_dt = datetime.combine(as_of_date, datetime.min.time())
        days = (end_dt - start_dt).days
        days = max(1, days)  # mínimo 1 día

        monthly = (loan["interest_rate"] or 20)/100.0
        daily = monthly/30.0
        principal = float(loan["amount"] or 0.0)
        return max(0.0, principal * daily * days)

def monthly_interest_breakdown(loan_row, from_month:str, to_month:str):
    """from_month / to_month en formato 'YYYY-MM'. Devuelve lista [(AAAA-MM, interes_mes)], total."""
    y1, m1 = map(int, from_month.split("-"))
    y2, m2 = map(int, to_month.split("-"))
    months = list(months_range_inclusive(y1, m1, y2, m2))
    per_month = monthly_interest(loan_row["amount"], loan_row["interest_rate"])
    rows = [("%04d-%02d" % (y, m), per_month) for (y,m) in months]
    total = per_month * len(months)
    return rows, total

# ======= DASHBOARD =======
@app.route("/dashboard")
@login_required
def dashboard():
    with closing(get_db()) as conn:
        today = date.today().isoformat()
        totals = conn.execute("""
          SELECT
            SUM(CASE WHEN status='ACTIVO' THEN amount ELSE 0 END) as capital_prestado,
            COUNT(CASE WHEN status='ACTIVO' THEN 1 END) as activos,
            COUNT(CASE WHEN due_date < ? AND status!='RETIRADO' THEN 1 END) as vencidos
          FROM loans
        """, (today,)).fetchone()

        upcoming = conn.execute("""
          SELECT id, customer_name, item_name, amount, due_date
          FROM loans
          WHERE status!='RETIRADO' AND due_date BETWEEN ? AND date(?, '+7 day')
          ORDER BY due_date ASC LIMIT 10
        """, (today, today)).fetchall()

        d0 = today + " 00:00:00"
        d1 = today + " 23:59:59"
        caja = conn.execute("""
          SELECT COALESCE(SUM(amount),0) AS neto
          FROM cash_movements
          WHERE when_at BETWEEN ? AND ?
        """, (d0, d1)).fetchone()

    body = f"""
    <div class="flex gap-2 justify-end no-print mb-3">
      <a href="{url_for('facturacion')}" class="px-4 py-2 rounded-xl bg-amber-500 hover:bg-amber-600 text-stone-900 font-semibold">🧾 Facturación</a>
    </div>
    <section class="grid md:grid-cols-3 gap-4">
      <div class="glass rounded-2xl p-4">
        <div class="text-sm text-yellow-200/80">Empeños activos</div>
        <div class="text-3xl font-extrabold">{(totals['activos'] or 0)}</div>
      </div>
      <div class="glass rounded-2xl p-4">
        <div class="text-sm text-yellow-200/80">Capital prestado (activos)</div>
        <div class="text-3xl font-extrabold">${(totals['capital_prestado'] or 0):.2f}</div>
      </div>
      <div class="glass rounded-2xl p-4">
        <div class="text-sm text-yellow-200/80">Caja de hoy</div>
        <div class="text-3xl font-extrabold">${(caja['neto'] or 0):.2f}</div>
      </div>
    </section>
    <section class="glass rounded-2xl p-4 mt-4">
      <h2 class="text-xl font-bold text-yellow-300 mb-2">Vencimientos próximos (7 días)</h2>
      <div class="overflow-auto rounded-xl border border-yellow-200/30">
        <table class="min-w-full text-sm">
          <thead class="thead-gold"><tr><th class="py-2 pl-3">ID</th><th>Cliente</th><th>Artículo</th><th>Monto</th><th>Vence</th></tr></thead>
          <tbody class="divide-y divide-stone-800/40 bg-black/40">
            {''.join(f"<tr><td class='py-2 pl-3'>{r['id']}</td><td>{r['customer_name']}</td><td>{r['item_name']}</td><td>${r['amount']:.2f}</td><td>{r['due_date']}</td></tr>" for r in upcoming) or "<tr><td class='py-2 pl-3' colspan='5'>Sin próximos vencimientos</td></tr>"}
          </tbody>
        </table>
      </div>
    </section>
    """
    return render_page(body, title="Dashboard", active="dashboard")

# ====== CLIENTES ======
CLIENTS_TPL = """
<section class="grid md:grid-cols-3 gap-6">
  <div class="glass rounded-2xl p-4">
    <h2 class="text-lg font-bold text-yellow-300 mb-2">Nuevo cliente</h2>
    <form method="post" action="{{ url_for('clients_new') }}" class="space-y-2">
      <input name="name" placeholder="Nombre completo" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <input name="document" placeholder="Documento / ID" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <input name="phone" placeholder="Teléfono" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <input name="address" placeholder="Dirección" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <button class="gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl">Guardar</button>
    </form>
  </div>
  <div class="md:col-span-2 glass rounded-2xl p-4">
    <form method="get" class="flex gap-2 mb-3">
      <input name="q" value="{{ q or '' }}" placeholder="Buscar por nombre o documento" class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <button class="px-4 py-2 rounded-xl bg-stone-900 text-yellow-300">Buscar</button>
    </form>
    <div class="overflow-auto rounded-xl border border-yellow-200/30">
      <table class="min-w-full text-sm">
        <thead class="thead-gold"><tr><th class="py-2 pl-3">ID</th><th>Nombre</th><th>Documento</th><th>Teléfono</th><th>Dirección</th><th>Registrado</th><th class="no-print pr-3 text-right">Acciones</th></tr></thead>
        <tbody class="divide-y divide-stone-800/40 bg-black/40">
          {% for c in rows %}
            <tr>
              <td class="py-2 pl-3">{{ c.id }}</td>
              <td>{{ c.name }}</td>
              <td>{{ c.document }}</td>
              <td>{{ c.phone or '' }}</td>
              <td>{{ c.address or '' }}</td>
              <td>{{ c.created_at[:10] }}</td>
              <td class="pr-3 text-right">
                <a href="{{ url_for('clients_confirm_delete', client_id=c.id) }}" class="px-2 py-1 rounded bg-red-700 hover:bg-red-800">Eliminar</a>
              </td>
            </tr>
          {% endfor %}
          {% if not rows %}<tr><td class="py-2 pl-3" colspan="7">Sin resultados</td></tr>{% endif %}
        </tbody>
      </table>
    </div>
  </div>
</section>
"""

CONFIRM_DELETE_CLIENT_TPL = """
<div class="max-w-md mx-auto glass p-6 rounded-2xl">
  <h2 class="text-xl font-bold text-yellow-300 mb-2">Eliminar cliente</h2>
  <p class="text-sm mb-3">Vas a eliminar al cliente <b>{{ client.name }}</b> (ID interno {{ client.id }}). Esta acción no se puede deshacer.</p>
  {% if error %}<div class="mb-3 p-2 bg-red-900/40 border border-red-700 rounded">{{ error }}</div>{% endif %}
  <form method="post" action="{{ url_for('clients_delete', client_id=client.id) }}" class="space-y-3">
    <label class="text-xs">Confirma tu contraseña</label>
    <input name="password" type="password" placeholder="Tu contraseña" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    <div class="flex gap-2">
      <button class="px-4 py-2 rounded-xl bg-red-700 hover:bg-red-800">Eliminar definitivamente</button>
      <a href="{{ url_for('clients') }}" class="px-4 py-2 rounded-xl border border-yellow-200/30">Cancelar</a>
    </div>
  </form>
</div>
"""

@app.route("/clientes")
@login_required
def clients():
    q = request.args.get("q","").strip()
    sql = "SELECT * FROM clients"
    params=[]
    if q:
        sql += " WHERE name LIKE ? OR document LIKE ?"
        like=f"%{q}%"; params=[like,like]
    sql += " ORDER BY id DESC LIMIT 500"
    with closing(get_db()) as conn:
        rows = conn.execute(sql, params).fetchall()
    body = render_template_string(CLIENTS_TPL, rows=rows, q=q)
    return render_page(body, title="Clientes", active="clients")

@app.route("/clientes/nuevo", methods=["POST"])
@login_required
def clients_new():
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_db()) as conn:
        conn.execute("INSERT INTO clients(name,document,phone,address,created_at) VALUES (?,?,?,?,?)",
                     (request.form["name"].strip(), request.form["document"].strip(),
                      request.form.get("phone","").strip(), request.form.get("address","").strip(), now))
        conn.commit()
    return redirect(url_for("clients"))

@app.route("/clientes/confirm/<int:client_id>")
@login_required
def clients_confirm_delete(client_id:int):
    with closing(get_db()) as conn:
        c = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not c: return "No encontrado", 404
    return render_page(render_template_string(CONFIRM_DELETE_CLIENT_TPL, client=c, error=None), title="Eliminar cliente", active="clients")

@app.route("/clientes/delete/<int:client_id>", methods=["POST"])
@login_required
def clients_delete(client_id:int):
    password = request.form.get("password","")
    with closing(get_db()) as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (session.get("uid"),)).fetchone()
        c = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not user or not check_password_hash(user["pass_hash"], password):
            return render_page(render_template_string(CONFIRM_DELETE_CLIENT_TPL, client=c, error="Contraseña incorrecta"), title="Eliminar cliente", active="clients")
        conn.execute("DELETE FROM clients WHERE id=?", (client_id,))
        conn.commit()
    return redirect(url_for("clients"))

# ====== EMPeÑOS (lista + alta) ======
LIST_TPL = """
<div class="grid lg:grid-cols-3 gap-6">
  <section class="glass rounded-2xl p-4">
    <h2 class="text-lg font-bold text-yellow-300 mb-2">Nuevo empeño</h2>
    <form method="post" action="{{ url_for('new_loan') }}" enctype="multipart/form-data" class="space-y-2">
      <input name="item_name" placeholder="Artículo" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <div class="grid grid-cols-2 gap-2">
        <input name="weight_grams" type="number" step="0.01" placeholder="Peso (g)" required class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
        <input name="amount" type="number" step="0.01" placeholder="Monto ($)" required class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      </div>
      <div class="grid grid-cols-2 gap-2">
        <input name="customer_name" placeholder="Cliente" required class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
        <input name="customer_id" placeholder="Documento" required class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      </div>
      <input name="phone" placeholder="Teléfono" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <input name="photo" type="file" accept="image/*" capture="environment" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>

      <div class="grid grid-cols-3 gap-2">
        <input name="interest_rate" type="number" step="0.01" value="{{ default_rate }}" class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
        <input name="due_date" type="date" value="{{ default_due }}" class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
        <button class="gold-gradient text-stone-900 font-semibold rounded-xl">Guardar</button>
      </div>

      <div class="grid grid-cols-2 gap-2">
        <div class="col-span-2 text-xs text-yellow-200/70">Opcional: establecer <b>fecha de inicio</b> (si el empeño fue días antes)</div>
        <input name="start_date" type="date" class="rounded-xl border border-yellow-200/30 bg-black/40 p-2" />
      </div>
    </form>
  </section>

  <section class="lg:col-span-2 glass rounded-2xl p-4">
    <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-3 mb-3">
      <h2 class="text-lg font-bold text-yellow-300">Empeños</h2>
      <form method="get" action="{{ url_for('index') }}" class="flex flex-col sm:flex-row gap-2 no-print">
        <input name="q" value="{{ q or '' }}" placeholder="Buscar nombre, ID, tel o artículo" class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
        <select name="status" class="rounded-xl border border-yellow-200/30 bg-black/40 p-2">
          {% for s in ["TODOS","ACTIVO","VENCIDO","RETIRADO"] %}
            <option value="{{s}}" {% if status==s %}selected{% endif %}>{{s}}</option>
          {% endfor %}
        </select>
        <button class="rounded-xl bg-stone-900 text-yellow-300 px-3 py-2 hover:bg-black transition">Filtrar</button>
      </form>
    </div>
    <div class="overflow-auto rounded-xl border border-yellow-200/30">
      <table class="min-w-full text-sm">
        <thead class="thead-gold"><tr><th class="py-2 pl-3">#</th><th>Inicio</th><th>Artículo</th><th>Cliente</th><th>Monto</th><th>Vence</th><th>Interés</th><th>Estado</th><th class="no-print pr-3">Acciones</th></tr></thead>
        <tbody class="divide-y divide-stone-800/40 bg-black/40">
        {% for r in rows %}
          {% set start_dt = parse_dt(r.created_at) %}
          {% set last_int_dt = last_interest_paid_dt_fn(r.id) %}
          {% set next_int_due = next_interest_due_date_fn(r, last_int_dt) %}
          {% set overdue_m = months_overdue_fn(r, now.date()) %}
          {% set monthly = r.interest_rate or 20 %}
          {% set monthly_amt = (r.amount or 0) * (monthly/100.0) %}
          {% set interest_due_now = monthly_amt * overdue_m %}
          {% set overdue = r.due_date and (parse_date(r.due_date) < now.date()) %}
          {% set show_status = 'VENCIDO' if overdue and r.status!='RETIRADO' else r.status %}
          <tr>
            <td class="py-2 pl-3 font-semibold">{{ r.id }}</td>
            <td>{{ r.created_at[:10] }}</td>
            <td>
              <div class="font-semibold">{{ r.item_name }}</div>
              <div class="text-yellow-200/70">{{ ("%.2f"|format(r.weight_grams)) }} g</div>
              {% if r.photo_path %}<img src="{{ r.photo_path }}" class="h-12 w-12 object-cover rounded mt-1"/>{% endif %}
            </td>
            <td>{{ r.customer_name }}</td>
            <td>${{ '%.2f'|format(r.amount or 0) }}</td>
            <td>
              {{ r.due_date }}
              <div class="text-xs text-yellow-200/70 mt-1">Próx. interés: <b>{{ next_int_due }}</b></div>
            </td>
            <td>
              <div class="text-xs">/mes: ${{ '%.2f'|format(monthly_amt) }}</div>
              <div class="text-xs">Atraso: {{ overdue_m }} mes(es)</div>
              <div class="text-xs">Interés vencido: ${{ '%.2f'|format(interest_due_now) }}</div>
              <a class="text-xs underline" href="{{ url_for('interest_calc_page', loan_id=r.id) }}">Ver meses</a>
            </td>
            <td>{{ show_status }}</td>
            <td class="no-print">
              <div class="flex flex-wrap gap-2">
                <a href="{{ url_for('ticket', loan_id=r.id) }}" class="px-2 py-1 border rounded">Recibo</a>
                <a href="{{ url_for('edit_loan_page', loan_id=r.id) }}" class="px-2 py-1 gold-gradient text-stone-900 rounded">Editar</a>
                <a href="{{ url_for('payment_new_page', loan_id=r.id) }}" class="px-2 py-1 bg-emerald-700 rounded">Pago</a>
                <a href="{{ url_for('loan_confirm_delete', loan_id=r.id) }}" class="px-2 py-1 bg-red-700 rounded">Eliminar</a>
              </div>
            </td>
          </tr>
        {% endfor %}
        {% if not rows %}<tr><td class="py-2 pl-3" colspan="9">Sin resultados</td></tr>{% endif %}
        </tbody>
      </table>
    </div>
  </section>
</div>
"""

@app.route("/")
def __root():
    if not session.get("uid"):
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))

@app.route("/empenos")
@login_required
def index():
    q = request.args.get("q","").strip()
    status = request.args.get("status","TODOS")
    params, where = [], []
    if q:
        like = f"%{q}%"
        where.append("(item_name LIKE ? OR customer_name LIKE ? OR customer_id LIKE ? OR phone LIKE ?)")
        params += [like, like, like, like]
    if status and status != "TODOS":
        where.append("status=?"); params.append(status)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    sql = f"SELECT * FROM loans {where_sql} ORDER BY id DESC LIMIT 500"
    with closing(get_db()) as conn:
        rows = conn.execute(sql, params).fetchall()
    now = datetime.now()
    default_rate = float(get_setting("default_interest_rate","20"))
    term_days = int(get_setting("default_term_days","90"))
    default_due = (datetime.now() + timedelta(days=term_days)).strftime("%Y-%m-%d")
    # helpers para plantilla
    def _last_int_dt(loan_id:int):
        with closing(get_db()) as c:
            return last_interest_paid_dt(c, loan_id)
    def _next_int_due(row, last_dt):
        return next_interest_due_date_raw(row, last_dt).strftime("%Y-%m-%d")
    def _months_overdue(row, as_of):
        with closing(get_db()) as c:
            return months_overdue_since(c, row, as_of)
    body = render_template_string(
        LIST_TPL, rows=rows, q=q, status=status, now=now, parse_dt=parse_dt, parse_date=parse_date,
        default_rate=default_rate, default_due=default_due,
        last_interest_paid_dt_fn=_last_int_dt, next_interest_due_date_fn=_next_int_due,
        months_overdue_fn=_months_overdue
    )
    return render_page(body, title="Empeños", active="loans")

@app.route("/new", methods=["POST"])
@login_required
def new_loan():
    now_dt = datetime.now()
    # Fecha de inicio opcional (permite backdating)
    start_date_str = (request.form.get("start_date") or "").strip()
    if start_date_str:
        try:
            start_dt = datetime.strptime(start_date_str, "%Y-%m-%d").replace(hour=9, minute=0, second=0)
        except Exception:
            start_dt = now_dt
    else:
        start_dt = now_dt
    now_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")

    item_name = request.form.get("item_name","").strip()
    weight_grams = float(request.form.get("weight_grams",0) or 0)
    customer_name = request.form.get("customer_name","").strip()
    customer_id = request.form.get("customer_id","").strip()
    phone = request.form.get("phone","").strip()
    amount = float(request.form.get("amount",0) or 0)
    interest_rate = float(request.form.get("interest_rate", get_setting("default_interest_rate","20")))
    due_date = request.form.get("due_date") or (start_dt + timedelta(days=int(get_setting("default_term_days","90")))).strftime("%Y-%m-%d")

    photo_path = ''
    file = request.files.get('photo')
    if file and getattr(file, 'filename',''):
        fname = f"{int(time.time())}_" + secure_filename(file.filename)
        disk_path = UPLOAD_DIR / fname
        file.save(str(disk_path))
        photo_path = '/uploads/' + fname

    if not (item_name and customer_name and customer_id and phone):
        return "Datos incompletos", 400

    with closing(get_db()) as conn:
        conn.execute("""INSERT INTO loans (created_at,item_name,weight_grams,customer_name,customer_id,phone,amount,interest_rate,due_date,photo_path)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     (now_str, item_name, weight_grams, customer_name, customer_id, phone, amount, interest_rate, due_date, photo_path))
        conn.execute("INSERT INTO cash_movements(when_at,concept,amount,ref) VALUES (?,?,?,?)",
                     (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), f"Desembolso empeño {customer_name}", -amount, "LOAN"))
        conn.commit()
    return redirect(url_for("index"))

# ====== Editar, ticket ======
EDIT_TPL = """
<h2 class="text-xl font-bold text-yellow-300 mb-3">Editar empeño #{{row.id}}</h2>
<form method='post' enctype='multipart/form-data' class='grid md:grid-cols-2 gap-3 glass p-4 rounded-2xl'>
  <input name='item_name' value='{{row.item_name}}' class='rounded-xl border border-yellow-200/30 bg-black/40 p-2'/>
  <input name='weight_grams' type='number' step='0.01' value='{{row.weight_grams}}' class='rounded-xl border border-yellow-200/30 bg-black/40 p-2'/>
  <input name='customer_name' value='{{row.customer_name}}' class='rounded-xl border border-yellow-200/30 bg-black/40 p-2'/>
  <input name='customer_id' value='{{row.customer_id}}' class='rounded-xl border border-yellow-200/30 bg-black/40 p-2'/>
  <input name='phone' value='{{row.phone}}' class='rounded-xl border border-yellow-200/30 bg-black/40 p-2'/>
  <input name='amount' type='number' step='0.01' value='{{row.amount}}' class='rounded-xl border border-yellow-200/30 bg-black/40 p-2'/>
  <input name='interest_rate' type='number' step='0.01' value='{{row.interest_rate}}' class='rounded-xl border border-yellow-200/30 bg-black/40 p-2'/>
  <input name='due_date' type='date' value='{{row.due_date}}' class='rounded-xl border border-yellow-200/30 bg-black/40 p-2'/>
  <div class='md:col-span-2'>
    {% if row.photo_path %}<img src='{{ row.photo_path }}' class='h-24 w-24 object-cover rounded-lg ring-2 ring-amber-300 mb-2'/>{% endif %}
    <input name='photo' type='file' accept='image/*' capture='environment' class='w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2'/>
  </div>
  <div class="md:col-span-2 flex gap-2">
    <button class='gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl'>Guardar</button>
    <a href='{{ url_for("index") }}' class='px-4 py-2 rounded-xl border border-yellow-200/30'>Cancelar</a>
  </div>
</form>
"""

@app.route("/edit/<int:loan_id>")
@login_required
def edit_loan_page(loan_id: int):
    with closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not row: return "No encontrado", 404
    return render_page(render_template_string(EDIT_TPL, row=row), title=f"Editar {loan_id}", active="loans")

@app.route("/edit/<int:loan_id>", methods=["POST"])
@login_required
def edit_loan(loan_id: int):
    file = request.files.get('photo')
    photo_path = None
    if file and getattr(file, 'filename',''):
        fname = f"{int(time.time())}_" + secure_filename(file.filename)
        disk_path = UPLOAD_DIR / fname
        file.save(str(disk_path))
        photo_path = '/uploads/' + fname
    fields = (
        request.form.get("item_name","").strip(),
        float(request.form.get("weight_grams",0) or 0),
        request.form.get("customer_name","").strip(),
        request.form.get("customer_id","").strip(),
        request.form.get("phone","").strip(),
        float(request.form.get("amount",0) or 0),
        float(request.form.get("interest_rate",20) or 20),
        request.form.get("due_date"),
        loan_id
    )
    with closing(get_db()) as conn:
        conn.execute("""UPDATE loans SET item_name=?, weight_grams=?, customer_name=?, customer_id=?, phone=?,
                        amount=?, interest_rate=?, due_date=? WHERE id=?""", fields)
        if photo_path:
            conn.execute("UPDATE loans SET photo_path=? WHERE id=?", (photo_path, loan_id))
        conn.commit()
    return redirect(url_for("index"))

# ====== Ticket (incluye WhatsApp/SMS) ======
TICKET_TPL = """
<div class='max-w-xl mx-auto glass p-6 rounded-2xl'>
  <h1 class='text-2xl font-extrabold text-stone-950 text-center mb-1'>{{ brand }}</h1>
  <p class='text-center text-sm text-stone-300 mb-4'>Recibo #{{ row.id }} · {{ row.created_at[:10] }}</p>
  <div class='grid grid-cols-2 gap-3 text-sm'>
    <div><div class='font-semibold'>Cliente</div><div>{{ row.customer_name }}</div><div>ID: {{ row.customer_id }}</div><div>Tel: {{ row.phone }}</div></div>
    <div><div class='font-semibold'>Artículo</div><div>{{ row.item_name }}</div><div>Peso: {{ ("%.2f"|format(row.weight_grams)) }} g</div></div>
  </div>
  <div class='my-3 h-px bg-gradient-to-r from-transparent via-amber-300 to-transparent'></div>
  <div class='text-sm space-y-1'>
    <div>Monto: <b>${{ '%.2f'|format(row.amount or 0) }}</b></div>
    <div>Interés mensual: <b>{{ '%.2f'|format(row.interest_rate) }}%</b></div>
    <div>Vence (empeño): <b>{{ row.due_date }}</b></div>
    <div>Próximo interés vence: <b>{{ next_interest_due }}</b></div>
    <div class='mt-2'>Interés acumulado hoy (mín. 1 día): <b>${{ '%.2f'|format(interest_today) }}</b></div>
    <div>Total a la fecha: <b>${{ '%.2f'|format(total_today) }}</b></div>
    <div class='my-2 h-px bg-gradient-to-r from-transparent via-amber-300 to-transparent'></div>
    <div>Interés al vencimiento (~{{ '%.0f'|format(months_to_due) }} meses): <b>${{ '%.2f'|format(interest_at_due) }}</b></div>
    <div>Total al vencimiento: <b>${{ '%.2f'|format(total_at_due) }}</b></div>
    {% if m_rows %}
      <div class='mt-3'>
        <div class='font-semibold mb-1'>Desglose mensual estimado:</div>
        <ul class='list-disc pl-5'>
          {% for mk, mi in m_rows %}
            <li>{{ mk }}: ${{ '%.2f'|format(mi) }}</li>
          {% endfor %}
        </ul>
        <div class='mt-1'>Suma ({{ m_rows|length }} mes/es): <b>${{ '%.2f'|format(m_total) }}</b></div>
      </div>
    {% endif %}
  </div>
  <div class='mt-4 flex gap-2 justify-center no-print'>
    <button onclick='window.print()' class='px-4 py-2 rounded-xl bg-stone-900 text-yellow-300'>Imprimir</button>
    {% if wa_url %}
      <a href='{{ wa_url }}' target='_blank' class='px-4 py-2 rounded-xl bg-green-700 hover:bg-green-800'>Enviar WhatsApp</a>
    {% endif %}
    {% if sms_url %}
      <a href='{{ sms_url }}' class='px-4 py-2 rounded-xl bg-blue-700 hover:bg-blue-800'>Enviar SMS</a>
    {% endif %}
  </div>
</div>
"""

def build_ticket_message(row, interest_today, total_today, interest_at_due, total_at_due, next_interest_due):
    lines = [
        f"{APP_BRAND} - Recibo #{row['id']}",
        f"Fecha: {row['created_at'][:10]}",
        f"Cliente: {row['customer_name']} (ID {row['customer_id']})",
        f"Artículo: {row['item_name']} - {row['weight_grams']:.2f} g",
        f"Capital: ${row['amount']:.2f}",
        f"Interés mensual: {row['interest_rate']:.2f}%",
        f"Vence (empeño): {row['due_date']}",
        f"Próximo interés: {next_interest_due}",
        f"Interés al día: ${interest_today:.2f}",
        f"Total a la fecha: ${total_today:.2f}",
        f"Interés al vencimiento: ${interest_at_due:.2f}",
        f"Total al vencimiento: ${total_at_due:.2f}"
    ]
    return "\n".join(lines)

@app.route("/ticket/<int:loan_id>")
@login_required
def ticket(loan_id: int):
    with closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        last_int = last_interest_paid_dt(conn, loan_id)
    if not row: return "No encontrado", 404
    created_dt = parse_dt(row["created_at"])
    now = datetime.now()
    days_elapsed = max(1, (now - created_dt).days)
    monthly_rate = (row["interest_rate"] or 20)/100
    daily_rate = monthly_rate/30.0
    amount = row["amount"] or 0.0
    interest_today = amount*daily_rate*days_elapsed
    total_today = amount + interest_today
    due_date = datetime.strptime(row["due_date"], "%Y-%m-%d").date()
    months_to_due = (due_date - created_dt.date()).days/30.0
    interest_at_due = amount*monthly_rate*months_to_due
    total_at_due = amount + interest_at_due

    next_int_due = next_interest_due_date_raw(row, last_int).strftime("%Y-%m-%d")

    from_m = request.args.get("from_m")
    to_m = request.args.get("to_m")
    m_rows = []
    m_total = 0.0
    if from_m and to_m:
        m_rows, m_total = monthly_interest_breakdown(row, from_m, to_m)

    phone = normalize_phone(row["phone"])
    wa_url = None
    sms_url = None
    msg = build_ticket_message(row, interest_today, total_today, interest_at_due, total_at_due, next_int_due)
    if phone:
        wa_url = f"https://wa.me/{phone}?text={quote_plus(msg)}"
        sms_url = f"sms:{phone}?&body={quote_plus(msg)}"

    body = render_template_string(
        TICKET_TPL, row=row, interest_today=interest_today, total_today=total_today,
        months_to_due=months_to_due, interest_at_due=interest_at_due, total_at_due=total_at_due,
        brand=APP_BRAND, wa_url=wa_url, sms_url=sms_url,
        next_interest_due=next_int_due, m_rows=m_rows, m_total=m_total
    )
    return render_page(body, title="Recibo", active="loans")

# ====== Estimador de interés mensual por rango ======
INTEREST_CALC_TPL = """
<div class="max-w-xl mx-auto glass p-6 rounded-2xl">
  <h2 class="text-xl font-bold text-yellow-300 mb-3">Interés por meses — Empeño #{{ row.id }}</h2>
  <form method="get" class="grid grid-cols-2 gap-2 mb-3">
    <div>
      <label class="text-xs">Desde (mes)</label>
      <input type="month" name="from_m" value="{{ from_m or default_from }}" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    </div>
    <div>
      <label class="text-xs">Hasta (mes)</label>
      <input type="month" name="to_m" value="{{ to_m or default_to }}" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    </div>
    <div class="col-span-2">
      <button class="gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl">Calcular</button>
      <a href="{{ url_for('index') }}" class="px-4 py-2 rounded-xl border border-yellow-200/30">Volver</a>
    </div>
  </form>

  {% if rows is not none %}
    <div class="overflow-auto rounded-xl border border-yellow-200/30">
      <table class="min-w-full text-sm">
        <thead class="thead-gold"><tr><th class="py-2 pl-3">Mes</th><th class="text-right pr-3">Interés</th></tr></thead>
        <tbody class="divide-y divide-stone-800/40 bg-black/40">
          {% for mk, mi in rows %}
            <tr><td class="py-2 pl-3">{{ mk }}</td><td class="text-right pr-3">${{ '%.2f'|format(mi) }}</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
    <div class="text-right mt-2">Total ({{ rows|length }} mes/es): <b>${{ '%.2f'|format(total) }}</b></div>
  {% endif %}
</div>
"""

@app.route("/interes/<int:loan_id>")
@login_required
def interest_calc_page(loan_id:int):
    with closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not row: return "No encontrado", 404
    start = parse_dt(row["created_at"]).date()
    today = date.today()
    default_from = f"{start.year:04d}-{start.month:02d}"
    default_to = f"{today.year:04d}-{today.month:02d}"
    from_m = request.args.get("from_m", default_from)
    to_m = request.args.get("to_m", default_to)
    rows = None
    total = 0.0
    if from_m and to_m:
        rows, total = monthly_interest_breakdown(row, from_m, to_m)
    body = render_template_string(INTEREST_CALC_TPL, row=row, rows=rows, total=total, from_m=from_m, to_m=to_m,
                                  default_from=default_from, default_to=default_to)
    return render_page(body, title="Interés por meses", active="loans")

# ====== Pagos con fechas y modo de aplicación ======
PAY_TPL = """
<h2 class="text-xl font-bold text-yellow-300 mb-3">Pago — empeño #{{row.id}} ({{row.customer_name}})</h2>

<div class="glass p-4 rounded-2xl mb-3 text-sm">
  <div>Capital actual: <b>${{ '%.2f'|format(row.amount) }}</b></div>
  <div>Interés estimado al <b>{{ as_of }}</b>: <b>${{ '%.2f'|format(interest_due) }}</b></div>
  <p class="mt-2 text-yellow-200/80">Elige cómo aplicar el pago y desde qué fecha calcular el interés.</p>
</div>

<form method="post" class="glass p-4 rounded-2xl space-y-3">
  <div class="grid md:grid-cols-6 grid-cols-1 gap-2">
    <div class="md:col-span-2">
      <label class="text-xs">Desde (opcional)</label>
      <input name="from_date" type="date" value="{{ from_date or '' }}" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <p class="text-[11px] text-yellow-200/70 mt-1">Si vacío: usa último pago de interés o la fecha inicial del empeño.</p>
    </div>
    <div class="md:col-span-2">
      <label class="text-xs">Hasta (fecha de pago efectiva)</label>
      <input name="as_of_date" type="date" value="{{ as_of }}" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    </div>
    <div class="md:col-span-2">
      <label class="text-xs">Aplicar a</label>
      <select name="apply_mode" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2">
        <option value="AUTO" selected>AUTO (primero interés, resto a capital)</option>
        <option value="SOLO_INTERES">SOLO INTERÉS (no abona capital)</option>
        <option value="SOLO_CAPITAL">SOLO CAPITAL (no cubre interés)</option>
      </select>
    </div>
    <div class="md:col-span-2">
      <label class="text-xs">Monto</label>
      <input name="amount" type="number" step="0.01" placeholder="0.00" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    </div>
  </div>

  <div class="grid md:grid-cols-2 grid-cols-1 gap-2">
    <div>
      <label class="text-xs">Rango (mes a mes) — opcional</label>
      <div class="grid grid-cols-2 gap-2">
        <input type="month" name="from_m" class="rounded-xl border border-yellow-200/30 bg-black/40 p-2" />
        <input type="month" name="to_m" class="rounded-xl border border-yellow-200/30 bg-black/40 p-2" />
      </div>
      <p class="text-[11px] text-yellow-200/70 mt-1">Ej.: Agosto a Diciembre para estimar suma de intereses.</p>
    </div>
    <div>
      {% if m_rows %}
        <div class="text-xs mb-1 font-semibold">Estimación seleccionada:</div>
        <ul class="list-disc pl-5 text-xs">
          {% for mk, mi in m_rows %}
            <li>{{ mk }}: ${{ '%.2f'|format(mi) }}</li>
          {% endfor %}
        </ul>
        <div class="text-xs mt-1">Total estimado: <b>${{ '%.2f'|format(m_total) }}</b></div>
      {% endif %}
    </div>
  </div>

  <input name="notes" placeholder="Notas (opcional)" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
  <div class="flex gap-2">
    <button class="gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl">Registrar</button>
    <a href="{{ url_for('index') }}" class="px-4 py-2 rounded-xl border border-yellow-200/30">Cancelar</a>
  </div>
</form>
"""

@app.route("/pago/<int:loan_id>")
@login_required
def payment_new_page(loan_id:int):
    with closing(get_db()) as conn:
        row = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not row:
        return "No encontrado", 404

    try:
        as_of_str = request.args.get("as_of") or date.today().isoformat()
        as_of = datetime.strptime(as_of_str, "%Y-%m-%d").date()
    except Exception:
        as_of = date.today()

    from_str = request.args.get("from")
    start_override = None
    if from_str:
        try:
            start_override = datetime.strptime(from_str, "%Y-%m-%d").date()
        except Exception:
            start_override = None

    interest_due = interest_due_as_of(loan_id, as_of, start_override)

    from_m = request.args.get("from_m")
    to_m = request.args.get("to_m")
    m_rows = None
    m_total = 0.0
    if from_m and to_m:
        m_rows, m_total = monthly_interest_breakdown(row, from_m, to_m)

    return render_page(
        render_template_string(
            PAY_TPL, row=row, interest_due=interest_due, as_of=as_of.isoformat(),
            from_date=(start_override.isoformat() if start_override else ""),
            m_rows=m_rows, m_total=m_total
        ),
        title="Pago", active="loans"
    )

@app.route("/pago/<int:loan_id>", methods=["POST"])
@login_required
def payment_new(loan_id:int):
    amt = float(request.form.get("amount",0) or 0)
    notes = request.form.get("notes","").strip()
    mode = (request.form.get("apply_mode","AUTO") or "AUTO").upper()
    if amt <= 0:
        return "Monto inválido", 400

    try:
        as_of = datetime.strptime(request.form.get("as_of_date", date.today().isoformat()), "%Y-%m-%d").date()
    except Exception:
        as_of = date.today()
    from_str = request.form.get("from_date","").strip()
    start_override = None
    if from_str:
        try:
            start_override
            start_override = datetime.strptime(from_str, "%Y-%m-%d").date()
        except Exception:
            start_override = None

    now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    paid_ts = datetime.combine(as_of, datetime.min.time()).strftime("%Y-%m-%d %H:%M:%S")

    with closing(get_db()) as conn:
        loan = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if not loan:
            return "No encontrado", 404

        interest_due = interest_due_as_of(loan_id, as_of, start_override)

        to_interest = min(amt, interest_due)
        to_principal = max(0.0, amt - to_interest)

        if mode == "SOLO_INTERES":
            to_interest = amt
            to_principal = 0.0
        elif mode == "SOLO_CAPITAL":
            to_interest = 0.0
            to_principal = amt

        if to_interest > 0:
            conn.execute(
                "INSERT INTO payments(loan_id,paid_at,amount,type,notes) VALUES (?,?,?,?,?)",
                (loan_id, paid_ts, round(to_interest,2), "INTERES", notes)
            )
        if to_principal > 0:
            conn.execute(
                "INSERT INTO payments(loan_id,paid_at,amount,type,notes) VALUES (?,?,?,?,?)",
                (loan_id, paid_ts, round(to_principal,2), "ABONO", notes)
            )
            conn.execute("UPDATE loans SET amount = amount - ? WHERE id=?", (round(to_principal,2), loan_id))

        conn.execute(
            "INSERT INTO cash_movements(when_at,concept,amount,ref) VALUES (?,?,?,?)",
            (now_ts, f"Pago {mode} empeño #{loan_id}", round(amt,2), "PAY")
        )

        fully_covered_interest = (mode!="SOLO_CAPITAL") and (abs(to_interest - interest_due) < 0.01)
        if fully_covered_interest and to_principal == 0.0:
            try:
                renew_days = int(get_setting("renew_days","30") or "30")
            except:
                renew_days = 30
            current_due = datetime.strptime(loan["due_date"], "%Y-%m-%d").date() if loan["due_date"] else as_of
            base = as_of if as_of > current_due else current_due
            new_due = (base + timedelta(days=renew_days)).strftime("%Y-%m-%d")
            conn.execute("UPDATE loans SET due_date=? WHERE id=?", (new_due, loan_id))

        conn.commit()
    return redirect(url_for("index"))

# ====== Confirmación de eliminación de Empeños ======
CONFIRM_DELETE_LOAN_TPL = """
<div class="max-w-md mx-auto glass p-6 rounded-2xl">
  <h2 class="text-xl font-bold text-yellow-300 mb-2">Eliminar empeño</h2>
  <p class="text-sm mb-3">Vas a eliminar el empeño <b>#{{ loan.id }}</b> del cliente <b>{{ loan.customer_name }}</b>. Esta acción no se puede deshacer.</p>
  {% if error %}<div class="mb-3 p-2 bg-red-900/40 border border-red-700 rounded">{{ error }}</div>{% endif %}
  <form method="post" action="{{ url_for('loan_delete', loan_id=loan.id) }}" class="space-y-3">
    <label class="text-xs">Confirma tu contraseña</label>
    <input name="password" type="password" placeholder="Tu contraseña" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    <div class="flex gap-2">
      <button class="px-4 py-2 rounded-xl bg-red-700 hover:bg-red-800">Eliminar definitivamente</button>
      <a href="{{ url_for('index') }}" class="px-4 py-2 rounded-xl border border-yellow-200/30">Cancelar</a>
    </div>
  </form>
</div>
"""

@app.route("/empenos/confirm/<int:loan_id>")
@login_required
def loan_confirm_delete(loan_id:int):
    with closing(get_db()) as conn:
        loan = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not loan: return "No encontrado", 404
    return render_page(render_template_string(CONFIRM_DELETE_LOAN_TPL, loan=loan, error=None), title="Eliminar empeño", active="loans")

@app.route("/empenos/delete/<int:loan_id>", methods=["POST"])
@login_required
def loan_delete(loan_id:int):
    password = request.form.get("password","")
    with closing(get_db()) as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (session.get("uid"),)).fetchone()
        loan = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if not user or not check_password_hash(user["pass_hash"], password):
            return render_page(render_template_string(CONFIRM_DELETE_LOAN_TPL, loan=loan, error="Contraseña incorrecta"), title="Eliminar empeño", active="loans")
        conn.execute("DELETE FROM loans WHERE id=?", (loan_id,))
        conn.commit()
    return redirect(url_for("index"))

# ====== Caja (+ eliminar) ======
CASH_TPL = """
<h2 class="text-xl font-bold text-yellow-300 mb-3">Caja</h2>
<form method="post" class="glass p-4 rounded-2xl space-y-2">
  <div class="grid grid-cols-3 gap-2">
    <input name="concept" placeholder="Concepto" required class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    <input name="amount" type="number" step="0.01" placeholder="Monto (+ingreso / -egreso)" required class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    <input name="ref" placeholder="Referencia (opcional)" class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
  </div>
  <button class="gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl">Registrar</button>
</form>
<div class="glass rounded-2xl p-4 mt-4">
  <h3 class="font-semibold mb-2">Movimientos recientes</h3>
  <div class="overflow-auto rounded-xl border border-yellow-200/30">
    <table class="min-w-full text-sm">
      <thead class="thead-gold"><tr><th class="py-2 pl-3">#</th><th>Fecha</th><th>Concepto</th><th>Ref</th><th class="text-right pr-3">Monto</th><th class="text-right pr-3 no-print">Acciones</th></tr></thead>
      <tbody class="divide-y divide-stone-800/40 bg-black/40">
        {% for m in rows %}
          <tr>
            <td class="py-2 pl-3">{{ m.id }}</td>
            <td>{{ m.when_at }}</td>
            <td>{{ m.concept }}</td>
            <td>{{ m.ref or '' }}</td>
            <td class="pr-3 text-right">${{ '%.2f'|format(m.amount) }}</td>
            <td class="pr-3 text-right no-print">
              <a href="{{ url_for('cash_confirm_delete', mov_id=m.id) }}" class="px-2 py-1 bg-red-700 rounded hover:bg-red-800">Eliminar</a>
            </td>
          </tr>
        {% endfor %}
        {% if not rows %}<tr><td class="py-2 pl-3" colspan="6">Sin movimientos</td></tr>{% endif %}
      </tbody>
    </table>
  </div>
</div>
"""

CONFIRM_DELETE_CASH_TPL = """
<div class="max-w-md mx-auto glass p-6 rounded-2xl">
  <h2 class="text-xl font-bold text-yellow-300 mb-2">Eliminar movimiento de caja</h2>
  <p class="text-sm mb-3">Vas a eliminar el movimiento <b>#{{ mov.id }}</b>: {{ mov.when_at }} — {{ mov.concept }} — ${{ '%.2f'|format(mov.amount) }}.</p>
  {% if error %}<div class="mb-3 p-2 bg-red-900/40 border border-red-700 rounded">{{ error }}</div>{% endif %}
  <form method="post" action="{{ url_for('cash_delete', mov_id=mov.id) }}" class="space-y-3">
    <label class="text-xs">Confirma tu contraseña</label>
    <input name="password" type="password" placeholder="Tu contraseña" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    <div class="flex gap-2">
      <button class="px-4 py-2 rounded-xl bg-red-700 hover:bg-red-800">Eliminar definitivamente</button>
      <a href="{{ url_for('cash') }}" class="px-4 py-2 rounded-xl border border-yellow-200/30">Cancelar</a>
    </div>
  </form>
</div>
"""

@app.route("/caja", methods=["GET","POST"])
@login_required
def cash():
    if request.method=="POST":
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        concept = request.form["concept"].strip()
        amount = float(request.form.get("amount",0) or 0)
        ref = request.form.get("ref","").strip()
        with closing(get_db()) as conn:
            conn.execute("INSERT INTO cash_movements(when_at,concept,amount,ref) VALUES (?,?,?,?)", (now, concept, amount, ref))
            conn.commit()
        return redirect(url_for("cash"))
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT * FROM cash_movements ORDER BY id DESC LIMIT 200").fetchall()
    return render_page(render_template_string(CASH_TPL, rows=rows), title="Caja", active="cash")

@app.route("/caja/confirm/<int:mov_id>")
@login_required
def cash_confirm_delete(mov_id:int):
    with closing(get_db()) as conn:
        mov = conn.execute("SELECT * FROM cash_movements WHERE id=?", (mov_id,)).fetchone()
    if not mov: return "No encontrado", 404
    return render_page(render_template_string(CONFIRM_DELETE_CASH_TPL, mov=mov, error=None), title="Eliminar movimiento", active="cash")

@app.route("/caja/delete/<int:mov_id>", methods=["POST"])
@login_required
def cash_delete(mov_id:int):
    password = request.form.get("password","")
    with closing(get_db()) as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (session.get("uid"),)).fetchone()
        mov = conn.execute("SELECT * FROM cash_movements WHERE id=?", (mov_id,)).fetchone()
        if not user or not check_password_hash(user["pass_hash"], password):
            return render_page(render_template_string(CONFIRM_DELETE_CASH_TPL, mov=mov, error="Contraseña incorrecta"), title="Eliminar movimiento", active="cash")
        conn.execute("DELETE FROM cash_movements WHERE id=?", (mov_id,))
        conn.commit()
    return redirect(url_for("cash"))

# ====== Reportes ======
REPORTS_TPL = """
<h2 class="text-xl font-bold text-yellow-300 mb-3">Reportes</h2>
<form method="get" class="glass p-4 rounded-2xl grid md:grid-cols-4 gap-2 mb-4">
  <input type="date" name="from" value="{{ dfrom }}" class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
  <input type="date" name="to" value="{{ dto }}" class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
  <select name="kind" class="rounded-xl border border-yellow-200/30 bg-black/40 p-2">
    <option value="intereses" {% if kind=='intereses' %}selected{% endif %}>Intereses cobrados</option>
    <option value="abonos" {% if kind=='abonos' %}selected{% endif %}>Abonos a capital</option>
    <option value="riesgo" {% if kind=='riesgo' %}selected{% endif %}>Artículos en riesgo (vence ≤ 7 días)</option>
  </select>
  <button class="gold-gradient text-stone-900 font-semibold rounded-xl">Ver</button>
</form>
<div class="glass rounded-2xl p-4">
  {% if kind in ['intereses','abonos'] %}
    <div class="overflow-auto rounded-xl border border-yellow-200/30">
      <table class="min-w-full text-sm">
        <thead class="thead-gold"><tr><th class="py-2 pl-3">Fecha</th><th>Empeño</th><th>Tipo</th><th class="text-right pr-3">Monto</th></tr></thead>
        <tbody class="divide-y divide-stone-800/40 bg-black/40">
          {% for p in rows %}
            <tr><td class="py-2 pl-3">{{ p.paid_at }}</td><td>#{{ p.loan_id }}</td><td>{{ p.type }}</td><td class="text-right pr-3">${{ '%.2f'|format(p.amount) }}</td></tr>
          {% endfor %}
          {% if not rows %}<tr><td class="py-2 pl-3" colspan="4">Sin datos</td></tr>{% endif %}
        </tbody>
      </table>
    </div>
    <div class="text-right mt-2">Total: <b>${{ '%.2f'|format(total) }}</b></div>
  {% else %}
    <div class="overflow-auto rounded-xl border border-yellow-200/30">
      <table class="min-w-full text-sm">
        <thead class="thead-gold"><tr><th class="py-2 pl-3">ID</th><th>Cliente</th><th>Artículo</th><th>Monto</th><th>Vence</th></tr></thead>
        <tbody class="divide-y divide-stone-800/40 bg-black/40">
          {% for r in rows %}
            <tr><td class="py-2 pl-3">{{ r.id }}</td><td>{{ r.customer_name }}</td><td>{{ r.item_name }}</td><td>${{ '%.2f'|format(r.amount) }}</td><td>{{ r.due_date }}</td></tr>
          {% endfor %}
          {% if not rows %}<tr><td class="py-2 pl-3" colspan="5">Sin datos</td></tr>{% endif %}
        </tbody>
      </table>
    </div>
  {% endif %}
</div>
"""

@app.route("/reportes")
@login_required
def reports():
    kind = request.args.get("kind","intereses")
    dfrom = request.args.get("from", (date.today().replace(day=1)).isoformat())
    dto = request.args.get("to", date.today().isoformat())
    dfrom_ts = f"{dfrom} 00:00:00"; dto_ts = f"{dto} 23:59:59"
    rows=[]; total=0.0
    with closing(get_db()) as conn:
        if kind in ["intereses","abonos"]:
            ty = "INTERES" if kind=="intereses" else "ABONO"
            rows = conn.execute("SELECT * FROM payments WHERE type=? AND paid_at BETWEEN ? AND ? ORDER BY id DESC", (ty, dfrom_ts, dto_ts)).fetchall()
            total = sum(r["amount"] for r in rows)
        else:
            rows = conn.execute("""
                SELECT id, customer_name, item_name, amount, due_date
                FROM loans
                WHERE status!='RETIRADO' AND due_date BETWEEN ? AND date(?, '+7 day')
                ORDER BY due_date ASC
            """, (date.today().isoformat(), date.today().isoformat())).fetchall()
    body = render_template_string(REPORTS_TPL, rows=rows, total=total, kind=kind, dfrom=dfrom, dto=dto)
    return render_page(body, title="Reportes", active="reports")

# ====== Configuración / Usuarios / Email ======
SETTINGS_TPL = """
<h2 class="text-xl font-bold text-yellow-300 mb-3">Configuración</h2>
<form method="post" class="glass p-4 rounded-2xl grid md:grid-cols-4 gap-2">
  <div><label class="text-sm">Interés mensual % (default)</label>
    <input name="default_interest_rate" type="number" step="0.01" value="{{ ir }}" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/></div>
  <div><label class="text-sm">Plazo por defecto (días)</label>
    <input name="default_term_days" type="number" step="1" value="{{ td }}" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/></div>
  <div><label class="text-sm">Renovar vencimiento (+días)</label>
    <input name="renew_days" type="number" step="1" value="{{ renew_days }}" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/></div>
  <div class="flex items-end"><button class="gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl">Guardar</button></div>
</form>
<p class="text-xs text-yellow-200/70 mt-2">Los cambios aplican a nuevos empeños (y la renovación usa el valor actual).</p>

<h3 class="text-lg font-bold text-yellow-300 mt-6 mb-2">Email de recuperación</h3>
<form method="post" action="{{ url_for('email_settings') }}" class="glass p-4 rounded-2xl grid md:grid-cols-6 gap-2">
  <div class="md:col-span-2">
    <label class="text-xs">Correo de recuperación</label>
    <input name="recovery_email" value="{{ recovery_email }}" placeholder="correo@dominio.com" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
  </div>
  <div><label class="text-xs">SMTP host</label><input name="smtp_host" value="{{ smtp_host }}" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/></div>
  <div><label class="text-xs">SMTP puerto</label><input name="smtp_port" value="{{ smtp_port }}" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/></div>
  <div><label class="text-xs">SMTP usuario</label><input name="smtp_user" value="{{ smtp_user }}" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/></div>
  <div><label class="text-xs">SMTP contraseña</label><input name="smtp_pass" type="password" value="{{ smtp_pass }}" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/></div>
  <div class="md:col-span-6 flex gap-2">
    <button class="gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl">Guardar Email/SMTP</button>
    <a class="px-4 py-2 rounded-xl border border-yellow-200/30" href="{{ url_for('recover') }}">Probar recuperación</a>
  </div>
  <p class="md:col-span-6 text-xs text-yellow-200/70">Si no configuras SMTP, el correo se mostrará en la consola como respaldo.</p>
</form>

<h3 class="text-lg font-bold text-yellow-300 mt-6 mb-2">Usuarios</h3>
<form method="post" action="{{ url_for('users_add') }}" class="glass p-4 rounded-2xl grid md:grid-cols-4 gap-2">
  <input name="username" placeholder="Usuario nuevo" required class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
  <input name="password" type="password" placeholder="Contraseña" required class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
  <select name="role" class="rounded-xl border border-yellow-200/30 bg-black/40 p-2"><option>admin</option></select>
  <button class="gold-gradient text-stone-900 font-semibold rounded-xl">Crear usuario</button>
</form>
<div class="glass rounded-2xl p-4 mt-3">
  <div class="overflow-auto rounded-xl border border-yellow-200/30">
    <table class="min-w-full text-sm">
      <thead class="thead-gold"><tr><th class="py-2 pl-3">ID</th><th>Usuario</th><th>Rol</th><th>Creado</th></tr></thead>
      <tbody class="divide-y divide-stone-800/40 bg-black/40">
        {% for u in users %}
          <tr><td class="py-2 pl-3">{{ u.id }}</td><td>{{ u.username }}</td><td>{{ u.role }}</td><td>{{ u.created_at }}</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
"""

@app.route("/config", methods=["GET","POST"])
@login_required
def settings_page():
    if request.method=="POST":
        set_setting("default_interest_rate", request.form.get("default_interest_rate","20"))
        set_setting("default_term_days", request.form.get("default_term_days","90"))
        set_setting("renew_days", request.form.get("renew_days","30"))
        return redirect(url_for("settings_page"))
    ir = get_setting("default_interest_rate","20")
    td = get_setting("default_term_days","90")
    renew_days = get_setting("renew_days","30")
    recovery_email = get_setting("recovery_email","jdm299102@gmail.com")
    smtp_host = get_setting("smtp_host","")
    smtp_port = get_setting("smtp_port","587")
    smtp_user = get_setting("smtp_user","")
    smtp_pass = get_setting("smtp_pass","")
    with closing(get_db()) as conn:
        users = conn.execute("SELECT id,username,role,created_at FROM users ORDER BY id").fetchall()
    return render_page(render_template_string(SETTINGS_TPL, ir=ir, td=td, users=users,
                                              recovery_email=recovery_email, smtp_host=smtp_host, smtp_port=smtp_port,
                                              smtp_user=smtp_user, smtp_pass=smtp_pass, renew_days=renew_days),
                       title="Configuración", active="settings")

@app.route("/config/email", methods=["POST"])
@login_required
def email_settings():
    set_setting("recovery_email", request.form.get("recovery_email","").strip())
    set_setting("smtp_host", request.form.get("smtp_host","").strip())
    set_setting("smtp_port", request.form.get("smtp_port","587").strip())
    set_setting("smtp_user", request.form.get("smtp_user","").strip())
    set_setting("smtp_pass", request.form.get("smtp_pass","").strip())
    return redirect(url_for("settings_page"))

@app.route("/config/users/add", methods=["POST"])
@login_required
def users_add():
    username = request.form.get("username","").strip()
    password = request.form.get("password","")
    role = request.form.get("role","admin")
    if not username or not password:
        return redirect(url_for("settings_page"))
    with closing(get_db()) as conn:
        try:
            conn.execute("INSERT INTO users(username, pass_hash, role, created_at) VALUES (?,?,?,?)",
                         (username, generate_password_hash(password), role, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
        except sqlite3.IntegrityError:
            pass
    return redirect(url_for("settings_page"))

# ====== INVENTARIO ======
INVENTORY_TPL = """
<h2 class="text-xl font-bold text-yellow-300 mb-3">Inventario — Artículos perdidos</h2>
<section class="grid md:grid-cols-3 gap-6">
  <div class="glass rounded-2xl p-4">
    <h3 class="text-lg font-bold text-yellow-300 mb-2">Agregar artículo</h3>
    <form method="post" action="{{ url_for('inventory_add') }}" class="space-y-2">
      <input name="item_desc" placeholder="Descripción del artículo" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <select name="status" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2">
        <option value="PERDIDO">PERDIDO</option>
        <option value="ENCONTRADO">ENCONTRADO</option>
      </select>
      <button class="gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl">Guardar</button>
    </form>
  </div>
  <div class="md:col-span-2 glass rounded-2xl p-4">
    <div class="overflow-auto rounded-xl border border-yellow-200/30">
      <table class="min-w-full text-sm">
        <thead class="thead-gold"><tr><th class="py-2 pl-3">ID</th><th>Descripción</th><th>Estado</th><th>Creado</th><th class="text-right pr-3">Acciones</th></tr></thead>
        <tbody class="divide-y divide-stone-800/40 bg-black/40">
          {% for r in rows %}
            <tr>
              <td class="py-2 pl-3">{{ r.id }}</td>
              <td>{{ r.item_desc }}</td>
              <td>{{ r.status }}</td>
              <td>{{ r.created_at }}</td>
              <td class="text-right pr-3">
                <a href="{{ url_for('inventory_confirm_delete', item_id=r.id) }}" class="px-2 py-1 bg-red-700 rounded">Eliminar</a>
              </td>
            </tr>
          {% endfor %}
          {% if not rows %}<tr><td class="py-2 pl-3" colspan="5">Sin artículos</td></tr>{% endif %}
        </tbody>
      </table>
    </div>
  </div>
</section>
"""

CONFIRM_DELETE_INV_TPL = """
<div class="max-w-md mx-auto glass p-6 rounded-2xl">
  <h2 class="text-xl font-bold text-yellow-300 mb-2">Eliminar artículo de inventario</h2>
  <p class="text-sm mb-3">Vas a eliminar el artículo <b>#{{ item.id }}</b>: {{ item.item_desc }} — estado: {{ item.status }}.</p>
  {% if error %}<div class="mb-3 p-2 bg-red-900/40 border border-red-700 rounded">{{ error }}</div>{% endif %}
  <form method="post" action="{{ url_for('inventory_delete', item_id=item.id) }}" class="space-y-3">
    <label class="text-xs">Confirma tu contraseña</label>
    <input name="password" type="password" placeholder="Tu contraseña" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    <div class="flex gap-2">
      <button class="px-4 py-2 rounded-xl bg-red-700 hover:bg-red-800">Eliminar definitivamente</button>
      <a href="{{ url_for('inventory') }}" class="px-4 py-2 rounded-xl border border-yellow-200/30">Cancelar</a>
    </div>
  </form>
</div>
"""

@app.route("/inventario")
@login_required
def inventory():
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT * FROM inventory_items ORDER BY id DESC LIMIT 500").fetchall()
    return render_page(render_template_string(INVENTORY_TPL, rows=rows), title="Inventario", active="inventory")

@app.route("/inventario/nuevo", methods=["POST"])
@login_required
def inventory_add():
    desc = request.form.get("item_desc","").strip()
    status = request.form.get("status","PERDIDO").strip() or "PERDIDO"
    if not desc:
        return redirect(url_for("inventory"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_db()) as conn:
        conn.execute("INSERT INTO inventory_items(item_desc,status,created_at) VALUES (?,?,?)", (desc, status, now))
        conn.commit()
    return redirect(url_for("inventory"))

@app.route("/inventario/confirm/<int:item_id>")
@login_required
def inventory_confirm_delete(item_id:int):
    with closing(get_db()) as conn:
        item = conn.execute("SELECT * FROM inventory_items WHERE id=?", (item_id,)).fetchone()
    if not item: return "No encontrado", 404
    return render_page(render_template_string(CONFIRM_DELETE_INV_TPL, item=item, error=None), title="Eliminar inventario", active="inventory")

@app.route("/inventario/delete/<int:item_id>", methods=["POST"])
@login_required
def inventory_delete(item_id:int):
    password = request.form.get("password","")
    with closing(get_db()) as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (session.get("uid"),)).fetchone()
        item = conn.execute("SELECT * FROM inventory_items WHERE id=?", (item_id,)).fetchone()
        if not user or not check_password_hash(user["pass_hash"], password):
            return render_page(render_template_string(CONFIRM_DELETE_INV_TPL, item=item, error="Contraseña incorrecta"), title="Eliminar inventario", active="inventory")
        conn.execute("DELETE FROM inventory_items WHERE id=?", (item_id,))
        conn.commit()
    return redirect(url_for("inventory"))

# ====== VENTAS ======
SALES_TPL = """
<h2 class="text-xl font-bold text-yellow-300 mb-3">Ventas — Artículos en venta</h2>
<section class="grid md:grid-cols-3 gap-6">
  <div class="glass rounded-2xl p-4">
    <h3 class="text-lg font-bold text-yellow-300 mb-2">Agregar a venta</h3>
    <form method="post" action="{{ url_for('sales_add') }}" class="space-y-2">
      <input name="item_desc" placeholder="Descripción del artículo" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <input name="price" type="number" step="0.01" placeholder="Precio" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <button class="gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl">Guardar</button>
    </form>
  </div>
  <div class="md:col-span-2 glass rounded-2xl p-4">
    <div class="overflow-auto rounded-xl border border-yellow-200/30">
      <table class="min-w-full text-sm">
        <thead class="thead-gold"><tr><th class="py-2 pl-3">ID</th><th>Descripción</th><th>Precio</th><th>Status</th><th>Vendido</th><th class="text-right pr-3">Acciones</th></tr></thead>
        <tbody class="divide-y divide-stone-800/40 bg-black/40">
          {% for s in rows %}
            <tr>
              <td class="py-2 pl-3">{{ s.id }}</td>
              <td>{{ s.item_desc }}</td>
              <td>${{ '%.2f'|format(s.price) }}</td>
              <td>{{ s.status }}</td>
              <td>{{ s.sold_at or '' }}</td>
              <td class="text-right pr-3">
                {% if s.status != 'VENDIDO' %}
                  <form method="post" action="{{ url_for('sales_mark_sold', sale_id=s.id) }}" class="inline">
                    <button class="px-2 py-1 bg-emerald-700 rounded">Marcar vendido</button>
                  </form>
                {% endif %}
                <a href="{{ url_for('sales_confirm_delete', sale_id=s.id) }}" class="px-2 py-1 bg-red-700 rounded">Eliminar</a>
              </td>
            </tr>
          {% endfor %}
          {% if not rows %}<tr><td class="py-2 pl-3" colspan="6">Sin artículos</td></tr>{% endif %}
        </tbody>
      </table>
    </div>
  </div>
</section>
"""

CONFIRM_DELETE_SALE_TPL = """
<div class="max-w-md mx-auto glass p-6 rounded-2xl">
  <h2 class="text-xl font-bold text-yellow-300 mb-2">Eliminar artículo de ventas</h2>
  <p class="text-sm mb-3">Vas a eliminar el artículo <b>#{{ item.id }}</b>: {{ item.item_desc }} — estado: {{ item.status }}.</p>
  {% if error %}<div class="mb-3 p-2 bg-red-900/40 border border-red-700 rounded">{{ error }}</div>{% endif %}
  <form method="post" action="{{ url_for('sales_delete', sale_id=item.id) }}" class="space-y-3">
    <label class="text-xs">Confirma tu contraseña</label>
    <input name="password" type="password" placeholder="Tu contraseña" required class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    <div class="flex gap-2">
      <button class="px-4 py-2 rounded-xl bg-red-700 hover:bg-red-800">Eliminar definitivamente</button>
      <a href="{{ url_for('sales_page') }}" class="px-4 py-2 rounded-xl border border-yellow-200/30">Cancelar</a>
    </div>
  </form>
</div>
"""

@app.route("/ventas")
@login_required
def sales_page():
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT * FROM sales ORDER BY id DESC LIMIT 500").fetchall()
    return render_page(render_template_string(SALES_TPL, rows=rows), title="Ventas", active="sales")

@app.route("/ventas/nuevo", methods=["POST"])
@login_required
def sales_add():
    desc = request.form.get("item_desc","").strip()
    price = float(request.form.get("price",0) or 0)
    if not desc or price <= 0:
        return redirect(url_for("sales_page"))
    with closing(get_db()) as conn:
        conn.execute("INSERT INTO sales(item_desc,price,status) VALUES (?,?,?)", (desc, price, "EN_VENTA"))
        conn.commit()
    return redirect(url_for("sales_page"))

@app.route("/ventas/vender/<int:sale_id>", methods=["POST"])
@login_required
def sales_mark_sold(sale_id:int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_db()) as conn:
        conn.execute("UPDATE sales SET status='VENDIDO', sold_at=? WHERE id=?", (now, sale_id))
        conn.commit()
    return redirect(url_for("sales_page"))

@app.route("/ventas/confirm/<int:sale_id>")
@login_required
def sales_confirm_delete(sale_id:int):
    with closing(get_db()) as conn:
        item = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
    if not item: return "No encontrado", 404
    return render_page(render_template_string(CONFIRM_DELETE_SALE_TPL, item=item, error=None), title="Eliminar venta", active="sales")

@app.route("/ventas/delete/<int:sale_id>", methods=["POST"])
@login_required
def sales_delete(sale_id:int):
    password = request.form.get("password","")
    with closing(get_db()) as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (session.get("uid"),)).fetchone()
        item = conn.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        if not user or not check_password_hash(user["pass_hash"], password):
            return render_page(render_template_string(CONFIRM_DELETE_SALE_TPL, item=item, error="Contraseña incorrecta"), title="Eliminar venta", active="sales")
        conn.execute("DELETE FROM sales WHERE id=?", (sale_id,))
        conn.commit()
    return redirect(url_for("sales_page"))

# ====== Uploads fuera del exe ======
@app.route("/uploads/<path:filename>")
def uploads(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# ====== Export CSV ======
@app.route("/export.csv")
@login_required
def export_csv():
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT * FROM loans ORDER BY id DESC").fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id","created_at","item_name","weight_grams","customer_name","customer_id","phone","amount","interest_rate","due_date","photo_path","status","redeemed_at"])
    for r in rows:
        writer.writerow([r["id"], r["created_at"], r["item_name"], r["weight_grams"], r["customer_name"], r["customer_id"], r["phone"],
                        r["amount"], r["interest_rate"], r["due_date"], r["photo_path"], r["status"], r["redeemed_at"]])
    csv_data = output.getvalue().encode("utf-8")
    return Response(csv_data, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=empenos.csv"})

# ====== Empeño retirado (marcar) ======
@app.route("/redeem/<int:loan_id>", methods=["POST"])
@login_required
def mark_redeemed(loan_id: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_db()) as conn:
        conn.execute("UPDATE loans SET status='RETIRADO', redeemed_at=? WHERE id=?", (now, loan_id))
        conn.commit()
    return redirect(url_for("index"))

# ====== Usuarios (placeholder visual) ======
@app.route("/usuarios")
@login_required
def users_page():
    body = """
    <h2 class="text-xl font-bold text-yellow-300 mb-3">Usuarios y permisos</h2>
    <p class="glass p-4 rounded-2xl">La gestión completa de usuarios está en Configuración ➜ Usuarios.</p>
    """
    return render_page(body, title="Usuarios", active="users")

# ====== Facturación (simple): abrir ticket por # de empeño ======
FACT_TPL = """
<div class="max-w-xl mx-auto glass p-6 rounded-2xl">
  <h2 class="text-xl font-bold text-yellow-300 mb-3">Facturación</h2>
  <form method="get" class="grid md:grid-cols-3 gap-2">
    <div class="md:col-span-2">
      <label class="text-xs"># de Empeño</label>
      <input name="loan_id" type="number" min="1" placeholder="Ej. 101" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    </div>
    <div class="flex items-end">
      <button class="gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl">Abrir Ticket/Recibo</button>
    </div>
  </form>

  <div class="mt-4 text-sm text-yellow-200/80">Tip: también puedes entrar desde Empeños ➜ “Recibo”.</div>

  <h3 class="text-lg font-semibold mt-5 mb-2">Empeños recientes</h3>
  <div class="overflow-auto rounded-xl border border-yellow-200/30">
    <table class="min-w-full text-sm">
      <thead class="thead-gold"><tr><th class="py-2 pl-3">#</th><th>Cliente</th><th>Artículo</th><th>Monto</th><th>Acción</th></tr></thead>
      <tbody class="divide-y divide-stone-800/40 bg-black/40">
        {% for r in rows %}
          <tr>
            <td class="py-2 pl-3">{{ r.id }}</td>
            <td>{{ r.customer_name }}</td>
            <td>{{ r.item_name }}</td>
            <td>${{ '%.2f'|format(r.amount or 0) }}</td>
            <td><a href="{{ url_for('ticket', loan_id=r.id) }}" class="px-2 py-1 border rounded">Abrir Ticket</a></td>
          </tr>
        {% endfor %}
        {% if not rows %}<tr><td class="py-2 pl-3" colspan="5">Sin datos</td></tr>{% endif %}
      </tbody>
    </table>
  </div>
</div>
"""

@app.route("/facturacion")
@login_required
def facturacion():
    loan_id = request.args.get("loan_id")
    if loan_id and str(loan_id).isdigit():
        return redirect(url_for("ticket", loan_id=int(loan_id)))
    with closing(get_db()) as conn:
        rows = conn.execute("SELECT id,customer_name,item_name,amount FROM loans ORDER BY id DESC LIMIT 20").fetchall()
    return render_page(render_template_string(FACT_TPL, rows=rows), title="Facturación", active="dashboard")

# ====== Main ======
@app.route("/inicio")
def root_redirect():
    if not session.get("uid"):
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))

if __name__ == "__main__":
    # abre el navegador automáticamente
    def _open(): time.sleep(1.0); webbrowser.open("http://127.0.0.1:5010")
    threading.Thread(target=_open, daemon=True).start()
    print(f"=== Iniciando {APP_BRAND} en http://127.0.0.1:5010 ===")
    app.run(debug=False, host="127.0.0.1", port=5010)

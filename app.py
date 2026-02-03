# app.py
# World Jewerly — Sistema modular
# -*- coding: utf-8 -*-
from __future__ import annotations

# =========================
# IMPORTS BASE
# =========================
from flask import (
    Flask, request, redirect, url_for,
    Response, render_template_string,
    send_from_directory, session
)

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, date
from pathlib import Path
from functools import wraps
import os, sys, uuid, csv, io, secrets, threading, time, webbrowser
import smtplib, ssl
from email.mime.text import MIMEText
from urllib.parse import quote_plus
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

# =========================
# PATHS + DB (PRIMERO)
# =========================
BASE_DIR = Path(__file__).resolve().parent

# 👉 En Render usamos el mismo directorio del proyecto
DB_PATH = BASE_DIR / "empenos.db"

UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_ITEMS = UPLOAD_DIR / "items"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_ITEMS.mkdir(parents=True, exist_ok=True)

# =========================
# FLASK APP (UNA SOLA VEZ)
# =========================
app = Flask(__name__, static_folder="static", static_url_path="/static")
app.secret_key = "world-jewelry"
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB

APP_BRAND = "World Jewerly"

# =========================
# DATABASE
# =========================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# =========================
# SCHEMA
# =========================
SCHEMA = """
CREATE TABLE IF NOT EXISTS loans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,

    item_name TEXT NOT NULL,
    weight_grams REAL NOT NULL,

    customer_name TEXT DEFAULT '',
    customer_id TEXT DEFAULT '',

    phone TEXT NOT NULL,

    amount REAL NOT NULL,
    interest_rate REAL NOT NULL,
    due_date TEXT NOT NULL,

    photo_path TEXT DEFAULT '',
    id_front_path TEXT DEFAULT '',
    id_back_path TEXT DEFAULT '',
    signature_path TEXT DEFAULT '',

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
    type TEXT NOT NULL,
    notes TEXT,
    FOREIGN KEY(loan_id) REFERENCES loans(id)
);

CREATE TABLE IF NOT EXISTS cash_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    when_at TEXT NOT NULL,
    concept TEXT NOT NULL,
    amount REAL NOT NULL,
    ref TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_desc TEXT NOT NULL,
    price REAL NOT NULL,
    sold_at TEXT,
    status TEXT NOT NULL DEFAULT 'EN_VENTA'
);

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


# =========================
# INIT DB
# =========================
def init_db():
    print("🟢 Inicializando base de datos...")
    with closing(get_db()) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


# =========================
# FIX USERS TABLE (MIGRACIÓN SEGURA)
# =========================
def ensure_users_columns():
    with closing(get_db()) as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]

        if "name" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN name TEXT")

        if "role" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'staff'")

        if "created_at" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN created_at TEXT")

        conn.commit()


# =========================
# AUTO INIT DB (RENDER SAFE)
# =========================
try:
    if not DB_PATH.exists():
        print("🟡 DB no existe, creando...")
        init_db()
    else:
        with closing(get_db()) as conn:
            conn.execute("SELECT 1 FROM loans LIMIT 1")
        print("🟢 DB OK, tabla loans existe")
except Exception as e:
    print("🔴 Error DB, recreando:", e)
    init_db()


# =========================
# APPLY USERS MIGRATION
# =========================
try:
    ensure_users_columns()
    print("🟢 Tabla users verificada")
except Exception as e:
    print("🔴 Error ajustando users:", e)


# =========================
# SERVIR FOTOS DE ARTÍCULOS
# =========================
@app.route("/uploads/items/<filename>")
def item_photo(filename):
    return send_from_directory(UPLOAD_ITEMS, filename)

# =========================
# INTERÉS AUTOMÁTICO
# =========================
def calcular_interes_por_fechas(
    capital: float,
    tasa_mensual: float,
    fecha_desde: date,
    fecha_hasta: date
) -> float:

    if not fecha_desde or not fecha_hasta:
        return 0.0

    dias = (fecha_hasta - fecha_desde).days
    if dias <= 0:
        dias = 1

    interes = capital * (tasa_mensual / 100) * (dias / 30)
    return round(interes, 2)

# =========================
# SETTINGS HELPERS
# =========================
def set_setting(key, value):
    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value))
        )
        conn.commit()


def get_setting(key, default=None):
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ).fetchone()
    return row["value"] if row else default


def ensure_users_columns():
    with closing(get_db()) as conn:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
        if "name" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN name TEXT")
        if "role" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'staff'")
        conn.commit()

# =========================
# EMAIL HELPER
# =========================
def send_email(to_email: str, subject: str, html_body: str) -> bool:
    host = get_setting("smtp_host", "")
    port = int(get_setting("smtp_port", "587") or 587)
    user = get_setting("smtp_user", "")
    pwd = get_setting("smtp_pass", "")

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
            return False
    else:
        print("== SMTP NO CONFIGURADO ==")
        print("Para:", to_email)
        print("Asunto:", subject)
        print(html_body)
        return False

# =========================
# AUTH DECORATOR (TEMP)
# =========================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        return f(*args, **kwargs)  # 🔓 acceso libre temporal
    return decorated


# ======= Páginas de autenticación =======
LOGIN_TPL = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>

<!-- ===== PWA ===== -->
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/icons/icon-192.png">
<meta name="theme-color" content="#facc15">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="World Jewelry">
<!-- ===== /PWA ===== -->

<title>{{ brand }} - Iniciar sesión</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
.glass{
  background:rgba(255,255,255,.08);
  backdrop-filter:blur(10px);
  border:1px solid rgba(255,255,255,.12);
}
</style>
</head>

<body class="min-h-screen flex items-center justify-center bg-stone-900 text-stone-100">

  <div class="w-full max-w-sm glass p-6 rounded-2xl">
    <div class="text-center mb-4">
      <div class="text-4xl">💎</div>
      <h1 class="text-2xl font-extrabold mt-1">{{ brand }}</h1>
      <p class="text-sm text-yellow-200/70 mt-1">Inicia sesión para continuar</p>
    </div>

    {% if msg %}
      <div class="mb-3 p-2 bg-emerald-900/40 border border-emerald-700 rounded">{{ msg }}</div>
    {% endif %}

    {% if error %}
      <div class="mb-3 p-2 bg-red-900/40 border border-red-700 rounded">{{ error }}</div>
    {% endif %}

    <form method="post" class="space-y-3">
      <input name="username" placeholder="Usuario" required
        class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <input name="password" type="password" placeholder="Contraseña" required
        class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
      <button class="w-full bg-yellow-400 text-stone-900 font-semibold px-4 py-2 rounded-xl">
        Entrar
      </button>
    </form>

    <div class="text-center mt-3">
      <a class="text-yellow-300 underline" href="{{ url_for('recover') }}">
        ¿Olvidaste usuario o contraseña?
      </a>
    </div>
  </div>

  <!-- Service Worker -->
  <script>
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/static/sw.js")
        .then(() => console.log("✅ PWA activa"))
        .catch(err => console.error("❌ SW error", err));
    }
  </script>

</body>
</html>
"""

@app.route("/uploads/legal/<path:filename>")
def legal_uploads(filename):
    return send_from_directory("uploads/legal", filename)


@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")

        with closing(get_db()) as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username=?",
                (u,)
            ).fetchone()

        if row and check_password_hash(row["pass_hash"], p):
            session["uid"] = row["id"]
            session["username"] = row["username"]
            session["role"] = row["role"]
            return redirect(url_for("dashboard"))
        else:
            return render_template_string(
                LOGIN_TPL,
                brand=APP_BRAND,
                error="Usuario o contraseña inválidos",
                msg=None
            )

    return render_template_string(
        LOGIN_TPL,
        brand=APP_BRAND,
        error=None,
        msg=request.args.get("msg")
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ===== Recuperación de acceso =====
RECOVER_TPL = """
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>

<title>{{ brand }} — Recuperar acceso</title>
<script src="https://cdn.tailwindcss.com"></script>

<!-- ===== PWA ===== -->
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/icons/icon-192.png">
<meta name="theme-color" content="#facc15">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="World Jewelry">
<!-- ===== /PWA ===== -->

<style>
.glass{
  background:rgba(255,255,255,.08);
  backdrop-filter:blur(10px);
  border:1px solid rgba(255,255,255,.12);
}
</style>
</head>

<body class="min-h-screen flex items-center justify-center bg-stone-900 text-stone-100">

  <div class="w-full max-w-lg glass p-6 rounded-2xl">
    <h1 class="text-xl font-bold text-yellow-300 mb-3">Recuperar acceso</h1>

    <p class="text-sm text-yellow-200/80 mb-3">
      Se enviará un correo con tus usuarios y enlaces para restablecer la contraseña
      a <b>{{ email }}</b>.
    </p>

    {% if msg %}
      <div class="mb-3 p-2 bg-emerald-900/40 border border-emerald-700 rounded">
        {{ msg }}
      </div>
    {% endif %}

    {% if error %}
      <div class="mb-3 p-2 bg-red-900/40 border border-red-700 rounded">
        {{ error }}
      </div>
    {% endif %}

    <form method="post" class="space-y-3">
      <button class="bg-yellow-400 text-stone-900 font-semibold px-4 py-2 rounded-xl">
        Enviar correo de recuperación
      </button>

      <a href="{{ url_for('login') }}"
         class="px-4 py-2 rounded-xl border border-yellow-200/30 text-center block">
        Volver
      </a>
    </form>
  </div>

  <!-- Service Worker -->
  <script>
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/static/sw.js")
        .then(() => console.log("✅ PWA activa (recover)"))
        .catch(err => console.error("❌ SW error", err));
    }
  </script>

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
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>

<title>{{ brand }} — Restablecer contraseña</title>
<script src="https://cdn.tailwindcss.com"></script>

<!-- ===== PWA ===== -->
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/icons/icon-192.png">
<meta name="theme-color" content="#facc15">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="World Jewelry">
<!-- ===== /PWA ===== -->

<style>
.glass{
  background:rgba(255,255,255,.08);
  backdrop-filter:blur(10px);
  border:1px solid rgba(255,255,255,.12);
}
</style>
</head>

<body class="min-h-screen flex items-center justify-center bg-stone-900 text-stone-100">
  <div class="w-full max-w-sm glass p-6 rounded-2xl">
    <h1 class="text-xl font-bold text-yellow-300 mb-3">Restablecer contraseña</h1>
    <p class="text-sm text-yellow-200/80 mb-3">
      Usuario: <b>{{ username }}</b>
    </p>

    {% if error %}
      <div class="mb-3 p-2 bg-red-900/40 border border-red-700 rounded">
        {{ error }}
      </div>
    {% endif %}

    <form method="post" class="space-y-3">
      <input type="hidden" name="token" value="{{ token }}"/>
      <input type="hidden" name="u" value="{{ username }}"/>

      <input name="password" type="password" placeholder="Nueva contraseña" required
        class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>

      <input name="password2" type="password" placeholder="Repetir contraseña" required
        class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>

      <button class="w-full bg-yellow-400 text-stone-900 font-semibold px-4 py-2 rounded-xl">
        Guardar
      </button>
    </form>
  </div>

  <!-- Service Worker -->
  <script>
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/static/sw.js")
        .then(() => console.log("✅ PWA activa (reset)"))
        .catch(err => console.error("❌ SW error", err));
    }
  </script>

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
<html lang="es">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1"/>
<title>{{ brand }} - {{ title or '' }}</title>

<!-- ===== PWA ===== -->
<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/icons/icon-192.png">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="{{ brand }}">
<!-- ===== /PWA ===== -->

<script src="https://cdn.tailwindcss.com"></script>

<style>
/* ===== SAFE ===== */
html,body{width:100%;max-width:100%;overflow-x:hidden}
*{-webkit-tap-highlight-color:transparent}

:root{
  --gold:#facc15;
  --danger:#ff3b30;
  --ok:#34c759;
}

/* ===== BACKGROUND iOS / APPLE MUSIC STYLE ===== */
html,body{
  background:
    radial-gradient(1200px 600px at 10% -10%, #1e293b 0%, transparent 60%),
    radial-gradient(900px 500px at 90% 10%, #0f172a 0%, transparent 60%),
    linear-gradient(180deg,#020617,#000);
  color:#e5e7eb;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto;
}

/* ===== HEADER ===== */
header{
  background:linear-gradient(135deg,#facc15,#f59e0b);
  box-shadow:0 20px 40px rgba(0,0,0,.45);
}
.app-logo{
  height:52px;width:52px;
  border-radius:16px;
  background:#020617;
  display:flex;
  align-items:center;
  justify-content:center;
  font-size:26px;
}

/* ===== GLASS MENU ===== */
.menu-backdrop{
  position:fixed;
  inset:0;
  background:rgba(0,0,0,.55);
  backdrop-filter:blur(14px);
  display:none;
  z-index:999;
}
.menu-backdrop.show{display:block}

.menu-panel{
  position:absolute;
  top:20px;
  left:16px;
  width:min(90%,340px);
  background:linear-gradient(180deg,rgba(30,41,59,.95),rgba(2,6,23,.95));
  border-radius:28px;
  padding:16px;
  box-shadow:0 30px 80px rgba(0,0,0,.7);
}

.menu-item{
  display:flex;
  align-items:center;
  gap:14px;
  padding:14px 16px;
  border-radius:18px;
  font-weight:900;
  background:rgba(255,255,255,.08);
  border:1px solid rgba(255,255,255,.12);
  margin-bottom:8px;
  transition:.18s;
}
.menu-item:active{transform:scale(.96)}
.menu-danger{color:#ffb4b0}
</style>
</head>

<body>

<header class="px-4 py-5">
  <div class="flex items-center justify-between">

    <!-- ☰ MENU IZQUIERDA -->
    <button id="menuBtn"
      class="w-12 h-12 rounded-2xl bg-black/80 text-white text-2xl font-black flex items-center justify-center">
      ☰
    </button>

    <h1 class="text-2xl font-extrabold text-black text-center flex-1">
      {{ brand }}
    </h1>

    <!-- LOGO -->
    <div class="app-logo">💎</div>
  </div>
</header>

<!-- ===== MENU iOS GLASS ===== -->
<div id="menuBackdrop" class="menu-backdrop">
  <div class="menu-panel">
    <a class="menu-item" href="{{ url_for('dashboard') }}">🏠 Inicio</a>
    <a class="menu-item" href="{{ url_for('empenos_index') }}">💍 Empeños</a>
    <a class="menu-item" href="{{ url_for('cash') }}">💵 Caja</a>
    <a class="menu-item" href="{{ url_for('reports') }}">📊 Reportes</a>
    <a class="menu-item" href="{{ url_for('inventory') }}">📦 Inventario</a>
    <a class="menu-item" href="{{ url_for('sales_page') }}">🧾 Ventas</a>
    <a class="menu-item" href="{{ url_for('users_page') }}">👤 Usuarios</a>
    <a class="menu-item" href="{{ url_for('settings_page') }}">⚙️ Config</a>

    <div class="h-px bg-white/10 my-2"></div>

    <a class="menu-item menu-danger" href="{{ url_for('logout') }}">🚪 Salir</a>
  </div>
</div>

<main class="px-4 py-6">
  {{ body|safe }}
</main>

<script>
/* ===== HAPTIC ENGINE iOS ===== */
function haptic(type="tap"){
  if(!navigator.vibrate) return;
  const patterns={
    tap:[15],
    open:[25],
    nav:[10],
    success:[20,40,20],
    danger:[60,20,60],
    close:[8]
  };
  navigator.vibrate(patterns[type]||[10]);
}

/* ===== MENU LOGIC ===== */
const btn=document.getElementById("menuBtn");
const back=document.getElementById("menuBackdrop");

btn.onclick=()=>{
  haptic("open");
  back.classList.add("show");
};

back.onclick=(e)=>{
  if(e.target===back){
    haptic("close");
    back.classList.remove("show");
  }
};

document.querySelectorAll(".menu-item").forEach(el=>{
  el.addEventListener("click",()=>{
    haptic(el.classList.contains("menu-danger")?"danger":"nav");
  });
});
</script>

</body>
</html>
"""

# =========================
# EJEMPLO: EMPENOS INDEX (USA EL WALLET TPL)
# =========================

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

# ======= DASHBOARD PREMIUM (Jewelry / Pawn Shop) =======
@app.route("/dashboard")
@login_required
def dashboard():

    conn = get_db()
    cur = conn.cursor()

    # ================== MÉTRICAS ==================
    cur.execute("SELECT COUNT(*) FROM loans WHERE status='ACTIVO'")
    activos = cur.fetchone()[0] or 0

    cur.execute("SELECT SUM(amount) FROM loans WHERE status='ACTIVO'")
    capital_prestado = cur.fetchone()[0] or 0

    cur.execute("""
        SELECT COALESCE(SUM(amount),0) AS neto
        FROM cash_movements
        WHERE DATE(when_at) = DATE('now')
    """)
    caja = cur.fetchone()["neto"] or 0

    # ================== PRÓXIMOS VENCIMIENTOS ==================
    cur.execute("""
        SELECT id, customer_name, item_name, amount, due_date
        FROM loans
        WHERE status='ACTIVO'
          AND due_date BETWEEN DATE('now') AND DATE('now','+7 day')
        ORDER BY due_date
    """)
    upcoming = cur.fetchall()

    cur.close()
    conn.close()

    # ================== TARJETAS DE VENCIMIENTO ==================
    fichas = ""
    for r in upcoming:
        fichas += f"""
        <div class="glass-card hover-lift" onclick="haptic('tap')">
          <div class="flex justify-between items-center mb-2">
            <div class="font-extrabold text-yellow-300">
              💎 #{r['id']} • {r['customer_name']}
            </div>
            <div class="text-xs opacity-70">
              📅 {r['due_date']}
            </div>
          </div>
          <div class="text-sm opacity-90">
            <b>Artículo:</b> {r['item_name']}
          </div>
          <div class="text-xl font-extrabold mt-2 text-yellow-200">
            ${r['amount']:,.2f}
          </div>
        </div>
        """

    if not fichas:
        fichas = """
        <div class="text-center text-yellow-200/70 py-12">
          ✨ No hay vencimientos próximos
        </div>
        """

    caja_color = "emerald" if caja >= 0 else "rose"

    # ================== BODY ==================
    body = f"""
<style>
.gold-gradient {{
  background:linear-gradient(135deg,#facc15,#f59e0b);
  color:#020617;
}}
.glass-card {{
  background:linear-gradient(180deg,rgba(255,255,255,.12),rgba(255,255,255,.04));
  backdrop-filter:blur(18px);
  border:1px solid rgba(255,255,255,.18);
  border-radius:22px;
  padding:18px;
  box-shadow:0 20px 50px rgba(0,0,0,.45);
}}
.hover-lift {{
  transition:.25s;
}}
.hover-lift:hover {{
  transform:translateY(-4px) scale(1.01);
}}
.metric {{
  position:relative;
  overflow:hidden;
}}
.metric::after {{
  content:"";
  position:absolute;
  inset:-40%;
  background:radial-gradient(circle at top left,rgba(255,255,255,.25),transparent 60%);
}}
.metric-value {{
  font-size:2.2rem;
  font-weight:900;
}}
.badge-green{{ color:#34d399 }}
.badge-emerald{{ color:#10b981 }}
.badge-rose{{ color:#fb7185 }}
</style>

<div class="space-y-10">

  <section class="grid grid-cols-1 md:grid-cols-3 gap-6">

    <div class="glass-card metric">
      <div class="text-sm opacity-80">Empeños activos</div>
      <div class="metric-value badge-green" data-count="{activos}">0</div>
    </div>

    <div class="glass-card metric">
      <div class="text-sm opacity-80">Capital en custodia</div>
      <div class="metric-value badge-emerald" data-count="{capital_prestado}">0</div>
    </div>

    <div class="glass-card metric">
      <div class="text-sm opacity-80">Caja del día</div>
      <div class="metric-value badge-{caja_color}" data-count="{caja}">0</div>
    </div>

  </section>

  <section class="glass-card">
    <h2 class="text-xl font-extrabold text-yellow-300 mb-5">
      ⏰ Vencimientos próximos (7 días)
    </h2>
    <div class="grid gap-4">
      {fichas}
    </div>
  </section>

</div>

<script>
document.querySelectorAll('[data-count]').forEach(el => {{
  const target = parseFloat(el.dataset.count);
  let val = 0;
  const step = target / 35;

  function animate() {{
    val += step;
    if (val >= target) val = target;
    el.textContent = Number.isInteger(target)
      ? Math.round(val)
      : '$' + val.toLocaleString(undefined, {{
          minimumFractionDigits:2,
          maximumFractionDigits:2
        }});
    if (val < target) requestAnimationFrame(animate);
  }}
  animate();
}});
</script>
"""

    return render_page(body, title="Dashboard", active="dashboard")


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




LOANS_LIST_TPL = """
<section class="space-y-4">

  <!-- HEADER -->
  <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-3 mb-4">
    <h2 class="text-lg font-bold text-yellow-300">Empeños</h2>

    <a href="/empenos/nuevo"
       class="gold-gradient px-4 py-2 rounded-xl font-bold text-center">
       ➕ Nuevo empeño
    </a>

    <form method="get" class="flex gap-2">
      <input
        name="q"
        value="{{ q or '' }}"
        placeholder="Buscar cliente, ID, tel o artículo"
        class="w-full md:w-64 rounded-xl border border-yellow-200/30 bg-black/40 p-2 text-sm"
      />
      <button
        class="px-4 py-2 rounded-xl bg-yellow-400 text-stone-900 font-semibold text-sm">
        Buscar
      </button>
    </form>
  </div>

  {% for r in rows %}
  {% set monthly_amt = (r.amount or 0) * ((r.interest_rate or 20) / 100) %}

  <!-- CARD -->
  <div class="glass rounded-2xl p-4">

    <div class="flex justify-between items-start gap-3">
      <div>
        <div class="text-lg font-extrabold">
          {{ r.customer_name }}
        </div>

        <div class="text-sm opacity-80">
          Empeño #{{ r.id }} · {{ r.item_name }}
          {% if r.weight_grams %}
            ({{ "%.2f"|format(r.weight_grams) }} g)
          {% endif %}
        </div>

        <span class="inline-block mt-2 px-3 py-1 rounded-full text-xs font-bold
          {% if r.status == 'ACTIVO' %}
            bg-green-500/20 text-green-300
          {% else %}
            bg-red-500/20 text-red-300
          {% endif %}
        ">
          {{ r.status }}
        </span>
      </div>

      <div class="text-right">
        <div class="text-xl font-extrabold text-yellow-300">
          ${{ "%.2f"|format(r.amount or 0) }}
        </div>
        <div class="text-xs opacity-70">
          ${{ "%.2f"|format(monthly_amt) }} / mes
        </div>
      </div>
    </div>

    <!-- SINGLE ACTION -->
    <div class="mt-4">
      <details class="group">
        <summary class="btn btn-primary w-full text-center cursor-pointer">
          ⚙️ Ver opciones
        </summary>

        <div class="mt-3 grid grid-cols-2 gap-2 text-sm">

          <a href="{{ url_for('loan_ticket', loan_id=r.id) }}"
             class="btn btn-ghost">🧾 Recibo</a>

          <a href="{{ url_for('payment_page', loan_id=r.id) }}"
             class="btn btn-ok">💵 Pago</a>

          <a href="{{ url_for('edit_loan_page', loan_id=r.id) }}"
             class="btn btn-ghost">✏️ Editar</a>

          <a href="{{ url_for('empeno_legal_view', loan_id=r.id) }}"
             class="btn btn-ghost">📜 Documento</a>

          <a href="{{ url_for('loan_confirm_delete', loan_id=r.id) }}"
             class="btn btn-danger col-span-2">
             🗑 Eliminar
          </a>

        </div>
      </details>
    </div>

  </div>
  {% endfor %}

  {% if not rows %}
  <div class="text-center opacity-70 py-12">
    No hay empeños registrados
  </div>
  {% endif %}

</section>
"""

IOS_PWA_STYLE = """
<!-- ==============================
     iPHONE + GLASS + PWA
================================ -->

<link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/icons/icon-192.png">
<meta name="theme-color" content="#facc15">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="World Jewelry">

<style>
body {
  background: linear-gradient(180deg, #0b0b0b, #111);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto;
  font-size: 16px; /* 🔥 clave para móvil */
  -webkit-font-smoothing: antialiased;
}

/* ===== GLASS BASE ===== */
.glass {
  background: rgba(20,20,20,0.55);
  backdrop-filter: blur(14px);
  -webkit-backdrop-filter: blur(14px);
  border: 1px solid rgba(255,255,255,0.08);
  box-shadow: 0 10px 30px rgba(0,0,0,0.45);
}

/* ===== INPUTS MÓVIL (CLAVE) ===== */
input, select, textarea {
  width: 100%;
  padding: 14px 16px;
  font-size: 16px;
  color: #000;
  background: #ffffff;
  border-radius: 12px;
  border: 1px solid #d1d5db;
  -webkit-appearance: none;
  appearance: none;
}

input::placeholder,
textarea::placeholder {
  color: #6b7280;
}

/* ===== BOTONES ===== */
button, a.btn, .btn {
  min-height: 48px;
  padding: 12px 18px;
  border-radius: 14px;
  font-size: 16px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.gold-gradient {
  background: linear-gradient(135deg, #facc15, #f59e0b);
  color: #000;
  border: none;
}

/* ===== FICHAS ===== */
.empeno-ficha {
  background: rgba(0,0,0,0.35);
  border-radius: 18px;
  padding: 14px;
  margin-bottom: 12px;
}

/* ===== DASHBOARD iOS CARDS ===== */
.ios-card{
  position:relative;
  padding:26px;
  border-radius:28px;
  background:linear-gradient(135deg,#020617,#020617);
  overflow:hidden;
  border:1px solid rgba(250,204,21,.28);
  box-shadow:0 30px 80px rgba(0,0,0,.65);
}

.ios-card.green{border-color:rgba(34,197,94,.45)}
.ios-card.blue{border-color:rgba(56,189,248,.45)}

.ios-label{
  font-size:14px;
  opacity:.7;
  margin-bottom:6px;
  font-weight:600;
}

.ios-value{
  font-size:44px;
  font-weight:900;
  letter-spacing:-1px;
}
/* ===== FIX TEXTO BORROSO iOS (FORZAR NEGRO REAL) ===== */
input,
select,
textarea {
  color: #000000 !important;
  -webkit-text-fill-color: #000000 !important; /* CLAVE */
  opacity: 1 !important;
  text-shadow: none !important;
  filter: none !important;
}

/* Cuando el input tiene valor */
input:not(:placeholder-shown),
textarea:not(:placeholder-shown) {
  color: #000000 !important;
  -webkit-text-fill-color: #000000 !important;
}

/* Placeholder (gris claro, NO borroso) */
input::placeholder,
textarea::placeholder {
  color: #9ca3af !important;
  opacity: 1 !important;
}

/* Evita efecto "disabled" fantasma en iOS */
input:disabled,
select:disabled,
textarea:disabled {
  opacity: 1 !important;
  -webkit-text-fill-color: #000000 !important;
}

/* Anti-blur extra en iOS */
input,
textarea {
  -webkit-font-smoothing: auto !important;
}


/* ===== GLOW ===== */
.ios-glow{
  position:absolute;
  inset:-40%;
  background:
    radial-gradient(circle at 30% 30%, rgba(250,204,21,.28), transparent 40%),
    radial-gradient(circle at 70% 70%, rgba(34,197,94,.18), transparent 45%);
  animation:glowMove 9s linear infinite;
}

.ios-card.green .ios-glow{
  background:radial-gradient(circle at 30% 30%, rgba(34,197,94,.35), transparent 45%);
}

.ios-card.blue .ios-glow{
  background:radial-gradient(circle at 30% 30%, rgba(56,189,248,.35), transparent 45%);
}
/* ===== FIX iOS INPUTS (EXTRA SEGURO) ===== */
input:disabled,
select:disabled,
textarea:disabled {
  opacity: 1 !important;
  -webkit-text-fill-color: #111 !important;
}

/* Evita que iOS pinte gris los inputs */
input:not([type="file"]),
select,
textarea {
  background-color: #ffffff !important;
  color: #111111 !important;
}

/* Placeholder visible */
input::placeholder,
textarea::placeholder {
  color: #9ca3af !important;
  opacity: 1 !important;
}

/* File input (Choose File en iOS) */
input[type="file"] {
  background: transparent !important;
  color: #ffffff !important;
  border: none !important;
}

/* Evita zoom raro en Safari */
input,
select,
textarea,
button {
  font-size: 16px !important;
}


@keyframes glowMove{
  0%{transform:translate(0,0)}
  50%{transform:translate(22px,-22px)}
  100%{transform:translate(0,0)}
}
</style>
"""




@app.route("/")
def __root():
    return redirect(url_for("empenos_index"))


@app.route("/index")
@login_required
def index():
    return redirect(url_for("empenos_index"))
    
@app.route("/clients")
@login_required
def clients():
    return redirect(url_for("clients_new"))

@app.route("/empenos")
@login_required
def empenos_index():

    q = request.args.get("q", "").strip()
    status = request.args.get("status", "TODOS")

    params = []
    where = []

    if q:
        like = f"%{q}%"
        where.append("(l.item_name LIKE ? OR l.customer_name LIKE ? OR l.phone LIKE ?)")
        params.extend([like, like, like])

    if status and status != "TODOS":
        where.append("l.status = ?")
        params.append(status)

    where_sql = "WHERE " + " AND ".join(where) if where else ""

    # ===== SQL BLINDADO (SIN TRIPLE COMILLAS) =====
    sql = (
        "SELECT "
        "l.id, "
        "l.created_at, "
        "l.item_name, "
        "l.weight_grams, "
        "l.amount, "
        "l.interest_rate, "
        "l.status, "
        "l.due_date, "
        "l.customer_name, "
        "l.phone "
        "FROM loans l "
        f"{where_sql} "
        "ORDER BY l.id DESC "
        "LIMIT 500"
    )

    with closing(get_db()) as conn:
        rows = conn.execute(sql, params).fetchall()

    fixed_rows = []
    for r in rows:
        row = dict(r)
        row["loan_customer_name"] = (row.get("customer_name") or "").strip()
        fixed_rows.append(row)

    now = datetime.now()
    default_rate = float(get_setting("default_interest_rate", "20"))
    term_days = int(get_setting("default_term_days", "90"))
    default_due = (now + timedelta(days=term_days)).strftime("%Y-%m-%d")

    body = render_template_string(
        LOANS_LIST_TPL,
        rows=fixed_rows,
        q=q,
        status=status,
        now=now,
        default_rate=default_rate,
        default_due=default_due
    )

    return render_page(body, title="Empeños", active="loans")



from datetime import datetime, date
from contextlib import closing

@app.route("/empenos/<int:loan_id>")
@login_required
def view_empeno(loan_id):

    # =========================
    # LEER EMPEÑO
    # =========================
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM loans WHERE id=?",
            (loan_id,)
        ).fetchone()

    if not row:
        return "No encontrado", 404

    # =========================
    # FECHAS
    # =========================
    created_dt = parse_dt(row["created_at"])
    start_date = created_dt.date()
    today = date.today()

    days = (today - start_date).days
    if days < 0:
        days = 0

    # =========================
    # INTERÉS DIARIO
    # =========================
    monthly_rate = (row["interest_rate"] or 20) / 100
    daily_rate = monthly_rate / 30

    interest_today = row["amount"] * daily_rate * days
    total_today = row["amount"] + interest_today

    # =========================
    # HTML
    # =========================
    body = '''
    <div class="max-w-3xl mx-auto glass p-6 rounded-2xl">

      <div class="ficha-header mb-4">
        <div>Empeño #{id}</div>
        <div>{created}</div>
      </div>

      <div class="grid md:grid-cols-2 gap-4 text-sm">

        <div>
          <div class="ficha-line"><b>Cliente:</b> {customer}</div>
          <div class="ficha-line"><b>Teléfono:</b> {phone}</div>
          <div class="ficha-line"><b>Artículo:</b> {item}</div>
          <div class="ficha-line"><b>Peso:</b> {weight} g</div>
        </div>

        <div>
          <div class="ficha-line"><b>Capital:</b> ${amount}</div>
          <div class="ficha-line"><b>Interés mensual:</b> {rate}%</div>
          <div class="ficha-line"><b>Interés al día:</b> ${interest}</div>
          <div class="ficha-line"><b>Total hoy:</b> ${total}</div>
        </div>

      </div>

      <div class="ficha-line mt-4">
        <b>Inició:</b> {start}
      </div>

      <div class="ficha-actions mt-6">
        <a href="{pay_url}">Pago</a>
        <a href="{edit_url}">Editar</a>
        <a href="{back_url}">Volver</a>
      </div>

    </div>
    '''.format(
        id=row["id"],
        created=row["created_at"][:10],
        customer=row["customer_name"],
        phone=row["phone"],
        item=row["item_name"],
        weight=f"{row['weight_grams']:.2f}",
        amount=f"{row['amount']:.2f}",
        rate=f"{row['interest_rate']:.2f}",
        interest=f"{interest_today:.2f}",
        total=f"{total_today:.2f}",
        start=start_date,
        pay_url=url_for("payment_page", loan_id=row["id"]),
        edit_url=url_for("edit_loan_page", loan_id=row["id"]),
        back_url=url_for("empenos_index")
    )

    return render_page(body, title=f"Empeño #{loan_id}", active="loans")



@app.route("/empenos/nuevo", methods=["GET", "POST"])
@login_required
def empenos_nuevo():

    from datetime import datetime, timedelta, date
    from contextlib import closing
    from werkzeug.utils import secure_filename
    import time

    # =========================
    # POST → GUARDAR EMPEÑO
    # =========================
    if request.method == "POST":

        now_time = datetime.now().strftime("%H:%M:%S")

        customer_name = request.form.get("customer_name", "").strip()
        customer_id   = request.form.get("customer_id", "").strip()
        phone         = request.form.get("phone", "").strip()

        item_name    = request.form.get("item_name", "").strip()
        weight_grams = float(request.form.get("weight_grams", 0) or 0)
        amount       = float(request.form.get("amount", 0) or 0)

        interest_rate = float(
            request.form.get(
                "interest_rate",
                get_setting("default_interest_rate", "20")
            ) or 20
        )

        start_date = request.form.get("start_date")

        if not customer_name or not customer_id or not phone or not item_name or amount <= 0 or not start_date:
            return "Faltan campos obligatorios", 400

        created_at = f"{start_date} {now_time}"

        term_days = int(get_setting("default_term_days", "90"))
        due_date = (
            datetime.strptime(start_date, "%Y-%m-%d") +
            timedelta(days=term_days)
        ).strftime("%Y-%m-%d")

        photo_path = ""
        file = request.files.get("photo")
        if file and file.filename:
            fname = f"{int(time.time())}_" + secure_filename(file.filename)
            file.save(UPLOAD_DIR / fname)
            photo_path = "/uploads/" + fname

        # ===== GUARDAR EN BD =====
        with closing(get_db()) as conn:

            conn.execute(
                """
                INSERT INTO loans (
                    created_at,
                    item_name,
                    weight_grams,
                    customer_name,
                    customer_id,
                    phone,
                    amount,
                    interest_rate,
                    due_date,
                    photo_path
                )
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    created_at,
                    item_name,
                    weight_grams,
                    customer_name,
                    customer_id,
                    phone,
                    amount,
                    interest_rate,
                    due_date,
                    photo_path
                )
            )

            conn.execute(
                """
                INSERT INTO cash_movements (
                    when_at,
                    concept,
                    amount,
                    ref
                )
                VALUES (?,?,?,?)
                """,
                (
                    created_at,
                    f"Desembolso empeño {customer_name}",
                    -amount,
                    "LOAN"
                )
            )

            conn.commit()

        return redirect(url_for("empenos_index"))

    # =========================
    # GET → FORMULARIO
    # =========================
    today = date.today().isoformat()
    default_rate = float(get_setting("default_interest_rate", "20"))

    body = f"""
    <div class="max-w-3xl mx-auto glass p-6 rounded-2xl">
      <h2 class="text-xl font-bold mb-4">Nuevo Empeño</h2>

      <form method="post" enctype="multipart/form-data" class="space-y-4">
        <input name="customer_name" placeholder="Nombre del cliente" required class="w-full p-2"/>
        <input name="customer_id" placeholder="ID del cliente" required class="w-full p-2"/>
        <input name="phone" placeholder="Teléfono" required class="w-full p-2"/>
        <input name="item_name" placeholder="Artículo" required class="w-full p-2"/>
        <input name="weight_grams" type="number" step="0.01" placeholder="Peso (g)" class="w-full p-2"/>
        <input name="amount" type="number" step="0.01" required placeholder="Monto" class="w-full p-2"/>
        <input name="interest_rate" type="number" step="0.01" value="{default_rate:.2f}" class="w-full p-2"/>
        <input name="start_date" type="date" value="{today}" class="w-full p-2"/>
        <input name="photo" type="file" accept="image/*" class="w-full p-2"/>

        <button class="gold-gradient px-6 py-3 rounded-xl">Guardar empeño</button>
        <a href="/empenos">Cancelar</a>
      </form>
    </div>
    """

    return render_page(body, title="Nuevo empeño", active="loans")




# ====== Editar Empeño ======
EDIT_TPL = """
<div class="max-w-2xl mx-auto glass p-6 rounded-2xl">

  <div class="ficha-header mb-4 flex justify-between">
    <div>Editar empeño #{{ row.id }}</div>
    <div>{{ row.created_at[:10] }}</div>
  </div>

  <form method="post" enctype="multipart/form-data" class="space-y-4">

    <div class="ficha-line">
      <label class="block text-sm mb-1">Artículo</label>
      <input name="item_name" value="{{ row.item_name }}" class="w-full rounded-xl border bg-black/40 p-2"/>
    </div>

    <div class="ficha-line">
      <label class="block text-sm mb-1">Peso (gramos)</label>
      <input name="weight_grams" type="number" step="0.01" value="{{ row.weight_grams }}" class="w-full rounded-xl border bg-black/40 p-2"/>
    </div>

    <div class="ficha-line">
      <label class="block text-sm mb-1">Cliente</label>
      <input name="customer_name" value="{{ row.customer_name }}" class="w-full rounded-xl border bg-black/40 p-2"/>
    </div>

    <div class="ficha-line">
      <label class="block text-sm mb-1">ID Cliente</label>
      <input name="customer_id" value="{{ row.customer_id }}" class="w-full rounded-xl border bg-black/40 p-2"/>
    </div>

    <div class="ficha-line">
      <label class="block text-sm mb-1">Teléfono</label>
      <input name="phone" value="{{ row.phone }}" class="w-full rounded-xl border bg-black/40 p-2"/>
    </div>

    <div class="ficha-line">
      <label class="block text-sm mb-1">Monto</label>
      <input name="amount" type="number" step="0.01" value="{{ row.amount }}" class="w-full rounded-xl border bg-black/40 p-2"/>
    </div>

    <div class="ficha-line">
      <label class="block text-sm mb-1">Interés (%) mensual</label>
      <input name="interest_rate" type="number" step="0.01" value="{{ row.interest_rate }}" class="w-full rounded-xl border bg-black/40 p-2"/>
    </div>

    <div class="ficha-line">
      <label class="block text-sm mb-1">Fecha de vencimiento</label>
      <input name="due_date" type="date" value="{{ row.due_date }}" class="w-full rounded-xl border bg-black/40 p-2"/>
    </div>

    <div class="ficha-line">
      <label class="block text-sm mb-1">Foto del artículo</label>

      {% if row.photo_path %}
        <img src="{{ row.photo_path }}" class="h-28 w-28 object-cover rounded-xl ring-2 ring-amber-300 mb-2"/>
      {% else %}
        <div class="h-28 w-28 flex items-center justify-center rounded-xl border border-amber-300/40 bg-black/30 text-stone-400 text-xs mb-2">
          Sin foto
        </div>
      {% endif %}

      <input type="file" name="photo" accept="image/*" class="w-full text-sm rounded-xl border bg-black/40 p-2"/>
    </div>

    <div class="ficha-actions pt-4 flex gap-3">
      <button class="gold-gradient font-semibold px-6 py-2 rounded-xl">
        Guardar cambios
      </button>

      <a href="{{ url_for('empenos_index') }}" class="px-6 py-2 rounded-xl border">
        Cancelar
      </a>
    </div>

  </form>
</div>
"""

# ====== Detalle / Ticket Empeño ======
DETAIL_TPL = """
<div class="max-w-3xl mx-auto empeno-ficha glass p-6 rounded-2xl">

  <div class="ficha-header flex justify-between mb-4">
    <div>Empeño #{{ row.id }}</div>
    <div>{{ row.created_at[:10] }}</div>
  </div>

  <div class="grid md:grid-cols-2 gap-4 text-sm">

    <div>
      <div class="ficha-line"><b>Cliente:</b> {{ row.customer_name }}</div>
      <div class="ficha-line"><b>ID:</b> {{ row.customer_id }}</div>
      <div class="ficha-line"><b>Teléfono:</b> {{ row.phone }}</div>
    </div>

    <div>
      <div class="ficha-line"><b>Artículo:</b> {{ row.item_name }}</div>
      <div class="ficha-line"><b>Peso:</b> {{ "%.2f"|format(row.weight_grams) }} g</div>
      <div class="ficha-line">
        <b>Estado:</b>
        <span class="estado {{ row.status|lower }}">{{ row.status }}</span>
      </div>
    </div>

  </div>

  <div class="my-4 h-px bg-gradient-to-r from-transparent via-amber-300 to-transparent"></div>

  <div class="grid md:grid-cols-2 gap-4 text-sm">

    <div>
      <div class="ficha-line"><b>Capital:</b> ${{ "%.2f"|format(row.amount) }}</div>
      <div class="ficha-line"><b>Interés mensual:</b> {{ "%.2f"|format(row.interest_rate) }}%</div>
      <div class="ficha-line"><b>Interés al día:</b> ${{ "%.2f"|format(interest_today) }}</div>
      <div class="ficha-line"><b>Total hoy:</b> ${{ "%.2f"|format(total_today) }}</div>
    </div>

    <div>
      <div class="ficha-line"><b>Fecha inicio:</b> {{ row.created_at[:10] }}</div>
      <div class="ficha-line"><b>Vence:</b> {{ row.due_date }}</div>
    </div>

  </div>

  {% if row.photo_path %}
  <div class="mt-4">
    <b>Foto del artículo:</b><br>
    <img src="{{ row.photo_path }}"
         class="mt-2 h-48 rounded-xl ring-2 ring-amber-300"/>
  </div>
  {% endif %}

  <div class="ficha-actions mt-6 flex flex-wrap gap-3">
    <a href="{{ url_for('ticket', loan_id=row.id) }}">Recibo</a>
    <a href="{{ url_for('edit_loan_page', loan_id=row.id) }}">Editar</a>
    <a href="{{ url_for('payment_page', loan_id=row.id) }}">Pago</a>
    <a href="{{ url_for('empenos_index') }}">Volver</a>
  </div>

</div>
"""

# ====== RUTA EDITAR ======
@app.route("/edit/<int:loan_id>", methods=["GET", "POST"])
@login_required
def edit_loan_page(loan_id: int):

    from pathlib import Path
    import uuid
    from contextlib import closing

    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM loans WHERE id=?",
            (loan_id,)
        ).fetchone()

    if not row:
        return "No encontrado", 404

    if request.method == "POST":

        item_name = request.form.get("item_name", "")
        weight = request.form.get("weight_grams", 0)
        customer_name = request.form.get("customer_name", "")
        customer_id = request.form.get("customer_id", "")
        phone = request.form.get("phone", "")
        amount = request.form.get("amount", 0)
        rate = request.form.get("interest_rate", 0)
        due_date = request.form.get("due_date")

        photo = request.files.get("photo")
        photo_path = None

        with closing(get_db()) as conn:

            if photo and photo.filename:
                upload_dir = Path("uploads/items")
                upload_dir.mkdir(parents=True, exist_ok=True)
                ext = Path(photo.filename).suffix.lower()
                fname = f"item_{loan_id}_{uuid.uuid4().hex}{ext}"
                photo.save(upload_dir / fname)
                photo_path = f"/uploads/items/{fname}"

            conn.execute("""
                UPDATE loans
                SET
                    item_name=?,
                    weight_grams=?,
                    customer_name=?,
                    customer_id=?,
                    phone=?,
                    amount=?,
                    interest_rate=?,
                    due_date=?,
                    photo_path=COALESCE(?, photo_path)
                WHERE id=?
            """, (
                item_name,
                weight,
                customer_name,
                customer_id,
                phone,
                amount,
                rate,
                due_date,
                photo_path,
                loan_id
            ))

            conn.commit()

        return redirect(url_for("empenos_index"))

    return render_page(
        render_template_string(EDIT_TPL, row=row),
        title=f"Editar {loan_id}",
        active="loans"
    )



    # ========= GET =========
    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM loans WHERE id=?",
            (loan_id,)
        ).fetchone()

    if not row:
        return "No encontrado", 404

    body = render_template_string(EDIT_TPL, row=row)
    return render_page(body, title=f"Editar {loan_id}", active="loans")



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

# ======================================================
# TICKET / RECIBO DE EMPEÑO — FORMATO EMPRESARIAL
# ======================================================

TICKET_TPL = """
<style>

/* ================= PÁGINA ================= */
@page {
  size: auto;
  margin: 10mm;
  background: white;
}

/* ================= BASE ================= */
html, body {
  background: #fff;
  margin: 0;
  padding: 0;
}

.ticket{
  max-width:420px;
  margin:30px auto;
  background:#fff;
  color:#111;
  border-radius:16px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto;
  box-shadow:0 12px 30px rgba(0,0,0,.15);
}

.header{
  background:#000;
  color:#d4af37;
  text-align:center;
  padding:20px 16px;
  border-radius:16px 16px 0 0;
}

.header img{
  width:70px;
  margin:0 auto 6px;
  display:block;
}

.header h1{
  margin:0;
  font-size:22px;
  font-weight:900;
}

.section{
  padding:16px 20px;
  font-size:14px;
}

.section-title{
  font-weight:800;
  border-bottom:1px solid #eee;
  margin-bottom:6px;
  padding-bottom:4px;
}

.row{
  display:flex;
  justify-content:space-between;
  margin:5px 0;
}

.row span{
  color:#555;
}

.highlight{
  background:#fff8eb;
  border:1px solid #f1d8a8;
  border-radius:12px;
  padding:12px;
  margin-top:8px;
}

.total{
  font-size:18px;
  font-weight:900;
}

.msg{
  text-align:center;
  font-size:13px;
  margin-top:10px;
  color:#333;
}

.contact{
  text-align:center;
  font-size:12px;
  margin-top:12px;
  line-height:1.6;
  color:#333;
}

.actions{
  display:flex;
  gap:10px;
  padding:16px 20px 20px;
}

.btn{
  flex:1;
  padding:12px;
  border-radius:10px;
  font-weight:700;
  border:none;
  cursor:pointer;
}

.print{background:#111;color:#d4af37;}
.whatsapp{background:#16a34a;color:#fff;}
.sms{background:#2563eb;color:#fff;}

/* ================= IMPRESIÓN (FIX DEFINITIVO) ================= */
@media print {

  html, body {
    background: white !important;
    margin: 0 !important;
    padding: 0 !important;
  }

  body * {
    visibility: hidden !important;
    background: transparent !important;
    box-shadow: none !important;
  }

  .ticket, .ticket * {
    visibility: visible !important;
    background: white !important;
  }

  .ticket {
    margin: 0 auto !important;
    border-radius: 0 !important;
    box-shadow: none !important;
  }

  .actions {
    display: none !important;
  }
}
</style>

<div class="ticket">

  <div class="header">
    <img src="/static/world_jewelry_logo.png">
    <h1>World Jewelry</h1>
  </div>

  <div class="section">
    <div class="section-title">CLIENTE</div>
    <div class="row"><span>Nombre</span><strong>{{ row.customer_name }}</strong></div>
    <div class="row"><span>ID</span><strong>{{ row.customer_id }}</strong></div>
    <div class="row"><span>Teléfono</span><strong>{{ row.phone }}</strong></div>
  </div>

  <div class="section">
    <div class="section-title">ARTÍCULO EMPEÑADO</div>
    <div class="row"><span>Artículo</span><strong>{{ row.item_name }}</strong></div>
    <div class="row"><span>Peso</span><strong>{{ "%.2f"|format(row.weight_grams) }} g</strong></div>

    <div class="highlight">
      <div class="row"><span>Monto entregado</span><strong>${{ "%.2f"|format(row.amount) }}</strong></div>
      <div class="row"><span>Interés mensual</span><strong>{{ "%.2f"|format(row.interest_rate) }}%</strong></div>
      <div class="row"><span>Vence</span><strong>{{ row.due_date }}</strong></div>
    </div>
  </div>

  <div class="section">
    <div class="section-title">RESUMEN</div>

    <div class="row total">
      <span>Total a pagar hoy</span>
      <strong>${{ "%.2f"|format(total_pagado) }}</strong>
    </div>

    <div class="row total" style="margin-top:6px;">
      <span>Saldo pendiente</span>
      <strong style="color:#b91c1c;">
        ${{ "%.2f"|format(saldo_pendiente) }}
      </strong>
    </div>
  </div>

  <div class="msg">
    Gracias por preferirnos 🙏<br>
    Agradecemos su confianza en <b>World Jewelry</b>.
  </div>

  <div class="contact">
    📍 Av. Rexach 671 Calle 14, San Juan, Puerto Rico 00915<br>
    📞 787-320-5842 · 320-414-2211<br>
    📱 787-451-4342<br>
    Instagram: <b>@worldjewelrypr</b>
  </div>

</div>

<div class="actions">
  <button class="btn print" onclick="window.print()">🖨️ Imprimir</button>
</div>
"""


# ======================================================
# RUTA DEL RECIBO
# ======================================================

@app.route("/ticket/<int:loan_id>")
@login_required
def loan_ticket(loan_id: int):

    from contextlib import closing

    with closing(get_db()) as conn:
        r = conn.execute("""
            SELECT
                l.id,
                l.created_at,
                l.item_name,
                l.weight_grams,
                l.interest_rate,
                l.due_date,
                l.customer_name,
                l.customer_id,
                l.phone,

                -- 🔒 MONTO ORIGINAL ENTREGADO (NO CAMBIA JAMÁS)
                (
                    l.amount +
                    COALESCE((
                        SELECT SUM(amount)
                        FROM payments
                        WHERE loan_id = l.id
                          AND type = 'ABONO'
                    ), 0)
                ) AS monto_entregado

            FROM loans l
            WHERE l.id = ?
        """, (loan_id,)).fetchone()

    if not r:
        return "No encontrado", 404

    body = f"""
<style>
/* ================= ESTILO BASE ================= */
.ticket {{
  max-width: 380px;
  margin: 30px auto;
  padding: 20px;
  background: #fff;
  color: #111;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto;
  border-radius: 14px;
  box-shadow: 0 10px 30px rgba(0,0,0,.15);
}}

.center {{
  text-align: center;
}}

.badge {{
  display: inline-block;
  margin: 6px 0;
  padding: 4px 10px;
  font-size: 12px;
  font-weight: 700;
  background: #111;
  color: #d4af37;
  border-radius: 999px;
}}

.row {{
  display: flex;
  justify-content: space-between;
  font-size: 14px;
  margin: 6px 0;
}}

.row span {{
  color: #555;
}}

.row strong {{
  font-weight: 600;
}}

hr {{
  border: none;
  height: 1px;
  background: #eee;
  margin: 14px 0;
}}

.footer {{
  text-align: center;
  font-size: 12px;
  color: #444;
  margin-top: 14px;
  line-height: 1.4;
}}

.print-btn {{
  width: 100%;
  margin-top: 14px;
  padding: 10px;
  background: #111;
  color: #d4af37;
  border: none;
  border-radius: 10px;
  font-weight: 700;
  cursor: pointer;
}}

/* ================= IMPRESIÓN ================= */
@media print {{
  html, body {{
    background: white !important;
    margin: 0 !important;
    padding: 0 !important;
  }}

  body * {{
    visibility: hidden;
    background: transparent !important;
  }}

  .ticket, .ticket * {{
    visibility: visible;
    background: white !important;
  }}

  .ticket {{
    margin: 0 auto !important;
    box-shadow: none !important;
  }}

  button {{
    display: none !important;
  }}
}}

@page {{
  margin: 10mm;
  background: white;
}}
</style>

<div class="ticket">
  <div class="center">
    <h2>World Jewelry</h2>
    <div class="badge">COMPROBANTE DE EMPEÑO</div>
    <small>Este monto no cambia con pagos</small>
  </div>

  <hr>

  <div class="row"><span>Empeño #</span><strong>{r['id']}</strong></div>
  <div class="row"><span>Fecha</span><strong>{r['created_at']}</strong></div>
  <div class="row"><span>Cliente</span><strong>{r['customer_name']}</strong></div>
  <div class="row"><span>ID</span><strong>{r['customer_id']}</strong></div>
  <div class="row"><span>Teléfono</span><strong>{r['phone']}</strong></div>

  <hr>

  <div class="row"><span>Artículo</span><strong>{r['item_name']}</strong></div>
  <div class="row"><span>Peso</span><strong>{r['weight_grams']} g</strong></div>
  <div class="row">
    <span>Monto entregado</span>
    <strong>$ {r['monto_entregado']:.2f}</strong>
  </div>
  <div class="row"><span>Vence</span><strong>{r['due_date']}</strong></div>

  <hr>

  <div class="footer">
    Este documento certifica el monto entregado al cliente.<br>
    No se modifica por pagos posteriores.<br><br>
    Gracias por su confianza 🙏<br>
    <b>World Jewelry</b>
  </div>

  <button class="print-btn" onclick="window.print()">
    🖨️ Imprimir comprobante
  </button>
</div>
"""

    return render_page(body, title="Comprobante de empeño", active="loans")


      


@app.route("/pago/recibo/<int:payment_id>")
@login_required
def payment_receipt(payment_id: int):

    from contextlib import closing

    with closing(get_db()) as conn:

        # ==================================================
        # DATOS BASE DEL RECIBO + MONTO ENTREGADO ORIGINAL
        # ==================================================
        base = conn.execute("""
            SELECT
                p.id            AS receipt_number,
                p.loan_id,
                p.paid_at,
                DATE(p.paid_at) AS paid_date,

                l.item_name,
                l.weight_grams,
                l.interest_rate,
                l.due_date,

                -- 🔒 MONTO ENTREGADO ORIGINAL (NUNCA CAMBIA)
                (
                    l.amount +
                    COALESCE((
                        SELECT SUM(amount)
                        FROM payments
                        WHERE loan_id = l.id
                          AND type = 'ABONO'
                    ), 0)
                ) AS monto_original,

                l.customer_name,
                l.customer_id,
                l.phone

            FROM payments p
            JOIN loans l ON l.id = p.loan_id
            WHERE p.id = ?
        """, (payment_id,)).fetchone()

        if not base:
            return "Pago no encontrado", 404

        # ==================================================
        # PAGOS DEL MISMO RECIBO (MISMA FECHA / HORA)
        # ==================================================
        rows = conn.execute("""
            SELECT type, amount
            FROM payments
            WHERE loan_id = ?
              AND paid_at = ?
        """, (base["loan_id"], base["paid_at"])).fetchall()

        # ==================================================
        # TOTAL DE CAPITAL ABONADO HISTÓRICO
        # ==================================================
        capital_total = conn.execute("""
            SELECT COALESCE(SUM(amount),0)
            FROM payments
            WHERE loan_id = ?
              AND type = 'ABONO'
        """, (base["loan_id"],)).fetchone()[0]

    # ================= CÁLCULOS =================
    interest_amt = sum(r["amount"] for r in rows if r["type"] == "INTERES")
    capital_amt  = sum(r["amount"] for r in rows if r["type"] == "ABONO")

    total_pagado = interest_amt + capital_amt

    saldo_pendiente = float(base["monto_original"]) - float(capital_total)
    if saldo_pendiente < 0:
        saldo_pendiente = 0.0

    # ================= HTML =================
    body = f"""
<style>
/* ================= ESTILO BASE ================= */
.pay-wrap {{
  max-width: 430px;
  margin: 40px auto;
  padding-bottom: 30px;
  background: #fff;
  color: #111;
  font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto;
  border-radius: 16px;
  box-shadow: 0 12px 40px rgba(0,0,0,.2);
}}

.pay-header {{
  text-align: center;
  padding: 24px 20px 18px;
}}

.pay-header img {{
  width: 90px;
  margin: 0 auto 8px;
  display: block;
}}

.pay-header h1 {{
  margin: 0;
  font-size: 20px;
  font-weight: 800;
}}

.pay-header small {{
  color: #555;
}}

.divider {{
  height: 1px;
  background: #eee;
  margin: 14px 0;
}}

.pay-body {{
  padding: 0 22px 22px;
  font-size: 14px;
}}

.row {{
  display: flex;
  justify-content: space-between;
  margin: 6px 0;
}}

.row span {{
  color: #555;
}}

.row strong {{
  font-weight: 600;
}}

.total {{
  font-size: 20px;
  font-weight: 800;
}}

.thanks {{
  text-align: center;
  font-size: 13px;
  color: #444;
  margin-top: 18px;
}}

.contact {{
  text-align: center;
  font-size: 12px;
  color: #555;
  margin-top: 10px;
  line-height: 1.5;
}}

.footer {{
  text-align: center;
  font-size: 11px;
  color: #777;
  margin-top: 14px;
}}

.print-btn {{
  width: 100%;
  margin-top: 16px;
  padding: 12px;
  background: #111;
  color: #d4af37;
  border: none;
  border-radius: 10px;
  font-weight: 700;
  cursor: pointer;
}}

/* ================= IMPRESIÓN ================= */
@media print {{
  html, body {{
    background: white !important;
    margin: 0 !important;
    padding: 0 !important;
  }}

  body * {{
    visibility: hidden;
    background: transparent !important;
  }}

  .pay-wrap, .pay-wrap * {{
    visibility: visible;
    background: white !important;
  }}

  .pay-wrap {{
    margin: 0 auto !important;
    box-shadow: none !important;
    border-radius: 0 !important;
  }}

  button {{
    display: none !important;
  }}
}}

@page {{
  margin: 10mm;
  background: white;
}}
</style>

<div class="pay-wrap">

  <div class="pay-header">
    <img src="/static/world_jewelry_logo.png">
    <h1>World Jewelry</h1>
    <small>Recibo de Pago</small>
  </div>

  <div class="divider"></div>

  <div class="pay-body">

    <div class="row"><span>Recibo #</span><strong>{base['receipt_number']}</strong></div>
    <div class="row"><span>Empeño #</span><strong>{base['loan_id']}</strong></div>
    <div class="row"><span>Cliente</span><strong>{base['customer_name']}</strong></div>
    <div class="row"><span>Artículo</span><strong>{base['item_name']}</strong></div>
    <div class="row"><span>Fecha</span><strong>{base['paid_date']}</strong></div>

    <div class="divider"></div>

    <div class="row"><span>Interés aplicado</span><strong>$ {interest_amt:.2f}</strong></div>
    <div class="row"><span>Capital aplicado</span><strong>$ {capital_amt:.2f}</strong></div>

    <div class="divider"></div>

    <div class="row total">
      <span>Total pagado</span>
      <strong>$ {total_pagado:.2f}</strong>
    </div>

    <div class="row total" style="margin-top:6px;">
      <span>Saldo pendiente</span>
      <strong style="color:#b91c1c;">$ {saldo_pendiente:.2f}</strong>
    </div>

    <div class="thanks">
      Gracias por preferirnos 🙏<br>
      Agradecemos su pago y confianza.
    </div>

    <div class="contact">
      📍 Av. Rexach 671 Calle 14, San Juan<br>
      Puerto Rico, 00915<br><br>
      📞 787-320-5842 · 320-414-2211<br>
      📱 <strong>787-451-4342</strong>
    </div>

    <div class="footer">
      Instagram: <strong>@worldjewelrypr</strong>
    </div>

    <button class="print-btn" onclick="window.print()">
      🖨️ Imprimir recibo
    </button>

  </div>
</div>
"""

    return render_page(body, title="Recibo de pago", active="loans")





# ====== Estimador de interés mensual por rango ======
INTEREST_CALC_TPL = """
<div class="max-w-xl mx-auto glass p-6 rounded-2xl">
  <h2 class="text-xl font-bold text-yellow-300 mb-3">
    Interés por meses - Empeño #{{ row.id }}
  </h2>

  <form method="get" class="grid grid-cols-2 gap-2 mb-3">
    <div>
      <label class="text-xs">Desde (mes)</label>
      <input type="month" name="from_m" value="{{ from_m or default_from }}"
             class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    </div>

    <div>
      <label class="text-xs">Hasta (mes)</label>
      <input type="month" name="to_m" value="{{ to_m or default_to }}"
             class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    </div>

    <div class="col-span-2 flex gap-2">
      <button class="gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl">
        Calcular
      </button>
      <a href="{{ url_for('index') }}"
         class="px-4 py-2 rounded-xl border border-yellow-200/30">
        Volver
      </a>
    </div>
  </form>

  {% if rows is not none %}
    <div class="overflow-auto rounded-xl border border-yellow-200/30">
      <table class="min-w-full text-sm">
        <thead class="thead-gold">
          <tr>
            <th class="py-2 pl-3">Mes</th>
            <th class="text-right pr-3">Interés</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-stone-800/40 bg-black/40">
          {% for mk, mi in rows %}
            <tr>
              <td class="py-2 pl-3">{{ mk }}</td>
              <td class="text-right pr-3">${{ '%.2f'|format(mi) }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <div class="text-right mt-2">
      Total ({{ rows|length }} mes/es):
      <b>${{ '%.2f'|format(total) }}</b>
    </div>
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
# ====== Pagos con historial y recibos ======

PAY_TPL = """
<h2 class="text-xl font-bold text-yellow-300 mb-3">
  Pago — empeño #{{ row.id }} ({{ row.customer_name }})
</h2>
<!-- 👤 BOTÓN PERFIL CLIENTE -->
<button onclick="togglePerfil()"
        class="glass w-full p-3 rounded-2xl mb-4 flex justify-between items-center">
  <span class="font-bold text-yellow-300">👤 Cliente Perfil</span>
  <span class="text-sm opacity-70">Ver detalles</span>
</button>

<!-- 👤 PERFIL CLIENTE (OCULTO) -->
<div id="perfil_cliente" class="glass rounded-2xl p-4 mb-5" style="display:none;">


  <div class="flex gap-4 items-center">

    <!-- FOTO -->
    <div class="w-24 h-24 rounded-xl overflow-hidden border border-yellow-500">
      {% if row.item_photo %}
        <img src="{{ url_for('item_photo', filename=row.item_photo) }}"
     class="w-full h-full object-cover">
 class="w-full h-full object-cover">
      {% else %}
        <div class="w-full h-full flex items-center justify-center text-xs text-gray-400">
          Sin foto
        </div>
      {% endif %}
    </div>

    <!-- DATOS -->
    <div class="flex-1 text-sm">
      <div class="text-lg font-bold text-yellow-300">
        {{ row.customer_name }}
      </div>

      <div class="text-xs opacity-80">
        ID: {{ row.customer_id }} · 📞 {{ row.phone }}
      </div>

      <div class="mt-1">
        💎 {{ row.item_name }} ({{ row.weight_grams }} g)
      </div>

      <div class="mt-1">
        💰 Capital: <b>${{ '%.2f'|format(capital_pendiente) }}</b><br>
        📅 Vence: {{ row.due_date }}
      </div>
    </div>

  </div>
</div>


<script>
function togglePerfil() {
  const el = document.getElementById("perfil_cliente");
  if (el.style.display === "none") {
    el.style.display = "block";
  } else {
    el.style.display = "none";
  }
}
</script>



<div class="glass p-4 rounded-2xl mb-4 text-sm"
     id="capital_box"
     data-capital="{{ capital_pendiente }}"
     data-tasa="{{ row.interest_rate }}">

  <div class="flex justify-between">
    <span>Capital pendiente</span>
    <b>${{ '%.2f'|format(capital_pendiente) }}</b>
  </div>

  <div class="flex justify-between">
    <span>Interés pendiente al {{ as_of }}</span>
    <b>${{ '%.2f'|format(interest_due) }}</b>
  </div>

  <div class="flex justify-between text-yellow-300 text-lg font-extrabold mt-2">
    <span>TOTAL ADEUDADO</span>
    <span>${{ '%.2f'|format(total_debiendo) }}</span>
  </div>
</div>

<form method="post" class="glass p-4 rounded-2xl space-y-4">

  <!-- FECHAS -->
  <div class="grid md:grid-cols-6 gap-2">
    <div class="md:col-span-2">
      <label class="text-xs">Desde</label>
      <input
        id="from_date"
        name="from_date"
        type="date"
        value="{{ loan_date_html }}"
        min="{{ loan_date_html }}"
        class="w-full rounded-xl border bg-black/40 p-2"
        required
      />
    </div>

    <div class="md:col-span-2">
      <label class="text-xs">Hasta</label>
      <input
        id="as_of_date"
        name="as_of_date"
        type="date"
        value="{{ as_of }}"
        required
        class="w-full rounded-xl border bg-black/40 p-2"
      />
    </div>

    <div class="md:col-span-2">
      <label class="text-xs">Aplicar a</label>
      <select
        name="apply_mode"
        class="w-full rounded-xl border bg-black/40 p-2"
      >
        <option value="AUTO">AUTO (interés → capital)</option>
        <option value="SOLO_INTERES">SOLO INTERÉS</option>
        <option value="SOLO_CAPITAL">SOLO CAPITAL</option>
      </select>
    </div>
  </div>


  <!-- INTERÉS AUTOMÁTICO -->
  <div>
    <label class="text-xs text-yellow-300">
      Interés calculado automáticamente
    </label>
    <input id="amount" name="amount" type="number" step="0.01"
           readonly
           class="w-full rounded-xl border bg-black/40 p-2"/>
  </div>

  <!-- ABONO A CAPITAL -->
  <div>
    <label class="text-xs text-green-400">
      Abono adicional a capital (opcional)
    </label>
    <input name="capital_extra" type="number" step="0.01"
           placeholder="Ej: 100, 500, 1000"
           class="w-full rounded-xl border bg-black/40 p-2"/>
  </div>

  <input name="notes" placeholder="Notas (opcional)"
         class="w-full rounded-xl border bg-black/40 p-2"/>

  <button class="gold-gradient px-4 py-2 rounded-xl w-full">
    Registrar pago
  </button>
</form>

<script>
(function () {

  function calcularInteres() {
    const box = document.getElementById("capital_box");
    const capital = parseFloat(box.dataset.capital || 0);
    const tasa = parseFloat(box.dataset.tasa || 0);

    const desde = document.getElementById("from_date").value;
    const hasta = document.getElementById("as_of_date").value;
    const monto = document.getElementById("amount");

    if (!capital || !tasa || !hasta) return;

    const d1 = desde ? new Date(desde) : new Date(hasta);
    const d2 = new Date(hasta);

    let dias = Math.ceil((d2 - d1) / 86400000);
    if (dias <= 0) dias = 1;

    const interes = capital * (tasa / 100) * (dias / 30);
    monto.value = interes.toFixed(2);
  }

  ["from_date", "as_of_date"].forEach(id => {
    document.getElementById(id).addEventListener("change", calcularInteres);
  });

  window.addEventListener("load", calcularInteres);

})();
</script>

<h3 class="text-lg font-bold text-yellow-300 mt-8">
  Historial de pagos
</h3>

<table class="w-full text-sm mt-3 border rounded-xl overflow-hidden">
  <thead>
    <tr>
      <th class="p-2">Pago</th>
      <th>Fecha</th>
      <th>Monto</th>
      <th>Detalle</th>
      <th>Notas</th>
      <th>Recibo</th>
      <th>Acción</th>
    </tr>
  </thead>

  <tbody>
  {% for p in payments %}
    <tr class="border-t">
      <td class="p-2 font-bold text-yellow-300">
        Pago #{{ loop.index }}
      </td>

      <td>{{ p['paid_date'] }}</td>

      <td class="font-bold">
        ${{ '%.2f'|format(p['total_amount']) }}
      </td>

      <td class="text-xs">
        {% if p['interest_amount'] > 0 %}
          Interés: ${{ '%.2f'|format(p['interest_amount']) }}<br>
        {% endif %}
        {% if p['capital_amount'] > 0 %}
          Capital: ${{ '%.2f'|format(p['capital_amount']) }}
        {% endif %}
      </td>

      <td>{{ p['notes'] or '' }}</td>

      <td>
        <a href="{{ url_for('payment_receipt', payment_id=p['receipt_id']) }}"
           class="text-blue-400 underline">
          Ver recibo
        </a>
      </td>

      <!-- 🔴 DESHACER PAGO (VISIBLE SIEMPRE) -->
      <td>
        <form method="post"
              onsubmit="return confirm('¿Seguro que deseas DESHACER este pago?');"
              class="space-y-1">

          <input type="hidden" name="action" value="UNDO">
          <input type="hidden" name="receipt_id" value="{{ p['receipt_id'] }}">

          <input type="password"
                 name="admin_key"
                 placeholder="Clave"
                 required
                 class="w-20 text-xs rounded border p-1 bg-black/40"/>

          <button class="text-xs bg-red-600 hover:bg-red-700 px-2 py-1 rounded text-white">
            Deshacer
          </button>
        </form>
      </td>
    </tr>
  {% else %}
    <tr>
      <td colspan="7" class="p-3 text-center">
        No hay pagos registrados
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>
"""
@app.route("/pago/<int:loan_id>", methods=["GET", "POST"])
@login_required
def payment_page(loan_id: int):

    from contextlib import closing
    from datetime import datetime, date

    # =========================
    # POST → REGISTRAR / DESHACER PAGO
    # =========================
    if request.method == "POST":

        action = request.form.get("action", "PAY")

        # ==================================================
        # 🔴 DESHACER PAGO (SOLO ADMIN + CLAVE)
        # ==================================================
        if action == "UNDO":

            if session.get("role") != "admin":
                return "Acceso denegado", 403

            if request.form.get("admin_key") != "0219":
                return "Clave incorrecta", 403

            receipt_id = int(request.form.get("receipt_id"))

            with closing(get_db()) as conn:

                base = conn.execute("""
                    SELECT loan_id, paid_at
                    FROM payments
                    WHERE id = ?
                """, (receipt_id,)).fetchone()

                if not base:
                    return "Pago no encontrado", 404

                paid_at = base["paid_at"]

                capital_devuelto = conn.execute("""
                    SELECT COALESCE(SUM(amount),0)
                    FROM payments
                    WHERE loan_id = ?
                      AND paid_at = ?
                      AND type = 'ABONO'
                """, (loan_id, paid_at)).fetchone()[0]

                total_pagado = conn.execute("""
                    SELECT COALESCE(SUM(amount),0)
                    FROM payments
                    WHERE loan_id = ?
                      AND paid_at = ?
                """, (loan_id, paid_at)).fetchone()[0]

                if capital_devuelto > 0:
                    conn.execute("""
                        UPDATE loans
                        SET amount = amount + ?
                        WHERE id = ?
                    """, (round(capital_devuelto, 2), loan_id))

                conn.execute("""
                    DELETE FROM payments
                    WHERE loan_id = ?
                      AND paid_at = ?
                """, (loan_id, paid_at))

                conn.execute("""
                    INSERT INTO cash_movements (when_at, concept, amount, ref)
                    VALUES (?,?,?,?)
                """, (
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    f"REVERSO pago empeño #{loan_id}",
                    -round(total_pagado, 2),
                    "UNDO"
                ))

                conn.commit()

            return redirect(url_for("payment_page", loan_id=loan_id))

        # ==================================================
        # 🟢 REGISTRAR PAGO NORMAL
        # ==================================================
        interest_amt = float(request.form.get("amount", 0) or 0)
        capital_extra = float(request.form.get("capital_extra", 0) or 0)
        notes = request.form.get("notes", "").strip()

        if interest_amt <= 0 and capital_extra <= 0:
            return "Monto inválido", 400

        # FECHA HASTA
        try:
            as_of = datetime.strptime(
                request.form.get("as_of_date", date.today().isoformat()),
                "%Y-%m-%d"
            ).date()
        except Exception:
            as_of = date.today()

        # FECHA DESDE (override manual)
        from_str = request.form.get("from_date", "").strip()
        start_override = None
        if from_str:
            try:
                start_override = datetime.strptime(from_str, "%Y-%m-%d").date()
            except Exception:
                start_override = None

        paid_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with closing(get_db()) as conn:

            loan = conn.execute(
                "SELECT * FROM loans WHERE id=?",
                (loan_id,)
            ).fetchone()

            if not loan:
                return "No encontrado", 404

            start_date = (
                start_override
                if start_override
                else datetime.strptime(loan["created_at"][:10], "%Y-%m-%d").date()
            )

            interest_due = interest_due_as_of(
                loan_id,
                as_of,
                start_date
            )

            to_interest = min(interest_amt, interest_due)
            to_principal = capital_extra

            if to_interest > 0:
                conn.execute("""
                    INSERT INTO payments (loan_id, paid_at, amount, type, notes)
                    VALUES (?,?,?,?,?)
                """, (loan_id, paid_ts, round(to_interest, 2), "INTERES", notes))

            if to_principal > 0:
                conn.execute("""
                    INSERT INTO payments (loan_id, paid_at, amount, type, notes)
                    VALUES (?,?,?,?,?)
                """, (loan_id, paid_ts, round(to_principal, 2), "ABONO", notes))

                conn.execute("""
                    UPDATE loans
                    SET amount = amount - ?
                    WHERE id = ?
                """, (round(to_principal, 2), loan_id))

            total_pagado = round(to_interest + to_principal, 2)

            conn.execute("""
                INSERT INTO cash_movements (when_at, concept, amount, ref)
                VALUES (?,?,?,?)
            """, (paid_ts, f"Pago empeño #{loan_id}", total_pagado, "PAY"))

            conn.commit()

        return redirect(url_for("payment_page", loan_id=loan_id))

    # =========================
    # GET → FORM + HISTORIAL
    # =========================
    with closing(get_db()) as conn:

        row = conn.execute(
            "SELECT * FROM loans WHERE id=?",
            (loan_id,)
        ).fetchone()

        if not row:
            return "No encontrado", 404

        # =========================
        # FECHA DEL EMPEÑO (DESDE)
        # =========================
        loan_date = row["created_at"]
        if isinstance(loan_date, str):
            loan_date = datetime.strptime(loan_date[:10], "%Y-%m-%d")

        loan_date_html = loan_date.strftime("%Y-%m-%d")

        start_date = loan_date.date()
        as_of = date.today()

        interest_due = interest_due_as_of(
            loan_id,
            as_of,
            start_date
        )

        capital_pendiente = float(row["amount"] or 0)
        total_debiendo = capital_pendiente + interest_due

        payments = conn.execute("""
            SELECT
                DATE(paid_at) AS paid_date,
                SUM(amount) AS total_amount,
                SUM(CASE WHEN type='INTERES' THEN amount ELSE 0 END) AS interest_amount,
                SUM(CASE WHEN type='ABONO' THEN amount ELSE 0 END) AS capital_amount,
                GROUP_CONCAT(notes, ' | ') AS notes,
                MIN(id) AS receipt_id
            FROM payments
            WHERE loan_id = ?
            GROUP BY paid_at
            ORDER BY paid_at ASC
        """, (loan_id,)).fetchall()

    body = render_template_string(
        PAY_TPL,
        row=row,
        payments=payments,
        as_of=as_of,
        interest_due=interest_due,
        capital_pendiente=capital_pendiente,
        total_debiendo=total_debiendo,
        loan_date_html=loan_date_html,
        session=session
    )

    return render_page(body, title="Pago", active="loans")


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

# ===============================
# EMPEÑO → MARCAR COMO PERDIDO
# ===============================
@app.route("/empenos/perdido/<int:loan_id>")
@login_required
def loan_mark_lost(loan_id):

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with closing(get_db()) as conn:
        loan = conn.execute(
            "SELECT * FROM loans WHERE id=?",
            (loan_id,)
        ).fetchone()

        if not loan:
            return "Empeño no encontrado", 404

        # 1️⃣ Marcar empeño como PERDIDO
        conn.execute("""
            UPDATE loans
            SET status='PERDIDO'
            WHERE id=?
        """, (loan_id,))

        # 2️⃣ Pasar artículo a INVENTARIO
        conn.execute("""
            INSERT INTO inventory_items (
                item_desc,
                status,
                created_at
            ) VALUES (?,?,?)
        """, (
            f"{loan['item_name']} - Cliente {loan['customer_name']} (Empeño #{loan_id})",
            "EN_VENTA",
            now
        ))

        conn.commit()

    return redirect(url_for("empenos_index"))

INVENTORY_TPL = """
<h2 class="text-xl font-bold text-yellow-300 mb-3">
  Inventario — Empeños perdidos
</h2>

<div class="glass rounded-2xl p-4">

  <div class="overflow-auto rounded-xl border border-yellow-200/30">
    <table class="min-w-full text-sm">
      <thead class="thead-gold">
        <tr>
          <th class="py-2 pl-3">ID</th>
          <th>Descripción</th>
          <th>Status</th>
          <th class="text-right pr-3">Acciones</th>
        </tr>
      </thead>

      <tbody class="divide-y divide-stone-800/40 bg-black/40">

      {% for item in rows %}
        <tr>
          <td class="py-2 pl-3">{{ item.id }}</td>
          <td>{{ item.item_desc }}</td>
          <td>
            {% if item.status == 'PERDIDO' %}
              <span class="text-red-400 font-semibold">PERDIDO</span>
            {% elif item.status == 'VENDIDO' %}
              <span class="text-emerald-400 font-semibold">VENDIDO</span>
            {% else %}
              {{ item.status }}
            {% endif %}
          </td>

          <td class="text-right pr-3">

            {% if item.status == 'PERDIDO' %}
              <form method="post"
                    action="{{ url_for('inventory_sell', item_id=item.id) }}"
                    class="flex gap-2 justify-end">

                <input type="number"
                       name="price"
                       step="0.01"
                       placeholder="Precio"
                       required
                       class="w-24 rounded-xl border border-yellow-200/30 bg-black/40 p-1 text-sm"/>

                <button
                  class="bg-emerald-700 hover:bg-emerald-800 text-white px-3 py-1 rounded-xl text-sm">
                  💰 Vender
                </button>
              </form>
            {% else %}
              <span class="text-stone-400 text-sm">✔ Cerrado</span>
            {% endif %}

          </td>
        </tr>
      {% endfor %}

      {% if not rows %}
        <tr>
          <td colspan="4" class="py-4 text-center text-yellow-200/60">
            No hay artículos en inventario
          </td>
        </tr>
      {% endif %}

      </tbody>
    </table>
  </div>

</div>
"""



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
# ====== Caja (Resumen diario por cliente) ======

CASH_DAILY_TPL = """
<h2 class="text-xl font-bold text-yellow-300 mb-3">💵 Caja del día</h2>

<div class="grid md:grid-cols-3 gap-3 mb-4">
  <div class="glass rounded-2xl p-4">
    <div class="text-xs text-yellow-200/70">Total pagado hoy</div>
    <div class="text-2xl font-extrabold">${{ '%.2f'|format(t_total) }}</div>
  </div>
  <div class="glass rounded-2xl p-4">
    <div class="text-xs text-yellow-200/70">Interés cobrado hoy</div>
    <div class="text-2xl font-extrabold">${{ '%.2f'|format(t_int) }}</div>
  </div>
  <div class="glass rounded-2xl p-4">
    <div class="text-xs text-yellow-200/70">Capital cobrado hoy</div>
    <div class="text-2xl font-extrabold">${{ '%.2f'|format(t_cap) }}</div>
  </div>
</div>

<form method="get" class="glass p-4 rounded-2xl mb-4 grid md:grid-cols-4 gap-2">
  <div class="md:col-span-2">
    <label class="text-xs text-yellow-200/70">Fecha</label>
    <input type="date" name="d" value="{{ d }}" class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
  </div>

  <div class="md:col-span-2">
    <label class="text-xs text-yellow-200/70">Buscar (nombre)</label>
    <input name="q" value="{{ q or '' }}" placeholder="Ej: jose"
           class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
  </div>

  <div class="md:col-span-4">
    <button class="gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl">
      Ver caja
    </button>
  </div>
</form>

<div class="glass rounded-2xl p-4">
  <h3 class="font-semibold mb-3">👤 Pagos del día por cliente</h3>

  <div class="space-y-3">
    {% for r in rows %}
      <div class="empeno-ficha">
        <div class="ficha-header">
          <div>{{ r.customer_name }}</div>
          <div>${{ '%.2f'|format(r.total) }}</div>
        </div>

        <div class="ficha-line">💸 Interés: <b>${{ '%.2f'|format(r.interes) }}</b></div>
        <div class="ficha-line">💰 Capital: <b>${{ '%.2f'|format(r.capital) }}</b></div>

        {% if r.loans_count %}
          <div class="ficha-docs">
            <span>Empeños: {{ r.loans_count }}</span>
            <span>Pagos: {{ r.payments_count }}</span>
          </div>
        {% endif %}
      </div>
    {% else %}
      <div class="text-center text-yellow-200/70 py-10">
        No hay pagos para esa fecha.
      </div>
    {% endfor %}
  </div>
</div>
"""
# ============================
# CONFIRMAR ELIMINAR VENTA
# ============================
CONFIRM_DELETE_SALE_TPL = """
<div class="max-w-md mx-auto glass p-6 rounded-2xl mt-10">
  <h2 class="text-xl font-bold text-yellow-300 mb-2">
    Eliminar artículo de ventas
  </h2>

  <p class="text-sm mb-4">
    Vas a eliminar el artículo
    <b>#{{ item.id }}</b> — {{ item.item_desc }}<br>
    <span class="text-yellow-200/70">
      Estado actual: {{ item.status }}
    </span>
  </p>

  {% if error %}
  <div class="mb-4 p-3 bg-red-900/40 border border-red-700 rounded-xl">
    {{ error }}
  </div>
  {% endif %}

  <form method="post"
        action="{{ url_for('sales_delete', sale_id=item.id) }}"
        class="space-y-4">

    <div>
      <label class="text-xs text-yellow-200/70 block mb-1">
        Confirma tu contraseña
      </label>
      <input name="password" type="password" required
        placeholder="Tu contraseña"
        class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    </div>

    <div class="flex gap-3 justify-end">
      <a href="{{ url_for('sales_page') }}"
         class="px-4 py-2 rounded-xl border border-yellow-200/30">
        Cancelar
      </a>

      <button
        class="px-4 py-2 rounded-xl bg-red-700 hover:bg-red-800 font-semibold">
        Eliminar definitivamente
      </button>
    </div>
  </form>
</div>
"""



@app.route("/caja", methods=["GET"])
@login_required
def cash():

    # Fecha (por defecto hoy)
    d = request.args.get("d", date.today().isoformat())
    q = (request.args.get("q", "") or "").strip().lower()

    dfrom_ts = f"{d} 00:00:00"
    dto_ts   = f"{d} 23:59:59"

    with closing(get_db()) as conn:

        # Traer resumen por cliente desde payments + loans
        rows = conn.execute("""
            SELECT
                COALESCE(NULLIF(TRIM(l.customer_name),''),'(Sin nombre)') AS customer_name,

                SUM(CASE WHEN p.type='INTERES' THEN p.amount ELSE 0 END) AS interes,
                SUM(CASE WHEN p.type='ABONO' THEN p.amount ELSE 0 END)    AS capital,
                SUM(p.amount) AS total,

                COUNT(DISTINCT l.id) AS loans_count,
                COUNT(p.id) AS payments_count

            FROM payments p
            JOIN loans l ON l.id = p.loan_id
            WHERE p.paid_at BETWEEN ? AND ?
            GROUP BY COALESCE(NULLIF(TRIM(l.customer_name),''),'(Sin nombre)')
            ORDER BY total DESC
        """, (dfrom_ts, dto_ts)).fetchall()

    # Filtro por nombre (en python para no complicar SQL)
    fixed = []
    t_total = 0.0
    t_int   = 0.0
    t_cap   = 0.0

    for r in rows:
        r = dict(r)
        name = (r.get("customer_name") or "").strip()
        if q and q not in name.lower():
            continue

        r["interes"] = float(r.get("interes") or 0.0)
        r["capital"] = float(r.get("capital") or 0.0)
        r["total"]   = float(r.get("total") or 0.0)

        fixed.append(r)

        t_total += r["total"]
        t_int   += r["interes"]
        t_cap   += r["capital"]

    return render_page(
        render_template_string(
            CASH_DAILY_TPL,
            rows=fixed,
            d=d,
            q=q,
            t_total=t_total,
            t_int=t_int,
            t_cap=t_cap
        ),
        title="Caja",
        active="cash"
    )

# ====== REPORTES SIMPLES (3 OPCIONES) ======

REPORTS_TPL = """
<h2 class="text-xl font-bold text-yellow-300 mb-4">📊 Reportes</h2>

<form method="get" class="glass p-4 rounded-2xl grid md:grid-cols-4 gap-3 mb-5">

  <div>
    <label class="text-xs text-yellow-200">Desde</label>
    <input type="date" name="from" value="{{ dfrom }}"
           class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
  </div>

  <div>
    <label class="text-xs text-yellow-200">Hasta</label>
    <input type="date" name="to" value="{{ dto }}"
           class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
  </div>

  <div>
    <label class="text-xs text-yellow-200">Tipo de reporte</label>
    <select name="kind"
            class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2">
      <option value="intereses" {% if kind=='intereses' %}selected{% endif %}>
        💰 Intereses cobrados
      </option>
      <option value="capital" {% if kind=='capital' %}selected{% endif %}>
        💵 Capital recuperado
      </option>
      <option value="riesgo" {% if kind=='riesgo' %}selected{% endif %}>
        ⚠️ Empeños en riesgo
      </option>
    </select>
  </div>

  <div class="flex items-end">
    <button class="w-full gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl">
      Ver reporte
    </button>
  </div>

</form>

<div class="glass rounded-2xl p-4">

{% if kind in ['intereses','capital'] %}

  <div class="overflow-auto rounded-xl border border-yellow-200/30">
    <table class="min-w-full text-sm">
      <thead class="thead-gold">
        <tr>
          <th class="py-2 pl-3">Fecha</th>
          <th>Empeño</th>
          <th>Tipo</th>
          <th class="text-right pr-3">Monto</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-stone-800/40 bg-black/40">
        {% for r in rows %}
        <tr>
          <td class="py-2 pl-3">{{ r.paid_at[:10] }}</td>
          <td>#{{ r.loan_id }}</td>
          <td>{{ r.type }}</td>
          <td class="text-right pr-3">${{ '%.2f'|format(r.amount) }}</td>
        </tr>
        {% endfor %}
        {% if not rows %}
        <tr><td colspan="4" class="p-3 text-center text-yellow-200/60">Sin datos</td></tr>
        {% endif %}
      </tbody>
    </table>
  </div>

  <div class="text-right mt-3 text-lg">
    Total: <b class="text-yellow-300">${{ '%.2f'|format(total) }}</b>
  </div>

{% else %}

  <div class="overflow-auto rounded-xl border border-yellow-200/30">
    <table class="min-w-full text-sm">
      <thead class="thead-gold">
        <tr>
          <th class="py-2 pl-3">ID</th>
          <th>Cliente</th>
          <th>Artículo</th>
          <th>Monto</th>
          <th>Vence</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-stone-800/40 bg-black/40">
        {% for r in rows %}
        <tr>
          <td class="py-2 pl-3">{{ r.id }}</td>
          <td>{{ r.customer_name }}</td>
          <td>{{ r.item_name }}</td>
          <td>${{ '%.2f'|format(r.amount) }}</td>
          <td class="text-red-400 font-semibold">{{ r.due_date }}</td>
        </tr>
        {% endfor %}
        {% if not rows %}
        <tr><td colspan="5" class="p-3 text-center text-yellow-200/60">Sin empeños en riesgo</td></tr>
        {% endif %}
      </tbody>
    </table>
  </div>

{% endif %}
</div>
"""

@app.route("/reportes")
@login_required
def reports():

    kind = request.args.get("kind", "intereses")
    dfrom = request.args.get("from", date.today().replace(day=1).isoformat())
    dto = request.args.get("to", date.today().isoformat())

    rows = []
    total = 0.0

    with closing(get_db()) as conn:

        if kind == "intereses":
            rows = conn.execute("""
                SELECT loan_id, paid_at, amount, type
                FROM payments
                WHERE type='INTERES'
                  AND paid_at BETWEEN ? AND ?
                ORDER BY paid_at DESC
            """, (dfrom+" 00:00:00", dto+" 23:59:59")).fetchall()
            total = sum(r["amount"] for r in rows)

        elif kind == "capital":
            rows = conn.execute("""
                SELECT loan_id, paid_at, amount, type
                FROM payments
                WHERE type='ABONO'
                  AND paid_at BETWEEN ? AND ?
                ORDER BY paid_at DESC
            """, (dfrom+" 00:00:00", dto+" 23:59:59")).fetchall()
            total = sum(r["amount"] for r in rows)

        else:  # EMPEÑOS EN RIESGO
            today = date.today().isoformat()
            rows = conn.execute("""
                SELECT id, customer_name, item_name, amount, due_date
                FROM loans
                WHERE status='ACTIVO'
                  AND due_date BETWEEN ? AND date(?, '+7 day')
                ORDER BY due_date
            """, (today, today)).fetchall()

    return render_page(
        render_template_string(
            REPORTS_TPL,
            rows=rows,
            total=total,
            kind=kind,
            dfrom=dfrom,
            dto=dto
        ),
        title="Reportes",
        active="reports"
    )

# ==============================
# CONFIGURACIÓN BÁSICA (SIMPLE)
# ==============================

SETTINGS_TPL = """
<h2 class="text-xl font-bold text-yellow-300 mb-4">Configuración</h2>

<form method="post" class="glass rounded-2xl p-5 space-y-4 max-w-xl">

  <div>
    <label class="text-xs text-yellow-200/70">Interés mensual por defecto (%)</label>
    <input name="default_interest_rate" value="{{ ir }}"
           class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
  </div>

  <div>
    <label class="text-xs text-yellow-200/70">Días del empeño</label>
    <input name="default_term_days" value="{{ td }}"
           class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
  </div>

  <div>
    <label class="text-xs text-yellow-200/70">Días de renovación</label>
    <input name="renew_days" value="{{ renew_days }}"
           class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
  </div>

  <button class="gold-gradient text-stone-900 font-semibold px-6 py-3 rounded-xl">
    Guardar configuración
  </button>

</form>
"""

@app.route("/config", methods=["GET","POST"])
def settings_page():

    if request.method == "POST":
        set_setting("default_interest_rate", request.form.get("default_interest_rate","20"))
        set_setting("default_term_days", request.form.get("default_term_days","90"))
        set_setting("renew_days", request.form.get("renew_days","30"))
        return redirect(url_for("settings_page"))

    ir = get_setting("default_interest_rate","20")
    td = get_setting("default_term_days","90")
    renew_days = get_setting("renew_days","30")

    return render_page(
        render_template_string(
            SETTINGS_TPL,
            ir=ir,
            td=td,
            renew_days=renew_days
        ),
        title="Configuración",
        active="settings"
    )


# ==============================
# INVENTARIO — EMPEÑOS PERDIDOS
# ==============================

INVENTORY_TPL = """
<h2 class="text-xl font-bold text-yellow-300 mb-4">
  📦 Inventario — Empeños perdidos
</h2>

<div class="space-y-4">

{% for r in rows %}
  <div class="glass p-4 rounded-2xl border border-yellow-200/20">

    <div class="text-lg font-semibold text-yellow-200">
      {{ r.item_name }}
    </div>

    <div class="text-sm text-stone-400 mt-1">
      Empeño #{{ r.id }} · {{ r.customer_name }} ·
      ${{ "%.2f"|format(r.amount) }}
    </div>

    <div class="mt-2 text-red-400 font-semibold">
      Estado: PERDIDO
    </div>

    <form method="post"
          action="{{ url_for('inventory_sell', loan_id=r.id) }}"
          class="mt-4 flex gap-2">

      <input type="number" step="0.01" name="price" required
             placeholder="Precio de venta"
             class="flex-1 rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>

      <button class="px-4 py-2 rounded-xl bg-emerald-600 text-black font-semibold">
        💰 Vender
      </button>

    </form>

  </div>
{% endfor %}

{% if not rows %}
  <div class="text-center text-yellow-200/70 py-10">
    No hay empeños perdidos
  </div>
{% endif %}

</div>
"""

@app.route("/inventario")
def inventory():
    with closing(get_db()) as conn:
        rows = conn.execute("""
            SELECT
                id,
                item_name,
                customer_name,
                amount
            FROM loans
            WHERE status = 'PERDIDO'
            ORDER BY id DESC
        """).fetchall()

    return render_page(
        render_template_string(INVENTORY_TPL, rows=rows),
        title="Inventario",
        active="inventory"
    )


@app.route("/inventario/vender/<int:loan_id>", methods=["POST"])
def inventory_sell(loan_id:int):

    price = float(request.form.get("price", 0) or 0)
    if price <= 0:
        return redirect(url_for("inventory"))

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with closing(get_db()) as conn:

        loan = conn.execute(
            "SELECT item_name FROM loans WHERE id=?",
            (loan_id,)
        ).fetchone()

        if not loan:
            return redirect(url_for("inventory"))

        # 1️⃣ Marcar empeño como vendido
        conn.execute(
            "UPDATE loans SET status='VENDIDO' WHERE id=?",
            (loan_id,)
        )

        # 2️⃣ Registrar dinero en CAJA
        conn.execute("""
            INSERT INTO cash_movements(when_at, concept, amount, ref)
            VALUES (?,?,?,?)
        """, (
            now,
            f"Venta empeño perdido #{loan_id} - {loan['item_name']}",
            price,
            "VENTA"
        ))

        conn.commit()

    return redirect(url_for("inventory"))


# ====== VENTAS ======
SALES_TPL = """
<h2 class="text-xl font-bold text-yellow-300 mb-3">
  Ventas — Artículos en venta
</h2>

<section class="grid md:grid-cols-3 gap-6">

  <!-- AGREGAR A VENTA -->
  <div class="glass rounded-2xl p-4">
    <h3 class="text-lg font-bold text-yellow-300 mb-2">Agregar a venta</h3>
    <form method="post" action="{{ url_for('sales_add') }}" class="space-y-2">
      <input name="item_desc" placeholder="Descripción del artículo" required
        class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>

      <input name="price" type="number" step="0.01" placeholder="Precio" required
        class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>

      <button class="gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl">
        Guardar
      </button>
    </form>
  </div>

  <!-- TABLA -->
  <div class="md:col-span-2 glass rounded-2xl p-4">
    <div class="overflow-auto rounded-xl border border-yellow-200/30">
      <table class="min-w-full text-sm">
        <thead class="thead-gold">
          <tr>
            <th class="py-2 pl-3">ID</th>
            <th>Descripción</th>
            <th>Precio</th>
            <th>Status</th>
            <th>Vendido</th>
            <th class="text-right pr-3">Acciones</th>
          </tr>
        </thead>
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
              <form method="post"
                    action="{{ url_for('sales_mark_sold', sale_id=s.id) }}"
                    class="inline">
                <button class="px-2 py-1 bg-emerald-700 rounded">
                  Marcar vendido
                </button>
              </form>
              {% endif %}
              <a href="{{ url_for('sales_confirm_delete', sale_id=s.id) }}"
                 class="px-2 py-1 bg-red-700 rounded">
                Eliminar
              </a>
            </td>
          </tr>
          {% endfor %}
          {% if not rows %}
          <tr>
            <td class="py-2 pl-3" colspan="6">Sin artículos</td>
          </tr>
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- ========================= -->
  <!-- HISTORIAL DE VENTAS -->
  <!-- ========================= -->
  <div class="md:col-span-3 glass rounded-2xl p-5">
    <h3 class="text-lg font-bold text-yellow-300 mb-4">
      📊 Historial de Ventas
    </h3>

    <div class="grid md:grid-cols-2 gap-4">
      <div class="bg-black/40 rounded-xl p-4 text-center border border-yellow-200/20">
        <div class="text-sm text-yellow-200/70">Artículos vendidos</div>
        <div class="text-3xl font-extrabold text-yellow-300">
          {{ total_vendidos }}
        </div>
      </div>

      <div class="bg-black/40 rounded-xl p-4 text-center border border-yellow-200/20">
        <div class="text-sm text-yellow-200/70">Total vendido</div>
        <div class="text-3xl font-extrabold text-emerald-400">
          ${{ '%.2f'|format(total_monto) }}
        </div>
      </div>
    </div>
  </div>

</section>
"""

@app.route("/ventas")
@login_required
def sales_page():
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT * FROM sales ORDER BY id DESC LIMIT 500"
        ).fetchall()

        resumen = conn.execute("""
            SELECT 
                COUNT(*) AS total_vendidos,
                IFNULL(SUM(price), 0) AS total_monto
            FROM sales
            WHERE status='VENDIDO'
        """).fetchone()

    return render_page(
        render_template_string(
            SALES_TPL,
            rows=rows,
            total_vendidos=resumen["total_vendidos"],
            total_monto=resumen["total_monto"]
        ),
        title="Ventas",
        active="sales"
    )


@app.route("/ventas/nuevo", methods=["POST"])
@login_required
def sales_add():
    desc = request.form.get("item_desc","").strip()
    price = float(request.form.get("price",0) or 0)
    if not desc or price <= 0:
        return redirect(url_for("sales_page"))

    with closing(get_db()) as conn:
        conn.execute(
            "INSERT INTO sales(item_desc, price, status) VALUES (?,?,?)",
            (desc, price, "EN_VENTA")
        )
        conn.commit()

    return redirect(url_for("sales_page"))


@app.route("/ventas/vender/<int:sale_id>", methods=["POST"])
@login_required
def sales_mark_sold(sale_id:int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with closing(get_db()) as conn:
        conn.execute(
            "UPDATE sales SET status='VENDIDO', sold_at=? WHERE id=?",
            (now, sale_id)
        )
        conn.commit()
    return redirect(url_for("sales_page"))


@app.route("/ventas/confirm/<int:sale_id>")
@login_required
def sales_confirm_delete(sale_id:int):
    with closing(get_db()) as conn:
        item = conn.execute(
            "SELECT * FROM sales WHERE id=?",
            (sale_id,)
        ).fetchone()
    if not item:
        return "No encontrado", 404

    return render_page(
        render_template_string(CONFIRM_DELETE_SALE_TPL, item=item, error=None),
        title="Eliminar venta",
        active="sales"
    )


@app.route("/ventas/delete/<int:sale_id>", methods=["POST"])
@login_required
def sales_delete(sale_id:int):
    password = request.form.get("password","")
    with closing(get_db()) as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE id=?",
            (session.get("uid"),)
        ).fetchone()

        item = conn.execute(
            "SELECT * FROM sales WHERE id=?",
            (sale_id,)
        ).fetchone()

        if not user or not check_password_hash(user["pass_hash"], password):
            return render_page(
                render_template_string(
                    CONFIRM_DELETE_SALE_TPL,
                    item=item,
                    error="Contraseña incorrecta"
                ),
                title="Eliminar venta",
                active="sales"
            )

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

# ====== USUARIOS ======
USERS_TPL = """
<h2 class="text-xl font-bold text-yellow-300 mb-4">
  Usuarios y permisos
</h2>

<section class="grid md:grid-cols-3 gap-6">

  <!-- CREAR USUARIO -->
  <div class="glass rounded-2xl p-4">
    <h3 class="text-lg font-bold text-yellow-300 mb-3">
      ➕ Crear usuario
    </h3>

    <form method="post" action="{{ url_for('users_create') }}" class="space-y-3">
      <input name="name" placeholder="Nombre completo" required
        class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>

      <input name="username" placeholder="Usuario" required
        class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>

      <input name="password" type="password" placeholder="Contraseña" required
        class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>

      <select name="role"
        class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2">
        <option value="staff">Empleado</option>
        <option value="admin">Administrador</option>
      </select>

      <button class="gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl w-full">
        Crear usuario
      </button>
    </form>
  </div>

  <!-- LISTA DE USUARIOS -->
  <div class="md:col-span-2 glass rounded-2xl p-4">
    <h3 class="text-lg font-bold text-yellow-300 mb-3">
      👥 Usuarios existentes
    </h3>

    <div class="overflow-auto rounded-xl border border-yellow-200/30">
      <table class="min-w-full text-sm">
        <thead class="thead-gold">
  <tr>
    <th class="py-2 pl-3">ID</th>
    <th>Nombre</th>
    <th>Usuario</th>
    <th>Rol</th>
    <th class="text-center">Acciones</th>
  </tr>
</thead>

<tbody class="divide-y divide-stone-800/40 bg-black/40">
  {% for u in users %}
  <tr>
    <td class="py-2 pl-3">{{ u.id }}</td>
    <td>{{ u.name }}</td>
    <td>{{ u.username }}</td>
    <td class="font-semibold">{{ u.role }}</td>
    <td class="text-center">
      {% if session.get('role') == 'admin' and session.get('user_id', -1) != u.id %}
      <form method="post"
            action="{{ url_for('users_delete', user_id=u.id) }}"
            onsubmit="return confirm('¿Seguro que deseas eliminar este usuario?');">
        <button
          class="bg-red-600 hover:bg-red-700 text-white px-3 py-1 rounded-lg text-xs font-bold">
          🗑 Eliminar
        </button>
      </form>
      {% else %}
        —
      {% endif %}
    </td>
  </tr>
  {% endfor %}

  {% if not users %}
  <tr>
    <td colspan="5" class="py-3 pl-3">No hay usuarios</td>
  </tr>
  {% endif %}
</tbody>

      </table>
    </div>
  </div>

</section>
"""

@app.route("/usuarios")
@login_required
def users_page():
    from contextlib import closing

    with closing(get_db()) as conn:
        users = conn.execute(
            "SELECT id, name, username, role FROM users ORDER BY id ASC"
        ).fetchall()

    return render_page(
    render_template_string(
        USERS_TPL,
        users=users,
        session=session
    ),
    title="Usuarios",
    active="users"
)




@app.route("/usuarios/crear", methods=["POST"])
@login_required
def users_create():
    from contextlib import closing
    from datetime import datetime
    from werkzeug.security import generate_password_hash

    name = request.form.get("name","").strip()
    username = request.form.get("username","").strip()
    password = request.form.get("password","")
    role = request.form.get("role","staff")

    if not name or not username or not password:
        return redirect(url_for("users_page"))

    pass_hash = generate_password_hash(password)

    with closing(get_db()) as conn:
        exists = conn.execute(
            "SELECT id FROM users WHERE username=?",
            (username,)
        ).fetchone()

        if exists:
            return redirect(url_for("users_page"))


        # =========================
        # INSERT CORREGIDO (created_at)
        # =========================
        conn.execute("""
            INSERT INTO users (
                name,
                username,
                pass_hash,
                role,
                created_at
            )
            VALUES (?,?,?,?,?)
        """, (
            name,
            username,
            pass_hash,
            role,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))

        conn.commit()

@app.route("/usuarios/eliminar/<int:user_id>", methods=["POST"])
@login_required
def users_delete(user_id):

    from contextlib import closing

    # 🔐 Solo admin
    if session.get("role") != "admin":
        return "Acceso denegado", 403

    with closing(get_db()) as conn:

        # 🔎 Usuario a eliminar
        target = conn.execute(
            "SELECT username FROM users WHERE id=?",
            (user_id,)
        ).fetchone()

        if not target:
            return redirect(url_for("users_page"))

        # 🚫 No permitir borrarse a sí mismo
        if session.get("username") == target["username"]:
            return redirect(url_for("users_page"))

        conn.execute(
            "DELETE FROM users WHERE id=?",
            (user_id,)
        )
        conn.commit()

    return redirect(url_for("users_page"))



# ====== Facturación (simple): abrir ticket por # de empeño ======
FACT_TPL = """
<div class="max-w-xl mx-auto glass p-6 rounded-2xl">
  <h2 class="text-xl font-bold text-yellow-300 mb-3">Facturación</h2>

  <form method="get" class="grid md:grid-cols-3 gap-2">
    <div class="md:col-span-2">
      <label class="text-xs"># de Empeño</label>
      <input name="loan_id" type="number" min="1"
             placeholder="Ej. 101"
             class="w-full rounded-xl border border-yellow-200/30 bg-black/40 p-2"/>
    </div>
    <div class="flex items-end">
      <button class="gold-gradient text-stone-900 font-semibold px-4 py-2 rounded-xl">
        Abrir Ticket / Recibo
      </button>
    </div>
  </form>

  <div class="mt-4 text-sm text-yellow-200/80">
    Tip: también puedes entrar desde Empeños ➜ “Recibo”.
  </div>

  <h3 class="text-lg font-semibold mt-5 mb-2">Empeños recientes</h3>

  <div class="overflow-auto rounded-xl border border-yellow-200/30">
    <table class="min-w-full text-sm">
      <thead class="thead-gold">
        <tr>
          <th class="py-2 pl-3">#</th>
          <th>Cliente</th>
          <th>Artículo</th>
          <th>Monto</th>
          <th>Acción</th>
        </tr>
      </thead>

      <tbody class="divide-y divide-stone-800/40 bg-black/40">
        {% for r in rows %}
          <tr>
            <td class="py-2 pl-3">{{ r.id }}</td>
            <td>{{ r.customer_name }}</td>
            <td>{{ r.item_name }}</td>
            <td>${{ '%.2f'|format(r.amount or 0) }}</td>
            <td>
              <a href="{{ url_for('loan_ticket', loan_id=r.id) }}"
                 class="px-2 py-1 border border-yellow-200/40 rounded text-yellow-300 hover:bg-yellow-300 hover:text-black transition">
                Abrir Ticket
              </a>
            </td>
          </tr>
        {% endfor %}

        {% if not rows %}
          <tr>
            <td class="py-2 pl-3 text-center text-yellow-200/60" colspan="5">
              Sin datos
            </td>
          </tr>
        {% endif %}
      </tbody>
    </table>
  </div>
</div>
"""
@app.route("/empenos/legal/upload/<side>/<int:loan_id>", methods=["POST"])
@login_required
def upload_legal_id(side, loan_id):

    from pathlib import Path
    from werkzeug.utils import secure_filename

    file = request.files.get("file")
    if not file:
        return redirect(url_for("empeno_legal_view", loan_id=loan_id))

    upload_dir = Path("uploads/legal")
    upload_dir.mkdir(parents=True, exist_ok=True)

    fname = secure_filename(file.filename)
    final = upload_dir / f"{side}_{loan_id}_{fname}"
    file.save(final)

    col = "id_front_path" if side == "front" else "id_back_path"

    with get_db() as conn:
        conn.execute(
            f"UPDATE loans SET {col}=? WHERE id=?",
            (f"/uploads/legal/{final.name}", loan_id)
        )
        conn.commit()

    # 👇 ESTO ES LO QUE ACTIVA EL MODO VIEW
    return redirect(url_for("empeno_legal_view", loan_id=loan_id))
@app.route("/empenos/legal/view/<int:loan_id>", methods=["GET","POST"])
@login_required
def empeno_legal_view(loan_id):

    from contextlib import closing
    from pathlib import Path
    import base64, uuid

    upload_dir = Path("uploads/legal")
    upload_dir.mkdir(parents=True, exist_ok=True)

    with closing(get_db()) as conn:
        row = conn.execute(
            "SELECT * FROM loans WHERE id=?",
            (loan_id,)
        ).fetchone()

    if not row:
        return "No encontrado", 404

    # ========= GUARDAR FIRMA =========
    if request.method == "POST" and request.form.get("signature_data"):
        raw = request.form["signature_data"]
        img = base64.b64decode(raw.split(",")[1])

        fname = f"signature_{loan_id}_{uuid.uuid4().hex}.png"
        (upload_dir / fname).write_bytes(img)

        with closing(get_db()) as conn:
            conn.execute(
                "UPDATE loans SET signature_path=? WHERE id=?",
                (f"/uploads/legal/{fname}", loan_id)
            )
            conn.commit()

        return redirect(url_for("empeno_legal_view", loan_id=loan_id))

    id_front = row["id_front_path"]
    id_back = row["id_back_path"]
    signature = row["signature_path"]

    body = f"""
<style>
@media print {{
  button, form, nav, footer {{ display:none !important; }}
  body {{ background:white; }}
}}

.legal {{
  max-width:1000px;
  margin:40px auto;
  background:#ffffff;
  padding:40px;
  border-radius:20px;
  box-shadow:0 20px 60px rgba(0,0,0,.25);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto;
  color:#0f172a;
}}

.section {{ margin-top:30px; }}

.grid {{
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:30px;
}}

.box {{
  background:#f8fafc;
  padding:16px;
  border-radius:14px;
  text-align:center;
}}

.box img {{
  width:100%;
  max-height:320px;
  object-fit:contain;
  border-radius:12px;
}}

canvas {{
  width:100%;
  height:220px;
  background:white;
  border:2px dashed #16a34a;
  border-radius:14px;
  touch-action:none;
}}

.btn {{
  background:#16a34a;
  color:white;
  padding:12px 26px;
  border-radius:999px;
  border:none;
  font-weight:900;
  cursor:pointer;
}}
</style>

<div class="legal">

<h2>📜 Contrato Legal de Empeño</h2>

<p><b>Cliente:</b> {row["customer_name"]}</p>
<p><b>Monto entregado:</b> USD$ {row["amount"]:.2f}</p>
<p><b>Fecha:</b> {row["created_at"][:10]}</p>

<hr>

<div class="section">
<b>DECLARACIÓN LEGAL</b>
<p>
El cliente <b>{row["customer_name"]}</b> declara de manera libre, voluntaria e
irrevocable que <b>ENTREGA UNA PRENDA EN GARANTÍA</b> a favor de
<b>WORLD JEWELRY</b>.
</p>
<p>
La prenda queda bajo custodia de WORLD JEWELRY hasta el pago total del capital,
intereses y cargos aplicables.
</p>
<p>
En caso de incumplimiento, el cliente autoriza expresamente a WORLD JEWELRY a
disponer de la prenda conforme a la ley vigente.
</p>
</div>

<hr>

<div class="section">
<b>🪪 Identificación del Cliente</b>

<div class="grid">

<div class="box">
<b>ID (Frente)</b><br><br>
{f"<img src='{id_front}'>" if id_front else f'''
<form method="post" action="/empenos/legal/upload/front/{loan_id}" enctype="multipart/form-data">
<input type="file" name="file" accept="image/*" required><br><br>
<button class="btn">Subir ID Frente</button>
</form>
'''}
</div>

<div class="box">
<b>ID (Atrás)</b><br><br>
{f"<img src='{id_back}'>" if id_back else f'''
<form method="post" action="/empenos/legal/upload/back/{loan_id}" enctype="multipart/form-data">
<input type="file" name="file" accept="image/*" required><br><br>
<button class="btn">Subir ID Atrás</button>
</form>
'''}
</div>

</div>
</div>

<hr>

<div class="section">
<b>✍️ Firma del Cliente</b><br><br>

{f"<img src='{signature}' style='max-width:420px'>" if signature else '''
<form method="post">
<canvas id="sig"></canvas>
<input type="hidden" name="signature_data" id="sigdata"><br><br>
<button type="submit" class="btn" onclick="saveSig()">Guardar firma</button>
</form>
'''}
</div>

<br>
<button class="btn" onclick="window.print()">🖨️ Imprimir contrato</button>

</div>

<script>
const canvas = document.getElementById("sig");
if (canvas) {{
  const ctx = canvas.getContext("2d");

  function resize() {{
    const r = canvas.getBoundingClientRect();
    canvas.width = r.width;
    canvas.height = r.height;
    ctx.lineWidth = 2;
    ctx.lineCap = "round";
    ctx.strokeStyle = "#000";
  }}
  resize();
  window.addEventListener("resize", resize);

  let drawing = false;

  function pos(e) {{
    const r = canvas.getBoundingClientRect();
    const p = e.touches ? e.touches[0] : e;
    return {{ x: p.clientX - r.left, y: p.clientY - r.top }};
  }}

  function start(e) {{
    drawing = true;
    const p = pos(e);
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
  }}

  function draw(e) {{
    if (!drawing) return;
    e.preventDefault();
    const p = pos(e);
    ctx.lineTo(p.x, p.y);
    ctx.stroke();
  }}

  function end() {{ drawing = false; }}

  canvas.addEventListener("mousedown", start);
  canvas.addEventListener("mousemove", draw);
  canvas.addEventListener("mouseup", end);
  canvas.addEventListener("touchstart", start, {{passive:false}});
  canvas.addEventListener("touchmove", draw, {{passive:false}});
  canvas.addEventListener("touchend", end);
}}

function saveSig() {{
  document.getElementById("sigdata").value =
    document.getElementById("sig").toDataURL("image/png");
}}
</script>
"""

    return render_page(body, title="Documento Legal", active="loans")




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
    return redirect(url_for("empenos_index"))

# ==============================
# 🔐 RESET TOTAL DEL SISTEMA (CLAVE 0219)
# ==============================
@app.route("/system/reset", methods=["GET", "POST"])
@login_required
def system_reset():

    from contextlib import closing
    import shutil

    ERROR = ""
    OK = ""

    if request.method == "POST":
        password = request.form.get("password", "").strip()

        if password != "0219":
            ERROR = "❌ Clave incorrecta"
        else:
            # ===== LIMPIAR BASE DE DATOS =====
            with closing(get_db()) as conn:
                cur = conn.cursor()

                cur.execute("DELETE FROM payments")
                cur.execute("DELETE FROM loans")
                cur.execute("DELETE FROM cash_movements")

                # Reiniciar IDs
                cur.execute("""
                    DELETE FROM sqlite_sequence
                    WHERE name IN ('payments','loans','cash_movements')
                """)

                conn.commit()

            # ===== LIMPIAR UPLOADS =====
            try:
                if UPLOAD_DIR.exists():
                    shutil.rmtree(UPLOAD_DIR)
                    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                print("Error limpiando uploads:", e)

            OK = "✅ SISTEMA LIMPIO — Todo fue borrado correctamente"

    return render_template_string("""
    <div style="max-width:420px;margin:60px auto;
                background:#111;color:#fff;
                padding:30px;border-radius:16px;
                font-family:Arial;text-align:center">

      <h2 style="color:#facc15">⚠️ RESET TOTAL DEL SISTEMA</h2>

      <p style="font-size:14px;color:#ccc">
        Esta acción elimina TODOS los empeños, pagos y caja.<br>
        <b>NO se puede deshacer.</b>
      </p>

      {% if ERROR %}
        <div style="background:#7f1d1d;padding:10px;border-radius:8px;margin-bottom:12px">
          {{ ERROR }}
        </div>
      {% endif %}

      {% if OK %}
        <div style="background:#14532d;padding:12px;border-radius:8px;margin-bottom:12px">
          {{ OK }}
        </div>
        <a href="/" style="color:#facc15">Volver al inicio</a>
      {% else %}
        <form method="post">
          <input type="password" name="password"
                 placeholder="Clave de seguridad"
                 style="width:100%;padding:10px;
                        border-radius:8px;border:none;
                        margin-bottom:14px">

          <button style="width:100%;padding:12px;
                         background:#dc2626;
                         color:white;font-weight:800;
                         border:none;border-radius:10px">
            🔥 BORRAR TODO
          </button>
        </form>
      {% endif %}
    </div>
    """, ERROR=ERROR, OK=OK)


    
    
if __name__ == "__main__":
    # SOLO PARA DESARROLLO LOCAL
    import os, time, threading, webbrowser

    def _open():
        time.sleep(1.0)
        webbrowser.open("http://127.0.0.1:5010")

    if os.environ.get("RENDER") is None:
        threading.Thread(target=_open, daemon=True).start()

    print("=== Iniciando World Jewelry en local ===")
    app.run(host="0.0.0.0", port=5010, debug=False)
















































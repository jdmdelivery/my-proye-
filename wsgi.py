# wsgi.py — Wrapper robusto para Render/Gunicorn
from __future__ import annotations
import sys, os, glob, importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 1) Intento normal: importar "app" (app.py en el CWD)
try:
    from app import app as application
    app = application
except Exception:
    # 2) Si falla, buscar app.py en el repo y cargarlo por ruta absoluta
    matches = glob.glob(str(HERE / "**" / "app.py"), recursive=True)
    if not matches:
        raise ModuleNotFoundError("No se encontró app.py en el repositorio.")
    app_path = matches[0]
    spec = importlib.util.spec_from_file_location("app_module", app_path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    if not hasattr(mod, "app"):
        raise AttributeError("El archivo app.py encontrado no define 'app = Flask(__name__)'.")
    app = getattr(mod, "app")

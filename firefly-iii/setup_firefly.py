# Setup one-shot: crea categorias y presupuestos en Firefly III via API.
# Ajustar PRESUPUESTOS a tu propia estructura de presupuesto mensual.
import os, requests
from pathlib import Path

env = {}
for line in Path(__file__).with_name("token.env").read_text().splitlines():
    if "=" in line:
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()

BASE = env["FIREFLY_URL"] + "/api/v1"
H = {"Authorization": f"Bearer {env['FIREFLY_TOKEN']}", "Accept": "application/json"}

def post(path, payload):
    r = requests.post(f"{BASE}/{path}", headers=H, json=payload)
    ok = r.status_code in (200, 201)
    print(f"  {'OK' if ok else 'ERR'} {path} {payload.get('name','')}: {r.status_code}"
          + ("" if ok else f" {r.text[:200]}"))
    return r.json() if ok else None

# Categorias finas (iguales a CATEGORIES en finanzas_bpd.py)
CATEGORIAS = ["Transporte", "Alimentación", "Supermercado", "Entretenimiento",
              "Suscripciones", "Salud", "Servicios", "Compras", "Educación",
              "Gasolina", "Otros"]

print("=== Categorias ===")
for c in CATEGORIAS:
    post("categories", {"name": c})

# Presupuestos (envelope mensual). Ejemplo generico: reemplaza por tu propia
# estructura y montos antes de correr este script.
PRESUPUESTOS = {
    "Vivienda y familia": 10000,
    "Deudas y compromisos fijos": 3000,
    "Educacion": 6000,
    "Gym": 3000,
    "Telefono y servicios": 1200,
    "Transporte publico": 500,
    "Cuidado personal": 1000,
    "Suscripciones": 3500,
    "Seguro y comisiones banco": 550,
    "Transporte (rideshare)": 5500,
    "Comida fuera y salidas": 6000,
    "Compras": 3500,
    "Salud y farmacia": 2000,
    "Fondo de pareja/familia": 5000,
    "Supermercado": 2500,
    "Otros y gasolina": 1000,
}

# Mes objetivo del limite (auto-budget: se reinicia cada mes por el mismo monto)
MES_INICIO = "2026-01-01"
MES_FIN = "2026-01-31"

print("=== Presupuestos ===")
budget_ids = {}
for nombre, monto in PRESUPUESTOS.items():
    data = post("budgets", {"name": nombre, "active": True})
    if data:
        bid = data["data"]["id"]
        budget_ids[nombre] = bid
        post(f"budgets/{bid}/limits", {
            "start": MES_INICIO, "end": MES_FIN,
            "amount": str(monto), "currency_code": "DOP",
        })

print("\nIDs de presupuestos:", budget_ids)

#!/usr/bin/env python3
"""
Sincroniza consumos de la tarjeta BPD 5916 hacia Firefly III via API, para que
el presupuesto se descuente solo. Idempotente: guarda en firefly_synced.json
que ya se subio, asi correrlo de nuevo (o desde finanzas_bpd.py) no duplica.

USO:
  python sync_firefly.py transacciones.json          # una corrida puntual
  python sync_firefly.py transacciones_5meses.json   # carga historica

Tambien se puede importar: from sync_firefly import push_to_firefly
"""
import hashlib
import json
import sys
from pathlib import Path

import requests

HERE = Path(__file__).parent
ESTADO_FILE = HERE / "firefly_synced.json"

_env = {}
for _line in (HERE / "token.env").read_text().splitlines():
    if "=" in _line:
        _k, _, _v = _line.partition("=")
        _env[_k.strip()] = _v.strip()

BASE = _env["FIREFLY_URL"] + "/api/v1"
H = {"Authorization": f"Bearer {_env['FIREFLY_TOKEN']}", "Accept": "application/json"}
FX = 59.33

CUENTA_TARJETA_ID = "2"  # BPD Tarjeta 5916

# categoria (finanzas_bpd.py) -> presupuesto (envelope) por defecto
CATEGORIA_A_PRESUPUESTO = {
    "Transporte":      "Transporte (Uber)",
    "Alimentación":    "Comida fuera y salidas",
    "Supermercado":    "Supermercado",
    "Entretenimiento": "Fondo pareja",
    "Suscripciones":   "Suscripciones",
    "Salud":           "Salud y farmacia",
    "Servicios":       "Telefono y servicios",
    "Compras":         "Compras",
    "Educación":       "Universidad UNIR",
    "Gasolina":        "Otros y gasolina",
    "Otros":           "Otros y gasolina",
}

# comercios recurrentes que rompen la regla general de su categoria
MERCHANT_OVERRIDES = [
    (["SHAPE N SHAKE-CR"], "Gym"),
    (["BROTHERHOOD"], "Barberia"),
]


def _budget_para(categoria: str, comercio: str) -> str:
    upper = comercio.upper()
    for keywords, presupuesto in MERCHANT_OVERRIDES:
        if any(k in upper for k in keywords):
            return presupuesto
    return CATEGORIA_A_PRESUPUESTO.get(categoria, "Otros y gasolina")


def _fetch_name_to_id(recurso: str) -> dict:
    r = requests.get(f"{BASE}/{recurso}?limit=200", headers=H)
    r.raise_for_status()
    return {item["attributes"]["name"]: item["id"] for item in r.json()["data"]}


def _tx_id(tx: dict) -> str:
    """ID estable para deduplicar: el _id de Gmail si existe, si no un hash del contenido."""
    if tx.get("_id"):
        return tx["_id"]
    raw = f"{tx['fecha']}|{tx['comercio']}|{tx['monto']}|{tx['moneda']}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def push_to_firefly(transactions: list[dict]) -> int:
    """Sube las transacciones nuevas (Aprobada) a Firefly. Retorna cuantas subio."""
    sincronizadas = json.loads(ESTADO_FILE.read_text()) if ESTADO_FILE.exists() else {}

    budgets = _fetch_name_to_id("budgets")
    categories = _fetch_name_to_id("categories")

    subidas = 0
    for tx in transactions:
        if tx.get("estatus") != "Aprobada":
            continue
        tid = _tx_id(tx)
        if tid in sincronizadas:
            continue

        monto_dop = tx["monto"] * FX if tx["moneda"] == "US$" else tx["monto"]
        categoria = tx.get("categoria", "Otros")
        presupuesto = _budget_para(categoria, tx["comercio"])

        payload = {
            "error_if_duplicate_hash": False,
            "transactions": [{
                "type": "withdrawal",
                "date": tx["fecha"],
                "amount": f"{monto_dop:.2f}",
                "description": tx["comercio"],
                "source_id": CUENTA_TARJETA_ID,
                "destination_name": tx["comercio"],
                "category_id": categories.get(categoria),
                "budget_id": budgets.get(presupuesto),
                "external_id": tid,
                "notes": f"moneda original: {tx['moneda']} {tx['monto']}" if tx["moneda"] == "US$" else None,
            }],
        }
        resp = requests.post(f"{BASE}/transactions", headers=H, json=payload)
        if resp.status_code in (200, 201):
            sincronizadas[tid] = {"fecha": tx["fecha"], "comercio": tx["comercio"], "monto": monto_dop}
            subidas += 1
        else:
            print(f"  ERROR {tx['fecha']} {tx['comercio']} {monto_dop:.2f}: {resp.status_code} {resp.text[:200]}")

    ESTADO_FILE.write_text(json.dumps(sincronizadas, ensure_ascii=False, indent=1))
    return subidas


def main():
    if len(sys.argv) != 2:
        print("Uso: python sync_firefly.py <archivo.json>")
        sys.exit(1)
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    transacciones = list(data.values()) if isinstance(data, dict) else data
    print(f"Cargando {len(transacciones)} transacciones desde {sys.argv[1]}...")
    n = push_to_firefly(transacciones)
    print(f"Subidas a Firefly: {n} nuevas (el resto ya estaba sincronizado o Rechazada).")


if __name__ == "__main__":
    main()

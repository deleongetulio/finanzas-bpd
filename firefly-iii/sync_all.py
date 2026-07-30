#!/usr/bin/env python3
"""
Sincronizacion completa BPD -> Firefly III, 100% via correo (sin login al banco):
  - Notificacion de Consumo            -> Withdrawal desde Tarjeta 5916
  - Notificacion de Retiro             -> Transfer Cuenta 654 -> Efectivo
  - Pagos al Instante (enviada)        -> Transfer/Withdrawal segun beneficiario
  - Notificacion Deposito de Nomina    -> Deposit a Cuenta 654 (monto ESTIMADO,
                                          el correo no trae el monto; confirmar
                                          con estado de cuenta cuando puedas)

Lo que NO cubre (sin notificacion por correo, requiere CSV manual de vez en
cuando): comisiones bancarias, impuesto DGII, Bancaseguro, pago de la tarjeta
de credito (PagoTC). Son montos chicos e infrecuentes.

Idempotente: usa el message-id de Gmail como external_id en Firefly, correrlo
de nuevo no duplica nada.

USO:
  python sync_all.py                 # sincroniza los ultimos 45 dias
  python sync_all.py --dias 90       # rango mas amplio
"""
import argparse
import email
import email.policy
import hashlib
import html
import imaplib
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from finanzas_bpd import categorize_transactions, keyword_categorize  # noqa: E402

HERE = Path(__file__).parent
ESTADO_FILE = HERE / "firefly_synced.json"
FX = 59.33

# IDs de cuentas Firefly propias (ver en la UI: Cuentas -> el numero en la URL)
CUENTA_ID = "1"
TARJETA_ID = "2"
EFECTIVO_ID = "115"

# Solo se usa si Firefly no tiene NINGUN deposito de nomina previo del que
# partir; ajustar a tu salario aproximado o leerlo de una variable de entorno.
SALARIO_FALLBACK = float(os.environ.get("SALARIO_FALLBACK", "0"))

CATEGORIA_A_PRESUPUESTO = {
    "Transporte": "Transporte", "Alimentación": "Comida fuera y salidas",
    "Supermercado": "Supermercado", "Entretenimiento": "Entretenimiento",
    "Suscripciones": "Suscripciones", "Salud": "Salud y farmacia",
    "Servicios": "Servicios", "Compras": "Compras",
    "Educación": "Educación", "Gasolina": "Otros y gasolina",
    "Otros": "Otros y gasolina",
}
# Ejemplo: comercios recurrentes que rompen la regla general de su categoria
MERCHANT_OVERRIDES = [(["EJEMPLO GYM"], "Gym")]

# Ejemplo: beneficiarios de transferencia recurrentes con presupuesto/cuenta
# destino propios (una suscripcion, un pago fijo a un tercero, etc).
# Cada entrada: (patron en el nombre del beneficiario, tipo, destino).
#   tipo="transfer" -> destino es el ID de otra cuenta Firefly propia.
#   tipo="withdrawal" -> destino es el nombre del presupuesto (budget_name).
BENEFICIARIO_OVERRIDES: list[tuple[list[str], str, str]] = [
    # (["MI OTRA CUENTA"], "transfer", "116"),
    # (["PAGO FIJO RECURRENTE"], "withdrawal", "Nombre del presupuesto"),
]


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _html_a_texto(h: str) -> str:
    """Conversion cruda HTML->texto para los correos que no traen text/plain
    (Pagos al Instante, Deposito de Nomina): quita <style>/<script> enteros,
    agrega saltos de linea donde estaban los <tr>/<td>/<p>/<br> y quita el
    resto de las etiquetas."""
    h = re.sub(r"(?is)<style.*?</style>", "", h)
    h = re.sub(r"(?is)<script.*?</script>", "", h)
    h = re.sub(r"(?i)</(tr|td|p|div|br)\s*>", "\n", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    return html.unescape(h)


def _cuerpo_texto(msg) -> str:
    plano = "".join(part.get_content() for part in msg.walk()
                     if part.get_content_type() == "text/plain")
    if plano.strip():
        return plano
    htmls = "".join(part.get_content() for part in msg.walk()
                     if part.get_content_type() == "text/html")
    return _html_a_texto(htmls)


def _tx_id_consumo(fecha: str, comercio: str, monto: float, moneda: str) -> str:
    """MISMO esquema de hash que uso sync_firefly.py (fetch_5meses.py no traia
    Message-ID), para no duplicar los 493 consumos ya cargados en la carga
    historica."""
    raw = f"{fecha}|{comercio}|{monto}|{moneda}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _budget_para(categoria: str, comercio: str) -> str | None:
    upper = comercio.upper()
    for kws, presupuesto in MERCHANT_OVERRIDES:
        if any(k in upper for k in kws):
            return presupuesto
    return CATEGORIA_A_PRESUPUESTO.get(categoria)


def _cargar_env():
    """Lee FIREFLY_URL/FIREFLY_TOKEN de token.env. GMAIL_USER y
    GMAIL_APP_PASSWORD se esperan en el entorno (variables de sistema o
    exportadas antes de correr el script)."""
    env = {}
    for line in (HERE / "token.env").read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def _imap_connect():
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"])
    status, folders = M.list()
    allmail = next(f.decode().split(' "/" ')[-1].strip('"') for f in folders if "\\All" in f.decode())
    M.select(f'"{allmail}"', readonly=True)
    return M


def _fetch_todos_bpd(M, desde: date) -> list[tuple[str, str, str]]:
    """
    Trae TODOS los correos de notificaciones@popularenlinea.com desde `desde` y
    los devuelve como (msg_id, subject, cuerpo_texto_plano). Clasificar por
    asunto se hace despues, en Python (evita el problema de IMAP SEARCH con
    tildes/UTF-8, que imaplib no soporta bien).
    """
    fecha_txt = desde.strftime("%d-%b-%Y")
    status, data = M.search(None, f'(FROM "notificaciones@popularenlinea.com" SINCE {fecha_txt})')
    out = []
    for num in data[0].split():
        status, msgdata = M.fetch(num, "(RFC822)")
        msg = email.message_from_bytes(msgdata[0][1], policy=email.policy.default)
        subj = msg.get("Subject", "")
        msg_id_hdr = msg.get("Message-ID", num.decode())
        body = _cuerpo_texto(msg)
        out.append((msg_id_hdr, subj, body))
    return out


def _clasificar(subject: str) -> str | None:
    s = _strip_accents(subject).lower()
    if "notificacion de consumo" in s:
        return "consumo"
    if "notificacion de retiro" in s:
        return "retiro"
    if "pagos al instante" in s and "enviada" in s:
        return "transferencia"
    if "deposito de nomina" in s:
        return "nomina"
    return None


PATRON_TABLA = re.compile(
    r"(RD\$|US\$)([\d,.]+)\s+(Peso dominicano|D[oó]lar estadounidense)\s+"
    r"(\d{2}/\d{2}/\d{4})\s+([\s\S]+?)\s+(Aprobada|Rechazada|Pendiente)"
)
PATRON_PAGOS_INSTANTE = re.compile(
    r"Beneficiario:\s*([^\n]+?)\s*\n\s*Cuenta o Producto[:\s]*[^\n]*\s*"
    r"Monto:\s*(RD\$|US\$)\s*([\d,.]+)\s*Fecha:\s*(\d{1,2}/\d{1,2}/\d{4})"
)
PATRON_NOMINA = re.compile(r"en fecha\s*(\d{8})\s*.*?deposito por concepto de pago de nomina")


def _monto_dop(monto: float, simbolo: str) -> float:
    return monto * FX if simbolo == "US$" else monto


AJUSTE_SALARIO_FILE = HERE / "ajuste_salario.json"


def _ultimo_salario_conocido(env) -> float:
    """
    El correo de 'Deposito de Nomina' NUNCA trae el monto (solo la fecha), asi
    que no hay forma de leerlo de ahi. En su lugar, usamos el ULTIMO deposito
    de nomina que ya esta en Firefly (el mas reciente que Getulio haya
    confirmado/corregido a mano en la UI) como mejor estimado para el
    siguiente. Si el salario cambia (ej. por un prestamo con descuento de
    nomina), basta con corregir UNA vez el monto de ese deposito en Firefly
    y los siguientes ya salen con el numero correcto, sin tocar el codigo.

    Si existe ajuste_salario.json (un ajuste puntual que Getulio ya sabe que
    viene, ej. un descuento nuevo de nomina), ese monto tiene prioridad UNA
    sola vez para el siguiente deposito estimado; el archivo se borra despues
    de usarse para no seguir aplicandolo en los meses siguientes.
    """
    if AJUSTE_SALARIO_FILE.exists():
        import json as _json
        info = _json.loads(AJUSTE_SALARIO_FILE.read_text(encoding="utf-8"))
        AJUSTE_SALARIO_FILE.unlink()
        print(f"  Usando ajuste puntual de salario: RD$ {info['monto']:,.2f} ({info.get('nota', '')})")
        return float(info["monto"])
    try:
        r = requests.get(f"{env['FIREFLY_URL']}/api/v1/transactions",
                          headers={"Authorization": f"Bearer {env['FIREFLY_TOKEN']}", "Accept": "application/json"},
                          params={"type": "deposit", "limit": 50})
        r.raise_for_status()
        # OJO: la cuenta tambien recibe otros creditos (regalos, transferencias
        # entre cuentas propias, etc). Solo cuentan los depositos de SALARIO:
        # description empieza con "Salario" (asi se crean siempre; si se
        # corrige el monto en la UI, hay que conservar esa palabra al inicio
        # para que se siga reconociendo).
        depositos = [
            (t["attributes"]["transactions"][0]["date"][:10], float(t["attributes"]["transactions"][0]["amount"]))
            for t in r.json()["data"]
            if t["attributes"]["transactions"][0].get("destination_id") == CUENTA_ID
            and t["attributes"]["transactions"][0].get("description", "").startswith("Salario")
        ]
        if depositos:
            return max(depositos, key=lambda x: x[0])[1]
    except (requests.RequestException, KeyError, ValueError):
        pass
    return SALARIO_FALLBACK


def _post_firefly(env, payload) -> bool:
    r = requests.post(f"{env['FIREFLY_URL']}/api/v1/transactions",
                       headers={"Authorization": f"Bearer {env['FIREFLY_TOKEN']}", "Accept": "application/json"},
                       json=payload)
    ok = r.status_code in (200, 201)
    if not ok:
        desc = payload["transactions"][0]["description"]
        print(f"  ERROR {desc}: {r.status_code} {r.text[:200]}")
    return ok


def sync(dias: int = 45):
    env = _cargar_env()
    sincronizadas = {}
    if ESTADO_FILE.exists():
        import json
        sincronizadas = json.loads(ESTADO_FILE.read_text())

    desde = date.today() - timedelta(days=dias)
    print(f"Conectando a Gmail (IMAP)... buscando desde {desde}")
    M = _imap_connect()

    print("Bajando correos de notificaciones@popularenlinea.com...")
    todos = _fetch_todos_bpd(M, desde)
    por_tipo: dict[str, list] = {"consumo": [], "retiro": [], "transferencia": [], "nomina": []}
    for msg_id, subj, body in todos:
        tipo = _clasificar(subj)
        if tipo:
            por_tipo[tipo].append((msg_id, subj, body))
    print(f"  {len(todos)} correos totales -> "
          f"{len(por_tipo['consumo'])} consumo, {len(por_tipo['retiro'])} retiro, "
          f"{len(por_tipo['transferencia'])} transferencia, {len(por_tipo['nomina'])} nomina")

    # ── Consumos (tarjeta 5916) ──────────────────────────────────────────
    # OJO: la clave de dedup es el hash fecha|comercio|monto|moneda (no el
    # Message-ID) para que calce con lo que ya subio la carga historica.
    nuevas_tx = []
    for msg_id, subj, body in por_tipo["consumo"]:
        m = PATRON_TABLA.search(body)
        if not m:
            continue
        merchant_raw = re.sub(r"\s+", " ", m.group(5)).strip()
        parts = merchant_raw.split("*")
        merchant = parts[-1].strip() if len(parts) >= 2 else merchant_raw
        monto = float(m.group(2).replace(",", ""))
        fecha = datetime.strptime(m.group(4), "%d/%m/%Y").strftime("%Y-%m-%d")
        tid = _tx_id_consumo(fecha, merchant, monto, m.group(1))
        if tid in sincronizadas:
            continue
        nuevas_tx.append({
            "_id": tid, "monto": monto, "moneda": m.group(1),
            "fecha": fecha, "comercio": merchant, "estatus": m.group(6), "categoria": "Otros",
        })
    print(f"  {len(nuevas_tx)} consumos nuevos por categorizar")
    if nuevas_tx:
        categorize_transactions(nuevas_tx)

    subidos_consumo = 0
    for tx in nuevas_tx:
        if tx["estatus"] != "Aprobada":
            sincronizadas[tx["_id"]] = {"tipo": "consumo_rechazado"}
            continue
        monto_dop = _monto_dop(tx["monto"], tx["moneda"])
        presupuesto = _budget_para(tx["categoria"], tx["comercio"])
        ok = _post_firefly(env, {"error_if_duplicate_hash": False, "transactions": [{
            "type": "withdrawal", "date": tx["fecha"], "amount": f"{monto_dop:.2f}",
            "description": tx["comercio"], "source_id": TARJETA_ID,
            "destination_name": tx["comercio"], "category_name": tx["categoria"],
            "budget_name": presupuesto, "external_id": tx["_id"],
        }]})
        if ok:
            sincronizadas[tx["_id"]] = {"tipo": "consumo", "comercio": tx["comercio"], "monto": monto_dop}
            subidos_consumo += 1

    # ── Retiros de cajero ────────────────────────────────────────────────
    subidos_retiro = 0
    for msg_id, subj, body in por_tipo["retiro"]:
        if msg_id in sincronizadas:
            continue
        m = PATRON_TABLA.search(body)
        if not m or m.group(6) != "Aprobada":
            sincronizadas[msg_id] = {"tipo": "retiro_omitido"}
            continue
        monto = float(m.group(2).replace(",", ""))
        monto_dop = _monto_dop(monto, m.group(1))
        fecha = datetime.strptime(m.group(4), "%d/%m/%Y").strftime("%Y-%m-%d")
        cajero = re.sub(r"\s+", " ", m.group(5)).strip()
        ok = _post_firefly(env, {"error_if_duplicate_hash": False, "transactions": [{
            "type": "transfer", "date": fecha, "amount": f"{monto_dop:.2f}",
            "description": f"Retiro cajero: {cajero}", "source_id": CUENTA_ID,
            "destination_id": EFECTIVO_ID, "external_id": msg_id,
        }]})
        if ok:
            sincronizadas[msg_id] = {"tipo": "retiro", "monto": monto_dop}
            subidos_retiro += 1

    # ── Transferencias salientes (Pagos al Instante) ────────────────────
    subidos_transfer = 0
    for msg_id, subj, body in por_tipo["transferencia"]:
        if msg_id in sincronizadas:
            continue
        m = PATRON_PAGOS_INSTANTE.search(body)
        if not m:
            sincronizadas[msg_id] = {"tipo": "pagos_instante_sin_parsear"}
            continue
        beneficiario = m.group(1).strip()
        monto = float(m.group(3).replace(",", ""))
        monto_dop = _monto_dop(monto, m.group(2))
        fecha = datetime.strptime(m.group(4), "%d/%m/%Y").strftime("%Y-%m-%d") \
            if len(m.group(4).split("/")[0]) == 2 else datetime.strptime(m.group(4), "%d/%m/%Y").strftime("%Y-%m-%d")
        ben_upper = beneficiario.upper()

        override = next(
            ((tipo, destino) for patrones, tipo, destino in BENEFICIARIO_OVERRIDES
             if any(p in ben_upper for p in patrones)),
            None,
        )
        if override and override[0] == "transfer":
            payload = {"transactions": [{
                "type": "transfer", "date": fecha, "amount": f"{monto_dop:.2f}",
                "description": f"Transferencia a {beneficiario}", "source_id": CUENTA_ID,
                "destination_id": override[1], "external_id": msg_id,
            }]}
        elif override and override[0] == "withdrawal":
            payload = {"transactions": [{
                "type": "withdrawal", "date": fecha, "amount": f"{monto_dop:.2f}",
                "description": f"Transferencia a {beneficiario}", "source_id": CUENTA_ID,
                "destination_name": beneficiario, "budget_name": override[1], "external_id": msg_id,
            }]}
        else:
            # beneficiario desconocido: se sube SIN presupuesto para que quede
            # visible como "sin categorizar" en Firefly y se revise a mano.
            payload = {"transactions": [{
                "type": "withdrawal", "date": fecha, "amount": f"{monto_dop:.2f}",
                "description": f"Transferencia a {beneficiario} (SIN IDENTIFICAR)",
                "source_id": CUENTA_ID, "destination_name": beneficiario,
                "external_id": msg_id,
            }]}
        payload["error_if_duplicate_hash"] = False
        ok = _post_firefly(env, payload)
        if ok:
            sincronizadas[msg_id] = {"tipo": "transferencia", "beneficiario": beneficiario, "monto": monto_dop}
            subidos_transfer += 1

    # ── Deposito de nomina (monto estimado, el correo no lo trae) ───────
    # El estimado se toma del ULTIMO deposito ya confirmado en Firefly (no de
    # una constante fija), asi que si Getulio corrige el monto una vez en la
    # UI (ej. porque baja por un prestamo con descuento de nomina), los
    # siguientes depositos estimados salen ya con el numero correcto.
    subidos_nomina = 0
    salario_base = None
    for msg_id, subj, body in por_tipo["nomina"]:
        if msg_id in sincronizadas:
            continue
        texto = _strip_accents(body)
        m = PATRON_NOMINA.search(texto)
        if not m:
            sincronizadas[msg_id] = {"tipo": "nomina_sin_parsear"}
            continue
        if salario_base is None:
            salario_base = _ultimo_salario_conocido(env)
        fecha = datetime.strptime(m.group(1), "%Y%m%d").strftime("%Y-%m-%d")
        ok = _post_firefly(env, {"error_if_duplicate_hash": False, "transactions": [{
            "type": "deposit", "date": fecha, "amount": f"{salario_base:.2f}",
            "description": "Salario (MONTO ESTIMADO - confirmar y corregir con estado de cuenta)",
            "source_name": "Empleador", "destination_id": CUENTA_ID,
            "external_id": msg_id,
        }]})
        if ok:
            sincronizadas[msg_id] = {"tipo": "nomina_estimada", "monto": salario_base}
            subidos_nomina += 1

    M.logout()

    import json
    ESTADO_FILE.write_text(json.dumps(sincronizadas, ensure_ascii=False, indent=1))

    print(f"\nResumen: {subidos_consumo} consumos, {subidos_retiro} retiros, "
          f"{subidos_transfer} transferencias, {subidos_nomina} depositos de nomina (estimados).")
    if subidos_nomina:
        print(f"  OJO: el deposito de nomina se subio con el ULTIMO monto conocido "
              f"({salario_base:,.2f}) porque el correo de BPD no trae la cifra. Confirma "
              f"y corrige en Firefly si cambio.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dias", type=int, default=45)
    args = parser.parse_args()
    sync(args.dias)

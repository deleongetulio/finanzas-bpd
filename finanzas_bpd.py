#!/usr/bin/env python3
"""
Análisis de Finanzas Personales — Banco Popular Dominicano
Lee notificaciones de consumo de Gmail, categoriza con Claude y genera dashboard HTML.

SETUP (solo la primera vez):
  1. pip install -r requirements.txt
  2. En Google Cloud Console:
       - Habilitar Gmail API
       - Crear credenciales OAuth2 (tipo "Aplicación de escritorio")
       - Descargar el JSON y guardarlo como client_secret.json en este directorio
  3. Asegúrate de que ANTHROPIC_API_KEY esté en tu entorno

USO:
  python finanzas_bpd.py --desde 2026-01-01 --hasta 2026-05-15
  python finanzas_bpd.py --desde 2026-01-01 --hasta 2026-05-15 --output mayo.html


USO:
  python finanzas_bpd.py --desde 2026-01-01 --hasta 2026-05-15
  python finanzas_bpd.py --desde 2026-01-01 --hasta 2026-05-15 --maps-key AIza...
"""

import argparse
import base64
import json
import os
import re
import time
import webbrowser
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from anthropic import Anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Configuración ─────────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE    = Path(__file__).parent / "token.json"
CLIENT_SECRET = Path(__file__).parent / "client_secret.json"
CONFIG_FILE   = Path(__file__).parent / "config.json"
CACHE_FILE    = Path(__file__).parent / "transacciones.json"

SENDER  = "notificaciones@popularenlinea.com"
SUBJECT = "Notificación de Consumo"

# Correos que se manda el atajo de iOS (trigger Transaction/Wallet) en cada tap
# de Apple Pay. Formato esperado del asunto:
#   ApplePay | <monto> | <comercio> | <tarjeta>
APPLEPAY_SUBJECT = "ApplePay"

# Tasa de cambio por defecto para convertir US$ a RD$ en los totales del
# dashboard (se puede fijar en config.json con "fx_usd_dop")
DEFAULT_FX_USD_DOP = 59.33


def monto_dop(tx: dict, fx: float) -> float:
    """Monto de la transacción expresado en RD$."""
    return tx["monto"] * fx if tx.get("moneda") == "US$" else tx["monto"]


def load_config() -> dict:
    """Lee config.json si existe."""
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return {}


def load_cache() -> dict:
    """Carga el cache de transacciones procesadas (indexado por Gmail message ID)."""
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

CATEGORIES = [
    "Transporte",
    "Alimentación",
    "Supermercado",
    "Entretenimiento",
    "Suscripciones",
    "Salud",
    "Servicios",
    "Compras",
    "Educación",
    "Gasolina",
    "Otros",
]

COLORS = {
    "Transporte":     "#3B82F6",
    "Alimentación":   "#F59E0B",
    "Supermercado":   "#10B981",
    "Entretenimiento":"#8B5CF6",
    "Suscripciones":  "#0EA5E9",
    "Salud":          "#EF4444",
    "Servicios":      "#6B7280",
    "Compras":        "#EC4899",
    "Educación":      "#14B8A6",
    "Gasolina":       "#F97316",
    "Otros":          "#9CA3AF",
}

# ── Capa 1: keywords (rápido, gratis, determinístico) ─────────────────────────
# Cada tupla: (lista de palabras clave, categoría)
# Se compara en MAYÚSCULAS contra el nombre del comercio
KEYWORD_RULES: list[tuple[list[str], str]] = [
    # Transporte
    # OJO: sin "METRO" a secas (matcheaba "ALISS METRO PZA", el mall); el metro
    # de SD se paga con tarjeta fisica recargable y no aparece en estos consumos
    (["UBER", "CABIFY", "TAXI", "PARKING", "PARK", "TRANSIT"], "Transporte"),
    # Suscripciones digitales
    (["NETFLIX", "SPOTIFY", "APPLE.COM", "APPLE SERVICES", "ANTHROPIC",
      "YOUTUBE", "YOUTUBE PREMIUM", "DISNEY", "HBO", "MAX ", "AMAZON PRIME",
      "PRIME VIDEO", "CRUNCHYROLL", "PARAMOUNT", "TWITCH", "DEEZER",
      "GOOGLE ONE", "GOOGLE STORAGE", "ICLOUD", "MICROSOFT 365",
      "OFFICE 365", "ADOBE", "DROPBOX", "CANVA",
      "OBSIDIAN", "BITWARDEN", "TUNEMYMUSI", "PADDLE"], "Suscripciones"),
    # Supermercados
    (["LA SIRENA", "JUMBO", "BRAVO", "PRICESMART", "NACIONAL",
      "SUPERMERCADO", "SUPERMAX", "IBERIA"], "Supermercado"),
    # Gasolina
    (["SHELL", "TEXACO", "PUMA", "ESSO", "GASOLINERA",
      "COMBUSTIBLE", "TOTAL GAS"], "Gasolina"),
    # Servicios básicos
    (["EDEESTE", "EDENORTE", "EDESUR", "CLARO", "ALTICE", "WIND",
      "TRICOM", "AGUA", "CAASD", "CORAAPLATA"], "Servicios"),
    # Salud
    (["SHAPE", "GYM", "FITNESS", "CAROL", "FARMACIA", "DROGUERIA",
      "CLINICA", "HOSPITAL", "MEDICO", "LABORATORIO", "DENTAL",
      "DOCTOR", "FARMA", "SALUD"], "Salud"),
    # Entretenimiento
    (["CINEMA", "CINE", "MOVIE", "PALACIO DEL CINE", "CCPLAZA",
      "TEATRO", "BOWLING", "KARTING"], "Entretenimiento"),
    # Alimentación (incluye comercios locales verificados 2026-07-19)
    (["MCDONALD", "BURGER", "KFC", "WENDY", "PIZZA", "SUBWAY",
      "DOMINO", "POLLO", "RESTAURANT", "CAFE", "COFFEE",
      "STARBUCKS", "DUNKIN", "TACO",
      "LINCOLN ROAD", "MARACA", "NOCCIOLA", "CHICKEN ROOMING",
      "ROLLING STEAK", "ASADERO", "MIGUELINA", "SQUARE ONE",
      "PEDIDOSYA", "PedidosYa".upper(), "CHIMI", "FOOD TRUCK",
      "CASA DE LA CANA", "PROPINA"], "Alimentación"),
    # Compras (ALISS = tienda por depto; BM CARGO = courier de compras online)
    (["AMAZON", "IKEA", "ZARA", "H&M", "NIKE", "ADIDAS",
      "APPLE STORE", "BESTBUY", "EBAY",
      "ALISS", "BM CARGO", "MINISO", "TEMU", "ZENNI"], "Compras"),
    # Educación
    (["UNIVERSIDAD", "INTEC", "PUCMM", "UASD", "UNIBE",
      "UDEMY", "COURSERA", "PLATZI", "DUOLINGO"], "Educación"),
]


def keyword_categorize(merchant: str) -> str | None:
    """Retorna categoría si hay match por keyword, None si no hay."""
    upper = merchant.upper()
    for keywords, category in KEYWORD_RULES:
        if any(kw in upper for kw in keywords):
            return category
    return None

# ── Gmail ─────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET.exists():
                raise FileNotFoundError(
                    f"\nNo se encontró {CLIENT_SECRET}.\n"
                    "Descarga las credenciales OAuth desde Google Cloud Console y\n"
                    "guárdalas como client_secret.json en este directorio."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds)


def _get_message_ids(service, desde: date, hasta: date) -> list[str]:
    """Retorna los IDs de Gmail que coinciden con el rango de fechas."""
    hasta_query = (hasta + timedelta(days=1)).strftime("%Y/%m/%d")
    query = (
        f"from:{SENDER} "
        f'subject:"{SUBJECT}" '
        f"after:{desde.strftime('%Y/%m/%d')} "
        f"before:{hasta_query}"
    )
    print(f"  Query: {query}")

    ids = []
    page_token = None
    while True:
        kwargs: dict = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        ids.extend(ref["id"] for ref in result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return ids


def fetch_transactions(service, desde: date, hasta: date, cache: dict) -> tuple[list[dict], list[str]]:
    """
    Retorna (todas_las_transacciones, ids_nuevos).
    - Las transacciones en cache se devuelven tal cual (ya categorizadas).
    - Los ids_nuevos son los que aún no están en cache y necesitan procesarse.
    """
    all_ids = _get_message_ids(service, desde, hasta)

    cached_txs = []
    new_ids = []
    for msg_id in all_ids:
        if msg_id in cache:
            cached_txs.append(cache[msg_id])
        else:
            new_ids.append(msg_id)

    print(f"  {len(cached_txs)} del cache, {len(new_ids)} nuevos")

    # Descargar y parsear solo los nuevos
    new_txs = []
    for msg_id in new_ids:
        msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        tx = _parse_message(msg)
        if tx:
            tx["_id"] = msg_id   # guardar el ID para indexar en cache después
            new_txs.append(tx)

    return cached_txs + new_txs, new_ids


def _extract_body(payload: dict) -> str:
    """Extrae el texto plano del payload (puede ser multipart)."""
    body = ""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            body += base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    for part in payload.get("parts", []):
        body += _extract_body(part)
    return body


def _parse_message(msg: dict) -> dict | None:
    body = _extract_body(msg.get("payload", {}))

    # Soporta RD$ y US$, y ambas monedas en texto
    pattern = (
        r"(RD\$|US\$)([\d,.]+)\s+"
        r"(Peso dominicano|Dólar estadounidense)\s+"
        r"(\d{2}/\d{2}/\d{4})\s+"
        r"([\s\S]+?)\s+"
        r"(Aprobada|Rechazada|Pendiente)"
    )
    m = re.search(pattern, body)
    if not m:
        return None

    currency_symbol = m.group(1)          # "RD$" o "US$"
    amount = float(m.group(2).replace(",", ""))
    fecha_str = m.group(4)
    merchant_raw = re.sub(r"\s+", " ", m.group(5)).strip()
    status = m.group(6)

    # BPD repite el nombre: "UBER*UBER RIDES" → tomar la parte más limpia
    parts = merchant_raw.split("*")
    merchant = parts[-1].strip() if len(parts) >= 2 else merchant_raw

    # Extraer producto de tarjeta → determinar Crédito / Débito
    card_m = re.search(r"utilizar su (.+?), terminada en (\d+)", body)
    if card_m:
        card_product = card_m.group(1).strip()   # "VISA ISI", "VISA Débito Popular"
        card = card_m.group(2)
    else:
        card_product = "N/A"
        card = re.search(r"terminada en (\d+)", body)
        card = card.group(1) if card else "N/A"

    card_type = "Débito" if re.search(r"[Dd][eé]bito", card_product) else "Crédito"

    return {
        "monto": amount,
        "moneda": currency_symbol,
        "fecha": datetime.strptime(fecha_str, "%d/%m/%Y").strftime("%Y-%m-%d"),
        "comercio": merchant,
        "estatus": status,
        "tarjeta": card,
        "tarjeta_producto": card_product,
        "tipo_tarjeta": card_type,
        "categoria": "Otros",
    }

# ── Categorización ─────────────────────────────────────────────────────────────

_PROMPT_TEMPLATE = """Eres un clasificador de gastos bancarios de República Dominicana.
Categoriza cada comercio en UNA de estas categorías: {cats}.

Criterios:
- Uber, Cabify, taxi, parking → Transporte
- Restaurantes, cafeterías, fast food, delivery → Alimentación
- La Sirena, Jumbo, Bravo, PriceSmart, supermercados → Supermercado
- Netflix, Spotify, Apple, Anthropic, YouTube, Disney, HBO → Suscripciones
- Shape N Shake, gimnasios, farmacias, médicos, laboratorios → Salud
- EDEESTE, EDENORTE, Claro, Altice, agua, internet → Servicios
- Tiendas, Amazon, ropa, electrónicos → Compras
- Universidades, Udemy, Coursera, cursos → Educación
- Shell, Texaco, Puma, Esso, gasolineras → Gasolina
- Si no encaja claramente → Otros

Comercios a categorizar:
{merchants}

IMPORTANTE: Responde ÚNICAMENTE con un JSON array de {n} strings, sin texto adicional.
Formato exacto: ["categoria1", "categoria2", ...]"""


def _parse_claude_categories(raw: str, expected: int) -> list[str] | None:
    """Intenta extraer el JSON array de la respuesta de Claude."""
    # Buscar el array más largo que tenga el número correcto de elementos
    for match in re.finditer(r"\[[\s\S]*?\]", raw):
        try:
            result = json.loads(match.group())
            if isinstance(result, list) and len(result) == expected:
                return result
        except json.JSONDecodeError:
            continue
    # Último intento: extraer líneas que parezcan categorías
    lines = [l.strip().strip('",') for l in raw.split("\n") if l.strip().strip('",') in CATEGORIES]
    if len(lines) == expected:
        return lines
    return None


def _categorize_batch(client: Anthropic, merchants: list[str]) -> list[str]:
    prompt = _PROMPT_TEMPLATE.format(
        cats=", ".join(CATEGORIES),
        merchants="\n".join(f"{i+1}. {m}" for i, m in enumerate(merchants)),
        n=len(merchants),
    )
    for attempt in range(3):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        result = _parse_claude_categories(raw, len(merchants))
        if result:
            return result
        print(f"  ⚠ Intento {attempt+1}: respuesta inesperada, reintentando...")
    print("  ✗ No se pudo categorizar este lote, usando 'Otros'")
    return ["Otros"] * len(merchants)


def categorize_transactions(transactions: list[dict]) -> list[dict]:
    if not transactions:
        return transactions

    # Capa 1: keywords (sin costo, instantáneo)
    pending = []
    for tx in transactions:
        cat = keyword_categorize(tx["comercio"])
        if cat:
            tx["categoria"] = cat
        else:
            pending.append(tx)

    kw_count = len(transactions) - len(pending)
    print(f"  Keywords: {kw_count} categorizados, {len(pending)} pasan a Claude")

    if not pending:
        return transactions

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  ⚠ Sin ANTHROPIC_API_KEY: los ambiguos quedan en 'Otros'")
        return transactions

    # Capa 2: Claude (solo los ambiguos)
    client = Anthropic()
    batch_size = 40

    for i in range(0, len(pending), batch_size):
        batch = pending[i : i + batch_size]
        merchants = [tx["comercio"] for tx in batch]
        print(f"  Claude: batch {i+1}–{i+len(batch)} de {len(pending)}...")
        cats = _categorize_batch(client, merchants)
        for j, tx in enumerate(batch):
            tx["categoria"] = cats[j] if j < len(cats) else "Otros"

    return transactions

# ── Google Places enrichment ───────────────────────────────────────────────────

# Mapeo de tipos de Google Places a nuestras categorías
_PLACES_TYPE_MAP = {
    "gym":                        "Salud",
    "health":                     "Salud",
    "pharmacy":                   "Salud",
    "hospital":                   "Salud",
    "doctor":                     "Salud",
    "dentist":                    "Salud",
    "spa":                        "Salud",
    "restaurant":                 "Alimentación",
    "food":                       "Alimentación",
    "cafe":                       "Alimentación",
    "bakery":                     "Alimentación",
    "meal_delivery":              "Alimentación",
    "meal_takeaway":              "Alimentación",
    "bar":                        "Entretenimiento",
    "night_club":                 "Entretenimiento",
    "movie_theater":              "Entretenimiento",
    "amusement_park":             "Entretenimiento",
    "bowling_alley":              "Entretenimiento",
    "supermarket":                "Supermercado",
    "grocery_or_supermarket":     "Supermercado",
    "convenience_store":          "Supermercado",
    "gas_station":                "Gasolina",
    "clothing_store":             "Compras",
    "shopping_mall":              "Compras",
    "electronics_store":          "Compras",
    "furniture_store":            "Compras",
    "home_goods_store":           "Compras",
    "book_store":                 "Compras",
    "department_store":           "Compras",
    "school":                     "Educación",
    "university":                 "Educación",
    "library":                    "Educación",
    "transit_station":            "Transporte",
    "subway_station":             "Transporte",
    "bus_station":                "Transporte",
    "taxi_stand":                 "Transporte",
    "car_rental":                 "Transporte",
    "parking":                    "Transporte",
    "electric_utility":           "Servicios",
    "post_office":                "Servicios",
    "insurance_agency":           "Servicios",
    "laundry":                    "Servicios",
    "beauty_salon":               "Servicios",
    "hair_care":                  "Servicios",
}


def enrich_with_places(transactions: list[dict], maps_key: str) -> list[dict]:
    """Para transacciones en 'Otros', busca en Google Places y re-categoriza."""
    otros = [tx for tx in transactions if tx["categoria"] == "Otros"]
    if not otros:
        return transactions

    print(f"  Enriqueciendo {len(otros)} transacciones 'Otros' con Google Places...")
    base_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"

    for tx in otros:
        query = f"{tx['comercio']} República Dominicana"
        try:
            resp = requests.get(base_url, params={"query": query, "key": maps_key}, timeout=5)
            data = resp.json()
            results = data.get("results", [])
            if results:
                place_types = results[0].get("types", [])
                for ptype in place_types:
                    if ptype in _PLACES_TYPE_MAP:
                        tx["categoria"] = _PLACES_TYPE_MAP[ptype]
                        print(f"    {tx['comercio']} → {tx['categoria']} (via Places: {ptype})")
                        break
            time.sleep(0.1)  # respetar rate limit
        except Exception as e:
            print(f"    ⚠ Places API error para '{tx['comercio']}': {e}")

    return transactions


# ── Apple Pay (atajo de iOS) ───────────────────────────────────────────────────
# El trigger "Transaction/Wallet" de Atajos se dispara en cada tap NFC de Apple
# Pay y conoce el nombre LIMPIO del comercio (el que muestra Wallet). El atajo
# se manda un correo a la misma cuenta Gmail con asunto:
#   ApplePay | <monto> | <comercio> | <tarjeta>
# Aquí cruzamos esos correos con las notificaciones del BPD (por monto y fecha,
# el último-4 de la tarjeta NO sirve: Apple Pay usa un PAN de dispositivo) y
# reemplazamos el descriptor bancario por el nombre limpio de Wallet.

def fetch_applepay_events(service, desde: date, hasta: date) -> list[dict]:
    hasta_query = (hasta + timedelta(days=1)).strftime("%Y/%m/%d")
    query = (f'subject:"{APPLEPAY_SUBJECT}" '
             f"after:{desde.strftime('%Y/%m/%d')} before:{hasta_query}")
    result = service.users().messages().list(userId="me", q=query, maxResults=500).execute()
    events = []
    for ref in result.get("messages", []):
        msg = service.users().messages().get(
            userId="me", id=ref["id"], format="metadata",
            metadataHeaders=["Subject"]).execute()
        subject = next((h["value"] for h in msg["payload"]["headers"]
                        if h["name"] == "Subject"), "")
        parts = [p.strip() for p in subject.split("|")]
        if len(parts) < 3 or not parts[0].upper().startswith("APPLEPAY"):
            continue
        monto_txt = re.sub(r"[^\d.,]", "", parts[1]).replace(",", "")
        try:
            monto = float(monto_txt)
        except ValueError:
            continue
        events.append({
            "monto": monto,
            "comercio": parts[2],
            "fecha": datetime.fromtimestamp(int(msg["internalDate"]) / 1000).date(),
        })
    return events


def enrich_with_applepay(transactions: list[dict], events: list[dict]) -> int:
    """Cruza eventos de Apple Pay con transacciones BPD por monto y fecha (±1 día)."""
    enriched = 0
    for ev in events:
        for tx in transactions:
            if tx.get("fuente") == "Apple Pay":
                continue
            if abs(tx["monto"] - ev["monto"]) > 0.01:
                continue
            tx_fecha = datetime.strptime(tx["fecha"], "%Y-%m-%d").date()
            if abs((tx_fecha - ev["fecha"]).days) > 1:
                continue
            tx["comercio_raw"] = tx["comercio"]
            tx["comercio"] = ev["comercio"]
            tx["fuente"] = "Apple Pay"
            cat = keyword_categorize(ev["comercio"])
            if cat:
                tx["categoria"] = cat
            enriched += 1
            break
    return enriched


# ── Dashboard HTML ─────────────────────────────────────────────────────────────

def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def generate_dashboard(transactions: list[dict], desde: date, hasta: date,
                       fx: float = DEFAULT_FX_USD_DOP) -> str:
    approved = [tx for tx in transactions if tx["estatus"] == "Aprobada"]
    # Todos los agregados en RD$ (los US$ se convierten con la tasa fx)
    total = sum(monto_dop(tx, fx) for tx in approved)
    n_tx = len(approved)
    avg = total / n_tx if n_tx else 0
    days = max((hasta - desde).days, 1)

    # Por categoría
    cat_totals: dict[str, float] = defaultdict(float)
    for tx in approved:
        cat_totals[tx["categoria"]] += monto_dop(tx, fx)
    cat_sorted = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)

    # Por día
    daily: dict[str, float] = defaultdict(float)
    for tx in approved:
        daily[tx["fecha"]] += monto_dop(tx, fx)
    daily_sorted = sorted(daily.items())

    # Tabla de transacciones (más recientes primero)
    tx_sorted = sorted(transactions, key=lambda x: x["fecha"], reverse=True)
    rows = "".join(
        f'<tr data-cat="{tx["categoria"]}" data-tipo="{tx.get("tipo_tarjeta","")}">'
        f'<td>{tx["fecha"]}</td>'
        f'<td>{tx["comercio"]}{" " if tx.get("fuente") == "Apple Pay" else ""}</td>'
        f'<td><span class="badge" style="background:{COLORS.get(tx["categoria"],"#9CA3AF")}">'
        f'{tx["categoria"]}</span></td>'
        f'<td class="amount">{tx.get("moneda","RD$")} {tx["monto"]:,.2f}</td>'
        f'<td><span class="badge-card {"badge-credit" if tx.get("tipo_tarjeta")=="Crédito" else "badge-debit"}">'
        f'{tx.get("tipo_tarjeta","—")} ·{tx.get("tarjeta","")}</span></td>'
        f'<td class="{"ok" if tx["estatus"]=="Aprobada" else "err"}">{tx["estatus"]}</td>'
        f'</tr>'
        for tx in tx_sorted
    )

    # Desglose categorías
    cat_rows = "".join(
        f'<tr>'
        f'<td><span class="dot" style="background:{COLORS.get(c,"#9CA3AF")}"></span>{c}</td>'
        f'<td>RD$ {a:,.2f}</td>'
        f'<td>{a/total*100:.1f}%</td>'
        f'</tr>'
        for c, a in cat_sorted
    )

    top_cat = cat_sorted[0] if cat_sorted else ("—", 0)

    # Datos para Chart.js
    pie_labels  = _j([c for c, _ in cat_sorted])
    pie_data    = _j([round(a, 2) for _, a in cat_sorted])
    pie_colors  = _j([COLORS.get(c, "#9CA3AF") for c, _ in cat_sorted])
    bar_labels  = _j([d for d, _ in daily_sorted])
    bar_data    = _j([round(a, 2) for _, a in daily_sorted])

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Finanzas BPD — {desde:%d/%m/%Y} al {hasta:%d/%m/%Y}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#F3F4F6;color:#111827}}
header{{background:#CC0000;color:#fff;padding:1.25rem 2rem}}
header h1{{font-size:1.2rem;font-weight:700}}
header small{{opacity:.8;font-size:.85rem}}
.wrap{{max-width:1200px;margin:2rem auto;padding:0 1.5rem}}
.kpis{{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem;margin-bottom:1.5rem}}
.kpi{{background:#fff;border-radius:.75rem;padding:1.25rem 1.5rem;box-shadow:0 1px 3px #0001}}
.kpi .lbl{{font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:#6B7280;margin-bottom:.3rem}}
.kpi .val{{font-size:1.65rem;font-weight:700}}
.kpi .sub{{font-size:.75rem;color:#9CA3AF;margin-top:.2rem}}
.grid2{{display:grid;grid-template-columns:1fr 2fr;gap:1rem;margin-bottom:1.5rem}}
.grid1{{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1.5rem}}
.card{{background:#fff;border-radius:.75rem;padding:1.25rem 1.5rem;box-shadow:0 1px 3px #0001}}
.card h2{{font-size:.85rem;font-weight:600;color:#374151;margin-bottom:1rem}}
.ch{{position:relative;height:260px}}
.cat-tbl{{width:100%;border-collapse:collapse;font-size:.85rem}}
.cat-tbl td{{padding:.4rem .5rem;border-bottom:1px solid #F3F4F6}}
.cat-tbl td:nth-child(2){{text-align:right;font-weight:500}}
.cat-tbl td:last-child{{text-align:right;color:#6B7280}}
.dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:.5rem}}
.badge{{display:inline-block;padding:.15rem .55rem;border-radius:999px;color:#fff;font-size:.72rem;font-weight:500}}
table.tx{{width:100%;border-collapse:collapse;font-size:.82rem}}
table.tx th{{text-align:left;padding:.55rem 1rem;background:#F9FAFB;color:#6B7280;font-size:.7rem;text-transform:uppercase;letter-spacing:.05em}}
table.tx td{{padding:.55rem 1rem;border-bottom:1px solid #F3F4F6}}
table.tx tr:hover{{background:#FAFAFA}}
.amount{{font-weight:600;text-align:right;font-variant-numeric:tabular-nums}}
.ok{{color:#059669;font-size:.78rem}}
.err{{color:#DC2626;font-size:.78rem}}
.badge-card{{display:inline-block;padding:.12rem .5rem;border-radius:999px;font-size:.7rem;font-weight:600}}
.badge-credit{{background:#EFF6FF;color:#1D4ED8}}
.badge-debit{{background:#F0FDF4;color:#15803D}}
.filters{{display:flex;gap:.75rem;margin-bottom:.9rem;flex-wrap:wrap}}
.filters input,.filters select{{padding:.45rem .9rem;border:1px solid #E5E7EB;border-radius:.5rem;font-size:.82rem;outline:none}}
.filters input:focus,.filters select:focus{{border-color:#CC0000}}
@media(max-width:768px){{.kpis,.grid2,.grid1{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<header>
  <h1>Análisis de Finanzas Personales — Banco Popular Dominicano</h1>
  <small>{desde:%d/%m/%Y} al {hasta:%d/%m/%Y} · {n_tx} transacciones aprobadas</small>
</header>

<div class="wrap">

  <div class="kpis">
    <div class="kpi">
      <div class="lbl">Total Gastado</div>
      <div class="val">RD$ {total:,.0f}</div>
      <div class="sub">{n_tx} transacciones en {days} días</div>
    </div>
    <div class="kpi">
      <div class="lbl">Promedio por Transacción</div>
      <div class="val">RD$ {avg:,.0f}</div>
      <div class="sub">~RD$ {total/days:,.0f} por día</div>
    </div>
    <div class="kpi">
      <div class="lbl">Mayor Categoría</div>
      <div class="val">{top_cat[0]}</div>
      <div class="sub">RD$ {top_cat[1]:,.0f} ({top_cat[1]/total*100:.0f}% del total)</div>
    </div>
  </div>

  <div class="grid2">
    <div class="card">
      <h2>Distribución por Categoría</h2>
      <div class="ch"><canvas id="pieChart"></canvas></div>
    </div>
    <div class="card">
      <h2>Gastos por Día</h2>
      <div class="ch"><canvas id="barChart"></canvas></div>
    </div>
  </div>

  <div class="grid2">
    <div class="card">
      <h2>Desglose por Categoría</h2>
      <table class="cat-tbl">
        <thead><tr><th>Categoría</th><th style="text-align:right">Monto</th><th style="text-align:right">%</th></tr></thead>
        <tbody>{cat_rows}</tbody>
      </table>
    </div>
    <div class="card">
      <h2>Todas las Transacciones</h2>
      <div class="filters">
        <input type="text" id="searchInput" placeholder="Buscar comercio..." oninput="filterTable()">
        <select id="catFilter" onchange="filterTable()">
          <option value="">Todas las categorías</option>
          {"".join(f'<option value="{c}">{c}</option>' for c in CATEGORIES)}
        </select>
        <select id="tipoFilter" onchange="filterTable()">
          <option value="">Crédito y Débito</option>
          <option value="Crédito">Crédito</option>
          <option value="Débito">Débito</option>
        </select>
      </div>
      <div style="overflow-x:auto;max-height:420px;overflow-y:auto">
        <table class="tx" id="txTable">
          <thead>
            <tr>
              <th>Fecha</th><th>Comercio</th><th>Categoría</th>
              <th style="text-align:right">Monto</th><th>Tarjeta</th><th>Estado</th>
            </tr>
          </thead>
          <tbody id="txBody">{rows}</tbody>
        </table>
      </div>
    </div>
  </div>

</div>

<script>
new Chart(document.getElementById('pieChart'),{{
  type:'doughnut',
  data:{{labels:{pie_labels},datasets:[{{data:{pie_data},backgroundColor:{pie_colors},borderWidth:2}}]}},
  options:{{responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{position:'bottom',labels:{{font:{{size:11}}}}}}}}
  }}
}});

new Chart(document.getElementById('barChart'),{{
  type:'bar',
  data:{{
    labels:{bar_labels},
    datasets:[{{label:'RD$',data:{bar_data},backgroundColor:'#CC000077',borderColor:'#CC0000',borderWidth:1}}]
  }},
  options:{{responsive:true,maintainAspectRatio:false,
    plugins:{{legend:{{display:false}}}},
    scales:{{
      x:{{ticks:{{font:{{size:10}},maxRotation:45}}}},
      y:{{ticks:{{callback:v=>'RD$'+v.toLocaleString('es-DO')}}}}
    }}
  }}
}});

function filterTable(){{
  const q    = document.getElementById('searchInput').value.toLowerCase();
  const cat  = document.getElementById('catFilter').value;
  const tipo = document.getElementById('tipoFilter').value;
  document.querySelectorAll('#txBody tr').forEach(row=>{{
    const text    = row.textContent.toLowerCase();
    const rowCat  = row.dataset.cat||'';
    const rowTipo = row.dataset.tipo||'';
    const ok = (!q || text.includes(q)) && (!cat || rowCat===cat) && (!tipo || rowTipo===tipo);
    row.style.display = ok ? '' : 'none';
  }});
}}
</script>
</body>
</html>"""

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Análisis de finanzas personales — Banco Popular Dominicano"
    )
    parser.add_argument("--desde", required=True, metavar="YYYY-MM-DD",
                        help="Fecha de inicio (inclusive)")
    parser.add_argument("--hasta", required=True, metavar="YYYY-MM-DD",
                        help="Fecha de fin (inclusive)")
    parser.add_argument("--output", default="dashboard.html",
                        help="Nombre del archivo HTML de salida")
    args = parser.parse_args()

    desde = datetime.strptime(args.desde, "%Y-%m-%d").date()
    hasta = datetime.strptime(args.hasta, "%Y-%m-%d").date()

    if desde > hasta:
        parser.error("--desde debe ser anterior a --hasta")

    # Leer config.json para las API keys opcionales
    config = load_config()
    maps_key = config.get("maps_key")

    # Cargar cache de transacciones ya procesadas
    cache = load_cache()

    print(f"\n📧 Conectando a Gmail...")
    service = get_gmail_service()

    print(f"📥 Descargando transacciones ({desde} → {hasta})...")
    transactions, new_ids = fetch_transactions(service, desde, hasta, cache)

    if not transactions:
        print("⚠  No se encontraron transacciones en ese período.")
        return

    # Identificar las nuevas (aún sin categoría final)
    new_txs = [tx for tx in transactions if tx.get("_id") in new_ids]

    if new_txs:
        print(f"🤖 Categorizando {len(new_txs)} transacciones nuevas con Claude...")
        categorize_transactions(new_txs)

        if maps_key:
            print("🗺  Enriqueciendo 'Otros' con Google Places...")
            enrich_with_places(new_txs, maps_key)

        # Guardar nuevas en cache (sin el campo _id temporal)
        for tx in new_txs:
            msg_id = tx.pop("_id")
            cache[msg_id] = tx
        save_cache(cache)
        print(f"  Cache actualizado ({len(cache)} transacciones guardadas)")
    else:
        print("  Todo desde cache, sin llamadas a APIs externas")

    # Limpiar _id de transacciones cacheadas que lo tengan
    for tx in transactions:
        tx.pop("_id", None)

    # Apple Pay: cruzar los correos del atajo de iOS con las transacciones
    print("🍎 Buscando correos de Apple Pay (atajo de iOS)...")
    ap_events = fetch_applepay_events(service, desde, hasta)
    if ap_events:
        enriched = enrich_with_applepay(transactions, ap_events)
        print(f"  {len(ap_events)} eventos Apple Pay, {enriched} transacciones enriquecidas")
        if enriched:
            save_cache(cache)
    else:
        print("  Sin correos de Apple Pay en el período")

    fx = float(config.get("fx_usd_dop", DEFAULT_FX_USD_DOP))
    print("📊 Generando dashboard...")
    html = generate_dashboard(transactions, desde, hasta, fx)

    output_path = Path(args.output)
    output_path.write_text(html, encoding="utf-8")
    print(f"✅ Dashboard listo: {output_path.resolve()}\n")
    webbrowser.open(output_path.resolve().as_uri())


if __name__ == "__main__":
    main()

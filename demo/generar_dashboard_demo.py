"""Genera demo/dashboard_demo.html con transacciones 100% ficticias.

No requiere credenciales de Gmail: llama directo a generate_dashboard() con
datos sinteticos. Los comercios son inventados (ninguno coincide con
KEYWORD_RULES de finanzas_bpd.py) para no revelar habitos de consumo reales.

Uso:
    python demo/generar_dashboard_demo.py
"""

import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from finanzas_bpd import generate_dashboard  # noqa: E402

random.seed(42)

COMERCIOS = {
    "Transporte":      ["ViaRapida Movilidad", "Estacionamiento Central"],
    "Alimentación":    ["Cafe Aurora", "Sabor Criollo Restaurant", "Pizzeria Buonasera"],
    "Supermercado":     ["Mercado Los Robles", "Super Familiar"],
    "Entretenimiento": ["CinePlex Metropolitano", "Bowling Zona Norte"],
    "Suscripciones":   ["StreamFlix Plus", "MusicWave Premium", "CloudBox Storage"],
    "Salud":           ["Farmacia San Rafael", "Clinica Bienestar", "GymForce Fitness"],
    "Servicios":       ["Electrica del Este", "TeleConecta Internet"],
    "Compras":         ["Tienda ModaViva", "ElectroHogar Plaza"],
    "Educación":       ["Instituto Saber Continuo", "Academia CodigoVivo"],
    "Gasolina":        ["Gasolinera El Sol", "EstacionExpress Combustibles"],
}

TARJETAS = [
    ("Crédito", "Visa Signature", "1234"),
    ("Débito", "Mastercard Classic", "5678"),
]


def generar_transacciones(n: int, desde: date, hasta: date) -> list[dict]:
    dias = (hasta - desde).days
    transacciones = []
    for _ in range(n):
        categoria = random.choice(list(COMERCIOS))
        comercio = random.choice(COMERCIOS[categoria])
        tipo_tarjeta, tarjeta_producto, tarjeta = random.choice(TARJETAS)
        fecha = desde + timedelta(days=random.randint(0, dias))
        monto = round(random.uniform(150, 4500), 2)
        estatus = "Aprobada" if random.random() > 0.05 else "Rechazada"
        transacciones.append({
            "monto": monto,
            "moneda": "RD$",
            "fecha": fecha.isoformat(),
            "comercio": comercio,
            "estatus": estatus,
            "tarjeta": tarjeta,
            "tarjeta_producto": tarjeta_producto,
            "tipo_tarjeta": tipo_tarjeta,
            "categoria": categoria,
        })
    return transacciones


def main():
    hasta = date.today()
    desde = hasta - timedelta(days=30)
    transacciones = generar_transacciones(60, desde, hasta)
    html = generate_dashboard(transacciones, desde, hasta)

    out_path = Path(__file__).parent / "dashboard_demo.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"Generado: {out_path} ({len(transacciones)} transacciones sinteticas)")


if __name__ == "__main__":
    main()

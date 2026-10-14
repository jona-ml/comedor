#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
script-bienestar
================

Cliente para la reserva de cupos de comedor de la UNSAAC
(http://bienestar.unsaac.edu.pe/, servido en HTTP plano).

Flujo:
    1. GET  /                                          -> crea PHPSESSID
    2. GET  /simple-php-captcha.php?_CAPTCHA&t=<time>  -> imagen captcha (PNG)
    3. Resolver el captcha con ddddocr
    4. POST /registro.php {codigo, pass, cap}          -> JSON
    5. Interpretar {respuesta: si|no, mensaje, alumno, nombre, fecharegistro}

Estrategia anti-colapso ("pre-calentar y disparar una sola vez"):
    - Se hace el "precalentamiento" (sesion + captcha resuelto) FUERA del pico.
    - A la hora indicada con --at se envia UN UNICO POST, reutilizando el
      captcha ya resuelto (no se vuelve a descargar durante la avalancha).
    - Solo se reintenta ante fallos de RED, con backoff exponencial.
    - Un rechazo logico ("respuesta":"no") no se reintenta.

Uso:
    python bienestar.py                     # un intento inmediato (prueba unica)
    python bienestar.py --dry-run           # solo precalienta y resuelve captcha
    python bienestar.py --at 18:00:00       # precalienta, espera y dispara a esa hora
    python bienestar.py --manual 1a2B3      # usa un captcha ingresado a mano
    python bienestar.py --wait-server       # espera a que el servidor responda

Opciones utiles:
    --attempts N    numero maximo de disparos (default 1)
    --interval S    segundos entre disparos (default 3)
    --window S      ventana total de reintentos de red por peticion (default 25)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
DEBUG_DIR = BASE_DIR / "debug"

DEFAULTS = {
    "base_url": "http://bienestar.unsaac.edu.pe",
    "captcha_url": "/simple-php-captcha.php?_CAPTCHA&t={t}",
    "registro_url": "/registro.php",
    "timeout": 20,
    "max_network_retries": 5,
    "retry_base_delay": 1.0,
}

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
}

# Frases del servidor que indican que el captcha ya no es valido (requiere
# volver a precalentar) frente a otros rechazos logicos (sin cupo, clave mala).
CAPTCHA_INVALID_MARKERS = ("captcha",)


def log(msg: str) -> None:
    print(msg, flush=True)


def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(
            f"[!] No se encontro '{path.name}'.\n"
            f"    Copia config.example.json a config.json y coloca tus datos."
        )
    cfg = dict(DEFAULTS)
    # utf-8-sig tolera el BOM que agrega PowerShell al guardar el archivo.
    cfg.update(json.loads(path.read_text(encoding="utf-8-sig")))
    for key in ("codigo", "pass"):
        value = str(cfg.get(key, ""))
        if not value or value.startswith("TU_"):
            sys.exit(f"[!] Falta configurar '{key}' en {path.name}.")
    return cfg


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    return session


def _retrying(func, *, label: str, retries: int, base_delay: float):
    """Ejecuta `func` reintentando SOLO ante errores de red, con backoff."""
    delay = base_delay
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return func()
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
            if attempt < retries:
                log(f"    [{label}] fallo de red ({exc.__class__.__name__}); "
                    f"reintento {attempt + 1}/{retries} en {delay:.0f}s")
                time.sleep(delay)
                delay *= 2
    raise last_error  # type: ignore[misc]


def server_reachable(cfg: dict, timeout: float = 8.0) -> bool:
    try:
        r = requests.get(cfg["base_url"].rstrip("/") + "/",
                         headers=BROWSER_HEADERS, timeout=timeout)
        return r.status_code < 500
    except requests.RequestException:
        return False


def wait_for_server(cfg: dict, poll: float, max_wait: float) -> None:
    log(f"[*] Esperando a que el servidor responda (cada {poll:.0f}s)...")
    start = time.time()
    while True:
        if server_reachable(cfg):
            log("    Servidor disponible.")
            return
        if max_wait and time.time() - start >= max_wait:
            sys.exit("[!] El servidor no respondio dentro de la ventana de espera.")
        log(f"    sin respuesta ({datetime.now():%H:%M:%S}); reintento en {poll:.0f}s")
        time.sleep(poll)


def prewarm(cfg: dict) -> tuple[requests.Session, str]:
    """Paso previo al pico: abre sesion, descarga y resuelve el captcha."""
    session = make_session()

    log(f"[1] Abriendo {cfg['base_url']}/ (sesion)...")
    resp = _retrying(
        lambda: session.get(cfg["base_url"].rstrip("/") + "/",
                            timeout=cfg["timeout"], allow_redirects=True),
        label="index", retries=cfg["max_network_retries"],
        base_delay=cfg["retry_base_delay"],
    )
    resp.raise_for_status()
    log(f"    HTTP {resp.status_code} | cookies: {list(session.cookies.keys())}")

    path = cfg["captcha_url"].format(t=f"{time.time():.8f}")
    url = cfg["base_url"].rstrip("/") + path
    log(f"[2] Descargando captcha: {path}")
    headers = {"Referer": cfg["base_url"].rstrip("/") + "/"}
    cap_resp = _retrying(
        lambda: session.get(url, timeout=cfg["timeout"], headers=headers),
        label="captcha", retries=cfg["max_network_retries"],
        base_delay=cfg["retry_base_delay"],
    )
    cap_resp.raise_for_status()
    data = cap_resp.content
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / "captcha.png").write_bytes(data)
    log(f"    HTTP {cap_resp.status_code} | type={cap_resp.headers.get('Content-Type', '?')} "
        f"| {len(data)} bytes -> debug/captcha.png")
    if not data:
        sys.exit(
            "[!] El captcha llego vacio (0 bytes). Suele indicar bloqueo por IP\n"
            "    o que la sesion no genero captcha. Espera y reintenta."
        )

    cap = solve_captcha(data)
    return session, cap


def solve_captcha(image_bytes: bytes) -> str:
    log("[3] Resolviendo captcha con ddddocr...")
    try:
        import ddddocr
    except ImportError:
        sys.exit("[!] Falta ddddocr. Instala: pip install -r requirements.txt")
    ocr = ddddocr.DdddOcr(show_ad=False)
    code = (ocr.classification(image_bytes) or "").strip()
    log(f"    Captcha reconocido: '{code}'")
    if not code:
        sys.exit("[!] No se pudo reconocer el captcha. Revisa debug/captcha.png "
                 "o usa --manual.")
    return code


def fire(session: requests.Session, cfg: dict, cap: str) -> dict:
    """Dispara el POST de reserva reutilizando el captcha ya resuelto."""
    url = cfg["base_url"].rstrip("/") + cfg["registro_url"]
    payload = {"codigo": str(cfg["codigo"]), "pass": str(cfg["pass"]), "cap": cap}
    log(f"[4] POST {cfg['registro_url']} (codigo={payload['codigo']}, cap={cap})")
    headers = {
        "Referer": cfg["base_url"].rstrip("/") + "/",
        "Origin": cfg["base_url"].rstrip("/"),
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    resp = _retrying(
        lambda: session.post(url, data=payload, headers=headers, timeout=cfg["timeout"]),
        label="registro", retries=cfg["max_network_retries"],
        base_delay=cfg["retry_base_delay"],
    )
    resp.raise_for_status()
    raw = resp.text.strip()
    DEBUG_DIR.mkdir(exist_ok=True)
    (DEBUG_DIR / "resultado.json").write_text(raw, encoding="utf-8")
    log(f"    HTTP {resp.status_code} | {raw[:400]}")
    try:
        return resp.json()
    except ValueError:
        sys.exit(f"[!] El servidor no devolvio JSON valido:\n{raw[:500]}")


def report(data: dict) -> int:
    respuesta = str(data.get("respuesta", "")).lower()
    mensaje = data.get("mensaje", "(sin mensaje)")
    print()
    if respuesta == "si":
        log("================ RESERVA EXITOSA ================")
        log(f"  Mensaje        : {mensaje}")
        log(f"  Codigo alumno  : {data.get('alumno', '-')}")
        log(f"  Nombre         : {data.get('nombre', '-')}")
        log(f"  Fecha y hora   : {data.get('fecharegistro', '-')}")
        log("=================================================")
        return 0
    log("================== SIN CUPO =====================")
    log(f"  Mensaje : {mensaje}")
    log("=================================================")
    return 2


def is_captcha_error(data: dict) -> bool:
    mensaje = str(data.get("mensaje", "")).lower()
    return str(data.get("respuesta", "")).lower() != "si" and \
        any(m in mensaje for m in CAPTCHA_INVALID_MARKERS)


def seconds_until(hhmmss: str) -> float:
    target = datetime.strptime(hhmmss, "%H:%M:%S").time()
    now = datetime.now()
    target_dt = datetime.combine(now.date(), target)
    if target_dt < now:
        target_dt += timedelta(days=1)
    return (target_dt - now).total_seconds()


def countdown(seconds: float) -> None:
    log(f"[*] Esperando {seconds:.0f}s hasta el disparo...")
    while seconds > 0:
        time.sleep(min(1.0, seconds))
        seconds -= 1.0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reserva de cupos de comedor UNSAAC (estrategia de disparo unico).")
    parser.add_argument("--config", default=str(BASE_DIR / "config.json"))
    parser.add_argument("--manual", metavar="CAPTCHA",
                        help="usar este captcha en lugar del OCR")
    parser.add_argument("--dry-run", action="store_true",
                        help="solo precalienta y resuelve el captcha")
    parser.add_argument("--at", metavar="HH:MM:SS",
                        help="precalienta ahora y dispara a esa hora (hoy o manana)")
    parser.add_argument("--attempts", type=int, default=1,
                        help="numero maximo de disparos (default 1)")
    parser.add_argument("--interval", type=float, default=3.0,
                        help="segundos entre disparos (default 3)")
    parser.add_argument("--wait-server", action="store_true",
                        help="esperar a que el servidor responda antes de precalentar")
    parser.add_argument("--poll", type=float, default=30.0,
                        help="segundos entre chequeos de --wait-server (default 30)")
    parser.add_argument("--wait-max", type=float, default=0.0,
                        help="tiempo maximo de espera del servidor en segundos (0 = infinito)")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))

    if args.wait_server:
        wait_for_server(cfg, args.poll, args.wait_max)

    # --- Fase de precalentamiento (fuera del pico) ---
    if args.manual:
        session = make_session()
        resp = _retrying(
            lambda: session.get(cfg["base_url"].rstrip("/") + "/",
                                timeout=cfg["timeout"]),
            label="index", retries=cfg["max_network_retries"],
            base_delay=cfg["retry_base_delay"])
        resp.raise_for_status()
        cap = args.manual
        log(f"[3] Usando captcha manual: '{cap}'")
    else:
        session, cap = prewarm(cfg)

    if args.dry_run:
        log("[*] --dry-run: precalentamiento listo, no se envia la reserva.")
        return 0

    # --- Espera hasta la hora de apertura ---
    if args.at:
        wait_s = seconds_until(args.at)
        countdown(wait_s)

    # --- Fase de disparo ---
    attempts = max(1, args.attempts)
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            log(f"    esperando {args.interval:.0f}s antes del disparo "
                f"{attempt}/{attempts}...")
            time.sleep(args.interval)

        log(f"[*] Disparo {attempt}/{attempts} a las {datetime.now():%H:%M:%S}")
        try:
            data = fire(session, cfg, cap)
        except requests.RequestException as exc:
            log(f"    fallo de red persistente: {exc.__class__.__name__}")
            continue

        if str(data.get("respuesta", "")).lower() == "si":
            return report(data)

        if is_captcha_error(data):
            log("    Captcha invalido/expirado: re-precalentando...")
            try:
                session, cap = prewarm(cfg)
            except requests.RequestException as exc:
                log(f"    no se pudo re-precalentar: {exc.__class__.__name__}")
                continue
            continue

        # Rechazo logico (sin cupo, clave incorrecta, etc.)
        return report(data)

    log("\n[!] Se agotaron los intentos sin exito (posible bloqueo de red).")
    return 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\n[!] Interrumpido por el usuario.")
        sys.exit(130)
    except requests.RequestException as exc:
        log(
            "\n[!] No se pudo conectar con el servidor.\n"
            f"    Detalle: {exc.__class__.__name__}: {exc}\n"
            "    El servidor parece estar bloqueando por IP o caido. "
            "Espera unos minutos y reintenta."
        )
        sys.exit(3)

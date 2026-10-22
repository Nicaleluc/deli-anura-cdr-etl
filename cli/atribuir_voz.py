"""
Atribuye tecnico por llamada: descarga audio, transcribe, diariza y vuelca
a A_Conversacion / Turno / R_Conversacion_Voz.

Uso:
    python -m cli.atribuir_voz [--desde YYYY-MM-DD] [--hasta YYYY-MM-DD]
                               [--limite N] [--semilla] [--lote] [--fusionar]
    python -m cli.atribuir_voz --reprocesar --desde YYYY-MM-DD --hasta YYYY-MM-DD
    python -m cli.atribuir_voz --nombrar-id 125 --nombre "Nombre Apellido"
    (requiere --nombre; renombra la voz y reconsolida Veces/Estado)
"""

import argparse
from datetime import date, timedelta

from etl.dominios.voz.conversaciones import main


def _fecha(s):
    return date.fromisoformat(s) if s else None


def _fusionar():
    from config.settings import VOZ_MODELO_VERSION
    from etl.conn.db import get_engine
    from etl.dominios.voz.enrolar import fusionar_duplicados_cliente

    fusionar_duplicados_cliente(get_engine(), VOZ_MODELO_VERSION, aplicar=True)


def _nombrar(voz_id, nombre):
    from config.settings import VOZ_MODELO_VERSION
    from etl.conn.db import get_engine
    from etl.dominios.voz.enrolar import _consolidar
    from sqlalchemy import text

    nombre = (nombre or "").strip()[:120]
    if not nombre:
        raise SystemExit("falta --nombre")
    engine = get_engine()
    try:
        with engine.begin() as conn:
            n = conn.execute(
                text("UPDATE dbo.A_Voz SET Nombre=:n, "
                     "FechaActualizacion=SYSUTCDATETIME() WHERE Id=:i"),
                {"n": nombre, "i": voz_id},
            ).rowcount
    except Exception as exc:
        raise SystemExit(f"no se pudo renombrar (nombre repetido?): {exc}")
    if not n:
        raise SystemExit(f"no existe A_Voz Id={voz_id}")
    _consolidar(engine, VOZ_MODELO_VERSION)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT Id, Nombre, EsTecnico, Estado, Veces FROM dbo.A_Voz "
                 "WHERE Id=:i"),
            {"i": voz_id},
        ).fetchone()
    print(f"nombrada: {tuple(row)}")


def _cdr_ids(ids):
    """Procesa CDRs puntuales por Id (cualquier direccion/accion grabada)."""
    from datetime import datetime

    from config.settings import (VOZ_MODELO_VERSION, VOZ_WHISPER_BEAM,
                                 VOZ_WHISPER_MODEL)
    from etl.conn.anura_client import AnuraClient
    from etl.conn.db import get_engine
    from etl.dominios.voz import modelos as M
    from etl.dominios.voz.conversaciones import (cargar_plantillas,
                                                 procesar_llamada)

    engine = get_engine()
    client = AnuraClient()
    token = client.get_access_token()
    with engine.connect() as conn:
        plantillas = cargar_plantillas(conn, VOZ_MODELO_VERSION)
    print(f"plantillas activas ({VOZ_MODELO_VERSION}): {len(plantillas)}")
    wm, spk = M.get_whisper(VOZ_WHISPER_MODEL), M.get_speaker()
    for cdr_id in ids:
        try:
            x = client.gcapi("AdminService.getCdr", [cdr_id], token)
        except Exception as exc:
            import requests as _rq

            if isinstance(exc, _rq.HTTPError) and exc.response is not None \
                    and exc.response.status_code == 401:
                token = client.get_access_token()  # sesion vencida: renovar
                try:
                    x = client.gcapi("AdminService.getCdr", [cdr_id], token)
                except Exception as exc2:
                    print(f"  cdr{cdr_id}: no visible ({type(exc2).__name__})")
                    continue
            else:
                print(f"  cdr{cdr_id}: no visible ({type(exc).__name__})")
                continue
        if not x or not x.get("wasRecorded"):
            print(f"  cdr{cdr_id}: sin grabacion")
            continue
        ts = (x.get("dialTime") or {}).get("time")
        if not ts:
            print(f"  cdr{cdr_id}: sin dialTime")
            continue
        conv = {"cdrId": cdr_id,
                "calling": str(x.get("calling") or "").strip(),
                "callingName": x.get("callingName"),
                "duration": int(x.get("duration") or 0),
                "fecha": datetime.fromtimestamp(ts / 1000.0),
                "via": x.get("direction")}
        try:
            r = procesar_llamada(client, engine, conv, plantillas, wm, spk,
                                 VOZ_WHISPER_BEAM, VOZ_WHISPER_MODEL,
                                 VOZ_MODELO_VERSION, token)
        except Exception as exc:
            import requests as _rq2

            if isinstance(exc, _rq2.HTTPError) and exc.response is not None \
                    and exc.response.status_code == 401:
                token = client.get_access_token()
                try:
                    r = procesar_llamada(client, engine, conv, plantillas, wm,
                                         spk, VOZ_WHISPER_BEAM,
                                         VOZ_WHISPER_MODEL, VOZ_MODELO_VERSION,
                                         token)
                except Exception as exc2:
                    print(f"  cdr{cdr_id}: ERROR reintento "
                          f"{type(exc2).__name__}: {str(exc2)[:80]}")
                    continue
            else:
                print(f"  cdr{cdr_id}: ERROR {type(exc).__name__}: {str(exc)[:120]}")
                continue
        print(f"  cdr{cdr_id}: {r['estado']} tec={r.get('tecnico') or '?'} "
              f"turnos={r.get('turnos')}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Voz Anura -> A_Conversacion")
    ap.add_argument("--desde", default=None, help="YYYY-MM-DD (default: hoy-7)")
    ap.add_argument("--hasta", default=None, help="YYYY-MM-DD (default: hoy)")
    ap.add_argument("--limite", type=int, default=None,
                    help="max conversaciones por corrida (default: setting)")
    ap.add_argument("--semilla", action="store_true",
                    help="upsert de voiceprints a A_Voz antes de procesar")
    ap.add_argument("--forzar-seed", action="store_true",
                    help="con --semilla: pisa tambien las plantillas que la BD tiene "
                         "con mas clips que el pack (por defecto se saltan)")
    ap.add_argument("--lote", action="store_true",
                    help="batch nocturno: corre dia por dia (reanudable, idempotente)")
    ap.add_argument("--fusionar", action="store_true",
                    help="fusiona candidatos-tecnico que en realidad eran el cliente "
                         "(solo, o al final de --lote)")
    ap.add_argument("--reconciliar", action="store_true",
                    help="re-apunta 'Sin identificar' a plantillas en banda ALTA "
                         "(dry-run salvo --aplicar; la banda MEDIA solo se reporta)")
    ap.add_argument("--reprocesar", action="store_true",
                    help="re-atribuye conversaciones YA cargadas con las plantillas "
                         "actuales (ej. voz enrolada despues): solo rellena grupos "
                         "con VozId NULL, sin re-descargar ni re-transcribir; nunca "
                         "degrada ni pisa una atribucion existente. Combinar con "
                         "--desde/--hasta (sin --limite no tiene tope)")
    ap.add_argument("--bautizar", action="store_true",
                    help="bautiza voces CLIENTE sin nombre con el nombre dicho "
                         "desde su telefono (regla de bautismo: telefono con historia + "
                         "voz coincidente + nombre valido sin colision). "
                         "Nunca toca tecnicos. Es dry-run salvo --aplicar")
    ap.add_argument("--aplicar", action="store_true",
                    help="con --bautizar: escribe los bautismos (sin esto es dry-run)")
    ap.add_argument("--cdr-id", type=int, nargs="+", default=None,
                    help="procesa CDRs puntuales por Id (ej. piernas OUT con "
                         "IN oculta): descarga + transcribe + atribuye igual "
                         "que el batch, con guard anti-duplicados")
    ap.add_argument("--workers", type=int, default=1,
                    help="procesos en paralelo por dia (default 1 = secuencial "
                         "de siempre; 2-4 acelera el backfill, cada worker "
                         "carga sus modelos una vez)")
    ap.add_argument("--nombrar-id", type=int, default=None,
                    help="Id de A_Voz a renombrar (requiere --nombre)")
    ap.add_argument("--nombre", default=None,
                    help="nombre real confirmado por escucha")
    args = ap.parse_args()
    if args.nombrar_id is not None:
        _nombrar(args.nombrar_id, args.nombre)
        raise SystemExit(0)
    if args.cdr_id:
        if args.semilla:
            from config.settings import (VOZ_MODELO_VERSION,
                                         VOZ_VOICEPRINTS_JSON)
            from etl.conn.db import get_engine as _ge
            from etl.dominios.voz.conversaciones import (
                UMBRALES_SEED, seed_voiceprints)

            # --semilla tambien rige con --cdr-id (si no, el orden de flags
            # decidiria en silencio si la plantilla nueva existe o no).
            seed_voiceprints(_ge(), VOZ_VOICEPRINTS_JSON, UMBRALES_SEED,
                             VOZ_MODELO_VERSION, forzar=args.forzar_seed)
        _cdr_ids(args.cdr_id)
        raise SystemExit(0)
    if args.reprocesar:
        from etl.dominios.voz.conversaciones import reprocesar_rango
        reprocesar_rango(desde=_fecha(args.desde), hasta=_fecha(args.hasta),
                         limite=args.limite)
        raise SystemExit(0)
    correr_principal = args.lote or any(
        [args.desde, args.hasta, args.limite, args.semilla])
    if args.lote:
        d, h = _fecha(args.desde), _fecha(args.hasta)
        h = h or date.today()
        d = d or h - timedelta(days=7)
        x = d
        while x <= h:
            print(f"=== lote {x} ===", flush=True)
            try:
                main(desde=x, hasta=x + timedelta(days=1),
                     limite=args.limite, semilla=args.semilla,
                     forzar_seed=args.forzar_seed, workers=args.workers)
            except Exception as exc:  # un dia no frena la noche
                print(f"  dia {x} fallo: {exc}")
            x += timedelta(days=1)
    elif correr_principal:
        main(desde=_fecha(args.desde), hasta=_fecha(args.hasta),
             limite=args.limite, semilla=args.semilla,
             forzar_seed=args.forzar_seed, workers=args.workers)
    if args.fusionar:
        if not correr_principal:
            print("=== fusion de duplicados ===")
        _fusionar()
    if args.reconciliar:
        from config.settings import VOZ_MODELO_VERSION
        from etl.conn.db import get_engine as _ge2
        from etl.dominios.voz.enrolar import reconciliar_a_tecnicos
        print(f"=== reconciliar a tecnicos "
              f"({'APLICAR' if args.aplicar else 'dry-run'}) ===")
        reconciliar_a_tecnicos(_ge2(), VOZ_MODELO_VERSION,
                               aplicar=args.aplicar)
    if args.bautizar:
        from config.settings import VOZ_MODELO_VERSION
        from etl.conn.db import get_engine
        from etl.dominios.voz.enrolar import bautizar_clientes
        print(f"=== bautizar clientes ({'APLICAR' if args.aplicar else 'dry-run'}) ===")
        bautizar_clientes(get_engine(), VOZ_MODELO_VERSION, aplicar=args.aplicar)
    elif not correr_principal and not args.fusionar and not args.bautizar \
            and not args.reconciliar and not args.reprocesar:
        ap.error("nada para hacer: usar --lote/--desde/... o "
                 "--fusionar/--bautizar/--reconciliar/--reprocesar/--cdr-id")

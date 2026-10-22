"""Motor de contactos: `A_Llamada.Origen` -> evidencia -> estado -> D_Contacto.

Flujo (por telefono entrante en la ventana):
  1. Clasifica cada Origen (razon | nombre | interna | carrier).
  2. Guarda la mencion en A_ContactoDetectado (historial append-only).
  3. Agrupa por identidad (mismo/completa/ambiguo) -> personas de esa linea.
  4. Resuelve CUIT en D_PadronARCA (sin inventar: ambiguo -> revision).
  5. Crea/completa D_Contacto y el vinculo A_ContactoTelefono.

`aplicar=False` (default) = dry-run: no escribe nada.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import text

from etl.conn.db import get_engine
from etl.dominios.contactos import identidad as I
from etl.dominios.contactos import normalizar as N
from etl.dominios.contactos.arca import resolver_cuit

# Valores de dominio como parametros bound, nunca como literales SQL
# (regla anti Defender for SQL: ningun valor va en el texto).
_DIR_IN = "IN"
_FUENTE_ORIGEN = "origen"
_ORIGEN_ANURA = "anura"
_NOMBRE_RESERVADO = "nombre"


def _leer_llamadas(conn, desde, hasta, buscar):
    sql = ("SELECT Id, FechaDT, Nro_Origen, Origen FROM dbo.A_Llamada "
           "WHERE Direccion=:d AND FechaDT>=:a AND FechaDT<:b ")
    params = {"d": _DIR_IN, "a": desde, "b": hasta}
    if buscar:
        params["t"] = N.normalizar_telefono(buscar) or buscar
        sql += "AND Nro_Origen=:t "
    sql += "ORDER BY FechaDT"
    return conn.execute(text(sql), params).fetchall()


def _canonicos(evs):
    """Agrupa menciones en personas (reglas de identidad)."""
    out = []
    for ev in sorted(evs, key=lambda e: e["fecha"]):
        nombre = ev["nombre"]
        if not nombre:
            continue
        hallado = None
        for c in out:
            if I.relacion(c["nombre"], nombre) in ("mismo", "completa"):
                hallado = c
                break
        if hallado is None:
            out.append({"nombre": nombre, "razon": ev["razon"], "veces": 1,
                        "primera": ev["fecha"], "ultima": ev["fecha"],
                        "revisar": False})
        else:
            hallado["veces"] += 1
            hallado["ultima"] = ev["fecha"]
            if I.mas_completo(hallado["nombre"], nombre) != hallado["nombre"]:
                hallado["nombre"] = nombre
            if not hallado["razon"] and ev["razon"]:
                hallado["razon"] = ev["razon"]
    for i, a in enumerate(out):
        for b in out[i + 1:]:
            if I.relacion(a["nombre"], b["nombre"]) == "ambiguo":
                a["revisar"] = b["revisar"] = True
    return out


def _accion_contacto(conn, telefono, nombre, razon, cuit):
    """Decide que hacer con D_Contacto para (telefono, persona)."""
    filas = conn.execute(text(
        "SELECT IdContacto, Nombre, Cuit, RazonSocial FROM dbo.D_Contacto "
        "WHERE Telefono=:t"), {"t": telefono}).mappings().all()
    for f in filas:
        if I.relacion(f["Nombre"] or "", nombre) in ("mismo", "completa"):
            return ("completar", f["IdContacto"])
    for f in filas:
        if not (f["Nombre"] or "").strip() or N.es_placeholder(f["Nombre"]):
            return ("completar", f["IdContacto"])
    return ("alta", None)


def main(desde=None, hasta=None, limite=None, aplicar=False, buscar=None):
    hasta = hasta or datetime.now()
    desde = desde or hasta - timedelta(days=30)
    engine = get_engine()

    with engine.connect() as conn:
        filas = _leer_llamadas(conn, desde, hasta, buscar)
    print(f"Llamadas IN leidas: {len(filas)}  (ventana {desde:%Y-%m-%d} .. {hasta:%Y-%m-%d})")

    eventos = defaultdict(list)
    for Id, fecha, tel_raw, origen in filas:
        tel = N.normalizar_telefono(tel_raw)
        if not tel:
            continue
        tipo, razon, nombre = N.clasificar_origen(origen)
        nombre_p = nombre or razon
        if not nombre_p:
            continue
        eventos[tel].append({"llamada": Id, "fecha": fecha, "tipo": tipo,
                             "razon": razon, "nombre": nombre_p, "origen": origen})

    resumen = {"alta": 0, "completar": 0, "revisar": 0, "cuit": 0, "cuit_amb": 0,
               "sin_cuit": 0, "telefonos": 0, "personas": 0}
    lineas = []
    plan = []
    with engine.connect() as conn:
        for tel, evs in sorted(eventos.items()):
            if limite and len(plan) >= limite:
                break
            canonicos = _canonicos(evs)
            if not canonicos:
                continue
            resumen["telefonos"] += 1
            principal = max(canonicos, key=lambda c: c["ultima"])
            for c in canonicos:
                resumen["personas"] += 1
                base = c["razon"] or c["nombre"]
                # Nunca resolver CUIT por un nombre de pila solo (ej. "Gustavo"):
                # exige >=2 tokens o sufijo societario.
                puede = len(base.split()) >= 2 or N.tiene_sufijo_empresa(base)
                if puede:
                    cuit, cand = resolver_cuit(conn, base)
                else:
                    cuit, cand = None, 0
                if cuit:
                    resumen["cuit"] += 1
                elif cand > 1:
                    resumen["cuit_amb"] += 1
                    c["revisar"] = True
                else:
                    resumen["sin_cuit"] += 1
                if not puede:
                    c["revisar"] = True
                accion, idc = _accion_contacto(conn, tel, c["nombre"], c["razon"], cuit)
                resumen[accion] += 1
                if c["revisar"]:
                    resumen["revisar"] += 1
                plan.append({"tel": tel, "persona": c, "cuit": cuit, "cand": cand,
                             "accion": accion, "idc": idc,
                             "principal": c is principal,
                             "razon": c["razon"], "nombre": c["nombre"]})

    # salida
    for p in plan[:40]:
        estrella = "*" if p["principal"] else " "
        if p["cuit"]:
            estado = f"CUIT {p['cuit']}"
        elif p["cand"] > 1:
            estado = f"REVISAR ({p['cand']} cand.)"
        elif p["persona"]["revisar"]:
            estado = "sin CUIT (revisar)"
        else:
            estado = "sin CUIT"
        print(f" {estrella} {p['tel']:>12}  {p['nombre'][:26]:<26} "
              f"{p['razon'][:30]:<30} {p['accion']:<9} {estado}  veces={p['persona']['veces']}")
    if len(plan) > 40:
        print(f"   ... y {len(plan)-40} mas")

    print("\nResumen:")
    print(f"  telefonos {resumen['telefonos']} | personas {resumen['personas']}")
    print(f"  altas {resumen['alta']} | completar {resumen['completar']} | a revisar {resumen['revisar']}")
    print(f"  CUIT resuelto {resumen['cuit']} | ambiguo {resumen['cuit_amb']} | sin match {resumen['sin_cuit']}")
    print("  (dry-run) no se escribio nada" if not aplicar else "  [APLICAR] escribiendo...")

    if aplicar:
        _aplicar(engine, plan, eventos, desde, hasta)
    return resumen


def _aplicar(engine, plan, eventos, desde, hasta):
    teles = {p["tel"] for p in plan}
    with engine.begin() as conn:
        # evidencia (idempotente por A_Llamada_Id + fuente origen)
        ya = {r[0] for r in conn.execute(text(
            "SELECT A_Llamada_Id FROM dbo.A_ContactoDetectado "
            "WHERE Fuente=:fo AND A_Llamada_Id IS NOT NULL"),
            {"fo": _FUENTE_ORIGEN})}
        filas = []
        for tel, evs in eventos.items():
            if tel not in teles:
                continue
            for ev in evs:
                if ev["llamada"] in ya:
                    continue
                filas.append({"f": ev["fecha"], "l": ev["llamada"], "t": tel,
                              "n": ev["nombre"], "r": ev["razon"], "cr": ev["origen"],
                              "fo": _FUENTE_ORIGEN})
        if filas:
            conn.execute(text(
                "INSERT INTO dbo.A_ContactoDetectado "
                "(Fecha, A_Llamada_Id, Telefono, Nombre, RazonSocial, Fuente, TextoCrudo) "
                "VALUES (:f, :l, :t, :n, :r, :fo, :cr)"), filas)

        # estado + D_Contacto
        for p in plan:
            tel, c = p["tel"], p["persona"]
            if p["accion"] == "alta":
                idc = conn.execute(text(
                    "INSERT INTO dbo.D_Contacto (Nombre, Telefono, Cuit, RazonSocial, Origen, FechaActualizacion) "
                    "OUTPUT INSERTED.IdContacto "
                    "VALUES (:n, :t, :c, :r, :o, SYSUTCDATETIME())"),
                    {"n": c["nombre"][:100], "t": tel, "c": p["cuit"], "r": (c["razon"] or None),
                     "o": _ORIGEN_ANURA}).scalar()
            else:
                idc = p["idc"]
                conn.execute(text(
                    "UPDATE dbo.D_Contacto SET "
                    "Nombre = CASE WHEN Nombre IS NULL OR LTRIM(RTRIM(Nombre))='' OR LOWER(Nombre)=:nv "
                    "              THEN :n ELSE Nombre END, "
                    "Cuit = COALESCE(NULLIF(Cuit,''), :c), "
                    "RazonSocial = COALESCE(RazonSocial, :r), "
                    "Origen = :o, FechaActualizacion = SYSUTCDATETIME() "
                    "WHERE IdContacto=:i"),
                    {"n": c["nombre"][:100], "nv": _NOMBRE_RESERVADO, "c": p["cuit"],
                     "r": (c["razon"] or None), "o": _ORIGEN_ANURA, "i": idc})
            existe = conn.execute(text(
                "SELECT Id FROM dbo.A_ContactoTelefono WHERE Telefono=:t AND ContactoId=:i"),
                {"t": tel, "i": idc}).scalar()
            estado = "revisar" if c["revisar"] else "activo"
            if existe:
                conn.execute(text(
                    "UPDATE dbo.A_ContactoTelefono SET Nombre=:n, RazonSocial=:r, Veces=:v, "
                    "UltimaVez=:u, EsPrincipal=:p, Estado=:e, FechaActualizacion=SYSUTCDATETIME() "
                    "WHERE Id=:x"),
                    {"n": c["nombre"][:100], "r": (c["razon"] or None), "v": c["veces"],
                     "u": c["ultima"], "p": 1 if p["principal"] else 0, "e": estado, "x": existe})
            else:
                conn.execute(text(
                    "INSERT INTO dbo.A_ContactoTelefono (Telefono, ContactoId, Nombre, RazonSocial, "
                    "Fuente, Veces, PrimeraVez, UltimaVez, EsPrincipal, Estado) "
                    "VALUES (:t, :i, :n, :r, :fo, :v, :pri, :u, :p, :e)"),
                    {"t": tel, "i": idc, "n": c["nombre"][:100], "r": (c["razon"] or None),
                     "fo": _FUENTE_ORIGEN,
                     "v": c["veces"], "pri": c["primera"], "u": c["ultima"],
                     "p": 1 if p["principal"] else 0, "e": estado})
    print("  [OK] persistido")

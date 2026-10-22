"""Enrola y audita voces (cliente + tecnico) de conversaciones ya transcritas.

No re-transcribe: recalcula el embedding de cada grupo (EtiquetaLocal) desde el
.ogg + los tiempos por turno de A_ConversacionTurno.

- Cliente: ancla = telefono -> contacto (A_ContactoTelefono/D_Contacto).
- Tecnico: match por embedding contra candidatos/plantillas; si no, crea
  candidato (sin nombre: 'Sin identificar').
- OTRO no-X (hablante sin match) tambien se enrola como candidato a tecnico:
  antes se descartaba. Se excluye X (IVR/jingle).
- Nombres dichos: solo si pasan `_dicho_valido` (evita frases/empresas);
  quedan como evidencia en A_ContactoDetectado (VozId/EsTecnico).
- Repeticion: A_Voz.Veces se recalcula de R_Conversacion_Voz; confirmado
  requiere Veces >= 3 *y* nombre real (o plantilla seed). 'Sin identificar'
  nunca se confirma solo.
- Grupos ya decididos (R.VozId SET) no se re-matchean (una sola fuente de
  verdad R/Turno). R.Rol se normaliza a TECNICO/CLIENTE al decidir.

Idempotente: solo enrola grupos con R_Conversacion_Voz.VozId NULL.
"""
from __future__ import annotations

import os
import re
from collections import defaultdict

import numpy as np
from sqlalchemy import text

from config.settings import VOZ_AUDIO_ROOT, VOZ_MODELO_VERSION
from etl.conn.db import get_engine
from etl.dominios.contactos import normalizar as CN
from etl.dominios.voz import modelos as M
from etl.dominios.voz import nombres as NB
from etl.dominios.voz.conversaciones import es_tecnico_roster

VECES_CONFIRMAR = 3
UMBRAL_CANDIDATO = 0.55
# Cliente: mismo umbral que ALTA. Bajo este, dos voces son la misma persona.
UMBRAL_CLIENTE = 0.45

# Etiqueta generica para voces auto-enroladas sin nombre dicho.
# Se repite (el UNIQUE solo rige para nombres reales).
SIN_IDENTIFICAR = "Sin identificar"

# Valores de dominio como parametros bound, nunca como literales SQL
# (regla anti Defender for SQL: ningun valor va en el texto).
_ROL_CLIENTE = "CLIENTE"
_ROL_TECNICO = "TECNICO"
_ROL_OTRO = "OTRO"
_ETIQUETA_IVR = "X"
_FUENTE_TRANSCRIPT = "transcript"
_ORIGEN_ANURA = "anura"
_FUENTE_VOZ = "voz"
_ESTADO_ACTIVO = "activo"
_ESTADO_CONFIRMADO = "confirmado"
_ESTADO_CANDIDATO = "candidato"


def _tecnicos(conn, modelo):
    out = []
    for r in conn.execute(text(
        "SELECT Id, Nombre, Centroide, Umbral FROM dbo.A_Voz "
        "WHERE Activo=:a AND EsTecnico=:et AND ModeloVersion=:v"),
        {"a": 1, "et": 1, "v": modelo}).fetchall():
        v = M.parse_vector(r[2]); v = v / (np.linalg.norm(v) or 1.0)
        out.append((int(r[0]), r[1], v.astype(np.float32), float(r[3])))
    return out


def _match_tecnico(cent, tecnicos):
    """VozId del tecnico mas cercano dentro de su umbral, o None."""
    mejor, dist, umbral = None, 9.0, 9.0
    for voz_id, _nombre, tpl, um in tecnicos:
        d = 1 - float(np.dot(cent, tpl))
        if d < dist:
            mejor, dist, umbral = voz_id, d, um
    return mejor if (mejor is not None and dist <= umbral) else None


def _grupos(conn, conv_id):
    g = defaultdict(list)
    for et, rol, ini, fin in conn.execute(text(
        "SELECT EtiquetaLocal, Rol, InicioSeg, FinSeg FROM dbo.A_ConversacionTurno "
        "WHERE ConversacionId=:c"), {"c": conv_id}).fetchall():
        g[(et, rol)].append((ini, fin))
    return g


def _turnos(conn, conv_id):
    return [{"rol": r[1], "etiqueta": r[0], "texto": r[2]} for r in conn.execute(text(
        "SELECT EtiquetaLocal, Rol, Texto FROM dbo.A_ConversacionTurno "
        "WHERE ConversacionId=:c ORDER BY Nro"), {"c": conv_id}).fetchall()]


def _centroide(spk, audio, tramos):
    embs = [e for e in M.embeber(spk, audio, [(a, b, "") for a, b in tramos]) if e is not None]
    if not embs:
        return None
    c = np.mean(embs, axis=0)
    return (c / (np.linalg.norm(c) or 1.0)).astype(np.float32)


def _dicho_valido(nombre):
    """True si el nombre dicho puede bautizar una voz (auditoria de nombres).

    Rechaza frases/empresas/jingle que el extractor deja pasar
    ('Perfecto, muchisimas gracias.', 'Sorbona General Pico', ...).
    """
    n = (nombre or "").strip()
    if not n or len(n.split()) > 3:
        return False
    if re.search(r"[.,¡!¿?;:()\"/\\d]", n):
        return False
    if not CN.limpiar_nombre(n):
        return False
    if CN.es_rol(n):
        return False
    return True


def _acumular_voz(conn, modelo, *, es_tecnico, contacto_id, nombre, cent, fecha):
    """Busca/crea la voz de una persona y promedia el centroide. Devuelve VozId.

    Clientes: una voz por contacto (merge por ContactoId).
    Tecnicos: siempre fila nueva (el merge ya lo hizo _match_tecnico por
    embedding; el nombre se puede repetir).
    """
    if not es_tecnico and contacto_id:
        fila = conn.execute(text(
            "SELECT Id, Centroide FROM dbo.A_Voz WHERE ContactoId=:x AND EsTecnico=:et "
            "AND ModeloVersion=:v"),
            {"x": contacto_id, "et": 0, "v": modelo}).fetchone()
        if fila:
            voz_id, cen_txt = int(fila[0]), fila[1]
            prev = M.parse_vector(cen_txt); prev = prev / (np.linalg.norm(prev) or 1.0)
            mezcla = prev + cent
            mezcla = mezcla / (np.linalg.norm(mezcla) or 1.0)
            conn.execute(text("UPDATE dbo.A_Voz SET Centroide=CAST(:c AS VECTOR(192)), "
                              "FechaActualizacion=SYSUTCDATETIME() WHERE Id=:i"),
                         {"c": M.vec_str(mezcla), "i": voz_id})
            return voz_id
    return int(conn.execute(text(
        "INSERT INTO dbo.A_Voz (Nombre, EsTecnico, Centroide, ModeloVersion, Umbral, "
        "Clips, ContactoId, Estado, PrimeraVez, UltimaVez) "
        "OUTPUT INSERTED.Id VALUES (:n, :et, CAST(:c AS VECTOR(192)), :v, :u, 0, :x, "
        ":e, :f, :f)"),
        {"n": nombre[:120], "et": 1 if es_tecnico else 0, "c": M.vec_str(cent),
         "v": modelo, "u": UMBRAL_CANDIDATO, "x": contacto_id, "e": _ESTADO_CANDIDATO,
         "f": fecha}).scalar())


def _clientes(conn, modelo):
    """{contacto_id: [(voz_id, centroide)]} voces cliente activas."""
    out = {}
    for cid, vid, cen in conn.execute(text(
            "SELECT ContactoId, Id, Centroide FROM dbo.A_Voz "
            "WHERE Activo=:a AND EsTecnico=:et AND ContactoId IS NOT NULL "
            "AND ModeloVersion=:v"), {"a": 1, "et": 0, "v": modelo}).fetchall():
        v = M.parse_vector(cen); v = v / (np.linalg.norm(v) or 1.0)
        out.setdefault(int(cid), []).append((int(vid), v.astype(np.float32)))
    return out


def _match_cliente(cent, clientes, contacto_id):
    """VozId del cliente de ese numero si coincide; si no, None."""
    mejor, dist = None, 9.0
    for vid, cen in clientes.get(contacto_id, []):
        d = 1 - float(np.dot(cent, cen))
        if d < dist:
            mejor, dist = vid, d
    return (mejor, dist) if (mejor is not None and dist <= UMBRAL_CLIENTE) else (None, 9.0)


def _anclar(engine, tel, aplicar=False):
    """ContactoId para el telefono; crea D_Contacto + A_ContactoTelefono si falta.

    A_ContactoTelefono es la fuente de verdad (N:M telefono<->contacto): todo
    contacto nuevo por voz queda vinculado ahi (si no, era huerfano).
    """
    if not tel:
        return None
    with engine.connect() as conn:
        cid = conn.execute(text(
            "SELECT TOP 1 ContactoId FROM dbo.A_ContactoTelefono WHERE Telefono=:t "
            "ORDER BY EsPrincipal DESC, UltimaVez DESC"), {"t": tel}).scalar()
        if not cid:
            cid = conn.execute(text(
                "SELECT TOP 1 IdContacto FROM dbo.D_Contacto WHERE Telefono=:t "
                "ORDER BY IdContacto DESC"), {"t": tel}).scalar()
    if not cid:
        if not aplicar:
            return None
        with engine.begin() as conn:
            cid = conn.execute(text(
                "INSERT INTO dbo.D_Contacto (Telefono, Origen, FechaActualizacion) "
                "OUTPUT INSERTED.IdContacto VALUES (:t, :o, SYSUTCDATETIME())"),
                {"t": tel, "o": _ORIGEN_ANURA}).scalar()
    if aplicar:
        with engine.begin() as conn:
            link = conn.execute(text(
                "SELECT Id FROM dbo.A_ContactoTelefono WHERE Telefono=:t AND ContactoId=:i"),
                {"t": tel, "i": cid}).scalar()
            if not link:
                conn.execute(text(
                    "INSERT INTO dbo.A_ContactoTelefono (Telefono, ContactoId, Nombre, "
                    "Fuente, Veces, PrimeraVez, UltimaVez, EsPrincipal, Estado) "
                    "VALUES (:t, :i, :n, :f, 0, SYSUTCDATETIME(), SYSUTCDATETIME(), 1, :e)"),
                    {"t": tel, "i": cid, "n": SIN_IDENTIFICAR,
                     "f": _FUENTE_VOZ, "e": _ESTADO_ACTIVO})
    return cid


def main(desde=None, hasta=None, limite=None, aplicar=False, con_nombres=True):
    modelo = VOZ_MODELO_VERSION
    engine = get_engine()
    sql = ("SELECT c.Id, c.Fecha, c.RutaAudio FROM dbo.A_Conversacion c "
           "WHERE c.RutaAudio IS NOT NULL "
           "AND EXISTS (SELECT 1 FROM dbo.R_Conversacion_Voz r "
           "           WHERE r.ConversacionId=c.Id AND r.VozId IS NULL "
           "           AND (r.Rol=:rc OR (r.Rol=:ro AND r.EtiquetaLocal<>:x))) ")
    params = {"rc": _ROL_CLIENTE, "ro": _ROL_OTRO, "x": _ETIQUETA_IVR}
    if desde:
        sql += "AND c.Fecha >= :desde "
        params["desde"] = f"{desde:%Y-%m-%d}"
    if hasta:
        sql += "AND c.Fecha < :hasta "
        params["hasta"] = f"{hasta:%Y-%m-%d}"
    sql += "ORDER BY c.Id"
    if limite:
        sql = sql.replace("SELECT c.Id", f"SELECT TOP {int(limite)} c.Id", 1)

    with engine.connect() as conn:
        convs = conn.execute(text(sql), params).fetchall()
        tecnicos = _tecnicos(conn, modelo)
        clientes_por_contacto = _clientes(conn, modelo)
    print(f"Conversaciones a enrolar: {len(convs)} | tecnicos conocidos: {len(tecnicos)} "
          f"| contactos con voz cliente: {len(clientes_por_contacto)}")

    spk = M.get_speaker()
    grupos_n = nombres_n = nuevos = cli_hits = 0
    for cid, fecha, ruta in convs:
        path = os.path.join(VOZ_AUDIO_ROOT, ruta.replace("/", os.sep))
        if not os.path.exists(path):
            continue
        audio = M.decode_audio(path)
        if audio.size < M.SR * 3:
            continue
        with engine.connect() as conn:
            grupos = _grupos(conn, cid)
            # Grupos ya decididos (R.VozId SET en atribuir): no se re-matchean.
            # Re-matchear con mas plantillas cambiaba el veredicto (R vs Turno
            # quedaban con VozId distinto). Fuente unica: lo decidido vale.
            decididos = {tuple(r) for r in conn.execute(text(
                "SELECT EtiquetaLocal, Rol FROM dbo.R_Conversacion_Voz "
                "WHERE ConversacionId=:c AND VozId IS NOT NULL"),
                {"c": cid}).fetchall()}
            tel = conn.execute(text(
                "SELECT l.Nro_Origen FROM dbo.A_Conversacion c "
                "LEFT JOIN dbo.A_Llamada l ON l.Id=c.A_Llamada_Id WHERE c.Id=:c"),
                {"c": cid}).scalar() or ""
            tel = CN.normalizar_telefono(tel)
            turnos = _turnos(conn, cid)
        contacto_id = _anclar(engine, tel, aplicar)
        menciones = NB.extraer(turnos) if con_nombres else []

        for (et, rol), tramos in grupos.items():
            if et == "X":
                continue  # IVR/jingle: nunca es persona
            if (et, rol) in decididos:
                continue  # ya tiene VozId: no re-matchear
            if rol not in ("TECNICO", "CLIENTE", "OTRO"):
                continue
            cent = _centroide(spk, audio, tramos)
            if cent is None:
                continue

            # ORDEN DE DECISION (lo pedido): 1) tecnico conocido.
            voz_id = _match_tecnico(cent, tecnicos) if rol in ("TECNICO", "OTRO") else None
            # 2) si no matcheo tecnico, ver si es la voz ya conocida del CLIENTE
            #    de ese numero. Asi el cliente no entra al poz de tecnicos.
            if voz_id is None and contacto_id:
                voz_cli, d_cli = _match_cliente(cent, clientes_por_contacto, int(contacto_id))
                if voz_cli is not None:
                    es_tec = False
                    voz_id = voz_cli
                    cli_hits += 1
                else:
                    es_tec = rol in ("TECNICO", "OTRO")
            else:
                es_tec = rol in ("TECNICO", "OTRO") and voz_id is not None
            rol_m = "TECNICO" if es_tec else "CLIENTE"
            dicho = next((m["nombre"] for m in menciones
                          if m["rol"] == rol_m and _dicho_valido(m["nombre"])), None)

            if not es_tec and not contacto_id:
                continue  # cliente sin numero: no se puede anclar
            if not es_tec:
                voz_id = None  # se resuelve/mergea por contacto en _acumular_voz

            # dry-run: nunca escribe (ni siquiera cuando el tecnico matchea)
            if not aplicar:
                grupos_n += 1
                nuevos += 1
                continue

            if es_tec:
                if voz_id is None:
                    with engine.begin() as conn:
                        voz_id = _acumular_voz(conn, modelo, es_tecnico=True, contacto_id=None,
                                               nombre=dicho or SIN_IDENTIFICAR,
                                               cent=cent, fecha=fecha)
                    tecnicos.append((voz_id, dicho or SIN_IDENTIFICAR, cent, UMBRAL_CANDIDATO))
                    nuevos += 1
            else:
                with engine.begin() as conn:
                    voz_id = _acumular_voz(conn, modelo, es_tecnico=False,
                                           contacto_id=int(contacto_id),
                                           nombre=dicho or SIN_IDENTIFICAR, cent=cent, fecha=fecha)
                nuevos += 1
            with engine.begin() as conn:
                # R.Rol pasa a TECNICO/CLIENTE (la etiqueta local A/B/C/D
                # conserva el origen); deja de estar 'OTRO' con VozId seteado.
                conn.execute(text("UPDATE dbo.R_Conversacion_Voz SET VozId=:v, Rol=:rol2 "
                                  "WHERE ConversacionId=:c AND EtiquetaLocal=:e AND Rol=:r"),
                             {"v": voz_id, "c": cid, "e": et, "r": rol,
                              "rol2": "TECNICO" if es_tec else "CLIENTE"})
                conn.execute(text("UPDATE dbo.A_ConversacionTurno SET VozId=:v, Rol=:rol2 "
                                  "WHERE ConversacionId=:c AND EtiquetaLocal=:e AND VozId IS NULL"),
                             {"v": voz_id, "c": cid, "e": et,
                              "rol2": "TECNICO" if es_tec else "CLIENTE"})
            grupos_n += 1
            if con_nombres and dicho:
                with engine.begin() as conn:
                    conn.execute(text(
                        "INSERT INTO dbo.A_ContactoDetectado (Fecha, ConversacionId, Telefono, "
                        "Nombre, Fuente, VozId, EsTecnico) "
                        "SELECT :f, :c, :t, :n, :fo, :v, :et "
                        "WHERE NOT EXISTS (SELECT 1 FROM dbo.A_ContactoDetectado "
                        "  WHERE ConversacionId=:c AND Nombre=:n AND Fuente=:fo)"),
                        {"f": fecha, "c": cid, "t": tel.strip() or None, "n": dicho,
                          "fo": _FUENTE_TRANSCRIPT,
                          "v": voz_id, "et": 1 if es_tec else 0})
                nombres_n += 1

    print(f"grupos enrolados: {grupos_n} | voces nuevas/candidatas: {nuevos} | "
          f"nombres: {nombres_n}")
    if aplicar:
        _consolidar(engine, modelo)
        print("  [OK] consolidado (Veces/Estado)")
    else:
        print("  (dry-run) no se escribio nada")


def fusionar_duplicados_cliente(engine, modelo, umbral=0.45, aplicar=False):
    """Candidatos EsTecnico=1 de un solo telefono cuya voz coincide con la
    voz cliente de ese contacto -> eran lado cliente de llamadas sin match:
    se reapuntan R/Turnos a la voz cliente y el candidato pasa a Activo=0.
    """
    with engine.connect() as conn:
        cands = conn.execute(text(
            "SELECT Id, Centroide FROM dbo.A_Voz WHERE Activo=:a AND EsTecnico=:et "
            "AND Clips=0 AND ModeloVersion=:m"), {"a": 1, "et": 1, "m": modelo}).fetchall()
    revisados = fusionados = 0
    for voz_id, cen_txt in cands:
        voz_id = int(voz_id)
        with engine.connect() as conn:
            tels = [r[0] for r in conn.execute(text(
                """SELECT DISTINCT l.Nro_Origen FROM dbo.R_Conversacion_Voz r
                   JOIN dbo.A_Conversacion co ON co.Id=r.ConversacionId
                   LEFT JOIN dbo.A_Llamada l ON l.Id=co.A_Llamada_Id
                   WHERE r.VozId=:v AND l.Nro_Origen IS NOT NULL"""),
                {"v": voz_id}).fetchall()]
        if len(tels) != 1:
            continue
        v = M.parse_vector(cen_txt)
        v = v / (np.linalg.norm(v) or 1.0)
        with engine.connect() as conn:
            wids = [r[0] for r in conn.execute(text(
                """SELECT DISTINCT w.Id FROM dbo.A_Voz w
                   JOIN dbo.R_Conversacion_Voz r ON r.VozId=w.Id
                   JOIN dbo.A_Conversacion co ON co.Id=r.ConversacionId
                   LEFT JOIN dbo.A_Llamada l ON l.Id=co.A_Llamada_Id
                   WHERE w.Activo=:a AND w.EsTecnico=:et AND w.ModeloVersion=:m
                   AND l.Nro_Origen=:t"""), {"a": 1, "et": 0, "m": modelo, "t": tels[0]}).fetchall()]
            cli = [tuple(r) for r in conn.execute(text(
                "SELECT Id, Centroide FROM dbo.A_Voz WHERE Id IN (%s)" %
                ",".join(":w%d" % k for k in range(len(wids)))),
                {"w%d" % k: int(w) for k, w in enumerate(wids)}).fetchall()] if wids else []
        mejor, dist = None, 9.0
        for wid, wtxt in cli:
            w = M.parse_vector(wtxt)
            w = w / (np.linalg.norm(w) or 1.0)
            d = 1 - float(np.dot(v, w))
            if d < dist:
                mejor, dist = int(wid), d
        revisados += 1
        if mejor is None or dist > umbral:
            continue
        if not aplicar:
            print(f"  pendiente: voz {voz_id} -> cliente {mejor} "
                  f"(tel {tels[0]}, dist {dist:.3f})")
            fusionados += 1
            continue
        with engine.begin() as conn:
            conn.execute(text("UPDATE dbo.R_Conversacion_Voz SET VozId=:w, Rol=:r "
                              "WHERE VozId=:v"), {"w": mejor, "r": _ROL_CLIENTE, "v": voz_id})
            conn.execute(text("UPDATE dbo.A_ConversacionTurno SET VozId=:w, Rol=:r "
                              "WHERE VozId=:v"), {"w": mejor, "r": _ROL_CLIENTE, "v": voz_id})
            conn.execute(text("UPDATE dbo.A_Voz SET Activo=:a WHERE Id=:v"), {"a": 0, "v": voz_id})
        print(f"  fusionada: voz {voz_id} -> cliente {mejor} "
              f"(tel {tels[0]}, dist {dist:.3f})")
        fusionados += 1
    print(f"duplicados: {revisados} revisados, {fusionados} fusionados")
    if aplicar and fusionados:
        _consolidar(engine, modelo)
    return revisados, fusionados


def reconciliar_a_tecnicos(engine, modelo, aplicar=False):
    """Re-apunta candidatos 'Sin identificar' a plantillas de tecnicos.

    Solo banda ALTA (dist <= 0.45, el mismo corte que atribuye con confianza):
    esos grupos ya SE HABRIAN atribuido al tecnico si la plantilla hubiese
    existido cuando se procesaron (candidatos enrolados antes del seed).
    La banda MEDIA (<= umbral de la persona) se reporta y no se toca: ahi
    decide el oido (ver V_A_ColaVoces).
    Nunca toca filas con Clips>0 (plantillas seed) ni renombra nada.
    """
    from etl.dominios.voz.conversaciones import cargar_plantillas

    with engine.connect() as conn:
        cands = conn.execute(text(
            "SELECT Id, Centroide FROM dbo.A_Voz "
            "WHERE Activo=:a AND EsTecnico=:et AND Nombre=:s AND Clips=:k0 "
            "AND ModeloVersion=:m"), {"a": 1, "et": 1, "s": SIN_IDENTIFICAR, "k0": 0, "m": modelo}
        ).fetchall()
        tecnicos = cargar_plantillas(conn, modelo)
    print(f"candidatos sin identificar: {len(cands)} | plantillas: {len(tecnicos)}")
    altas = medias = 0
    for voz_id, cen_txt in cands:
        voz_id = int(voz_id)
        v = M.parse_vector(cen_txt)
        v = v / (np.linalg.norm(v) or 1.0)
        mejor, dist, umb, vid = None, 9.0, 9.0, None
        for nombre, tpl, u, i in tecnicos:
            d = 1 - float(np.dot(v, tpl))
            if d < dist:
                mejor, dist, umb, vid = nombre, d, u, i
        if mejor is None or dist > umb:
            continue
        with engine.connect() as conn:
            n = conn.execute(text(
                "SELECT COUNT(DISTINCT ConversacionId) "
                "FROM dbo.R_Conversacion_Voz WHERE VozId=:v"),
                {"v": voz_id}).scalar() or 0
        if dist <= 0.45:
            print(f"  fusiona voz {voz_id} -> {mejor} (d={dist:.3f}, {n} convs)")
            altas += 1
            if not aplicar:
                continue
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE dbo.R_Conversacion_Voz SET VozId=:w, Rol=:r "
                    "WHERE VozId=:v"), {"w": int(vid), "r": _ROL_TECNICO, "v": voz_id})
                conn.execute(text(
                    "UPDATE dbo.A_ConversacionTurno SET VozId=:w, Rol=:r "
                    "WHERE VozId=:v"), {"w": int(vid), "r": _ROL_TECNICO, "v": voz_id})
                conn.execute(text("UPDATE dbo.A_Voz SET Activo=:a WHERE Id=:v"),
                             {"a": 0, "v": voz_id})
        else:
            print(f"  revisar voz {voz_id} -> {mejor} d={dist:.3f} "
                  f"(umb {umb:.2f}, {n} convs)")
            medias += 1
    print(f"reconciliacion: {altas} fusionadas (ALTA) | {medias} a revision (MEDIA)")
    if aplicar and altas:
        _consolidar(engine, modelo)
        print("  [OK] consolidado (Veces/Estado)")
    elif not aplicar:
        print("  (dry-run) no se escribio nada")
    return altas, medias


def reconciliar_por_telefono(engine, modelo, min_llamadas=2, aplicar=False):
    """Separa cliente vs tecnico SIN adivinar, usando el telefono.

    Evidencia dura:
      - la voz de un CLIENTE se repite en varias llamadas de SU numero;
      - la voz de un TECNICO aparece en llamadas de telefonos DISTINTOS.
    Para cada telefono con >= min_llamadas: la voz que mas se repite es la de
    su cliente. Los candidatos-tecnico que solo se vieron con ese telefono se
    re-apuntan a esa voz cliente (si se parecen) y se desactivan.
    """
    with engine.connect() as conn:
        filas = conn.execute(text("""
            SELECT l.Nro_Origen, r.VozId, COUNT(DISTINCT r.ConversacionId) n
            FROM dbo.R_Conversacion_Voz r
            JOIN dbo.A_Conversacion co ON co.Id = r.ConversacionId
            JOIN dbo.A_Llamada l ON l.Id = co.A_Llamada_Id
            WHERE r.VozId IS NOT NULL AND l.Nro_Origen IS NOT NULL
            GROUP BY l.Nro_Origen, r.VozId""")).fetchall()
    tel_voz = {}
    for tel, voz, n in filas:
        tel_voz.setdefault(tel, []).append((int(voz), int(n)))
    print(f"telefonos con voz: {len(tel_voz)}")
    cambiados = 0
    with engine.connect() as conn:
        cent_cands = {int(r[0]): (r[1], M.parse_vector(r[2])) for r in conn.execute(text(
            "SELECT Id, Nombre, Centroide FROM dbo.A_Voz WHERE Activo=:a AND EsTecnico=:et "
            "AND Clips=:k0 AND ModeloVersion=:m"), {"a": 1, "et": 1, "k0": 0, "m": modelo}).fetchall()}
    for tel, vs in tel_voz.items():
        # voz del cliente = la que aparece en mas llamadas de ese telefono
        candidatas = [x for x in vs if x[1] >= min_llamadas]
        if not candidatas:
            continue
        vid_cli = max(candidatas, key=lambda x: x[1])[0]
        # candidatos-tecnico que solo aparecen con este telefono
        for voz, _n in vs:
            if voz == vid_cli or voz not in cent_cands:
                continue
            solo_este = all(t == tel for t, lista in tel_voz.items()
                            if any(v == voz for v, _ in lista))
            if not solo_este:
                continue
            cen = cent_cands[voz][1]
            cen = cen / (np.linalg.norm(cen) or 1.0)
            with engine.connect() as conn:
                cli_txt = conn.execute(text("SELECT Centroide FROM dbo.A_Voz WHERE Id=:i"),
                                       {"i": vid_cli}).scalar()
            if cli_txt is None:
                continue
            c2 = M.parse_vector(cli_txt); c2 = c2 / (np.linalg.norm(c2) or 1.0)
            d = 1 - float(np.dot(cen, c2))
            if d > UMBRAL_CLIENTE:
                continue  # no se parece: es un tecnico real, no lo toco
            print(f"  voz {voz} -> cliente {vid_cli} (tel {tel}, dist {d:.3f})")
            cambiados += 1
            if aplicar:
                with engine.begin() as conn:
                    conn.execute(text("UPDATE dbo.R_Conversacion_Voz SET VozId=:c, Rol=:r "
                                      "WHERE VozId=:v"), {"c": vid_cli, "r": _ROL_CLIENTE, "v": voz})
                    conn.execute(text("UPDATE dbo.A_ConversacionTurno SET VozId=:c, Rol=:r "
                                      "WHERE VozId=:v"), {"c": vid_cli, "r": _ROL_CLIENTE, "v": voz})
                    conn.execute(text("UPDATE dbo.A_Voz SET Activo=:a WHERE Id=:v"), {"a": 0, "v": voz})
    print(f"reconciliacion: {cambiados} re-apuntados a cliente")
    if aplicar and cambiados:
        _consolidar(engine, modelo)
    return cambiados


def bautizar_clientes(engine, modelo, aplicar=False, min_menciones=2):
    """Bautiza voces CLIENTE sin nombre con el nombre dicho (regla de bautismo).

    Condiciones, todas a la vez:
      1. voz EsTecnico=0, Activa, Nombre='Sin identificar', con contacto;
      2. el telefono tiene historia (>=2 llamadas en A_Llamada: "ya habia
         llamado con ese telefono");
      3. UN solo nombre valido dicho desde ese telefono, con >=min_menciones
         menciones Fuente='transcript' (re-validadas con _dicho_valido) y al
         menos una en una conversacion donde aparece la voz ("coinciden las
         voces");
      4. sin colision: ninguna otra voz tiene ese Nombre en la version
         (UNIQUE Nombre+ModeloVersion para nombres reales).
    Los TECNICOS nunca se bautizan solos: el nombre dicho de un tecnico queda
    como evidencia y lo confirma un humano (--nombrar-id). Dry-run por defecto;
    con aplicar=True escribe + re-consolida (Estado lo decide _consolidar:
    confirma recien con Veces>=3, asi un bautismo prematuro sigue candidato).
    """
    with engine.connect() as conn:
        voces = conn.execute(text(
            "SELECT Id, ContactoId, Veces FROM dbo.A_Voz "
            "WHERE Activo=:a AND EsTecnico=:et AND Nombre=:s AND ModeloVersion=:m"),
            {"a": 1, "et": 0, "s": SIN_IDENTIFICAR, "m": modelo}).fetchall()
    print(f"voces cliente sin nombre: {len(voces)}")
    bautizadas, colisiones, ambiguas, sin_historia, sin_mencion, roster = 0, 0, 0, 0, 0, 0
    for voz_id, contacto_id, veces in voces:
        voz_id = int(voz_id)
        with engine.connect() as conn:
            tels = [r[0] for r in conn.execute(text(
                "SELECT Telefono FROM dbo.A_ContactoTelefono WHERE ContactoId=:i"),
                {"i": int(contacto_id) if contacto_id else -1}).fetchall()] \
                if contacto_id else []
            if not tels:
                tel_top = conn.execute(text(
                    """SELECT TOP 1 l.Nro_Origen FROM dbo.R_Conversacion_Voz r
                       JOIN dbo.A_Conversacion co ON co.Id=r.ConversacionId
                       LEFT JOIN dbo.A_Llamada l ON l.Id=co.A_Llamada_Id
                       WHERE r.VozId=:v AND l.Nro_Origen IS NOT NULL
                       GROUP BY l.Nro_Origen ORDER BY COUNT(*) DESC"""),
                    {"v": voz_id}).scalar()
                tels = [tel_top] if tel_top else []
            if not tels:
                continue
            hist = conn.execute(text(
                "SELECT COUNT(DISTINCT Id) FROM dbo.A_Llamada "
                "WHERE Nro_Origen IN (%s)" % ",".join(
                    ":t%d" % k for k in range(len(tels)))),
                {"t%d" % k: t for k, t in enumerate(tels)}).scalar() or 0
            if hist < 2:
                sin_historia += 1
                continue
            menc = conn.execute(text(
                """SELECT Nombre, ConversacionId, COUNT(*) FROM dbo.A_ContactoDetectado
                   WHERE Fuente=:fo AND EsTecnico=:et AND Telefono IN (%s)
                   GROUP BY Nombre, ConversacionId""" % ",".join(
                    ":t%d" % k for k in range(len(tels)))),
                {"fo": _FUENTE_TRANSCRIPT, "et": 0,
                 **{"t%d" % k: t for k, t in enumerate(tels)}}).fetchall()
        por_nombre = {}
        for nombre, conv, n in menc:
            if not _dicho_valido(nombre):
                continue
            e = por_nombre.setdefault(nombre.strip(), {"menc": 0, "con_voz": False})
            e["menc"] += int(n)
            if conv:
                with engine.connect() as conn:
                    hay = conn.execute(text(
                        "SELECT 1 FROM dbo.R_Conversacion_Voz "
                        "WHERE ConversacionId=:c AND VozId=:v"),
                        {"c": int(conv), "v": voz_id}).scalar()
                if hay:
                    e["con_voz"] = True
        validos = {n: e for n, e in por_nombre.items()
                   if e["menc"] >= min_menciones and e["con_voz"]}
        if not validos:
            sin_mencion += 1
            continue
        if len(validos) > 1:
            print(f"  voz {voz_id} AMBIGUA (voz escuchada como varios): "
                  f"{sorted(validos)} tel={tels[0]}")
            ambiguas += 1
            continue
        nombre = next(iter(validos))
        if es_tecnico_roster(nombre):
            print(f"  voz {voz_id} ROSTER: {nombre!r} es tecnico "
                  f"(tel={tels[0]}): no se bautiza, solo oido humano")
            roster += 1
            continue
        with engine.connect() as conn:
            choque = conn.execute(text(
                "SELECT Id FROM dbo.A_Voz WHERE Nombre=:n AND ModeloVersion=:m "
                "AND Id<>:v"), {"n": nombre, "m": modelo, "v": voz_id}).scalar()
        if choque:
            print(f"  voz {voz_id} COLISION: {nombre!r} ya es voz {int(choque)} "
                  f"(tel={tels[0]})")
            colisiones += 1
            continue
        print(f"  voz {voz_id} <- {nombre!r} "
              f"(tel={tels[0]}, menc={validos[nombre]['menc']}, veces={veces})")
        bautizadas += 1
        if not aplicar:
            continue
        with engine.begin() as conn:
            conn.execute(text("UPDATE dbo.A_Voz SET Nombre=:n, "
                              "FechaActualizacion=SYSUTCDATETIME() WHERE Id=:v"),
                         {"n": nombre, "v": voz_id})
            conn.execute(text("UPDATE dbo.A_ContactoTelefono SET Nombre=:n "
                              "WHERE ContactoId=:i AND Nombre=:s"),
                         {"n": nombre,
                          "i": int(contacto_id) if contacto_id else -1,
                          "s": SIN_IDENTIFICAR})
    print(f"bautismos: {bautizadas} | colisiones: {colisiones} | ambiguas: {ambiguas} "
          f"| roster-tecnico: {roster} | sin historia: {sin_historia} | sin mencion: {sin_mencion}")
    if aplicar and bautizadas:
        _consolidar(engine, modelo)
        print("  [OK] consolidado (Veces/Estado)")
    else:
        print("  (dry-run) no se escribio nada")
    return {"bautizadas": bautizadas, "colisiones": colisiones,
            "ambiguas": ambiguas, "roster": roster}


def _consolidar(engine, modelo):
    # confirmado = voz repetida (Veces>=k) CON nombre real, o plantilla seed.
    # 'Sin identificar' por mas que repita sigue candidato hasta que un
    # humano le ponga nombre (UPDATE por Id + re-consolidar).
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE v SET
                Veces = x.n, PrimeraVez = x.pri, UltimaVez = x.ult,
                Estado = CASE WHEN v.EsTecnico=:et1 AND v.Clips>0 THEN :cf
                              WHEN x.n >= :k AND v.Nombre <> :s THEN :cf
                              ELSE :ca END
            FROM dbo.A_Voz v
            JOIN (SELECT r.VozId, COUNT(DISTINCT r.ConversacionId) n,
                         MIN(c.Fecha) pri, MAX(c.Fecha) ult
                  FROM dbo.R_Conversacion_Voz r
                  JOIN dbo.A_Conversacion c ON c.Id=r.ConversacionId
                  WHERE r.VozId IS NOT NULL GROUP BY r.VozId) x ON x.VozId=v.Id
            WHERE v.ModeloVersion=:m
        """), {"k": VECES_CONFIRMAR, "m": modelo, "s": SIN_IDENTIFICAR,
               "cf": _ESTADO_CONFIRMADO, "ca": _ESTADO_CANDIDATO, "et1": 1})

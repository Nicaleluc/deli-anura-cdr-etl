"""Resumen + categorizacion de conversaciones con Ollama local (dry-run default).

Lee A_Conversacion (transcripto) y produce Resumen/Categoria/Subcategoria/Motivo.
Idempotente por (ConversacionId, Modelo): re-correr el mismo modelo actualiza.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import text

from config.settings import OLLAMA_MODEL
from etl.conn.db import get_engine
from etl.dominios.analisis.ollama import generar_json

PLANTILLA = """Sos asistente de un call center tecnico (estaciones de servicio de la empresa).
A partir del transcripto de una llamada, devolve SOLO un JSON con:
- "resumen": 1 linea corta (max 120 chars) del motivo de la llamada.
- "categoria": {cat}
- "subcategoria": frase corta o null.
- "motivo": frase corta o null.
- "confianza": numero 0..1.

Transcripto:
---
{texto}
---
JSON:"""

CATEGORIAS_BASE = ["Facturacion", "Certificados/AFIP", "Soporte remoto",
                   "Estanque", "Administracion", "Otro"]


def _prompt(texto: str, categorias: list[str] | None) -> str:
    cat = ("una de: " + ", ".join(categorias)) if categorias else "frase corta"
    return PLANTILLA.format(cat=cat, texto=texto[:3500])


def main(desde=None, hasta=None, limite=None, aplicar=False, modelo=None,
         categorias=None, min_chars=200):
    modelo = modelo or OLLAMA_MODEL
    engine = get_engine()
    sql = ("SELECT c.Id, c.CdrId, c.Transcripto FROM dbo.A_Conversacion c "
           "WHERE c.Transcripto IS NOT NULL AND LEN(c.Transcripto) >= :m "
           "AND NOT EXISTS (SELECT 1 FROM dbo.A_ConversacionAnalisis a "
           "                WHERE a.ConversacionId = c.Id AND a.Modelo = :mod) ")
    params = {"m": min_chars, "mod": modelo}
    if desde:
        sql += "AND c.Fecha >= :a "
        params["a"] = desde
    if hasta:
        sql += "AND c.Fecha < :b "
        params["b"] = hasta
    sql += "ORDER BY c.Id DESC"
    if limite:
        sql = sql.replace("SELECT c.Id", f"SELECT TOP {int(limite)} c.Id", 1)

    with engine.connect() as conn:
        convs = conn.execute(text(sql), params).fetchall()
    print(f"Conversaciones a analizar: {len(convs)}  (modelo {modelo})")

    conteo = Counter()
    analisis = []
    for i, (cid, cdr, tr) in enumerate(convs, 1):
        datos = generar_json(_prompt(tr, categorias), modelo)
        resumen = (datos.get("resumen") or "").strip()
        cat = (datos.get("categoria") or "").strip()
        if not resumen and not cat:
            print(f"  ! conv {cid}: respuesta no parseable")
            continue
        conteo[cat or "(vacia)"] += 1
        analisis.append({"c": cid, "r": resumen[:400], "cat": cat[:60],
                         "sub": (datos.get("subcategoria") or None),
                         "mot": (datos.get("motivo") or None),
                         "conf": datos.get("confianza")})
        print(f"  {i:>3}. conv {cid} [{cat or '-'}] {resumen[:90]}")

    print("\nCategorias encontradas:")
    for cat, n in conteo.most_common():
        print(f"  {n:>3}  {cat}")

    if aplicar and analisis:
        with engine.begin() as conn:
            for a in analisis:
                existe = conn.execute(text(
                    "SELECT Id FROM dbo.A_ConversacionAnalisis "
                    "WHERE ConversacionId=:c AND Modelo=:m"), {"c": a["c"], "m": modelo}).scalar()
                p = {"r": a["r"], "cat": a["cat"], "sub": a["sub"], "mot": a["mot"],
                     "conf": a["conf"], "c": a["c"], "m": modelo}
                if existe:
                    conn.execute(text(
                        "UPDATE dbo.A_ConversacionAnalisis SET Resumen=:r, Categoria=:cat, "
                        "Subcategoria=:sub, Motivo=:mot, Confianza=:conf, "
                        "FechaAnalisis=SYSUTCDATETIME() WHERE Id=:i"),
                        {**p, "i": existe})
                else:
                    conn.execute(text(
                        "INSERT INTO dbo.A_ConversacionAnalisis "
                        "(ConversacionId, Resumen, Categoria, Subcategoria, Motivo, Modelo, Confianza) "
                        "VALUES (:c, :r, :cat, :sub, :mot, :m, :conf)"), p)
        print(f"  [OK] {len(analisis)} analisis persistidos")
    else:
        print("  (dry-run) no se escribio nada")

    return {"analizadas": len(analisis), "categorias": dict(conteo)}

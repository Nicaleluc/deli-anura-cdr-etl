"""Genera powerbi/Metricas Anura (.pbip + SemanticModel + Report).

Estrategia NO destructiva (desde 2026-10-04): el builder posee las PAGINAS
del Report y las 2 tablas .tmdl; PRESERVA todo lo que genera Desktop
(date-tables, relationships, cultures, annotations de model.tmdl,
.platform, firmas). Regenerar nunca pierde trabajo de Desktop salvo
paginas agregadas a mano (esas van en PAGINAS, aca).

IDs estables (uuid5 por nombre): re-correr sin cambios no toca git.
Cada cambio queda commiteable y revertible por separado.

Convenciones copiadas del proyecto Metricas CAU que abre bien:
- Import desde Azure SQL (misma M, otra vista), sin Calendario
  (columnas Dia/Hora/Turno materializadas en la VISTA SQL, no DAX).
- Visuales minimos sin styling (defaults): cardVisual, tableEx,
  clusteredBarChart, lineChart, donutChart, slicer.
- queryRef "Entidad.Propiedad" / "Entidad.Medida", lineageTags estables.
"""
import json
import os
import re
import shutil
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "powerbi")
SM = "Metricas Anura.SemanticModel"
RP = "Metricas Anura.Report"
ENT = "V_A_ConversacionPBI"
PERD = "V_A_LlamadaPerdida"
NS = uuid.uuid5(uuid.NAMESPACE_URL, "deli-anura-cdr-etl:pbip")


def g(semilla=None):
    """ID estable si hay semilla (uuid5), aleatorio si no."""
    if semilla is None:
        return uuid.uuid4().hex
    return uuid.uuid5(NS, semilla).hex


def w(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def w_nuevo(path, content):
    """Escribe solo si no existe (preserva lo que toco Desktop)."""
    if os.path.exists(path):
        return False
    w(path, content)
    return True


def jt(o):
    return json.dumps(o, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- TMDL
def col(tabla, nombre, tipo, fmt=None, summarize="none", source=None):
    out = [f"\tcolumn {nombre}", f"\t\tdataType: {tipo}"]
    if fmt:
        out.append(f"\t\tformatString: {fmt}")
    out.append(f"\t\tlineageTag: {g(f'col:{tabla}.{nombre}')}")
    out.append(f"\t\tsummarizeBy: {summarize}")
    if source:
        out.append(f"\t\tsourceColumn: {source}")
    out.append("")
    out.append("\t\tannotation SummarizationSetBy = Automatic")
    out.append("")
    return "\n".join(out)


def _nombre_tmdl(nombre):
    """Cita el nombre si no es un identificador simple (ej: '% ALTA')."""
    import re
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", nombre):
        return nombre
    return "'" + nombre.replace("'", "''") + "'"


def _dax(nombre, dax, kind):
    """Devuelve la linea 'column/measure X = <expr>' en TMDL valido.

    DAX de una sola linea: va inline ('column X = EXPR').
    DAX multilinea: bloque ``` ... ``` (igual que Metricas CAU).
    OJO: TMDL NO acepta '= ' cortado por salto de linea (rompia el modelo).
    """
    dax = dax.strip()
    nm = _nombre_tmdl(nombre)
    if "\n" not in dax:
        return f"\t{kind} {nm} = {dax}"
    cuerpo = "\n".join(f"\t\t\t{ln}" if ln.strip() else "" for ln in dax.split("\n"))
    return f"\t{kind} {nm} = ```\n{cuerpo}\n\t\t\t```"


def medida(tabla, nombre, dax, fmt="0"):
    out = [_dax(nombre, dax, "measure")]
    if fmt:
        out.append(f"\t\tformatString: {fmt}")
    out.append(f"\t\tlineageTag: {g(f'med:{tabla}.{nombre}')}")
    out.append("")
    return "\n".join(out)


def _env_local(clave):
    """Busca una variable en el entorno y, si no está, en el `.env` del repo.

    Parser mínimo a propósito: el builder no importa `config.settings` (que
    aborta si faltan credenciales) para poder correr en un clone limpio.
    """
    valor = os.environ.get(clave)
    if valor:
        return valor
    env = os.path.join(REPO, ".env")
    if not os.path.exists(env):
        return None
    with open(env, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            k, _, v = linea.partition("=")
            if k.strip() == clave:
                return v.strip().strip("\"'")
    return None


def _sql_destino():
    """(servidor, base) para la partición M.

    Solo `PBI_SQL_SERVER` / `PBI_SQL_DATABASE` (entorno o `.env`). Sin esas
    variables se escribe un placeholder: el `.pbip` versionado NO lleva el
    host real. Para refrescar en local, definirlas en el `.env` y regenerar
    (ese cambio de powerbi/ NO se commitea).
    """
    servidor = _env_local("PBI_SQL_SERVER")
    base = _env_local("PBI_SQL_DATABASE")
    return (servidor or "tu-servidor.database.windows.net",
            base or "tu-base")


def particion(vista):
    # Sintaxis de particion en formato Power BI (Sql.Database + paso nav).
    servidor, base = _sql_destino()
    # M escapa comillas dobles duplicandolas.
    servidor = str(servidor).replace('"', '""')
    base = str(base).replace('"', '""')
    return (f"\tpartition {vista} = m\n"
            "\t\tmode: import\n"
            "\t\tsource =\n"
            "\t\t\t\tlet\n"
            f'\t\t\t\t  Origen = Sql.Database("{servidor}", "{base}"),\n'
            f'\t\t\t\t  #"Navegación 1" = Origen{{[Schema = "dbo", Item = "{vista}"]}}[Data]\n'
            "\t\t\t\tin\n"
            '\t\t\t\t  #"Navegación 1"\n'
            "\n"
            "\t\tannotation PBI_ResultType = Table\n")


CAL = "Calendario"


def tabla_calendario():
    """Tabla de fechas calculada: puente para que UN slicer filtre ambas
    tablas de hechos (sin relacion entre si no se propagaria el filtro).
    Sin variation/columnas extra a proposito (las auto-date de Desktop
    rompieron la carga una vez)."""
    return "\n".join([
        f"table {CAL}",
        f"\tlineageTag: {g('tbl:Calendario')}",
        "",
        "\tcolumn Date",
        "\t\tformatString: General Date",
        f"\t\tlineageTag: {g('col:Calendario.Date')}",
        "\t\tsummarizeBy: none",
        "\t\tisNameInferred",
        "\t\tsourceColumn: [Date]",
        "",
        "\t\tannotation SummarizationSetBy = Automatic",
        "",
        "\tpartition Calendario = calculated",
        "\t\tmode: import",
        "\t\tsource =",
        "\t\t\t\tCALENDAR(DATE(2025, 1, 1), DATE(2029, 12, 31))",
        ""])


def _rel_id(tabla):
    import uuid as _u
    return str(_u.uuid5(NS, f"rel:Calendario.{tabla}"))


def _merge_relationships(path):
    """relationships.tmdl: asegura Hechos.Dia -> Calendario.Date (convencion
    de Desktop: fromColumn = lado MUCHOS, toColumn = lado UNO; al reves el
    motor exige unicidad en Dia y rompe la carga). Preserva otras."""
    def bloque(tabla):
        return (f"relationship {_rel_id(tabla)}\n"
                f"\tjoinOnDateBehavior: datePartOnly\n"
                f"\tfromColumn: {tabla}.DiaFecha\n"
                f"\ttoColumn: {CAL}.Date\n")
    querer = {f"relationship {_rel_id(ENT)}": bloque(ENT),
              f"relationship {_rel_id(PERD)}": bloque(PERD)}
    conservados, cur = [], []
    if os.path.exists(path):
        for ln in open(path, encoding="utf-8").read().splitlines():
            if re.match(r"^relationship \S+", ln) and cur:
                conservados.append(cur)
                cur = []
            cur.append(ln)
        if cur:
            conservados.append(cur)
    # solo bloques validos (primera linea + from/to); el resto es basura
    def valido(b):
        txt = "\n".join(b)
        return (re.match(r"^relationship \S+$", b[0] or "")
                and "fromColumn:" in txt and "toColumn:" in txt)
    otros = [b for b in conservados
             if valido(b) and b[0] not in querer
             and f"toColumn: {CAL}.Date" not in "\n".join(b)]
    w(path, "\n\n".join(["\n".join(b) for b in
                          otros + [v.splitlines() for v in querer.values()]])
        + "\n")


# Columnas de la vista V_A_ConversacionPBI (todas materializadas en SQL:
# las derivadas de hora/turno viven en la vista, NO como calc en el modelo,
# porque Power BI las borraba al reabrir/guardar el proyecto).
STR = ["Telefono", "Cliente", "Tecnico", "Confianza", "CalidadTransc",
       "Cuenta", "Terminal", "Direccion", "Estado", "UltimaAccion",
       "DuracionLlamada", "Espera", "Categoria", "Subcategoria", "Motivo",
       "HoraTxt", "DiaSemanaTxt", "Turno", "EsBuzon",
       "SemanaTxt"]
MEDIDAS = [
    ("Llamadas", f'CALCULATE(COUNTROWS({ENT}), {ENT}[EsBuzon] <> "SI")', "0"),
    ("Minutos", f'DIVIDE(CALCULATE(SUM({ENT}[DuracionSeg]), {ENT}[EsBuzon] <> "SI"), 60)', "#,##0"),
    ("Seg Habla Tec", f'CALCULATE(SUM({ENT}[SegTec]), {ENT}[EsBuzon] <> "SI")', "#,##0"),
    ("Seg Habla Cli", f'CALCULATE(SUM({ENT}[SegCli]), {ENT}[EsBuzon] <> "SI")', "#,##0"),
    ("Dur Minima", f"MIN({ENT}[DuracionSeg])", "0"),
    ("Dur P25", f"PERCENTILE.INC({ENT}[DuracionSeg], 0.25)", "0"),
    ("Dur Mediana", f"MEDIAN({ENT}[DuracionSeg])", "0"),
    ("Dur P75", f"PERCENTILE.INC({ENT}[DuracionSeg], 0.75)", "0"),
    ("Dur Maxima", f"MAX({ENT}[DuracionSeg])", "0"),
    ("ALTA", f'CALCULATE(COUNTROWS({ENT}), {ENT}[Confianza] = "ALTA")', "0"),
    ("% ALTA", "DIVIDE([ALTA], [Llamadas])", "#0%"),
    ("Desconocidas",
     f'CALCULATE(COUNTROWS({ENT}), {ENT}[Tecnico] = "desconocido")', "0"),
    ("Total Día",
     f'CALCULATE([Llamadas], REMOVEFILTERS({ENT}[Turno]))', "0"),
    ("Llamadas s/buzon",
     f'CALCULATE(COUNTROWS({ENT}), {ENT}[EsBuzon] <> "SI")', "0"),
    ("Dur Minima s/buzon",
     f'CALCULATE(MIN({ENT}[DuracionSeg]), {ENT}[EsBuzon] <> "SI")', "0"),
    ("Dur P25 s/buzon",
     f'CALCULATE(PERCENTILE.INC({ENT}[DuracionSeg], 0.25), {ENT}[EsBuzon] <> "SI")',
     "0"),
    ("Dur Mediana s/buzon",
     f'CALCULATE(MEDIAN({ENT}[DuracionSeg]), {ENT}[EsBuzon] <> "SI")', "0"),
    ("Dur P75 s/buzon",
     f'CALCULATE(PERCENTILE.INC({ENT}[DuracionSeg], 0.75), {ENT}[EsBuzon] <> "SI")',
     "0"),
    ("Dur Maxima s/buzon",
     f'CALCULATE(MAX({ENT}[DuracionSeg]), {ENT}[EsBuzon] <> "SI")', "0"),
]

tbl = [f"table {ENT}", f"\tlineageTag: {g(f'tbl:{ENT}')}", ""]
tbl.append(col(ENT, "Id", "int64", "0", "count", "Id"))
tbl.append(col(ENT, "CdrId", "int64", None, "none", "CdrId"))
tbl.append(col(ENT, "Fecha", "dateTime", "General Date", "none", "Fecha"))
tbl.append(col(ENT, "DiaFecha", "dateTime", "yyyy-MM-dd", "none", "Dia"))
tbl.append(col(ENT, "Dia", "string", None, "none", "DiaTxt"))
tbl.append(col(ENT, "Semana", "dateTime", "yyyy-MM-dd", "none", "Semana"))
tbl.append(col(ENT, "Hora", "int64", "0", "none", "Hora"))
tbl.append(col(ENT, "DiaSemana", "int64", "0", "none", "DiaSemana"))
for nombre in STR:
    tbl.append(col(ENT, nombre, "string", None, "none", nombre))
tbl.append(col(ENT, "Distancia", "double", None, "none", "Distancia"))
tbl.append(col(ENT, "DuracionSeg", "int64", None, "none", "DuracionSeg"))
tbl.append(col(ENT, "TurnosTec", "int64", None, "none", "TurnosTec"))
tbl.append(col(ENT, "SegTec", "double", None, "none", "SegTec"))
tbl.append(col(ENT, "TurnosCli", "int64", None, "none", "TurnosCli"))
tbl.append(col(ENT, "SegCli", "double", None, "none", "SegCli"))
for nombre, dax, fmt in MEDIDAS:
    tbl.append(medida(ENT, nombre, dax, fmt))
tbl.append(particion(ENT))

PCOLS = ["Id", "Duracion", "Telefono", "Cliente", "Nro_Destino",
         "Resultado", "DejoMensaje", "Estado", "UltimaAccion",
         "DuracionConversacion", "HoraTxt", "DiaSemanaTxt", "Turno",
         "Devuelta", "TipoDevuelta"]
pt = [f"table {PERD}", f"\tlineageTag: {g(f'tbl:{PERD}')}", ""]
for c in PCOLS:
    if c == "Id":
        pt.append(col(PERD, c, "int64", "0", "count", c))
    else:
        pt.append(col(PERD, c, "string", None, "none", c))
pt.append(col(PERD, "Fecha", "dateTime", "General Date", "none", "Fecha"))
pt.append(col(PERD, "DiaFecha", "dateTime", "yyyy-MM-dd", "none", "Dia"))
pt.append(col(PERD, "FechaDevuelta", "dateTime", "General Date", "none",
               "FechaDevuelta"))
pt.append(col(PERD, "Hora", "int64", "0", "none", "Hora"))
pt.append(col(PERD, "DiaSemana", "int64", "0", "none", "DiaSemana"))
pt.append(medida(PERD, "Llamadas Perdidas", f"COUNTROWS({PERD})", "0"))
pt.append(medida(PERD, "Devueltas",
                 f'CALCULATE(COUNTROWS({PERD}), {PERD}[Devuelta] = "SI")', "0"))
pt.append(medida(PERD, "% Devueltas", "DIVIDE([Devueltas], [Llamadas Perdidas])",
                 "#0%"))
pt.append(particion(PERD))

model = ("model Model\n"
         "\tculture: es-AR\n"
         "\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
         "\tsourceQueryCulture: es-AR\n"
         "\n"
         # 'ref table' es objeto de nivel DOCUMENTO (sin tab); indentado
         # como hijo de model da InvalidLineType: ReferenceObject.
         f"ref table {ENT}\n"
         f"ref table {PERD}\n")

# ---------------------------------------------------------------- JSON
SCH_V = ("https://developer.microsoft.com/json-schemas/fabric/item/report/"
         "definition/visualContainer/2.12.0/schema.json")


def proj_col(ent, prop, label=None, active=False):
    p = {"field": {"Column": {"Expression": {"SourceRef": {"Entity": ent}},
                              "Property": prop}},
         "queryRef": f"{ent}.{prop}",
         "nativeQueryRef": label or prop}
    if active:
        p["active"] = True
    return p


def proj_med(ent, prop, label=None):
    return {"field": {"Measure": {"Expression": {"SourceRef": {"Entity": ent}},
                                  "Property": prop}},
            "queryRef": f"{ent}.{prop}",
            "nativeQueryRef": label or prop,
            "displayName": label or prop}


def visual(tipo, query, x, y, w, h, z, sort=None):
    q = {"queryState": query}
    if sort is not None:
        q["sortDefinition"] = sort
    return {"$schema": SCH_V, "name": g(),
            "position": {"x": x, "y": y, "z": z, "height": h, "width": w,
                         "tabOrder": z},
            "visual": {"visualType": tipo, "query": q,
                       "drillFilterOtherVisuals": True}}


def sort_med(ent, prop):
    return {"sort": [{"field": {"Measure": {
        "Expression": {"SourceRef": {"Entity": ent}}, "Property": prop}},
        "direction": "Descending"}],
        "isDefaultSort": True}


def sort_col(ent, prop):
    return {"sort": [{"field": {"Column": {
        "Expression": {"SourceRef": {"Entity": ent}}, "Property": prop}},
        "direction": "Ascending"}],
        "isDefaultSort": True}


def v_card(ent, medida, x, y, w=300, h=140, z=1000):
    return visual("cardVisual",
                  {"Data": {"projections": [proj_med(ent, medida)]}},
                  x, y, w, h, z)


def v_bar(ent, cat, medida, x, y, w=1280, h=260, z=2000, series=None):
    # con series: barras APILADAS con leyenda por serie (ej. Turno).
    # OJO: el tipo nativo es `barChart` (stackedBarChart NO existe y rompe
    # la carga con CustomVisualNotFound). Sin colores por serie: el schema
    # estricto de Desktop rechaza scopeId con Comparison (aunque Desktop lo
    # escriba al guardar); van con colores del tema.
    q = {"Category": {"projections": [proj_col(ent, cat, active=True)]},
         "Y": {"projections": [proj_med(ent, medida)]}}
    tipo = "clusteredBarChart"
    if series:
        tipo = "barChart"
        q["Series"] = {"projections": [proj_col(ent, series)]}
    return visual(tipo, q, x, y, w, h, z, sort=sort_med(ent, medida))


def v_line(ent, cat, medida, x, y, w=1280, h=280, z=3000, series=None):
    meds = medida if isinstance(medida, list) else [medida]
    q = {"Category": {"projections": [proj_col(ent, cat, active=True)]},
         "Y": {"projections": [proj_med(ent, m) for m in meds]}}
    if series:
        q["Series"] = {"projections": [proj_col(ent, series)]}
    return visual("lineChart", q, x, y, w, h, z, sort=sort_col(ent, cat))


def v_donut(ent, cat, medida, x, y, w=620, h=360, z=4000):
    return visual("donutChart",
                  {"Category": {"projections": [proj_col(ent, cat, active=True)]},
                   "Y": {"projections": [proj_med(ent, medida)]}},
                  x, y, w, h, z, sort=sort_med(ent, medida))


def v_table(ent, cols, x, y, w=1280, h=260, z=5000, sort_columna=None):
    return visual("tableEx",
                  {"Values": {"projections": [proj_col(ent, c) for c in cols]}},
                  x, y, w, h, z,
                  sort={"sort": [{"field": {"Column": {
                      "Expression": {"SourceRef": {"Entity": ent}},
                      "Property": sort_columna or cols[0]}},
                      "direction": "Ascending"}]})


def v_table_med(ent, cols, meds, x, y, w=1280, h=420, z=5000):
    projs = [proj_col(ent, c) for c in cols] + [proj_med(ent, m) for m in meds]
    return visual("tableEx", {"Values": {"projections": projs}},
                  x, y, w, h, z)


def v_stacked_col(ent, cat, serie, medida, tootips, x, y,
                  w=1280, h=280, z=3000):
    """Columnas apiladas por serie + tooltip con medida(s) extra (ej. total
    del dia). Tipo nativo `columnChart` (stackedColumnChart NO existe)."""
    return visual(
        "columnChart",
        {"Category": {"projections": [proj_col(ent, cat, active=True)]},
         "Series": {"projections": [proj_col(ent, serie)]},
         "Y": {"projections": [proj_med(ent, medida)]},
         "Tooltips": {"projections": [proj_med(ent, m) for m in tootips]}},
        x, y, w, h, z, sort=sort_col(ent, cat))


def v_column(ent, cat, medida, x, y, w=620, h=340, z=2000, series=None):
    # barras horizontales (clusteredBarChart): tipo usado por Metricas CAU
    # (que abre). Evitamos tipos no probados para no romper el render.
    q = {"Category": {"projections": [proj_col(ent, cat, active=True)]},
         "Y": {"projections": [proj_med(ent, medida)]}}
    if series:
        q["Series"] = {"projections": [proj_col(ent, series)]}
    return visual("clusteredBarChart", q, x, y, w, h, z,
                  sort=sort_med(ent, medida))


def v_matrix(ent, rows, cols_, medida, x, y, w=1280, h=340, z=2000):
    # tabla plana (tableEx) en vez de matrix: tipo probado en CAU.
    q = {"Values": {"projections": [
        proj_col(ent, rows), proj_col(ent, cols_), proj_med(ent, medida)]}}
    return visual("tableEx", q, x, y, w, h, z)


def v_slicer(ent, campo, x, y, w=300, h=120, z=100):
    return visual("slicer", {"Values": {"projections": [proj_col(ent, campo)]}},
                  x, y, w, h, z)


def _dt_lit(d):
    return f"datetime'{d.isoformat()}T00:00:00'"


def _theme0():
    return {"ThemeDataColor": {"ColorId": 0, "Percent": 0}}


def v_slicer_fecha(desde, hasta, x, y, w=1280, h=64, z=100,
                   grupo=None):
    """Slicer de rango de fechas (modo Between) sobre la tabla Calendario,
    que filtra AMBAS tablas de hechos via relacion. Plantilla copiada del
    slicer de Fecha de Metricas CAU (que abre). `hasta` es inclusivo:
    el filtro usa dia siguiente a las 00:00 como cota superior."""
    from datetime import timedelta
    ent, campo = CAL, "Date"
    hasta_excl = hasta + timedelta(days=1)
    v = visual(
        "slicer",
        {"Values": {"projections": [proj_col(ent, campo, active=True)]}},
        x, y, w, h, z,
        sort={"sort": [{"field": {"Column": {
            "Expression": {"SourceRef": {"Entity": ent}},
            "Property": campo}}, "direction": "Ascending"}],
            "isDefaultSort": True})
    q = v["visual"]["query"]
    vis = v["visual"]
    vis["objects"] = {
        "data": [{"properties": {
            "startDate": {"expr": {"Literal": {"Value": _dt_lit(desde)}}},
            "endDate": {"expr": {"Literal": {"Value": _dt_lit(hasta)}}},
            "mode": {"expr": {"Literal": {"Value": "'Between'"}}}}}],
        "general": [{"properties": {
            "outlineColor": {"solid": {"color": {"expr": _theme0()}}},
            "filter": {"filter": {
                "Version": 2,
                "From": [{"Name": "v", "Entity": CAL, "Type": 0}],
                "Where": [{"Condition": {"And": {
                    "Left": {"Comparison": {
                        "ComparisonKind": 2,
                        "Left": {"Column": {
                            "Expression": {"SourceRef": {"Source": "v"}},
                            "Property": "Date"}},
                        "Right": {"Literal": {
                            "Value": _dt_lit(desde)}}}},
                    "Right": {"Comparison": {
                        "ComparisonKind": 3,
                        "Left": {"Column": {
                            "Expression": {"SourceRef": {"Source": "v"}},
                            "Property": "Date"}},
                        "Right": {"Literal": {
                            "Value": _dt_lit(hasta_excl)}}}}}}}]}}}}],
        "header": [{"properties": {
            "textSize": {"expr": {"Literal": {"Value": "12D"}}},
            "fontColor": {"solid": {"color": {"expr": _theme0()}}}}}],
        "pendingChangesIcon": [{"properties": {
            "color": {"solid": {"color": {"expr": _theme0()}}}}}],
        "date": [{"properties": {
            "textSize": {"expr": {"Literal": {"Value": "11D"}}},
            "fontColor": {"solid": {"color": {"expr": _theme0()}}},
            "background": {"solid": {"color": {"expr": {
                "ThemeDataColor": {"ColorId": 1, "Percent": 0.1}}}}}}}],
        "slider": [{"properties": {
            "color": {"solid": {"color": {"expr": _theme0()}}}}}],
    }
    vis["visualContainerObjects"] = {
        "title": [{"properties": {
            "show": {"expr": {"Literal": {"Value": "false"}}},
            "titleWrap": {"expr": {"Literal": {"Value": "true"}}}}}],
        "background": [{"properties": {
            "show": {"expr": {"Literal": {"Value": "true"}}},
            "color": {"solid": {"color": {"expr": {
                "ThemeDataColor": {"ColorId": 1, "Percent": 0.1}}}}},
            "transparency": {"expr": {"Literal": {"Value": "0D"}}}}}],
        "border": [{"properties": {
            "show": {"expr": {"Literal": {"Value": "false"}}},
            "color": {"solid": {"color": {"expr": _theme0()}}},
            "width": {"expr": {"Literal": {"Value": "1D"}}}}}],
        "visualHeader": [{"properties": {
            "background": {"solid": {"color": {"expr": {
                "ThemeDataColor": {"ColorId": 1, "Percent": 0.1}}}}},
            "border": {"solid": {"color": {"expr": {
                "ThemeDataColor": {"ColorId": 1, "Percent": 0.1}}}}},
            "foreground": {"solid": {"color": {"expr": _theme0()}}}}}],
    }
    if grupo:
        vis["syncGroup"] = {"groupName": grupo, "fieldChanges": True,
                            "filterChanges": True}
    return v


def rango_fechas():
    """MIN/MAX de Dia en ambas vistas (para el slicer). Sin BD: amplio."""
    from datetime import date
    amplio = (date(2025, 1, 1), date(2027, 12, 31))
    try:
        import sys as _sys
        _sys.path.insert(0, REPO)
        from etl.conn.db import get_engine
        from sqlalchemy import text as _text
        e = get_engine()
        with e.connect() as c:
            a = c.execute(_text(
                "SELECT MIN(CAST(Fecha AS DATE)), MAX(CAST(Fecha AS DATE)) "
                "FROM dbo.V_A_ConversacionPBI")).fetchone()
            b = c.execute(_text(
                "SELECT MIN(CAST(TRY_CAST(Fecha AS DATETIME2) AS DATE)), "
                "MAX(CAST(TRY_CAST(Fecha AS DATETIME2) AS DATE)) "
                "FROM dbo.V_A_LlamadaPerdida")).fetchone()
        lo = min([x for x in (a[0], b[0]) if x is not None])
        hi = max([x for x in (a[1], b[1]) if x is not None])
        print(f"rango fechas BD: {lo} a {hi}")
        return lo, hi
    except Exception as ex:
        print(f"  aviso: sin BD para rango de fechas ({ex}); uso amplio")
        return amplio


# 6 paginas: Resumen, Horarios, Duraciones, Turno, Perdidas, Calidad.
# Filtro de fechas: slicer superior sincronizado (grupo RangoFechas).
# Boxplot: Power BI no trae boxplot nativo; se entrega bandas de
# cuartiles (P25/Mediana/P75 por tecnico) + tabla de 5 numeros.
# Alturas de pagina (default 800). Horarios lleva el semanal abajo.
ALTURAS = {"Horarios": 1060}


def paginas(desde, hasta):
    sl_e = v_slicer_fecha(desde, hasta, 0, 0, grupo="RangoFechas")
    sl_p = v_slicer_fecha(desde, hasta, 0, 0, grupo="RangoFechas")
    return [
        ("Resumen", [
            sl_e,
            v_card(ENT, "Llamadas", 0, 80), v_card(ENT, "Minutos", 320, 80),
            v_card(PERD, "Llamadas Perdidas", 640, 80),
            v_card(PERD, "Devueltas", 960, 80),
            v_stacked_col(ENT, "Dia", "Turno", "Llamadas", ["Total Día"],
                          0, 240, 1280, 260),
            v_bar(ENT, "Tecnico", "Llamadas", 0, 510, 630, 280, z=2000,
                  series="Turno"),
            v_bar(ENT, "Tecnico", "Minutos", 650, 510, 630, 280, z=2100,
                  series="Turno"),
        ]),
        ("Horarios", [
            v_slicer_fecha(desde, hasta, 0, 0, grupo="RangoFechas"),
            v_card(ENT, "Llamadas", 0, 80), v_card(ENT, "Minutos", 320, 80),
            v_bar(ENT, "Hora", "Llamadas", 0, 240, 1280, 300),
            v_table_med(ENT, ["DiaSemanaTxt", "Hora"], ["Llamadas"],
                        0, 560, 1280, 240),
            v_stacked_col(ENT, "SemanaTxt", "Turno", "Llamadas", ["Total Día"],
                          0, 820, 1280, 240),
        ]),
        ("Duraciones", [
            v_slicer_fecha(desde, hasta, 0, 0, grupo="RangoFechas"),
            v_table_med(ENT, ["Tecnico"],
                        ["Llamadas s/buzon", "Dur Minima s/buzon",
                         "Dur P25 s/buzon", "Dur Mediana s/buzon",
                         "Dur P75 s/buzon", "Dur Maxima s/buzon"],
                        0, 80, 1280, 280),
            v_line(ENT, "Tecnico",
                   ["Dur P25 s/buzon", "Dur Mediana s/buzon",
                    "Dur P75 s/buzon"],
                   0, 370, 1280, 210),
            v_bar(ENT, "Tecnico", "Dur Mediana s/buzon", 0, 590, 1280, 210),
        ]),
        ("Turno", [
            v_slicer_fecha(desde, hasta, 0, 0, grupo="RangoFechas"),
            v_donut(ENT, "Turno", "Llamadas", 0, 80, 620, 340),
            v_column(ENT, "Hora", "Llamadas", 660, 80, 620, 340,
                     series="Turno"),
            v_donut(ENT, "DiaSemanaTxt", "Llamadas", 0, 440, 620, 340),
            v_card(ENT, "Llamadas", 660, 440),
        ]),
        ("Perdidas", [
            sl_p,
            v_card(PERD, "Llamadas Perdidas", 0, 80),
            v_card(PERD, "Devueltas", 320, 80),
            v_card(PERD, "% Devueltas", 640, 80),
            v_donut(PERD, "Resultado", "Llamadas Perdidas", 0, 240, 420, 260),
            v_donut(PERD, "TipoDevuelta", "Llamadas Perdidas",
                    440, 240, 420, 260),
            v_bar(PERD, "Hora", "Llamadas Perdidas", 880, 240, 400, 260),
            v_table(PERD, ["Fecha", "Telefono", "Cliente", "Resultado",
                           "Devuelta", "TipoDevuelta", "FechaDevuelta"],
                    0, 520, 1280, 280, sort_columna="Fecha"),
        ]),
        ("Calidad", [
            v_slicer_fecha(desde, hasta, 0, 0, grupo="RangoFechas"),
            v_donut(ENT, "CalidadTransc", "Llamadas", 0, 80, 620, 340),
            v_donut(ENT, "Confianza", "Llamadas", 660, 80, 620, 340),
            v_table_med(ENT, ["Tecnico", "CalidadTransc", "Confianza"],
                        ["Llamadas", "% ALTA"], 0, 440, 1280, 340),
        ]),
    ]


def _rmtree_retry(path, intentos=6):
    """rmtree con reintento: OneDrive/AV lockean archivos (WinError 5/32)."""
    import time
    import stat

    def _onerr(func, p, exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    for i in range(intentos):
        try:
            if not os.path.exists(path):
                return
            shutil.rmtree(path, onerror=_onerr)
            if not os.path.exists(path):
                return
        except (PermissionError, OSError):
            pass
        time.sleep(1.5 * (i + 1))
    if os.path.exists(path):
        raise PermissionError(f"no se pudo borrar {path} (OneDrive/AV lock)")


def _merge_model(path):
    """model.tmdl: asegura refs ENT+PERD preservando lo de Desktop
    (annotations, date-tables, cultures). Si no existe, escribe base."""
    base = model
    if not os.path.exists(path):
        w(path, base)
        return
    txt = open(path, encoding="utf-8").read()
    tiene_e = re.search(r"^ref table " + re.escape(ENT) + r"\s*$", txt, re.M)
    tiene_p = re.search(r"^ref table " + re.escape(PERD) + r"\s*$", txt, re.M)
    tiene_c = re.search(r"^ref table " + re.escape(CAL) + r"\s*$", txt, re.M)
    if tiene_e and tiene_p and tiene_c:
        return
    faltan = []
    if not tiene_e:
        faltan.append(f"ref table {ENT}")
    if not tiene_p:
        faltan.append(f"ref table {PERD}")
    if not tiene_c:
        faltan.append(f"ref table {CAL}")
    if not txt.endswith("\n"):
        txt += "\n"
    w(path, txt + "\n".join(faltan) + "\n")


def _es_tabla_fecha(nombre):
    return nombre.startswith(("LocalDateTable_", "DateTableTemplate_"))


def _variations_cubiertas(sm):
    """Date-tables con variation valida: {tabla_fecha: True}.

    Una LocalDateTable con ShowAsVariationsOnly EXIGE que alguna columna
    base tenga bloque `variation` con relationship existente que apunte
    a ella; si no, el motor rechaza el modelo al cargarlo
    (PFE_TM_SHOW_AS_VARIATION_ONLY_TABLE_NOT_A_VARIATION_TARGET).
    """
    tdir = os.path.join(sm, "definition", "tables")
    rels = {}
    rp = os.path.join(sm, "definition", "relationships.tmdl")
    if os.path.exists(rp):
        rid, frm, to = None, None, None
        for ln in open(rp, encoding="utf-8"):
            m = re.match(r"^relationship (\S+)", ln)
            if m:
                if rid:
                    rels[rid] = (frm, to)
                rid, frm, to = m.group(1), None, None
                continue
            m = re.match(r"^\tfromColumn: (\S+)", ln)
            if m:
                frm = m.group(1)
            m = re.match(r"^\ttoColumn: (\S+)", ln)
            if m:
                to = m.group(1)
        if rid:
            rels[rid] = (frm, to)
    cubiertas = set()
    for fn in os.listdir(tdir):
        if not fn.endswith(".tmdl"):
            continue
        txt = open(os.path.join(tdir, fn), encoding="utf-8").read()
        # bloques variation con su relationship y jerarquia destino
        for m in re.finditer(
                r"^\tvariation [^\n]*\n((?:^\t\t[^\n]*\n)+)", txt, re.M):
            bloque = m.group(1)
            mr = re.search(r"^\t\trelationship: (\S+)", bloque, re.M)
            mh = re.search(r"^\t\tdefaultHierarchy: (\S+)", bloque, re.M)
            if not (mr and mh):
                continue
            dest = mh.group(1).split(".")[0].strip("'")
            if mr.group(1) in rels and _es_tabla_fecha(dest):
                cubiertas.add(dest)
    return cubiertas, rels


def _reparar_fechas(sm):
    """Si hay date-tables auto sin variation que las cubra, las elimina
    (archivos, refs de model.tmdl y relaciones que las tocan).
    El reporte no usa jerarquias de fecha (usa Dia/Hora de la vista),
    asi que no se pierde nada. Si Desktop las recrea al guardar, lo hace
    en forma consistente (con sus variation)."""
    mp = os.path.join(sm, "definition", "model.tmdl")
    if not os.path.exists(mp):
        return
    txt = open(mp, encoding="utf-8").read()
    refs = re.findall(r"^ref table (\S+)\s*$", txt, re.M)
    fechas = [r for r in refs if _es_tabla_fecha(r)]
    if not fechas:
        return
    cubiertas, rels = _variations_cubiertas(sm)
    huerfanas = [f for f in fechas if f not in cubiertas]
    if not huerfanas:
        print(f"  date-tables consistentes: {sorted(cubiertas)}")
        return
    print(f"  date-tables huerfanas (sin variation): {huerfanas} -> elimino")
    tdir = os.path.join(sm, "definition", "tables")
    for f in huerfanas:
        p = os.path.join(tdir, f + ".tmdl")
        if os.path.exists(p):
            os.remove(p)
    lineas = [ln for ln in txt.splitlines()
              if not re.match(r"^ref table (LocalDateTable_|DateTableTemplate_)", ln)]
    w(mp, "\n".join(lineas) + "\n")
    # relaciones: conservar solo las que NO tocan tablas de fecha
    rp = os.path.join(sm, "definition", "relationships.tmdl")
    if os.path.exists(rp):
        bloques, cur = [], []
        for ln in open(rp, encoding="utf-8").read().splitlines():
            if re.match(r"^relationship \S+", ln) and cur:
                bloques.append(cur)
                cur = []
            cur.append(ln)
        if cur:
            bloques.append(cur)

        def toca_fecha(b):
            return any(_es_tabla_fecha(t)
                       for t in re.findall(
                           r"(?:fromColumn|toColumn): (\S+)", "\n".join(b)))

        keep = [b for b in bloques if not toca_fecha(b)]
        if keep:
            w(rp, "\n\n".join("\n".join(b) for b in keep) + "\n")
        else:
            os.remove(rp)


def build():
    desde, hasta = rango_fechas()
    pags = paginas(desde, hasta)
    sm = os.path.join(OUT, SM)
    rp = os.path.join(OUT, RP)
    w_nuevo(os.path.join(sm, ".platform"), jt(
        {"$schema": "https://developer.microsoft.com/json-schemas/fabric/"
                    "gitIntegration/platformProperties/2.0.0/schema.json",
         "metadata": {"type": "SemanticModel", "displayName": "Metricas Anura"},
         "config": {"version": "2.0",
                    "logicalId": str(uuid.uuid5(NS, "platform:sm"))}}))
    w_nuevo(os.path.join(sm, "definition.pbism"), jt(
        {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
                    "semanticModel/definitionProperties/1.0.0/schema.json",
         "version": "4.2", "settings": {}}))
    w(os.path.join(sm, "definition", "database.tmdl"),
      "database\n\tcompatibilityLevel: 1606\n")
    _merge_model(os.path.join(sm, "definition", "model.tmdl"))
    w_nuevo(os.path.join(sm, ".pbi", "editorSettings.json"), jt(
        {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
                    "semanticModel/editorSettings/1.0.0/schema.json",
         "autodetectRelationships": True, "parallelQueryLoading": True,
         "typeDetectionEnabled": True, "relationshipImportEnabled": True,
         "shouldNotifyUserOfNameConflictResolution": True}))
    w(os.path.join(sm, "definition", "tables", f"{ENT}.tmdl"), "\n".join(tbl))
    w(os.path.join(sm, "definition", "tables", f"{PERD}.tmdl"), "\n".join(pt))
    w(os.path.join(sm, "definition", "tables", "Calendario.tmdl"),
      tabla_calendario())
    _merge_relationships(os.path.join(sm, "definition", "relationships.tmdl"))
    _reparar_fechas(sm)
    for _p, _c in ((os.path.join(OUT, "Metricas Anura.pbip"), jt(
            {"$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/"
                        "pbipProperties/1.0.0/schema.json",
             "version": "1.0",
             "artifacts": [{"report": {"path": RP}}],
             "settings": {"enableAutoRecovery": True}})),
            (os.path.join(rp, ".platform"), jt(
                {"$schema": "https://developer.microsoft.com/json-schemas/fabric/"
                            "gitIntegration/platformProperties/2.0.0/schema.json",
                 "metadata": {"type": "Report", "displayName": "Metricas Anura"},
                 "config": {"version": "2.0",
                            "logicalId": str(uuid.uuid5(NS, "platform:rp"))}})),
            (os.path.join(rp, "definition.pbir"), jt(
                {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
                            "report/definitionProperties/2.0.0/schema.json",
                 "version": "4.0",
                 "datasetReference": {"byPath": {"path": f"../{SM}"}}})),
            (os.path.join(rp, "definition", "report.json"), jt(
                {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
                            "report/definition/report/3.3.0/schema.json",
                 # themeCollection es OBLIGATORIO segun el schema oficial 3.3.0.
                 # Base theme copiado de Metricas CAU (recurso estandar).
                 "themeCollection": {
                     "baseTheme": {
                         "name": "CY26SU05",
                         "reportVersionAtImport": {
                             "visual": "2.12.0", "report": "3.3.0",
                             "page": "2.1.0"},
                         "type": "SharedResources"}},
                 "resourcePackages": [{
                     "name": "SharedResources", "type": "SharedResources",
                     "items": [{"name": "CY26SU05",
                                "path": "BaseThemes/CY26SU05.json",
                                "type": "BaseTheme"}]}],
                 "settings": {"useStylableVisualContainerHeader": True,
                              "exportDataMode": "AllowSummarized",
                              "defaultDrillFilterOtherVisuals": True,
                              "allowChangeFilterTypes": True,
                              "useEnhancedTooltips": True,
                              "useDefaultAggregateDisplayName": True}})),
            (os.path.join(rp, "definition", "version.json"), jt(
                {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
                            "report/definition/versionMetadata/1.0.0/schema.json",
                 "version": "2.0.0"})),
            (os.path.join(rp, ".pbi", "localSettings.json"), jt(
                {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
                            "report/localSettings/1.0.0/schema.json"}))):
        w_nuevo(_p, _c)
    # tema base: asset versionado en tools/assets (solo si falta)
    import shutil as _sh
    _theme_src = os.path.join(REPO, "tools", "assets", "CY26SU05.json")
    _theme_dst = os.path.join(rp, "StaticResources", "SharedResources",
                              "BaseThemes", "CY26SU05.json")
    if not os.path.exists(_theme_dst):
        os.makedirs(os.path.dirname(_theme_dst), exist_ok=True)
        _sh.copyfile(_theme_src, _theme_dst)
    # paginas: el builder las posee, SALVO las congeladas (las edita el
    # usuario en Desktop y no se tocan nunca mas).
    CONGELADAS = {"Duraciones"}
    _pages = os.path.join(rp, "definition", "pages")
    os.makedirs(_pages, exist_ok=True)
    _ids_viejos = {}
    try:
        _pj = json.load(open(os.path.join(_pages, "pages.json"), encoding="utf-8"))
        for _pid in _pj.get("pageOrder", []):
            _fpj = os.path.join(_pages, _pid, "page.json")
            if os.path.isdir(os.path.join(_pages, _pid)) and os.path.exists(_fpj):
                _nm = json.load(open(_fpj, encoding="utf-8")).get("displayName")
                if _nm:
                    _ids_viejos[_nm] = _pid
    except Exception:
        pass
    for _d in os.listdir(_pages):
        _fp = os.path.join(_pages, _d)
        if os.path.isdir(_fp) and _d not in _ids_viejos.values():
            _rmtree_retry(_fp)
    order = []
    for nombre, visuals in pags:
        if nombre in CONGELADAS and nombre in _ids_viejos and os.path.isdir(
                os.path.join(_pages, _ids_viejos[nombre])):
            order.append(_ids_viejos[nombre])
            print(f"  pagina congelada (no se toca): {nombre}")
            continue
        pg = g(f"page:{nombre}")
        order.append(pg)
        _pgdir = os.path.join(rp, "definition", "pages", pg)
        if os.path.isdir(_pgdir):
            _rmtree_retry(_pgdir)  # limpia visuales viejos (IDs estables)
        w(os.path.join(rp, "definition", "pages", pg, "page.json"), jt(
            {"$schema": "https://developer.microsoft.com/json-schemas/fabric/"
                        "item/report/definition/page/2.1.0/schema.json",
             "name": pg, "displayName": nombre, "displayOption": "FitToPage",
             "height": ALTURAS.get(nombre, 800), "width": 1280}))
        for v in visuals:
            pos = v.get("position", {})
            vt = v.get("visual", {}).get("visualType", "?")
            v["name"] = g(f"vis:{nombre}:{vt}:{pos.get('x')},{pos.get('y')}")
            vd = os.path.join(rp, "definition", "pages", pg, "visuals",
                              v["name"])
            w(os.path.join(vd, "visual.json"),
              jt({k: val for k, val in v.items()}))
    w(os.path.join(rp, "definition", "pages", "pages.json"), jt(
        {"$schema": "https://developer.microsoft.com/json-schemas/fabric/item/"
                    "report/definition/pagesMetadata/1.1.0/schema.json",
         "pageOrder": order, "activePageName": order[0]}))
    n_json = n_tmdl = n_vis = 0
    for dp, _, fns in os.walk(OUT):
        for fn in fns:
            p = os.path.join(dp, fn)
            if fn.endswith(".json"):
                json.load(open(p, encoding="utf-8"))
                n_json += 1
            if fn.endswith(".tmdl"):
                assert os.path.getsize(p) > 30, p
                n_tmdl += 1
            if fn == "visual.json":
                n_vis += 1

    # --- validacion estructural modelo<->visuales --------------------------
    problemas, resumen = validar_modelo(sm, rp)
    print(f"proyecto: {n_json} json, {n_tmdl} tmdl, {n_vis} visuales, "
          f"{len(order)} paginas, {resumen['medidas']} medidas")
    if problemas:
        for p in problemas:
            print(f"  ERROR: {p}")
        raise SystemExit(f"validacion FALLO: {len(problemas)} problema(s)")
    print(f"validacion OK: {resumen['refs_med']} medidas, "
          f"{resumen['refs_col']} columnas referidas; todas resuelven a su "
          f"tipo correcto")


def _parse_tmdl(path):
    """Devuelve {(entidad, prop): 'measure'|'calc'|'column'} de un TMDL."""
    import re
    props = {}
    ent = None
    for ln in open(path, encoding="utf-8"):
        m = re.match(r"^table (\S+)", ln)
        if m:
            ent = m.group(1)
            continue
        m = re.match(r"^\tmeasure ([^\n=]+?) =", ln)
        if m:
            props[(ent, m.group(1).strip().strip("'"))] = "measure"
            continue
        m = re.match(r"^\tcolumn ([^\n=]+?) =", ln)
        if m:
            props[(ent, m.group(1).strip())] = "calc"
            continue
        m = re.match(r"^\tcolumn (?!.*\s=\s)(\S+)", ln)
        if m and "sourceColumn" not in ln:
            props[(ent, m.group(1))] = "column"
    return props


def _walk_query(obj, med, col):
    """Recorre el query JSON y junta Properties por tipo de field."""
    if isinstance(obj, dict):
        if isinstance(obj.get("Measure"), dict) and obj["Measure"].get("Property"):
            med.add(obj["Measure"]["Property"])
        if isinstance(obj.get("Column"), dict) and obj["Column"].get("Property"):
            col.add(obj["Column"]["Property"])
        agg = obj.get("Aggregation")
        if isinstance(agg, dict):
            c = agg.get("Expression", {}).get("Column", {})
            if c.get("Property"):
                col.add(c["Property"])
        for v in obj.values():
            _walk_query(v, med, col)
    elif isinstance(obj, list):
        for v in obj:
            _walk_query(v, med, col)


def validar_modelo(sm, rp):
    """Valida TMDL real y refs de visuales. Devuelve (problemas, resumen)."""
    props = {}
    for fn in os.listdir(os.path.join(sm, "definition", "tables")):
        if fn.endswith(".tmdl"):
            props.update(_parse_tmdl(os.path.join(
                sm, "definition", "tables", fn)))

    problemas = []

    # TMDL en PBIP: medidas de distintas tablas NO pueden compartir nombre
    # (el cargador las sube al scope 'Model' y colisiona).
    nombres = [p for (e, p), t in props.items() if t == "measure"]
    dup = sorted({n for n in nombres if nombres.count(n) > 1})
    for n in dup:
        problemas.append(f"nombre de medida duplicado entre tablas: '{n}'")

    # model.tmdl: 'ref table' debe ir a nivel documento (sin tab) y apuntar
    # a una tabla declarada. Indentado => InvalidLineType: ReferenceObject.
    mp = os.path.join(sm, "definition", "model.tmdl")
    if os.path.exists(mp):
        refs = []
        for ln in open(mp, encoding="utf-8"):
            s = ln.rstrip("\n")
            m = re.match(r"^(\s*)ref table (\S+)", s)
            if m:
                refs.append((m.group(1), m.group(2)))
        tablas = {k[0] for k in props}
        for indent, nombre in refs:
            if indent:
                problemas.append(
                    f"model.tmdl: 'ref table {nombre}' indentado con "
                    f"'{indent}' (debe ir a nivel documento, sin tab)")
            if nombre not in tablas:
                problemas.append(
                    f"model.tmdl: 'ref table {nombre}' sin tabla .tmdl")
    else:
        problemas.append("falta model.tmdl")

    # date-tables huerfanas: LocalDateTable sin variation que la cubra
    # rompe la CARGA del modelo (no el parseo TMDL):
    # PFE_TM_SHOW_AS_VARIATION_ONLY_TABLE_NOT_A_VARIATION_TARGET.
    cubiertas, _rels = _variations_cubiertas(sm)
    for ent in sorted({e for (e, _p) in props}):
        if _es_tabla_fecha(ent) and ent not in cubiertas:
            problemas.append(
                f"date-table '{ent}' sin variation que la cubra "
                f"(rompe la carga del modelo)")

    # relaciones: ambos extremos deben existir como tabla .tmdl
    rpath = os.path.join(sm, "definition", "relationships.tmdl")
    if os.path.exists(rpath):
        txtr = open(rpath, encoding="utf-8").read()
        extremos = set(re.findall(
            r"(?:fromColumn|toColumn): (\S+)\.(\S+)", txtr))
        tablas = {k[0] for k in props}
        for tab, col in sorted(extremos):
            if tab not in tablas:
                problemas.append(
                    f"relationships.tmdl: tabla inexistente '{tab}'")
            elif not any(k == (tab, col) or
                         (k[0] == tab and k[1].strip("'") == col.strip("'"))
                         for k in props):
                problemas.append(
                    f"relationships.tmdl: columna inexistente '{tab}.{col}'")

    refs_med, refs_col = set(), set()
    for dp, _, fns in os.walk(os.path.join(rp, "definition", "pages")):
        for fn in fns:
            if fn != "visual.json":
                continue
            d = json.loads(open(os.path.join(dp, fn), encoding="utf-8").read())
            _walk_query(d["visual"].get("query", {}), refs_med, refs_col)

    def tipo(prop):
        k = next((k for k in props if k[1] == prop), None)
        return props.get(k)

    for p in refs_med:
        t = tipo(p)
        if t != "measure":
            problemas.append(f"visual usa [Measure] '{p}' pero el modelo la "
                             f"declara como '{t or 'inexistente'}'")
    for p in refs_col:
        t = tipo(p)
        if t is None:
            problemas.append(f"visual usa [Column] '{p}' inexistente")
        elif t == "measure":
            problemas.append(f"visual usa [Column] '{p}' pero es una measure")

    n_med = sum(1 for v in props.values() if v == "measure")
    resumen = {"medidas": n_med, "refs_med": len(refs_med),
               "refs_col": len(refs_col)}
    return problemas, resumen


if __name__ == "__main__":
    build()

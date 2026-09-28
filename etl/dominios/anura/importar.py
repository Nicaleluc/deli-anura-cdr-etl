"""
Importación de llamadas telefónicas (CDRs) desde la API Anura → A_Llamada.

Lógica productiva detrás de `cli.importar_anura` (antes también paso 4 de
`etl.pipeline.main` en `deli/`, separado al repo propio 2026-09-27):
descarga el CSV de CDRs desde la última FechaDT, normaliza columnas (las crea
dinámicamente con ALTER TABLE), deduplica por FechaDT y vuelca con JsonData.
"""

import json
import unicodedata
from datetime import datetime, timedelta
from io import StringIO

import pandas as pd

from etl.conn.anura_client import AnuraClient
from etl.conn.db import get_engine
from etl.dominios.tickets.stats import ImportStats


def main(stats=None):
    if stats is None:
        stats = ImportStats()

    engine = get_engine()

    create_table_sql = """
    IF OBJECT_ID('[dbo].[A_Llamada]', 'U') IS NULL
    BEGIN
        CREATE TABLE [dbo].[A_Llamada] (
            Id INT IDENTITY(1,1) PRIMARY KEY,
            FechaImportacion DATETIME NOT NULL DEFAULT GETDATE()
        )
    END
    """

    with engine.begin() as conn:
        conn.exec_driver_sql(create_table_sql)

    validar_fecha_dt_sql = """
    IF COL_LENGTH('[dbo].[A_Llamada]', 'FechaDT') IS NULL
    BEGIN
        ALTER TABLE [dbo].[A_Llamada]
        ADD FechaDT DATETIME2(3) NULL
    END
    """

    with engine.begin() as conn:
        conn.exec_driver_sql(validar_fecha_dt_sql)

    # Nota 2026-08-27: FechaDT quedó como datetime (redondeo 0/3/7) en la v1,
    # no datetime2(3). El ALTER a datetime2 requiere ventana de mantenimiento
    # (DROP INDEX + ALTER COLUMN bloquea 5+ min en 162k filas). No se hace acá
    # para no colgar el import incremental. La idempotencia ahora se garantiza
    # con el filtro df[FechaDT > ultima_fecha] abajo, que descarta viejos aunque
    # haya drift 405→407. Ver ../deli-support-docs/integraciones/Anura.md §4 y fix_cols.py.

    create_index_sql = """
    IF NOT EXISTS (
        SELECT 1
        FROM sys.indexes
        WHERE name = 'IX_AnuraLlamadas_FechaDT'
    )
    BEGIN
        CREATE INDEX IX_AnuraLlamadas_FechaDT
        ON [dbo].[A_Llamada](FechaDT)
    END
    """

    with engine.begin() as conn:
        conn.exec_driver_sql(create_index_sql)

    query_fecha = """
    SELECT MAX(FechaDT) AS UltimaFecha
    FROM [dbo].[A_Llamada]
    """

    df_fecha = pd.read_sql(query_fecha, engine)
    ultima_fecha = df_fecha.iloc[0]["UltimaFecha"]

    end_date = datetime.now()

    if pd.notna(ultima_fecha):
        start_date = ultima_fecha + timedelta(milliseconds=1)
    else:
        start_date = datetime(2026, 1, 1)

    start_date_text = start_date.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    end_date_text = end_date.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    anura = AnuraClient()
    access_token = anura.get_access_token()
    gcapi_token = anura.get_gcapi_token(access_token)
    csv_text = anura.download_cdrs_csv(start_date_text, end_date_text, gcapi_token)

    csv_data = StringIO(csv_text)

    df = pd.read_csv(
        csv_data,
        delimiter=",",
        encoding="utf-8",
        dtype=str,
        low_memory=False
    )

    df = df.where(pd.notnull(df), None)

    def normalize_column(col_name):
        col = str(col_name).strip()
        col = unicodedata.normalize('NFKD', col)
        col = col.encode('ascii', 'ignore').decode('utf-8')
        replacements = {
            " ": "_",
            "-": "_",
            "/": "_",
            "(": "",
            ")": "",
            ".": "",
            ":": "",
            "%": "Pct",
            "#": "Num",
            ",": "",
            ";": "",
            "'": "",
            "\"": ""
        }
        for old, new in replacements.items():
            col = col.replace(old, new)
        return col[:120]

    df.columns = [normalize_column(c) for c in df.columns]

    if "Fecha" not in df.columns:
        raise Exception("NO EXISTE COLUMNA Fecha")

    df["FechaDT"] = pd.to_datetime(df["Fecha"], errors="coerce")
    df = df[df["FechaDT"].notnull()]
    df = df.drop_duplicates(subset=["FechaDT"])
    # Filtro incremental robusto: descarta todo lo <= ultima FechaDT ya guardada.
    # Sin esto, si la API devuelve filas viejas (ignora startDate a nivel ms) y
    # la columna era datetime (redondeo 0/3/7), el dedup por FechaKey fallaba
    # (405 vs 407) e insertaba duplicados.
    if pd.notna(ultima_fecha):
        df = df[df["FechaDT"] > ultima_fecha]

    df["JsonData"] = df.apply(
        lambda row: json.dumps(
            row.to_dict(),
            default=str,
            ensure_ascii=False
        ),
        axis=1
    )

    with engine.begin() as conn:
        existing_cols = pd.read_sql(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_NAME = 'A_Llamada'
            """,
            conn
        )["COLUMN_NAME"].tolist()

        for col in df.columns:
            if col not in existing_cols:
                col_type = "DATETIME2(3)" if col == "FechaDT" else "NVARCHAR(MAX)"
                conn.exec_driver_sql(
                    f"ALTER TABLE [dbo].[A_Llamada] ADD [{col}] {col_type} NULL"
                )

    query_existentes = """
    SELECT CONVERT(VARCHAR(23), FechaDT, 121) AS FechaDT
    FROM [dbo].[A_Llamada]
    WHERE FechaDT IS NOT NULL
    """

    df_existentes = pd.read_sql(query_existentes, engine)
    fechas_existentes = set(df_existentes["FechaDT"])

    df["FechaKey"] = df["FechaDT"].dt.strftime("%Y-%m-%d %H:%M:%S.%f").str[:-3]
    df_nuevos = df[~df["FechaKey"].isin(fechas_existentes)]
    df_nuevos = df_nuevos.drop(columns=["FechaKey"])

    if len(df_nuevos) > 0:
        df_nuevos.to_sql(
            name="A_Llamada",
            con=engine,
            schema="dbo",
            if_exists="append",
            index=False,
            chunksize=1000
        )
        stats.llamadas_added += len(df_nuevos)

    stats.print_pipeline()

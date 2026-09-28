-- 001_create_A_Llamada.sql
-- Bootstrap idempotente de [dbo].[A_Llamada] para deli-anura-cdr-etl.
--
-- Filosofia: crea solo el nucleo. El ETL (etl/dominios/anura/importar.py)
-- agrega solo las columnas que falten con ALTER TABLE (FechaDT -> DATETIME2(3),
-- resto -> NVARCHAR(MAX)) e inserta JsonData por fila. Re-ejecutable sin riesgo.
--
-- NOTA prod: la tabla real puede tener columnas extra por
-- evolucion historica del CSV (ingles -> espanol, con/sin acentos) + indices
-- extra de reporting.
-- Para instalacion limpia NO hace falta replicarlas: se autocrean en la
-- primera importacion. Si igual quieres replica exacta, genera el snapshot con:
--   SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
--   FROM INFORMATION_SCHEMA.COLUMNS
--   WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='A_Llamada' ORDER BY ORDINAL_POSITION;

-- 1) Tabla base -----------------------------------------------------------
IF OBJECT_ID('[dbo].[A_Llamada]', 'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[A_Llamada] (
        Id INT IDENTITY(1,1) PRIMARY KEY,
        FechaImportacion DATETIME NOT NULL DEFAULT GETDATE()
    );
END
GO

-- 2) Columna de idempotencia incremental ------------------------------------
IF COL_LENGTH('[dbo].[A_Llamada]', 'FechaDT') IS NULL
BEGIN
    ALTER TABLE [dbo].[A_Llamada]
    ADD FechaDT DATETIME2(3) NULL;
END
GO

-- 3) Raw Fecha del CSV (texto original; FechaDT es el parseado) ------------
IF COL_LENGTH('[dbo].[A_Llamada]', 'Fecha') IS NULL
BEGIN
    ALTER TABLE [dbo].[A_Llamada]
    ADD Fecha NVARCHAR(MAX) NULL;
END
GO

-- 4) Snapshot JSON por fila -------------------------------------------------
IF COL_LENGTH('[dbo].[A_Llamada]', 'JsonData') IS NULL
BEGIN
    ALTER TABLE [dbo].[A_Llamada]
    ADD JsonData NVARCHAR(MAX) NULL;
END
GO

-- 5) Indice de ventana incremental (usado por MAX(FechaDT)) ------------------
IF NOT EXISTS (
    SELECT 1
    FROM sys.indexes
    WHERE name = 'IX_AnuraLlamadas_FechaDT'
      AND object_id = OBJECT_ID('[dbo].[A_Llamada]')
)
BEGIN
    CREATE INDEX IX_AnuraLlamadas_FechaDT
    ON [dbo].[A_Llamada](FechaDT);
END
GO

-- 002_views.sql
-- Vistas de reporting sobre [dbo].[A_Llamada].
-- Idempotente (CREATE OR ALTER, Azure SQL / SQL Server 2016 SP1+).
-- Requiere 001_create_A_Llamada.sql + columnas del CSV que el ETL autocrea:
--   Direccion, Estado, Cuenta_contesto, Duracion_total, Nro_Origen, Origen,
--   Nro_Destino_Limpio, Destino, Ultima_accion, Duracion_conversacion.
--
-- RIESGO CORREGIDO (2026-09-27): Duracion_total es NVARCHAR(MAX); se usa
-- TRY_CAST(... AS INT) en vez de CAST para que una fila no numerica / vacia
-- devuelva Duracion NULL en lugar de romper TODA la vista.

-- Llamadas entrantes atendidas ------------------------------------------------
CREATE OR ALTER VIEW dbo.V_A_Llamada
AS
SELECT
    Id,
    Fecha,

    RIGHT('00' + CAST(TRY_CAST(Duracion_total AS INT) / 3600 AS VARCHAR), 2)
    + ':' +
    RIGHT('00' + CAST((TRY_CAST(Duracion_total AS INT) % 3600) / 60 AS VARCHAR), 2)
    + ':' +
    RIGHT('00' + CAST(TRY_CAST(Duracion_total AS INT) % 60 AS VARCHAR), 2)
    AS Duracion,

    Nro_Origen AS Telefono,
    Origen AS Cliente,
    Nro_Destino_Limpio AS Nro_Destino

FROM dbo.A_Llamada
WHERE Direccion = 'IN'
  AND Estado = 'ANSWER'
  AND Cuenta_contesto IS NOT NULL;
GO

-- Llamadas entrantes perdidas (con/sin mensaje) --------------------------------
CREATE OR ALTER VIEW dbo.V_A_LlamadaPerdida
AS
SELECT
    Id,
    Fecha,
    RIGHT('00' + CAST(TRY_CAST(Duracion_total AS INT) / 3600 AS VARCHAR), 2)
    + ':' + RIGHT('00' + CAST((TRY_CAST(Duracion_total AS INT) % 3600) / 60 AS VARCHAR), 2)
    + ':' + RIGHT('00' + CAST(TRY_CAST(Duracion_total AS INT) % 60 AS VARCHAR), 2) AS Duracion,
    Nro_Origen              AS Telefono,
    Origen                  AS Cliente,
    Nro_Destino_Limpio      AS Nro_Destino,
    CASE
        WHEN Destino = 'Mensaje Grabado'
          OR Ultima_accion IN ('VOICEMAIL','PLAYBACK')
            THEN 'PERDIDA - DEJO MENSAJE'
        WHEN Ultima_accion = 'MISSED_CALL'
            THEN 'PERDIDA - SIN MENSAJE'
        ELSE 'PERDIDA'
    END                     AS Resultado,
    CASE
        WHEN Destino = 'Mensaje Grabado'
          OR Ultima_accion IN ('VOICEMAIL','PLAYBACK')
            THEN 'SI'
        ELSE 'NO'
    END                     AS DejoMensaje,
    Estado                  AS Estado,
    Ultima_accion           AS UltimaAccion,
    Duracion_conversacion   AS DuracionConversacion
FROM dbo.A_Llamada
WHERE Direccion = 'IN'
  AND (Ultima_accion = 'MISSED_CALL'
       OR Destino = 'Mensaje Grabado'
       OR Ultima_accion IN ('VOICEMAIL','PLAYBACK'));
GO

-- ============================================================================
-- 02_views.sql — vistas y procedimientos de deli-anura-cdr-etl
--
-- Consolidado e idempotente (CREATE OR ALTER). Requiere 01_schema.sql.
-- Las vistas asumen las columnas del CSV que el ETL autocrea en A_Llamada:
--   Direccion, Estado, Cuenta_contesto, Duracion_total, Nro_Origen, Origen,
--   Nro_Destino_Limpio, Destino, Ultima_accion, Duracion_conversacion.
-- Usa TRY_CAST para que una fila con duración no numérica no rompa la vista.
--
-- V_A_Llamada            entrantes atendidas
-- V_A_LlamadaPerdida     perdidas (con/sin mensaje + devuelta a 5 h)
-- V_A_Conversacion       voice-ID + calidad de transcripto
-- V_A_ConversacionPBI    mesa plana para Power BI (sin PII)
-- V_A_ColaVoces          cola de revisión de voces
-- V_A_TurnoVoz           quién atendió por (día, turno, técnico)
-- V_A_PerdidaRacha       rachas de perdidas (tabla materializada + SP)
-- V_A_PerdidaResp        perdidas por técnico de turno
-- V_A_ColaCandidatos     candidatos por proximidad (no atribuye)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- V_A_Llamada: entrantes atendidas
-- ----------------------------------------------------------------------------
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

-- ----------------------------------------------------------------------------
-- V_A_LlamadaPerdida: perdidas + devuelta (mismo número vuelve a tener
-- CONTACTO dentro de las 5 h; cada perdida evalúa en forma independiente).
-- ----------------------------------------------------------------------------
CREATE OR ALTER VIEW dbo.V_A_LlamadaPerdida
AS
SELECT
    Id,
    Fecha,
    CAST(TRY_CAST(Fecha AS DATETIME2) AS DATE)             AS Dia,
    DATEPART(HOUR, TRY_CAST(Fecha AS DATETIME2))           AS Hora,
    FORMAT(TRY_CAST(Fecha AS DATETIME2), 'HH:mm')          AS HoraTxt,
    (DATEDIFF(DAY, '18991231', TRY_CAST(Fecha AS DATETIME2)) % 7) + 1
                                                           AS DiaSemana,
    CASE (DATEDIFF(DAY, '18991231', TRY_CAST(Fecha AS DATETIME2)) % 7) + 1
        WHEN 1 THEN 'Domingo' WHEN 2 THEN 'Lunes'   WHEN 3 THEN 'Martes'
        WHEN 4 THEN 'Miercoles' WHEN 5 THEN 'Jueves' WHEN 6 THEN 'Viernes'
        ELSE 'Sabado' END                                  AS DiaSemanaTxt,
    CASE
        WHEN ((DATEDIFF(DAY, '18991231', TRY_CAST(Fecha AS DATETIME2)) % 7) + 1) = 7
             AND DATEPART(HOUR, TRY_CAST(Fecha AS DATETIME2)) >= 18 THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', TRY_CAST(Fecha AS DATETIME2)) % 7) + 1) = 1 THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', TRY_CAST(Fecha AS DATETIME2)) % 7) + 1) = 2
             AND DATEPART(HOUR, TRY_CAST(Fecha AS DATETIME2)) < 7 THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', TRY_CAST(Fecha AS DATETIME2)) % 7) + 1) BETWEEN 2 AND 7
             AND (DATEPART(HOUR, TRY_CAST(Fecha AS DATETIME2)) >= 21
                  OR DATEPART(HOUR, TRY_CAST(Fecha AS DATETIME2)) < 7) THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', TRY_CAST(Fecha AS DATETIME2)) % 7) + 1) BETWEEN 2 AND 7
             AND DATEPART(HOUR, TRY_CAST(Fecha AS DATETIME2)) >= 18
             AND DATEPART(HOUR, TRY_CAST(Fecha AS DATETIME2)) < 21 THEN 'Vespertino'
        WHEN ((DATEDIFF(DAY, '18991231', TRY_CAST(Fecha AS DATETIME2)) % 7) + 1) = 7
             AND DATEPART(HOUR, TRY_CAST(Fecha AS DATETIME2)) >= 7
             AND DATEPART(HOUR, TRY_CAST(Fecha AS DATETIME2)) < 18 THEN 'Sabado'
        WHEN ((DATEDIFF(DAY, '18991231', TRY_CAST(Fecha AS DATETIME2)) % 7) + 1) BETWEEN 2 AND 7
             AND DATEPART(HOUR, TRY_CAST(Fecha AS DATETIME2)) >= 7
             AND DATEPART(HOUR, TRY_CAST(Fecha AS DATETIME2)) < 18 THEN 'Laboral'
        ELSE 'Otro'
    END                                                    AS Turno,
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
    Duracion_conversacion   AS DuracionConversacion,
    CASE WHEN dev.FechaDT IS NULL THEN 'NO' ELSE 'SI' END AS Devuelta,
    dev.FechaDT             AS FechaDevuelta,
    CASE dev.Direccion
        WHEN 'IN' THEN 'ENTRANTE'
        WHEN 'OUT' THEN 'SALIENTE'
    END                     AS TipoDevuelta
FROM dbo.A_Llamada p
OUTER APPLY (
    SELECT TOP 1 x.FechaDT, x.Direccion
    FROM dbo.A_Llamada x
    WHERE x.Id <> p.Id
      AND x.FechaDT > DATEADD(second, 60, p.FechaDT)
      AND x.FechaDT <= DATEADD(hour, 5, p.FechaDT)
      AND ( (x.Direccion = 'IN' AND x.Estado = 'ANSWER'
             AND (x.Ultima_accion IN ('MULTIDIAL', 'DIAL')
                  OR x.Cuenta_contesto IS NOT NULL)
             AND x.TelNorm = p.TelNorm AND p.TelNorm IS NOT NULL)
         OR (x.Direccion = 'OUT' AND x.Estado = 'ANSWER'
             AND x.DestNorm = p.TelNorm AND p.TelNorm IS NOT NULL) )
    ORDER BY x.FechaDT
) dev
WHERE p.Direccion = 'IN'
  AND (p.Ultima_accion = 'MISSED_CALL'
        OR p.Destino = 'Mensaje Grabado'
        OR p.Ultima_accion IN ('VOICEMAIL','PLAYBACK'));
GO

-- ----------------------------------------------------------------------------
-- V_A_Conversacion: voice-ID + calidad de transcripto.
-- El técnico sale de R (EsPrincipal=1) y sólo si la voz es de TECNICO
-- (o el placeholder 'Sin identificar'): clientes bautizados no son técnicos.
-- ----------------------------------------------------------------------------
CREATE OR ALTER VIEW dbo.V_A_Conversacion
AS
SELECT
    c.Id,
    c.CdrId,
    c.Fecha,
    c.DuracionSeg,
    l.Nro_Origen AS Telefono,
    l.Origen AS Cliente,
    COALESCE(v.Nombre, 'desconocido') AS Tecnico,
    r.Confianza,
    r.Distancia,
    c.RutaAudio,
    c.CalidadTransc
FROM dbo.A_Conversacion c
LEFT JOIN dbo.A_Llamada l
  ON l.Id = c.A_Llamada_Id
LEFT JOIN dbo.R_Conversacion_Voz r
  ON r.ConversacionId = c.Id
 AND r.EsPrincipal = 1
LEFT JOIN dbo.A_Voz v
  ON v.Id = r.VozId
 AND (v.EsTecnico = 1 OR v.Nombre = 'Sin identificar');
GO

-- ----------------------------------------------------------------------------
-- V_A_ConversacionPBI: 1 fila por conversación, SIN PII (no expone transcripto,
-- texto ni ruta de audio). Columnas derivadas (día/hora/turno) viven acá y no
-- en DAX: al reabrir/guardar en Desktop las columnas calculadas se pierden.
-- ----------------------------------------------------------------------------
CREATE OR ALTER VIEW dbo.V_A_ConversacionPBI
AS
SELECT
    c.Id,
    c.CdrId,
    c.Fecha,
    CAST(c.Fecha AS DATE)             AS Dia,
    CONVERT(VARCHAR(10), CAST(c.Fecha AS DATE), 120) AS DiaTxt,
    CAST(DATEADD(DAY,
        - (((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1 + 5) % 7),
        CAST(c.Fecha AS DATE)) AS DATE) AS Semana,
    CONVERT(VARCHAR(10), CAST(DATEADD(DAY,
        - (((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1 + 5) % 7),
        CAST(c.Fecha AS DATE)) AS DATE), 120) AS SemanaTxt,
    DATEPART(HOUR, c.Fecha)           AS Hora,
    FORMAT(c.Fecha, 'HH:mm')          AS HoraTxt,
    -- 1=domingo .. 7=sabado (independiente de @@DATEFIRST / idioma)
    (DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1 AS DiaSemana,
    CASE (DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1
        WHEN 1 THEN 'Domingo' WHEN 2 THEN 'Lunes'   WHEN 3 THEN 'Martes'
        WHEN 4 THEN 'Miercoles' WHEN 5 THEN 'Jueves' WHEN 6 THEN 'Viernes'
        ELSE 'Sabado' END             AS DiaSemanaTxt,
    CASE
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) = 7
             AND DATEPART(HOUR, c.Fecha) >= 18 THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) = 1 THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) = 2
             AND DATEPART(HOUR, c.Fecha) < 7 THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) BETWEEN 2 AND 7
             AND (DATEPART(HOUR, c.Fecha) >= 21
                  OR DATEPART(HOUR, c.Fecha) < 7) THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) BETWEEN 2 AND 7
             AND DATEPART(HOUR, c.Fecha) >= 18
             AND DATEPART(HOUR, c.Fecha) < 21 THEN 'Vespertino'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) = 7
             AND DATEPART(HOUR, c.Fecha) >= 7
             AND DATEPART(HOUR, c.Fecha) < 18 THEN 'Sabado'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) BETWEEN 2 AND 7
             AND DATEPART(HOUR, c.Fecha) >= 7
             AND DATEPART(HOUR, c.Fecha) < 18 THEN 'Laboral'
        ELSE 'Otro'
    END                               AS Turno,
    l.Nro_Origen AS Telefono,
    l.Origen AS Cliente,
    COALESCE(v.Nombre, 'desconocido') AS Tecnico,
    r.Confianza,
    r.Distancia,
    c.DuracionSeg,
    CASE WHEN c.EsBuzon = 1 THEN 'SI' ELSE 'NO' END AS EsBuzon,
    r.Turnos AS TurnosTec,
    r.Segundos AS SegTec,
    cli.Turnos AS TurnosCli,
    cli.Segundos AS SegCli,
    c.CalidadTransc,
    c.CanalTecnico,
    c.CandidatoTecnico,
    c.CandidatoDist,
    l.Cuenta_contesto AS Cuenta,
    l.Terminal_contesto AS Terminal,
    l.Direccion,
    l.Estado,
    l.Ultima_accion AS UltimaAccion,
    l.Duracion_total AS DuracionLlamada,
    l.Tiempo_espera AS Espera,
    an.Categoria,
    an.Subcategoria,
    an.Motivo
FROM dbo.A_Conversacion c
LEFT JOIN dbo.A_Llamada l
  ON l.Id = c.A_Llamada_Id
LEFT JOIN dbo.R_Conversacion_Voz r
  ON r.ConversacionId = c.Id
 AND r.EsPrincipal = 1
LEFT JOIN dbo.A_Voz v
  ON v.Id = r.VozId
 AND (v.EsTecnico = 1 OR v.Nombre = 'Sin identificar')
OUTER APPLY (
    SELECT SUM(CASE WHEN x.Rol = 'CLIENTE' THEN x.Turnos ELSE 0 END) AS Turnos,
           SUM(CASE WHEN x.Rol = 'CLIENTE' THEN x.Segundos ELSE 0 END) AS Segundos
    FROM dbo.R_Conversacion_Voz x
    WHERE x.ConversacionId = c.Id
) cli
LEFT JOIN dbo.A_ConversacionAnalisis an
  ON an.ConversacionId = c.Id;
GO

-- ----------------------------------------------------------------------------
-- V_A_ColaVoces: un renglón por voz sin nombre, con evidencia y sugerencia.
--   ESCUCHAR: probable técnico/cliente recurrente -> oído humano + --nombrar-id
--   BAUTIZAR: nombre dicho >=2 veces de 1 solo teléfono -> bautizar_clientes
--   REVISAR: patrón imposible (cluster mezclado / posible cliente mal enrolado)
-- ----------------------------------------------------------------------------
CREATE OR ALTER VIEW dbo.V_A_ColaVoces
AS
SELECT
    v.Id AS VozId,
    v.EsTecnico,
    v.Veces,
    v.UltimaVez,
    ISNULL(t.NroTels, 0) AS NroTels,
    t.TelTop,
    m.CdrId AS MuestraCdrId,
    m.Fecha AS MuestraFecha,
    m.RutaAudio AS MuestraRuta,
    d.Nombre AS NombreDicho,
    ISNULL(d.Menc, 0) AS MencDicho,
    CASE
        WHEN d.Nombre IS NOT NULL AND ISNULL(d.Menc, 0) >= 2 AND ISNULL(d.Tels, 0) = 1
             AND v.EsTecnico = 0
            THEN N'BAUTIZAR: ' + d.Nombre
        WHEN v.EsTecnico = 1 AND ISNULL(t.NroTels, 0) >= 3 AND v.Veces >= 5
            THEN N'ESCUCHAR: probable tecnico'
        WHEN v.EsTecnico = 1
            THEN N'REVISAR: posible cliente mal enrolado'
        WHEN v.EsTecnico = 0 AND ISNULL(t.NroTels, 0) > 3
            THEN N'REVISAR: cluster imposible (muchos telefonos)'
        WHEN v.EsTecnico = 0
            THEN N'ESCUCHAR: cliente recurrente'
        ELSE N'REVISAR'
    END AS Sugerencia
FROM dbo.A_Voz v
OUTER APPLY (
    SELECT COUNT(DISTINCT l.Nro_Origen) AS NroTels,
           (SELECT TOP 1 l2.Nro_Origen FROM dbo.R_Conversacion_Voz r2
            JOIN dbo.A_Conversacion co2 ON co2.Id = r2.ConversacionId
            LEFT JOIN dbo.A_Llamada l2 ON l2.Id = co2.A_Llamada_Id
            WHERE r2.VozId = v.Id AND l2.Nro_Origen IS NOT NULL
            GROUP BY l2.Nro_Origen ORDER BY COUNT(*) DESC) AS TelTop
    FROM dbo.R_Conversacion_Voz r
    JOIN dbo.A_Conversacion co ON co.Id = r.ConversacionId
    LEFT JOIN dbo.A_Llamada l ON l.Id = co.A_Llamada_Id
    WHERE r.VozId = v.Id
) t
OUTER APPLY (
    SELECT TOP 1 co.CdrId, co.Fecha, co.RutaAudio
    FROM dbo.R_Conversacion_Voz r
    JOIN dbo.A_Conversacion co ON co.Id = r.ConversacionId
    WHERE r.VozId = v.Id
    ORDER BY co.Fecha DESC
) m
OUTER APPLY (
    SELECT TOP 1 x.Nombre, COUNT(*) AS Menc,
           COUNT(DISTINCT x.Telefono) AS Tels
    FROM dbo.A_ContactoDetectado x
    WHERE x.Fuente = 'transcript' AND x.EsTecnico = 0
      AND x.Telefono = t.TelTop
    GROUP BY x.Nombre
    ORDER BY COUNT(*) DESC
) d
WHERE v.Activo = 1 AND v.Nombre = N'Sin identificar';
GO

-- ----------------------------------------------------------------------------
-- V_A_TurnoVoz: quién atendió por (Día, Turno, Técnico), SOLO por voz.
-- Turno: Guardia / Vespertino (18-21) / Laboral (7-18) / Sábado (7-18).
-- Los huecos (desconocido/Sin identificar) NO generan fila.
-- ----------------------------------------------------------------------------
CREATE OR ALTER VIEW dbo.V_A_TurnoVoz
AS
SELECT
    CAST(c.Fecha AS DATE) AS Dia,
    CASE
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) = 7
             AND DATEPART(HOUR, c.Fecha) >= 18 THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) = 1 THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) = 2
             AND DATEPART(HOUR, c.Fecha) < 7 THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) BETWEEN 2 AND 7
             AND (DATEPART(HOUR, c.Fecha) >= 21
                  OR DATEPART(HOUR, c.Fecha) < 7) THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) BETWEEN 2 AND 7
             AND DATEPART(HOUR, c.Fecha) >= 18
             AND DATEPART(HOUR, c.Fecha) < 21 THEN 'Vespertino'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) = 7
             AND DATEPART(HOUR, c.Fecha) >= 7
             AND DATEPART(HOUR, c.Fecha) < 18 THEN 'Sabado'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) BETWEEN 2 AND 7
             AND DATEPART(HOUR, c.Fecha) >= 7
             AND DATEPART(HOUR, c.Fecha) < 18 THEN 'Laboral'
        ELSE 'Otro'
    END AS Turno,
    v.Nombre AS Tecnico,
    COUNT(*) AS Atendidas,
    SUM(c.DuracionSeg) / 60 AS Minutos
FROM dbo.A_Conversacion c
JOIN dbo.R_Conversacion_Voz r
  ON r.ConversacionId = c.Id
 AND r.EsPrincipal = 1
 AND r.Rol = 'TECNICO'
JOIN dbo.A_Voz v
  ON v.Id = r.VozId
WHERE v.Nombre NOT IN ('desconocido', 'Sin identificar')
GROUP BY CAST(c.Fecha AS DATE),
    CASE
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) = 7
             AND DATEPART(HOUR, c.Fecha) >= 18 THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) = 1 THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) = 2
             AND DATEPART(HOUR, c.Fecha) < 7 THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) BETWEEN 2 AND 7
             AND (DATEPART(HOUR, c.Fecha) >= 21
                  OR DATEPART(HOUR, c.Fecha) < 7) THEN 'Guardia'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) BETWEEN 2 AND 7
             AND DATEPART(HOUR, c.Fecha) >= 18
             AND DATEPART(HOUR, c.Fecha) < 21 THEN 'Vespertino'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) = 7
             AND DATEPART(HOUR, c.Fecha) >= 7
             AND DATEPART(HOUR, c.Fecha) < 18 THEN 'Sabado'
        WHEN ((DATEDIFF(DAY, '18991231', c.Fecha) % 7) + 1) BETWEEN 2 AND 7
             AND DATEPART(HOUR, c.Fecha) >= 7
             AND DATEPART(HOUR, c.Fecha) < 18 THEN 'Laboral'
        ELSE 'Otro'
    END,
    v.Nombre;
GO

-- ----------------------------------------------------------------------------
-- Rachas de perdidas por teléfono. La vista con CTE recursivo no terminaba en
-- tiempos razonables (sin índices el plan es malo), así que se materializa en
-- dbo.A_PerdidaRacha con sp_RebuildPerdidaRacha (rebuild completo, off-peak)
-- y la vista pasa a ser un SELECT plano. Correr el SP antes del refresh de PBI.
--   - racha = primera perdida + las siguientes dentro de 5 h NO SOLAPADAS
--   - Devuelta = primer CONTACTO después de la ÚLTIMA perdida de la racha
--   - TecDevuelve sólo si la devolución es ENTRANTE (el pipeline voz es IN-only)
--   - TecnicosTurno sale de V_A_TurnoVoz (solo voz); vacío = sin identificar
-- ----------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE dbo.sp_RebuildPerdidaRacha
AS
BEGIN
    SET NOCOUNT ON;

    SELECT TelNorm, FechaDT, Id, Nro_Origen, Origen,
           CASE
               WHEN Destino = 'Mensaje Grabado'
                 OR Ultima_accion IN ('VOICEMAIL', 'PLAYBACK')
                   THEN 'PERDIDA - DEJO MENSAJE'
               WHEN Ultima_accion = 'MISSED_CALL'
                   THEN 'PERDIDA - SIN MENSAJE'
               ELSE 'PERDIDA'
           END AS Resultado,
           CASE
               WHEN Destino = 'Mensaje Grabado'
                 OR Ultima_accion IN ('VOICEMAIL', 'PLAYBACK')
                   THEN 'SI'
               ELSE 'NO'
           END AS DejoMensaje,
           ROW_NUMBER() OVER (PARTITION BY TelNorm ORDER BY FechaDT, Id) AS rn
    INTO #Ord
    FROM dbo.A_Llamada
    WHERE Direccion = 'IN'
      AND TelNorm IS NOT NULL
      AND (Ultima_accion = 'MISSED_CALL'
           OR Destino = 'Mensaje Grabado'
           OR Ultima_accion IN ('VOICEMAIL', 'PLAYBACK'));

    CREATE CLUSTERED INDEX IX_tmp_Ord ON #Ord (TelNorm, rn);

    ;WITH Rec AS (
        SELECT TelNorm, FechaDT AS Ini, FechaDT, Resultado, DejoMensaje,
               Origen AS Cliente, rn
        FROM #Ord
        WHERE rn = 1
        UNION ALL
        SELECT o.TelNorm,
               CASE WHEN o.FechaDT <= DATEADD(hour, 5, r.Ini)
                    THEN r.Ini ELSE o.FechaDT END,
               o.FechaDT, o.Resultado, o.DejoMensaje,
               CASE WHEN o.FechaDT <= DATEADD(hour, 5, r.Ini)
                    THEN r.Cliente ELSE o.Origen END,
               o.rn
        FROM #Ord o
        JOIN Rec r
          ON r.TelNorm = o.TelNorm
         AND o.rn = r.rn + 1
    ),
    Agg AS (
        SELECT TelNorm AS Telefono, Ini AS PrimeraFecha,
               MAX(FechaDT) AS UltimaFecha, COUNT(*) AS Cantidad,
               MIN(Cliente) AS Cliente,
               CASE WHEN MIN(Resultado) = MAX(Resultado)
                    THEN MIN(Resultado) ELSE 'MIXTA' END AS Resultado,
               MAX(DejoMensaje) AS DejoMensaje
        FROM Rec
        GROUP BY TelNorm, Ini
    ),
    Base AS (
        SELECT
            a.Telefono + '|' + FORMAT(a.PrimeraFecha, 'yyyyMMddHHmmss') AS RachaId,
            a.Telefono, a.Cliente,
            CAST(a.PrimeraFecha AS DATE) AS Dia,
            DATEPART(HOUR, a.PrimeraFecha) AS Hora,
            FORMAT(a.PrimeraFecha, 'HH:mm') AS HoraTxt,
            CASE
                WHEN ((DATEDIFF(DAY, '18991231', a.PrimeraFecha) % 7) + 1) = 7
                     AND DATEPART(HOUR, a.PrimeraFecha) >= 18 THEN 'Guardia'
                WHEN ((DATEDIFF(DAY, '18991231', a.PrimeraFecha) % 7) + 1) = 1 THEN 'Guardia'
                WHEN ((DATEDIFF(DAY, '18991231', a.PrimeraFecha) % 7) + 1) = 2
                     AND DATEPART(HOUR, a.PrimeraFecha) < 7 THEN 'Guardia'
                WHEN ((DATEDIFF(DAY, '18991231', a.PrimeraFecha) % 7) + 1) BETWEEN 2 AND 7
                     AND (DATEPART(HOUR, a.PrimeraFecha) >= 21
                          OR DATEPART(HOUR, a.PrimeraFecha) < 7) THEN 'Guardia'
                WHEN ((DATEDIFF(DAY, '18991231', a.PrimeraFecha) % 7) + 1) BETWEEN 2 AND 7
                     AND DATEPART(HOUR, a.PrimeraFecha) >= 18
                     AND DATEPART(HOUR, a.PrimeraFecha) < 21 THEN 'Vespertino'
                WHEN ((DATEDIFF(DAY, '18991231', a.PrimeraFecha) % 7) + 1) = 7
                     AND DATEPART(HOUR, a.PrimeraFecha) >= 7
                     AND DATEPART(HOUR, a.PrimeraFecha) < 18 THEN 'Sabado'
                WHEN ((DATEDIFF(DAY, '18991231', a.PrimeraFecha) % 7) + 1) BETWEEN 2 AND 7
                     AND DATEPART(HOUR, a.PrimeraFecha) >= 7
                     AND DATEPART(HOUR, a.PrimeraFecha) < 18 THEN 'Laboral'
                ELSE 'Otro'
            END AS Turno,
            a.PrimeraFecha, a.UltimaFecha, a.Cantidad, a.Resultado, a.DejoMensaje
        FROM Agg a
    )
    SELECT
        b.RachaId, b.Telefono, b.Cliente, b.Dia, b.Hora, b.HoraTxt, b.Turno,
        b.PrimeraFecha, b.UltimaFecha, b.Cantidad, b.Resultado, b.DejoMensaje,
        CASE WHEN dev.Id IS NULL THEN 'No' ELSE 'Si' END AS Devuelta,
        dev.FechaDT AS FechaDevuelta,
        CASE WHEN dev.Direccion = 'OUT' THEN 'Saliente' ELSE 'Entrante' END AS TipoDevuelve,
        v.Nombre AS TecDevuelve,
        r.Confianza AS ConfDevuelve,
        dev.Cuenta_contesto AS CuentaDevuelve,
        dev.Terminal_contesto AS TerminalDevuelve,
        tt.TecnicosTurno, tt.N_TecnicosTurno,
        SYSUTCDATETIME() AS ActualizadoEn
    INTO #Out
    FROM Base b
    OUTER APPLY (
        SELECT TOP 1 x.Id, x.FechaDT, x.Direccion,
                     x.Cuenta_contesto, x.Terminal_contesto
        FROM dbo.A_Llamada x
        WHERE x.Direccion = 'IN'
          AND x.TelNorm = b.Telefono
          AND x.FechaDT > DATEADD(second, 60, b.UltimaFecha)
          AND x.FechaDT <= DATEADD(hour, 5, b.UltimaFecha)
          AND x.Estado = 'ANSWER'
          AND (x.Ultima_accion IN ('MULTIDIAL', 'DIAL')
               OR x.Cuenta_contesto IS NOT NULL)
        ORDER BY x.FechaDT
    ) din
    OUTER APPLY (
        SELECT TOP 1 x.Id, x.FechaDT, x.Direccion,
                     x.Cuenta_contesto, x.Terminal_contesto
        FROM dbo.A_Llamada x
        WHERE x.Direccion = 'OUT'
          AND x.DestNorm = b.Telefono
          AND x.FechaDT > DATEADD(second, 60, b.UltimaFecha)
          AND x.FechaDT <= DATEADD(hour, 5, b.UltimaFecha)
          AND x.Estado = 'ANSWER'
        ORDER BY x.FechaDT
    ) dout
    CROSS APPLY (
        SELECT CASE
            WHEN din.Id IS NULL THEN dout.Id
            WHEN dout.Id IS NULL THEN din.Id
            WHEN din.FechaDT <= dout.FechaDT THEN din.Id
            ELSE dout.Id END AS Id,
        CASE
            WHEN din.Id IS NULL THEN dout.FechaDT
            WHEN dout.Id IS NULL THEN din.FechaDT
            WHEN din.FechaDT <= dout.FechaDT THEN din.FechaDT
            ELSE dout.FechaDT END AS FechaDT,
        CASE
            WHEN din.Id IS NULL THEN dout.Direccion
            WHEN dout.Id IS NULL THEN din.Direccion
            WHEN din.FechaDT <= dout.FechaDT THEN din.Direccion
            ELSE dout.Direccion END AS Direccion,
        CASE
            WHEN din.Id IS NULL THEN dout.Cuenta_contesto
            WHEN dout.Id IS NULL THEN din.Cuenta_contesto
            WHEN din.FechaDT <= dout.FechaDT THEN din.Cuenta_contesto
            ELSE dout.Cuenta_contesto END AS Cuenta_contesto,
        CASE
            WHEN din.Id IS NULL THEN dout.Terminal_contesto
            WHEN dout.Id IS NULL THEN din.Terminal_contesto
            WHEN din.FechaDT <= dout.FechaDT THEN din.Terminal_contesto
            ELSE dout.Terminal_contesto END AS Terminal_contesto
    ) dev
    LEFT JOIN dbo.A_Conversacion co
      ON co.A_Llamada_Id = dev.Id
    LEFT JOIN dbo.R_Conversacion_Voz r
      ON r.ConversacionId = co.Id
     AND r.EsPrincipal = 1
     AND r.Rol = 'TECNICO'
    LEFT JOIN dbo.A_Voz v
      ON v.Id = r.VozId
     AND v.Nombre NOT IN ('desconocido', 'Sin identificar')
    OUTER APPLY (
        SELECT STRING_AGG(t.Tecnico, ', ') WITHIN GROUP (ORDER BY t.Tecnico) AS TecnicosTurno,
               COUNT(*) AS N_TecnicosTurno
        FROM dbo.V_A_TurnoVoz t
        WHERE t.Dia = b.Dia
          AND t.Turno = b.Turno
    ) tt;

    BEGIN TRAN;
    TRUNCATE TABLE dbo.A_PerdidaRacha;
    INSERT INTO dbo.A_PerdidaRacha
        (RachaId, Telefono, Cliente, Dia, Hora, HoraTxt, Turno,
         PrimeraFecha, UltimaFecha, Cantidad, Resultado, DejoMensaje,
         Devuelta, FechaDevuelta, TipoDevuelve, TecDevuelve, ConfDevuelve,
         CuentaDevuelve, TerminalDevuelve, TecnicosTurno, N_TecnicosTurno,
         ActualizadoEn)
    SELECT RachaId, Telefono, Cliente, Dia, Hora, HoraTxt, Turno,
           PrimeraFecha, UltimaFecha, Cantidad, Resultado, DejoMensaje,
           Devuelta, FechaDevuelta, TipoDevuelve, TecDevuelve, ConfDevuelve,
           CuentaDevuelve, TerminalDevuelve, TecnicosTurno, N_TecnicosTurno,
           ActualizadoEn
    FROM #Out;
    COMMIT TRAN;
    DROP TABLE #Ord;
    DROP TABLE #Out;
END
GO

CREATE OR ALTER VIEW dbo.V_A_PerdidaRacha
AS
SELECT RachaId, Telefono, Cliente, Dia, Hora, HoraTxt, Turno,
       PrimeraFecha, UltimaFecha, Cantidad, Resultado, DejoMensaje,
       Devuelta, FechaDevuelta, TipoDevuelve, TecDevuelve, ConfDevuelve,
       CuentaDevuelve, TerminalDevuelve, TecnicosTurno, N_TecnicosTurno
FROM dbo.A_PerdidaRacha;
GO

-- ----------------------------------------------------------------------------
-- V_A_PerdidaResp: 1 fila por (racha, técnico de turno). Si la racha no fue
-- devuelta, cada técnico del turno suma 1 (no devueltas por técnico).
-- Rachas sin técnico identificado van como 'sin tecnico identificado'.
-- ----------------------------------------------------------------------------
CREATE OR ALTER VIEW dbo.V_A_PerdidaResp
AS
SELECT
    r.RachaId,
    r.Telefono,
    r.Cliente,
    r.Dia,
    r.Hora,
    r.HoraTxt,
    r.Turno,
    r.PrimeraFecha,
    r.UltimaFecha,
    r.Cantidad,
    r.Resultado,
    r.DejoMensaje,
    r.Devuelta,
    r.FechaDevuelta,
    r.TipoDevuelve,
    r.TecDevuelve,
    r.ConfDevuelve,
    s.TecnicoTurno
FROM dbo.A_PerdidaRacha r
CROSS APPLY (
    SELECT LTRIM(RTRIM(value)) AS TecnicoTurno
    FROM STRING_SPLIT(r.TecnicosTurno, ',')
    WHERE r.TecnicosTurno IS NOT NULL
      AND LTRIM(RTRIM(value)) <> ''
    UNION ALL
    SELECT 'sin tecnico identificado'
    WHERE r.TecnicosTurno IS NULL
) s;
GO

-- ----------------------------------------------------------------------------
-- V_A_ColaCandidatos: voces cercanas en la banda de candidato, para revisión
-- humana. No atribuye nada por sí sola.
-- ----------------------------------------------------------------------------
CREATE OR ALTER VIEW dbo.V_A_ColaCandidatos
AS
SELECT
    c.Id,
    c.CdrId,
    c.Fecha,
    l.Nro_Origen       AS Telefono,
    l.Origen           AS Cliente,
    c.CandidatoTecnico AS Candidato,
    c.CandidatoDist    AS Distancia,
    c.CanalTecnico     AS Canal,
    c.DuracionSeg,
    c.RutaAudio
FROM dbo.A_Conversacion c
LEFT JOIN dbo.A_Llamada l ON l.Id = c.A_Llamada_Id
WHERE c.CandidatoTecnico IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM dbo.R_Conversacion_Voz r
      WHERE r.ConversacionId = c.Id AND r.EsPrincipal = 1 AND r.VozId IS NOT NULL);
GO

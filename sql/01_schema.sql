-- ============================================================================
-- 01_schema.sql — DDL de deli-anura-cdr-etl (tablas, columnas, índices)
--
-- Consolidado e idempotente (re-ejecutable): CREATE sólo si falta, ALTER sólo
-- si la columna no existe, índices sólo si no existen.
--
-- ORDEN: correr este archivo ANTES de 02_views.sql.
-- NOTA: el ETL (etl/dominios/anura/importar.py) autocrea las columnas del CSV
-- en A_Llamada (Direccion, Estado, Nro_Origen, ...). Los índices de reporting
-- que dependen de esas columnas corren sólo si ya existen, así que conviene
-- ejecutar este script con la tabla ya poblada por una primera importación.
--
-- A_Llamada            CDRs crudos + JsonData (1 fila por registro de Anura)
-- fn_TelNorm           normalizador de teléfonos (área+número, 10-11 dígitos)
-- A_Voz                plantillas de voice-ID (centroides ECAPA)
-- A_Conversacion       cabecera por llamada grabada (CdrId UNIQUE)
-- A_ConversacionTurno  diálogo turno por turno
-- R_Conversacion_Voz   puente N:M conversación <-> voces
-- A_ContactoDetectado  historial de menciones de contacto (append-only)
-- A_ContactoTelefono   estado actual teléfono <-> contacto
-- A_ConversacionAnalisis  resumen/categoría por modelo (Ollama)
-- A_PerdidaRacha       rachas de perdidas materializadas
-- ============================================================================

-- ----------------------------------------------------------------------------
-- A_Llamada (núcleo). El ETL agrega el resto de columnas del CSV.
-- ----------------------------------------------------------------------------
IF OBJECT_ID('[dbo].[A_Llamada]', 'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[A_Llamada] (
        Id INT IDENTITY(1,1) PRIMARY KEY,
        FechaImportacion DATETIME NOT NULL DEFAULT GETDATE()
    );
END
GO

IF COL_LENGTH('[dbo].[A_Llamada]', 'FechaDT') IS NULL
    ALTER TABLE [dbo].[A_Llamada] ADD FechaDT DATETIME2(3) NULL;
GO

-- Raw Fecha del CSV (texto original; FechaDT es el parseado)
IF COL_LENGTH('[dbo].[A_Llamada]', 'Fecha') IS NULL
    ALTER TABLE [dbo].[A_Llamada] ADD Fecha NVARCHAR(MAX) NULL;
GO

-- Snapshot JSON por fila
IF COL_LENGTH('[dbo].[A_Llamada]', 'JsonData') IS NULL
    ALTER TABLE [dbo].[A_Llamada] ADD JsonData NVARCHAR(MAX) NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_AnuraLlamadas_FechaDT'
      AND object_id = OBJECT_ID('[dbo].[A_Llamada]')
)
    CREATE INDEX IX_AnuraLlamadas_FechaDT ON [dbo].[A_Llamada](FechaDT);
GO

-- ----------------------------------------------------------------------------
-- fn_TelNorm + columnas computadas persistidas (base de devoluciones y rachas)
-- Determinística + schemabound => indexable.
-- ----------------------------------------------------------------------------
CREATE OR ALTER FUNCTION dbo.fn_TelNorm(@d NVARCHAR(MAX))
RETURNS NVARCHAR(11)
WITH SCHEMABINDING
AS
BEGIN
    DECLARE @s NVARCHAR(MAX) = @d;
    IF @s IS NULL OR @s = '' RETURN NULL;
    IF LEN(@s) > 11 AND LEFT(@s, 2) = '54' SET @s = SUBSTRING(@s, 3, 60);
    IF LEN(@s) > 10 AND LEFT(@s, 1) = '9' SET @s = SUBSTRING(@s, 2, 60);
    IF LEN(@s) > 10 AND LEFT(@s, 1) = '0' SET @s = SUBSTRING(@s, 2, 60);
    -- infijo 15 movil: area (2-4) + '15' + numero (6-8), solo si sobran digitos
    IF LEN(@s) > 10 AND (
           (SUBSTRING(@s, 3, 2) = '15' AND LEN(@s) - 4 BETWEEN 6 AND 8)
        OR (SUBSTRING(@s, 4, 2) = '15' AND LEN(@s) - 5 BETWEEN 6 AND 8)
        OR (SUBSTRING(@s, 5, 2) = '15' AND LEN(@s) - 6 BETWEEN 6 AND 8))
    BEGIN
        IF SUBSTRING(@s, 3, 2) = '15' AND LEN(@s) - 4 BETWEEN 6 AND 8
            SET @s = SUBSTRING(@s, 1, 2) + SUBSTRING(@s, 5, 60);
        ELSE IF SUBSTRING(@s, 4, 2) = '15' AND LEN(@s) - 5 BETWEEN 6 AND 8
            SET @s = SUBSTRING(@s, 1, 3) + SUBSTRING(@s, 6, 60);
        ELSE IF SUBSTRING(@s, 5, 2) = '15' AND LEN(@s) - 6 BETWEEN 6 AND 8
            SET @s = SUBSTRING(@s, 1, 4) + SUBSTRING(@s, 7, 60);
    END
    IF LEN(@s) IN (10, 11) RETURN CAST(@s AS NVARCHAR(11));
    RETURN NULL;
END
GO

-- Las columnas computadas dependen de columnas que crea el ETL en la primera
-- importación: en una base limpia se omiten hasta que existan.
IF COL_LENGTH('[dbo].[A_Llamada]', 'Nro_Origen') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'TelNorm') IS NULL
    ALTER TABLE [dbo].[A_Llamada]
        ADD TelNorm AS (dbo.fn_TelNorm(Nro_Origen)) PERSISTED;
GO

IF COL_LENGTH('[dbo].[A_Llamada]', 'Nro_Destino_Limpio') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'DestNorm') IS NULL
    ALTER TABLE [dbo].[A_Llamada]
        ADD DestNorm AS (dbo.fn_TelNorm(Nro_Destino_Limpio)) PERSISTED;
GO

-- ----------------------------------------------------------------------------
-- A_Voz: padrón de personas identificadas (plantillas de voice-ID).
-- Requiere tipo VECTOR (Azure SQL). Embeddings ECAPA 192-dims L2-normalizados;
-- ModeloVersion obligatoria (modelos distintos NO comparan).
-- ----------------------------------------------------------------------------
IF OBJECT_ID('[dbo].[A_Voz]', 'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[A_Voz] (
        Id INT IDENTITY(1,1) PRIMARY KEY,
        Nombre NVARCHAR(120) NOT NULL,
        EsTecnico BIT NOT NULL DEFAULT 0,
        Centroide VECTOR(192) NOT NULL,
        ModeloVersion NVARCHAR(40) NOT NULL,
        Umbral FLOAT NOT NULL,
        Clips INT NOT NULL DEFAULT 0,
        FechaActualizacion DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        Activo BIT NOT NULL DEFAULT 1
    );
END
GO

-- Auditoría de voces (contadores de menciones)
IF COL_LENGTH('[dbo].[A_Voz]', 'Veces') IS NULL
    ALTER TABLE [dbo].[A_Voz] ADD Veces INT NOT NULL DEFAULT 0;
GO
IF COL_LENGTH('[dbo].[A_Voz]', 'PrimeraVez') IS NULL
    ALTER TABLE [dbo].[A_Voz] ADD PrimeraVez DATETIME2 NULL;
GO
IF COL_LENGTH('[dbo].[A_Voz]', 'UltimaVez') IS NULL
    ALTER TABLE [dbo].[A_Voz] ADD UltimaVez DATETIME2 NULL;
GO
IF COL_LENGTH('[dbo].[A_Voz]', 'Estado') IS NULL
    ALTER TABLE [dbo].[A_Voz] ADD Estado NVARCHAR(20) NOT NULL DEFAULT 'confirmado';
GO

-- UNIQUE (Nombre, ModeloVersion) en modo FILTRADO: 'Sin identificar' se repite.
-- (Antes era un CONSTRAINT UQ_A_Voz total; se suelta si existe.)
IF EXISTS (
    SELECT 1 FROM sys.objects
    WHERE name = 'UQ_A_Voz' AND parent_object_id = OBJECT_ID('[dbo].[A_Voz]')
)
    ALTER TABLE [dbo].[A_Voz] DROP CONSTRAINT [UQ_A_Voz];
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'UQ_A_Voz_Nombre' AND object_id = OBJECT_ID('[dbo].[A_Voz]')
)
BEGIN
    CREATE UNIQUE INDEX UQ_A_Voz_Nombre
    ON [dbo].[A_Voz](Nombre, ModeloVersion)
    WHERE Nombre <> N'Sin identificar';
END
GO

-- ----------------------------------------------------------------------------
-- A_Conversacion: 1 fila por llamada grabada (CdrId es la clave idempotente)
-- ----------------------------------------------------------------------------
IF OBJECT_ID('[dbo].[A_Conversacion]', 'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[A_Conversacion] (
        Id INT IDENTITY(1,1) PRIMARY KEY,
        CdrId BIGINT NOT NULL UNIQUE,
        A_Llamada_Id INT NULL,
        Fecha DATETIME2(3) NOT NULL,
        DuracionSeg INT NULL,
        RutaAudio NVARCHAR(500) NULL,
        Transcripto NVARCHAR(MAX) NULL,
        EmbTecnico VECTOR(192) NULL,
        ModeloTranscripcion NVARCHAR(40) NOT NULL,
        ModeloVersion NVARCHAR(40) NOT NULL,
        FechaProceso DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

-- Calidad de transcripto (OK/DUDOSA/MALA) y fracción de segmentos descartados
IF COL_LENGTH('[dbo].[A_Conversacion]', 'CalidadTransc') IS NULL
    ALTER TABLE [dbo].[A_Conversacion] ADD CalidadTransc NVARCHAR(10) NULL;
GO
IF COL_LENGTH('[dbo].[A_Conversacion]', 'PctBasura') IS NULL
    ALTER TABLE [dbo].[A_Conversacion] ADD PctBasura FLOAT NULL;
GO

-- Contestador/buzón (solo por oído humano o frase del sistema). NULL = sin revisar.
IF COL_LENGTH('[dbo].[A_Conversacion]', 'EsBuzon') IS NULL
    ALTER TABLE [dbo].[A_Conversacion] ADD EsBuzon BIT NULL;
GO

-- Canal del técnico (L/R) y candidato por proximidad (no atribuye)
IF COL_LENGTH('[dbo].[A_Conversacion]', 'CanalTecnico') IS NULL
    ALTER TABLE [dbo].[A_Conversacion] ADD CanalTecnico NVARCHAR(1) NULL;
GO
IF COL_LENGTH('[dbo].[A_Conversacion]', 'CandidatoTecnico') IS NULL
    ALTER TABLE [dbo].[A_Conversacion] ADD CandidatoTecnico NVARCHAR(120) NULL;
GO
IF COL_LENGTH('[dbo].[A_Conversacion]', 'CandidatoDist') IS NULL
    ALTER TABLE [dbo].[A_Conversacion] ADD CandidatoDist FLOAT NULL;
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_A_Conversacion_Fecha'
      AND object_id = OBJECT_ID('[dbo].[A_Conversacion]')
)
    CREATE INDEX IX_A_Conversacion_Fecha ON [dbo].[A_Conversacion](Fecha);
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_A_Conversacion_Llamada'
      AND object_id = OBJECT_ID('[dbo].[A_Conversacion]')
)
    CREATE INDEX IX_A_Conversacion_Llamada ON [dbo].[A_Conversacion](A_Llamada_Id);
GO

-- ----------------------------------------------------------------------------
-- R_Conversacion_Voz: puente N:M (admite >2 voces y VozId NULL)
-- ----------------------------------------------------------------------------
IF OBJECT_ID('[dbo].[R_Conversacion_Voz]', 'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[R_Conversacion_Voz] (
        Id INT IDENTITY(1,1) PRIMARY KEY,
        ConversacionId INT NOT NULL REFERENCES [dbo].[A_Conversacion](Id),
        EtiquetaLocal NVARCHAR(10) NOT NULL,
        VozId INT NULL REFERENCES [dbo].[A_Voz](Id),
        Rol NVARCHAR(10) NOT NULL,
        EsPrincipal BIT NOT NULL DEFAULT 0,
        Confianza NVARCHAR(10) NOT NULL,
        Distancia FLOAT NULL,
        Turnos INT NOT NULL DEFAULT 0,
        Segundos FLOAT NOT NULL DEFAULT 0
    );
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_R_Conversacion_Voz_Conv'
      AND object_id = OBJECT_ID('[dbo].[R_Conversacion_Voz]')
)
    CREATE INDEX IX_R_Conversacion_Voz_Conv
    ON [dbo].[R_Conversacion_Voz](ConversacionId);
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_R_Conversacion_Voz_Voz'
      AND object_id = OBJECT_ID('[dbo].[R_Conversacion_Voz]')
)
    CREATE INDEX IX_R_Conversacion_Voz_Voz ON [dbo].[R_Conversacion_Voz](VozId);
GO

-- ----------------------------------------------------------------------------
-- A_ConversacionTurno: diálogo turno por turno (permite re-atribuir por UPDATE)
-- ----------------------------------------------------------------------------
IF OBJECT_ID('[dbo].[A_ConversacionTurno]', 'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[A_ConversacionTurno] (
        Id BIGINT IDENTITY(1,1) PRIMARY KEY,
        ConversacionId INT NOT NULL REFERENCES [dbo].[A_Conversacion](Id),
        Nro INT NOT NULL,
        InicioSeg FLOAT NOT NULL,
        FinSeg FLOAT NOT NULL,
        VozId INT NULL REFERENCES [dbo].[A_Voz](Id),
        EtiquetaLocal NVARCHAR(10) NOT NULL,
        Rol NVARCHAR(10) NOT NULL,
        Texto NVARCHAR(MAX) NOT NULL,
        Confianza FLOAT NULL
    );
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_A_ConversacionTurno_Conv'
      AND object_id = OBJECT_ID('[dbo].[A_ConversacionTurno]')
)
    CREATE INDEX IX_A_ConversacionTurno_Conv
    ON [dbo].[A_ConversacionTurno](ConversacionId, Nro);
GO

-- ----------------------------------------------------------------------------
-- Contactos. NOTA: `D_Contacto` pertenece al núcleo DELI (repo `deli`);
-- si no existe, las tablas se crean sin FK y el vínculo queda pendiente.
-- ----------------------------------------------------------------------------
IF OBJECT_ID('[dbo].[A_ContactoDetectado]', 'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[A_ContactoDetectado] (
        Id BIGINT IDENTITY(1,1) PRIMARY KEY,
        Fecha DATETIME2(3) NOT NULL,
        A_Llamada_Id INT NULL,
        CdrId BIGINT NULL,
        ConversacionId INT NULL REFERENCES [dbo].[A_Conversacion](Id),
        Telefono NVARCHAR(50) NULL,       -- NULL en menciones de técnicos
        Nombre NVARCHAR(100) NULL,
        RazonSocial NVARCHAR(100) NULL,
        Cuit CHAR(11) NULL,
        Fuente NVARCHAR(20) NOT NULL,     -- origen|transcript|ticket|manual
        Confianza FLOAT NULL,
        TextoCrudo NVARCHAR(400) NULL,
        ContactoId INT NULL,
        Promovido BIT NOT NULL DEFAULT 0,
        FechaPromocion DATETIME2 NULL,
        FechaAlta DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

IF COL_LENGTH('[dbo].[A_ContactoDetectado]', 'VozId') IS NULL
    ALTER TABLE [dbo].[A_ContactoDetectado] ADD VozId INT NULL;
GO
IF COL_LENGTH('[dbo].[A_ContactoDetectado]', 'EsTecnico') IS NULL
    ALTER TABLE [dbo].[A_ContactoDetectado] ADD EsTecnico BIT NULL;
GO

IF OBJECT_ID('[dbo].[D_Contacto]', 'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys
                   WHERE name = 'FK_A_ContactoDetectado_D_Contacto')
BEGIN
    ALTER TABLE [dbo].[A_ContactoDetectado]
        ADD CONSTRAINT FK_A_ContactoDetectado_D_Contacto
        FOREIGN KEY (ContactoId) REFERENCES [dbo].[D_Contacto](IdContacto);
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_A_ContactoDetectado_Telefono'
      AND object_id = OBJECT_ID('[dbo].[A_ContactoDetectado]')
)
    CREATE INDEX IX_A_ContactoDetectado_Telefono
    ON [dbo].[A_ContactoDetectado](Telefono);
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_A_ContactoDetectado_CdrId'
      AND object_id = OBJECT_ID('[dbo].[A_ContactoDetectado]')
)
    CREATE INDEX IX_A_ContactoDetectado_CdrId ON [dbo].[A_ContactoDetectado](CdrId);
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_A_ContactoDetectado_VozId'
      AND object_id = OBJECT_ID('[dbo].[A_ContactoDetectado]')
)
    CREATE INDEX IX_A_ContactoDetectado_VozId ON [dbo].[A_ContactoDetectado](VozId);
GO

IF OBJECT_ID('[dbo].[A_ContactoTelefono]', 'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[A_ContactoTelefono] (
        Id INT IDENTITY(1,1) PRIMARY KEY,
        Telefono NVARCHAR(50) NOT NULL,
        ContactoId INT NOT NULL,
        Nombre NVARCHAR(100) NULL,
        RazonSocial NVARCHAR(100) NULL,
        Fuente NVARCHAR(20) NULL,
        Confianza FLOAT NULL,
        Veces INT NOT NULL DEFAULT 0,
        PrimeraVez DATETIME2(3) NULL,
        UltimaVez DATETIME2(3) NULL,
        EsPrincipal BIT NOT NULL DEFAULT 0,
        Estado NVARCHAR(20) NOT NULL DEFAULT 'activo',  -- activo|revision|superado
        EnviadoAnura BIT NOT NULL DEFAULT 0,
        NombreEnviadoAnura NVARCHAR(100) NULL,
        FechaEnvioAnura DATETIME2 NULL,
        FechaActualizacion DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_A_ContactoTelefono UNIQUE (Telefono, ContactoId)
    );
END
GO

IF OBJECT_ID('[dbo].[D_Contacto]', 'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys
                   WHERE name = 'FK_A_ContactoTelefono_D_Contacto')
BEGIN
    ALTER TABLE [dbo].[A_ContactoTelefono]
        ADD CONSTRAINT FK_A_ContactoTelefono_D_Contacto
        FOREIGN KEY (ContactoId) REFERENCES [dbo].[D_Contacto](IdContacto);
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_A_ContactoTelefono_Telefono'
      AND object_id = OBJECT_ID('[dbo].[A_ContactoTelefono]')
)
    CREATE INDEX IX_A_ContactoTelefono_Telefono
    ON [dbo].[A_ContactoTelefono](Telefono);
GO

-- Vínculo voz del cliente -> contacto (si D_Contacto existe)
IF COL_LENGTH('[dbo].[A_Voz]', 'ContactoId') IS NULL
    ALTER TABLE [dbo].[A_Voz] ADD ContactoId INT NULL;
GO
IF OBJECT_ID('[dbo].[D_Contacto]', 'U') IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_A_Voz_D_Contacto')
BEGIN
    ALTER TABLE [dbo].[A_Voz]
        ADD CONSTRAINT FK_A_Voz_D_Contacto
        FOREIGN KEY (ContactoId) REFERENCES [dbo].[D_Contacto](IdContacto);
END
GO

-- Columnas de enriquecimiento en D_Contacto (nullable, bajo riesgo)
IF OBJECT_ID('[dbo].[D_Contacto]', 'U') IS NOT NULL
BEGIN
    IF COL_LENGTH('[dbo].[D_Contacto]', 'RazonSocial') IS NULL
        ALTER TABLE [dbo].[D_Contacto] ADD RazonSocial NVARCHAR(100) NULL;
    IF COL_LENGTH('[dbo].[D_Contacto]', 'Origen') IS NULL
        ALTER TABLE [dbo].[D_Contacto] ADD Origen VARCHAR(20) NULL;
    IF COL_LENGTH('[dbo].[D_Contacto]', 'FechaActualizacion') IS NULL
        ALTER TABLE [dbo].[D_Contacto] ADD FechaActualizacion DATETIME2 NULL;
END
GO

-- ----------------------------------------------------------------------------
-- A_ConversacionAnalisis: resumen/categoría por (conversación, modelo)
-- ----------------------------------------------------------------------------
IF OBJECT_ID('[dbo].[A_ConversacionAnalisis]', 'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[A_ConversacionAnalisis] (
        Id INT IDENTITY(1,1) PRIMARY KEY,
        ConversacionId INT NOT NULL REFERENCES [dbo].[A_Conversacion](Id),
        Resumen NVARCHAR(400) NULL,
        Categoria NVARCHAR(60) NULL,
        Subcategoria NVARCHAR(60) NULL,
        Motivo NVARCHAR(60) NULL,
        Modelo NVARCHAR(60) NOT NULL,
        Confianza FLOAT NULL,
        FechaAnalisis DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT UQ_A_ConversacionAnalisis UNIQUE (ConversacionId, Modelo)
    );
END
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_A_ConversacionAnalisis_Conv'
      AND object_id = OBJECT_ID('[dbo].[A_ConversacionAnalisis]')
)
    CREATE INDEX IX_A_ConversacionAnalisis_Conv
    ON [dbo].[A_ConversacionAnalisis](ConversacionId);
GO

IF NOT EXISTS (
    SELECT 1 FROM sys.indexes
    WHERE name = 'IX_A_ConversacionAnalisis_Categoria'
      AND object_id = OBJECT_ID('[dbo].[A_ConversacionAnalisis]')
)
    CREATE INDEX IX_A_ConversacionAnalisis_Categoria
    ON [dbo].[A_ConversacionAnalisis](Categoria);
GO

-- ----------------------------------------------------------------------------
-- Performance de Power BI: estrechar columnas de filtro (NVARCHAR(MAX) no se
-- puede indexar) y crear los covering de las vistas de reporting.
-- OJO: ALTER COLUMN de MAX a corto reescribe A_Llamada (minutos en tablas
-- grandes) y hay que soltar/recrear IX_AnuraLlamadas_InAnswer.
-- ----------------------------------------------------------------------------
IF EXISTS (SELECT 1 FROM sys.indexes
           WHERE name='IX_AnuraLlamadas_InAnswer'
             AND object_id=OBJECT_ID('[dbo].[A_Llamada]'))
    DROP INDEX IX_AnuraLlamadas_InAnswer ON [dbo].[A_Llamada];
GO

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME='A_Llamada' AND COLUMN_NAME='Direccion'
             AND CHARACTER_MAXIMUM_LENGTH = -1)
    EXEC('ALTER TABLE [dbo].[A_Llamada] ALTER COLUMN [Direccion] NVARCHAR(10) NULL');
GO

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME='A_Llamada' AND COLUMN_NAME='Ultima_accion'
             AND CHARACTER_MAXIMUM_LENGTH = -1)
    EXEC('ALTER TABLE [dbo].[A_Llamada] ALTER COLUMN [Ultima_accion] NVARCHAR(50) NULL');
GO

IF EXISTS (SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
           WHERE TABLE_NAME='A_Llamada' AND COLUMN_NAME='Destino'
             AND CHARACTER_MAXIMUM_LENGTH = -1)
    EXEC('ALTER TABLE [dbo].[A_Llamada] ALTER COLUMN [Destino] NVARCHAR(200) NULL');
GO

-- Índice filtrado de entrantes atendidas (IX_AnuraLlamadas_InAnswer)
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name='IX_AnuraLlamadas_InAnswer'
                 AND object_id=OBJECT_ID('[dbo].[A_Llamada]'))
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Nro_Destino_Limpio') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Direccion') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Estado') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Cuenta_contesto') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Duracion_total') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Nro_Origen') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Origen') IS NOT NULL
BEGIN
    CREATE NONCLUSTERED INDEX IX_AnuraLlamadas_InAnswer
    ON [dbo].[A_Llamada]([Nro_Destino_Limpio], [Fecha])
    INCLUDE ([Id], [Duracion_total], [Nro_Origen], [Origen], [Cuenta_contesto])
    WHERE [Direccion]='IN' AND [Estado]='ANSWER' AND [Cuenta_contesto] IS NOT NULL;
END
GO

-- Covering para V_A_LlamadaPerdida (filtro + columnas proyectadas)
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_A_Llamada_Perdidas'
                 AND object_id = OBJECT_ID('[dbo].[A_Llamada]'))
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Direccion') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Ultima_accion') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Destino') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Duracion_total') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Nro_Origen') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Origen') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Nro_Destino_Limpio') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Estado') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Duracion_conversacion') IS NOT NULL
BEGIN
    CREATE NONCLUSTERED INDEX IX_A_Llamada_Perdidas
    ON [dbo].[A_Llamada]([Direccion], [Ultima_accion], [Destino])
    INCLUDE ([Fecha], [Duracion_total], [Nro_Origen], [Origen],
             [Nro_Destino_Limpio], [Estado], [Duracion_conversacion]);
END
GO

-- Índices para el EXISTS de devolución (TelNorm/DestNorm)
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_A_Llamada_TelNorm'
                 AND object_id = OBJECT_ID('[dbo].[A_Llamada]'))
   AND COL_LENGTH('[dbo].[A_Llamada]', 'TelNorm') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Direccion') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'FechaDT') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Estado') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Ultima_accion') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Cuenta_contesto') IS NOT NULL
BEGIN
    CREATE NONCLUSTERED INDEX IX_A_Llamada_TelNorm
    ON [dbo].[A_Llamada](Direccion, TelNorm, FechaDT)
    INCLUDE (Estado, Ultima_accion, Cuenta_contesto);
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_A_Llamada_DestNorm'
                 AND object_id = OBJECT_ID('[dbo].[A_Llamada]'))
   AND COL_LENGTH('[dbo].[A_Llamada]', 'DestNorm') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Direccion') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'FechaDT') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Estado') IS NOT NULL
BEGIN
    CREATE NONCLUSTERED INDEX IX_A_Llamada_DestNorm
    ON [dbo].[A_Llamada](Direccion, DestNorm, FechaDT)
    INCLUDE (Estado);
END
GO

-- Covering de V_A_PerdidaRacha (sessionización por teléfono)
IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_A_Llamada_PerdidasTel'
                 AND object_id = OBJECT_ID('[dbo].[A_Llamada]'))
   AND COL_LENGTH('[dbo].[A_Llamada]', 'TelNorm') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Direccion') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Ultima_accion') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Destino') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'FechaDT') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Nro_Origen') IS NOT NULL
   AND COL_LENGTH('[dbo].[A_Llamada]', 'Origen') IS NOT NULL
BEGIN
    CREATE NONCLUSTERED INDEX IX_A_Llamada_PerdidasTel
    ON [dbo].[A_Llamada](Direccion, Ultima_accion, Destino)
    INCLUDE (TelNorm, FechaDT, Nro_Origen, Origen);
END
GO

-- ----------------------------------------------------------------------------
-- A_PerdidaRacha: materialización de las rachas (la llena el SP de 02_views.sql)
-- ----------------------------------------------------------------------------
IF OBJECT_ID('[dbo].[A_PerdidaRacha]', 'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[A_PerdidaRacha] (
        RachaId            NVARCHAR(60)   NOT NULL PRIMARY KEY,
        Telefono           NVARCHAR(11)   NOT NULL,
        Cliente            NVARCHAR(MAX)  NULL,
        Dia                DATE           NOT NULL,
        Hora               INT            NOT NULL,
        HoraTxt            NVARCHAR(5)    NOT NULL,
        Turno              NVARCHAR(20)   NOT NULL,
        PrimeraFecha       DATETIME2      NOT NULL,
        UltimaFecha        DATETIME2      NOT NULL,
        Cantidad           INT            NOT NULL,
        Resultado          NVARCHAR(30)   NOT NULL,
        DejoMensaje        NVARCHAR(2)    NOT NULL,
        Devuelta           NVARCHAR(2)    NOT NULL,
        FechaDevuelta      DATETIME2      NULL,
        TipoDevuelve       NVARCHAR(10)   NULL,
        TecDevuelve        NVARCHAR(120)  NULL,
        ConfDevuelve       NVARCHAR(10)   NULL,
        CuentaDevuelve     NVARCHAR(120)  NULL,
        TerminalDevuelve   NVARCHAR(120)  NULL,
        TecnicosTurno      NVARCHAR(MAX)  NULL,
        N_TecnicosTurno    INT            NOT NULL,
        ActualizadoEn      DATETIME2      NOT NULL
    );
    CREATE INDEX IX_A_PerdidaRacha_Dia ON [dbo].[A_PerdidaRacha](Dia);
END
GO

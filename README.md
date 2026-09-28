# deli-anura-cdr-etl — CDRs Anura → Azure SQL

Importación **incremental** de Call Detail Records (CDRs) desde la API GCAPI de
Anura hacia SQL Server (`dbo.A_Llamada`). Cada ejecución trae solo las llamadas
nuevas desde la última importación y las vuelca normalizadas + snapshot JSON
por fila.

## Qué hace

1. Se autentica contra Anura (OAuth2 Keycloak → token GCAPI).
2. Descarga el CSV de CDRs (`ExportCdrs`) con ventana incremental.
3. Normaliza columnas, deduce `FechaDT`, deduplica y hace `APPEND` a
   `[dbo].[A_Llamada]` (crea la tabla/columnas que falten con `ALTER TABLE`).
4. Expone reporting con vistas (`V_A_Llamada`, `V_A_LlamadaPerdida`).

## Cómo funciona

```
MAX(FechaDT) en A_Llamada + 1ms  →  ahora
        │                              │
        ▼                              ▼
  GET GCAPI/Download/ExportCdrs (CSV)
        │
        ▼
  pandas: normalizar columnas → FechaDT → dedup → JsonData
        │
        ▼
  to_sql(APPEND, chunksize=1000) → dbo.A_Llamada
```

* **Auth** (`etl/conn/anura_client.py`): `POST openid-connect/token` con
  `Basic CLIENT_ID:PASSWORD` → `access_token`; luego
  `AdminService.getDownloadToken` → token de descarga. Timeout 60s + 3
  reintentos con backoff.
* **Ventana incremental** (`etl/dominios/anura/importar.py`): si la tabla está
  vacía arranca en `2026-01-01`; si no, desde `MAX(FechaDT) + 1 milisegundo`.
* **Idempotencia**: filtro `FechaDT > ultima_fecha` + dedup por `FechaDT`.
  Re-correr es seguro.
* **Columnas dinámicas**: el CSV de Anura evoluciona; toda columna nueva se
  agrega sola como `NVARCHAR(MAX)` (`FechaDT` como `DATETIME2(3)`).
* **JsonData**: cada fila guarda además su snapshot completo en JSON.

## Esquema SQL (`sql/`)

| Script | Contenido |
|---|---|
| `sql/001_create_A_Llamada.sql` | Bootstrap idempotente: tabla base (`Id`, `FechaImportacion`, `FechaDT`, `Fecha`, `JsonData`) + índice `IX_AnuraLlamadas_FechaDT`. El resto de columnas las autocrea el ETL. |
| `sql/002_views.sql` | `V_A_Llamada` (entrantes atendidas) y `V_A_LlamadaPerdida` (perdidas con/sin mensaje). Usan `TRY_CAST` para que una fila con duración no numérica no rompa la vista. |

Aplicar en orden con `sqlcmd -i` o SSMS. Ambos scripts son re-ejecutables.

## Requisitos

* Python ≥ 3.12
* ODBC Driver 18 for SQL Server
* SQL Server / Azure SQL con acceso de escritura a `dbo`
* Credenciales Anura (`CLIENT_ID` / `CLIENT_PASSWORD`)

## Uso

```
pip install -e .
copy .env.template .env   # completar las 6 variables (nunca commitear .env)
py -u -m cli.importar_anura
```

Salida esperada: tablero con `LLAMADAS AGREGADAS` y `ERRORES: 0`.
Para producción, programarlo (Task Scheduler / cron) cada 15–60 min; cada
corrida posterior es incremental y liviana.

## Configuración (`.env`)

| Variable | Uso |
|---|---|
| `CLIENT_ID` / `CLIENT_PASSWORD` | OAuth Keycloak → GCAPI Anura |
| `SQL_SERVER` / `SQL_DATABASE` | Destino (tabla `dbo.A_Llamada`) |
| `SQL_USERNAME` / `SQL_PASSWORD` | Auth SQL Server |

Al importar, si falta alguna el proceso aborta con mensaje claro (fail-fast).

## Estructura

```
cli/importar_anura.py        # entrypoint: python -m cli.importar_anura
config/settings.py           # lee .env + valida requeridas
etl/conn/anura_client.py     # OAuth + descarga CSV (timeout/retry)
etl/conn/db.py               # engine SQLAlchemy/pyodbc
etl/dominios/anura/importar.py  # ETL pandas → A_Llamada
sql/                         # DDL idempotente (tabla + vistas)
```

## Seguridad

* `.env` está en `.gitignore` y nunca se commitea (ver `.env.template`).
* Las vistas exponen teléfonos/nombres: restringir `GRANT SELECT` en la DB.
* Se recomienda activar *Secret scanning + Push protection* en GitHub.

## Licencia

MIT — ver `LICENSE`. Uso libre con atribución (conservar aviso de copyright).

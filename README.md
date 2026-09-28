# deli-anura-cdr-etl — CDRs Anura → Azure SQL

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)

Importación **incremental** de Call Detail Records (CDRs) desde la API GCAPI de
Anura hacia SQL Server / Azure SQL (`dbo.A_Llamada`). Cada ejecución trae solo
las llamadas nuevas desde la última importación y las vuelca normalizadas, con
un snapshot JSON por fila. Corre como **CLI** o como **Azure Function** (timer).

---

## Índice

- [Qué hace](#qué-hace)
- [Cómo funciona](#cómo-funciona)
- [Requisitos](#requisitos)
- [Instalación y uso (local)](#instalación-y-uso-local)
- [Configuración (`.env`)](#configuración-env)
- [Esquema SQL](#esquema-sql)
- [Azure Function (timer)](#azure-function-timer)
- [Estructura](#estructura)
- [Seguridad](#seguridad)
- [Solución de problemas](#solución-de-problemas)
- [Licencia](#licencia)

---

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
  `AdminService.getDownloadToken` → token de descarga. Timeout 60 s y 3
  reintentos con backoff exponencial (`2^intento`).
* **Ventana incremental** (`etl/dominios/anura/importar.py`): si la tabla está
  vacía arranca en `2026-01-01`; si no, desde `MAX(FechaDT) + 1 milisegundo`.
* **Idempotencia**: filtro `FechaDT > ultima_fecha` + dedup por `FechaDT`
  (y por `FechaKey` a milisegundos contra lo ya guardado). Re-correr es seguro.
* **Columnas dinámicas**: el CSV de Anura evoluciona; toda columna nueva se
  agrega sola como `NVARCHAR(MAX)` (`FechaDT` como `DATETIME2(3)`).
* **JsonData**: cada fila guarda además su snapshot completo en JSON.

## Requisitos

* Python **≥ 3.12**
* **ODBC Driver 18 for SQL Server**
* SQL Server / Azure SQL con permiso de escritura en `dbo`
* Credenciales Anura (`CLIENT_ID` / `CLIENT_PASSWORD`)

## Instalación y uso (local)

```bash
pip install -e .
copy .env.template .env   # completar las 6 variables (nunca commitear .env)
py -u -m cli.importar_anura
```

Salida esperada: tablero con `LLAMADAS AGREGADAS` y `ERRORES: 0`.

Para producción podés usar la **Azure Function** (ver abajo) o programarlo
(Task Scheduler / cron) cada 15–60 min; cada corrida posterior es incremental
y liviana.

## Configuración (`.env`)

| Variable | Uso |
|---|---|
| `CLIENT_ID` / `CLIENT_PASSWORD` | OAuth Keycloak → GCAPI Anura |
| `SQL_SERVER` / `SQL_DATABASE` | Destino (tabla `dbo.A_Llamada`) |
| `SQL_USERNAME` / `SQL_PASSWORD` | Auth SQL Server |

Si falta alguna, el proceso **aborta con mensaje claro** (fail-fast) en vez de
fallar más tarde con un 401 o un error de conexión.

## Esquema SQL

| Script | Contenido |
|---|---|
| `sql/001_create_A_Llamada.sql` | Bootstrap idempotente: tabla base (`Id`, `FechaImportacion`, `FechaDT`, `Fecha`, `JsonData`) + índice `IX_AnuraLlamadas_FechaDT`. El resto de columnas las autocrea el ETL. |
| `sql/002_views.sql` | `V_A_Llamada` (entrantes atendidas) y `V_A_LlamadaPerdida` (perdidas con/sin mensaje). Usan `TRY_CAST` para que una fila con duración no numérica no rompa la vista. |

Aplicar en orden con `sqlcmd -i` o SSMS. Ambos scripts son re-ejecutables
(normalmente no hace falta: la primera importación crea lo necesario).

## Azure Function (timer)

El repo también corre como **Azure Function** (Python v2, timer horario).
No usa `.env` en Azure: las credenciales se cargan como **Application settings**.

| Archivo | Rol |
|---|---|
| `function_app.py` | Timer `0 0 * * * *` (cada hora, minuto 0) → llama a `etl.dominios.anura.importar.main`. `run_on_startup=False`. |
| `host.json` | Configuración del runtime (bundle de extensiones v4). |
| `requirements.txt` | Dependencias del deployment. |
| `.funcignore` | Excluye del deploy `.env`, tests, `__pycache__`, etc. |

### Requisitos en Azure

1. **Function App** Python (runtime 3.12) — Linux o Windows.
2. **ODBC Driver 18 for SQL Server** en el host (necesario para `pyodbc`).
3. **Application settings** con: `CLIENT_ID`, `CLIENT_PASSWORD`,
   `SQL_SERVER`, `SQL_DATABASE`, `SQL_USERNAME`, `SQL_PASSWORD`.

### Deploy

```bash
func azure functionapp publish <FUNCION_APP>
```

### Verificación

* Log en vivo: `func azure functionapp logstream <FUNCION_APP>`.
* El log de la invocación debe terminar con
  `importar_anura_timer: fin OK`.
* Chequear en la base: `SELECT MAX(FechaDT) FROM dbo.A_Llamada;` (debe avanzar).

## Estructura

```
function_app.py                 # Azure Function (timer horario) -> ETL Anura
host.json                       # config del runtime de Functions
requirements.txt                # dependencias del deployment
.funcignore                     # qué NO se sube al deploy
cli/importar_anura.py           # entrypoint: python -m cli.importar_anura
config/settings.py              # lee .env (local) o Application settings (Azure)
etl/conn/anura_client.py        # OAuth + descarga CSV (timeout/retry)
etl/conn/db.py                  # engine SQLAlchemy/pyodbc
etl/dominios/anura/importar.py  # ETL pandas -> A_Llamada
etl/dominios/tickets/stats.py   # panel de progreso
sql/                            # DDL idempotente (tabla + vistas)
```

## Seguridad

* `.env` está en `.gitignore` y **nunca** se commitea (ver `.env.template`).
* Las vistas exponen teléfonos/nombres: restringir `GRANT SELECT` en la DB y
  usar una cuenta SQL de mínimos privilegios.
* Se recomienda activar *Secret scanning + Push protection* en GitHub.

## Solución de problemas

| Síntoma | Causa / solución |
|---|---|
| `Faltan variables en .env ...` | Completar `.env` local o los *Application settings* en Azure. |
| `401` de Anura | `CLIENT_ID` / `CLIENT_PASSWORD` incorrectos o vencidos. |
| `Can't open lib 'ODBC Driver 18 for SQL Server'` | Falta el ODBC Driver 18 en el host. |
| Duplicados | No debería: el ETL filtra `FechaDT > ultima_fecha` y dedup por `FechaDT`. Si pasa, revisar la ventana y el tipo de `FechaDT`. |
| Cortes de red / timeouts | Los reintentos con backoff ya cubren transitorios; re-correr es seguro (idempotente). |

## Licencia

MIT — ver [`LICENSE`](LICENSE). Uso libre con atribución (conservar aviso de
copyright).

# deli-anura-cdr-etl — CDRs Anura → Azure SQL

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)

Importación **incremental** de Call Detail Records (CDRs) desde la API GCAPI de
Anura hacia SQL Server / Azure SQL (`dbo.A_Llamada`). Cada ejecución trae solo
las llamadas nuevas desde la última importación y las vuelca normalizadas, con
un snapshot JSON por fila. Corre como **CLI** o como **Azure Function** (timer).
Incluye además el pipeline de **voice-ID** (whisper + ECAPA, con revisión humana)
y un proyecto **Power BI** (`.pbip`) generado por script.

---

## Índice

- [Qué hace](#qué-hace)
- [Cómo funciona](#cómo-funciona)
- [Requisitos](#requisitos)
- [Instalación y uso (local)](#instalación-y-uso-local)
- [Configuración (`.env`)](#configuración-env)
- [Esquema SQL](#esquema-sql)
- [Voz: voice-ID y transcripción](#voz-voice-id-y-transcripción)
- [Power BI](#power-bi)
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
4. Expone reporting con vistas (`V_A_Llamada`, `V_A_LlamadaPerdida`,
   `V_A_ConversacionPBI`, ...) y un proyecto Power BI listo para abrir.
5. **Voz** (opcional): descarga la grabación, la transcribe, diariza hablantes y
   atribuye el técnico por voz; lo dudoso queda en cola para revisión humana.

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
| `VOZ_*` (opcional) | Rutas, modelo y umbrales del pipeline de voz; defaults en `config/settings.py` |
| `PBI_SQL_SERVER` / `PBI_SQL_DATABASE` (opcional) | Host/base que hornea `tools/build_pbip.py`; sin ellas usa un placeholder |

Si falta alguna de las 6 credenciales, el proceso **aborta con mensaje claro**
(fail-fast) en vez de fallar más tarde con un 401 o un error de conexión. Las
variables opcionales nunca rompen el ETL de CDRs.

## Esquema SQL

| Script | Contenido |
|---|---|
| `sql/01_schema.sql` | DDL idempotente: `A_Llamada` (núcleo + columnas que autocrea el ETL), `fn_TelNorm` + columnas computadas, `A_Voz`, `A_Conversacion`, `A_ConversacionTurno`, `R_Conversacion_Voz`, `A_ContactoDetectado`, `A_ContactoTelefono`, `A_ConversacionAnalisis`, `A_PerdidaRacha` + índices. |
| `sql/02_views.sql` | Vistas y SP: `V_A_Llamada`, `V_A_LlamadaPerdida`, `V_A_Conversacion`, `V_A_ConversacionPBI` (sin PII), `V_A_ColaVoces`, `V_A_TurnoVoz`, `sp_RebuildPerdidaRacha` + `V_A_PerdidaRacha`, `V_A_PerdidaResp`, `V_A_ColaCandidatos`. |

Aplicar en orden con `sqlcmd -i` o SSMS. Ambos scripts son re-ejecutables y
usan `CREATE OR ALTER`, `IF COL_LENGTH` y `IF NOT EXISTS`. Correr `01_schema.sql`
con `A_Llamada` ya poblada por una primera importación: el ETL autocrea las
columnas del CSV de las que dependen los índices y las vistas. El tipo `VECTOR`
requiere Azure SQL. `A_Contacto*` referencia opcionalmente a `D_Contacto` (tabla
del núcleo DELI): si no existe, las tablas se crean sin FK.

## Voz: voice-ID y transcripción

```bash
py -u -m cli.atribuir_voz --semilla --desde 2026-09-28 --hasta 2026-09-28 --limite 20
```

Por llamada con grabación (IN + MULTIDIAL/DIAL + `wasRecorded`; más fallback a
la pierna OUT si la IN no se expone en la API, con guard anti-duplicados por
Fecha/Duración): descarga el `.ogg` a `<VOZ_AUDIO_ROOT>/YYYY-MM-DD/` (default:
`audios/` del pack en el repo hermano `deli-support-docs`), transcribe con
faster-whisper (calidad OK/DUDOSA/MALA por llamada), diariza en hablantes,
matchea el lado técnico contra `A_Voz` (umbral por persona) y vuelca
`A_Conversacion` + turnos + `R_Conversacion_Voz`. Sin match no inventa nombre:
queda `desconocido`/`BAJA` para revisión. `--semilla` carga las plantillas desde
`voiceprints.json` del pack. Idempotente por `CdrId` (re-correr es seguro).
Corre como batch nocturno, no en el timer horario; `--workers 2-4` paraleliza
el backfill (default 1 = secuencial). `--cdr-id` procesa CDRs puntuales.
`decode_audio` resamplea a 16 k (hay CDRs en 8 kHz) y las grabaciones traen una
pierna por canal (ver `docs/canales-estereo.md`).

```bash
py -u -m cli.atribuir_voz --semilla --desde 2026-09-28 --hasta 2026-09-28 --limite 20 --workers 2
py -u -m cli.atribuir_voz --cdr-id 1234567890
py -u -m cli.atribuir_voz --bautizar          # dry-run: bautiza clientes (nunca tecnicos)
py -u -m cli.atribuir_voz --fusionar          # fusiona candidatos que eran cliente
py -u -m cli.chequear_completitud --dias 7 --voz   # cobertura API vs CSV + faltantes
```

Normalizar voces: `V_A_ColaVoces` (cola con sugerencia ESCUCHAR/BAUTIZAR/REVISAR),
`enrolar.bautizar_clientes` (nombre dicho + teléfono con historia + voz coincidente),
`--nombrar-id ID --nombre "..."` (técnicos, solo oído humano), `--fusionar`.
El pack de clips/voiceprints/escucha vive fuera de este repo (`deli-support-docs`).

El roster de técnicos (nombres y umbrales calibrados) es **dato personal y no se
versiona**: se carga de `config/roster.json` (ignorado por git); sin ese archivo
el pipeline funciona pero no bloquea bautismos por nombre de técnico. Copiar
`config/roster.example.json` como plantilla.

## Power BI

`powerbi/Metricas Anura.pbip` es un proyecto de Power BI Desktop (PBIP):
SemanticModel en modo Import sobre `V_A_ConversacionPBI` y `V_A_LlamadaPerdida`,
más un Report de 6 páginas (Resumen, Horarios, Duraciones, Turno, Perdidas,
Calidad). La vista de PBI no expone transcripto, texto ni ruta de audio.

* Requiere `sql/01_schema.sql` y `sql/02_views.sql` aplicados.
* Regenerar: `py tools/build_pbip.py`. Usa IDs estables (uuid5): re-correr sin
  cambios no ensucia git; preserva lo editado en Desktop salvo las páginas que
  posee el builder.
* Si el repo trae el placeholder y necesitás refrescar: `py tools/set_pbi_destino.py`
  hornea `PBI_SQL_*` en las 4 particiones sin tocar páginas ni visuales. Ese
  cambio de `powerbi/` queda local: **no se commitea**.
* `.pbi/` (cache binario y firma de la máquina) está en `.gitignore`.
* Antes de refrescar, correr `EXEC dbo.sp_RebuildPerdidaRacha` (la vista de
  rachas lee la tabla materializada).
* El host/base sale de `PBI_SQL_SERVER` / `PBI_SQL_DATABASE`; sin ellas el
  `.pbip` versionado queda con placeholder.

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
cli/atribuir_voz.py             # entrypoint voz: python -m cli.atribuir_voz --semilla ...
cli/chequear_completitud.py     # cobertura API vs CSV por día (--voz)
cli/chequear_plantillas.py      # higiene de plantillas (débiles/colisiones)
cli/menu.py                     # menú interactivo del pipeline de voz (menu.bat)
config/settings.py              # lee .env (local) o Application settings (Azure)
config/roster.example.json      # plantilla del roster local (el real NO se versiona)
docs/canales-estereo.md         # hallazgo de una pierna por canal L/R
etl/conn/anura_client.py        # OAuth + descarga CSV (timeout/retry)
etl/conn/db.py                  # engine SQLAlchemy/pyodbc
etl/dominios/anura/importar.py  # ETL pandas -> A_Llamada
etl/dominios/voz/audio.py       # descarga de grabaciones (getCdrs -> getRecordingURL -> .ogg)
etl/dominios/voz/modelos.py     # whisper + ECAPA (carga diferida), diarización, matching
etl/dominios/voz/conversaciones.py  # pipeline voz -> A_Conversacion (+ seed de plantillas)
etl/dominios/voz/roster.py      # carga el roster local (umbrales + excluidos)
etl/dominios/tickets/stats.py   # panel de progreso
sql/01_schema.sql               # DDL idempotente (tablas + índices)
sql/02_views.sql                # vistas + SP de reporting
tools/build_pbip.py             # regenera powerbi/ (partición M parametrizada)
tools/set_pbi_destino.py        # hornea PBI_SQL_* en los .tmdl (uso local)
```

## Seguridad

* `.env` está en `.gitignore` y **nunca** se commitea (ver `.env.template`).
* Datos personales fuera del repo: `config/roster.json` (nombres/umbrales del
  roster), `voiceprints.json` (plantillas) y `audios/` (grabaciones).
* El `.pbip` versionado no lleva host/base reales: `tools/build_pbip.py` usa
  `PBI_SQL_SERVER` / `PBI_SQL_DATABASE` (entorno o `.env`) y, sin ellas, escribe
  un placeholder. Para refrescar en local, definirlas y regenerar; ese cambio de
  `powerbi/` **no** se commitea.
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
| Power BI no refresca | Aplicar `01_schema.sql` + `02_views.sql`, correr `sp_RebuildPerdidaRacha` y revisar `PBI_SQL_*` (el repo versiona un placeholder). |
| Cortes de red / timeouts | Los reintentos con backoff ya cubren transitorios; re-correr es seguro (idempotente). |

## Licencia

MIT — ver [`LICENSE`](LICENSE). Uso libre con atribución (conservar aviso de
copyright).

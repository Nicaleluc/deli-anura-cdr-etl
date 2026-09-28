"""
Azure Function timer para deli-anura-cdr-etl.

Schedule: cada 1 hora en minuto 0 -> NCRONTAB "0 0 * * * *"
Equivale al CLI: python -m cli.importar_anura

Deploy: subir todo el repo como root de la Function App (Linux, Python v2).
Env vars se configuran en Configuration > Application settings (no usa .env en Azure).
  CLIENT_ID, CLIENT_PASSWORD, SQL_SERVER, SQL_DATABASE, SQL_USERNAME, SQL_PASSWORD
"""

import logging

import azure.functions as func

app = func.FunctionApp()


@app.timer_trigger(
    schedule="0 0 * * * *",
    arg_name="mytimer",
    run_on_startup=False,
    use_monitor=True,
)
def importar_anura_timer(mytimer: func.TimerRequest) -> None:
    if mytimer.past_due:
        logging.warning("Timer is past due, running late.")

    logging.info("importar_anura_timer: inicio ETL Anura -> A_Llamada")

    # Import diferido para que el host de Functions cargue rapido
    # y los errores de config salgan en el log de la invocacion.
    from etl.dominios.anura.importar import main

    main()

    logging.info("importar_anura_timer: fin OK")

"""Configuración mínima para correr los tests sin `.env`.

`config/settings.py` aborta al importarse si faltan credenciales (fail-fast
deliberado en producción). Los tests son de funciones puras, así que acá se
completan con valores dummy: nunca se conectan a Anura ni a SQL.
"""
import os

for _clave in ("CLIENT_ID", "CLIENT_PASSWORD", "SQL_SERVER", "SQL_DATABASE",
               "SQL_USERNAME", "SQL_PASSWORD"):
    os.environ.setdefault(_clave, "test")

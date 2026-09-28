from urllib.parse import quote_plus

import pyodbc
from sqlalchemy import create_engine

from config.settings import SQL_DATABASE, SQL_PASSWORD, SQL_SERVER, SQL_USERNAME


def _conn_str():
    # PWD entre llaves ODBC (} escapado como }}) para tolerar ; } etc.
    # en el password sin romper el connection string.
    pwd = (SQL_PASSWORD or "").replace("}", "}}")
    return (
        "DRIVER={ODBC Driver 18 for SQL Server};"
        f"SERVER={SQL_SERVER};"
        f"DATABASE={SQL_DATABASE};"
        f"UID={SQL_USERNAME};"
        f"PWD={{{pwd}}};"
        "TrustServerCertificate=yes;"
    )


def get_connection():
    return pyodbc.connect(_conn_str())


def get_engine():
    # quote_plus: un futuro @ : / en el password no rompe la URL.
    return create_engine(
        f"mssql+pyodbc://{SQL_USERNAME}:{quote_plus(SQL_PASSWORD or '')}"
        f"@{SQL_SERVER}/{SQL_DATABASE}"
        f"?driver=ODBC+Driver+18+for+SQL+Server",
        fast_executemany=True,
    )

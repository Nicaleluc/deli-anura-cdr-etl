"""
Importa llamadas telefónicas (CDRs) desde la API Anura → A_Llamada.

Uso:
    python -m cli.importar_anura
"""

from etl.dominios.anura.importar import main


if __name__ == "__main__":
    main()
"""Re-escribe la conexión SQL en los .tmdl de powerbi/ (sin tocar nada más).

El repo versiona `powerbi/` con un placeholder (`tu-servidor...`) para no
publicar el host real. Después de un clone/pull (o si Power BI no refresca),
este script hornea el destino local desde `PBI_SQL_SERVER` / `PBI_SQL_DATABASE`
(entorno o `.env`):

    py tools/set_pbi_destino.py

No regenera páginas, medidas ni visuales: sólo reemplaza el `Sql.Database(...)`
de las particiones. `powerbi/` queda modificado a propósito y NO se commitea.
"""
import re
from pathlib import Path

try:
    from build_pbip import REPO, _sql_destino
except ImportError:  # ejecutado como módulo: py -m tools.set_pbi_destino
    from tools.build_pbip import REPO, _sql_destino

TABLAS = (Path(REPO) / "powerbi" / "Metricas Anura.SemanticModel"
          / "definition" / "tables")


def main():
    servidor, base = _sql_destino()
    # M escapa comillas dobles duplicándolas.
    servidor = str(servidor).replace('"', '""')
    base = str(base).replace('"', '""')
    reemplazo = f'Sql.Database("{servidor}", "{base}")'
    patron = re.compile(r'Sql\.Database\("[^"]*", "[^"]*"\)')

    n = 0
    for path in sorted(TABLAS.glob("*.tmdl")):
        with open(path, encoding="utf-8", newline="") as f:
            texto = f.read()
        nuevo, k = patron.subn(reemplazo, texto)
        if k:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(nuevo)
            print(f"  {path.name}: {k} particion(es)")
            n += k

    print(f"{n} particiones -> {servidor} / {base}")
    if servidor.startswith("tu-") or base == "tu-base":
        print("AVISO: sigue el placeholder; definí PBI_SQL_SERVER y "
              "PBI_SQL_DATABASE en el .env (o en el entorno).")


if __name__ == "__main__":
    main()

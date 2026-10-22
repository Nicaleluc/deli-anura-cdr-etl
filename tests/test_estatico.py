"""Guardas estaticas baratas: errores que solo aparecen al correr, y caro.

Sin linter instalado (ruff/flake8/pylint ausentes), asi que chequeamos a mano lo
que mas caro sale: un nombre global inexistente dentro del pipeline.

Bug real: `etl/dominios/voz/conversaciones.py` importaba `VOZ_UMBRAL_RECHAZO`
pero usaba `UMBRAL_RECHAZO`. El modulo importaba bien, asi que ningun test de
importacion lo detectaba: reventaba recien al transcribir la llamada N, con
NameError, y costo 1h33m de las 38 conversaciones del 19-09.
"""

import ast
import builtins
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
MODULOS = sorted(
    p for d in ("cli", "config", "etl")
    for p in (RAIZ / d).rglob("*.py")
)

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__"}


def _nombres_bound(nodo):
    """Nombres que el nodo declara (params, asignaciones, imports, loops, etc.)."""
    out = set()
    for n in ast.walk(nodo):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            out.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.Global) or isinstance(n, ast.Nonlocal):
            out.update(n.names)
    return out


def _modulos_de_arbol(arbol):
    """Todos los nombres declarados a nivel de modulo."""
    return _nombres_bound(arbol)


def test_no_hay_nombres_globales_inexistentes():
    """Ningun modulo debe usar un global que no exista en el archivo.

    Sobre-aproxima los bindings (cuenta como declarado cualquier asignacion del
    archivo), pero eso no importa: el bug que buscamos es un nombre que NO esta
    declarado en ningun lado, tipogRAFia included.
    """
    problemas = []
    for path in MODULOS:
        arbol = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        global_declarado = _modulos_de_arbol(arbol) | BUILTINS
        for func in ast.walk(arbol):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local = _nombres_bound(func) | global_declarado
            for n in ast.walk(func):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    if n.id not in local:
                        problemas.append(f"{path.relative_to(RAIZ)}:{n.lineno}: {n.id}")
    assert not problemas, "nombres globales inexistentes:\n  " + "\n  ".join(problemas)


def test_todo_modulo_compila():
    for path in MODULOS:
        ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def test_banda_de_rechazo_usa_el_nombre_importado():
    """Regresion directa del bug de UMBRAL_RECHAZO."""
    import etl.dominios.voz.conversaciones as cv
    import inspect

    fuente = inspect.getsource(cv)
    assert "UMBRAL_RECHAZO" in fuente, "se perdio la banda de rechazo"
    # todo uso debe ser el nombre que settings.py define
    assert " VOZ_UMBRAL_RECHAZO" in fuente
    assert cv.VOZ_UMBRAL_RECHAZO == 0.40
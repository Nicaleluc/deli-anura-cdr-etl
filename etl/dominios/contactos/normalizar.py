"""Normalizacion y clasificacion de datos de contacto desde `A_Llamada.Origen`.

`Origen` viene del `callingName` de Anura y trae mezclado:
  "Razon Social (Nombre)"  -> empresa + persona de contacto
  "Razon Social"           -> solo empresa
  "Nombre Apellido"        -> persona (monotributista)
  "Abel"                   -> solo nombre
  "Argentina/AMBA - FIJO"  -> carrier/geo (sin dato util)
  "203 - Guardia 3"        -> interna
  "Restringido"            -> sin numero
"""
from __future__ import annotations

import re
import unicodedata

_SUFIJOS = re.compile(
    r"\b(S\.?\s?A\.?|S\.?\s?R\.?\s?L\.?|S\.?A\.?S\.?|LTDA|SCS|SH|"
    r"COOP(?:ERATIVA)?|SOCIEDAD|S\.?A\.?U\.?)\b", re.I)
_RE_INTERNA = re.compile(r"^\d{2,4}\s*-\s*\w")
_RE_MOVIL_15 = re.compile(r"^(\d{2,4})15(\d{6,8})$")
_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PLACEHOLDERS = {"nombre", "contacto", "sin nombre", "no informado",
                 "sin datos", "n/a", "na", "-", "no"}
_ACRONIMOS = {"SA", "SAS", "SRL", "LTDA", "SH", "SCS", "SC", "Y", "E",
              "YPF", "SHELL", "AXION", "AWA", "COMBEX"}

# Palabras que son un ROL, no una persona (encabezados de ticket/telefono).
_ROLES = {"encargada", "encargado", "encargade", "administracion", "administracion",
          "recepcion", "reception", "mantenimiento", "dueno", "duena", "contador",
          "contadora", "secretaria", "secretario", "atencion", "operador",
          "operadora", "jefe", "jefa", "gerente", "playa", "caja", "estacion",
          "guardia", "guardian", "auxiliar", "tecnico", "tecnica", "vendedor",
          "vendedora", "empleado", "empleada", "personal", "supervisor",
          "supervisora", "responsable", "titular", "gerencia", "compras",
          "ventas", "logistica", "tesoreria", "sucursal"}


def sin_acentos(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto or "")
                   if not unicodedata.combining(c))


def clave(texto: str) -> str:
    """Clave de identidad: minusculas, sin acentos, espacios colapsados."""
    return re.sub(r"\s+", " ", sin_acentos((texto or "").lower())).strip()


def normalizar_telefono(raw: str) -> str:
    """Area + numero (sin 54/9/0). "" si no parece telefono argentino."""
    d = re.sub(r"\D", "", raw or "")
    if not d:
        return ""
    if d.startswith("00"):
        d = d[2:]
    if d.startswith("54") and len(d) > 11:
        d = d[2:]
    if d.startswith("9") and len(d) > 10:
        d = d[1:]
    if d.startswith("0") and len(d) > 10:
        d = d[1:]
    # El "15" movil solo existe como infijo extra: en un numero ya de
    # 10 digitos no hay nada que pelar (evita falsos positivos tipo
    # 3515000001 -> "35|15|000001" que lo rompia a 8 digitos).
    m = _RE_MOVIL_15.match(d) if len(d) > 10 else None
    if m:
        d = m.group(1) + m.group(2)
    return d if 10 <= len(d) <= 11 else ""


def normalizar_cuit(raw: str) -> str:
    d = re.sub(r"\D", "", raw or "")
    return d if len(d) == 11 else ""


def normalizar_correo(raw: str) -> str:
    m = _RE_EMAIL.search(raw or "")
    return m.group(0).lower().strip(".") if m else ""


def es_placeholder(nombre: str) -> bool:
    return clave(nombre) in _PLACEHOLDERS


def es_rol(nombre: str) -> bool:
    return clave(nombre) in _ROLES


def tiene_sufijo_empresa(texto: str) -> bool:
    return bool(_SUFIJOS.search(texto or ""))


def titulo(texto: str) -> str:
    """Proper case preservando siglas (SA/SRL/YPF...) y tokens con numeros."""
    partes = []
    for tok in (texto or "").split():
        base = tok.rstrip(".,").upper()
        sin_puntos = tok.replace(".", "")
        if base in _ACRONIMOS or any(c.isdigit() for c in tok):
            partes.append(tok)
        elif "." in tok and len(sin_puntos) <= 4 and sin_puntos.isalpha():
            partes.append(tok.upper())
        else:
            partes.append(tok[:1].upper() + tok[1:].lower())
    return " ".join(partes)


def limpiar_nombre(raw: str) -> str:
    n = re.sub(r"\s+", " ", (raw or "").replace("\xa0", " ")).strip(" \t-")
    if not n or es_placeholder(n) or es_rol(n) or not re.search(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", n):
        return ""
    return titulo(n)


def limpiar_razon(raw: str) -> str:
    n = re.sub(r"\s+", " ", (raw or "")).strip(" \t-")
    if not n or es_placeholder(n):
        return ""
    return re.sub(r"\s+", " ", n)


def clasificar_origen(origen: str) -> tuple[str, str, str]:
    """Devuelve (tipo, razon_social, nombre_contacto).

    tipo: razon | nombre | interna | carrier | vacio
    """
    o = re.sub(r"\s+", " ", (origen or "").strip())
    if not o:
        return ("vacio", "", "")
    low = sin_acentos(o.lower())
    if low.startswith("argentina/") or low.startswith("argentina ") or low == "restringido":
        return ("carrier", "", "")
    if _RE_INTERNA.match(o) or "guardia" in low:
        return ("interna", "", "")

    razon, nombre = o, ""
    if " (" in o and o.endswith(")"):
        razon, nombre = o.split(" (", 1)
        razon, nombre = razon.strip(), limpiar_nombre(nombre[:-1])
    razon = limpiar_razon(razon)
    if not razon:
        return ("vacio", "", nombre)
    if es_rol(razon):
        return ("rol", "", nombre)
    if _SUFIJOS.search(razon) or len(razon.split()) >= 2:
        return ("razon", razon, nombre)
    return ("nombre", "", limpiar_nombre(razon))

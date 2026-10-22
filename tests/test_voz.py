"""Tests de funciones puras que ya causaron bugs (sin DB ni modelos)."""

import os
import tempfile

import numpy as np

from etl.dominios.contactos.normalizar import normalizar_telefono
from etl.dominios.voz.enrolar import _dicho_valido


def test_telefono_10_digitos_no_se_rompe():
    # Bug historico: el regex del '15' mordia '35|15|225854' -> "".
    assert normalizar_telefono("3515000001") == "3515000001"
    assert normalizar_telefono("2910000002") == "2910000002"
    assert normalizar_telefono("2210000003") == "2210000003"
    assert normalizar_telefono("3510000004") == "3510000004"


def test_telefono_formatos_argentinos():
    assert normalizar_telefono("+5493515000001") == "3515000001"
    assert normalizar_telefono("351150000001") == "3510000001"
    assert normalizar_telefono("1130000005") == "1130000005"


def test_telefono_invalido():
    assert normalizar_telefono("") == ""
    assert normalizar_telefono("123") == ""
    assert normalizar_telefono("Argentina/AMBA - MOVIL") == ""


def test_dicho_valido_acepta_nombres():
    assert _dicho_valido("Santiago Perez")
    assert _dicho_valido("Malena")
    assert _dicho_valido("Juan Pablo")


def test_dicho_valido_rechaza_basura():
    assert not _dicho_valido("Perfecto, muchísimas gracias.")
    assert not _dicho_valido("Me paso, güey.")
    assert not _dicho_valido("Sorbona General Pico S.A.")
    assert not _dicho_valido("")
    assert not _dicho_valido("dos 3 cuatro")
    assert not _dicho_valido("tecnico")  # es un rol, no un nombre


def test_norm_nombre_ignora_tildes_y_espacios():
    # Incidente: el pack escribe "José Núñez" y el listening crea
    # "Jose Nunez"; el seed comparaba con `Nombre = :n` exacto y hacia un
    # duplicado de la MISMA persona en cada corrida con --semilla.
    from etl.dominios.voz.conversaciones import _norm_nombre
    assert _norm_nombre("José Núñez") == _norm_nombre("Jose Nunez")
    assert _norm_nombre("Inigo Aguirre") == _norm_nombre("Íñigo Aguirre")
    assert _norm_nombre("  CARLA   Gomez ") == "carla gomez"
    assert _norm_nombre(None) == ""
    # y NO debe colapsar nombres de personas distintas
    assert _norm_nombre("Jose Nunez") != _norm_nombre("Jose Nuñez Alvarez")


def _ogg_estereo(path, sr, segundos):
    # OGG Vorbis estereo como los de Anura (planar). Dos tonos distintos por
    # canal para detectar mezclas o recortes.
    import soundfile as sf

    t = np.arange(int(sr * segundos)) / sr
    estereo = np.stack([0.5 * np.sin(2 * np.pi * 440 * t),
                        0.5 * np.sin(2 * np.pi * 660 * t)], axis=1)
    sf.write(path, estereo, sr, format="OGG", subtype="VORBIS")


def test_decode_audio_respeta_duracion_real_en_8k():    # Incidente 2026-10-02 (un CDR medido, unico 8 kHz entre 815): sin
    # resample, 832 s de audio se leian como 416 s (doble velocidad,
    # "voz de raton") y todo el pipeline cascadeaba mal.
    from etl.dominios.voz.modelos import SR, decode_audio

    with tempfile.TemporaryDirectory() as d:
        p8 = os.path.join(d, "ocho.ogg")
        p16 = os.path.join(d, "dieciseis.ogg")
        _ogg_estereo(p8, 8000, 2.0)
        _ogg_estereo(p16, 16000, 2.0)
        x8 = decode_audio(p8)
        x16 = decode_audio(p16)
        assert abs(len(x8) / SR - 2.0) < 0.15, len(x8) / SR
        assert abs(len(x16) / SR - 2.0) < 0.15, len(x16) / SR
        assert abs(len(x8) - len(x16)) / len(x16) < 0.05


def _turnos(textos):
    return [{"rol": "CLIENTE", "texto": t} for t in textos]


def test_nombres_por_reglas_detecta_presentacion():
    from etl.dominios.voz.nombres import por_reglas

    m = por_reglas(_turnos(["Hola, me llamo Alejandro, tengo un problema",
                            "todo bien"]))
    assert len(m) == 1 and m[0]["nombre"] == "Alejandro"
    m = por_reglas(_turnos(["Te habla Laura Roldan de San Cristobal"]))
    assert len(m) == 1 and m[0]["nombre"] == "Laura Roldan"
    assert por_reglas(_turnos(["me llamo un tecnico"])) == []
    assert por_reglas(_turnos(["hola, buen dia"])) == []


def test_extraer_sin_ollama_usa_reglas():
    # Sin Ollama (usar_ollama=False) debe devolver lo de por_reglas,
    # nunca reventar ni devolver vacio si hay presentacion.
    from etl.dominios.voz.nombres import extraer

    m = extraer(_turnos(["Buen dia, mi nombre es Silvio Baldessari"]),
                usar_ollama=False)
    assert len(m) == 1 and m[0]["nombre"] == "Silvio Baldessari"


def test_extraer_con_ollama_caido_cae_a_reglas(monkeypatch):    # Incidente: si Ollama no responde, extraer reventaba y el enrolar
    # moria (por eso el menu corria con con_nombres=False).
    import etl.dominios.voz.nombres as NB

    def _caido(_prompt):
        raise ConnectionError("ollama apagado")

    monkeypatch.setattr(NB, "generar_json", _caido)
    m = NB.extraer(_turnos(["Hola, me llamo Alejandro"]), usar_ollama=True)
    assert len(m) == 1 and m[0]["nombre"] == "Alejandro"


def test_calificar_umbrales_medidos():
    # Medidos 2026-10-02 sobre 810 convs (cps med 8.8 p5 3.1; tpm med 9.0 p5
    # 3.3): MALA si cps<2 (ej. 65 s con 18 chars), DUDOSA si cps<5 o tpm<3.
    from etl.dominios.voz.modelos import calificar

    assert calificar(8.8, 9.0) == "OK"
    assert calificar(0.28, 0.9) == "MALA"
    assert calificar(1.99, 9.0) == "MALA"
    assert calificar(2.0, 9.0) == "DUDOSA"
    assert calificar(4.9, 12.0) == "DUDOSA"
    assert calificar(8.0, 2.9) == "DUDOSA"
    assert calificar(5.0, 3.0) == "OK"


class _WmFalso:
    def __init__(self, segmentos):
        self._segs = segmentos

    def transcribe(self, audio, **kw):
        from types import SimpleNamespace

        return ([SimpleNamespace(start=a, end=b, text=t)
                 for a, b, t in self._segs], None)


def test_transcribir_con_metricas_cuenta_rechazos():
    # Compat: transcribir() devuelve lo mismo que antes; la variante con
    # metricas cuenta raw/corta/basura/ok sin cambiar el filtrado.
    import numpy as np

    from etl.dominios.voz.modelos import transcribir, transcribir_con_metricas

    segs = [(0.0, 5.0, "buen dia, te hablo de San Cristobal"),   # ok
            (5.0, 5.5, "si"),                                     # corta
            (6.0, 12.0, "y y y y y y"),                           # basura
            (12.0, 16.0, "el ID es 976")]                        # ok
    wm = _WmFalso(segs)
    audio = np.zeros(16000 * 17, dtype=np.float32)
    assert transcribir(wm, audio) == [(0.0, 5.0, "buen dia, te hablo de San Cristobal"),
                                     (12.0, 16.0, "el ID es 976")]
    out, met = transcribir_con_metricas(wm, audio)
    assert len(out) == 2
    assert met == {"raw": 4, "corta": 1, "basura": 1, "ok": 2}


def _frec_dom(y, sr):
    import numpy as np

    esp = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    return float(np.fft.rfftfreq(len(y), 1 / sr)[int(np.argmax(esp))])


def test_decode_estereo_preserva_canales_y_tasa():
    # Las grabaciones traen una pierna por canal: el decode estereo no debe
    # mezclarlos ni acortarlos (8 kHz tambien se resamplea por canal).
    import numpy as np

    from etl.dominios.voz.modelos import SR, decode_estereo

    with tempfile.TemporaryDirectory() as d:
        for sr_in, nombre in ((8000, "ocho.ogg"), (16000, "dieciseis.ogg")):
            p = os.path.join(d, nombre)
            _ogg_estereo(p, sr_in, 2.0)
            L, R = decode_estereo(p)
            assert abs(len(L) / SR - 2.0) < 0.15, len(L) / SR
            assert abs(len(R) / SR - 2.0) < 0.15, len(R) / SR
            assert abs(_frec_dom(L, SR) - 440) < 30, _frec_dom(L, SR)
            assert abs(_frec_dom(R, SR) - 660) < 30, _frec_dom(R, SR)


def test_elegir_lado_pide_margen():
    from etl.dominios.voz.modelos import elegir_lado

    assert elegir_lado(0.2, 0.8) == "L"
    assert elegir_lado(0.8, 0.2) == "R"
    assert elegir_lado(0.5, 0.55) is None  # diferencia < margen: no decir
    assert elegir_lado(None, 0.2) is None
    assert elegir_lado(0.2, None) is None


def test_roster_se_carga_de_json():
    # El roster real es dato personal: vive en config/roster.json (gitignored).
    import json
    from etl.dominios.voz.roster import cargar_roster

    datos = {"umbrales": {"Ana Perez": 0.50, "Beto Diaz": 0.60},
             "excluidos": ["Beto Diaz"]}
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "roster.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(datos, f)
        r = cargar_roster(p)
    assert r["umbrales"] == {"Ana Perez": 0.50, "Beto Diaz": 0.60}
    assert r["excluidos"] == ["Beto Diaz"]


def test_roster_vacio_si_no_hay_archivo():
    from etl.dominios.voz.roster import cargar_roster

    r = cargar_roster(os.path.join(tempfile.gettempdir(), "no-existe-xyz.json"))
    assert r == {"umbrales": {}, "excluidos": []}


def test_es_tecnico_roster_guarda_bautismo():
    from etl.dominios.voz.conversaciones import es_tecnico_roster

    roster = {"Ana Perez": 0.55, "Beto Diaz": 0.55}
    excluidos = {"Beto Diaz"}
    assert es_tecnico_roster("Ana", roster, excluidos)
    assert es_tecnico_roster("ana perez", roster, excluidos)
    assert es_tecnico_roster("José Núñez", {"Jose Nunez": 0.55}, set())  # sin tilde vale
    assert not es_tecnico_roster("Beto Diaz", roster, excluidos)  # excluido
    assert not es_tecnico_roster("Alejandro", roster, excluidos)
    assert not es_tecnico_roster("", roster, excluidos)


def _cdr(**kw):
    base = {"id": 1, "direction": "IN", "calling": "1100001111",
            "duration": 842, "wasRecorded": True, "lastAction": "MULTIDIAL"}
    base.update(kw)
    return base


def test_hermana_in_presente_detecta_la_pierna():
    from etl.dominios.voz.audio import hermana_in_presente

    out = _cdr(direction="OUT", lastAction=None)
    dia = [_cdr(), _cdr(direction="OUT")]
    assert hermana_in_presente(out, dia)
    # tolerancia CSV vs API en duracion (±15 s)
    assert hermana_in_presente(out, [_cdr(duration=850)])
    assert not hermana_in_presente(out, [_cdr(duration=900)])


def test_hermana_in_presente_no_inventa():
    from etl.dominios.voz.audio import hermana_in_presente

    out = _cdr(direction="OUT", lastAction=None)
    assert not hermana_in_presente(out, [])
    assert not hermana_in_presente(out, None)
    # otro llamante no es hermana aunque coincida duracion
    assert not hermana_in_presente(out, [_cdr(calling="1111111111")])
    # solo OUTs: la IN esta oculta -> sin hermana
    assert not hermana_in_presente(out, [_cdr(direction="OUT")])
    # solo IVR: no es conversacion
    assert not hermana_in_presente(
        out, [_cdr(lastAction="IVR", duration=13, wasRecorded=False)])


def test_get_cdrs_day_pagina_hasta_agotar_nuevos():
    # Incidente 2026-10-05: el 3er arg de getCdrs es NUMERO DE PAGINA
    # (no flag de "dia completo"). Con pagina 1 sola se perdian ~20 %
    # de las piernas, entre ellas una MULTIDIAL grabada.
    from etl.conn.anura_client import AnuraClient

    paginas = {
        1: [_cdr(id=1), _cdr(id=2)],
        2: [_cdr(id=2), _cdr(id=3)],
        3: [_cdr(id=3)],
    }
    llamadas = []

    class Fake(AnuraClient):
        def __init__(self):
            pass

        def gcapi(self, method, params, access_token=None):
            llamadas.append(params)
            assert method == "AdminService.getCdrs"
            return paginas.get(params[2], [])

    res = Fake().get_cdrs_day("2026-10-05")
    assert sorted(c["id"] for c in res) == [1, 2, 3]
    # pagina 1, 2 con nuevos, 3 sin nuevos -> frena ahi
    assert [p[2] for p in llamadas] == [1, 2, 3]


def test_get_cdrs_day_dia_vacio_una_sola_pagina():
    from etl.conn.anura_client import AnuraClient

    class Fake(AnuraClient):
        def __init__(self):
            self.n = 0

        def gcapi(self, method, params, access_token=None):
            self.n += 1
            return []

    fake = Fake()
    assert fake.get_cdrs_day("2026-10-05") == []
    assert fake.n == 1


def test_detectar_buzon_casilla_toda_operadora():
    # Caso medido: "Te comunicaste con la casilla de mensajes",
    # toda la voz votante es operadora, sin match tecnico.
    from etl.dominios.voz.conversaciones import detectar_buzon
    txt = ["Te comunicaste con la casilla de mensajes.",
           "Si quieres grabar un mensaje, presiona 2."]
    assert detectar_buzon(txt, False, ["A"], ["A"])


def test_detectar_buzon_frase_corta_sin_voz():
    # Caso medido: 1 turno "Bienvenido al buzón de voz claro...",
    # con tilde (whisper la escribe) -> normaliza igual.
    from etl.dominios.voz.conversaciones import detectar_buzon
    txt = ["Bienvenido al buzón de voz claro de Nombre, "
           "después del tono, deja tu mensaje."]
    assert detectar_buzon(txt, False, [], [])


def test_detectar_buzon_no_marca_si_hay_tecnico():
    # Si un tecnico matcheo, es conversacion real aunque se mencione el buzon.
    from etl.dominios.voz.conversaciones import detectar_buzon
    txt = ["Te comunicaste con la casilla de mensajes."]
    assert not detectar_buzon(txt, True, ["A"], ["A"])


def test_detectar_buzon_no_marca_real_larga():
    # Caso medido: conversacion real de 83 turnos que dice "mensaje";
    # larga y sin voz operadora -> no es buzon.
    from etl.dominios.voz.conversaciones import detectar_buzon
    txt = ["Hola, ¿qué tal?"] * 80 + ["te mando un mensaje al celu"]
    assert not detectar_buzon(txt, False, ["A", "B"], [])


def test_detectar_buzon_no_esta_disponible_no_vale():
    # Criterio 014: "no esta disponible" la dicen clientes reales.
    from etl.dominios.voz.conversaciones import detectar_buzon
    assert not detectar_buzon(["el cliente no esta disponible"], False, [], [])


def test_canal_de_segmento_elige_el_dominante():
    # El embedding por canal es lo que separa tecnico/cliente (0,75 mono
    # -> 0,37 canal correcto). El canal es el de mayor energia.
    import numpy as np
    from etl.dominios.voz import modelos as M
    n = M.SR
    sil = np.zeros(n, dtype=np.float32)
    senal = (np.random.RandomState(0).randn(n) * 0.2).astype(np.float32)
    assert M.canal_de_segmento(senal, sil, 0, 1) == "L"
    assert M.canal_de_segmento(sil, senal, 0, 1) == "R"

# Canales estéreo en grabaciones Anura (hallazgo)

Los `.ogg` que devuelve Anura traen **una pierna por canal** (no son
dual-mono). El pipeline no puede asumir que el técnico está siempre en L: el
canal se decide por llamada, comparando cada lado contra las plantillas de
voice-ID. Este documento resume la medición que llevó a esa regla.

## Regla medida

1. Los dos canales son **independientes**: correlación L/R ≈ 0,00–0,003 en
   10/10 archivos (16 kHz y 8 kHz).
2. **IN y OUT de la misma llamada son espejos**: lo que va en L en una va en R
   en la otra (mismo lado siempre >0,75). 4/4 pares analizados.
3. Tendencia fuerte pero **no ley**: en 7/8 piernas IN el técnico iba en L;
   una pierna IN genuina llevaba al técnico en R. Conclusión operativa:
   **determinar el canal por llamada** (`canal_del_tecnico` /
   `M.embeber_estereo`), nunca fijarlo en código.

## Evidencia (energía por turno, roles TECNICO/CLIENTE de la BD)

| Llamada | Técnico | Turnos téc. en L | Turnos cli. en R |
|---|---|---|---|
| #1 | Técnico A | 75 % (n=4) | 100 % (n=4) |
| #2 | Técnico B | 99 % (n=106) | 60 % (n=60) |
| #3 | Técnico C | 88 % (n=8) | 100 % (n=4) |
| #4 | Técnico D | 100 % (n=7) | 67 % (n=3) |
| #5 | Técnico E | 66 % (n=41) | s/d |
| #6 | Técnico F | s/d (n=3) | 88 % (n=8) |
| #7 (excepción) | Técnico G | mezclado | 100 % en L |
| #8 (OUT) | Técnico H | R | L 100 % |

En la excepción los roles estaban verificados (el técnico explica y cierra la
llamada): el canal simplemente no siguió la tendencia. Su gemela OUT es espejo
exacto, así que no fue un error de medición.

## Implicancias

- Promediar L+R a mono contamina las voces cuando hay solapes o ruido de
  fondo; en single-talk no cambia nada.
- Una plantilla construida con un canal arbitrario puede volverse un "hub"
  (matchea todo): siempre reconstruir con el canal del técnico de llamadas
  confirmadas.
- Las piernas OUT medidas vinieron en **8 kHz**; `decode_audio` resamplea
  siempre a 16 kHz para que comparen contra plantillas wideband.
- Con n chico no se fija la regla en código: se decide por llamada.

## Abierto

- Confirmar L=dueño / R=interlocutor con más piernas OUT.
- Revisar llamadas con roles posiblemente invertidos antes de reprocesar.

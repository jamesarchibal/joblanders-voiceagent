# Empezar aqui (agente de voz)

Nota de arranque. La escribio Claude desde la sesion del dashboard el 2026-09-28.
Objetivo de la sesion: anadir funcionalidad al agente y resolver el "se queda en blanco".

## Que es esto
Agente de voz de entrada (inbound), independiente. Codigo Python con LiveKit Agents.
Pipeline en cascada: Deepgram (STT) -> Claude (cerebro) -> ElevenLabs/Cartesia (TTS).
Este repo es "el hilo" del agente. La web (boton "Habla con Lander") vive en OTRO repo
(el dashboard, en `content/media/landeros-site/index.html`). Aqui NO esta la web.

## El numero
- La web anuncia y marca `+34 930 485 117` (enlace `tel:`).
- Ese numero NO aparece en el codigo de este repo. El enrutado de entrada (netelip -> LiveKit)
  se configura en el panel de LiveKit Cloud + netelip, no en un fichero de aqui.
- Proveedor SIP: netelip. Trunk de salida (para pasar a humano): `ST_sk7yUi8zGoGz`.
- Numero de transfer a humano en el codigo: `+34 623 004 169` (`agent.py:100`).
  Ojo: netelip quiere el nacional de 9 digitos, no E.164 (ver `_netelip_national`).

## Despliegue
- Corre en LiveKit Cloud. Agente `CA_qkG3NUww67Tv`, subdominio `joblanders-7ac5oyti`
  (`livekit.toml`).
- El worker escala a cero cuando esta ocioso. La primera llamada tras un rato hace
  cold-start (4-5 s) y puede sonar sin que nadie conteste.
- Workflow `.github/workflows/keep-warm.yml` lo despierta cada 10 min, Lun-Vie 06:00-19:59 UTC.
  Noches y fines de semana siguen en frio. Para 24/7 caliente hay que subir LiveKit al plan Ship.
- Estado 2026-09-28: keep-warm en verde (ultima 12:25 UTC, success), pero los cron de GitHub
  llegan espaciados. "success" solo significa que desperto una sala, no que el deploy este sano.

## El problema "se queda en blanco" (silencio al llamar)
Hipotesis, de mas a menos probable:
1. Cold-start: el worker estaba a cero y la llamada cae mientras arranca -> silencio unos segundos.
2. Deploy en LiveKit Cloud caido o con error de runtime (revisar logs del agente en el panel Cloud).
3. Una API key caida (Deepgram / Anthropic / ElevenLabs / Cartesia) -> el pipeline no habla.
4. La regla de dispatch SIP de entrada no manda la llamada a este agente.
El `worker.log` local esta viejo (acaba el 15 jul en "process exiting"), es solo de una prueba
local, no refleja el deploy en Cloud.

Como confirmar cual es:
- Mirar los logs del agente `CA_qkG3NUww67Tv` en el panel de LiveKit Cloud durante una llamada.
- Verificar que las keys de `.env` estan vivas.
- Probar el cerebro sin gastar minutos (ver abajo).

## Probar sin gastar minutos de llamada
`.venv/bin/python agent.py console`
Corre el MISMO `agent.py` con el microfono y altavoces del Mac. Sin PSTN, sin minutos de
LiveKit. Solo coste minimo de API (Deepgram/Anthropic/TTS). Prueba saludo, idioma,
conversacion y memoria. NO prueba el caller-ID SIP ni el transfer a humano real.

## Anadir funcionalidad
- El cerebro y las tools estan en `agent.py`.
- Los prompts en `prompts/`.
- Regresion a nivel de texto: `evals/` y `test_routing.py`.
- Tras cambiar el prompt o las tools, correr `test_routing.py` antes de desplegar.

## Preferencias del proyecto
- Escribir a James en ingles tecnico simple: frases cortas, una idea por frase, voz activa.
- Nunca usar el guion largo ni el guion medio. Usar coma, dos puntos, punto o parentesis.
  Para rangos numericos usar un guion normal (150-250) o la palabra "a".

## Primeros pasos sugeridos en la nueva sesion
1. `git log --oneline -5` para ver el ultimo estado del codigo.
2. Confirmar que las keys de `.env` estan vivas.
3. Reproducir el silencio con una llamada mientras se miran los logs de LiveKit Cloud.
4. Decidir la funcionalidad nueva y anadirla en `agent.py` + `prompts/`, con test en `test_routing.py`.

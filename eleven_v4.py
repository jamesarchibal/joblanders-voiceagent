"""Hace que el plugin de ElevenLabs de LiveKit hable con Eleven v4.

POR QUÉ HACE FALTA ESTO
-----------------------
ElevenLabs publicó `eleven_v4` y `eleven_v4_turbo` el 28-sep-2026. Los dos existen
y nuestra clave los puede usar, pero NO aceptan el websocket que usa el plugin
(`/v1/text-to-speech/{voice}/multi-stream-input`): devuelven 400, igual que
`eleven_v3`. ElevenLabs los sirve por OTRO socket, `/v1/text-to-dialogue/
multi-stream-input`, con un protocolo parecido pero no idéntico.

El plugin 1.6.5 que tenemos instalado no conoce ese camino. El 1.8.3 sí lo trae,
pero atado al prefijo `eleven_v3`, no a v4(los PRs de LiveKit para v4 siguen sin
publicar). Y actualizar el plugin nos sacaría del pin `livekit-agents==1.6.5`, que
es load-bearing: con 1.8.x el plugin de Anthropic revienta el entrypoint.

Así que traducimos nosotros, desde fuera, sin tocar el paquete instalado.

QUÉ CAMBIA ENTRE LOS DOS PROTOCOLOS (medido, no deducido)
---------------------------------------------------------
1. URL: la voz NO va en la ruta. Y desaparecen varios parámetros
   (`enable_ssml_parsing`, `inactivity_timeout`, `auto_mode`).
2. Registro: se manda `voices: [voice_id]` en vez de un primer `text: " "`.
3. Texto: viaja en `inputs: [{text, voice_id}]`, no en `text`.
4. Fin: el servidor cierra con `is_final` (snake_case). El socket normal usa
   `isFinal` (camelCase), que es lo único que mira el plugin. Mirar solo uno deja
   el contexto colgado hasta el timeout y PARECE que v4 no funciona. Me pasó.

SEGURIDAD
---------
Nada de esto se ejecuta salvo que `ELEVENLABS_MODEL` empiece por `eleven_v4`.
Sin esa variable el parche no se aplica y el comportamiento es exactamente el de
antes, así que volver atrás es quitar la variable y reiniciar, sin tocar código.

LO QUE SE SABE QUE SE PIERDE
----------------------------
- v4 habla ~17% más despacio: la misma frase de la llamada 05 dura 9,17s contra
  7,81s. Arranca antes (ttfb 0,11s contra 0,21s) pero termina más tarde, y al que
  llama le importa cuándo terminas.
- El plugin 1.8.3 filtra todo `voice_settings` salvo `stability` en este socket.
  Aquí los mandamos ENTEROS a propósito: es lo que se envió en el A/B que James
  aprobó, y el servidor los acepta sin error. Si algún día v4 suena distinto a
  aquella prueba, empezar mirando aquí.
"""

from __future__ import annotations

import json
from typing import Any

import aiohttp
from livekit.plugins.elevenlabs import tts as _el


def is_v4(model: str) -> bool:
    return str(model).startswith("eleven_v4")


def _dialogue_url(opts: Any) -> str:
    base = opts.base_url.replace("https://", "wss://").replace("http://", "ws://")
    params = [f"model_id={opts.model}", f"output_format={opts.encoding}"]
    if _el.is_given(opts.language):
        params.append(f"language_code={opts.language.language}")
    params.append(f"enable_logging={str(opts.enable_logging).lower()}")
    params.append(f"apply_text_normalization={opts.apply_text_normalization}")
    if opts.sync_alignment:
        params.append("sync_alignment=true")
    return f"{base}/text-to-dialogue/multi-stream-input?" + "&".join(params)


def _dialogue_init_packet(opts: Any, *, context_id: str) -> dict[str, Any]:
    pkt: dict[str, Any] = {"context_id": context_id, "voices": [opts.voice_id]}
    if _el.is_given(opts.voice_settings):
        settings = _el._strip_nones(_el.dataclasses.asdict(opts.voice_settings))
        if settings:
            pkt["voice_settings"] = settings
    return pkt


class _DialogueWS:
    """Envoltorio del websocket que traduce entre los dos protocolos.

    El plugin construye y lee paquetes del socket normal. Aquí los convertimos al
    vuelo, de forma que el plugin no se entera de que está hablando con otro
    endpoint. Todo lo que no traducimos (`closed`, `close_code`, `close()`) se
    delega al websocket real.
    """

    def __init__(self, ws: aiohttp.ClientWebSocketResponse, voice_id: str) -> None:
        self._ws = ws
        self._voice_id = voice_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ws, name)

    async def send_json(self, pkt: dict[str, Any]) -> None:
        # El paquete de registro y el de cierre ya van en la forma correcta.
        if "voices" in pkt or pkt.get("close_context"):
            return await self._ws.send_json(pkt)
        # Paquete de texto: {"text": ..., "context_id": ...} -> inputs[]
        if "text" in pkt:
            out: dict[str, Any] = {
                "context_id": pkt["context_id"],
                "inputs": [{"text": pkt["text"], "voice_id": self._voice_id}],
            }
            if pkt.get("flush"):
                out["flush"] = True
            return await self._ws.send_json(out)
        return await self._ws.send_json(pkt)

    async def receive(self, *a: Any, **kw: Any) -> aiohttp.WSMessage:
        msg = await self._ws.receive(*a, **kw)
        if msg.type is not aiohttp.WSMsgType.TEXT:
            return msg
        try:
            data = json.loads(msg.data)
        except (ValueError, TypeError):
            return msg
        # El plugin solo entiende `isFinal`. Sin esto el contexto nunca se cierra.
        if isinstance(data, dict) and data.get("is_final") and "isFinal" not in data:
            data["isFinal"] = True
            return aiohttp.WSMessage(msg.type, json.dumps(data), msg.extra)
        return msg


async def _connect(self: Any) -> None:
    """Sustituye a `_Connection.connect`, idéntica salvo por el envoltorio."""
    if self._ws or self._closed:
        return
    ws = await self._session.ws_connect(
        _dialogue_url(self._opts),
        headers={_el.AUTHORIZATION_HEADER: self._opts.api_key},
    )
    self._ws = _DialogueWS(ws, self._opts.voice_id)
    self._send_task = _el.asyncio.create_task(self._send_loop())
    self._recv_task = _el.asyncio.create_task(self._recv_loop())


def enable() -> None:
    """Aplica el parche. Idempotente."""
    if getattr(_el, "_v4_patched", False):
        return
    _el._multi_stream_url = _dialogue_url
    _el._build_context_init_packet = _dialogue_init_packet
    _el._Connection.connect = _connect
    _el._v4_patched = True
    print("[V4] parche text-to-dialogue aplicado al plugin de ElevenLabs")

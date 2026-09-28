#!/usr/bin/env python3
"""Throwaway: ¿entiende el STT el catalán? Genera audio catalán con ElevenLabs
(la voz clonada CA) y se lo pasa a Deepgram con varias configuraciones.
"""
import json, os, sys, urllib.request

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV = {}
for line in open(os.path.join(AGENT_DIR, ".env")):
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        ENV[k.strip()] = v.strip().strip('"').strip("'")

EL_KEY = ENV["ELEVENLABS_API_KEY"]
DG_KEY = ENV["DEEPGRAM_API_KEY"]
VOICE_CA = "x5Jilxt6jZdQy7DBFXLh"

FRASE = ("Hola, sóc en James, he aplicat a una posició de consultor "
         "per a l'empresa Marlex.")


def tts(text, voice, model):
    url = (f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
           f"?output_format=mp3_44100_128")
    req = urllib.request.Request(url, data=json.dumps(
        {"text": text, "model_id": model}).encode(), method="POST",
        headers={"content-type": "application/json", "xi-api-key": EL_KEY})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def stt(audio, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(
        f"https://api.deepgram.com/v1/listen?{qs}", data=audio, method="POST",
        headers={"content-type": "audio/mpeg", "Authorization": f"Token {DG_KEY}"})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.load(r)
    alt = d["results"]["channels"][0]["alternatives"][0]
    lang = d["results"]["channels"][0].get("detected_language", "-")
    return alt["transcript"], lang


print(f"FRASE ORIGINAL:\n  {FRASE}\n")

for tts_model in ("eleven_turbo_v2_5", "eleven_multilingual_v2"):
    print(f"--- TTS {tts_model} ---")
    try:
        audio = tts(FRASE, VOICE_CA, tts_model)
    except Exception as exc:
        print(f"  TTS ERROR: {exc}\n")
        continue
    open(f"/tmp/ca_{tts_model}.mp3", "wb").write(audio)
    print(f"  audio ok ({len(audio)} bytes) -> /tmp/ca_{tts_model}.mp3")
    for label, params in [
        ("nova-3 multi", {"model": "nova-3", "language": "multi"}),
        ("nova-3 ca", {"model": "nova-3", "language": "ca"}),
        ("nova-2 ca", {"model": "nova-2", "language": "ca"}),
        ("nova-2 es", {"model": "nova-2", "language": "es"}),
        ("nova-3 es", {"model": "nova-3", "language": "es"}),
    ]:
        try:
            txt, lang = stt(audio, **params)
            print(f"  [{label:14}] lang={lang!r} :: {txt}")
        except Exception as exc:
            body = getattr(exc, "read", lambda: b"")()
            print(f"  [{label:14}] ERROR {exc} {body[:200]!r}")
    print()

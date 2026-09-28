#!/usr/bin/env python3
"""¿Fijar el STT en catalán rompe a un llamante en castellano?"""
import json, os, urllib.request

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV = {}
for line in open(os.path.join(AGENT_DIR, ".env")):
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        ENV[k.strip()] = v.strip().strip('"').strip("'")

EL_KEY, DG_KEY = ENV["ELEVENLABS_API_KEY"], ENV["DEEPGRAM_API_KEY"]
VOICE_ES = ENV.get("ELEVENLABS_VOICE_ID") or "wEQE8Agx2GxUkKIOCNny"

CASOS = [
    ("ES", VOICE_ES, "Hola, soy James, he aplicado a una posición de consultor "
                     "para la empresa Marlex."),
    ("ES", VOICE_ES, "Estaría buscando unos cuarenta y cinco mil brutos al año, "
                     "y podría incorporarme en dos semanas."),
    ("CA", "x5Jilxt6jZdQy7DBFXLh",
     "Estaria buscant uns quaranta-cinc mil bruts a l'any, i em podria "
     "incorporar d'aquí a dues setmanes."),
]


def tts(text, voice):
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
        data=json.dumps({"text": text, "model_id": "eleven_turbo_v2_5"}).encode(),
        method="POST",
        headers={"content-type": "application/json", "xi-api-key": EL_KEY})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def stt(audio, lang, model="nova-3"):
    req = urllib.request.Request(
        f"https://api.deepgram.com/v1/listen?model={model}&language={lang}",
        data=audio, method="POST",
        headers={"content-type": "audio/mpeg", "Authorization": f"Token {DG_KEY}"})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.load(r)
    return d["results"]["channels"][0]["alternatives"][0]["transcript"]


for idioma, voice, frase in CASOS:
    print(f"[{idioma}] {frase}")
    audio = tts(frase, voice)
    for lang in ("ca", "multi", "es"):
        try:
            print(f"   STT lang={lang:5} :: {stt(audio, lang)}")
        except Exception as exc:
            print(f"   STT lang={lang:5} :: ERROR {exc}")
    print()

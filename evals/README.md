# Lander evals — text-level regression harness

Run this **before every `lk agent deploy`**. It exercises Lander's real brain
(same `system_es.md` prompt + directory/caller blocks + tools + `knowledge.py`,
same model) over the scenarios saved in the dashboard's **LanderOS → Evals** tab,
and grades each with a strong judge model. No audio, no phone, no dialling —
~30s, a few cents.

```bash
python3 evals/run.py
```

Needs `ANTHROPIC_API_KEY` in `.env` and the Supabase creds from the dashboard's
`.env.local` (auto-read from the sibling `joblanders-dashboard/`, or pass
`SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` as env vars).

Gate: if it drops below your bar (~11/13, ignoring the "arranque en frío" case —
a known false negative, since the cold-start greeting is static TTS not
reproducible in a text-only test), **don't deploy** — fix the prompt first.
The scenarios themselves are edited in the Evals tab (persisted to Supabase),
not here.

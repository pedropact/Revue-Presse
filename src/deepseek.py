"""Client minimal pour l'API DeepSeek (format compatible OpenAI)."""

import json
import logging
import time

import requests

log = logging.getLogger(__name__)

URL = "https://api.deepseek.com/chat/completions"


class DeepSeek:
    def __init__(self, cle: str, modele: str):
        self.cle = cle
        self.modele = modele

    def json(self, systeme: str, utilisateur: str, max_tokens: int = 8000) -> dict:
        """Envoie une demande et renvoie la réponse JSON décodée."""
        corps = {
            "model": self.modele,
            "messages": [
                {"role": "system", "content": systeme},
                {"role": "user", "content": utilisateur},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        for essai in range(3):
            r = requests.post(URL, json=corps, timeout=600,
                              headers={"Authorization": f"Bearer {self.cle}"})
            if r.ok:
                choix = r.json()["choices"][0]
                if choix.get("finish_reason") not in (None, "stop"):
                    raise RuntimeError(f"DeepSeek : réponse interrompue ({choix['finish_reason']})")
                return json.loads(choix["message"]["content"], strict=False)
            detail = f"DeepSeek HTTP {r.status_code} : {r.text[:200]}"
            if r.status_code not in (429, 500, 502, 503) or essai == 2:
                raise RuntimeError(detail)
            log.info("DeepSeek occupé (%s), nouvel essai dans 20 s", r.status_code)
            time.sleep(20)

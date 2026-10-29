"""Résumé (en français) de chaque sujet : Google Gemini si une clé est configurée,
sinon (ou en cas d'échec) un petit modèle local exécuté sur CPU."""

import json
import logging
import os
import time
from collections import Counter

import requests

from analyse import Sujet

log = logging.getLogger(__name__)

SYSTEME = (
    "Tu es un journaliste québécois qui rédige une revue de presse en français, agréable à lire "
    "sur un téléphone. On te donne un ou plusieurs articles (en français ou en anglais) portant "
    "sur le même sujet. Fusionne l'information de tous les articles et rédige, en français et "
    "avec tes propres mots (ne recopie pas les phrases des articles) :\n"
    "- titre : un titre neutre et informatif (12 mots maximum);\n"
    "- chapeau : une seule phrase (30 mots maximum) qui dit l'essentiel;\n"
    "- points : 3 points clés très courts (15 mots maximum chacun), avec les chiffres importants;\n"
    "- texte : 2 ou 3 paragraphes courts (3 phrases maximum chacun) : contexte, réactions, "
    "conséquences ou prochaines étapes. Sépare les paragraphes par une ligne vide.\n"
    "N'invente aucun fait, aucun chiffre ni aucune citation. "
    'Réponds uniquement en JSON : {"titre": "...", "chapeau": "...", "points": ["...", "...", "..."], "texte": "..."}'
)


def preparer(sujet: Sujet, max_articles: int) -> str:
    # Priorité aux articles avec texte complet, puis aux plus récents
    choisis = sorted(sujet.articles, key=lambda a: (len(a.texte), a.publie), reverse=True)[:max_articles]
    return "\n\n".join(
        f"### Article {i} — {a.source} ({'anglais' if a.langue == 'en' else 'français'})\n"
        f"Titre : {a.titre}\n{a.texte or a.chapeau}"
        for i, a in enumerate(choisis, 1)
    )


def appliquer(sujet: Sujet, reponse: str) -> None:
    data = json.loads(reponse)
    sujet.titre = data["titre"].strip()
    sujet.chapeau = str(data.get("chapeau", "")).strip()
    sujet.points = [str(p).strip() for p in data.get("points", []) if str(p).strip()][:4]
    sujet.resume = str(data.get("texte") or data.get("resume") or "").strip()


class ResumeurLocal:
    def __init__(self, config: dict):
        from llama_cpp import Llama
        m = config["modeles"]
        self.llm = Llama.from_pretrained(
            repo_id=m["llm_repo"],
            filename=m["llm_fichier"],
            n_ctx=m["llm_contexte"],
            n_threads=os.cpu_count(),
            verbose=False,
        )

    def resumer(self, sujet: Sujet, max_articles: int) -> None:
        r = self.llm.create_chat_completion(
            messages=[
                {"role": "system", "content": SYSTEME},
                {"role": "user", "content": preparer(sujet, max_articles)},
            ],
            temperature=0.2,
            max_tokens=1000,
            response_format={"type": "json_object"},
        )
        appliquer(sujet, r["choices"][0]["message"]["content"])


class ResumeurGemini:
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

    def __init__(self, config: dict, cle: str):
        self.modele = config["modeles"]["gemini_modele"]
        self.pause = 60 / config["modeles"]["gemini_requetes_par_minute"]
        self.cle = cle
        self.dernier = 0.0

    def resumer(self, sujet: Sujet, max_articles: int) -> None:
        corps = {
            "systemInstruction": {"parts": [{"text": SYSTEME}]},
            "contents": [{"role": "user", "parts": [{"text": preparer(sujet, max_articles)}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
        }
        for essai in range(4):
            # Respecte la limite de requêtes par minute du forfait gratuit
            attente = self.dernier + self.pause - time.monotonic()
            if attente > 0:
                time.sleep(attente)
            self.dernier = time.monotonic()
            r = requests.post(self.URL.format(self.modele), json=corps, timeout=120,
                              headers={"x-goog-api-key": self.cle})
            if r.status_code in (429, 500, 503):
                log.info("Gemini occupé (%s), nouvel essai dans %d s", r.status_code, 20 * (essai + 1))
                time.sleep(20 * (essai + 1))
                continue
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
            appliquer(sujet, "".join(p["text"] for p in parts if not p.get("thought")))
            return
        raise RuntimeError("Gemini indisponible après plusieurs essais")


class Resumeur:
    """Gemini en priorité; le modèle local n'est chargé qu'en cas de besoin."""

    def __init__(self, config: dict):
        self.config = config
        cle = os.getenv("GEMINI_API_KEY")
        self.gemini = ResumeurGemini(config, cle) if cle else None
        self.local = None
        self.bilan = Counter()
        log.info("Résumés : %s", "Google Gemini" if self.gemini else "modèle local")

    def resumer(self, sujet: Sujet, max_articles: int) -> None:
        if self.gemini:
            try:
                self.gemini.resumer(sujet, max_articles)
                self.bilan["Gemini"] += 1
                return
            except Exception as e:
                log.warning("Gemini a échoué (%s), repli sur le modèle local", e)
                self.bilan["erreur Gemini : " + str(e)[:120]] += 0
        try:
            if self.local is None:
                self.local = ResumeurLocal(self.config)
            self.local.resumer(sujet, max_articles)
            self.bilan["modèle local"] += 1
        except Exception as e:
            log.warning("Résumé impossible (%s), utilisation du texte RSS", e)
            resume_sans_ia(sujet)
            self.bilan["sans IA"] += 1


def resume_sans_ia(sujet: Sujet) -> None:
    """Repli : titre et chapeau d'un article francophone si possible."""
    fr = [a for a in sujet.articles if a.langue == "fr"] or sujet.articles
    a = max(fr, key=lambda a: len(a.chapeau))
    sujet.titre, sujet.chapeau = a.titre, a.chapeau[:600]

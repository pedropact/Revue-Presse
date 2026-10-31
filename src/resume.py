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
    "- texte : 3 ou 4 paragraphes de 3 à 4 phrases chacun : ce qui s'est passé en détail, "
    "le contexte, les chiffres et les réactions, puis les conséquences ou les prochaines étapes. "
    "Chaque paragraphe se termine par une phrase complète. Sépare les paragraphes par une ligne "
    "vide. Si les articles contiennent peu d'information, écris 2 paragraphes plutôt que "
    "d'inventer.\n"
    "N'invente aucun fait, aucun chiffre ni aucune citation. "
    "Un titre accompagné d'un court chapeau suffit pour rédiger un résumé bref. Mets ignorer à "
    "true uniquement si les articles ne contiennent aucune information sur une nouvelle (page "
    "d'erreur, accès refusé, texte vide); dans ce cas, ne rédige rien. "
    'Réponds uniquement en JSON : {"titre": "...", "chapeau": "...", "points": ["...", "...", "..."], "texte": "...", "ignorer": false}'
)


SYSTEME_LOT = SYSTEME.replace(
    "On te donne un ou plusieurs articles (en français ou en anglais) portant sur le même sujet.",
    "On te donne plusieurs SUJETS numérotés; chaque sujet regroupe un ou plusieurs articles "
    "(en français ou en anglais) portant sur la même nouvelle. Traite chaque sujet séparément.",
).split("Réponds uniquement")[0] + (
    'Réponds uniquement en JSON : {"sujets": [{"id": 1, "titre": "...", "chapeau": "...", '
    '"points": ["...", "...", "..."], "texte": "...", "ignorer": false}, ...]} avec un élément par sujet.'
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
    if data.get("ignorer") is True:
        sujet.ignorer = True
        return
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
            max_tokens=1600,
            response_format={"type": "json_object"},
        )
        appliquer(sujet, r["choices"][0]["message"]["content"])


class QuotaEpuise(Exception):
    pass


class ResumeurGemini:
    """Un seul appel par section (quelques sujets à la fois) pour rester dans le forfait gratuit."""

    URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

    def __init__(self, config: dict, cle: str):
        m = config["modeles"]
        self.modeles = list(m["gemini_modeles"])
        self.pause = 60 / m["gemini_requetes_par_minute"]
        self.cle = cle
        self.dernier = 0.0

    def appeler(self, modele: str, texte: str) -> str:
        corps = {
            "systemInstruction": {"parts": [{"text": SYSTEME_LOT}]},
            "contents": [{"role": "user", "parts": [{"text": texte}]}],
            "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json",
                                 "maxOutputTokens": 32768},
        }
        for essai in range(2):
            # Respecte la limite de requêtes par minute du forfait gratuit
            attente = self.dernier + self.pause - time.monotonic()
            if attente > 0:
                time.sleep(attente)
            self.dernier = time.monotonic()
            r = requests.post(self.URL.format(modele), json=corps, timeout=300,
                              headers={"x-goog-api-key": self.cle})
            if r.ok:
                parts = r.json()["candidates"][0]["content"]["parts"]
                return "".join(p["text"] for p in parts if not p.get("thought"))
            detail = f"{modele} HTTP {r.status_code} : {r.text[:200]}"
            if r.status_code == 429 and "PerDay" in r.text:
                raise QuotaEpuise(detail)
            if r.status_code not in (429, 500, 503) or essai == 1:
                raise RuntimeError(detail)
            log.info("Gemini occupé (%s), nouvel essai dans 30 s", r.status_code)
            time.sleep(30)

    def resumer_lot(self, sujets: list[Sujet], max_articles: int) -> list[Sujet]:
        """Résume les sujets; renvoie ceux qui n'ont pas pu l'être."""
        texte = "\n\n".join(
            f"======== SUJET {i} ========\n{preparer(s, max_articles)}" for i, s in enumerate(sujets, 1)
        )
        reponse, derniere_erreur = None, None
        for modele in list(self.modeles):
            try:
                reponse = json.loads(self.appeler(modele, texte))
                break
            except QuotaEpuise as e:
                log.warning("Quota quotidien épuisé : %s", e)
                self.modeles.remove(modele)  # inutile de le réessayer aujourd'hui
                derniere_erreur = e
            except Exception as e:
                # Surcharge passagère (503...) : on tente le modèle suivant pour ce lot
                log.warning("Échec de %s : %s", modele, e)
                derniere_erreur = e
        if reponse is None:
            if not self.modeles:
                raise QuotaEpuise("tous les modèles Gemini ont atteint leur quota")
            raise RuntimeError(str(derniere_erreur))
        restants = []
        resultats = {int(r.get("id", 0)): r for r in reponse.get("sujets", [])}
        for i, s in enumerate(sujets, 1):
            try:
                appliquer(s, json.dumps(resultats[i]))
            except (KeyError, TypeError, ValueError):
                restants.append(s)
        return restants


class Resumeur:
    """Gemini en priorité; le modèle local n'est chargé qu'en cas de besoin."""

    def __init__(self, config: dict):
        self.config = config
        cle = os.getenv("GEMINI_API_KEY")
        self.gemini = ResumeurGemini(config, cle) if cle else None
        self.local = None
        self.bilan = Counter()
        log.info("Résumés : %s", "Google Gemini" if self.gemini else "modèle local")

    def resumer_section(self, sujets: list[Sujet], max_articles: int) -> None:
        restants = sujets
        if self.gemini and sujets:
            try:
                restants = self.gemini.resumer_lot(sujets, max_articles)
                if restants:
                    # Gemini a parfois oublié un sujet du lot : un second essai pour ceux-là seulement
                    restants = self.gemini.resumer_lot(restants, max_articles)
                self.bilan["Gemini"] += len(sujets) - len(restants)
            except Exception as e:
                log.warning("Gemini a échoué (%s), repli sur le modèle local", e)
                self.bilan["erreur Gemini : " + " ".join(str(e).split())[:250]] += 0
                if isinstance(e, QuotaEpuise):
                    self.gemini = None
        for s in restants:
            self.resumer_local(s, max_articles)

    def resumer_local(self, sujet: Sujet, max_articles: int) -> None:
        try:
            if self.local is None:
                self.local = ResumeurLocal(self.config)
            self.local.resumer(sujet, max_articles)
            self.bilan["modèle local"] += 1
        except Exception as e:
            log.warning("Résumé impossible (%s), utilisation du texte RSS", e)
            resume_sans_ia(sujet)
            self.bilan["sans IA"] += 1
            self.bilan["erreur modèle local : " + " ".join(str(e).split())[:200]] += 0


def resume_sans_ia(sujet: Sujet) -> None:
    """Repli : titre et chapeau d'un article francophone si possible."""
    fr = [a for a in sujet.articles if a.langue == "fr"] or sujet.articles
    a = max(fr, key=lambda a: len(a.chapeau))
    sujet.titre, sujet.chapeau = a.titre, a.chapeau[:600]

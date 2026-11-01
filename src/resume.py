"""Résumé (en français) de chaque sujet.

Le modèle principal est choisi dans config.yaml (modeles.principal) :
- « local » : Qwen 2.5 exécuté sur le processeur du serveur GitHub, Gemini en secours;
- « gemini » : Google Gemini, le modèle local en secours.
En dernier recours, le titre et le chapeau du flux RSS sont affichés."""

import json
import logging
import os
import re
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


def preparer(sujet: Sujet, max_articles: int, max_car: int | None = None) -> str:
    # Priorité aux articles avec texte complet, puis aux plus récents
    choisis = sorted(sujet.articles, key=lambda a: (len(a.texte), a.publie), reverse=True)[:max_articles]
    return "\n\n".join(
        f"### Article {i} — {a.source} ({'anglais' if a.langue == 'en' else 'français'})\n"
        f"Titre : {a.titre}\n{(a.texte or a.chapeau)[:max_car]}"
        for i, a in enumerate(choisis, 1)
    )


# Caractères chinois, japonais ou coréens : signe qu'un modèle a dérivé hors du français
NON_FRANCAIS = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def appliquer(sujet: Sujet, reponse: str) -> None:
    data = json.loads(reponse)
    if data.get("ignorer") is True:
        sujet.ignorer = True
        return
    contenu = " ".join([str(data.get("titre", "")), str(data.get("chapeau", "")),
                        str(data.get("texte", "")), *map(str, data.get("points", []))])
    if NON_FRANCAIS.search(contenu):
        raise ValueError("texte rédigé dans une autre langue que le français")
    if not str(data.get("titre", "")).strip():
        raise ValueError("réponse sans titre")
    sujet.titre = data["titre"].strip()
    sujet.chapeau = str(data.get("chapeau", "")).strip()
    sujet.points = [str(p).strip() for p in data.get("points", []) if str(p).strip()][:4]
    sujet.resume = str(data.get("texte") or data.get("resume") or "").strip()


# Structure imposée à la réponse du modèle local (un sujet à la fois)
SCHEMA_SUJET = {
    "type": "object",
    "properties": {
        "titre": {"type": "string"},
        "chapeau": {"type": "string"},
        "points": {"type": "array", "items": {"type": "string"}},
        "texte": {"type": "string"},
        "ignorer": {"type": "boolean"},
    },
    "required": ["titre", "chapeau", "points", "texte", "ignorer"],
}

CONSIGNE_LANGUE = (
    "\nIMPORTANT : écris uniquement en français, jamais en chinois ni en anglais, "
    "même si les articles sont en anglais."
)


class ResumeurLocal:
    def __init__(self, config: dict):
        from llama_cpp import Llama
        m = config["modeles"]
        self.max_articles = m["local_max_articles"]
        self.max_car = m["local_max_caracteres"]
        self.llm = Llama.from_pretrained(
            repo_id=m["llm_repo"],
            filename=m["llm_fichier"],
            n_ctx=m["llm_contexte"],
            n_threads=os.cpu_count(),
            verbose=False,
        )

    def resumer(self, sujet: Sujet) -> None:
        derniere = None
        for essai in range(2):  # un second essai si le modèle dérive vers une autre langue
            r = self.llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": SYSTEME + CONSIGNE_LANGUE},
                    {"role": "user", "content": preparer(sujet, self.max_articles, self.max_car)},
                ],
                temperature=0.2 if essai == 0 else 0.1,
                max_tokens=1600,
                response_format={"type": "json_object", "schema": SCHEMA_SUJET},
            )
            try:
                return appliquer(sujet, r["choices"][0]["message"]["content"])
            except ValueError as e:  # JSON invalide, autre langue ou réponse vide
                derniere = e
        raise derniere


# Structure imposée à la réponse de Gemini : garantit un JSON valide, même pour de longs textes
SCHEMA_LOT = {
    "type": "OBJECT",
    "properties": {
        "sujets": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "id": {"type": "INTEGER"},
                    "titre": {"type": "STRING"},
                    "chapeau": {"type": "STRING"},
                    "points": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "texte": {"type": "STRING"},
                    "ignorer": {"type": "BOOLEAN"},
                },
                "required": ["id", "ignorer"],
            },
        }
    },
    "required": ["sujets"],
}


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
                                 "responseSchema": SCHEMA_LOT, "maxOutputTokens": 32768},
        }
        for essai in range(3):
            # Respecte la limite de requêtes par minute du forfait gratuit
            attente = self.dernier + self.pause - time.monotonic()
            if attente > 0:
                time.sleep(attente)
            self.dernier = time.monotonic()
            r = requests.post(self.URL.format(modele), json=corps, timeout=300,
                              headers={"x-goog-api-key": self.cle})
            if r.ok:
                candidat = r.json()["candidates"][0]
                fin = candidat.get("finishReason", "STOP")
                if fin != "STOP":
                    # Ex. MAX_TOKENS (réponse coupée) ou RECITATION (trop proche d'un texte source)
                    raise RuntimeError(f"{modele} : réponse interrompue ({fin})")
                parts = candidat["content"]["parts"]
                return "".join(p["text"] for p in parts if not p.get("thought"))
            detail = f"{modele} HTTP {r.status_code} : {r.text[:200]}"
            if r.status_code == 429 and "PerDay" in r.text:
                raise QuotaEpuise(detail)
            if r.status_code == 400 and "responseSchema" in corps["generationConfig"]:
                # Schéma refusé par ce modèle : on réessaie aussitôt en JSON simple
                log.warning("Schéma refusé par %s, essai sans schéma", modele)
                del corps["generationConfig"]["responseSchema"]
                self.dernier = 0.0
                continue
            if r.status_code not in (429, 500, 503) or essai == 2:
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
    def __init__(self, config: dict):
        self.config = config
        self.principal = config["modeles"].get("principal", "gemini")
        cle = os.getenv("GEMINI_API_KEY")
        self.gemini = ResumeurGemini(config, cle) if cle else None
        self.local = None
        self.local_hs = False  # le modèle local n'a pas pu être chargé
        self.bilan = Counter()
        log.info("Résumés : modèle principal %s, Gemini %s",
                 self.principal, "disponible" if self.gemini else "absent")

    def noter(self, cle: str, e: Exception) -> None:
        self.bilan[cle + " : " + " ".join(str(e).split())[:200]] += 0

    def resumer_section(self, sujets: list[Sujet], max_articles: int, nb: int) -> None:
        if self.principal == "local":
            self.section_locale(sujets, max_articles, nb)
        else:
            self.section_gemini(sujets, max_articles)

    # --- Modèle local en premier -------------------------------------------------------

    def section_locale(self, sujets: list[Sujet], max_articles: int, nb: int) -> None:
        """Résume dans l'ordre jusqu'à obtenir nb sujets : les réserves ne sont résumées
        que si un sujet est écarté, ce qui économise beaucoup de temps de calcul."""
        echecs, retenus = [], 0
        for s in sujets:
            if retenus >= nb:
                s.non_resume = True  # réserve inutile : non résumée, non affichée
                continue
            if self.essai_local(s):
                retenus += not s.ignorer
            else:
                echecs.append(s)
                retenus += 1
        # Secours : Gemini pour les sujets que le modèle local n'a pas su rédiger
        restants = echecs
        if echecs and self.gemini:
            try:
                restants = self.gemini.resumer_lot(echecs, max_articles)
                self.bilan["Gemini (secours)"] += len(echecs) - len(restants)
            except Exception as e:
                self.noter("erreur Gemini", e)
                if isinstance(e, QuotaEpuise):
                    self.gemini = None
        for s in restants:
            resume_sans_ia(s)
            self.bilan["sans IA"] += 1

    def essai_local(self, sujet: Sujet) -> bool:
        if self.local_hs:
            return False
        try:
            if self.local is None:
                self.local = ResumeurLocal(self.config)
        except Exception as e:
            self.local_hs = True
            self.noter("modèle local indisponible", e)
            return False
        debut = time.monotonic()
        try:
            self.local.resumer(sujet)
        except Exception as e:
            self.noter("erreur modèle local", e)
            return False
        self.bilan["modèle local"] += 1
        self.bilan["secondes modèle local"] += round(time.monotonic() - debut)
        return True

    # --- Gemini en premier -------------------------------------------------------------

    def section_gemini(self, sujets: list[Sujet], max_articles: int) -> None:
        restants = sujets
        if self.gemini and sujets:
            try:
                restants = self.gemini.resumer_lot(sujets, max_articles)
                if restants:
                    # Gemini a parfois oublié un sujet du lot : un second essai pour ceux-là seulement
                    restants = self.gemini.resumer_lot(restants, max_articles)
                self.bilan["Gemini"] += len(sujets) - len(restants)
            except Exception as e:
                log.warning("Gemini a échoué sur la section (%s)", e)
                self.noter("erreur Gemini", e)
                if isinstance(e, QuotaEpuise):
                    self.gemini = None
                else:
                    # Réponse illisible ou incomplète pour le lot : on retente sujet par sujet
                    restants = self.un_par_un(sujets, max_articles)
        for s in restants:
            if not self.essai_local(s):
                resume_sans_ia(s)
                self.bilan["sans IA"] += 1

    def un_par_un(self, sujets: list[Sujet], max_articles: int) -> list[Sujet]:
        restants = []
        for s in sujets:
            try:
                if self.gemini and not self.gemini.resumer_lot([s], max_articles):
                    self.bilan["Gemini"] += 1
                    continue
            except QuotaEpuise:
                self.gemini = None
            except Exception as e:
                log.warning("Gemini a échoué sur un sujet (%s)", e)
            restants.append(s)
        return restants


def resume_sans_ia(sujet: Sujet) -> None:
    """Repli : titre et chapeau d'un article francophone si possible."""
    fr = [a for a in sujet.articles if a.langue == "fr"] or sujet.articles
    a = max(fr, key=lambda a: len(a.chapeau))
    sujet.titre, sujet.chapeau = a.titre, a.chapeau[:600]

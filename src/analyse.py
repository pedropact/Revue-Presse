"""Classement par thème, regroupement des articles traitant du même sujet, et classement."""

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np

from collecte import Article

log = logging.getLogger(__name__)


class Encodeur:
    def __init__(self, modele: str):
        from fastembed import TextEmbedding
        self.modele = TextEmbedding(modele)

    def encoder(self, textes: list[str]) -> np.ndarray:
        v = np.array(list(self.modele.embed(textes)), dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)


def contient(texte: str, mots: list[str]) -> int:
    texte = texte.lower()
    return sum(1 for m in mots if re.search(r"\b" + re.escape(m.lower()) + r"\b", texte))


def classer_themes(articles: list[Article], vecteurs: np.ndarray, config: dict, enc: Encodeur) -> None:
    """Attribue à chaque article un score par catégorie (similarité + mots-clés + indice du flux)."""
    ancrage = config.get("ancrage", [])
    for theme in config["categories"]:
        protos = enc.encoder(theme["descriptions"])
        sims = (vecteurs @ protos.T).max(axis=1)
        for a, sim in zip(articles, sims):
            texte = a.texte_court()
            score = float(sim) + min(0.05 * contient(texte, theme.get("mots_cles", [])), 0.15)
            if a.indice_theme == theme["id"]:
                score += 0.10
            if theme.get("ancrage_requis") and not contient(texte, ancrage):
                score -= 1.0
            a.themes[theme["id"]] = score


@dataclass
class Sujet:
    articles: list[Article] = field(default_factory=list)
    centre: np.ndarray | None = None
    theme: str | None = None
    pertinence: float = 0.0
    score: float = 0.0
    titre: str = ""
    chapeau: str = ""
    points: list[str] = field(default_factory=list)
    resume: str = ""
    ignorer: bool = False

    @property
    def sources(self) -> set[str]:
        return {a.source for a in self.articles}

    @property
    def plus_recent(self) -> datetime:
        return max(a.publie for a in self.articles)


def regrouper(articles: list[Article], vecteurs_titres: np.ndarray, seuil: float) -> list[Sujet]:
    """Regroupement glouton par centroïde : un article rejoint le sujet le plus proche si
    la similarité dépasse le seuil. Fonctionne entre le français et l'anglais car le modèle
    d'embeddings est multilingue."""
    sujets: list[Sujet] = []
    ordre = sorted(range(len(articles)), key=lambda i: articles[i].publie)
    for i in ordre:
        v = vecteurs_titres[i]
        meilleur, sim_max = None, seuil
        for s in sujets:
            sim = float(s.centre @ v)
            if sim >= sim_max:
                meilleur, sim_max = s, sim
        if meilleur is None:
            sujets.append(Sujet(articles=[articles[i]], centre=v.copy()))
        else:
            n = len(meilleur.articles)
            meilleur.articles.append(articles[i])
            c = meilleur.centre * n + v
            meilleur.centre = c / np.linalg.norm(c)
    log.info("%d articles regroupés en %d sujets", len(articles), len(sujets))
    return sujets


def portee(sujet: Sujet, config: dict) -> str:
    """« quebec » si les articles parlent davantage du Québec que du Canada, sinon « canada »."""
    q = sum(contient(a.texte_court(), config["marqueurs_quebec"]) for a in sujet.articles)
    c = sum(contient(a.texte_court(), config["marqueurs_canada"]) for a in sujet.articles)
    return "quebec" if q > c else "canada"


def selectionner(sujets: list[Sujet], config: dict) -> dict[str, list[Sujet]]:
    """Attribue chaque sujet à une section (catégorie + portée) et retient les meilleurs."""
    g = config["general"]
    maintenant = datetime.now(timezone.utc)
    categories = [c["id"] for c in config["categories"]]
    par_section = {s["id"]: [] for s in config["sections"]}
    for s in sujets:
        moyennes = {c: float(np.mean([a.themes[c] for a in s.articles])) for c in categories}
        categorie, pertinence = max(moyennes.items(), key=lambda kv: kv[1])
        if pertinence < g["seuil_theme"]:
            continue
        geo = portee(s, config)
        section = next(
            (sec for sec in config["sections"]
             if sec["categorie"] == categorie and sec["portee"] in ("tous", geo)),
            None,
        )
        if section is None:
            continue
        heures = (maintenant - s.plus_recent).total_seconds() / 3600
        s.theme, s.pertinence = section["id"], pertinence
        # Le nombre de médias différents qui couvrent un sujet est le meilleur signal d'importance
        s.score = (
            1.0 * len(s.sources)
            + 0.2 * min(len(s.articles), 6)
            + 1.0 * math.exp(-heures / 12)
            + 3.0 * pertinence
        )
        par_section[section["id"]].append(s)
    for sec in config["sections"]:
        # On garde quelques sujets de réserve pour remplacer ceux que l'IA écarterait
        garde = sec["nb"] + g.get("reserve_par_section", 0)
        par_section[sec["id"]] = sorted(par_section[sec["id"]], key=lambda s: s.score, reverse=True)[:garde]
    return par_section

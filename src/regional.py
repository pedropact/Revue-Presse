"""Section régionale : nouvelles de la Rive-Sud, de la Montérégie et de Montréal, classées
selon la ville (Longueuil et ses voisines d'abord) puis selon le thème (sécurité, circulation,
politique, économie, santé et éducation d'abord)."""

import math
import re
from datetime import datetime, timezone

from analyse import Sujet


def _motifs(mots: list[str]) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(re.escape(m) for m in mots) + r")\b", re.IGNORECASE)


class Regional:
    def __init__(self, config: dict):
        r = config["regional"]
        self.section = r["section"]
        self.sources = {s["nom"] for s in config["sources"] if s.get("regional")}
        self.description = r["description"]
        self.villes = [(_motifs(gr["villes"]), gr["poids"], gr.get("admissible", True)) for gr in r["villes"]]
        # Thème des sections thématiques -> thème régional (ex. « societe » compte comme éducation)
        self.theme_par_section = {sec["id"]: r["themes_par_categorie"].get(sec["categorie"], sec["categorie"])
                                  for sec in config["sections"]}
        self.circulation = _motifs(r["mots_circulation"])
        self.exclure = _motifs(r.get("exclure", [])) if r.get("exclure") else None
        self.themes_prioritaires = set(r["themes_prioritaires"])
        self.poids_theme = r["poids_theme_prioritaire"], r["poids_autre_theme"]

    @staticmethod
    def _texte(s: Sujet) -> str:
        return " ".join(f"{a.titre}. {a.chapeau}" for a in s.articles)

    def poids_ville(self, s: Sujet) -> float:
        texte = self._texte(s)
        return max((poids for motif, poids, _ in self.villes if motif.search(texte)), default=0.0)

    def ville_mentionnee(self, s: Sujet) -> bool:
        """Mentionne une ville de la Rive-Sud ou de la Montérégie (pas seulement Montréal)."""
        texte = self._texte(s)
        return any(adm and motif.search(texte) for motif, _, adm in self.villes)

    def hors_region(self, s: Sujet) -> bool:
        """Parle d'une autre région du Québec (ex. Lévis, Rive-Sud de Québec) sans mentionner
        aucune ville de la nôtre."""
        return bool(self.exclure and self.exclure.search(self._texte(s))) and not self.ville_mentionnee(s)

    def admissible(self, s: Sujet) -> bool:
        """Publié par un média régional, ou mentionne une ville de la Rive-Sud ou de la Montérégie
        (une simple mention de Montréal ne suffit pas : les grands médias y sont tous basés)."""
        if self.hors_region(s):
            return False
        if s.sources & self.sources:
            return True
        texte = self._texte(s)
        return any(adm and motif.search(texte) for motif, _, adm in self.villes)

    def theme(self, s: Sujet, theme_ia: str | None = None, section: str | None = None) -> str:
        """Thème régional : celui donné par l'IA, sinon circulation si les mots-clés le montrent,
        sinon le thème de la section thématique du sujet."""
        if theme_ia:
            return theme_ia
        if self.circulation.search(self._texte(s)):
            return "circulation"
        return self.theme_par_section.get(section or s.theme or "", "autre")

    def score(self, s: Sujet, theme: str) -> float:
        heures = (datetime.now(timezone.utc) - s.plus_recent).total_seconds() / 3600
        prioritaire, autre = self.poids_theme
        return (2.0 * self.poids_ville(s)
                + (prioritaire if theme in self.themes_prioritaires else autre)
                + 0.5 * len(s.sources)
                + 1.0 * math.exp(-heures / 12))

    def section_par_similarite(self, sujets: list[Sujet], nb_garde: int) -> list[Sujet]:
        """Sans l'IA : tous les sujets admissibles, classés par ville puis par thème."""
        retenus = [s for s in sujets if self.admissible(s)]
        for s in retenus:
            s.score_regional = self.score(s, self.theme(s))
        return sorted(retenus, key=lambda s: s.score_regional, reverse=True)[:nb_garde]

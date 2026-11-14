"""Choix éditorial par l'IA : regroupe les articles qui racontent le même événement et
attribue à chaque événement une ou deux sections (ex. une manifestation qui tourne à
l'affrontement avec la police : Société et Sécurité)."""

import logging
import math
from datetime import datetime, timezone

from analyse import Sujet
from deepseek import DeepSeek
from regional import Regional

log = logging.getLogger(__name__)

MAX_CANDIDATS = 220


def editorialiser(sujets: list[Sujet], config: dict, ds: DeepSeek) -> dict[str, list[Sujet]]:
    g = config["general"]
    # Les pré-regroupements par similarité servent de point de départ; l'IA les fusionne au besoin
    reg = Regional(config) if "regional" in config else None
    tries = sorted(sujets, key=lambda s: (len(s.sources), s.plus_recent), reverse=True)
    candidats = tries[:MAX_CANDIDATS]
    if reg:  # les nouvelles locales ont souvent un seul média : on les ajoute quand même
        deja_la = {id(s) for s in candidats}
        candidats += [s for s in tries[MAX_CANDIDATS:] if reg.admissible(s) and id(s) not in deja_la][:120]
    lignes = []
    for i, s in enumerate(candidats):
        titres = " / ".join(dict.fromkeys(a.titre[:120] for a in s.articles))[:300]
        lignes.append(f"{i}: {titres} [{', '.join(sorted(s.sources))}]")

    descriptions = {c["id"]: c["descriptions"][0] for c in config["categories"]}
    if reg:
        descriptions["regional"] = reg.description
    sections = "\n".join(
        f"- {sec['id']} : {sec['titre']} ({descriptions.get(sec['categorie'], '')})"
        for sec in config["sections"]
    )
    max_sec = g.get("max_sections_par_sujet", 2)
    systeme = (
        "Tu es le rédacteur en chef d'une revue de presse sur l'actualité du Québec et du Canada. "
        "On te donne une liste numérotée de nouvelles (titres de médias québécois et canadiens, "
        "en français ou en anglais). Ta tâche :\n"
        "1. Regroupe les numéros qui racontent le MÊME événement, même si les titres sont très "
        "différents ou dans des langues différentes.\n"
        f"2. Pour chaque événement, choisis de 0 à {max_sec} sections thématiques dans la liste ci-dessous, "
        "la plus pertinente en premier. Ajoute une seconde section seulement si l'événement la "
        "concerne vraiment (ex. une manifestation étudiante avec affrontements policiers : "
        "societe puis securite). Laisse la liste vide pour les sports, les spectacles, la "
        "météo, les faits divers mineurs et ce qui ne concerne pas le Canada ou le Québec. "
        "La section regional s'AJOUTE aux sections thématiques, elle ne les remplace pas : une "
        "nouvelle de la région qui a une portée plus large reçoit aussi sa ou ses sections "
        "thématiques (ex. une grève étudiante avec affrontements policiers à Montréal et sur la "
        "Rive-Sud : societe, securite et regional). Pour la section regional, garde aussi les "
        "nouvelles locales, même petites (accidents, circulation, conseils municipaux, écoles, "
        "commerces), mais pas les nouvelles nationales simplement reprises par un média régional, "
        "ni les publireportages (palmarès d'entreprises, publicités).\n"
        "3. Donne une importance de 1 (mineur) à 5 (majeur) pour le public québécois.\n"
        "4. Pour les événements de la section regional, ajoute theme_regional parmi : securite, "
        "circulation, politique, economie, sante, education, autre.\n\n"
        f"Sections :\n{sections}\n\n"
        'Réponds uniquement en JSON : {"evenements": [{"ids": [3, 17], "sections": ["societe", '
        '"securite"], "importance": 4, "theme_regional": null}, ...]}. Chaque numéro apparaît dans un seul événement; '
        "n'inclus pas les événements sans section."
    )
    reponse = ds.json(systeme, "\n".join(lignes), max_tokens=8000)

    valides = {sec["id"] for sec in config["sections"]}
    maintenant = datetime.now(timezone.utc)
    par_section = {sec["id"]: [] for sec in config["sections"]}
    deja = set()
    for ev in reponse.get("evenements", []):
        ids = [i for i in ev.get("ids", []) if isinstance(i, int) and 0 <= i < len(candidats) and i not in deja]
        # Jusqu'à max_sec sections thématiques, plus la section régionale le cas échéant
        brutes = [x for x in ev.get("sections", []) if x in valides]
        secs = [x for x in brutes if not reg or x != reg.section][:max_sec]
        if reg and reg.section in brutes:
            secs.append(reg.section)
        if not ids:
            continue
        articles = [a for i in ids for a in candidats[i].articles]
        s = Sujet(articles=articles)
        # Les médias régionaux couvrent aussi l'actualité nationale : seule la mention d'une ville
        # de la région ajoute d'office la section régionale; sinon on suit le choix de l'IA
        if reg and reg.section not in secs and reg.ville_mentionnee(s):
            secs.append(reg.section)
        if not secs:
            continue
        deja.update(ids)
        heures = (maintenant - s.plus_recent).total_seconds() / 3600
        importance = max(1, min(5, int(ev.get("importance", 2))))
        s.theme = secs[0]
        s.score = (1.0 * len(s.sources) + 0.2 * min(len(articles), 6)
                   + 1.0 * math.exp(-heures / 12) + 1.0 * importance)
        if reg and reg.section in secs:
            autre = next((x for x in secs if x != reg.section), None)
            s.score_regional = reg.score(s, reg.theme(s, ev.get("theme_regional"), autre))
        for sec in secs:
            par_section[sec].append(s)

    for sec in config["sections"]:
        garde = sec["nb"] + g.get("reserve_par_section", 0)
        cle = (lambda s: s.score_regional) if reg and sec["id"] == reg.section else (lambda s: s.score)
        par_section[sec["id"]] = sorted(par_section[sec["id"]], key=cle, reverse=True)[:garde]
    log.info("Choix éditorial : %d événements retenus", sum(len(v) for v in par_section.values()))
    return par_section

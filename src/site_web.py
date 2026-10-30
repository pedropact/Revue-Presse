"""Génère le site statique (PWA) à partir des sujets retenus."""

import html
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from analyse import Sujet

FUSEAU = ZoneInfo("America/Toronto")
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
        "août", "septembre", "octobre", "novembre", "décembre"]
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def date_longue(d: datetime) -> str:
    d = d.astimezone(FUSEAU)
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]} {d.year}"


def vers_dict(s: Sujet) -> dict:
    return {
        "titre": s.titre,
        "chapeau": s.chapeau,
        "points": s.points,
        "texte": s.resume,
        "sources": [
            {"nom": a.source, "titre": a.titre, "lien": a.lien, "langue": a.langue,
             "publie": a.publie.isoformat()}
            for a in sorted(s.articles, key=lambda a: a.publie, reverse=True)
        ],
    }


def generer(par_theme: dict[str, list[Sujet]], config: dict, sortie: Path, modele_dir: Path,
            avis: str | None = None) -> None:
    sortie.mkdir(parents=True, exist_ok=True)
    for f in modele_dir.iterdir():
        if f.name != "index.html":
            shutil.copy(f, sortie / f.name)

    maintenant = datetime.now(timezone.utc)
    donnees = {
        "genere": maintenant.isoformat(),
        "themes": [
            {"id": t["id"], "titre": t["titre"], "sujets": [vers_dict(s) for s in par_theme[t["id"]]]}
            for t in config["sections"]
        ],
    }
    (sortie / "data.json").write_text(json.dumps(donnees, ensure_ascii=False, indent=1), "utf-8")

    e = html.escape
    nav, sections = [], []
    for t, sec in zip(donnees["themes"], config["sections"]):
        nav.append(f'<a href="#{t["id"]}">{e(sec.get("court", t["titre"]))}</a>')
        cartes = []
        for n, s in enumerate(t["sujets"], 1):
            # Un lien par média (l'article le plus récent), même s'il a publié plusieurs articles
            par_media = {}
            for src in s["sources"]:
                par_media.setdefault(src["nom"], src)
            liens = " · ".join(
                f'<a href="{e(src["lien"])}" target="_blank" rel="noopener" title="{e(src["titre"])}">'
                f'{e(src["nom"])}{" (EN)" if src["langue"] == "en" else ""}</a>'
                for src in par_media.values()
            )
            nb = len(par_media)
            badge = f'<span class="badge">{nb} médias</span>' if nb > 1 else ""
            chapeau = f'<p class="chapeau">{e(s["chapeau"])}</p>' if s["chapeau"] else ""
            points = ("<ul class=\"points\">" + "".join(f"<li>{e(p)}</li>" for p in s["points"]) + "</ul>") if s["points"] else ""
            paragraphes = "".join(f"<p>{e(p.strip())}</p>" for p in s["texte"].split("\n") if p.strip())
            suite = (f'<details><summary>Lire la suite</summary><div class="texte">{paragraphes}</div></details>'
                     if paragraphes else "")
            cartes.append(
                f'<article class="carte"><div class="meta"><span class="num">{n}</span>{badge}</div>'
                f'<h3>{e(s["titre"])}</h3>{chapeau}{points}{suite}'
                f'<p class="sources">Sources : {liens}</p></article>'
            )
        contenu = "".join(cartes) if cartes else '<p class="vide">Aucune nouvelle marquante aujourd\'hui.</p>'
        nb_sujets = f'<span class="compte">{len(cartes)}</span>' if cartes else ""
        sections.append(
            f'<details class="section" id="{t["id"]}"><summary><h2>{e(t["titre"])}</h2>{nb_sujets}'
            f'<span class="chevron" aria-hidden="true"></span></summary>{contenu}</details>'
        )

    heure = maintenant.astimezone(FUSEAU).strftime("%H h %M")
    page = (modele_dir / "index.html").read_text("utf-8")
    page = (page.replace("{{DATE}}", e(date_longue(maintenant)))
                .replace("{{HEURE}}", heure)
                .replace("{{NAV}}", "".join(nav))
                .replace("{{AVIS}}", f'<p class="avis" role="alert">{e(avis)}</p>' if avis else "")
                .replace("{{SECTIONS}}", "\n".join(sections)))
    (sortie / "index.html").write_text(page, "utf-8")

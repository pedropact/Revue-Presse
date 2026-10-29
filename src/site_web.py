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
        "resume": s.resume,
        "sources": [
            {"nom": a.source, "titre": a.titre, "lien": a.lien, "langue": a.langue,
             "publie": a.publie.isoformat()}
            for a in sorted(s.articles, key=lambda a: a.publie, reverse=True)
        ],
    }


def generer(par_theme: dict[str, list[Sujet]], config: dict, sortie: Path, modele_dir: Path) -> None:
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
    for t in donnees["themes"]:
        nav.append(f'<a href="#{t["id"]}">{e(t["titre"])}</a>')
        items = []
        for s in t["sujets"]:
            liens = " · ".join(
                f'<a href="{e(src["lien"])}" target="_blank" rel="noopener" title="{e(src["titre"])}">'
                f'{e(src["nom"])}{" (EN)" if src["langue"] == "en" else ""}</a>'
                for src in s["sources"]
            )
            nb = len({src["nom"] for src in s["sources"]})
            badge = f'<span class="badge">{nb} médias</span>' if nb > 1 else ""
            paragraphes = "".join(f"<p>{e(par.strip())}</p>" for par in s["resume"].split("\n") if par.strip())
            items.append(
                f'<li><h3>{e(s["titre"])} {badge}</h3>{paragraphes}'
                f'<p class="sources">{liens}</p></li>'
            )
        contenu = "<ol>" + "".join(items) + "</ol>" if items else '<p class="vide">Aucune nouvelle marquante aujourd\'hui.</p>'
        sections.append(f'<section id="{t["id"]}"><h2>{e(t["titre"])}</h2>{contenu}</section>')

    heure = maintenant.astimezone(FUSEAU).strftime("%H h %M")
    page = (modele_dir / "index.html").read_text("utf-8")
    page = (page.replace("{{DATE}}", e(date_longue(maintenant)))
                .replace("{{HEURE}}", heure)
                .replace("{{NAV}}", "".join(nav))
                .replace("{{SECTIONS}}", "\n".join(sections)))
    (sortie / "index.html").write_text(page, "utf-8")

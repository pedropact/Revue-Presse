"""Revue de presse quotidienne Québec / Canada.

Usage :
    python src/main.py                 # exécution complète
    python src/main.py --sans-ia       # sans modèle de résumé (test rapide)
    python src/main.py --verifier-flux # vérifie seulement que les flux RSS répondent
"""

import argparse
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from analyse import Encodeur, classer_themes, regrouper, selectionner
from collecte import collecter, extraire_texte, lire_flux
from jeton import avis_jeton
from resume import Resumeur, resume_sans_ia
from site_web import generer

RACINE = Path(__file__).resolve().parent.parent
log = logging.getLogger("revue")


def verifier_flux(config: dict) -> None:
    from datetime import datetime, timezone
    debut = datetime(2000, 1, 1, tzinfo=timezone.utc)
    for s in config["sources"]:
        for f in s["flux"]:
            n = len(lire_flux(s, f, debut))
            print(f"{'OK ' if n else 'ÉCHEC'} {n:3d}  {s['nom']:<20} {f['url']}")


def diagnostiquer(motif: str, articles: list, sujets: list, config: dict) -> None:
    """Explique où sont passés les articles qui contiennent un motif (sans rien publier)."""
    import copy
    import re

    import numpy as np

    rx = re.compile(motif, re.IGNORECASE)
    touches = {id(a) for a in articles if rx.search(f"{a.titre} {a.chapeau}")}
    seuil = config["general"]["seuil_theme"]
    large = copy.deepcopy(config)
    large["general"]["reserve_par_section"] = 10_000
    rangs = {}
    for sec_id, lst in selectionner(sujets, large).items():
        for i, s in enumerate(lst, 1):
            rangs[id(s)] = (sec_id, i, len(lst))
    nb = {sec["id"]: sec["nb"] for sec in config["sections"]}
    lignes = []
    for s in sujets:
        if not any(id(a) in touches for a in s.articles):
            continue
        moy = {c["id"]: float(np.mean([a.themes[c["id"]] for a in s.articles])) for c in config["categories"]}
        top = ", ".join(f"{c} {v:.2f}" for c, v in sorted(moy.items(), key=lambda kv: -kv[1])[:3])
        if id(s) in rangs:
            sec, r, total = rangs[id(s)]
            etat = f"section {sec}, rang {r}/{total} ({'AFFICHÉ' if r <= nb[sec] else 'hors des ' + str(nb[sec]) + ' premiers'})"
        elif max(moy.values()) < seuil:
            etat = f"ÉCARTÉ : aucun thème assez proche (meilleur {max(moy.values()):.2f} < seuil {seuil})"
        else:
            etat = "ÉCARTÉ : aucune section pour ce thème et cette portée"
        titre = max(s.articles, key=lambda a: a.langue == "fr").titre
        autres = "; ".join(a.titre[:50] for a in s.articles if a.titre != titre)[:200]
        lignes.append(f"{titre[:90]} | {len(s.sources)} médias | thèmes : {top} | {etat}"
                      + (f" | regroupé avec : {autres}" if autres else ""))
    print(f"\n=== Diagnostic « {motif} » : {len(touches)} articles, {len(lignes)} sujets ===")
    for l in lignes:
        print(l)
    if os.getenv("GITHUB_ACTIONS"):
        print(f"::notice::Diagnostic « {motif} » : {len(touches)} articles trouvés, {len(lignes)} sujets")
        for l in lignes[:8]:  # GitHub n'affiche qu'un nombre limité d'annotations
            print(f"::notice::{l}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=RACINE / "config.yaml", type=Path)
    p.add_argument("--sortie", default=RACINE / "public", type=Path)
    p.add_argument("--sans-ia", action="store_true")
    p.add_argument("--verifier-flux", action="store_true")
    p.add_argument("--diagnostic", metavar="MOTIF",
                   help="explique ce qui est arrivé aux articles contenant ce motif, sans publier")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = yaml.safe_load(args.config.read_text("utf-8"))
    if args.verifier_flux:
        return verifier_flux(config)
    g = config["general"]

    articles = collecter(config)
    if not articles:
        raise SystemExit("Aucun article récupéré : vérifiez les flux (--verifier-flux).")

    enc = Encodeur(config["modeles"]["embeddings"])
    classer_themes(articles, enc.encoder([a.texte_court() for a in articles]), config, enc)
    # Le chapeau aide à reconnaître un même événement décrit par des titres très différents
    textes = [a.titre if g.get("regrouper_sur") == "titre" else f"{a.titre}. {a.chapeau[:300]}"
              for a in articles]
    sujets = regrouper(articles, enc.encoder(textes), g["seuil_regroupement"])
    if args.diagnostic:
        return diagnostiquer(args.diagnostic, articles, sujets, config)
    par_theme = selectionner(sujets, config)
    retenus = [s for lst in par_theme.values() for s in lst]

    # Texte complet seulement pour les sujets retenus (beaucoup moins de requêtes)
    a_extraire = [a for s in retenus for a in s.articles]
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda a: extraire_texte(a, g["max_caracteres_article"]), a_extraire))

    resumeur = None if args.sans_ia else Resumeur(config)
    nb_par_section = {sec["id"]: sec["nb"] for sec in config["sections"]}
    for section, sujets in par_theme.items():
        log.info("%s : %d sujets", section, len(sujets))
        if resumeur:
            resumeur.resumer_section(sujets, g["max_articles_par_resume"], nb_par_section[section])
        else:
            for s in sujets:
                resume_sans_ia(s)

    ecartes = sum(s.ignorer for lst in par_theme.values() for s in lst)
    for sec in config["sections"]:
        par_theme[sec["id"]] = [s for s in par_theme[sec["id"]] if not (s.ignorer or s.non_resume)][: sec["nb"]]
    if ecartes:
        log.info("%d sujets écartés (aucune information exploitable)", ecartes)
        if os.getenv("GITHUB_ACTIONS"):
            print(f"::notice::{ecartes} sujets écartés par l'IA (aucune information exploitable)")

    if resumeur and os.getenv("GITHUB_ACTIONS"):
        print("::notice::Résumés : " + ", ".join(f"{k} {v}" if v else k for k, v in resumeur.bilan.items()))

    generer(par_theme, config, args.sortie, RACINE / "site", avis_jeton(os.getenv("GITHUB_REPOSITORY", "")))
    log.info("Site généré dans %s", args.sortie)


if __name__ == "__main__":
    main()

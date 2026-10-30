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


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=RACINE / "config.yaml", type=Path)
    p.add_argument("--sortie", default=RACINE / "public", type=Path)
    p.add_argument("--sans-ia", action="store_true")
    p.add_argument("--verifier-flux", action="store_true")
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
    sujets = regrouper(articles, enc.encoder([a.titre for a in articles]), g["seuil_regroupement"])
    par_theme = selectionner(sujets, config)
    retenus = [s for lst in par_theme.values() for s in lst]

    # Texte complet seulement pour les sujets retenus (beaucoup moins de requêtes)
    a_extraire = [a for s in retenus for a in s.articles]
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda a: extraire_texte(a, g["max_caracteres_article"]), a_extraire))

    resumeur = None if args.sans_ia else Resumeur(config)
    for section, sujets in par_theme.items():
        log.info("%s : %d sujets", section, len(sujets))
        if resumeur:
            resumeur.resumer_section(sujets, g["max_articles_par_resume"])
        else:
            for s in sujets:
                resume_sans_ia(s)

    ecartes = sum(s.ignorer for lst in par_theme.values() for s in lst)
    for sec in config["sections"]:
        par_theme[sec["id"]] = [s for s in par_theme[sec["id"]] if not s.ignorer][: sec["nb"]]
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

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
from deepseek import DeepSeek
from editorial import editorialiser
from regional import Regional
from jeton import avis_jeton
from resume import SEUIL_PEU_DE_MATIERE, Resumeur, resume_sans_ia
from site_web import generer

RACINE = Path(__file__).resolve().parent.parent
log = logging.getLogger("revue")


def verifier_flux(config: dict) -> None:
    """Teste chaque flux et, pour le premier article, la récupération du texte complet."""
    from datetime import datetime, timezone
    debut = datetime(2000, 1, 1, tzinfo=timezone.utc)
    lignes = []
    for s in config["sources"]:
        for f in s["flux"]:
            lus = lire_flux(s, f, debut)
            texte = ""
            if lus:
                extraire_texte(lus[0], config["general"]["max_caracteres_article"])
                texte = f", texte {len(lus[0].texte)} car." + (" (paywall)" if lus[0].paywall else "")
            ligne = f"{'OK' if lus else 'ÉCHEC'} {s['nom']} : {len(lus)} articles{texte}"
            print(ligne, f["url"])
            lignes.append(ligne)
    if os.getenv("GITHUB_ACTIONS"):
        # GitHub limite le nombre d'annotations : on regroupe tout en quelques lignes
        for i in range(0, len(lignes), 12):
            print("::notice::" + " | ".join(lignes[i:i + 12]))


def decouvrir_flux(urls: list[str]) -> None:
    """Cherche les flux RSS d'un site (balises <link> et chemins habituels) et les teste."""
    import re
    from datetime import datetime, timezone
    from urllib.parse import urljoin, urlparse

    import requests

    from collecte import UA

    debut = datetime(2000, 1, 1, tzinfo=timezone.utc)
    for url in urls:
        candidats = []
        try:
            page = requests.get(url, headers={"User-Agent": UA}, timeout=20).text
            for balise in re.findall(r"<link[^>]+>", page, re.I):
                if re.search(r"(rss|atom)\+xml", balise, re.I):
                    m = re.search(r'href=["\']([^"\']+)', balise)
                    if m:
                        candidats.append(urljoin(url, m.group(1)))
        except Exception as e:
            print(f"::warning::{url} inaccessible : {e}")
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        candidats += [url.rstrip("/") + "/feed/", url.rstrip("/") + "/rss", base + "/feed/", base + "/rss.xml", base + "/rss"]
        resultats = []
        for c in dict.fromkeys(candidats):
            lus = lire_flux({"nom": "test", "langue": "fr"}, {"url": c}, debut)
            if lus:
                resultats.append(f"{c} ({len(lus)} articles, 1er : « {lus[0].titre[:60]} »)")
        if not resultats:
            # Pas de flux : liens d'articles visibles sur la page, pour configurer une lecture de page
            try:
                liens = re.findall(r'href=["\'](/[^"\'#?]{25,})["\']', page)
                liens = [l for l in dict.fromkeys(liens) if l.count("-") >= 3][:6]
                resultats.append("AUCUN FLUX RSS. Liens d'articles : " + " ".join(liens))
            except Exception:
                resultats.append("AUCUN FLUX RSS TROUVÉ")
        message = f"{url} → " + "; ".join(resultats)
        print(message)
        if os.getenv("GITHUB_ACTIONS"):
            print("::notice::" + message[:900])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=RACINE / "config.yaml", type=Path)
    p.add_argument("--sortie", default=RACINE / "public", type=Path)
    p.add_argument("--sans-ia", action="store_true")
    p.add_argument("--verifier-flux", action="store_true")
    p.add_argument("--decouvrir-flux", nargs="+", metavar="URL",
                   help="cherche et teste les flux RSS de ces sites, sans publier")
    p.add_argument("--diagnostic", metavar="MOTIF",
                   help="explique ce qui est arrivé aux articles contenant ce motif, sans publier")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = yaml.safe_load(args.config.read_text("utf-8"))
    if args.verifier_flux:
        return verifier_flux(config)
    if args.decouvrir_flux:
        return decouvrir_flux(args.decouvrir_flux)
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
    par_theme = None
    cle_ds = os.getenv("DEEPSEEK_API_KEY")
    if g.get("editorial") == "deepseek" and cle_ds and not args.sans_ia:
        try:
            par_theme = editorialiser(sujets, config, DeepSeek(cle_ds, config["modeles"]["deepseek_modele"]))
            if os.getenv("GITHUB_ACTIONS"):
                print("::notice::Choix des sujets et des sections : DeepSeek")
        except Exception as e:
            log.warning("Choix éditorial par DeepSeek impossible (%s), méthode par similarité", e)
            if os.getenv("GITHUB_ACTIONS"):
                print("::warning::Choix éditorial DeepSeek impossible : " + " ".join(str(e).split())[:200])
    if args.diagnostic:
        return diagnostiquer(args.diagnostic, articles, sujets, config, par_theme)
    if par_theme is None:
        par_theme = selectionner(sujets, config)
        if "regional" in config:
            reg = Regional(config)
            sec = next(x for x in config["sections"] if x["id"] == reg.section)
            par_theme[reg.section] = reg.section_par_similarite(sujets, sec["nb"] + g.get("reserve_par_section", 0))
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

    # « Brève » : les médias n'ont fourni qu'un titre et quelques lignes (paywall, accès refusé)
    for s in {id(s): s for lst in par_theme.values() for s in lst}.values():
        matiere = sum(len(a.texte or a.chapeau) for a in s.articles)
        paragraphes = [p for p in s.resume.split("\n") if p.strip()]
        s.breve = bool(s.titre) and (matiere < SEUIL_PEU_DE_MATIERE or len(paragraphes) <= 1)

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

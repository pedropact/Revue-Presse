"""Collecte des articles à partir des flux RSS, et extraction du texte complet."""

import html
import logging
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import feedparser
import requests

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; RevuePresseQC/1.0; usage personnel)"
TIMEOUT = 20


@dataclass
class Article:
    source: str
    langue: str
    titre: str
    chapeau: str
    lien: str
    publie: datetime
    paywall: bool
    indice_theme: str | None = None
    texte: str = ""
    themes: dict = field(default_factory=dict)  # id -> score

    def texte_court(self) -> str:
        return f"{self.titre}. {self.chapeau[:400]}"


def nettoyer(texte: str) -> str:
    texte = re.sub(r"<[^>]+>", " ", texte or "")
    return re.sub(r"\s+", " ", html.unescape(texte)).strip()


def date_entree(entree) -> datetime | None:
    for cle in ("published_parsed", "updated_parsed"):
        t = entree.get(cle)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    for cle in ("published", "updated"):
        if entree.get(cle):
            try:
                return parsedate_to_datetime(entree[cle]).astimezone(timezone.utc)
            except (TypeError, ValueError):
                pass
    return None


def _meta(page: str, nom: str) -> str:
    """Contenu d'une balise <meta property|name="nom" content="..."> (ordre des attributs libre)."""
    for balise in re.findall(r"<meta[^>]+>", page, re.IGNORECASE):
        if re.search(rf'(property|name|itemprop)=["\']{re.escape(nom)}["\']', balise, re.IGNORECASE):
            m = re.search(r'content=["\']([^"\']*)', balise)
            if m:
                return html.unescape(m.group(1)).strip()
    return ""


def lire_article_page(source: dict, flux: dict, url: str) -> Article | None:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        log.debug("Article inaccessible %s : %s", url, e)
        return None
    page = r.text
    titre = _meta(page, "og:title")
    if not titre:
        return None
    date = _meta(page, "article:published_time") or _meta(page, "datePublished")
    if not date:
        m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', page)
        date = m.group(1) if m else ""
    try:
        publie = datetime.fromisoformat(date.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        publie = datetime.now(timezone.utc)  # date inconnue : on la considère comme récente
    return Article(
        source=source["nom"], langue=source.get("langue", "fr"), titre=nettoyer(titre),
        chapeau=nettoyer(_meta(page, "og:description")), lien=url, publie=publie,
        paywall=source.get("paywall", False), indice_theme=flux.get("theme"),
    )


def lire_page(source: dict, flux: dict, limite: datetime) -> list[Article]:
    """Pour un site sans flux RSS : repère les liens d'articles dans la page de la rubrique
    (y compris dans le code JavaScript de la page) et lit le titre et la date de chacun."""
    url = flux["url"]
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("Page inaccessible %s : %s", url, e)
        return []
    trouves = re.findall(flux["motif"], r.text.replace("\\/", "/"))
    liens = list(dict.fromkeys(urljoin(url, l) for l in trouves))[: flux.get("max", 20)]
    with ThreadPoolExecutor(max_workers=6) as ex:
        lus = [a for a in ex.map(lambda l: lire_article_page(source, flux, l), liens) if a]
    articles = [a for a in lus if a.publie >= limite]
    log.info("%-20s %3d articles  %s (page, %d liens)", source["nom"], len(articles), url, len(liens))
    return articles


def lire_flux(source: dict, flux: dict, limite: datetime) -> list[Article]:
    if flux.get("type") == "page":
        return lire_page(source, flux, limite)
    url = flux["url"]
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("Flux inaccessible %s : %s", url, e)
        return []
    articles = []
    for e in feedparser.parse(r.content).entries:
        publie = date_entree(e)
        if not publie or publie < limite or not e.get("link"):
            continue
        articles.append(Article(
            source=source["nom"],
            langue=source.get("langue", "fr"),
            titre=nettoyer(e.get("title", "")),
            chapeau=nettoyer(e.get("summary", "")),
            lien=e["link"],
            publie=publie,
            paywall=source.get("paywall", False),
            indice_theme=flux.get("theme"),
        ))
    log.info("%-20s %3d articles  %s", source["nom"], len(articles), url)
    return articles


def resumer_flux(taches: list, resultats: list) -> None:
    """Dans GitHub Actions, affiche le bilan des flux dans le résumé de l'exécution."""
    if not os.getenv("GITHUB_ACTIONS"):
        return
    par_source = Counter()
    vides = []
    for (source, flux), lot in zip(taches, resultats):
        par_source[source["nom"]] += len(lot)
        if not lot:
            vides.append(flux["url"])
    print("::notice::Articles par source : " + ", ".join(f"{s} {n}" for s, n in par_source.items()))
    if vides:
        print("::warning::Flux vides ou inaccessibles : " + " ".join(vides))


def collecter(config: dict) -> list[Article]:
    limite = datetime.now(timezone.utc) - timedelta(hours=config["general"]["fenetre_heures"])
    taches = [(s, f) for s in config["sources"] for f in s["flux"]]
    with ThreadPoolExecutor(max_workers=8) as ex:
        resultats = ex.map(lambda sf: lire_flux(*sf, limite), taches)
    # Un même article apparaît souvent dans plusieurs flux d'un même site
    resultats = list(resultats)
    resumer_flux(taches, resultats)
    vus, articles = {}, []
    for a in (a for lot in resultats for a in lot):
        cle = a.lien.split("?")[0]
        if cle in vus:
            vus[cle].indice_theme = vus[cle].indice_theme or a.indice_theme
            continue
        vus[cle] = a
        articles.append(a)
    log.info("Total : %d articles uniques", len(articles))
    return articles


# Indices d'une page d'erreur ou de blocage plutôt que d'un article
MARQUEURS_BLOCAGE = [
    "access denied", "accès refusé", "acces refuse", "forbidden", "captcha",
    "enable javascript", "activez javascript", "are you a robot", "êtes-vous un robot",
    "unusual traffic", "trafic inhabituel", "request blocked", "requête bloquée",
    "not authorized", "erreur d'autorisation", "page introuvable", "page not found",
]


def page_bloquee(texte: str) -> bool:
    """Un texte court contenant des mots typiques d'une page d'erreur n'est pas un article."""
    if not texte:
        return False
    debut = texte[:600].lower()
    return len(texte) < 1500 and any(m in debut for m in MARQUEURS_BLOCAGE)


def extraire_texte(article: Article, max_car: int) -> None:
    """Récupère le texte complet pour les sources sans paywall; sinon garde le chapeau RSS."""
    if article.paywall:
        article.texte = article.chapeau
        return
    try:
        import trafilatura
        r = requests.get(article.lien, headers={"User-Agent": UA}, timeout=TIMEOUT)
        texte = ""
        if r.status_code == 200:
            texte = trafilatura.extract(r.text, include_comments=False, include_tables=False) or ""
        if page_bloquee(texte):
            texte = ""
    except Exception as e:  # le texte complet est un bonus : on continue avec le chapeau
        log.debug("Extraction échouée %s : %s", article.lien, e)
        texte = ""
    article.texte = (texte or article.chapeau)[:max_car]

"""Collecte des articles à partir des flux RSS, et extraction du texte complet."""

import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

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


def lire_flux(source: dict, flux: dict, limite: datetime) -> list[Article]:
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


def collecter(config: dict) -> list[Article]:
    limite = datetime.now(timezone.utc) - timedelta(hours=config["general"]["fenetre_heures"])
    taches = [(s, f) for s in config["sources"] for f in s["flux"]]
    with ThreadPoolExecutor(max_workers=8) as ex:
        resultats = ex.map(lambda sf: lire_flux(*sf, limite), taches)
    # Un même article apparaît souvent dans plusieurs flux d'un même site
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


def extraire_texte(article: Article, max_car: int) -> None:
    """Récupère le texte complet pour les sources sans paywall; sinon garde le chapeau RSS."""
    if article.paywall:
        article.texte = article.chapeau
        return
    try:
        import trafilatura
        r = requests.get(article.lien, headers={"User-Agent": UA}, timeout=TIMEOUT)
        texte = trafilatura.extract(r.text, include_comments=False, include_tables=False) or ""
    except Exception as e:  # le texte complet est un bonus : on continue avec le chapeau
        log.debug("Extraction échouée %s : %s", article.lien, e)
        texte = ""
    article.texte = (texte or article.chapeau)[:max_car]

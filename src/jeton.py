"""Surveille la date d'expiration du jeton GitHub utilisé par le déclencheur Cloudflare."""

import logging
import os
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

SEUIL_JOURS = 30


def lire_expiration(valeur: str) -> datetime | None:
    # GitHub renvoie par ex. « 2028-10-30 12:00:00 UTC » ou « 2028-10-30 12:00:00 -0400 »
    for fmt in ("%Y-%m-%d %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            d = datetime.strptime(valeur.strip(), fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def avis_jeton(depot: str) -> str | None:
    """Renvoie un message d'avertissement si le jeton expire bientôt ou ne fonctionne plus."""
    jeton = os.getenv("GH_DECLENCHEUR_TOKEN")
    if not jeton or not depot:
        return None
    try:
        r = requests.get(f"https://api.github.com/repos/{depot}", timeout=20,
                         headers={"Authorization": f"Bearer {jeton}",
                                  "Accept": "application/vnd.github+json"})
    except requests.RequestException as e:
        log.warning("Vérification du jeton impossible : %s", e)
        return None
    if r.status_code == 401:
        return ("Le jeton GitHub du déclenchement automatique a expiré ou n'est plus valide : "
                "la revue ne sera plus mise à jour chaque matin. Créez un nouveau jeton et "
                "mettez à jour le secret GH_DECLENCHEUR_TOKEN.")
    expiration = lire_expiration(r.headers.get("github-authentication-token-expiration", ""))
    if expiration is None:
        return None
    jours = (expiration - datetime.now(timezone.utc)).days
    if os.getenv("GITHUB_ACTIONS"):
        print(f"::notice::Jeton du déclencheur : expire le {expiration:%Y-%m-%d} (dans {jours} jours)")
    if jours > SEUIL_JOURS:
        return None
    return (f"Le jeton GitHub du déclenchement automatique expire le {expiration:%d/%m/%Y} "
            f"(dans {max(jours, 0)} jours). Pensez à le renouveler et à mettre à jour le secret "
            "GH_DECLENCHEUR_TOKEN, sinon la revue ne sera plus mise à jour chaque matin.")

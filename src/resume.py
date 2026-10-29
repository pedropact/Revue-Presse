"""Résumé (en français) de chaque sujet avec un petit modèle local exécuté sur CPU."""

import json
import logging
import os

from analyse import Sujet

log = logging.getLogger(__name__)

SYSTEME = (
    "Tu es un journaliste québécois qui rédige une revue de presse en français. "
    "On te donne un ou plusieurs articles (en français ou en anglais) portant sur le même sujet. "
    "Rédige en français, avec tes propres mots, un titre neutre et un texte de 2 à 3 paragraphes "
    "qui fusionne l'information de tous les articles : d'abord les faits essentiels (qui, quoi, "
    "quand, où), puis le contexte, les chiffres et les réactions, et enfin les conséquences "
    "ou les prochaines étapes. Ne recopie pas les phrases des articles. "
    "N'invente aucun fait, aucun chiffre ni aucune citation. "
    "Sépare les paragraphes par une ligne vide. "
    'Réponds uniquement en JSON : {"titre": "...", "resume": "..."}'
)


class Resumeur:
    def __init__(self, config: dict):
        from llama_cpp import Llama
        m = config["modeles"]
        self.llm = Llama.from_pretrained(
            repo_id=m["llm_repo"],
            filename=m["llm_fichier"],
            n_ctx=m["llm_contexte"],
            n_threads=os.cpu_count(),
            verbose=False,
        )

    def resumer(self, sujet: Sujet, max_articles: int) -> None:
        # Priorité aux articles avec texte complet, puis aux plus récents
        choisis = sorted(sujet.articles, key=lambda a: (len(a.texte), a.publie), reverse=True)[:max_articles]
        blocs = [
            f"### Article {i} — {a.source} ({'anglais' if a.langue == 'en' else 'français'})\n"
            f"Titre : {a.titre}\n{a.texte or a.chapeau}"
            for i, a in enumerate(choisis, 1)
        ]
        try:
            r = self.llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": SYSTEME},
                    {"role": "user", "content": "\n\n".join(blocs)},
                ],
                temperature=0.2,
                max_tokens=900,
                response_format={"type": "json_object"},
            )
            data = json.loads(r["choices"][0]["message"]["content"])
            sujet.titre = data["titre"].strip()
            sujet.resume = data["resume"].strip()
        except Exception as e:
            log.warning("Résumé impossible (%s), utilisation du texte RSS", e)
            resume_sans_ia(sujet)


def resume_sans_ia(sujet: Sujet) -> None:
    """Repli : titre et chapeau d'un article francophone si possible."""
    fr = [a for a in sujet.articles if a.langue == "fr"] or sujet.articles
    a = max(fr, key=lambda a: len(a.chapeau))
    sujet.titre, sujet.resume = a.titre, a.chapeau[:600]

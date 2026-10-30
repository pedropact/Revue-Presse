// Déclencheur quotidien de la revue de presse (Cloudflare Worker, tâche planifiée).
// Cloudflare ne connaît que l'heure UTC : deux horaires sont planifiés (heure d'été et
// heure normale) et seul celui qui tombe à 6 h à Montréal lance la revue.

export default {
  async scheduled(event, env, ctx) {
    const heure = new Intl.DateTimeFormat("en-US", {
      timeZone: "America/Toronto",
      hour: "2-digit",
      hourCycle: "h23",
    }).format(new Date(event.scheduledTime));
    if (heure !== "06") return;
    ctx.waitUntil(declencher(env));
  },
};

async function declencher(env) {
  const url = `https://api.github.com/repos/${env.DEPOT}/actions/workflows/revue-quotidienne.yml/dispatches`;
  const r = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "revue-presse-declencheur",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  if (!r.ok) throw new Error(`GitHub a refusé le déclenchement : ${r.status} ${await r.text()}`);
}

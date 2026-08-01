/**
 * Cloudflare Worker: loest den Padel-Watch-Workflow alle 10 Minuten aus.
 *
 * Hintergrund: GitHubs eigener Scheduler hat fuer dieses Repo nie ausgeloest
 * (HANDOFF.md §9), und cron-job.org lief beim POST auf /dispatches in einen
 * Timeout - GitHub antwortet mit "204 No Content", der Client wartete auf einen
 * Antwortkoerper. Ein Worker liest die Antwort selbst und kennt das Problem nicht.
 *
 * Einrichtung (dash.cloudflare.com, kostenloser Plan):
 *   1. Workers & Pages -> Create -> Start with Hello World -> Deploy
 *   2. Diesen Code in den Editor kopieren -> Deploy
 *   3. Settings -> Variables and Secrets -> Add: GH_TOKEN = <Fine-grained PAT>
 *      (Typ "Secret", nicht "Text" - dann ist er auch fuer dich nicht mehr lesbar)
 *   4. Settings -> Trigger Events -> Cron Triggers -> Add: *\/10 * * * *
 *
 * Testen ohne auf den Cron zu warten: Worker-URL im Browser aufrufen -
 * fetch() loest denselben Aufruf aus und zeigt das Ergebnis als Text.
 */

const REPO = "dolcevitalij/padel-watch";
const WORKFLOW = "padel-watch.yml";

async function triggerWorkflow(env) {
  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Accept": "application/vnd.github+json",
      "Authorization": `Bearer ${env.GH_TOKEN}`,
      "X-GitHub-Api-Version": "2022-11-28",
      // GitHub verlangt einen User-Agent, sonst 403
      "User-Agent": "padel-watch-cron",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main" }),
  });

  // 204 = ausgeloest. Alles andere ist ein Fehler und landet im Worker-Log
  // (Cloudflare-Dashboard -> Logs), damit stille Ausfaelle auffallen.
  const body = res.status === 204 ? "" : await res.text();
  const line = `dispatch ${REPO}/${WORKFLOW}: HTTP ${res.status}${body ? " " + body : ""}`;
  if (res.status !== 204) console.error(line);
  else console.log(line);
  return { ok: res.status === 204, status: res.status, line };
}

export default {
  // Vom Cron Trigger aufgerufen
  async scheduled(event, env, ctx) {
    ctx.waitUntil(triggerWorkflow(env));
  },

  // Manueller Aufruf ueber die Worker-URL, zum Testen
  async fetch(request, env) {
    const r = await triggerWorkflow(env);
    return new Response(r.line + "\n", { status: r.ok ? 200 : 502 });
  },
};

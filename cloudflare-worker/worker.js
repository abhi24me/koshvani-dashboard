/**
 * Tiny proxy so the public dashboard can trigger a live GitHub Actions run
 * without exposing a GitHub token to the browser.
 *
 * The dashboard's "Refresh" button POSTs to this Worker; the Worker (which
 * holds GH_TOKEN as a Cloudflare secret, never sent to the client) calls
 * GitHub's workflow_dispatch API on the dashboard's behalf.
 *
 * Required environment:
 *   GH_TOKEN    (secret) - fine-grained PAT, "Actions: write" on this repo only
 *   GH_OWNER    (var)    - your GitHub username/org
 *   GH_REPO     (var)    - the repo name
 *   GH_WORKFLOW (var)    - workflow file name, e.g. "scrape.yml"
 *   GH_BRANCH   (var)    - branch to run against, e.g. "main"
 *   ALLOWED_ORIGIN (var) - your GitHub Pages origin, e.g.
 *                          "https://<user>.github.io" (use "*" while testing)
 */
export default {
  async fetch(request, env) {
    const cors = {
      "Access-Control-Allow-Origin": env.ALLOWED_ORIGIN || "*",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: cors });
    }

    const url = new URL(request.url);
    if (url.pathname !== "/trigger" || request.method !== "POST") {
      return new Response("Not found", { status: 404, headers: cors });
    }

    const ghRes = await fetch(
      `https://api.github.com/repos/${env.GH_OWNER}/${env.GH_REPO}/actions/workflows/${env.GH_WORKFLOW}/dispatches`,
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.GH_TOKEN}`,
          "Accept": "application/vnd.github+json",
          "User-Agent": "koshvani-dashboard-worker",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ ref: env.GH_BRANCH || "main" }),
      }
    );

    const ok = ghRes.status === 204;
    return new Response(JSON.stringify({ ok, status: ghRes.status }), {
      status: ok ? 200 : 502,
      headers: { "Content-Type": "application/json", ...cors },
    });
  },
};

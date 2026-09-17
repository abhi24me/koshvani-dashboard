/**
 * Tiny proxy so the public dashboard can request a live refresh without
 * exposing a GitHub token to the browser.
 *
 * There's no GitHub Actions runner in this setup (koshvani.up.nic.in blocks
 * every cloud/datacenter IP, and a phone can't reliably host an always-on
 * Actions runner listener either) - instead, a Python script running on a
 * phone (Termux) polls the repo for a pending request and runs the scraper
 * itself when it sees one. This Worker's only job is to record "someone
 * clicked Refresh at time X" by writing docs/data/refresh_request.json in
 * the repo via GitHub's Contents API - the polling script compares that
 * timestamp against docs/data/index.json's own generated_at to know
 * whether a scrape is still owed.
 *
 * Required environment:
 *   GH_TOKEN    (secret) - fine-grained PAT, "Contents: read and write" on
 *                          this repo only
 *   GH_OWNER    (var)    - your GitHub username/org
 *   GH_REPO     (var)    - the repo name
 *   GH_BRANCH   (var)    - branch to write to, e.g. "main"
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

    const branch = env.GH_BRANCH || "main";
    const path = "docs/data/refresh_request.json";
    const apiUrl = `https://api.github.com/repos/${env.GH_OWNER}/${env.GH_REPO}/contents/${path}`;
    const ghHeaders = {
      "Authorization": `Bearer ${env.GH_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "User-Agent": "koshvani-dashboard-worker",
      "X-GitHub-Api-Version": "2022-11-28",
    };

    // Contents API requires the current file's sha to update it.
    let sha;
    try {
      const getRes = await fetch(`${apiUrl}?ref=${branch}`, { headers: ghHeaders });
      if (getRes.status === 200) {
        sha = (await getRes.json()).sha;
      } else if (getRes.status !== 404) {
        return new Response(JSON.stringify({ ok: false, step: "get", status: getRes.status }), {
          status: 502,
          headers: { "Content-Type": "application/json", ...cors },
        });
      }
    } catch (e) {
      return new Response(JSON.stringify({ ok: false, step: "get", error: String(e) }), {
        status: 502,
        headers: { "Content-Type": "application/json", ...cors },
      });
    }

    const body = JSON.stringify({ requested_at: new Date().toISOString() }, null, 2) + "\n";
    const putRes = await fetch(apiUrl, {
      method: "PUT",
      headers: { ...ghHeaders, "Content-Type": "application/json" },
      body: JSON.stringify({
        message: "Request live refresh",
        content: btoa(body),
        sha,
        branch,
      }),
    });

    const ok = putRes.status === 200 || putRes.status === 201;
    return new Response(JSON.stringify({ ok, status: putRes.status }), {
      status: ok ? 200 : 502,
      headers: { "Content-Type": "application/json", ...cors },
    });
  },
};

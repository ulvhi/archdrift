#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "anthropic>=0.69",
#   "rich>=13.7",
# ]
# ///
"""archdrift web — pick a namespace, a microservice and an HLD; the gate runs and the report opens.

A thin local shell around archdrift.py: repos are pulled straight from GitLab with a token
(no local checkout needed), the audit pipeline is the exact one CI runs, and the report is
the same self-contained HTML the CLI emits. Nothing is hosted — this binds to localhost.
"""

from __future__ import annotations

import argparse, importlib.util, json, os, sys, tarfile, tempfile
import urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _engine():
    # TODO: the web UI is a shell around the same single-file engine CI runs — import it by path,
    # so a browser run can never disagree with the merge gate.
    spec = importlib.util.spec_from_file_location("archdrift", Path(__file__).with_name("archdrift.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["archdrift"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── GitLab access ────────────────────────────────────────────────────────────


def gl_json(base: str, token: str, path: str, **params) -> list | dict:
    # TODO: one authenticated GitLab API call, JSON out — the token never reaches the browser.
    url = f"{base}/api/v4/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    with urllib.request.urlopen(
            urllib.request.Request(url, headers={"PRIVATE-TOKEN": token}), timeout=30) as resp:
        return json.loads(resp.read())


def gl_pages(base: str, token: str, path: str, **params) -> list:
    # TODO: flatten pagination — enough pages for a corporate group tree, no infinite walk.
    out: list = []
    for page in range(1, 11):
        batch = gl_json(base, token, path, per_page=100, page=page, **params)
        out += batch
        if len(batch) < 100:
            break
    return out


def fetch_repo(base: str, token: str, project_id: int, ref: str) -> Path:
    # TODO: a repo snapshot without git — the archive endpoint streamed into a temp dir; the tarball
    # wraps everything in one "<project>-<sha>" folder, which becomes the audit root.
    url = (f"{base}/api/v4/projects/{project_id}/repository/archive.tar.gz?"
           + urllib.parse.urlencode({"sha": ref}))
    dest = Path(tempfile.mkdtemp(prefix="archdrift-"))
    with urllib.request.urlopen(
            urllib.request.Request(url, headers={"PRIVATE-TOKEN": token}), timeout=180) as resp, \
            tarfile.open(fileobj=resp, mode="r|gz") as tar:
        tar.extractall(dest, filter="data")
    inner = [p for p in dest.iterdir() if p.is_dir()]
    return inner[0] if len(inner) == 1 else dest


def _gitlab_file_raw(base: str, token: str, spec: str) -> bytes | None:
    # TODO: the web /-/raw/ endpoint wants a browser session, not a read_api token — it 302s to the
    # sign-in page. Rewrite the pasted link onto the API raw endpoint, which the token CAN open.
    # A branch name may itself contain slashes, so try every ref/path split until one answers.
    project, _, rest = urllib.parse.urlsplit(spec).path.lstrip("/").partition("/-/raw/")
    if not rest:
        return None
    proj = urllib.parse.quote(project, safe="")
    parts = rest.split("/")
    for i in range(1, len(parts)):
        ref = urllib.parse.quote("/".join(parts[:i]), safe="")
        file_path = urllib.parse.quote("/".join(parts[i:]), safe="")
        url = f"{base}/api/v4/projects/{proj}/repository/files/{file_path}/raw?ref={ref}"
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers={"PRIVATE-TOKEN": token}), timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as err:
            if err.code == 404:
                continue                       # wrong ref/path split — try the next candidate
            raise
    return None


def fetch_diagram(spec: str, base: str, token: str) -> Path:
    # TODO: the HLD source is a local path or a URL. Devs paste the viewer link they're looking at,
    # not the raw one — /-/blob/ is normalised to /-/raw/, and a same-host GitLab link is fetched
    # through the API raw endpoint so a plain read_api token opens private repos.
    if spec.startswith(("http://", "https://")):
        spec = spec.replace("/-/blob/", "/-/raw/", 1).split("?", 1)[0]
        same_host = urllib.parse.urlsplit(spec).netloc == urllib.parse.urlsplit(base).netloc
        if same_host and token and (data := _gitlab_file_raw(base, token, spec)) is not None:
            body = data
        else:
            req = urllib.request.Request(
                spec, headers={"PRIVATE-TOKEN": token} if token and same_host else {})
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read()
        if body.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
            raise ValueError("the URL returned a web page, not a .drawio file — for a private repo "
                             "make sure the link points at the file and the token can read it")
        out = Path(tempfile.mkstemp(prefix="archdrift-", suffix=".drawio")[1])
        out.write_bytes(body)
        return out
    path = Path(spec).expanduser()
    if not path.is_file():
        raise ValueError(f"diagram not found: {path}")
    return path


def anthropic_models() -> list[dict]:
    # TODO: the model dropdown is whatever the account can actually run — served live from the
    # Models API rather than hardcoded, so new releases appear without touching this file.
    import anthropic
    return [{"id": m.id, "name": m.display_name}
            for m in anthropic.Anthropic().models.list()]   # auto-paginates, newest first


# ── the page ─────────────────────────────────────────────────────────────────

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>archdrift</title><style>
:root{color-scheme:light;--page:#f9f9f7;--card:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;
--muted:#898781;--hairline:#e1e0d9;--ring:rgba(11,11,11,.10);--ok:#0ca30c;--fail:#d03b3b;--accent:#2a78d6}
@media (prefers-color-scheme:dark){:root{color-scheme:dark;--page:#0d0d0d;--card:#1a1a19;
--ink:#fff;--ink2:#c3c2b7;--hairline:#2c2c2a;--ring:rgba(255,255,255,.10);--accent:#3987e5}}
*{box-sizing:border-box;margin:0}
body{background:var(--page);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;padding:32px 16px}
main{max-width:1080px;margin:auto}
.card{background:var(--card);border:1px solid var(--ring);border-radius:12px;padding:26px 30px;margin-bottom:20px}
h1{font-size:20px} h1 small{color:var(--muted);font-weight:400;font-size:13px;margin-left:10px}
form{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin-top:18px}
label{display:flex;flex-direction:column;gap:5px;font-size:12px;font-weight:600;color:var(--ink2)}
input,select{font:inherit;color:var(--ink);background:var(--page);border:1px solid var(--hairline);border-radius:8px;padding:8px 10px}
input:focus,select:focus{outline:2px solid var(--accent);outline-offset:0;border-color:transparent}
.row{grid-column:1/-1;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
button{font:inherit;font-weight:600;color:#fff;background:var(--accent);border:0;border-radius:8px;padding:9px 22px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
/* share row: appears with the report; actions are placeholders until wired up */
#share{display:none;justify-content:flex-end;align-items:center;gap:10px;margin:0 0 12px}
#share span.lbl{color:var(--muted);font-size:12px;margin-right:2px}
.ghost{color:var(--ink2);background:transparent;border:1px solid var(--hairline);font-weight:500;
padding:7px 14px;display:inline-flex;align-items:center;gap:8px}
.ghost:disabled{opacity:.65;cursor:not-allowed}
.soon{font-size:9px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
border:1px solid var(--hairline);border-radius:999px;padding:1px 7px}
/* auto-run: the button is real, the scheduling isn't yet — the panel only previews it */
.head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
#autopanel{display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap;margin-top:16px;
padding:14px 16px;border:1px dashed var(--hairline);border-radius:10px}
#autopanel[hidden]{display:none}
#autopanel .note{color:var(--muted);font-size:12px;flex-basis:100%}
#autopanel .lblx{font-size:12px;font-weight:600;color:var(--ink2);display:block;margin-bottom:6px}
#ar-days{display:grid;grid-template-columns:repeat(7,38px);gap:6px;width:max-content}
.day{width:38px;height:32px;border:1px solid var(--hairline);border-radius:8px;background:var(--page);
color:var(--ink);font:inherit;font-size:13px;font-weight:500;padding:0;cursor:pointer}
.day.on{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:700}
#status{color:var(--muted);font-size:13px} #status.err{color:var(--fail)}
.spin{display:inline-block;width:14px;height:14px;border:2px solid var(--hairline);border-top-color:var(--accent);
border-radius:50%;animation:r .8s linear infinite;vertical-align:-2px;margin-right:6px}
@keyframes r{to{transform:rotate(360deg)}}
iframe{width:100%;height:1200px;border:0;border-radius:12px;background:var(--card);display:none}
/* hero: just the wordmark */
.hero{padding:40px 16px 26px;text-align:center}
.hero h1{font-size:clamp(48px,8vw,92px);font-weight:900;letter-spacing:-.045em;line-height:1.02;
margin:0;color:var(--ink)}
.hero h1 em{font-style:normal;color:var(--accent)}
@media (prefers-color-scheme:dark){
.hero h1 em{text-shadow:0 0 14px color-mix(in srgb,var(--accent) 55%,transparent),
0 0 36px color-mix(in srgb,var(--accent) 30%,transparent)}}
</style></head><body>
<header class="hero"><h1>arch<em>drift</em></h1></header>
<main>
<div class="card"><div class="head">
<h1>archdrift <small>architecture match gate — pick a service, run the audit</small></h1>
<button type="button" id="autorun" class="ghost">&#128197; auto-run <span class="soon">soon</span></button>
</div>
<div id="autopanel" hidden>
<div style="flex-basis:100%">
<span class="lblx">runs every month on — pick one or more days</span>
<div id="ar-days"></div>
</div>
<label>time <input type="time" id="ar-time" value="07:00"></label>
<button type="button" class="ghost" disabled title="coming soon — not wired up yet">
schedule <span class="soon">soon</span></button>
<span class="note">Scheduled audits are coming soon: the gate will run itself on the selected
days of every month and deliver the report.</span>
</div>
<form id="f">
<label>namespace / group
<select id="group" required><option value="">loading groups…</option></select></label>
<label>microservice
<select id="proj" required disabled><option value="">pick a group first</option></select></label>
<label>branch <select id="ref" required disabled><option value="">pick a service first</option></select></label>
<label style="grid-column:span 2">HLD diagram — local path or URL
<input id="drawio" placeholder="~/Desktop/service-HLD.drawio  ·  https://…/raw/…/hld.drawio" required></label>
<label>consul profile <select id="profile" data-def="__PROFILE__">
<option value="dev">application-dev</option>
<option value="load">application-load</option>
<option value="preprod" selected>application-preprod</option>
<option value="prod">application-prod</option>
<option value="qa">application-qa</option>
</select></label>
<label>model <select id="model">
<option value="claude-haiku-4-5">Claude Haiku 4.5</option>
<option value="claude-sonnet-5" selected>Claude Sonnet 5</option></select></label>
<div class="row"><button id="go">run</button><span id="status">__SETUP__</span></div>
</form></div>
<div id="share">
<span class="lbl">share the report</span>
<button type="button" class="ghost" disabled title="coming soon — not wired up yet">
&#9993;&#65039; send e-mail <span class="soon">soon</span></button>
<button type="button" class="ghost" disabled title="coming soon — not wired up yet">
post to a Teams channel <span class="soon">soon</span></button>
</div>
<iframe id="report" title="archdrift report"></iframe>
<script>
const $=id=>document.getElementById(id), status=(t,err)=>{ $("status").innerHTML=t; $("status").className=err?"err":""; };
$("autorun").onclick=()=>{ $("autopanel").hidden=!$("autopanel").hidden; };
{const d=$("ar-days"); for(let i=1;i<=31;i++){ const b=document.createElement("button");
  b.type="button"; b.className="day"+([1,15].includes(i)?" on":""); b.textContent=i;
  b.onclick=()=>b.classList.toggle("on"); d.appendChild(b); }}
const api=async(p)=>{const r=await fetch(p); const j=await r.json(); if(!r.ok) throw new Error(j.error||r.statusText); return j;};
{const d=$("profile").dataset.def; if([...$("profile").options].some(o=>o.value===d)) $("profile").value=d;}
(async()=>{try{  // live model list from the Models API; the two hardcoded options stay as fallback
  const ms=await api("/api/models");
  if(ms.length) $("model").innerHTML=ms.map(m=>
    `<option value="${m.id}"${m.id==="claude-sonnet-5"?" selected":""}>${m.name}</option>`).join("");
}catch(e){}})();
(async()=>{try{
  const groups=await api("/api/groups");
  $("group").innerHTML='<option value="">— choose —</option>'+groups.map(g=>`<option value="${g.id}">${g.full_path}</option>`).join("");
}catch(e){status("groups: "+e.message,1)}})();
$("group").onchange=async()=>{
  $("proj").disabled=true; $("proj").innerHTML="<option>loading…</option>";
  try{
    const ps=await api("/api/projects?group="+$("group").value);
    $("proj").innerHTML=ps.map(p=>`<option value="${p.id}" data-ref="${p.default_branch||""}">${p.path}</option>`).join("");
    $("proj").disabled=false;
    $("proj").onchange=async()=>{
      const o=$("proj").selectedOptions[0]; if(!o) return;
      $("ref").disabled=true; $("ref").innerHTML="<option>loading…</option>";
      try{
        const bs=await api("/api/branches?project="+o.value);
        const def=o.dataset.ref||bs.find(b=>b.default)?.name||bs[0]?.name||"";  // repo-nun öz default branch-i
        $("ref").innerHTML=bs.map(b=>`<option value="${b.name}"${b.name===def?" selected":""}>${b.name}</option>`).join("");
      }catch(e){status("branches: "+e.message,1); $("ref").innerHTML='<option value="">—</option>'; return;}
      $("ref").disabled=false;
    };
    $("proj").onchange();
  }catch(e){status("projects: "+e.message,1)}
};
$("f").onsubmit=async ev=>{
  ev.preventDefault(); $("go").disabled=true; $("report").style.display="none";
  $("share").style.display="none";
  status('<span class="spin"></span>pulling repo → parsing HLD → auditing… (this takes a minute)');
  try{
    const r=await fetch("/api/run",{method:"POST",headers:{"content-type":"application/json"},
      body:JSON.stringify({project_id:+$("proj").value, ref:$("ref").value, service:$("proj").selectedOptions[0].text,
        drawio:$("drawio").value, profile:$("profile").value,
        project:($("group").selectedOptions[0]?.text||"").split("/").pop(),  // consul {project} = git group

        model:$("model").value})});
    const j=await r.json(); if(!r.ok) throw new Error(j.error||r.statusText);
    status(`done — ${j.total}% match`, !j.passed);
    $("report").srcdoc=j.html; $("report").style.display="block";
    $("share").style.display="flex";
  }catch(e){status(e.message,1)}
  $("go").disabled=false;
};
</script></main></body></html>"""


# ── server ───────────────────────────────────────────────────────────────────


def make_handler(cfg, engine):
    # TODO: three routes, all local — the page, the two GitLab lookups, and the run itself.

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            # TODO: one response path — status, content type, length, bytes.
            self.send_response(code)
            self.send_header("content-type", f"{ctype}; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict | list, code: int = 200) -> None:
            self._send(code, json.dumps(payload).encode())

        def do_GET(self) -> None:  # noqa: N802 — http.server API
            # TODO: page + the two token-guarded lookups (token stays server-side).
            route, _, query = self.path.partition("?")
            try:
                match route:
                    case "/":
                        setup = "  ·  ".join(filter(None, (
                            "" if cfg.token else "⚠ export ARCHDRIFT_GITLAB_TOKEN to browse repos",
                            "" if os.environ.get("ANTHROPIC_API_KEY")
                            else "⚠ export ANTHROPIC_API_KEY, then restart — audits will fail without it")))
                        page = (PAGE.replace("__PROFILE__", cfg.profile)
                                    .replace("__SETUP__", setup))
                        self._send(200, page.encode(), "text/html")
                    case "/api/models":
                        self._json(anthropic_models())
                    case "/api/branches":
                        pid = urllib.parse.parse_qs(query)["project"][0]
                        branches = gl_pages(cfg.gitlab, cfg.token,
                                            f"projects/{pid}/repository/branches")
                        self._json([{"name": br["name"], "default": br.get("default", False)}
                                    for br in branches])
                    case "/api/groups":
                        groups = gl_pages(cfg.gitlab, cfg.token, "groups", order_by="path")
                        self._json(sorted(
                            ({"id": g["id"], "full_path": g["full_path"]} for g in groups),
                            key=lambda g: g["full_path"]))
                    case "/api/projects":
                        gid = urllib.parse.parse_qs(query)["group"][0]
                        projects = gl_pages(cfg.gitlab, cfg.token, f"groups/{gid}/projects",
                                            include_subgroups="true", archived="false",
                                            order_by="path", sort="asc")
                        self._json([{"id": p["id"], "path": p["path"],
                                     "default_branch": p.get("default_branch")}
                                    for p in projects])
                    case _:
                        self._json({"error": "not found"}, 404)
            except Exception as err:  # surfaced in the UI status line
                self._json({"error": str(err)}, 502)

        def do_POST(self) -> None:  # noqa: N802 — http.server API
            # TODO: the run — snapshot the repo, resolve the HLD, execute the SAME gate CI runs,
            # hand back the self-contained report.
            if self.path != "/api/run":
                return self._json({"error": "not found"}, 404)
            try:
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                root = fetch_repo(cfg.gitlab, cfg.token, body["project_id"], body["ref"])
                diagram = fetch_diagram(body["drawio"], cfg.gitlab, cfg.token)
                service, dgm, audit, total, per_dim, penalty, notices = engine.run_gate(
                    root, diagram,
                    service=body.get("service") or None,
                    project=body.get("project") or cfg.project,
                    profile=body.get("profile") or cfg.profile,
                    consul_url=cfg.consul_url,
                    model=body.get("model") or "claude-sonnet-5",
                    redact_spec=os.environ.get("ARCHDRIFT_REDACT", ""),
                    no_consul=not cfg.consul_url)
                report = Path(tempfile.mkstemp(prefix="archdrift-", suffix=".html")[1])
                engine.render_html(report, service, dgm, audit, total, per_dim, penalty,
                                   cfg.threshold, body.get("profile") or cfg.profile, notices)
                self._json({"total": total, "passed": total >= cfg.threshold,
                            "html": report.read_text(encoding="utf-8")})
            except (ValueError, KeyError, urllib.error.URLError) as err:
                self._json({"error": str(err)}, 400)
            except SystemExit as err:
                # the engine bails out with sys.exit() on model/API failures — surface the message
                # instead of letting it kill the handler thread mid-response ("Failed to fetch")
                self._json({"error": str(err) or "audit aborted"}, 500)
            except Exception as err:
                self._json({"error": f"{type(err).__name__}: {err}"}, 500)

    return Handler


def main() -> int:
    # TODO: wire config from env/flags, load the engine once, serve on localhost only.
    env = lambda k, d="": os.environ.get(f"ARCHDRIFT_{k}", d)  # noqa: E731 — tiny local alias
    cli = argparse.ArgumentParser(description="archdrift web UI — localhost shell around archdrift.py")
    cli.add_argument("--gitlab", default=env("GITLAB"),
                     help="GitLab base url, e.g. https://gitlab.example.com [env: ARCHDRIFT_GITLAB]")
    cli.add_argument("--token", default=env("GITLAB_TOKEN") or os.environ.get("GITLAB_TOKEN", ""),
                     help="read_api token [env: ARCHDRIFT_GITLAB_TOKEN]")
    cli.add_argument("--consul-url", default=env("CONSUL_URL") or None)
    cli.add_argument("--project", default=env("PROJECT"), help="consul KV project segment")
    cli.add_argument("--profile", default=env("PROFILE", "preprod"))
    cli.add_argument("--threshold", type=int, default=int(env("THRESHOLD", "70")))
    cli.add_argument("--port", type=int, default=int(env("PORT", "8787")))
    cfg = cli.parse_args()
    if not cfg.gitlab:
        cli.error("GitLab url required — pass --gitlab or export ARCHDRIFT_GITLAB")
    cfg.gitlab = cfg.gitlab.rstrip("/")

    engine = _engine()
    server = ThreadingHTTPServer(("127.0.0.1", cfg.port), make_handler(cfg, engine))
    print(f"archdrift web → http://localhost:{cfg.port}   (gitlab: {cfg.gitlab})")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠ ANTHROPIC_API_KEY is not set — audits will fail until you export it and restart")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

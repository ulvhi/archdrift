# 🧭 archdrift

Checks that a microservice actually matches its draw.io HLD — components, flows,
bindings — and gives a match %. Runs in the browser: pick a repo, point at the
diagram, hit run.

## Run it

You need three things: an [Anthropic API key](https://platform.claude.com), your GitLab url,
and a GitLab token with **read_api** scope (GitLab → Preferences → Access Tokens).

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export ARCHDRIFT_GITLAB="https://gitlab.example.com"
export ARCHDRIFT_GITLAB_TOKEN="glpat-..."

uv run webapp.py
```

Open **http://localhost:8787** and fill the form:

| Field | What to put |
|---|---|
| namespace / group | pick your team's GitLab group |
| microservice | pick the service — branch fills in automatically |
| HLD diagram | local path (`~/Desktop/my-HLD.drawio`) or a raw GitLab URL |
| consul profile / project | only matter if Consul is configured (below) |

Hit **run audit** — takes about a minute, the report opens on the same page.

## Optional: check live Consul config too

Without this the audit still runs; it just skips runtime-config validation.

```bash
export ARCHDRIFT_CONSUL_URL="https://consul.example.com/v1/kv/config/{project}/{service}/application-{profile}/data?raw=true"
export CONSUL_HTTP_TOKEN="..."     # only if your KV is behind an ACL
```

`{project}` auto-fills from the group you pick, `{service}` from the service, `{profile}` from the form.

## Optional: keep company names out of the AI payload

```bash
export ARCHDRIFT_REDACT="acmecorp,internal.host"
```

Those strings become neutral tokens before anything is sent, and are mapped back
locally in the report you read.

## What the score means

| Dimension | Weight | Question |
|---|:-:|---|
| components | 40% | is every box real, and every real integration drawn? |
| flows | 35% | does every arrow run the drawn direction, in the numbered order? |
| bindings | 25% | do topic / client / DB names equal the labels, character for character? |

Schemas and request/response models are contract testing's job — not checked here.
Dead code is never penalized. Vault-hidden values are never a mismatch.
`CLEAN` ≥ 90 · `PASSING` ≥ 70 · below that, fix the code or update the diagram.

## Drawing rules the gate relies on

Open [`sample-hld.drawio`](sample-hld.drawio) in [diagrams.net](https://app.diagrams.net) as a template.
Boxes are components (service, topics, DBs, external systems). Glue every arrow to
both boxes. One arrow per interaction. Number your service's steps `1.0 1.1 1.2` in
execution order. Namespace in brackets: `ms-card [team-namespace]`.

![supported HLD shape](sample-hld.svg)

## CLI / CI

Same engine, no browser:

```bash
uv run archdrift.py /path/to/ms-service /path/to/HLD.drawio --html report.html
# exit 0 → merge · exit 1 → drift
```

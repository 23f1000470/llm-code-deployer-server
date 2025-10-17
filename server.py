import os, re, json, base64, time, asyncio
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx

app = FastAPI()

# ---- ENV ----
SECRET = os.getenv("SECRET", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "")
LLM_URL = (os.getenv("LLM_URL") or "").strip()
LLM_AUTH = (os.getenv("LLM_AUTH") or "").strip()
GITHUB_API = "https://api.github.com"

# ---- MODELS ----
class Attachment(BaseModel):
    name: str
    url: str  # data URI

class TaskPayload(BaseModel):
    email: str
    secret: str
    task: str
    round: int
    nonce: str
    brief: str
    checks: List[str]
    evaluation_url: str
    attachments: List[Attachment] = []

# ---- CONSTANTS ----
MIT_LICENSE = """MIT License

Copyright (c) {year} {owner}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction...
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND...
"""

# Minimal workflow: upload artifact -> deploy pages (no configure-pages step)
PAGES_WORKFLOW = """name: GitHub Pages

on:
  push:
    branches: [ main ]
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: true

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Upload static site
        uses: actions/upload-pages-artifact@v3
        with:
          path: ./
  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - id: deployment
        uses: actions/deploy-pages@v4
"""

DATA_URI_RE = re.compile(r"^data:([\w/.\+\-]+);base64,(.*)$", re.IGNORECASE)

# ---- HTTP helpers ----
async def gh(method: str, url: str, json_body=None, content=None, extra_headers=None):
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    if extra_headers:
        headers.update(extra_headers)
    async with httpx.AsyncClient(timeout=60) as client:
        return await client.request(method, url, json=json_body, headers=headers, content=content)

def b64encode_bytes(b: bytes) -> str:
    return base64.b64encode(b).decode()

def parse_attachments(attachments: List[Attachment]) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    for a in attachments:
        m = DATA_URI_RE.match(a.url)
        if not m:
            raise ValueError(f"Attachment {a.name} is not a base64 data URI")
        out[a.name] = base64.b64decode(m.group(2))
    return out

# ---- LLM (optional) ----
async def try_llm_generate(brief: str, att_names: List[str]) -> Optional[Dict[str, str]]:
    if not (LLM_URL and LLM_AUTH):
        return None
    url = LLM_URL.rstrip("/") + "/chat/completions"
    body = {
        "model": "google/gemini-2.0-flash-lite-001",
        "messages": [
            {"role": "system", "content": "Return STRICT JSON with a top-level 'files' object."},
            {"role": "user", "content":
                f"Build a minimal static app for GitHub Pages. Include index.html and README.md.\nBrief: {brief}\nAttachments: {att_names}\nReturn JSON ONLY."}
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"}
    }
    headers = {"Authorization": LLM_AUTH, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
            parsed = json.loads(content)
            files = parsed.get("files")
            if isinstance(files, dict) and "index.html" in files:
                return {k: str(v) for k, v in files.items()}
    except Exception:
        return None
    return None

# ---- Templates (fallback) ----
def tmpl_sum_of_sales() -> str:
    return """<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sales Summary</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
</head><body class="container p-4">
<h1>Sales Summary</h1>
<p>Total: <strong id="total-sales">0</strong></p>
<table id="product-sales" class="table table-striped d-none">
  <thead><tr><th>Product</th><th>Sales</th></tr></thead><tbody></tbody>
</table>
<script>
(async () => {
  const text = await fetch('data.csv').then(r=>r.text());
  const rows = text.trim().split(/\\n+/).slice(1).map(l=>l.split(','));
  let sum=0; const by={};
  for (const [p,v] of rows) { const n=parseFloat(v); if(!isNaN(n)){ sum+=n; by[p]=(by[p]||0)+n; } }
  document.querySelector('#total-sales').textContent = sum.toFixed(2);
  const tbody = document.querySelector('#product-sales tbody');
  for (const [k,v] of Object.entries(by)) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${k}</td><td>${v.toFixed(2)}</td>`;
    tbody.appendChild(tr);
  }
  if (Object.keys(by).length) document.querySelector('#product-sales').classList.remove('d-none');
})();
</script></body></html>"""

def template_from_brief(brief: str, atts: Dict[str, bytes]) -> Dict[str, bytes]:
    b = brief.lower()
    files: Dict[str, bytes] = {}
    if "sum-of-sales" in b or ("sales" in b and "csv" in b):
        files["index.html"] = tmpl_sum_of_sales().encode()
        if "data.csv" not in atts:
            files["data.csv"] = b"product,sales\nA,10\nB,20.5\n"
        return files
    # generic fallback
    files["index.html"] = f"<!doctype html><html><body><h1>Generated App</h1><pre>{brief}</pre></body></html>".encode()
    return files

# ---- GitHub helpers (automation) ----
async def ensure_repo(owner: str, repo: str) -> Dict:
    r = await gh("GET", f"{GITHUB_API}/repos/{owner}/{repo}")
    if r.status_code == 404:
        r = await gh("POST", f"{GITHUB_API}/user/repos", json_body={
            "name": repo,
            "private": False,
            "auto_init": True,
            "description": "Auto-generated by LLM code deployer"
        })
        r.raise_for_status()
    else:
        r.raise_for_status()
    return r.json()

async def ensure_actions_write_permissions(owner: str, repo: str):
    """
    Give the workflow GITHUB_TOKEN write perms so deploy-pages can publish.
    API: PUT /repos/{owner}/{repo}/actions/permissions/workflow
    body: { "default_workflow_permissions": "write", "can_approve_pull_request_reviews": false }
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/actions/permissions/workflow"
    payload = {"default_workflow_permissions": "write", "can_approve_pull_request_reviews": False}
    r = await gh("PUT", url, json_body=payload)
    if r.status_code not in (200, 204):
        print("⚠️ Failed to set workflow write permissions:", r.status_code, r.text)

async def ensure_pages_site(owner: str, repo: str):
    """
    Ensure GitHub Pages exists and is set to build from Actions.
    - GET /repos/{owner}/{repo}/pages
      - 404 -> POST (create)
      - 200 -> ensure build_type='workflow' via PUT (update)
    """
    get_url = f"{GITHUB_API}/repos/{owner}/{repo}/pages"
    r = await gh("GET", get_url)
    if r.status_code == 404:
        # Create
        create_url = get_url  # POST to /pages
        payload = {"build_type": "workflow"}
        r2 = await gh("POST", create_url, json_body=payload)
        if r2.status_code not in (201, 204):
            raise HTTPException(status_code=500, detail=f"Failed to create Pages site: {r2.status_code} {r2.text}")
        return
    elif r.status_code == 200:
        data = r.json()
        if data.get("build_type") != "workflow":
            # Update to workflow
            payload = {"build_type": "workflow"}
            r2 = await gh("PUT", get_url, json_body=payload)
            if r2.status_code not in (200, 204):
                print("⚠️ Could not switch Pages to workflow build_type:", r2.status_code, r2.text)
        return
    else:
        raise HTTPException(status_code=500, detail=f"Unexpected Pages GET: {r.status_code} {r.text}")

async def put_file(owner: str, repo: str, path: str, content_bytes: bytes, message: str):
    # Read current SHA if exists
    sha = None
    r = await gh("GET", f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}")
    if r.status_code == 200:
        sha = r.json().get("sha")
    enc = b64encode_bytes(content_bytes)
    payload = {"message": message, "content": enc}
    if sha:
        payload["sha"] = sha
    r = await gh("PUT", f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}", json_body=payload)
    r.raise_for_status()
    return r.json()["commit"]["sha"]

async def commit_bundle(owner: str, repo: str, files: Dict[str, bytes], brief: str, task: str, round_i: int, pages_url: str) -> str:
    # LICENSE
    commit_sha = await put_file(owner, repo, "LICENSE",
                                MIT_LICENSE.format(year=time.strftime("%Y"), owner=owner).encode(),
                                f"[{task}] LICENSE")
    # README
    readme = f"# {repo}\n\nTask: `{task}` (round {round_i})\n\nBrief:\n\n{brief}\n\nPages: {pages_url}\n\nLicense: MIT\n"
    commit_sha = await put_file(owner, repo, "README.md", readme.encode(), f"[{task}] README")
    # Workflow
    commit_sha = await put_file(owner, repo, ".github/workflows/pages.yml",
                                PAGES_WORKFLOW.encode(), f"[{task}] pages workflow")
    # App files
    for path, blob in files.items():
        commit_sha = await put_file(owner, repo, path, blob, f"[{task}] {path}")
    return commit_sha

async def wait_for_200(url: str, timeout_s: int = 180):
    start = time.time()
    async with httpx.AsyncClient(timeout=15) as client:
        last = None
        while time.time() - start < timeout_s:
            try:
                r = await client.get(url, headers={"Cache-Control": "no-cache"})
                last = r.status_code
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(3)
    print(f"⚠️ Pages not 200 within {timeout_s}s (last={last})")
    return False

async def post_with_backoff(url: str, payload: dict, max_tries: int = 8):
    delay = 1
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(max_tries):
            try:
                r = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(delay)
            delay *= 2
    return False

# ---- ROUTES ----
@app.get("/")
def root():
    return {"status": "ok", "app": "llm-code-deployer"}

@app.post("/api-task")
async def api_task(body: TaskPayload):
    if body.secret != SECRET:
        raise HTTPException(status_code=403, detail="invalid secret")
    if not GITHUB_TOKEN or not GITHUB_USERNAME:
        raise HTTPException(status_code=500, detail="server missing GitHub config")

    # attachments
    att_bytes = parse_attachments(body.attachments)

    # generate files via LLM if configured, else fallback templates
    files_text = await try_llm_generate(body.brief, list(att_bytes.keys()))
    if files_text:
        files: Dict[str, bytes] = {p: (v.encode() if isinstance(v, str) else v) for p, v in files_text.items()}
    else:
        files = template_from_brief(body.brief, att_bytes)

    # merge attachments (don’t overwrite generated ones)
    for name, blob in att_bytes.items():
        files.setdefault(name, blob)

    repo_name = body.task.replace("/", "-")
    pages_url = f"https://{GITHUB_USERNAME}.github.io/{repo_name}/"

    # ensure repo exists
    await ensure_repo(GITHUB_USERNAME, repo_name)

    # **programmatically allow workflow token to write**
    await ensure_actions_write_permissions(GITHUB_USERNAME, repo_name)

    # **programmatically create/enable Pages (build from Actions)**
    await ensure_pages_site(GITHUB_USERNAME, repo_name)

    # commit everything (workflow + files)
    commit_sha = await commit_bundle(GITHUB_USERNAME, repo_name, files, body.brief, body.task, body.round, pages_url)

    # wait briefly for Pages (best-effort)
    await wait_for_200(pages_url, timeout_s=180)

    resp = {
        "email": body.email,
        "task": body.task,
        "round": body.round,
        "nonce": body.nonce,
        "repo_url": f"https://github.com/{GITHUB_USERNAME}/{repo_name}",
        "commit_sha": commit_sha,
        "pages_url": pages_url,
    }

    # ping evaluator
    await post_with_backoff(body.evaluation_url, resp)
    return resp

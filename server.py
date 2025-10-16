import os, re, time, base64, json
from typing import List, Dict, Tuple

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# -------- ENV --------
SECRET = os.getenv("SECRET", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_USERNAME = os.getenv("GITHUB_USERNAME", "")

if not (SECRET and GITHUB_TOKEN and GITHUB_USERNAME):
    raise RuntimeError("Set SECRET, GITHUB_TOKEN, GITHUB_USERNAME in environment.")

# -------- Models --------
class Attachment(BaseModel):
    name: str
    url: str  # data URI

class TaskRequest(BaseModel):
    email: str
    secret: str
    task: str
    round: int
    nonce: str
    brief: str
    checks: List[str] = Field(default_factory=list)
    evaluation_url: str
    attachments: List[Attachment] = Field(default_factory=list)

# -------- Const --------
GH_API = "https://api.github.com"
PAGES_WORKFLOW = """name: Deploy static site to Pages
on:
  push:
    branches: ["main"]
  workflow_dispatch:
permissions:
  contents: read
  pages: write
  id-token: write
concurrency:
  group: "pages"
  cancel-in-progress: true
jobs:
  deploy:
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: '.'
      - id: deployment
        uses: actions/deploy-pages@v4
"""
LICENSE_TEXT = """MIT License

Copyright (c) {YEAR} {AUTHOR}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
GITIGNORE = ".env\n.DS_Store\nnode_modules/\n__pycache__/\n"

# Minimal captcha page using Tesseract.js; supports ?url=...
INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Captcha Solver</title>
</head>
<body>
  <h1>Captcha Solver</h1>
  <p>Pass an image with <code>?url=...</code>. Defaults to attached sample.</p>
  <img id="img" alt="captcha" style="max-width:320px;display:block;margin:1rem 0" />
  <div id="status" aria-live="polite">idle</div>
  <div><strong>Solved:</strong> <span id="solved"></span></div>

  <script src="https://cdn.jsdelivr.net/npm/tesseract.js@5/dist/tesseract.min.js"></script>
  <script>
    (async () => {
      const elImg = document.getElementById("img");
      const elStatus = document.getElementById("status");
      const elSolved = document.getElementById("solved");
      const params = new URLSearchParams(location.search);
      const url = params.get("url") || "sample.png";
      elImg.src = url;
      elStatus.textContent = "recognizing...";
      try {
        const { data: { text } } = await Tesseract.recognize(url, 'eng', {
          logger: m => elStatus.textContent = m.status || "working..."
        });
        elSolved.textContent = (text || "").trim();
        elStatus.textContent = "done";
      } catch (e) {
        elStatus.textContent = "error: " + e.message;
      }
    })();
  </script>
</body>
</html>
"""

# -------- Helpers --------
def repo_slug(task: str) -> str:
    return re.sub(r"[^a-z0-9-_]+", "-", task.lower()).strip("-") or "task-repo"

def gh_headers() -> Dict[str, str]:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": GITHUB_USERNAME or "llm-deployer",  # <-- add
    }

async def gh_create_repo(repo: str):
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{GH_API}/user/repos",
            headers=gh_headers(),
            json={
                "name": repo,
                "private": False,
                "auto_init": True,   # <-- was False
                "description": "Auto-generated for evaluation.",
                "has_issues": True,
                "has_wiki": False,
                "has_projects": False,
                "default_branch": "main"
            },
        )
        if r.status_code not in (201, 422):  # 422: already exists
            raise HTTPException(500, f"Repo create failed: {r.text}")


async def gh_put_bytes(repo: str, path: str, content: bytes, message: str):
    encoded = base64.b64encode(content).decode()
    url = f"{GH_API}/repos/{GITHUB_USERNAME}/{repo}/contents/{path}"
    async with httpx.AsyncClient(timeout=60) as client:
        # 1) Check if file already exists (to fetch its sha)
        sha = None
        get_r = await client.get(
            url,
            headers=gh_headers(),
            params={"ref": "main"}
        )
        if get_r.status_code == 200:
            try:
                sha = get_r.json().get("sha")
            except Exception:
                sha = None  # fallback safely

        # 2) Create or update with/without sha
        payload = {"message": message, "content": encoded, "branch": "main"}
        if sha:
            payload["sha"] = sha

        put_r = await client.put(url, headers=gh_headers(), json=payload)
        if put_r.status_code not in (200, 201):
            raise HTTPException(500, f"Put {path} failed: {put_r.text}")


async def gh_latest_sha(repo: str) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            f"{GH_API}/repos/{GITHUB_USERNAME}/{repo}/commits",
            headers=gh_headers(), params={"sha": "main", "per_page": 1}
        )
        r.raise_for_status()
        return r.json()[0]["sha"]

def pages_url(repo: str) -> str:
    return f"https://{GITHUB_USERNAME}.github.io/{repo}/"

def decode_data_uri(data_uri: str) -> Tuple[bytes, str]:
    if not data_uri.startswith("data:"):
        raise ValueError("Only data: URIs supported in this minimal build.")
    header, data = data_uri.split(",", 1)
    raw = base64.b64decode(data) if ";base64" in header else httpx.utils.unquote_to_bytes(data)
    mime = header.split(";")[0][5:] or "application/octet-stream"
    return raw, mime

async def wait_for_200(url: str, max_seconds=300) -> bool:
    start, delay = time.time(), 2
    while time.time() - start < max_seconds:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10) as c:
                r = await c.get(url)
                if r.status_code == 200:
                    return True
        except Exception:
            pass
        time.sleep(delay)
        delay = min(delay * 2, 30)
    return False

async def post_with_backoff(url: str, payload: Dict, attempts=6) -> bool:
    delay = 1
    for _ in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(url, json=payload, headers={"Content-Type": "application/json"})
                if r.status_code == 200:
                    return True
        except Exception:
            pass
        time.sleep(delay); delay = min(delay * 2, 64)
    return False

def build_readme(task: TaskRequest, live_url: str) -> str:
    return f"""# {task.task}

**Live:** {live_url}

## Summary
Auto-generated static page from a structured brief.

## Brief
> {task.brief}

## Usage
Open the Pages URL above. Pass an image at `?url=...` (defaults to `sample.png`).

## Code
- `index.html`: captcha solver using Tesseract.js from CDN.
- Attachments saved at repo root (e.g., `sample.png`).

## License
MIT (see LICENSE)
"""

# -------- Pipeline --------
async def create_or_update_repo(task: TaskRequest):
    repo = repo_slug(task.task)
    await gh_create_repo(repo)

    year = time.gmtime().tm_year
    await gh_put_bytes(repo, ".gitignore", GITIGNORE.encode(), "chore: gitignore")
    await gh_put_bytes(repo, "LICENSE", LICENSE_TEXT.format(YEAR=year, AUTHOR=GITHUB_USERNAME).encode(), "chore: MIT license")
    await gh_put_bytes(repo, "index.html", INDEX_HTML.encode(), "feat: add index.html")
    await gh_put_bytes(repo, ".github/workflows/pages.yml", PAGES_WORKFLOW.encode(), "ci: setup pages")
    live = pages_url(repo)
    readme = build_readme(task, live)
    await gh_put_bytes(repo, "README.md", readme.encode(), "docs: add README")

    for a in task.attachments:
        if a.url.startswith("data:"):
            raw, _ = decode_data_uri(a.url)
            await gh_put_bytes(repo, a.name, raw, f"feat: add attachment {a.name}")

    await wait_for_200(live, max_seconds=300)

    sha = await gh_latest_sha(repo)
    return f"https://github.com/{GITHUB_USERNAME}/{repo}", sha, live

async def notify(task: TaskRequest, repo_url: str, sha: str, live: str):
    payload = {
        "email": task.email,
        "task": task.task,
        "round": task.round,
        "nonce": task.nonce,
        "repo_url": repo_url,
        "commit_sha": sha,
        "pages_url": live,
    }
    ok = await post_with_backoff(task.evaluation_url, payload, attempts=6)
    if not ok:
        raise HTTPException(502, "Failed to POST to evaluation_url after retries.")

# -------- App --------
app = FastAPI(title="Bare-Min LLM Code Deployment API")

@app.post("/api-task")
async def api_task(task: TaskRequest):
    if task.secret != SECRET:
        raise HTTPException(401, "Invalid secret")
    repo_url, sha, live = await create_or_update_repo(task)
    await notify(task, repo_url, sha, live)
    return {"status": "ok", "round": task.round, "repo_url": repo_url, "commit_sha": sha, "pages_url": live}

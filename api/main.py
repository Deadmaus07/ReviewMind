"""ReviewMind demonstration server.

Serves two things for the project demonstration:

  /          a results dashboard -- the experiment, the ablation, the costs, and
             the three validity threats we found in our own method
  /demo      a live review page -- paste a code change, watch ReviewMind
             retrieve context and report defects

This also closes a gap against the original proposal, which specified FastAPI as
the backend orchestrating fetch -> retrieve -> review but had no exposed service
layer until now.

Run:
    .venv/bin/uvicorn api.main:app --reload --port 8000
"""

from __future__ import annotations

import glob
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from reviewmind.arms import llm_arm, static_arm  # noqa: E402
from reviewmind.parsing.chunker import chunk_repo  # noqa: E402
from reviewmind.retrieval.graph import GraphRetriever  # noqa: E402
from reviewmind.review.llm import build_llm  # noqa: E402

app = FastAPI(title="ReviewMind", version="0.2.0-a2")
templates = Jinja2Templates(directory=str(ROOT / "api" / "templates"))

CORPUS = ROOT / "experiments" / "corpus" / "taskapi"
DATASET = ROOT / "experiments" / "dataset" / "cases"

# Built once at startup: indexing on every request would make the live demo feel
# slow for reasons unrelated to the thing being demonstrated.
_retriever: Optional[GraphRetriever] = None


def retriever() -> GraphRetriever:
    global _retriever
    if _retriever is None:
        _retriever = GraphRetriever(chunk_repo(CORPUS))
    return _retriever


# --------------------------------------------------------------------------- #
# Results loading
# --------------------------------------------------------------------------- #

def latest_summary() -> dict[str, Any]:
    """Most recent run that is valid for reporting.

    Deliberately skips runs flagged valid_for_reporting=false (mock backend), so
    the dashboard cannot display pipeline-test output as though it were a result.
    """
    for path in sorted(glob.glob(str(ROOT / "results" / "run_*" / "summary.json")), reverse=True):
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("config", {}).get("valid_for_reporting"):
            if len(data.get("arms", {})) >= 3:
                return data
    return {}


def load_json(rel: str) -> dict[str, Any]:
    p = ROOT / rel
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def demo_cases() -> list[dict[str, Any]]:
    """Seeded cases offered as one-click examples on the live demo page."""
    out = []
    for cid in ("add_null_deref_001", "add_double_scale_001", "off_by_one_001",
                "add_ignored_return_001", "hardcoded_secret_001"):
        f = DATASET / cid / "case.json"
        if f.exists():
            d = json.loads(f.read_text())
            out.append({
                "case_id": d["case_id"],
                "defect_class": d["defect_class"],
                "description": d["description"],
                "cross_file": d["requires_cross_file_context"],
                "injection_mode": d.get("injection_mode", "deletion"),
                "diff": d["diff"],
            })
    return out


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    summary = latest_summary()
    arms = summary.get("arms", {})
    config = summary.get("config", {})

    def arm(name: str) -> dict[str, Any]:
        a = arms.get(name, {})
        return {
            "recall": a.get("recall", 0) * 100,
            "semantic": a.get("recall_semantic", 0) * 100,
            "ci": [round(x * 100) for x in a.get("recall_semantic_ci95", [0, 0])],
            "precision": a.get("precision_lower_bound", 0) * 100,
            "tokens": round(a.get("total_tokens", 0) / max(1, a.get("n_cases", 1))),
            "latency": a.get("median_latency_s", 0),
            "by_mode": a.get("semantic_by_injection_mode", {}),
            "by_class": a.get("recall_by_class", {}),
            "n_cases": a.get("n_cases", 0),
        }

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "static": arm("static"),
        "llm_diff": arm("llm_diff"),
        "llm_rag": arm("llm_rag"),
        "config": config,
        "ablation": load_json("results/retrieval_ablation.json"),
        "sourcegraph": load_json("results/sourcegraph_navigation.json"),
        "run_name": summary.get("run", "n/a"),
    })


@app.get("/demo", response_class=HTMLResponse)
def demo(request: Request):
    return templates.TemplateResponse("demo.html", {
        "request": request,
        "cases": demo_cases(),
    })


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

class ReviewRequest(BaseModel):
    diff: str
    use_retrieval: bool = True
    changed_file: Optional[str] = None


@app.post("/api/review")
def api_review(req: ReviewRequest):
    """Review a diff. `use_retrieval=False` reproduces the diff-only arm.

    Exposing both arms here is intentional: it lets a demonstration show the
    SAME change reviewed with and without retrieval, which is the project's
    central comparison, rather than asking the audience to take it on trust.
    """
    if not req.diff.strip():
        return JSONResponse({"error": "empty diff"}, status_code=400)

    changed = req.changed_file
    if not changed:
        for line in req.diff.splitlines():
            if line.startswith("+++ b/"):
                changed = line[6:].strip()
                break

    started = time.perf_counter()
    context: list[dict[str, Any]] = []

    if req.use_retrieval:
        r = retriever()
        src_path = CORPUS / changed if changed else None
        hits = r.search(
            req.diff, top_k=6,
            exclude_files=[changed] if changed else [],
            changed_file_source=(src_path.read_text() if src_path and src_path.exists() else None),
            changed_file=changed,
        )
        context = [{
            "file": h.chunk.file, "name": h.chunk.name,
            "start_line": h.chunk.start_line, "end_line": h.chunk.end_line,
            "direction": getattr(h, "direction", "lexical"),
            "text": h.chunk.text[:600],
        } for h in hits]
        result = llm_arm.review("live", req.diff, build_llm(), retriever=r,
                                repo_dir=CORPUS, changed_file=changed, top_k=6)
    else:
        result = llm_arm.review("live", req.diff, build_llm())

    payload = result.to_dict()
    payload["context"] = context
    payload["wall_s"] = round(time.perf_counter() - started, 2)
    return payload


@app.post("/api/static")
def api_static(req: ReviewRequest):
    """Run the static-analysis baseline (ruff + bandit) for side-by-side demos."""
    changed = req.changed_file
    if not changed:
        for line in req.diff.splitlines():
            if line.startswith("+++ b/"):
                changed = line[6:].strip()
                break
    if not changed:
        return JSONResponse({"error": "could not determine changed file"}, status_code=400)
    res = static_arm.review("live", CORPUS, [changed])
    return res.to_dict()


class AskRequest(BaseModel):
    question: str


@app.post("/api/ask")
def api_ask(req: AskRequest):
    """Code Q&A over the corpus."""
    from reviewmind.qa.bot import CodeQABot
    bot = CodeQABot(CORPUS, build_llm())
    return bot.ask(req.question).to_dict()


@app.get("/api/health")
def health():
    import os
    return {
        "ok": True,
        "chunks_indexed": len(retriever()),
        "llm_mode": os.getenv("REVIEWMIND_LLM_MODE", "mock"),
        "model": os.getenv("GROQ_MODEL", "unset"),
        "key_present": bool(os.getenv("GROQ_API_KEY", "").strip()),
    }

"""Phase 4 spike: does screenshot retrieval find the right page in YOUR Hebrew PDFs?

Before any docs stage is built, this answers one question with numbers: given
questions whose answers you already know (file + page), how often is the right
page among the top 5 results (recall@5)?

The method is PixelRAG's — render each page as an image, embed it with
Qwen3-VL-Embedding, embed the question as text, rank pages by cosine — without
PixelRAG's own infrastructure, which does not fit this machine:

- its PDF renderer is pdf2image/poppler, which has no official Windows build;
  pages are rendered here with PyMuPDF (already a PixelRAG dependency);
- its CPU path (`pixelrag_embed.embed_cpu`) does not apply the English LoRA
  adapter at all, so on CPU "PixelRAG" means the base embedding model anyway.

Prompts match PixelRAG 0.4.0 exactly (read from its source): a page is
embedded as [image + "What is shown in this image?"]; a query as
[system: DEFAULT_INSTRUCTION, user: text]; last-token pooling, L2 normalised.

Inputs, kept out of git (docs_input/ is ignored):
    docs_input/*.pdf
    docs_input/questions.yaml   — list of {question, file, pages: [n, ...]}
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np
import yaml

from .errors import ConfigError, ProviderError

log = logging.getLogger(__name__)

DEFAULT_MODEL = "Qwen/Qwen3-VL-Embedding-2B"
QUERY_INSTRUCTION = "Retrieve images or text relevant to the user's query."   # PixelRAG default
PAGE_PROMPT = "What is shown in this image?"                                   # PixelRAG embed_cpu
MAX_WIDTH = 875          # PixelRAG's CPU width clamp
PATCH = 28               # Qwen3-VL patch size


# ----------------------------------------------------------------- questions
@dataclass(frozen=True)
class Question:
    question: str
    file: str
    pages: tuple[int, ...]          # 1-based page numbers that answer it


def load_questions(path: Path, pdf_names: set[str]) -> list[Question]:
    if not path.exists():
        raise ConfigError(f"{path} not found. Write it as a YAML list of "
                          "{question, file, pages} — see examples/questions.example.yaml")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"{path}: expected a non-empty list of questions")
    out = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict) or not {"question", "file", "pages"} <= set(item):
            raise ConfigError(f"{path}: item {i} needs question, file and pages")
        pages = item["pages"] if isinstance(item["pages"], list) else [item["pages"]]
        if not pages or not all(isinstance(p, int) and p >= 1 for p in pages):
            raise ConfigError(f"{path}: item {i} pages must be page numbers from 1")
        if item["file"] not in pdf_names:
            raise ConfigError(f"{path}: item {i} names '{item['file']}', which is not in "
                              f"docs_input/ (found: {sorted(pdf_names)})")
        out.append(Question(str(item["question"]), str(item["file"]), tuple(pages)))
    return out


# ------------------------------------------------------------------ rendering
def render_pages(pdf: Path, out_dir: Path, *, dpi: int = 110) -> list[Path]:
    """Each page as a JPEG no wider than MAX_WIDTH, cached by file content."""
    import pymupdf
    from PIL import Image

    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()[:16]
    target = out_dir / f"{pdf.stem}-{digest}"
    done = target / "done"
    if done.exists():
        return sorted(target.glob("page_*.jpg"))
    target.mkdir(parents=True, exist_ok=True)
    try:
        doc = pymupdf.open(pdf)
    except Exception as exc:  # noqa: BLE001 — a broken PDF is reported, not crashed on
        raise ProviderError(f"cannot open {pdf.name}: {exc}") from exc
    paths = []
    for number, page in enumerate(doc, 1):
        pix = page.get_pixmap(dpi=dpi)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        if img.width > MAX_WIDTH:
            scale = MAX_WIDTH / img.width
            img = img.resize((_align(img.width * scale), _align(img.height * scale)))
        path = target / f"page_{number:04d}.jpg"
        img.save(path, "JPEG", quality=88)
        paths.append(path)
    done.write_text(str(len(paths)))
    return paths


def _align(v: float) -> int:
    return max(PATCH, round(v / PATCH) * PATCH)


# ------------------------------------------------------------------- models
class Embedder(Protocol):
    name: str

    def embed_pages(self, images: Sequence[Path]) -> np.ndarray: ...
    def embed_queries(self, texts: Sequence[str]) -> np.ndarray: ...


class QwenVLEmbedder:
    """Qwen3-VL-Embedding through transformers, the way PixelRAG uses it."""

    def __init__(self, model_id: str = DEFAULT_MODEL, *, device: str = "cpu",
                 dtype: str = "float32", revision: str | None = None):
        try:
            import torch
            from transformers import AutoProcessor, Qwen3VLForConditionalGeneration
        except ImportError as exc:
            raise ProviderError(f"transformers/torch missing ({exc}). "
                                "Run: pip install -r requirements.txt") from exc
        self.torch = torch
        self.name = f"{model_id}@{revision or 'latest'} ({dtype}, {device})"
        log.info("loading %s — the first run downloads the weights", self.name)
        try:
            self.processor = AutoProcessor.from_pretrained(model_id, revision=revision)
            self.model = Qwen3VLForConditionalGeneration.from_pretrained(
                model_id, revision=revision, dtype=getattr(torch, dtype),
                attn_implementation="sdpa").eval().to(device)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"could not load {model_id}: {type(exc).__name__}: {exc}") from exc
        self.device = device

    def _pool(self, messages: list, images) -> np.ndarray:
        torch = self.torch
        text = self.processor.apply_chat_template(messages, tokenize=False,
                                                  add_generation_prompt=True)
        kwargs = {"text": [text], "return_tensors": "pt", "padding": True}
        if images:
            kwargs["images"] = images
        inputs = {k: v.to(self.device) if hasattr(v, "to") else v
                  for k, v in self.processor(**kwargs).items()}
        with torch.no_grad():
            hidden = self.model.model(**inputs).last_hidden_state
        last = int(inputs["attention_mask"].sum(dim=1)[0]) - 1
        vec = torch.nn.functional.normalize(hidden[0, last].float(), dim=-1)
        return vec.cpu().numpy()

    def embed_pages(self, images: Sequence[Path]) -> np.ndarray:
        from PIL import Image

        out = []
        for path in images:
            img = Image.open(path).convert("RGB")
            out.append(self._pool([{"role": "user", "content": [
                {"type": "image", "image": img}, {"type": "text", "text": PAGE_PROMPT}]}], [img]))
        return np.stack(out)

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack([self._pool([
            {"role": "system", "content": [{"type": "text", "text": QUERY_INSTRUCTION}]},
            {"role": "user", "content": [{"type": "text", "text": t}]}], None) for t in texts])


# ------------------------------------------------------------------- the spike
@dataclass
class SpikeResult:
    model: str
    pages: int
    seconds_per_page: float
    recall_at_1: float
    recall_at_5: float
    mrr: float
    rows: list[dict]


def run_spike(input_dir: Path, work_dir: Path, embedder: Embedder, *, k: int = 5,
              dpi: int = 110) -> SpikeResult:
    pdfs = sorted(p for p in input_dir.glob("*.pdf"))
    if not pdfs:
        raise ConfigError(f"no PDF files in {input_dir}")
    questions = load_questions(input_dir / "questions.yaml", {p.name for p in pdfs})

    pages: list[tuple[str, int, Path]] = []
    for pdf in pdfs:
        for number, path in enumerate(render_pages(pdf, work_dir / "pages", dpi=dpi), 1):
            pages.append((pdf.name, number, path))
    for q in questions:
        count = sum(1 for f, _, _ in pages if f == q.file)
        bad = [p for p in q.pages if p > count]
        if bad:
            raise ConfigError(f"question '{q.question[:40]}': {q.file} has {count} pages, "
                              f"not {bad}")

    vectors = _cached_page_vectors(pages, work_dir / "vectors", embedder)
    started = time.monotonic()
    queries = embedder.embed_queries([q.question for q in questions])
    log.info("embedded %d questions in %.1fs", len(questions), time.monotonic() - started)

    scores = queries @ vectors["matrix"].T
    rows, hits1, hits5, rr = [], 0, 0, 0.0
    for q, row in zip(questions, scores):
        order = np.argsort(-row)
        ranked = [(pages[i][0], pages[i][1], float(row[i])) for i in order]
        rank = next((n for n, (f, p, _) in enumerate(ranked, 1) if f == q.file and p in q.pages),
                    None)
        hits1 += rank == 1
        hits5 += rank is not None and rank <= k
        rr += 1 / rank if rank else 0.0
        rows.append({"question": q.question, "expected": f"{q.file} p.{','.join(map(str, q.pages))}",
                     "rank": rank, "top": [f"{f} p.{p} ({s:.3f})" for f, p, s in ranked[:k]]})
    n = len(questions)
    return SpikeResult(model=embedder.name, pages=len(pages),
                       seconds_per_page=vectors["seconds_per_page"],
                       recall_at_1=hits1 / n, recall_at_5=hits5 / n, mrr=rr / n, rows=rows)


def _cached_page_vectors(pages, cache_dir: Path, embedder: Embedder) -> dict:
    """Page vectors cached per (model, page image), so a rerun with more
    questions does not re-embed hundreds of pages."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    model_key = hashlib.sha256(embedder.name.encode()).hexdigest()[:12]
    vectors, todo, timed = [None] * len(pages), [], []
    for i, (_, _, path) in enumerate(pages):
        key = hashlib.sha256(path.read_bytes()).hexdigest()[:20]
        cached = cache_dir / f"{model_key}-{key}.npy"
        if cached.exists():
            vectors[i] = np.load(cached)
        else:
            todo.append((i, path, cached))
    if todo:
        log.info("embedding %d page(s) (%d cached) — on CPU this can take a while",
                 len(todo), len(pages) - len(todo))
    for n, (i, path, cached) in enumerate(todo, 1):
        started = time.monotonic()
        vec = embedder.embed_pages([path])[0]
        timed.append(time.monotonic() - started)
        np.save(cached, vec)
        vectors[i] = vec
        if n % 10 == 0 or n == len(todo):
            log.info("  %d/%d pages embedded (%.1fs/page)", n, len(todo), float(np.mean(timed)))
    matrix = np.stack(vectors).astype(np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return {"matrix": matrix / np.where(norms == 0, 1, norms),
            "seconds_per_page": float(np.mean(timed)) if timed else 0.0}

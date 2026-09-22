"""Phase 4 spike harness: rendering, question file checks, recall maths.

The embedding model is replaced by a stand-in that "understands" pages only
through a lookup table, so recall is known in advance. PDFs are generated.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import yaml

from pipeline.docs_spike import MAX_WIDTH, load_questions, render_pages, run_spike
from pipeline.errors import ConfigError


def make_pdf(path: Path, pages: int, width: float = 1200, height: float = 1600) -> Path:
    import pymupdf

    doc = pymupdf.open()
    for n in range(pages):
        page = doc.new_page(width=width, height=height)
        page.insert_text((72, 144), f"page {n + 1} of {path.stem}", fontsize=40)
    doc.save(path)
    return path


class LookupEmbedder:
    """Each (file stem, page) gets its own axis; a question points at the page
    listed for it in `answers`, so the ranking is fully determined."""

    name = "lookup"

    def __init__(self, answers: dict[str, tuple[str, int]], stems: list[str], pages_each: int):
        self.axes = {(s, p): i for i, (s, p) in enumerate(
            (s, p) for s in stems for p in range(1, pages_each + 1))}
        self.answers = answers
        self.page_calls = 0

    def _vec(self, key):
        v = np.full(len(self.axes), 0.01)
        v[self.axes[key]] = 1.0
        return v

    def embed_pages(self, images):
        self.page_calls += len(images)
        out = []
        for path in images:
            stem = path.parent.name.rsplit("-", 1)[0]
            out.append(self._vec((stem, int(path.stem.split("_")[1]))))
        return np.stack(out)

    def embed_queries(self, texts):
        return np.stack([self._vec(self.answers[t]) for t in texts])


def write_questions(folder: Path, items):
    (folder / "questions.yaml").write_text(yaml.safe_dump(items, allow_unicode=True), encoding="utf-8")


def test_pages_render_narrow_enough_and_are_cached(tmp_path):
    pdf = make_pdf(tmp_path / "report.pdf", 3, width=2000, height=2800)
    first = render_pages(pdf, tmp_path / "out", dpi=110)
    assert len(first) == 3
    from PIL import Image
    assert all(Image.open(p).width <= MAX_WIDTH for p in first)
    stamp = first[0].stat().st_mtime_ns
    assert render_pages(pdf, tmp_path / "out", dpi=110) == first
    assert first[0].stat().st_mtime_ns == stamp            # not re-rendered


def test_hebrew_file_names_work(tmp_path):
    pdf = make_pdf(tmp_path / "דוח_רבעוני.pdf", 2)
    assert len(render_pages(pdf, tmp_path / "out")) == 2


@pytest.mark.parametrize("items,match", [
    ([{"question": "q", "file": "missing.pdf", "pages": [1]}], "not in docs_input"),
    ([{"question": "q", "file": "a.pdf"}], "needs question, file and pages"),
    ([{"question": "q", "file": "a.pdf", "pages": [0]}], "page numbers from 1"),
    ([], "non-empty list"),
])
def test_bad_question_files_are_explained(tmp_path, items, match):
    write_questions(tmp_path, items)
    with pytest.raises(ConfigError, match=match):
        load_questions(tmp_path / "questions.yaml", {"a.pdf"})


def test_recall_is_measured_against_the_expected_pages(tmp_path):
    docs = tmp_path / "docs_input"
    docs.mkdir()
    make_pdf(docs / "alpha.pdf", 4)
    make_pdf(docs / "beta.pdf", 4)
    write_questions(docs, [
        {"question": "q-right", "file": "alpha.pdf", "pages": [2]},
        {"question": "q-second-page-ok", "file": "beta.pdf", "pages": [1, 3]},
        {"question": "q-wrong", "file": "alpha.pdf", "pages": [4]},
    ])
    embedder = LookupEmbedder({"q-right": ("alpha", 2), "q-second-page-ok": ("beta", 3),
                               "q-wrong": ("beta", 2)}, ["alpha", "beta"], 4)
    result = run_spike(docs, tmp_path / "work", embedder)

    assert result.pages == 8
    ranks = [row["rank"] for row in result.rows]
    assert ranks[0] == 1 and ranks[1] == 1 and ranks[2] > 1
    assert result.recall_at_1 == pytest.approx(2 / 3)
    assert result.rows[0]["top"][0].startswith("alpha.pdf p.2")

    # a rerun re-embeds nothing: page vectors are cached per model and page
    calls = embedder.page_calls
    run_spike(docs, tmp_path / "work", embedder)
    assert embedder.page_calls == calls


def test_question_pointing_past_the_last_page_is_refused(tmp_path):
    docs = tmp_path / "docs_input"
    docs.mkdir()
    make_pdf(docs / "alpha.pdf", 2)
    write_questions(docs, [{"question": "q", "file": "alpha.pdf", "pages": [5]}])
    with pytest.raises(ConfigError, match="has 2 pages"):
        run_spike(docs, tmp_path / "work", LookupEmbedder({"q": ("alpha", 1)}, ["alpha"], 2))


def test_example_questions_file_is_valid_yaml():
    root = Path(__file__).resolve().parent.parent
    items = yaml.safe_load((root / "examples" / "questions.example.yaml").read_text(encoding="utf-8"))
    assert all({"question", "file", "pages"} <= set(i) for i in items)

"""Where should the semantic-cache threshold be? Ask the embedder, not a prior.

Twenty paraphrase pairs (the same question, reworded — a hit the cache should
serve) and twenty near-miss pairs (a few words apart, a different answer — a
hit that would be a wrong answer served with confidence). Each pair is rendered
exactly as the gateway renders a prompt, embedded, and scored with the same
cosine the cache uses. The two score distributions, and the gap between them,
are the whole argument for wherever the threshold lands.

The near misses are chosen to be *hard*: lexically almost identical, so a
threshold that only separates "capital of France" from "how tall is the Eiffel
Tower" has proved nothing. The pairs that matter are the ones where one token
changes the answer.

Usage (needs Ollama with the model pulled; the lockfile is not touched):

    ollama pull nomic-embed-text
    uv run --with matplotlib scripts/threshold_experiment.py [--model M] [--url U]

Writes ``docs/notes/threshold-experiment.png`` and prints the table.
"""

import argparse
import json
import sys
from pathlib import Path

import httpx
import numpy as np

from vortex_ai_gateway.contracts import ChatCompletionRequest
from vortex_ai_gateway.semantic import prompt_text, unit

#: The same question twice. The cache should answer the second from the first.
PARAPHRASES: list[tuple[str, str]] = [
    ("What is the capital of France?", "capital of france?"),
    ("How do I reverse a list in Python?", "python: reverse a list"),
    ("Explain what a mutex is.", "What's a mutex and what is it for?"),
    ("Convert 5 miles to kilometres.", "5 miles in km"),
    ("Why is the sky blue?", "What makes the sky look blue?"),
    ("Write a haiku about autumn.", "Compose a haiku on the theme of autumn."),
    ("What's the difference between TCP and UDP?", "TCP vs UDP — how do they differ?"),
    ("How many bytes are in a kilobyte?", "kilobyte = how many bytes"),
    ("Summarise the plot of Hamlet in two sentences.", "Give me a two-sentence summary of Hamlet."),
    ("What year did the Berlin Wall fall?", "When did the Berlin Wall come down?"),
    ("How do I undo my last git commit?", "git: revert the most recent commit"),
    ("What is the boiling point of water in Fahrenheit?", "water boils at what temp in °F?"),
    ("Translate 'good morning' into Spanish.", "How do you say good morning in Spanish?"),
    (
        "Recommend a beginner book on statistics.",
        "What's a good intro statistics book for beginners?",
    ),
    ("What does HTTP status 404 mean?", "meaning of a 404 error"),
    ("How long should I boil an egg for a soft yolk?", "soft boiled egg — how many minutes?"),
    ("Explain recursion to a ten-year-old.", "How would you describe recursion to a 10 year old?"),
    ("What's the square root of 144?", "sqrt(144) = ?"),
    ("Who painted the Mona Lisa?", "The Mona Lisa was painted by whom?"),
    ("List three uses for baking soda.", "Name 3 things you can do with baking soda."),
]

#: Almost the same words, a different answer. Serving one for the other is the
#: failure this cache can introduce and the exact cache cannot.
NEAR_MISSES: list[tuple[str, str]] = [
    ("What is the capital of France?", "What is the capital of Finland?"),
    ("How do I reverse a list in Python?", "How do I reverse a string in Python?"),
    ("Explain what a mutex is.", "Explain what a semaphore is."),
    ("Convert 5 miles to kilometres.", "Convert 5 kilometres to miles."),
    ("Why is the sky blue?", "Why is the sea blue?"),
    ("Write a haiku about autumn.", "Write a haiku about winter."),
    ("What's the difference between TCP and UDP?", "What's the difference between TCP and IP?"),
    ("How many bytes are in a kilobyte?", "How many bytes are in a megabyte?"),
    (
        "Summarise the plot of Hamlet in two sentences.",
        "Summarise the plot of Macbeth in two sentences.",
    ),
    ("What year did the Berlin Wall fall?", "What year was the Berlin Wall built?"),
    ("How do I undo my last git commit?", "How do I undo my last git merge?"),
    (
        "What is the boiling point of water in Fahrenheit?",
        "What is the freezing point of water in Fahrenheit?",
    ),
    ("Translate 'good morning' into Spanish.", "Translate 'good night' into Spanish."),
    ("Recommend a beginner book on statistics.", "Recommend an advanced book on statistics."),
    ("What does HTTP status 404 mean?", "What does HTTP status 403 mean?"),
    (
        "How long should I boil an egg for a soft yolk?",
        "How long should I boil an egg for a hard yolk?",
    ),
    ("Explain recursion to a ten-year-old.", "Explain iteration to a ten-year-old."),
    ("What's the square root of 144?", "What's the square root of 169?"),
    ("Who painted the Mona Lisa?", "Who painted The Starry Night?"),
    ("List three uses for baking soda.", "List three uses for baking powder."),
]

#: Candidate thresholds to tabulate. The chosen one need not be on this grid.
CANDIDATES = [0.80, 0.85, 0.90, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99]


def rendered(text: str) -> str:
    """The prompt exactly as the gateway would embed it: role-tagged."""
    request = ChatCompletionRequest.model_validate(
        {"model": "x", "messages": [{"role": "user", "content": text}]}
    )
    prompt = prompt_text(request)
    assert prompt is not None
    return prompt


def embed_all(texts: list[str], *, model: str, url: str) -> np.ndarray:
    """One batched call; rows are unit vectors, as the cache stores them."""
    response = httpx.post(
        f"{url.rstrip('/')}/api/embed", json={"model": model, "input": texts}, timeout=120
    )
    response.raise_for_status()
    return np.stack([unit(vector) for vector in response.json()["embeddings"]])


def pair_scores(pairs: list[tuple[str, str]], *, model: str, url: str) -> np.ndarray:
    left = embed_all([rendered(a) for a, _ in pairs], model=model, url=url)
    right = embed_all([rendered(b) for _, b in pairs], model=model, url=url)
    return np.einsum("ij,ij->i", left, right)


def choose(hits: np.ndarray, misses: np.ndarray) -> tuple[float | None, str]:
    """The threshold the data supports, and the sentence that justifies it.

    The costs are asymmetric — a false hit is a wrong answer, a false miss is
    one provider call — so the constraint is *no near miss clears it*. Within
    that, the lowest threshold keeps the most paraphrases. Three outcomes:

    - separable: every near miss scores below every paraphrase; take the
      midpoint of the gap.
    - overlapping: the threshold sits just above the worst near miss, and the
      paraphrases below it are the price, reported rather than hidden.
    - no safe threshold: the worst near miss outscores every paraphrase, so
      any threshold that serves a single paraphrase also serves a wrong answer.
      ``None`` — the honest number for this model on these prompts is "do not
      enable it", not a value that looks like a setting.
    """
    worst_miss = float(misses.max())
    weakest_hit = float(hits.min())
    strongest_hit = float(hits.max())
    if worst_miss < weakest_hit:
        gap = weakest_hit - worst_miss
        return round(worst_miss + gap / 2, 3), (
            f"separable: every near miss ≤ {worst_miss:.3f}, every paraphrase ≥ "
            f"{weakest_hit:.3f}; midpoint of the {gap:.3f} gap"
        )
    if worst_miss >= strongest_hit:
        return None, (
            f"no safe threshold: the worst near miss ({worst_miss:.3f}) outscores every "
            f"paraphrase (max {strongest_hit:.3f}); any threshold that hits at all serves "
            f"a wrong answer"
        )
    threshold = round(worst_miss + 0.005, 3)
    lost = int((hits < threshold).sum())
    if lost == len(hits):
        return None, (
            f"no safe threshold: the first value above the worst near miss ({threshold:.3f}) "
            f"is above every paraphrase too; a cache that never hits is a cache turned off"
        )
    return threshold, (
        f"overlapping: worst near miss {worst_miss:.3f} exceeds weakest paraphrase "
        f"{weakest_hit:.3f}; set just above the worst near miss, keeping "
        f"{len(hits) - lost}/{len(hits)} paraphrases"
    )


def table(hits: np.ndarray, misses: np.ndarray, chosen: float | None) -> str:
    rows = ["threshold  false hits  false misses"]
    for t in sorted({*CANDIDATES, *([chosen] if chosen is not None else [])}):
        mark = "  ◀ chosen" if t == chosen else ""
        rows.append(
            f"   {t:.3f}       {int((misses >= t).sum()):2d}/{len(misses)}        "
            f"{int((hits < t).sum()):2d}/{len(hits)}{mark}"
        )
    return "\n".join(rows)


def plot(hits: np.ndarray, misses: np.ndarray, chosen: float | None, model: str, out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, (top, bottom) = plt.subplots(
        2, 1, figsize=(9, 6), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    bins = np.linspace(0.5, 1.0, 51)
    top.hist(misses, bins=bins, alpha=0.7, color="#c0392b", label="near miss (must MISS)")
    top.hist(hits, bins=bins, alpha=0.7, color="#2980b9", label="paraphrase (should HIT)")
    verdict = "no safe threshold" if chosen is None else f"chosen threshold {chosen:.3f}"
    if chosen is not None:
        top.axvline(chosen, color="black", linestyle="--", label=verdict)
        bottom.axvline(chosen, color="black", linestyle="--")
    top.set_ylabel("pairs")
    top.set_title(f"Cosine similarity of prompt pairs — {model}: {verdict}")
    top.legend(loc="upper left")

    rng = np.random.default_rng(0)
    bottom.scatter(misses, rng.uniform(0.2, 0.4, len(misses)), color="#c0392b", s=18)
    bottom.scatter(hits, rng.uniform(0.6, 0.8, len(hits)), color="#2980b9", s=18)
    bottom.axvline(0.95, color="grey", linestyle=":")
    bottom.set_yticks([0.3, 0.7], ["near miss", "paraphrase"])
    bottom.set_xlabel("cosine similarity")
    bottom.set_xlim(0.5, 1.0)
    fig.tight_layout()
    fig.savefig(out, dpi=130)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default="nomic-embed-text")
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument(
        "--out", type=Path, default=None, help="default: docs/notes/threshold-<model>.png"
    )
    args = parser.parse_args()
    if args.out is None:
        args.out = Path("docs/notes") / f"threshold-{args.model.replace(':', '-')}.png"

    hits = pair_scores(PARAPHRASES, model=args.model, url=args.url)
    misses = pair_scores(NEAR_MISSES, model=args.model, url=args.url)
    chosen, why = choose(hits, misses)

    print(f"model: {args.model}   pairs: {len(hits)} paraphrase, {len(misses)} near miss\n")
    print(f"paraphrase  min {hits.min():.3f}  median {np.median(hits):.3f}  max {hits.max():.3f}")
    print(
        f"near miss   min {misses.min():.3f}  median {np.median(misses):.3f}  max {misses.max():.3f}\n"
    )
    print(table(hits, misses, chosen))
    print(f"\nchosen: {'none' if chosen is None else chosen}  ({why})\n")

    print("hardest near misses (highest score — the ones a lower threshold would serve wrongly):")
    for i in np.argsort(misses)[::-1][:5]:
        print(f"  {misses[i]:.3f}  {NEAR_MISSES[i][0]!r}  vs  {NEAR_MISSES[i][1]!r}")
    print("weakest paraphrases (lowest score — the ones a higher threshold loses first):")
    for i in np.argsort(hits)[:5]:
        print(f"  {hits[i]:.3f}  {PARAPHRASES[i][0]!r}  vs  {PARAPHRASES[i][1]!r}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    plot(hits, misses, chosen, args.model, args.out)
    args.out.with_suffix(".json").write_text(
        json.dumps(
            {
                "model": args.model,
                "chosen": chosen,
                "paraphrase": [round(float(s), 4) for s in hits],
                "near_miss": [round(float(s), 4) for s in misses],
            },
            indent=2,
        )
    )
    print(f"\nwrote {args.out} and {args.out.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

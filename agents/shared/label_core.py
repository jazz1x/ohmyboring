"""Pure logic for the recall labelling loop — sampling, prompting, verdict parsing, reporting.

`query_log` records which notes were injected and how far each one was. It has never recorded
whether any of them was worth injecting, so recall precision was unmeasurable and every argument
about the relevance ceiling was made from distances alone (that is why the enforce path was deleted
rather than tuned — see `agents/shared/recall_core.py`). This module is the missing half: pick which
logged hits to judge, ask one question about each, and turn the stored verdicts into a number that
refuses to exist when the sample is too small.

No I/O here. The CLI (`scripts/label-recall.py`) owns HTTP, the LLM call, and stdin.
"""

# A judge is either the local model or the person auditing it. Both labels for one hit are kept
# side by side; agreement between them is a measurement, not a conflict to resolve.
JUDGE_LLM = "llm"
JUDGE_HUMAN = "human"
JUDGES = (JUDGE_LLM, JUDGE_HUMAN)

VERDICT_RELEVANT = "relevant"
VERDICT_IRRELEVANT = "irrelevant"
VERDICT_UNSURE = "unsure"
VERDICTS = (VERDICT_RELEVANT, VERDICT_IRRELEVANT, VERDICT_UNSURE)

#: Below this many decided labels, a precision figure is noise dressed as evidence. The reporter
#: returns None instead, and callers must print the sample size rather than a percentage.
MIN_DECIDED = 30
#: Same rule for llm/human agreement: an agreement rate over a handful of pairs cannot clear an
#: unverified judge.
#:
#: The reason is NOT that the judge shares the retriever's embedding family — measured 2026-09-02,
#: the retriever embeds with `bge-m3` and the judge is `gemma4:12b`, different lineages. That
#: sentence was wrong and had been copied into three other files. The real reason is plainer and
#: survives the correction: nobody has ever checked whether this judge is any good, and a judge
#: cannot establish its own accuracy. What the floor buys is an independent reference, and the
#: construct being measured — "would a person reading this have been helped" — makes a person the
#: reference by definition rather than by preference.
MIN_COMPARED = 20


def audit_backlog(stats) -> int:
    """Human labels still owed before the agreement figure can be computed at all.

    The LLM judge accrues on its own every night; the human side only moves when a person sits
    down for a minute, and nothing in the system says so out loud. A precision number the
    contract forbids anyone from using is worse than no number: it looks like an answer. This
    turns the gap into something a daily surface can show, and returns 0 once the floor is met
    so the surface can fall silent rather than nag forever.
    """
    return max(0, MIN_COMPARED - int((stats or {}).get("compared") or 0))


#: An LLM judge whose agreement with the person falls below this is not usable as an instrument;
#: the reporter says so and the human-only numbers are the ones that count.
AGREEMENT_FLOOR = 0.80

#: Hits inside this cosine band are the ones a relevance decision would actually turn on, so the
#: human audit spends its minute there instead of on obvious hits. Chosen to straddle the measured
#: overlap (negatives from 0.4073, positives to 0.5146) rather than any single threshold.
AUDIT_BAND = (0.45, 0.56)


def _hits(entry):
    """(hit_index, path, dist, dist_kind) for one /query-log entry, positions preserved.

    `hit_index` is the position in `hit_paths`, which is what a label points at. Distances are
    optional per hit (text_rank and older rows carry none), so absent stays None — never 0.0.
    """
    paths = entry.get("hit_paths") or []
    dists = entry.get("hit_dists") or []
    kinds = entry.get("hit_dist_kinds") or []
    out = []
    for index, path in enumerate(paths):
        dist = dists[index] if index < len(dists) else None
        kind = kinds[index] if index < len(kinds) else None
        out.append((index, path, dist, kind))
    return out


def labeled_keys(labels, judge):
    """{(query_log_id, hit_index)} already carrying this judge's verdict."""
    return {(row.get("query_log_id"), row.get("hit_index")) for row in labels if row.get("judge") == judge}


def select_samples(
    entries,
    labels,
    judge=JUDGE_LLM,
    max_queries=5,
    max_hits=3,
    endpoint="search",
    require_judged_by=None,
):
    """Pick hits for `judge` to label: newest queries first, skipping what it already judged.

    Deterministic on purpose — no sampling randomness — so a rerun labels the next unlabelled
    hits instead of re-rolling the sample, and a resumed run cannot silently relabel.
    `max_hits` bounds work per query; the drop is reported by the caller, never hidden.

    `require_judged_by` restricts the pick to hits another judge has already ruled on. The human
    audit needs that, and needs it *here* rather than as a filter afterwards: the two selectors
    pull opposite ways. The newest queries are the ones the model has not reached yet, so taking
    the newest `max_queries` and then keeping only the model-judged ones yields nothing —
    measured 2026-08-26, 0 candidates while 24 hits sat waiting. Applying the requirement during
    the walk lets `max_queries` count the queries that can actually be audited.
    """
    seen = labeled_keys(labels, judge)
    required = labeled_keys(labels, require_judged_by) if require_judged_by else None
    samples = []
    used_queries = 0
    for entry in entries:
        if endpoint and entry.get("endpoint") != endpoint:
            continue
        if used_queries >= max_queries:
            break
        query = (entry.get("query") or "").strip()
        if not query:
            continue
        picked = 0
        for index, path, dist, kind in _hits(entry):
            if picked >= max_hits:
                break
            if (entry.get("id"), index) in seen:
                continue
            if required is not None and (entry.get("id"), index) not in required:
                continue
            samples.append(
                {
                    "query_log_id": entry.get("id"),
                    "hit_index": index,
                    "query": query,
                    "path": path,
                    "dist": dist,
                    "dist_kind": kind,
                }
            )
            picked += 1
        if picked:
            used_queries += 1
    return samples


def in_audit_band(dist):
    """True if this distance sits in the band a threshold decision would turn on."""
    if dist is None:
        return False
    low, high = AUDIT_BAND
    return low <= float(dist) <= high


def audit_candidates(samples, llm_verdicts):
    """Hits worth a person's time: the model abstained, or the distance is in the decision band.

    Everything else is where the model and any reasonable reader agree, and auditing it buys
    nothing. `llm_verdicts` maps (query_log_id, hit_index) -> verdict.
    """
    out = []
    for sample in samples:
        key = (sample["query_log_id"], sample["hit_index"])
        verdict = llm_verdicts.get(key)
        if verdict == VERDICT_UNSURE or in_audit_band(sample.get("dist")):
            out.append(sample)
    return out


def judge_prompt(query, excerpt):
    """One question, one word back. Deliberately not "is this related" — relatedness is what the
    embedding already scored; what a filter needs to know is whether reading it would have helped.
    """
    return (
        "A memory system injected the note below into a session because of the prompt below.\n"
        "Answer one question: would reading this note have helped answer that prompt?\n\n"
        f'PROMPT:\n"""\n{query.strip()}\n"""\n\n'
        f'INJECTED NOTE:\n"""\n{excerpt.strip()}\n"""\n\n'
        'Reply with JSON only: {"verdict": "relevant"|"irrelevant"|"unsure", "why": "<12 words>"}\n'
        '"relevant" = it addresses the prompt\'s subject or would inform the answer.\n'
        '"irrelevant" = a reader chasing this prompt gains nothing from it.\n'
        '"unsure" = genuinely borderline; do not use it to avoid deciding.'
    )


def parse_verdict(payload):
    """Verdict out of the model's JSON, or None when it did not answer the question asked.

    Returning None (rather than defaulting to `unsure`) keeps an unparseable answer out of the
    corpus of labels entirely — a malformed reply is a missing measurement, not an abstention.
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("verdict")
    if not isinstance(raw, str):
        return None
    verdict = raw.strip().lower()
    return verdict if verdict in VERDICTS else None


def precision(relevant, irrelevant):
    """relevant / decided, or None when the sample is under MIN_DECIDED.

    None is the point: an empty or tiny sample must not be renderable as a percentage. Callers
    print the counts instead.
    """
    decided = relevant + irrelevant
    if decided < MIN_DECIDED:
        return None
    return relevant / decided


def agreement(agreed, compared):
    """agreed / compared, or None under MIN_COMPARED (see `precision`)."""
    if compared < MIN_COMPARED:
        return None
    return agreed / compared


def llm_is_usable(agreed, compared):
    """Whether LLM labels may be reported as a precision figure at all.

    Unknown (too few audited pairs) is not permission. Until the person has audited enough hits,
    the LLM judge is an unvalidated instrument and only human labels carry a number.
    """
    rate = agreement(agreed, compared)
    return rate is not None and rate >= AGREEMENT_FLOOR


def format_report(stats):
    """Render `/recall-label-stats` as lines a weekly report can paste verbatim.

    Every line carries its sample size, and a figure that cannot be computed prints as
    "판단 보류" with the counts — never as 0%.
    """
    judges = {j.get("judge"): j for j in stats.get("judges") or []}
    agreed = int(stats.get("agreed") or 0)
    compared = int(stats.get("compared") or 0)
    lines = []
    for judge in JUDGES:
        row = judges.get(judge)
        if not row:
            lines.append(f"{judge}: 라벨 0건 — 판단 보류")
            continue
        relevant = int(row.get("relevant") or 0)
        irrelevant = int(row.get("irrelevant") or 0)
        unsure = int(row.get("unsure") or 0)
        rate = precision(relevant, irrelevant)
        decided = relevant + irrelevant
        if rate is None:
            lines.append(
                f"{judge}: precision 판단 보류 (decided {decided} < {MIN_DECIDED}, "
                f"relevant {relevant} / irrelevant {irrelevant} / unsure {unsure})"
            )
        else:
            lines.append(f"{judge}: precision {rate:.3f} (n={decided}, unsure {unsure})")
    rate = agreement(agreed, compared)
    if rate is None:
        lines.append(f"llm↔human 일치율 판단 보류 (compared {compared} < {MIN_COMPARED})")
    else:
        usable = "LLM 라벨 사용 가능" if rate >= AGREEMENT_FLOOR else "LLM 라벨 지표 제외"
        lines.append(f"llm↔human 일치율 {rate:.3f} (n={compared}) — {usable}")
    return lines

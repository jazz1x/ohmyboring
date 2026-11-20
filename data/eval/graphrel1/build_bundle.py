#!/usr/bin/env python3
"""C-GRAPHREL1 stage 1: build the 80-question multi-hop bundle (read-only).

READ-ONLY GUARD: every SQL call goes through psql with
PGOPTIONS='-c default_transaction_read_only=on'; the DSN host/db is printed
before any read. No engine (7700) / door (7710) calls: those write query logs.
Note bodies come from the vault dir on disk (BORING_VAULT_DIR, default
~/oh-my-boring/vault), the same files the engine ingests; DB `document` has no body.

Rules (judge verdict): 40 supersedes + 40 shared-claim, split design/holdout by
sha256(pair id) parity, 20/20 per half x source. supersedes: start = OLD note
(edge.dst), answer = NEW (edge.src). shared_claim: sha256("dir|"+pair id) parity
picks the start side. Filters: (a) claim carried by >HUB_MAX docs; (b) start and
answer note share a content word; (c) question shares a content word with the
answer note. Draw order = sha256(pair id) ascending, per (source, half) cell.

content word = lowercase token from [a-z0-9]+|[가-힣]+ over title+body (no
frontmatter), length >=2, containing a letter or Hangul (not pure digits), Hangul
tokens of length >=3 lose one trailing particle char from 은는이가을를의에도로와과,
not a stopword. Stopwords = fixed English list + every token in more than 2% of
all vault wiki notes (document frequency, computed here).

Usage: build_bundle.py --out data/eval/graphrel1 [--pin 2026-11-20T00:00:00Z] [--ask-cache FILE]
"""

import argparse
import concurrent.futures as cf
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

HUB_MAX = 20
PER_CELL = 20
DF_STOP = 0.02
ENGLISH_STOP = set(
    "the a an and or of to in on for with is are was were be been it this that as at by from not no but if then so we i you he she they them our your its into out up do does did has have had will can may should would could about over than also just only more most other such what which who when where how why there here all any each both one two".split()
)
VAULT = Path(os.environ.get("BORING_VAULT_DIR", "~/oh-my-boring/vault")).expanduser()
ENV = Path(__file__).resolve().parents[3] / ".env"
PSQL = "/opt/homebrew/opt/libpq/bin/psql"


def dsn():
    for cand in (
        Path(os.environ.get("BORING_ENV_FILE", "")),
        ENV,
        Path("/Users/jongyun/Development/mine/oh-my-boring/.env"),
    ):
        if cand.is_file():
            for line in cand.read_text().splitlines():
                if line.startswith("PG_DSN="):
                    return line.split("=", 1)[1].strip().strip('"')
    sys.exit("PG_DSN not found")


def sql_rows(d, q):
    env = dict(os.environ, PGOPTIONS="-c default_transaction_read_only=on")
    out = subprocess.run(
        [PSQL, d, "-v", "ON_ERROR_STOP=1", "-c", f"COPY ({q}) TO STDOUT WITH CSV"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return list(csv.reader(io.StringIO(out)))


def h(s):
    return hashlib.sha256(s.encode()).hexdigest()


def parity(s):
    return int(h(s)[:8], 16) & 1


def doc_file(doc_id):  # doc:/vault/wiki/x.md -> host path
    return VAULT / doc_id.removeprefix("doc:/vault/")


def read_note(doc_id):
    p = doc_file(doc_id)
    if not p.is_file():
        return None
    t = p.read_text(errors="replace")
    m = re.match(r"---\n(.*?)\n---\n?(.*)", t, re.S)
    fm, body = (m.group(1), m.group(2)) if m else ("", t)
    tm = re.search(r"^title:\s*(.+)$", fm, re.M)
    title = tm.group(1).strip().strip("'\"") if tm else ""
    body = body.strip()
    return {"title": title, "body": body} if body else None


TOK = re.compile(r"[a-z0-9]+|[가-힣]+")


def raw_tokens(text):
    out = set()
    for t in TOK.findall(text.lower()):
        if t.isdigit():
            continue
        if re.fullmatch(r"[가-힣]+", t) and len(t) >= 3 and t[-1] in "은는이가을를의에도로와과":
            t = t[:-1]
        if len(t) >= 2:
            out.add(t)
    return out


def build_stopwords():
    df, n = defaultdict(int), 0
    for p in (VAULT / "wiki").glob("*.md"):
        t = p.read_text(errors="replace")
        n += 1
        for w in raw_tokens(t.split("---", 2)[-1] if t.startswith("---") else t):
            df[w] += 1
    return ENGLISH_STOP | {w for w, c in df.items() if c > DF_STOP * n}, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--pin", default="2026-11-20T00:00:00Z")
    ap.add_argument("--cache", default=None, help="jsonl cache of claude -p answers (resume)")
    ap.add_argument("--max-calls", type=int, default=250)
    a = ap.parse_args()
    d = dsn()
    print(
        "DB:",
        re.sub(r"//[^@]*@", "//***@", d),
        "| read-only=on | pin first_seen_at <=",
        a.pin,
        "| vault",
        VAULT,
        flush=True,
    )
    pin = a.pin

    sup = sql_rows(
        d,
        f"select src,dst from edge where kind='supersedes' and first_seen_at<='{pin}' and src like 'doc:%' and dst like 'doc:%' and src<>dst order by 1,2",
    )
    carr = sql_rows(
        d,
        f"select dst,src from edge where kind='claims' and first_seen_at<='{pin}' and src like 'doc:%' order by 1,2",
    )
    by_claim = defaultdict(set)
    for c, s in carr:
        by_claim[c].add(s)
    stats = {"pin": pin, "supersedes_edges": len(sup)}
    hubs = {c: v for c, v in by_claim.items() if len(v) > HUB_MAX}
    shared = {c: sorted(v) for c, v in by_claim.items() if 2 <= len(v) <= HUB_MAX}
    stats["claims_total"] = len(by_claim)
    stats["claims_shared_ge2"] = len(shared) + len(hubs)
    stats["a_hub_claims_dropped"] = len(hubs)
    stats["a_hub_pairs_dropped"] = sum(len(v) * (len(v) - 1) // 2 for v in hubs.values())

    cands = {"supersedes": {}, "shared_claim": {}}
    for new, old in sup:
        pid = f"supersedes|{new}|{old}"
        cands["supersedes"][pid] = {
            "pair_id": pid,
            "edge": {"src_new": new, "dst_old": old},
            "start": old,
            "answer": new,
        }
    sup_pairs = {frozenset((n, o)) for n, o in sup}
    seen_docpair, dup_in_sup, dup_docpair = set(), 0, 0
    for c in sorted(shared):
        ds = shared[c]
        for i in range(len(ds)):
            for j in range(i + 1, len(ds)):
                key = frozenset((ds[i], ds[j]))
                if key in sup_pairs:
                    dup_in_sup += 1
                    continue
                if key in seen_docpair:
                    dup_docpair += 1
                    continue
                seen_docpair.add(key)
                pid = f"claim|{c}|{ds[i]}|{ds[j]}"
                s, t = (ds[i], ds[j]) if parity("dir|" + pid) == 0 else (ds[j], ds[i])
                cands["shared_claim"][pid] = {
                    "pair_id": pid,
                    "claim": c,
                    "docs": [ds[i], ds[j]],
                    "start": s,
                    "answer": t,
                }
    stats["shared_claim_pairs_nonhub_unique"] = len(cands["shared_claim"])
    stats["shared_pairs_dropped_also_supersedes"] = dup_in_sup
    stats["shared_pairs_dropped_duplicate_docpair"] = dup_docpair

    stop, nwiki = build_stopwords()
    stats["stopwords"] = len(stop)
    stats["vault_wiki_notes"] = nwiki
    notes, toks = {}, {}

    def tk(doc):
        if doc not in notes:
            n = read_note(doc)
            notes[doc] = n
            toks[doc] = (raw_tokens(n["title"] + "\n" + n["body"]) - stop) if n else None
        return notes[doc], toks[doc]

    survivors = {"supersedes": [], "shared_claim": []}
    for src_kind, cs in cands.items():
        miss = overlap = 0
        for c in cs.values():
            (ns, ts), (na, ta) = tk(c["start"]), tk(c["answer"])
            if ns is None or na is None:
                miss += 1
                continue
            if ts & ta:
                overlap += 1
                continue
            survivors[src_kind].append(c)
        stats[f"{src_kind}_missing_note_dropped"] = miss
        stats[f"b_{src_kind}_overlap_dropped"] = overlap
        stats[f"{src_kind}_survivors_after_ab"] = len(survivors[src_kind])
    print(json.dumps(stats, ensure_ascii=False), flush=True)

    prompt_t = (Path(__file__).parent / "question_prompt.txt").read_text()
    cache = {}
    cpath = Path(a.cache) if a.cache else None
    if cpath and cpath.is_file():
        for line in cpath.read_text().splitlines():
            r = json.loads(line)
            cache[r["pair_id"]] = r["raw"]
    calls = [0]

    def ask(c):
        if c["pair_id"] in cache:
            return cache[c["pair_id"]]
        n = notes[c["start"]]
        p = prompt_t.replace("{title}", n["title"]).replace("{body}", n["body"][:6000])
        if calls[0] >= a.max_calls:
            sys.exit("call budget exceeded")
        calls[0] += 1
        raw = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                "haiku",
                "--tools",
                "",
                "--setting-sources",
                "",
                "--strict-mcp-config",
                "--disable-slash-commands",
                "--no-session-persistence",
                "--system-prompt",
                "You write one question.",
            ],
            input=p,
            cwd="/tmp",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        cache[c["pair_id"]] = raw
        if cpath:
            with cpath.open("a") as f:
                f.write(json.dumps({"pair_id": c["pair_id"], "raw": raw}, ensure_ascii=False) + "\n")
        return raw

    def run_cell(src_kind, half):
        pool = sorted(
            (c for c in survivors[src_kind] if ("design", "holdout")[parity("half|" + c["pair_id"])] == half),
            key=lambda c: h(c["pair_id"]),
        )
        kept, drawn, dropped_c, order = [], 0, 0, []
        for c in pool:
            if len(kept) >= PER_CELL:
                break
            drawn += 1
            q = ask(c).splitlines()[0].strip() if ask(c) else ""
            order.append(c["pair_id"])
            if not q or (raw_tokens(q) - stop) & toks[c["answer"]]:  # (c)
                dropped_c += 1
                continue
            kept.append({**c, "question": q})
        return (
            (src_kind, half),
            kept,
            {"pool": len(pool), "drawn": drawn, "c_dropped": dropped_c, "draw_order": order},
        )

    cells = [(s, hf) for s in ("supersedes", "shared_claim") for hf in ("design", "holdout")]
    with cf.ThreadPoolExecutor(4) as ex:
        res = list(ex.map(lambda x: run_cell(*x), cells))
    items = {"design": [], "holdout": []}
    cellstats = {}
    for (s, hf), kept, st in res:
        cellstats[f"{hf}/{s}"] = {**st, "kept": len(kept)}
        for c in kept:
            doc_ids = [c["edge"]["src_new"], c["edge"]["dst_old"]] if s == "supersedes" else c["docs"]
            items[hf].append(
                {
                    "id": f"gr1-{hf[0]}-{'S' if s == 'supersedes' else 'C'}-{h(c['pair_id'])[:8]}",
                    "half": hf,
                    "source": s,
                    "source_pair": {
                        "pair_id": c["pair_id"],
                        **(
                            {"edge": c["edge"]}
                            if s == "supersedes"
                            else {"claim": c["claim"], "docs": c["docs"]}
                        ),
                        "documents": doc_ids,
                    },
                    "start_note": c["start"].removeprefix("doc:"),
                    "answer_note": c["answer"].removeprefix("doc:"),
                    "start_body_sha256": hashlib.sha256(notes[c["start"]]["body"].encode()).hexdigest(),
                    "answer_body_sha256": hashlib.sha256(notes[c["answer"]]["body"].encode()).hexdigest(),
                    "question": c["question"],
                }
            )
    out = Path(a.out)
    meta = {
        "stage": "C-GRAPHREL1 s1",
        "pin_first_seen_at_lte": pin,
        "stats": stats,
        "cells": {k: {x: y for x, y in v.items()} for k, v in cellstats.items()},
        "model": "haiku",
        "claude_calls_this_run": calls[0],
        "prompt_file": "question_prompt.txt",
        "content_word_definition": __doc__.split("content word = ")[1]
        .split("\n\nUsage")[0]
        .replace("\n", " "),
    }
    for hf in items:
        (out / f"{hf}.json").write_text(
            json.dumps(
                {"meta": {**meta, "half": hf}, "items": sorted(items[hf], key=lambda i: i["id"])},
                ensure_ascii=False,
                indent=1,
            )
            + "\n"
        )
    print(
        json.dumps({k: {x: y for x, y in v.items() if x != "draw_order"} for k, v in cellstats.items()}),
        "calls",
        calls[0],
    )


main()

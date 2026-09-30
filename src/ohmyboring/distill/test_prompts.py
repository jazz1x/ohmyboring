#!/usr/bin/env python3
"""증류 프롬프트 출력의 sha256 고정 — 옮기기 전후로 한 바이트도 달라지면 빨개진다.

Run: python3 src/ohmyboring/distill/test_prompts.py   (no pytest dependency)
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ohmyboring.adapters import llm as llm_adapter  # noqa: E402
from ohmyboring.distill import settings  # noqa: E402
from ohmyboring.distill.nodes import language  # noqa: E402
from ohmyboring.distill.prompts.draft import build_prompt  # noqa: E402
from ohmyboring.distill.prompts.polish import build_polish_prompt  # noqa: E402
from ohmyboring.distill.prompts.repair import build_repair_prompt  # noqa: E402
from ohmyboring.distill.prompts.sections import body_format_contract, localized_section_headers  # noqa: E402

LANGS = ("en", "ko", "ja", "auto")
RESOLUTIONS = ("compact", "evidence", "forensic", "standard")
INPUTS = (
    ("[user] hello world\n[assistant] done", "personal", "org/repo"),
    ("[user] PR #159 에서 8 checks 통과\n[assistant] 2m10s 걸림 — 日本語", "company", ""),
    ("", "", "omb"),
)
NOTE = {"title": "t", "body": "## Result\nok", "claims": [{"kind": "fact"}]}
POLISH_BODY = "2026-09-29 조사. 문(:7710) 을 재기동 — wiki-2278, `make door-up` 실행\n"
REPORTS = (
    SimpleNamespace(
        resolution="evidence",
        missing=["claims:min:3"],
        evidence_tokens_seen=("8", "2m10s"),
        evidence_tokens_kept=(),
    ),
    SimpleNamespace(
        resolution="forensic", missing=[], evidence_tokens_seen=(), evidence_tokens_kept=("pr#159",)
    ),
)


def _sha(parts):
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _correction():
    seen = []
    with (
        mock.patch.object(llm_adapter, "call_llm", lambda prompt: seen.append(prompt)),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        language.retry_language({"prompt": "P"})
    return seen[0].removeprefix("P")


def digests():
    out = {}
    for lang in LANGS:
        out[f"headers/{lang}"] = _sha([repr(sorted(localized_section_headers(lang).items()))])
        for res in RESOLUTIONS:
            out[f"contract/{lang}/{res}"] = _sha([body_format_contract(lang, res)])
            out[f"prompt/{lang}/{res}"] = _sha(
                [build_prompt(t, o, r, note_lang=lang, resolution=res) for t, o, r in INPUTS]
            )
            with mock.patch.object(settings, "NOTE_LANG", lang):
                out[f"repair/{lang}/{res}"] = _sha(
                    [
                        build_repair_prompt(t, o, r, NOTE, report, res)
                        for t, o, r in INPUTS
                        for report in REPORTS
                    ]
                )
    out["correction"] = _sha([_correction()])
    out["polish"] = _sha([build_polish_prompt(POLISH_BODY, lang) for lang in ("ko", "en", "ja")])
    out["polish-retry"] = _sha(
        [build_polish_prompt(POLISH_BODY, "ko", retry_reason="rewrite lost 1 fact(s): 7710")]
    )
    return out


EXPECTED = {
    "contract/auto/compact": "9f660ec52e3c1d7ef9c0867e8d98b39c495aed4701077864992207cedea69e80",
    "contract/auto/evidence": "76a64160395ad6bf777521f17f379f2521525478c543400feff74cd45aa08920",
    "contract/auto/forensic": "f92b47873bd318196e563d60562e0e000a4eda24c32b60ebdffbf9ec08844f2d",
    "contract/auto/standard": "03f8f0462e91e7f50a699b02cce5c12dedf9c9e06cbf1145216636bebd0f1493",
    "contract/en/compact": "9f660ec52e3c1d7ef9c0867e8d98b39c495aed4701077864992207cedea69e80",
    "contract/en/evidence": "76a64160395ad6bf777521f17f379f2521525478c543400feff74cd45aa08920",
    "contract/en/forensic": "f92b47873bd318196e563d60562e0e000a4eda24c32b60ebdffbf9ec08844f2d",
    "contract/en/standard": "03f8f0462e91e7f50a699b02cce5c12dedf9c9e06cbf1145216636bebd0f1493",
    "contract/ja/compact": "2c04927ba96a7b10cd3209c360007bb4f0abaff4c856e9942fe2d3c2e6adce97",
    "contract/ja/evidence": "961f3397140b1c681c7be41fe66b7c70d19dc49f1c7401ba5384b51f8e976ca5",
    "contract/ja/forensic": "7ab2a067fa6156d3a915eeb4630567b18c4f761b4235efbd9df37a9073d81056",
    "contract/ja/standard": "c07d4ee530b825e54266dcfb78b199c637a73fd65dc9c0d4a83e7f8bc71e886d",
    "contract/ko/compact": "7aebdd4028c012b8407802d1de6986ce7d2c2949c042c0b1337e0f4c55137fa2",
    "contract/ko/evidence": "4c13b1420db99f642b64ea68358550f46ad704b7c7991ff94bebe0e13dea197b",
    "contract/ko/forensic": "a9d51aaa6c8a7f7dbe3381343818fb949a5945a5c0998ca63cfcd22f4e96d2d2",
    "contract/ko/standard": "8048346a7cb8a4eb0ed98de8c84a7d80386a447ee809252abba4e48ed23e4da4",
    "correction": "6bae19e8f9e2e1fe3595879cd55ff7420ee2a82e8255982b2f945409a2267ac6",
    "headers/auto": "faef76c80a57a1578f4c92e1c7f660eb923cacb681af435643a1e579cb22cd93",
    "headers/en": "faef76c80a57a1578f4c92e1c7f660eb923cacb681af435643a1e579cb22cd93",
    "headers/ja": "02e3b14f14a77b7a03003c6d4e3fe22dbdfdbbe8f60754a183df2c1a8afc431b",
    "headers/ko": "90bc13be96210b7d043e0228a7ad4830ecc628101073eeb133606e080cef6b13",
    "prompt/auto/compact": "d41c159baa82b7904c94497f7f03721c4edb09b86fa7efbfafe1c959519b64d0",
    "prompt/auto/evidence": "d66afbf1b62713dca4c3fabf457fa84fc5142801f4a6ed0a047f0352187a71bc",
    "prompt/auto/forensic": "c8183ac9e4c884723402670f43c29808866da9813f7a4aa3c03d4f89da29b04a",
    "prompt/auto/standard": "ba8b57a7e30c07085955f480516031fcb69bbe07ee1d2b15f76fdfc77ab977e6",
    "prompt/en/compact": "49fb5a1f2b51494e90bc17b09a86f40247bd5e7c097e9bf71ab00af307a3b3ab",
    "prompt/en/evidence": "72c5b02c248adb25639e1bc6212ff412074c4bd135665b15b9c3fd9a41c568c6",
    "prompt/en/forensic": "3433e77f12738b64ea481f7796bd6b25c8d61a4067bff6ed60efa86cdad008e4",
    "prompt/en/standard": "bcbc294d93540a78e1cbd571580a01c3a927302459f7c288898da87ccfb36655",
    "prompt/ja/compact": "79c7b49a3bff98ef852bcb90d816a45b088755461967207802576b8830ebe48c",
    "prompt/ja/evidence": "dbb6ac882b9463e360c7c2c35931f584e5d58deaee3ae6cdac9dfe79fa3ad95d",
    "prompt/ja/forensic": "ecfadb13b4ec1dedc9a1de1560d48b22781ebd59cfadf9cccaaf49f9c752d631",
    "prompt/ja/standard": "dae689f978c2059d2e3e04e2bc50db410978bb402b8c0fdbd7574806858f9015",
    "prompt/ko/compact": "088d967cd10ae8be7bd59baa17db206a996063ccafac5c5bab849435106485b2",
    "prompt/ko/evidence": "f6b9fcf0c9b9e8c01dd5bf942474df3d0b4da7458a009a0f7ae005a51dfe9bf5",
    "prompt/ko/forensic": "63f9936508fe9f56c3ce9b0c88add812faebccd8f7b5a8637d17fc16aac88af2",
    "prompt/ko/standard": "9303d472c453c4922c0357f4c8b2fc4bd28d773ed86b987b46ecb4bf68020e7b",
    "polish": "9e1f514664d17dd1b4e6a6647ede991d8d580cdbda63766f2e6f0a5f04adb603",
    "polish-retry": "480ec59267cacde788a244728802cd4bcc6a9f395615844d76fe3ea5293609e6",
    "repair/auto/compact": "55f1626f3f1d9439cef9067ebf1e76ba3a1383fbfeb3a74e93dcb91b13ca738c",
    "repair/auto/evidence": "130327885db4fc08858b8246613cd68d20b48821cebf2c2b32aecbca31a13c31",
    "repair/auto/forensic": "7cfb8afad51be77947be4f2d21bfc7f53dd398665a4517e05438a5ec73111b5e",
    "repair/auto/standard": "5bc1e400d5e3c4fcb9abb4a7f512750ad1e66655ab33c25e9982a2dfb0e3964c",
    "repair/en/compact": "55f1626f3f1d9439cef9067ebf1e76ba3a1383fbfeb3a74e93dcb91b13ca738c",
    "repair/en/evidence": "130327885db4fc08858b8246613cd68d20b48821cebf2c2b32aecbca31a13c31",
    "repair/en/forensic": "7cfb8afad51be77947be4f2d21bfc7f53dd398665a4517e05438a5ec73111b5e",
    "repair/en/standard": "5bc1e400d5e3c4fcb9abb4a7f512750ad1e66655ab33c25e9982a2dfb0e3964c",
    "repair/ja/compact": "2ab1e79ba4841cd86bcc5b5e4bd6774cd866feb7f4d2eb7abb8317eafb334110",
    "repair/ja/evidence": "130452281e421681c57d441cd8d2d8fedf030fe0f27c4d1649d4bb05f88de3e2",
    "repair/ja/forensic": "b2698b9ea8cca0041e91ff9ff0c9c8637b81bca6897dbee348ebe92cf8d5e89c",
    "repair/ja/standard": "e41399e9e4bb312491f8211627b5a9902582a91f088d4afc63e81561a1665fe0",
    "repair/ko/compact": "f52316c8cd796cb1f1fb7b645a969c5db14d9ae4faa56fccfeecd7d75ff08d58",
    "repair/ko/evidence": "7c027c5990956b52274729c503866bf6314b9680c2c375af1dfad65c46df5f66",
    "repair/ko/forensic": "d1388cedaae0459f68107c9ac41046eda8faf2b29fcb9e0e5f4ac65c3c14e026",
    "repair/ko/standard": "5ee58069fd35981d172777fe893f3347e9ead53ab85cc89599c7dd2733977ca1",
}


class PromptPinTests(unittest.TestCase):
    def test_every_prompt_output_keeps_its_bytes(self):
        actual = digests()
        self.assertEqual(sorted(actual), sorted(EXPECTED), "the pinned set of prompt outputs changed")
        changed = sorted(k for k in EXPECTED if actual[k] != EXPECTED[k])
        self.assertEqual(changed, [], "prompt output bytes changed")

    def test_the_pins_tell_languages_and_resolutions_apart(self):
        self.assertGreater(len(set(EXPECTED.values())), 40, "pins collapsed to a few values — vacuous")


class BuildPromptTests(unittest.TestCase):
    def test_contains_json_skeleton_and_transcript(self):
        prompt = build_prompt("[user] hello world", "personal", "org/repo")
        self.assertIn('"title"', prompt)
        self.assertIn('"claims"', prompt)
        self.assertIn("=== SESSION TRANSCRIPT ===", prompt)
        self.assertIn("[user] hello world", prompt)

    def test_repo_and_origin_hints(self):
        with_repo = build_prompt("t", "company", "org/repo")
        self.assertIn("repo='org/repo'", with_repo)
        self.assertIn("origin='company'", with_repo)
        no_repo = build_prompt("t", "personal", "")
        self.assertNotIn("repo='", no_repo)
        self.assertIn("origin='personal'", no_repo)

    def test_skip_contract_present(self):
        # The prompt must teach the {"skip": true} escape hatch that distill_and_remember honors.
        self.assertIn('"skip": true', build_prompt("t", "personal", ""))


class ClaimExamplesTeachStatementsTests(unittest.TestCase):
    """The prompt's own examples are the strongest instruction in it.

    Measured 2026-09-20: 68% of the corpus's 10,267 current claims hold a value under 25
    characters or a predicate that restates the kind — and four of the five examples this prompt
    shipped were exactly that shape (`"removed"`, `"0.1.3"`, `"bedrock-converse"`). The model was
    not ignoring the instructions; it was copying them.
    """

    def _claim_examples(self):
        prompt = build_prompt("transcript", "personal", "omb")
        start = prompt.index("Examples:")
        end = prompt.index("Counter-examples", start)
        return [
            json.loads(line.strip())
            for line in prompt[start:end].splitlines()
            if line.strip().startswith("{")
        ]

    def test_every_example_value_reads_as_a_statement(self):
        examples = self._claim_examples()
        self.assertGreaterEqual(len(examples), 4, "the examples went missing from the prompt")
        short = [c["value"] for c in examples if len(c["value"]) < 25]
        self.assertEqual(short, [], f"example values shorter than the corpus threshold teach tags: {short}")

    def test_no_example_predicate_restates_its_kind(self):
        tautological = {"incident", "status", "decision", "state", "next-step", "action"}
        offenders = [
            (c["predicate"], c["kind"])
            for c in self._claim_examples()
            if c["predicate"].lower() in tautological
        ]
        self.assertEqual(offenders, [], f"a predicate that restates the kind says nothing: {offenders}")

    def test_the_prompt_states_the_rule_and_not_only_the_examples(self):
        """Examples alone drift when someone edits one; the rule survives an edit."""
        prompt = build_prompt("transcript", "personal", "omb")
        self.assertIn("READ AS A STATEMENT", prompt)
        self.assertIn("NAMES THE ASPECT", prompt)


if __name__ == "__main__":
    if "--print" in sys.argv:
        for key, value in sorted(digests().items()):
            print(f'    "{key}": "{value}",')
    else:
        unittest.main(verbosity=2)

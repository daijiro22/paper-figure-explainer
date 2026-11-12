"""結論の段（pfe/conclusion.py）。API は呼ばない。"""
from pfe import conclusion as C


def _text(paras, secs):
    return {"sections": [{"id": k, "title": v} for k, v in secs.items()],
            "paragraphs": [{"id": f"p{i}", "section": s, "text": t} for i, (s, t) in enumerate(paras)]}


def test_source_prefers_conclusion_section_and_drops_boilerplate():
    body = "Results show the peak at 350 °C. " * 20
    conc = "In conclusion, delignification raised the yield to 42 %. " * 6
    t = _text([("r", body), ("c", conc), (None, "Acknowledgments: We thank the lab.")],
              {"r": "Results", "c": "4. Conclusions"})
    src, where = C.conclusion_source(t)
    assert where == "結論の節" and src.startswith("In conclusion") and "Acknowledg" not in src


def test_source_falls_back_to_tail():
    t = _text([("r", "x" * 100), ("r", "y" * 300), (None, "Funding: none.")], {"r": "Results"})
    src, where = C.conclusion_source(t)
    assert where == "本文の終わり" and src.endswith("y" * 300) and "Funding" not in src


def test_check_flags_fake_quote_number_and_unknown_fig():
    body = "In conclusion, delignification raised the yield to 42 % in all samples."
    ok = {"steps": [{"say": "収率が 42 % に上がりました。", "caption_html": "収率 42 %", "point": None,
                     "figs": ["fig1"], "evidence": [{"source": "text", "quote": "delignification raised the yield to 42 %"}]}]}
    assert C.check(ok, body, ["fig1"]) == []
    bad = {"steps": [{"say": "収率が 55 % に上がりました。", "caption_html": "", "point": None,
                      "figs": ["fig9"], "evidence": [{"source": "text", "quote": "this sentence is not in the body at all"}]}]}
    errs = C.check(bad, body, ["fig1"])
    assert any("本文に見つからない" in e for e in errs) and any("55" in e for e in errs) and any("fig9" in e for e in errs)

# ── eval harness: grade the agent against eval sets (Pillar 1) ──────────────
# Eval sets are runtime-editable from the GUI's "Evals -> Edit Eval Sets..."
# menu. Defaults come from the canvas Eval node(s) (config['evals']) or the
# evals/*.jsonl files; GUI edits persist to evals.json and override the
# defaults — so an Eval node may be left empty in the canvas and filled in here.
EVALSETS_PATH = os.path.join(BASE_DIR, "evals.json")


def _default_eval_sets() -> list:
    sets = [dict(s) for s in (CONFIG.get("evals", []) or [])]
    if sets:
        return sets
    for rel in ("evals/evalset.jsonl", "evals/evalset.example.jsonl"):
        path = os.path.join(BASE_DIR, rel)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                cases = [json.loads(line) for line in f if line.strip()]
            return [{"name": "evalset", "target": None, "cases": cases}]
    return []


def _load_eval_sets() -> list:
    try:
        with open(EVALSETS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return _default_eval_sets()


EVAL_SETS = _load_eval_sets()


def save_eval_sets() -> None:
    try:
        with open(EVALSETS_PATH, "w", encoding="utf-8") as f:
            json.dump(EVAL_SETS, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def eval_targets() -> list:
    """Agent names a set may target (a set with target None tests the whole
    pipeline; single-agent builds have no sub-targets)."""
    return list(PIPELINE) if "PIPELINE" in globals() else []


def add_eval_set(name: str, target=None) -> None:
    EVAL_SETS.append({"name": (name or "").strip() or f"set{len(EVAL_SETS) + 1}",
                      "target": target or None, "cases": []})
    save_eval_sets()


def update_eval_set(index: int, name: str, target=None) -> None:
    if 0 <= index < len(EVAL_SETS):
        EVAL_SETS[index]["name"] = (name or "").strip() or EVAL_SETS[index]["name"]
        EVAL_SETS[index]["target"] = target or None
        save_eval_sets()


def remove_eval_set(index: int) -> None:
    if 0 <= index < len(EVAL_SETS):
        EVAL_SETS.pop(index)
        save_eval_sets()


def add_eval_case(set_index: int, case: dict) -> None:
    if 0 <= set_index < len(EVAL_SETS):
        EVAL_SETS[set_index].setdefault("cases", []).append(case)
        save_eval_sets()


def update_eval_case(set_index: int, case_index: int, case: dict) -> None:
    if 0 <= set_index < len(EVAL_SETS):
        cases = EVAL_SETS[set_index].setdefault("cases", [])
        if 0 <= case_index < len(cases):
            cases[case_index] = case
            save_eval_sets()


def remove_eval_case(set_index: int, case_index: int) -> None:
    if 0 <= set_index < len(EVAL_SETS):
        cases = EVAL_SETS[set_index].setdefault("cases", [])
        if 0 <= case_index < len(cases):
            cases.pop(case_index)
            save_eval_sets()


def _eval_run_input(target, task):
    """Run one case: the whole pipeline (target None) or one agent in isolation."""
    if target and "AGENTS" in globals() and target in AGENTS:
        return react(target, task, lambda s: None)
    return run(task, emit=lambda s: None)


def eval_judge(target, task, answer, criterion) -> bool:
    """LLM grader for free-form answers: does `answer` satisfy `criterion`?"""
    system = "You are a strict grader. Reply with ONLY 'YES' or 'NO'."
    prompt = (f"Criterion: {criterion}\n\nTask:\n{task}\n\nAnswer:\n"
              f"{answer}\n\nDoes the answer satisfy the criterion? "
              "Reply YES or NO.")
    try:
        if "PIPELINE" in globals():
            name = (target if (target and "AGENTS" in globals()
                               and target in AGENTS) else PIPELINE[0])
            text, _ = llm(name, system,
                          [{"role": "user", "content": prompt}], lambda s: None)
        else:
            text, _ = _llm_once(system, [{"role": "user", "content": prompt}])
    except Exception:
        return False
    return (text or "").strip().upper().startswith("Y")


# ── grader registry (dependency-free; stdlib only) ──────────────────────────
# A case is graded by one or more typed assertions. Modern form:
#   {"type": "contains", "value": "...", "case_sensitive": false}      # single
#   {"checks": [{...}, {...}], "match": "all"|"any"}                   # multiple
# Legacy form (still works, same precedence as before): expected_output /
# expected_regex / judge. Borrows the assertion taxonomy of promptfoo /
# LangChain / DeepEval, minus checks that need heavy deps (embeddings/BLEU/ROUGE).
def _g_norm(s, a):
    s = str(s).strip()
    return s if a.get("case_sensitive") else s.lower()


def _g_list(value):
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [p.strip() for p in re.split(r"[,\n]", str(value or "")) if p.strip()]


def _g_num(s):
    m = re.search(r"-?\d+(?:\.\d+)?", str(s))
    return float(m.group()) if m else None


def _g_json(ans):
    try:
        return json.loads(ans)
    except Exception:
        pass
    m = re.search(r"\{.*\}|\[.*\]", ans or "", re.DOTALL)   # first JSON-ish blob
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            return None
    return None


def _g_contains(ans, a, ctx):
    v = a.get("value", "")
    return bool(str(v).strip()) and _g_norm(v, a) in _g_norm(ans, a)


def _g_similar(ans, a, ctx):
    import difflib
    thr = float(a.get("threshold", 0.8) or 0.8)
    return difflib.SequenceMatcher(
        None, _g_norm(ans, a), _g_norm(a.get("value", ""), a)).ratio() >= thr


def _g_numeric(ans, a, ctx):
    got, want = _g_num(ans), _g_num(a.get("value"))
    if got is None or want is None:
        return False
    return abs(got - want) <= float(a.get("tolerance", 0) or 0)


def _g_length(ans, a, ctx):
    n = len(ans or "")
    lo, hi = a.get("min"), a.get("max")
    return ((lo is None or n >= int(lo)) and (hi is None or n <= int(hi)))


def _g_regex(ans, a, ctx):
    flags = 0 if a.get("case_sensitive") else re.IGNORECASE
    try:
        return bool(re.search(a.get("value", ""), ans or "", flags))
    except re.error:
        return False


_GRADERS = {
    "equals": lambda ans, a, ctx: _g_norm(ans, a) == _g_norm(a.get("value", ""), a),
    "contains": _g_contains,
    "icontains": _g_contains,
    "not_contains": lambda ans, a, ctx: not _g_contains(ans, a, ctx),
    "contains_all": lambda ans, a, ctx: bool(_g_list(a.get("value")))
        and all(_g_norm(x, a) in _g_norm(ans, a) for x in _g_list(a.get("value"))),
    "contains_any": lambda ans, a, ctx:
        any(_g_norm(x, a) in _g_norm(ans, a) for x in _g_list(a.get("value"))),
    "starts_with": lambda ans, a, ctx: _g_norm(ans, a).startswith(_g_norm(a.get("value", ""), a)),
    "ends_with": lambda ans, a, ctx: _g_norm(ans, a).endswith(_g_norm(a.get("value", ""), a)),
    "regex": _g_regex,
    "not_regex": lambda ans, a, ctx: not _g_regex(ans, a, ctx),
    "is_json": lambda ans, a, ctx: _g_json(ans) is not None,
    "json_has_keys": lambda ans, a, ctx: isinstance(_g_json(ans), dict)
        and all(k in _g_json(ans) for k in _g_list(a.get("value"))),
    "numeric": _g_numeric,
    "close": _g_numeric,
    "similar": _g_similar,
    "length": _g_length,
    "llm_rubric": lambda ans, a, ctx: eval_judge(
        ctx.get("target"), ctx.get("task", ""), ans, a.get("value", "")),
}
# friendly aliases
_GRADERS["exact"] = _GRADERS["equals"]
_GRADERS["judge"] = _GRADERS["llm_rubric"]
_GRADERS["rubric"] = _GRADERS["llm_rubric"]


def _run_assertion(ans, a, ctx) -> bool:
    fn = _GRADERS.get(str(a.get("type", "")).strip().lower())
    if fn is None:
        return False
    try:
        ok = bool(fn(ans, a, ctx))
    except Exception:
        ok = False
    return (not ok) if a.get("not") else ok        # per-assertion negation


def _case_assertions(case) -> list:
    """Normalize a case into a list of typed assertions. Legacy single-field
    cases keep their exact old precedence (judge > regex > contains = one
    assertion)."""
    if isinstance(case.get("checks"), list):
        return [c for c in case["checks"] if isinstance(c, dict) and c.get("type")]
    if case.get("type"):
        return [{k: v for k, v in case.items() if k not in ("id", "input")}]
    if case.get("judge"):
        return [{"type": "llm_rubric", "value": case["judge"]}]
    if case.get("expected_regex"):
        return [{"type": "regex", "value": case["expected_regex"]}]
    if case.get("expected_output"):
        return [{"type": "contains", "value": case["expected_output"]}]
    return []


def grade(case, answer, target=None) -> bool:
    ans = answer or ""
    assertions = _case_assertions(case)
    if not assertions:
        return False                          # no expectation declared = fail
    ctx = {"task": case.get("input", ""), "target": target}
    results = [_run_assertion(ans, a, ctx) for a in assertions]
    return (any(results) if str(case.get("match", "all")).lower() == "any"
            else all(results))


def run_evals(emit=print) -> list:
    """Run every eval set; returns [{name,target,passed,total,score}]. Disables
    history persistence and auto-approves HITL so it runs unattended."""
    global save_history, SUMMARY, record_turn
    _orig_save = save_history
    save_history = lambda: None
    # record_turn is the append+persist path run() uses now — it bypasses
    # save_history, so neutralize it too or evals would pollute the conversation.
    _orig_record = record_turn
    record_turn = lambda task, result: None
    _orig_hist = list(HISTORY) if "HISTORY" in globals() else None
    _orig_summary = SUMMARY if "SUMMARY" in globals() else None
    if "SUMMARY" in globals():
        SUMMARY = ""                      # isolate eval from any stored summary
    if "set_confirm_handler" in globals():
        set_confirm_handler(lambda tool_name, args: True)   # eval auto-allows
    if "set_review_handler" in globals():
        set_review_handler(lambda prompt, content: {
            "decision": "approve", "content": content, "feedback": ""})
    if "_CANCEL" in globals():
        _CANCEL.clear()
    results = []
    try:
        sets = EVAL_SETS
        if not sets:
            emit("[eval] no eval sets — add one via 'Evals -> Edit Eval "
                 "Sets...', an Eval node, or evals/evalset.jsonl")
            return results
        for s in sets:
            target = s.get("target")
            cases = [c for c in (s.get("cases", []) or [])
                     if (c.get("input") or "").strip()]
            emit(f"[eval] {s['name']} \u2192 {target or 'whole pipeline'} "
                 f"({len(cases)} case(s))")
            if not cases:
                emit("  (no cases yet — add some via 'Edit Eval Sets...')")
                results.append({"name": s["name"], "target": target,
                                "passed": 0, "total": 0, "score": 0.0})
                continue
            passed = 0
            for c in cases:
                if "HISTORY" in globals():
                    HISTORY[:] = []
                try:
                    ans = _eval_run_input(target, c.get("input", ""))
                except Exception as e:
                    ans = f"[crash] {type(e).__name__}: {e}"
                ok = grade(c, ans, target)
                passed += int(ok)
                # Show the FULL response (grading already uses it in full); a
                # truncated preview here hid why a case passed/failed.
                shown = (ans or "").strip() or "(empty response)"
                emit(f"  [{'PASS' if ok else 'FAIL'}] {c.get('id', '?')}: {shown}")
            score = passed / len(cases) if cases else 0.0
            emit(f"[eval] {s['name']}: {passed}/{len(cases)} = {score:.2f}")
            results.append({"name": s["name"], "target": target,
                            "passed": passed, "total": len(cases),
                            "score": score})
    finally:
        save_history = _orig_save
        record_turn = _orig_record
        if _orig_hist is not None:        # never pollute the live conversation
            HISTORY[:] = _orig_hist
        if _orig_summary is not None:     # ...nor the rolling summary
            SUMMARY = _orig_summary
    return results

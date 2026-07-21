"""Resume-voice writing rules, shared by every prompt that drafts
resume-facing text (weekly profile consolidation, LaTeX resume editor).

Distilled 2026-07-09 from Google's XYZ formula (Laszlo Bock), Harvard/MIT
career-center guidance, CtCI (Gayle McDowell), and tech-recruiter sources —
full rules, sources, and before/after examples in
skills/resume-writing-voice/SKILL.md.
"""

RESUME_VOICE_RULES = """\
Resume-voice rules for every highlight/bullet you write:
- Formula: "Accomplished X, as measured by Y, by doing Z" — result first, opened with a
  precise past-tense action verb (Designed / Led / Built / Optimized / Reduced / Shipped);
  the technology is supporting detail, never the subject of the sentence.
- A bullet must state what changed because of the owner. One that ends at the activity
  ("built a pipeline") is unfinished.
- Quantify: prefer before→after deltas ("p95 420ms → 170ms") over bare percentages; with no
  clean output metric, quantify scope/inputs (services, QPS, models supported, teams, data
  volume) or adoption (dependent projects, teams switched). Never invent precision — honest
  ranges or qualitative deltas ("days → hours") beat a fabricated number.
- Performance numbers carry workload context (the QPS / model size / fleet at which they held).
- One idea per bullet, 1–2 lines, 3–5 bullets per entry, ordered by impact — cut the weakest
  bullet rather than pad.
- Banned openers: "responsible for", "helped with", "worked on", "contributed to". No
  personality buzzwords ("results-driven", "innovative"). Exact canonical tech names
  ("Kubernetes", not "K8s").
- Open source: name the role precisely (Founding contributor / Maintainer / Core contributor /
  Reviewer) and treat maintainership as leadership — owning direction, reviewing, releasing;
  cite specific PRs/RFCs inline.
- Senior-level bullets prove scope, judgment, and influence (systems owned, architectural
  trade-offs, RFCs driven, mentees and their outcomes) — not task execution."""

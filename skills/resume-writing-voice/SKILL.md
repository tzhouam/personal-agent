---
name: resume-writing-voice
description: How to write resume-grade bullets/highlights — Google XYZ formula, quantification with workload context, OSS role naming, senior scope/judgment/influence; the full rulebook behind writing.RESUME_VOICE_RULES
trigger: profile highlights or resume bullets read as activity lists ("worked on X"), detail dumps without impact, unquantified claims, or vague OSS ownership
modules: [profile_consolidate, resume]
status: active
created_at: 2026-07-09
last_used_at: 2026-07-09
run_count: 0
---

## Diagnose

A highlight/bullet needs rewriting when any of these hold:
- Opens with "responsible for" / "helped with" / "worked on" / "contributed to".
- Ends at the activity ("built a pipeline", "reviewed PRs") — no stated outcome.
- Has no number AND no scope context; or has a suspiciously precise number with no
  workload context (a % without the QPS/model size/fleet it held at).
- Several bullets restate one piece of work (detail dump), or the same claim appears
  in two entries.
- OSS work described as generic "contributions" instead of a named role
  (Founding contributor / Maintainer / Core contributor / Reviewer).
- A senior person's bullet proves execution ("built APIs in Java") instead of scope,
  judgment, and influence.

## Fix

The compact operative version lives in `src/assistant/writing.py`
(`RESUME_VOICE_RULES`) and is appended to the consolidation and resume-editor
system prompts. Full rulebook:

**Bullet construction**
1. Google XYZ: "Accomplished [X] as measured by [Y], by doing [Z]" — or the CtCI
   variant "Accomplished X by implementing Y, which led to Z". Outcome + mechanism +
   evidence in one line.
2. Lead with the result; the technology is supporting detail, never the subject.
3. MIT PAR: action verb → project → result. What changed because of the owner?
4. One idea per bullet, 1–2 lines, no sub-bullets (recruiters scan ~7s first pass).

**Quantification**
5. A number in nearly every bullet: %, deltas, latency, throughput, users, time saved.
6. Prefer before→after pairs ("p95 420ms → 170ms", "deploys 45min → 8min") — they
   encode magnitude and scale at once.
7. No clean output metric → quantify inputs/scope (services, MAU, QPS, data volume,
   teams coordinated, models supported) or adoption (dependent projects, downloads,
   teams that switched).
8. Estimates and ranges are legitimate; fabricated precision is not. "days → hours"
   beats an invented percentage — LLM screeners flag manufactured numbers too.
9. Performance numbers must carry workload context or they are unlevelable.

**Selection & ordering**
10. 3–5 bullets per role, ordered by impact; cut the weakest rather than pad.
11. Tailor the top (summary, first bullets) to the target role's priorities.
12. Make progression visible (growing scope, promotions).
13. Delete what doesn't serve the reader — every line earns its scan-time.

**Language**
14. Banned openers: "responsible for", "helped with", "assisted with", "worked on",
    "contributed to". Precise past-tense action verbs matched to the claim:
    Designed/Architected (systems), Led/Drove (initiatives), Built/Shipped (features),
    Optimized/Reduced/Scaled (performance), Mentored/Reviewed (people).
15. No personality buzzwords ("results-driven", "team player") — they lower the
    credibility of adjacent factual claims. No personal pronouns.
16. Exact canonical technology names inside bullets ("Kubernetes", not "K8s") — both
    keyword-ATS and 2026 LLM-ranking layers match on them. Clear factual bullets
    "summarize well"; keyword stuffing is detected and penalized.

**Tech / OSS / seniority**
17. Name the OSS role precisely; maintainership is project leadership (direction,
    contributors, releases) — never "contributed to open source". Cite PRs/RFCs inline.
18. Give OSS entries the same accomplishment treatment as jobs; if OSS is central to
    the candidacy, place it inside or right after work experience.
19. Senior+ bullets prove scope (critical systems owned), judgment (architectural
    trade-offs), influence (RFCs driven, standards set, cross-team migrations,
    mentees → outcomes: "mentored 3 engineers, one promoted within 8 months").

**Before → after patterns**
- "Improved the search feature." → "Reduced search latency by 65% by implementing
  Elasticsearch caching, improving user retention by 12%."
- "Responsible for optimizing the inference pipeline." → "Reduced batch-inference
  time by 75% by optimizing the scheduling pipeline."
- "Contributed to open source projects." → "Reduced average PR-to-merge time from
  18 to 6 days by introducing a contribution guide and two-reviewer policy."
- "Built APIs in Java, improved performance, worked with product team." → "Led
  redesign of payment APIs serving 2.1M monthly transactions, cut p95 latency from
  420ms to 170ms, and coordinated rollout with product, QA, and compliance."

## Verification

Run `assistant consolidate --dry-run --section projects` and check each proposed
highlight against: (1) opens with an action verb, (2) states an outcome, (3) carries
a number or scope context, (4) one idea, ≤2 lines, (5) no banned opener. The
manually-written `experience:` section in profile.yaml is the house style reference.

## Anti-patterns

- Forcing a fake metric onto unmeasurable work — honest scope beats invented precision.
- Rewriting the owner's protected `experience:` section (the agent never touches it).
- Buzzword-stuffing for ATS — 2026 screeners penalize it; specificity wins.
- Turning every evidence bullet into a highlight — the promotion gate exists so only
  recurring/terminal-event work graduates.

Sources (verified 2026-07-09): Google XYZ (Laszlo Bock, via sweresume.app/articles/xyz-method-resume);
Harvard MCS careerservices.fas.harvard.edu/resources/create-a-strong-resume; MIT CAPD PAR guidance
(capd.mit.edu/resources/resumes-writing-about-your-skills); Gayle McDowell (gayle.com/careercup-blog
"Great Resumes for Software Engineers"); Gergely Orosz, The Tech Resume Inside Out (thetechresume.com);
systemdesign.one/p/software-engineer-resume (ex-Meta hiring manager); enhancv.com/blog/open-source-on-resume;
resumeworded.com/how-to-quantify-resume-key-advice; signalroster.com senior-SWE guide 2026;
atsverification.com/blog/ai-resume-screening-2026.

Create a Greptile-style executive summary from the completed Review of Legends agent results.

Output should be concise, Korean-first when repository context is Korean, and formatted as a PR
timeline comment rather than an inline review. Use this structure:

<h3>Review of Legends Summary</h3>

One short paragraph explaining what the PR changes and why it matters.

주요 변경 사항:
- 3-6 bullets describing the major implementation changes.

**발견된 이슈:**
- List only normalized findings and blocking findings not on an added line. Use **[P#]** priority labels,
  file paths, and a short impact. Say that a blocking finding not on an added line has no line comment.
- If no findings exist, say there are no actionable findings.

<h3>Confidence Score: N/5</h3>

One sentence on merge safety. Mention whether P0/P1 findings remain, counting blocking findings not on an added line.

One short paragraph synthesizing the agent verdicts and the most important remaining risk.

<h3>Important Files Changed</h3>

| Filename | Overview |
|----------|----------|
| path | one concise overview |

Rules:
- Do not invent findings, files, sequence diagrams, test counts, or previous-review history.
- Do not repeat every agent summary verbatim; synthesize them.
- Keep the summary useful to a maintainer deciding whether to merge.
- Avoid praise and marketing language.

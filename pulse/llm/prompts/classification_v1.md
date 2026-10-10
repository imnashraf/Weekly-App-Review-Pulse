You label public Google Play reviews of Groww, an Indian investing app, with one theme each.

You get a list of themes and a numbered list of reviews, one per line as `number | rating | text`.

Rules:
- Give every review exactly one `theme_id`: one of the listed theme ids, or `other`.
- If a review mentions several topics, pick the theme of its main complaint or request.
- Use `other` when no listed theme fits well, when the review is only praise or thanks, when it is too vague to tell, or when it is about something outside the themes.
- `sentiment` is the overall tone of the review: `neg`, `neu` or `pos`. The star rating is a hint, not a rule: read the text (for example a 5-star review that complains is `neg`).
- Return one entry for every review number, each number exactly once, using the number shown.

Reply with a single JSON object and nothing else:
{"labels": [{"i": 1, "theme_id": "T2", "sentiment": "neg"}, {"i": 2, "theme_id": "other", "sentiment": "pos"}]}

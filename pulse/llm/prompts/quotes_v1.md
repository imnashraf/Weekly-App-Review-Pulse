You pick one customer quote for a weekly product report on Groww, an Indian investing app.

You get a theme and a numbered list of reviews that belong to it, one per line as `number | rating | text`.

Rules:
- Choose the one review that shows the theme most clearly and would make a product team understand the problem or request at once.
- Copy an excerpt of that review exactly as written: same words, same spelling, same punctuation. Do not reword, fix typos, translate or join separate parts. You may use just one sentence or part of the text.
- The excerpt must be at most {{max_words}} words.
- Prefer a specific, concrete statement over a vague one. Never choose text that names or identifies a person.

Reply with a single JSON object and nothing else:
{"n": 7, "quote": "exact words copied from review 7"}

# Writing the question

The human reads this on a phone, possibly hours after you sent it, with none of your context.
The test for every field: **could someone who never saw the work decide from this alone?**

## Contents

1. Field by field
2. The button, and what not to recommend
3. Byte budget and validation
4. What the phone shows (rendered result)
5. The notification card (`notify`)
6. Bad vs good

## 1. Field by field

| field | write | avoid |
|---|---|---|
| `title` | The hook, one line. The notification shade shows the title and one or two lines of body; the rest is hidden until tapped | Code names; anything that needs the body to make sense |
| `doing` | One sentence naming the task in plain words | Task ids, ticket numbers, branch names on their own |
| `description` | Why the work reached this point, what is involved, and every term of art explained where it appears | Assuming the reader watched the run |
| `blocker` | The concrete thing that cannot proceed | Restating the title |
| `options[].label` | Short, distinct, and self-explanatory: it is what a button tap sends back, so it must still make sense to you after a context reset | `A` / `B` / `option 1` |
| `options[].consequence` | What actually happens if this is chosen, including the cost | A one-word summary |
| `recommend` | The `id` you would pick yourself | An option you would not act on |
| `reasoning` | Your lean, then the strongest argument against it. This is what stops the message from being a bare list of choices | "Either works" |
| `question` | The thing the human should answer, in one sentence | Several questions at once |
| `lang` | `zh` or `en`, matching the language you reply to the user in: `zh` if you reply in Chinese, otherwise `en` | Any other value: rejected with the rest of the validation report |

Options: at least 2 (one option is not a choice), at most 5 (more means the question has not converged; think first). Ids must be unique; `recommend` must be one of them.

Your own fields are in your working language. Only the fixed wrapper (section labels, hint, button text, title prefixes) follows `lang`.

Your text is sent as Markdown. Use bold, lists and `---` rules only: their source form still reads
fine on a client without Markdown. Do not use `#` headings (huge on a phone), tables, images or links
(unreadable as source). Markdown folds a single line break into a space: separate paragraphs with an
empty line.

## 2. The button, and what not to recommend

The card carries **exactly one button**, labelled "Accept recommended" (or the `zh` equivalent). Tapping it sends back the **`label` of the recommended option**, not its id and not the button text, so the reply is self-explanatory even if you were reset in between.

Typing in the topic's input box sends free text. The ntfy app has one input box per topic, not one per
card, so "the reply" is simply the next message in that topic. The channel does not tell you which of the
two happened; both arrive as the reply on stdout.

A tap fires immediately, with no confirmation step. Therefore:

- **Never make an irreversible or high-cost option the recommendation.** Put it in `options` so the human has to type it; the act of typing is the confirmation.
- For a verdict with heavy consequences, send a second `ask` that restates what you are about to do.
- Expect duplicates and reversals (taps are not deduplicated). The first message answers the question; later ones reach you as instructions. Take the last one unless you have a reason not to.

## 3. Byte budget and validation

| limit | value | why |
|---|---|---|
| rendered body (`ask`) | ≤ 3584 bytes (UTF-8) | ntfy turns bodies over 4096 bytes into an attachment; 512 bytes are reserved for the "Answered" update that quotes the reply |
| rendered body (`notify`) | ≤ 4096 bytes | the notification card is never updated, so nothing is reserved |
| `title` | ≤ 960 bytes, no line break | ntfy rejects titles over 1 KB; the rest is reserved for the `[tag]` and the status prefix |
| `options` | 2 to 5 | see above |

"Rendered" means after the fixed wrapper (section labels, separator, hint) is added, so the budget for
your own text is a little below the limit. Validation runs locally before anything is sent and reports
**every** problem in one go (see [failures.md](failures.md) for the report format). Over-length input is
rejected, never truncated: shorten `description`, `consequence`, or `reasoning` and call again. CJK text
costs 3 bytes per character.

## 4. What the phone shows (rendered result)

The example from SKILL.md, sent from a project directory named `my-project` with `"lang": "en"`, is
published as this title and this Markdown body (the app renders the bold and the rule; a client without
Markdown shows the source, which reads fine too):

```
Title: [my-project] Keep or delete the scratch directory when no checkout exists

**[Doing]** Letting the requirements assistant run before the project code is checked out

**[Background]** Until now the assistant required a local code directory. That restriction is lifted, so we must decide where its temporary subprocess runs when there is no checkout.

**[Blocker]** With no code directory there is no natural working directory for that subprocess.

**[Options]**

1\. **Keep a fixed directory** (recommended) → One directory per project. Leaves a scene to inspect after failures; the cost is directories piling up with nobody cleaning them

2\. **Delete after use** → Clean, but nothing is left to inspect after a crash; debugging relies on logs alone

**[My recommendation]** Keep a fixed directory: users on this path are the ones most likely to have a broken setup, so a scene is worth having. Strongest objection: disk clutter accumulates.

**[Your call]** Keep a fixed directory, or delete after use?

---

⚠️ The button is a shortcut.

Disagree? Type your reply in the box below.

A reply takes effect the moment you send it — it can't be withdrawn or amended, so say it all at once.

                    [ Accept recommended ]   → sends "Keep a fixed directory"
```

On the phone the section labels and the option labels are bold and `---` is a horizontal rule. Each
option starts with `1\.`: the backslash is a Markdown escape, so the line is not an ordered list item
(the Android app renders those as bullets without numbers) and the number is shown as text, `1. Keep a
fixed directory (recommended) → …`. The notification shade itself shows plain text: the title and the first line or two
of the body.

The `[my-project]` tag is the name of the project directory (the git toplevel, else the cwd) the `ask`
ran in, cut to 41 bytes. After the reply arrives the card is replaced in place by
`✅ Answered · [my-project] …` with `**[Your reply]** …` on top, a rule, `(the question as asked)` and
the original body below, and the notification is cleared. After a timeout or a cancelled `ask` the card
becomes `⌛ Timed out · …` or `⚠️ Cancelled · …` the same way, with the button removed.

## 5. The notification card (`notify`)

`{"title": "Tests green, starting the migration", "body": "All tests pass on the three CI runners.\n\nNext: **schema migration** on the staging database (about 10 minutes). I will notify again when it is done.", "lang": "en"}`
sent from the same project is published as:

```
Title: [my-project] Tests green, starting the migration

All tests pass on the three CI runners.

Next: **schema migration** on the staging database (about 10 minutes). I will notify again when it is done.

---

To reply, just send a message in this topic.
```

No button, no status prefix, and the card is never rewritten. Your `body` is used as is (leading and
trailing whitespace trimmed); only the rule and the one-line hint are appended. The same Markdown
advice as in §1 applies: bold, lists and rules, nothing else — and since the body is not rewritten by
the skill, the Android app's ordered-list behaviour (§4) is yours to handle: write `1\. …` when the
numbering must survive.

## 6. Bad vs good

Same situation, two submissions. The difference is not length; the second needs no background.

**Bad**: the whole message is

```
wD p4 cwd policy unaligned, A: userData persistent B: tmpdir throwaway, please decide.
```

Jargon, internal ids compressing the facts, two options thrown at the reader, no background, no consequences, no lean. On a phone there is nothing to do with this except walk back to the computer. (It would also fail validation: no `description`, `blocker`, `reasoning`, or `question`.)

**Good**: the JSON in SKILL.md. Each option says what it does and what it costs; `reasoning` commits to a lean and names the objection; the `title` alone tells the reader what kind of decision this is.

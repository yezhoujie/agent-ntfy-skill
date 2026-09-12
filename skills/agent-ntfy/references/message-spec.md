# Writing the question

The human reads this on a phone, possibly hours after you sent it, with none of your context.
The test for every field: **could someone who never saw the work decide from this alone?**

## Contents

1. Field by field
2. The button, and what not to recommend
3. Byte budget and validation
4. What the phone shows (rendered result)
5. Bad vs good

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

## 2. The button, and what not to recommend

The card carries **exactly one button**, labelled "Accept recommended" (or the `zh` equivalent). Tapping it sends back the **`label` of the recommended option**, not its id and not the button text, so the reply is self-explanatory even if you were reset in between.

Typing in the app's reply box sends free text. The channel does not tell you which of the two happened; both arrive as the reply on stdout.

A tap fires immediately, with no confirmation step. Therefore:

- **Never make an irreversible or high-cost option the recommendation.** Put it in `options` so the human has to type it; the act of typing is the confirmation.
- For a verdict with heavy consequences, send a second `ask` that restates what you are about to do.
- Expect duplicates and reversals (taps are not deduplicated). The first message answers the question; later ones reach you as instructions. Take the last one unless you have a reason not to.

## 3. Byte budget and validation

| limit | value | why |
|---|---|---|
| rendered body | ≤ 3584 bytes (UTF-8) | ntfy turns bodies over 4096 bytes into an attachment; 512 bytes are reserved for the "Answered" update that quotes the reply |
| `title` | ≤ 960 bytes, no line break | ntfy rejects titles over 1 KB; the rest is reserved for the `[tag]` and the status prefix |
| `options` | 2 to 5 | see above |

Validation runs locally before anything is sent and reports **every** problem in one go (see [failures.md](failures.md) for the report format). Over-length input is rejected, never truncated: shorten `description`, `consequence`, or `reasoning` and call again. CJK text costs 3 bytes per character.

## 4. What the phone shows (rendered result)

The example from SKILL.md, sent from herdr pane `wD:p1` with `"lang": "en"`, renders as:

```
Title: [wD:p1] Keep or delete the scratch directory when no checkout exists

[Doing] Letting the requirements assistant run before the project code is checked out

[Background] Until now the assistant required a local code directory. That restriction is lifted, so we must decide where its temporary subprocess runs when there is no checkout.

[Blocker] With no code directory there is no natural working directory for that subprocess.

[Options]
  1. Keep a fixed directory (recommended) → One directory per project. Leaves a scene to inspect after failures; the cost is directories piling up with nobody cleaning them
  2. Delete after use → Clean, but nothing is left to inspect after a crash; debugging relies on logs alone

[My recommendation] Keep a fixed directory: users on this path are the ones most likely to have a broken setup, so a scene is worth having. Strongest objection: disk clutter accumulates.

[Your call] Keep a fixed directory, or delete after use?
──────────
⚠️ The button is a shortcut. Disagree? Type your reply in the box below.
   A reply takes effect the moment you send it — it can't be withdrawn or amended, so say it all at once.

                    [ Accept recommended ]   → sends "Keep a fixed directory"
```

The `[wD:p1]` tag is your pane id inside herdr, otherwise the slot name. Plain text, no Markdown: the layout must survive every ntfy client. After the reply arrives the card is replaced in place by `✅ Answered · [wD:p1] …` with `[Your reply] …` on top and the original question quoted below, and the notification is cleared. After a timeout or a cancelled `ask` the card becomes `⌛ Timed out · …` or `⚠️ Cancelled · …` the same way.

## 5. Bad vs good

Same situation, two submissions. The difference is not length; the second needs no background.

**Bad**: the whole message is

```
wD p4 cwd policy unaligned, A: userData persistent B: tmpdir throwaway, please decide.
```

Jargon, internal ids compressing the facts, two options thrown at the reader, no background, no consequences, no lean. On a phone there is nothing to do with this except walk back to the computer. (It would also fail validation: no `description`, `blocker`, `reasoning`, or `question`.)

**Good**: the JSON in SKILL.md. Each option says what it does and what it costs; `reasoning` commits to a lean and names the objection; the `title` alone tells the reader what kind of decision this is.

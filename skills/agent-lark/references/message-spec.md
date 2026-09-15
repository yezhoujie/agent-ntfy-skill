# Writing the question

The human reads this on a phone, possibly hours after you sent it, with none of your context.
The test for every field: **could someone who never saw the work decide from this alone?**

## Contents

1. Field by field
2. Buttons, tick boxes, and what not to recommend
3. Limits and validation
4. What the phone shows (single choice)
5. Multi-select (`select: "multi"`)
6. The notification card (`notify`)
7. Bad vs good

## 1. Field by field

| field | write | avoid |
|---|---|---|
| `title` | The hook, one line. It is the card header on the phone, the notification preview, and the text a later `(reply to: "…")` quotes back to you | Code names; anything that needs the body to make sense |
| `doing` | One sentence naming the task in plain words | Task ids, ticket numbers, branch names on their own |
| `description` | Why the work reached this point, what is involved, and every term of art explained where it appears | Assuming the reader watched the run |
| `blocker` | The concrete thing that cannot proceed | Restating the title |
| `options[].label` | Short, distinct, self-explanatory: it is the button text **and** what a tap sends back, so it must still make sense to you after a context reset | `A` / `B` / `option 1` |
| `options[].consequence` | What actually happens if this is chosen, including the cost | A one-word summary |
| `options[].danger` | `true` on an option that is irreversible or expensive: red button, confirm dialog before it fires | Marking the safe default; recommending a danger option (rejected) |
| `recommend` | The `id` you would pick yourself; with `select: "multi"`, the ids to tick by default (an array) | An option you would not act on |
| `reasoning` | Your lean, then the strongest argument against it. This is what stops the message from being a bare list of choices | "Either works" |
| `question` | The thing the human should answer, in one sentence | Several questions at once |
| `select` | `"multi"` when the honest answer may be several of the options at once ("which of these checks do you want"); omit otherwise | Multi for a yes/no; single with options that are not mutually exclusive |
| `lang` | `zh` or `en`, matching the language you reply to the user in: `zh` if you reply in Chinese, otherwise `en`. Omitted is `en` | Any other value: rejected with the rest of the validation report |

Options: at least 2 (one option is not a choice), at most 5 (more means the question has not
converged; think first). Ids must be unique; `recommend` must name existing, non-danger ids.

Your own fields are in your working language. Only the fixed wrapper (section labels, hints, button
texts, the status word in the header, confirm dialogs) follows `lang`.

Your text is rendered by Feishu's Markdown card element: bold, lists, tables and fenced code blocks all
render, on the phone as on the desktop. Keep it short anyway — the phone shows a card, not a page.

## 2. Buttons, tick boxes, and what not to recommend

**Single choice** (the default): one button per option, in the order you gave them. The recommended
option's button is the highlighted (primary) one; the others are plain; a `danger` option's button is
red and opens a native confirm dialog first (`Confirm` / `"<label>" is irreversible or high-cost. Go ahead?`).
Tapping sends back that option's **`label`**, not its id and not the button position, so the reply is
self-explanatory even if you were reset in between. The buttons lock on the first tap: the answered card
comes back on the tap's own callback.

**Multi-select**: one tick box per option (the recommended ones pre-ticked) and a `Submit` button;
when any option is `danger`, the confirm dialog sits on `Submit`. Submitting with nothing ticked shows
`Pick at least one` and leaves the card open; a submit sends the ticked labels joined with `、`.

Typing in the group sends free text: the group has one input box, not one per card, so "the reply" is
simply the next message in the group while your question is pending. The channel does not tell you
which of the two happened; both arrive as the reply on stdout.

A tap fires immediately. Therefore:

- **Never make an irreversible or high-cost option the recommendation** — validation refuses it — and
  mark it `danger` so the phone asks "Go ahead?" before it fires.
- For a verdict with heavy consequences, send a second `ask` that restates what you are about to do.
- Expect corrections. The first reply closes the question; a later tap, submit or message reaches you as
  an instruction (`(follow-up) I pick <labels>` or the typed text). Take the last one unless you have a
  reason not to.
- `--urgent` adds Feishu's in-app urgent ping to the app owner. Reserve it for an irreversible step or a
  short timeout; a flag on every question is no flag at all.

## 3. Limits and validation

| what | limit |
|---|---|
| `title` | ≤ 200 characters, one line |
| `doing`, `description`, `blocker`, `reasoning`, `question` | ≤ 4000 characters each |
| `options` | 2 to 5 |
| `options[].label` | ≤ 60 characters |
| `options[].consequence` | ≤ 500 characters |
| `notify` `body` | ≤ 8000 characters |

Characters are counted as JavaScript does (a CJK character is one), after trimming leading and trailing
whitespace; an empty or whitespace-only value counts as missing. The caps are sanity caps — Feishu
takes far more than a push notification would — not a target. Validation runs locally before anything
is sent and reports **every** problem in one go (the report format is in [failures.md](failures.md) §2);
over-length input is rejected, never truncated.

## 4. What the phone shows (single choice)

The example from SKILL.md, sent from a project directory named `my-project` with `"lang": "en"`, is one
card with a **blue** header (🤔; red when `--urgent`) and these elements, top to bottom. The `[my-project]`
tag is the project directory's name.

```
🤔 [my-project] Keep or delete the scratch directory when no checkout exists

**Doing**　Letting the requirements assistant run before the project code is checked out
**Background**　Until now the assistant required a local code directory. That restriction is lifted, so we must decide where its temporary subprocess runs when there is no checkout.
**Blocker**　With no code directory there is no natural working directory for that subprocess.
────────────────────────────────
**Options**

1. **Keep a fixed directory** — One directory per project. Leaves a scene to inspect after failures; the cost is directories piling up with nobody cleaning them　← recommended
2. **Delete after use** — Clean, but nothing is left to inspect after a crash; debugging relies on logs alone
────────────────────────────────
**My recommendation**　Keep a fixed directory: users on this path are the ones most likely to have a broken setup, so a scene is worth having. Strongest objection: disk clutter accumulates.
**Your call**　Keep a fixed directory, or delete after use?

[ Keep a fixed directory ]  [ Delete after use ]          ← primary button = the recommendation
Want to say something else? Just send a message in this group — the first one is the answer.
```

A `danger` option gets `　⚠️` after its label in the list, a red button, and the grey hint changes to
`Red buttons ask for confirmation; you can also just type here.`

After the reply the same card is rewritten in place — **green** header `✅ [my-project] <title> · Answered`,
`**Your reply**　<the reply>` on top, a rule, `(the question as asked)`, then the original body without
buttons. A timeout gives a **grey** `⌛ … · Timed out`, a cancelled question a **grey** `⚠️ … · Cancelled`,
both without buttons. In `zh` the labels are 在做 / 背景 / 卡点 / 选项 / 我的判断 / 你的判断, the status
words 已回答 / 已超时 / 已取消, and `← 我推荐` marks the recommendation.

## 5. Multi-select (`select: "multi"`)

```json
{
  "title": "Which checks to run before the release",
  "doing": "Preparing the 2.4 release of the payment service",
  "description": "The release checklist has four optional checks. Each takes 10 to 40 minutes on the staging cluster; the cluster is shared with QA today.",
  "blocker": "I cannot tell which ones you consider mandatory for this release.",
  "options": [
    {"id": "load",   "label": "Load test",          "consequence": "40 minutes; occupies the staging cluster, QA is blocked meanwhile"},
    {"id": "migr",   "label": "Migration dry run",  "consequence": "10 minutes; catches schema mistakes before they hit production"},
    {"id": "sec",    "label": "Dependency audit",   "consequence": "15 minutes; may surface CVEs that block the release"},
    {"id": "wipe",   "label": "Reset staging data", "consequence": "Deletes every staging record; QA's fixtures are gone", "danger": true}
  ],
  "select": "multi",
  "recommend": ["migr", "sec"],
  "reasoning": "The migration dry run and the audit are cheap and catch the two failure classes we have actually had. Strongest objection: skipping the load test means the first real load is production traffic.",
  "question": "Which of the four checks should run before I tag the release?",
  "lang": "en"
}
```

The card body is the same as in §4; the buttons are replaced by one tick box per option (`**Load test** — 40 minutes; …`),
with `Migration dry run` and `Dependency audit` pre-ticked, and a `Submit` button. Because one option
is `danger`, `Submit` opens `The selection includes an irreversible or high-cost option. Submit anyway?`
first. The grey hint reads `Tick what applies, then Submit; to say something else just type in the group — the whole message is the reply.`
Submitting `Migration dry run` and `Dependency audit` puts `Migration dry run、Dependency audit` on
your stdout; typing `all four` puts `all four` there.

## 6. The notification card (`notify`)

`{"title": "Tests green, starting the migration", "body": "All tests pass on the three CI runners.\n\nNext: **schema migration** on the staging database (about 10 minutes). I will notify again when it is done.", "lang": "en"}`
sent from the same project is one card with a **light blue** header and your body as its only element:

```
📣 [my-project] Tests green, starting the migration

All tests pass on the three CI runners.

Next: **schema migration** on the staging database (about 10 minutes). I will notify again when it is done.
```

No button, no hint, no status word, and the card is never rewritten: replying to it changes nothing on
it (the reply reaches you as an instruction, or as the answer to a pending question). `lang` has no
visible effect on this card; it records the project's language for the cards the daemon sends on its
own (the "not delivered" receipt and the "waiting for you" alert).

`send-file` sends a plain message: with `--caption` a Markdown line `**[my-project]** <caption>` first,
then the image (as an image message) or the file (as a file message named after the file).

## 7. Bad vs good

Same situation, two submissions. The difference is not length; the second needs no background.

**Bad**: the whole message is

```
wD p4 cwd policy unaligned, A: userData persistent B: tmpdir throwaway, please decide.
```

Jargon, internal ids compressing the facts, two options thrown at the reader, no background, no
consequences, no lean. On a phone there is nothing to do with this except walk back to the computer.
(It would also fail validation: no `description`, `blocker`, `reasoning`, or `question`.)

**Good**: the JSON in SKILL.md. Each option says what it does and what it costs; `reasoning` commits to
a lean and names the objection; the `title` alone tells the reader what kind of decision this is.

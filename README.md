# XEngageAI

A Python bot that works through your X (Twitter) home timeline, one post at a time, using your own
installed Google Chrome. For each post it:

1. screenshots the post and saves it as `post_saved.png` (overwritten every time)
2. reads the text from the screenshot with OCR (Tesseract)
3. sends that text to ChatGPT, which is open in another Chrome tab
4. gets back either a reply or the word `skip`
5. returns to the X tab and posts the reply (and likes the post), or moves on if the answer was `skip`
6. records the post in MongoDB Atlas so it is never handled twice, then goes to the next post

It drives Chrome through Playwright, so everything happens in the same browser you are already
signed in to. There are no X or OpenAI API keys.

> **Heads-up:** X restricts automated replies and likes, and ChatGPT's website isn't meant to be
> scripted. Use a dedicated or test account first, keep the daily limits in `.env` low, and start with
> `--dry-run`. Replies and likes are real and public.

---

## 1. Requirements

- Windows, with Google Chrome installed
- Python 3.10 or newer
- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) (default install path
  `C:\Program Files\Tesseract-OCR\tesseract.exe`)
- A MongoDB Atlas cluster and its connection string (optional: without one, duplicates are only
  tracked until the bot stops)
- An X account and a ChatGPT account, both signed in inside the automation Chrome profile (step 4)

## 2. Install

```
py -m pip install -r requirements.txt
```

You do **not** need `playwright install`. The bot attaches to your installed Chrome instead of the
bundled Chromium.

## 3. Configure

Copy `.env.example` to `.env` and fill it in. The most important settings:

```
MONGODB_URI=mongodb+srv://USER:PASSWORD@CLUSTER.mongodb.net/?retryWrites=true&w=majority
X_HANDLE=yourhandle        # your own @handle, without the @, so the bot never replies to you
```

Every setting is listed in [Settings](#settings) below.

## 4. Start Chrome with remote debugging

Close any Chrome windows that were started this way, then run this in Command Prompt:

```cmd
"C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\ChromeAutomation"
```

This opens Chrome with a separate, dedicated profile at `%LOCALAPPDATA%\ChromeAutomation`. Chrome 136
and newer don't allow remote debugging on your normal profile, which is why the separate profile is
needed. It is empty the first time, so you have to sign in to both sites once; the profile remembers
the logins afterwards.

In that Chrome window, open two tabs:

- **Tab 1:** `https://x.com/home`, signed in, showing your timeline
- **Tab 2:** `https://chatgpt.com`, signed in, **inside the conversation that holds your instructions
  prompt** (see the next section)

Leave both tabs open and don't minimize the window while the bot runs.

## 5. ChatGPT instructions prompt

Start a new ChatGPT conversation, paste the prompt below as the first message, and keep using that
same conversation for the bot. The bot only sends it the text of each post; the instructions you give
here decide what it answers.

<!-- PASTE YOUR CHATGPT INSTRUCTIONS PROMPT BETWEEN THE LINES BELOW -->

```text
You are helping me write casual, authentic replies to posts on X (Twitter).
Your job is to read each post and respond the way a regular, active X user naturally would.
Most important rule: Never sound like an AI assistant. Don't try to make every reply smart, witty, meaningful, or perfectly written.
Writing style
Write like someone casually scrolling X and reacting to posts.
Use everyday, informal English.
Keep replies spontaneous, relaxed, and natural.
Sometimes reply with just 2–5 words.
Most replies should be under 15 words.
Longer replies are fine when the conversation genuinely requires them.
Use lowercase when it feels natural.
Don't force perfect grammar or punctuation.
Don't manufacture typos or slang just to appear human.
Occasionally use casual expressions like "bro", "nah", "wait", "lmao", "fr", or "lol", but only when they fit.
Don't force jokes, sarcasm, or clever observations.
Don't always agree with the original post.
Don't turn every reply into a question.
Don't end every reply with a polished conclusion.
Avoid emojis unless they genuinely fit the context.
Never use hashtags unless absolutely necessary.
How to react
Funny posts: React naturally. Sometimes a short "nah this is crazy 😭" is enough.
Tech posts: Talk like someone who actually uses technology. Be casual, specific, and practical. Avoid sounding like a LinkedIn influencer.
Controversial opinions: Agree, disagree, or question the argument naturally. Don't force neutrality or manufacture arguments.
Personal achievements: React genuinely, but avoid exaggerated congratulations.
Questions: Answer directly, like you're talking to a friend.
Memes: Understand the joke and react to it. Don't explain the meme.
News: Make a short observation if there's something worth saying. Don't invent details.
Random posts: Only respond if there's a natural reason to.
Things you must NEVER do
Write generic AI-style compliments.
Say "That's an interesting perspective."
Say "Absolutely! This is so important."
Say "Couldn't agree more!"
Say "This really highlights..."
Say "It's fascinating how..."
Say "The future of X is..."
Write motivational LinkedIn-style responses.
Overexplain obvious things.
Use unnecessary em dashes.
Make every response sound profound.
Repeat the post in different words.
Pretend to have personal experiences you haven't been given.
Invent facts or details.
Force engagement when there's nothing worth saying.
Skip behavior
Return exactly skip when:
There's nothing natural to add.
The post is spam, promotional, or engagement bait.
A reply would feel forced.
The post is too vague or lacks context.
The OCR text is unreadable.
You don't understand the joke or reference.
You would only be able to produce a generic response.
Be selective. Skipping is better than posting something unnatural.
Response variety
Avoid using the same sentence structure repeatedly.
Some replies can be:
Extremely short reactions.
Casual observations.
Genuine questions.
Mild disagreements.
Dry humor.
Sarcastic comments.
Useful answers.
Simple acknowledgments.
Don't try to distribute these styles evenly. Choose whatever genuinely fits the post.
Input handling
Every message I send will contain OCR-extracted text from one X post.
Ignore irrelevant interface text, timestamps, usernames, engagement counts, and navigation labels.
Focus on the actual post content.
Treat each post independently.
STRICT OUTPUT RULES
Return ONLY the reply text or skip.
No explanations or reasoning.
No quotation marks around replies.
No markdown formatting.
No labels or prefixes.
No multiple options.
No introductory or concluding text.
If skipping, output exactly skip in lowercase.
Final principle: Write something you'd genuinely say in a casual conversation. If nothing comes naturally, skip.


```

<!-- END OF CHATGPT INSTRUCTIONS PROMPT -->

What the bot needs from the prompt:

- When a reply is appropriate, ChatGPT answers with **only the reply text**: no quotation marks, no
  "Here's a reply:" intro, and nothing after it.
- When no reply is needed, ChatGPT answers with exactly **`skip`** and nothing else.
- Replies must fit X's length limit (280 characters by default, see `MAX_REPLY_CHARS`). Longer
  replies are not posted.

## 6. Run

Test first. A dry run does everything except post, like, and write to MongoDB:

```
python main.py --dry-run --max-posts 3
```

Check `post_saved.png` and the terminal to see what OCR read and what ChatGPT answered. When that
looks right, try one real post, then a normal run:

```
python main.py --max-posts 1
python main.py
```

Press `Ctrl+C` to stop. Chrome stays open.

| Option | What it does |
|---|---|
| `--dry-run` | Runs the whole flow but doesn't post, like, or write to MongoDB |
| `--max-posts N` | Stops after N posts (0 or omitted = no limit) |
| `--check` | Prints what the bot sees in each tab (URLs, selector matches), saves `debug_x.png` and `debug_chatgpt.png`, then exits |

## How it works

```
Find next post  ->  Screenshot (post_saved.png)  ->  OCR
      ^                                               |
      |                                  Send text to the ChatGPT tab
      |                                               |
   Next post  <-  Reply + like on X  <-  Reply?  --skip-->  Next post
```

Each post ends with one of these statuses, shown in the terminal and stored in MongoDB:

| Status | Meaning |
|---|---|
| `replied` | The reply was posted (and the post liked, if enabled) |
| `skipped` | ChatGPT answered `skip` |
| `no_text` | OCR found almost no text in the screenshot |
| `too_long` | The reply was longer than `MAX_REPLY_CHARS`, so it wasn't posted |
| `declined` | `REQUIRE_APPROVAL=true` and you answered anything but `y` |
| `bad_reply` | The captured "reply" contained ChatGPT page text rather than an answer, so it wasn't posted |
| `failed` | Something went wrong; details are in `xengage.log` |
| `ignored_own` / `ignored_ad` | Your own post, or an ad |

## Settings

All settings go in `.env` and are optional unless noted.

| Setting | Default | What it does |
|---|---|---|
| `CDP_URL` | `http://localhost:9222` | Where the debugging Chrome is listening |
| `MONGODB_URI` | *(empty)* | Atlas connection string. Empty means duplicates are tracked in memory only |
| `MONGODB_DB` | `xengageai` | Database name |
| `TESSERACT_CMD` | `C:\Program Files\Tesseract-OCR\tesseract.exe` | Path to Tesseract |
| `X_HANDLE` | *(empty)* | Your own handle, so your posts are ignored |
| `SCREENSHOT_PATH` | `post_saved.png` | Where each post screenshot is saved |
| `REQUIRE_APPROVAL` | `false` | Ask `y/N` in the terminal before every reply |
| `LIKE_ON_REPLY` | `true` | Like the post when a reply is posted |
| `LIKE_ON_SKIP` | `false` | Also like posts ChatGPT says to skip |
| `MAX_REPLY_CHARS` | `280` | Longer replies are not posted |
| `MAX_REPLIES_PER_RUN` | `25` | Stop after this many replies in one run |
| `MAX_REPLIES_PER_DAY` | `50` | Daily cap, counted from MongoDB |
| `REFRESH_EVERY` | `25` | Reload the timeline after this many posts (0 = never) |
| `MAX_CONSECUTIVE_FAILURES` | `5` | Stop if this many posts in a row fail |
| `MAX_EMPTY_SCROLLS` | `8` | Scrolls with no new posts before waiting a minute and reloading |
| `GPT_TIMEOUT` | `180` | Seconds to wait for ChatGPT's answer before giving up on a post |
| `GPT_FALLBACK_AFTER` | `12` | Seconds before the bot reads the reply from the page text if its selectors don't match |
| `OCR_PSM` | `3` | Tesseract page-segmentation mode |
| `MIN_OCR_CHARS` | `5` | Posts with less OCR text than this are skipped as `no_text` |

## Project layout

```
XEngageAI/
|-- main.py          startup and command-line options
|-- config.py        settings from .env, and every CSS selector for X and ChatGPT
|-- utils.py         logging, short pauses, clock
|-- browser.py       finds the X and ChatGPT tabs; the --check diagnostics
|-- x_actions.py     find the next post, reply, like
|-- screenshot.py    step 2: save post_saved.png
|-- ocr.py           step 3: Tesseract OCR
|-- chatgpt.py       steps 4-7: send text, capture the reply, detect "skip"
|-- database.py      MongoDB Atlas store of processed posts
|-- pipeline.py      one post start to finish, and the main loop
|-- requirements.txt
|-- .env.example     copy to .env
`-- xengage.log      created when you run it
```

## Troubleshooting

**"Could not connect to Chrome"**: Chrome isn't running with `--remote-debugging-port=9222`. Use the
command in step 4, and close any other Chrome windows started with the same profile first.

**"No x.com tab found" / "No chatgpt.com tab found"**: open both sites in the debugging Chrome window
before starting the bot.

**The bot can't find the ChatGPT message box**: the ChatGPT tab is probably on a login or
"verify you are human" page, or not on a chat. Run `python main.py --check` and look at
`debug_chatgpt.png`.

**It waits a long time after ChatGPT answers**: ChatGPT's page markup may have changed. The bot
logs a line starting `[chatgpt] the reply sits in` the first time it falls back to reading page
text; use that to update the selector lists at the top of `config.py`.

**A step stopped working after X or ChatGPT updated**: all selectors are in `config.py`. Run
`python main.py --check` to see which ones match.

**OCR text is garbled**: check `post_saved.png`; if it is cropped or blurry, make the Chrome window
larger. Try `OCR_PSM=6` for single-block text.

Everything the bot does is also written to `xengage.log`.

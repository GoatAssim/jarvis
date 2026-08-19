# jarvis

A tiny CLI where every subcommand is just an entry in a JSON file. Add a
command to the config, and `jarvis <thatCommand>` works — no code, no
reinstall. Commands can run multiple steps — in order, or in parallel —
each step can be conditioned on your variables (`if var1 is X and var2 is
Y, run this...`), and you can chain several commands into one call, again
in order or in parallel. Works the same way on Windows, Mac, and Linux.

```
$ jarvis
  -------------------------------
   J A R V I S
   your commands, your rules
  -------------------------------

Available commands:

  hello           Say hello to someone
  updateSpotify   Example command — edit 'run' for your OS's real updater
  deployExample   Example: multiple steps + conditions (edit or delete me)

Run 'jarvis <command> --help' for a command's options.
Chain several with 'jarvis cmd1 then cmd2'.
Edit /home/you/.jarvis/commands.json to add or change commands.
```

## Install

From this folder:

```
pip install .
```

That's it — `pip` builds a real `jarvis` executable (`jarvis.exe` on
Windows) and drops it in your Python environment's scripts folder, which is
normally already on PATH after a standard Python install.

**Check it worked:** open a new terminal and run `jarvis`. If you see the
banner, you're done.

**If `jarvis` isn't found:** your Python scripts folder isn't on PATH. Two
options:
- Find it with `python -m site --user-base` (scripts live in a `Scripts`
  subfolder on Windows, `bin` on Mac/Linux) and add that folder to PATH.
- Or skip PATH entirely and always run it as `python -m jarvis <command>` —
  works identically, no install-time PATH issues possible.

Want to keep editing the source after install? Use `pip install -e .`
instead — same result, but changes to `jarvis/cli.py` take effect
immediately without reinstalling. (You won't need this just to edit
commands — the config file below is separate from the code.)

## Adding commands

Your commands live in `~/.jarvis/commands.json` (created automatically on
first run — **not** inside this project folder, so it survives reinstalls
and updates). Find the exact path anytime with `jarvis config`.

Each command looks like this:

```json
{
  "commands": {
    "updateSpotify": {
      "description": "Update Spotify",
      "run": "winget upgrade Spotify.Spotify",
      "vars": {}
    },
    "greet": {
      "description": "Say hello to someone",
      "run": "echo Hello, {name}! You looking tuff today.",
      "vars": {
        "name": {
          "default": "World",
          "description": "Who to greet"
        }
      }
    },
    "deploy": {
      "description": "Deploy a branch to an environment",
      "run": "echo Deploying {branch} to {env}",
      "vars": {
        "env": {
          "description": "Target environment — no default, so it's required"
        },
        "branch": {
          "default": "main",
          "description": "Git branch to deploy"
        }
      }
    }
  }
}
```

Rules:
- **`run`** is any shell command. `{varName}` gets replaced with that
  variable's value before it runs. If your command needs a literal `{` or
  `}`, write it as `{{` / `}}`.
- **`vars`** is optional. Every key becomes a `--flag` on that subcommand.
- Give a var a **`"default"`** and it's optional on the command line. Leave
  `"default"` out and it becomes **required** — `jarvis` will refuse to run
  without it.
- `description` fields show up in `jarvis` (the list) and
  `jarvis <command> --help` (the detail view). Keep them short.

After editing the file, just run the command — no reinstall, no restart.

```
$ jarvis greet --name Amine
$ echo Hello, Amine! You looking tuff today.
Hello, Amine! You looking tuff today.

$ jarvis deploy --env staging
$ echo Deploying main to staging

$ jarvis deploy
usage: jarvis deploy [-h] --env ENV [--branch BRANCH]
jarvis deploy: error: the following arguments are required: --env
```

`jarvis` exits with whatever exit code the underlying command returned, so
it chains fine in scripts (`jarvis deploy --env prod && echo done`).

## Multiple steps in one command

`run` can be a list instead of a single string. Every item runs in order,
as if you'd typed each one and pressed enter:

```json
{
  "commands": {
    "release": {
      "description": "Build, then publish",
      "run": [
        "npm run build",
        "npm publish"
      ],
      "vars": {}
    }
  }
}
```

If a step fails (non-zero exit code), `jarvis` stops right there and exits
with that step's code — later steps don't run. Add `"continueOnError":
true` to a step to let the chain continue even if that one step fails:

```json
"run": [
  "npm run lint",
  {"run": "npm run test:flaky", "continueOnError": true},
  "npm run build"
]
```

You can give a step an optional `"name"` — it's printed before the step
runs, just for readability in the output:

```json
{"name": "Run tests", "run": "npm test"}
```

### Running steps in parallel

By default, each step waits for the one before it to finish. Add
`"parallel": true` to a step to have it start *alongside* whichever step(s)
came right before it instead of waiting — good for a batch of independent
launches that don't depend on each other:

```json
"run": [
  {"name": "syncplay", "run": "start \"\" \"C:\\Apps\\Syncplay\\Syncplay.exe\""},
  {"name": "stremio", "run": "start \"\" \"C:\\Apps\\Stremio\\stremio.exe\"", "parallel": true},
  {"name": "confirm", "run": "echo Both are opening now, sir.", "showCommand": false}
]
```

`syncplay` and `stremio` start together (`stremio` is `parallel`, so it
starts as soon as `syncplay` does rather than waiting for it); `confirm`
isn't `parallel`, so it starts a fresh batch that waits for *both* of them
to finish first. `"parallel": true` on the very first step does nothing —
there's nothing before it to run alongside.

If a step in a parallel batch fails (and isn't `continueOnError`), jarvis
still waits for the rest of that batch to finish — they're already
running, there's no half-un-starting them — then stops the chain, and
reports every step in the batch that failed, not just the first.

### Hiding a step's command line

Every step normally prints a trace line right before it runs —
`▶ name` (if it has one) then `$ the actual resolved command` — to stderr,
separate from whatever the command itself prints. Set `"showCommand":
false` on a step to suppress just that trace line, e.g. for a step whose
whole job is a spoken-style confirmation where the raw command would just
be visual noise:

```json
{"name": "confirm", "run": "echo Both are opening now, sir.", "showCommand": false}
```

This *only* hides the trace line — whatever the step itself actually
prints (its real stdout/stderr) always shows, exactly as before. There's
no way to hide that and still know what actually happened; the toggle is
purely about not double-printing an already-obvious `echo`.

## Conditions: run a step only when your vars match

Any step can take an `"if"` and/or `"unless"`. A step only runs when its
`if` is true (or absent) **and** its `unless` is false (or absent). This
is what you'd reach for to say "if var1 is X and var2 is Y, run this; if
var1 is X and var2 is Z, run that instead":

```json
{
  "commands": {
    "deploy": {
      "description": "Deploy, with different steps per environment",
      "run": [
        {
          "if": {"env": "prod", "branch": "main"},
          "run": "echo Deploying main to PROD"
        },
        {
          "if": {"env": "prod", "branch": "hotfix"},
          "run": "echo Deploying hotfix to PROD"
        },
        {
          "if": {"env": "staging"},
          "run": "echo Deploying {branch} to STAGING"
        },
        {
          "unless": {"env": "prod"},
          "run": "echo (non-prod deploys are logged here)"
        }
      ],
      "vars": {
        "env": {"description": "prod, staging, ..."},
        "branch": {"default": "main", "description": "Git branch"}
      }
    }
  }
}
```

```
$ jarvis deploy --env prod --branch main
$ echo Deploying main to PROD
Deploying main to PROD

$ jarvis deploy --env staging
$ echo Deploying main to STAGING
Deploying main to STAGING
```

Rules for `if` / `unless`:
- **Every step whose condition matches runs** — it's not "stop at the
  first match" (that's not `if`/`elif`). In the example above, if you
  wrote a step matching more than one condition at once, both would run.
  Write mutually exclusive conditions (like the example does) if you want
  branch-like, only-one-runs behavior.
- **Dict form** (recommended): `{"var": "value"}` means that variable must
  equal that value. Multiple keys are AND'd together. A list value means
  "equals any of these": `{"region": ["us", "eu"]}` matches `us` or `eu`.
  Every key must be one of that command's actual `vars` — a typo raises a
  clear error instead of silently never matching.
- **String form**, for anything the dict form can't say: a small, safe
  expression language supporting `==`, `!=`, `<`, `>`, `<=`, `>=`, `and`,
  `or`, `not`, and parentheses. A bare `=` works the same as `==`, and an
  unquoted word is treated as a literal (`"env = prod"` and `"env ==
  'prod'"` mean the same thing) — quote it if you want to be unambiguous,
  especially if the literal you're comparing against happens to share a
  name with one of your variables.
  ```json
  {"if": "env == 'prod' and (region == 'us' or region == 'eu')", "run": "..."}
  ```
  Conditions are evaluated by jarvis itself (a restricted, safe parser —
  never `eval()`), not by your shell, so this works identically on
  Windows, Mac, and Linux regardless of what shell you use.
- If **no** step's condition matches, jarvis prints a warning and exits
  with code 1 rather than silently doing nothing. Add a step with no
  `if`/`unless` at the end if you want a guaranteed default/fallback.

## Chaining multiple commands in one call

Put `then` between commands to run several in one `jarvis` invocation,
each with its own flags, stopping at the first one that fails:

```
$ jarvis build then test then deploy --env staging
```

Put `and` between two commands instead of `then` to run them *together* —
the same then/parallel relationship a single command's own steps have via
`"parallel"` (see above), just one level up, between whole commands:

```
$ jarvis openSyncplay and openStremio then announceReady
```

`openSyncplay` and `openStremio` start together; `announceReady` waits for
*both* to finish before it runs. Chain as many `then`/`and` links as you
like, in any mix — each `and` joins the batch that's already forming, each
`then` waits for the previous batch to finish and starts a fresh one.

This is different from your shell's own `&&`/`&` — it works identically no
matter which shell or OS you're running in, and it's one `jarvis` process
rather than several (commands running `and`-together do so on separate
threads within that one process, not separate processes). Because
`then`/`and` are the separators, avoid using either literal word as a
flag's value, and don't name a command `then` or `and` — jarvis will warn
you at startup if a command name collides with a reserved word (`then`,
`and`, `config`, `ai-config`, `ai-clear`, `-h`, `--help`).

## Talking to Jarvis (AI mode)

Anything you type at `jarvis` that *isn't* a command name is treated as
a message for an AI, not a CLI call — so this works right alongside
your regular commands, no separate subcommand needed:

```
$ jarvis "what's a good way to organize my downloads folder"
J.A.R.V.I.S: Might I suggest sorting by file type into a few folders —
Documents, Installers, Media — and setting up a rule so new downloads
land there automatically, sir?
```

You don't need quotes — `jarvis update my spotify` (three separate,
unquoted words, none of which match a command called `update`) reaches
the AI exactly the same way `jarvis "update my spotify"` does. Quoting
only matters because it stops your shell from splitting a sentence into
separate argv entries; jarvis treats both the same. A single word that
*does* match a command you've defined still just runs that command,
unchanged — that check always comes first.

### Setting it up

```
$ jarvis ai-config
/home/you/.jarvis/ai_config.json
```

That file is created (on first use) with every provider below already
listed, each with an empty `api_keys` list. Paste a real key into any one
block and that provider goes live immediately — read fresh on every call,
same as `commands.json`, so there's no reload step:

```json
{
  "persona": {
    "assistant_name": "J.A.R.V.I.S",
    "address_user_as": "sir",
    "extra_instructions": ""
  },
  "defaults": { "max_tokens": 700, "timeout": 30, "tools_enabled": true },
  "providers": [
    { "name": "openai", "type": "openai_compatible", "enabled": true,
      "base_url": "https://api.openai.com/v1/chat/completions",
      "api_keys": ["sk-...primary", "sk-...backup"], "model": "gpt-5-mini" },
    { "name": "anthropic", "type": "anthropic", "enabled": true,
      "base_url": "https://api.anthropic.com/v1/messages",
      "api_keys": [""], "model": "claude-haiku-4-5-20251001" }
  ]
}
```

(The full starter file lists all ten providers from the table below —
trimmed here for space.)

`api_keys` is a list, so a provider can hold more than one key — jarvis
tries them in order before giving up on that provider (see Failover,
below). Most people only ever need one; just leave the list at a single
entry. The older singular `"api_key": "..."` field (from before multi-key
support existed) still works too, including alongside `api_keys` — it's
just tried last, after everything in the list.

### Supported providers

| Provider | `type` | Needs a key? |
|---|---|---|
| OpenAI | `openai_compatible` | yes |
| Anthropic (Claude) | `anthropic` | yes |
| Google Gemini | `gemini` | yes |
| xAI (Grok) | `openai_compatible` | yes |
| Groq | `openai_compatible` | yes |
| Mistral | `openai_compatible` | yes |
| DeepSeek | `openai_compatible` | yes |
| OpenRouter | `openai_compatible` | yes |
| Cohere | `cohere` | yes |
| Ollama (local) | `ollama` | **no** — runs entirely on your own machine |

`"type": "openai_compatible"` is a generic adapter for any host that
mirrors OpenAI's `/chat/completions` request/response shape — which, as
of writing, is most of them. To add one that isn't listed (Azure OpenAI,
Together.ai, Fireworks, a company-internal proxy, ...), you don't need
new code — just add a provider block with this type and the right
`base_url`.

Model names drift fast; the ones in the starter file were current when
this was written, but double check against each provider's own docs if
a request fails with a "model not found"-style error — it's a one-line
fix in the JSON, nothing to reinstall.

### Failover: how "try the next one" actually works

The `providers` array's **order is the failover order**, and so is each
provider's own `api_keys` list. On every ask, jarvis tries provider index
0 first — and within it, key 0 first. If an attempt fails for *any*
reason — invalid key, rate limit, out of credits, a timeout, the
provider's own server erroring out, or the model refusing the request on
safety grounds — it logs why and moves to the *next key for that same
provider* (if there is one), only falling through to the next *provider*
once every one of this provider's keys has been tried. It stops at the
first attempt that actually answers. If everything fails, you get a clear
explanation instead of a silent hang:

```
$ jarvis "..."
  ↳ asking openai (key 1/2)…
  ✗ openai (key 1/2) — invalid or unauthorized API key (HTTP 401)
  ↳ asking openai (key 2/2)…
  ✗ openai (key 2/2) — rate limited or quota exceeded (HTTP 429)
  ↳ asking anthropic…
  ✗ anthropic — rate limited or quota exceeded (HTTP 429)
J.A.R.V.I.S: I tried every AI provider you've got configured and
couldn't get a response from any of them, sir. [...]
```

(The `(key i/N)` suffix only appears once a provider has more than one
key configured — a single-key provider's trace looks exactly like it
always has, e.g. plain `anthropic` above.)

A provider is only ever tried if `"enabled": true` **and** (it's
`"type": "ollama"`, which needs no key, **or** it has at least one
non-empty key in `api_keys`/`api_key`) — so the starter file's unfilled
cloud providers are silently skipped rather than "tried and failed" on
every single call. Reorder the provider array, or the keys within one
provider, to change priority without deleting anything.

Refusal detection uses each provider's own documented signal for it
(e.g. Anthropic's `stop_reason: "refusal"`, Gemini's
`promptFeedback.blockReason` / `finishReason`) — not guesswork about the
reply's wording — so Jarvis's own honest "I don't have a function for
that, sir" answers are never mistaken for a refusal and don't trigger a
pointless failover.

### What Jarvis actually knows

Every ask includes, as context: the commands you've defined (names +
descriptions only), your most-used commands, and a recap of your last
few exchanges. Each `jarvis` call is its own fresh process — that recap
is what stands in for a long-running conversation, giving it real
continuity between separate calls. `jarvis ai-clear` wipes that memory
and starts fresh.

**It can't run your commands yet, and says so.** Jarvis is told what
commands you have but isn't given any way to actually trigger one, and
is explicitly instructed to be honest about that rather than invent a
"done!" — ask it to do something it has no function for and (model
willing) you'll get an in-character "I'm afraid I don't have a function
for that yet, sir." Wiring up real execution — and letting Jarvis draft
new commands for you from a plain-English description — is next (see
[PROGRESS.md](../PROGRESS.md)).

### Tools: real answers about your computer

Separately from commands, Jarvis can call a small set of built-in,
read-only tools to answer with real, live data instead of guessing:

```
$ jarvis "how's my battery, and what's the wifi situation"
  ↳ asking openai…
    ⚙ checking battery…
    ⚙ checking wifi info…
J.A.R.V.I.S: 74% and not plugged in, and you're on "HomeNet-5G", sir.
```

| Tool | Returns |
|---|---|
| `get_battery` | Charge %, plugged-in state, time remaining. `has_battery: false` on a desktop. |
| `get_wifi_info` | The SSID you're connected to, if any. |
| `get_location` | Approximate city/region/country from your public IP — **not** precise GPS. |
| `get_datetime` | Local date, time, weekday, timezone. |
| `get_system_info` | OS, version, hostname, architecture, uptime. |
| `get_disk_usage` | Total/used/free space on your main drive. |
| `get_memory_usage` | Total/used/available RAM. |

The model decides on its own whether a question needs one — "what's my
battery at" triggers `get_battery`; "tell me a joke" doesn't touch any
of them. All seven only ever *read* something; none of them change a
setting, write a file, or run a command — a deliberately small, fixed,
read-only toolkit, not a way for the AI to execute arbitrary code.

This is genuinely separate from the "can't run your commands" honesty
note above — the system prompt draws the line explicitly so the model
doesn't confuse "I can check your battery" with "I can run your
commands," which stays false until step 2.

Set `"tools_enabled": false` in `ai_config.json`'s `defaults` block to
turn this off entirely (e.g. to guarantee every ask is exactly one
request, no matter what) — on by default.

### Customizing the persona

`persona.assistant_name` and `persona.address_user_as` change how it
refers to itself and to you; `persona.extra_instructions` is free text
appended to its system prompt verbatim — house rules, tone tweaks,
whatever you want it to always keep in mind.

### AI-mode notes

- Needs the `requests` and `psutil` packages. If you installed jarvis
  before this feature existed, run `pip install -e .` (or `pip install .`)
  again from `jarvis-cli/` to pick them up — plain code updates never
  need a reinstall here, but a *new dependency* is the one exception.
  Every tool except `get_battery`/`get_memory_usage`/uptime works fine
  even without `psutil` installed; those specific ones degrade to a
  clear error message instead of crashing the ask.
- Conversation history (`conversation_history.json`) and your API keys
  (`ai_config.json`) both live in `~/.jarvis/`, in plain text, same as
  `commands.json` — outside this project folder, so nothing ever ends up
  committed to a repo by accident. Treat `ai_config.json` like any other
  file that has API keys sitting in it.
- Like everything else in jarvis, the reply itself is what goes to
  **stdout** (`output=$(jarvis "...")` captures exactly the reply,
  nothing else); the provider-by-provider trace goes to **stderr**.

## Commands

| Command | What it does |
|---|---|
| `jarvis` | List all configured commands |
| `jarvis <name>` | Run that command |
| `jarvis <name> --help` | Show that command's variables and steps |
| `jarvis <name1> then <name2>` | Run multiple commands in one call, in order |
| `jarvis <name1> and <name2>` | Run multiple commands together instead of in order (mix freely with `then`) |
| `jarvis "<text>"` (or any words that don't match a command) | Ask the AI — see [Talking to Jarvis](#talking-to-jarvis-ai-mode) |
| `jarvis config` | Print the path to `commands.json` |
| `jarvis ai-config` | Print the path to `ai_config.json` (created if missing) |
| `jarvis ai-clear` | Wipe AI conversation memory |

## Notes

- `run` executes through your system shell, exactly like typing it
  yourself — so anything you could run in a terminal, you can put here
  (including calling out to your own scripts: `"run": "python
  ~/scripts/backup.py {target}"`).
- Colored output turns itself off automatically when piped to a file or
  another program, so it stays script-friendly. jarvis's own status/trace
  lines (the `$ ...` echo, step names, skip notices, chain progress) print
  to **stderr**; only the actual output of your commands goes to
  **stdout** — so `output=$(jarvis mycommand)` captures exactly what your
  command printed, nothing extra.
- `jarvis` exits with the exit code of the last step/command it ran, so it
  chains fine in scripts either way: `jarvis deploy --env prod && echo
  done`.

## Windows notes

jarvis works the same way on Windows as on Mac/Linux — `pip install .`
produces a real `jarvis.exe`, and everything above (multi-step, `then`
chaining, conditions) is handled by jarvis itself in Python, not by your
shell, so it behaves identically regardless of OS.

A couple of things that *are* OS-specific, because `run` is passed
straight to your system shell (`cmd.exe` on Windows):
- The **command text itself** has to be valid for your OS. `"run": "ls
  -la"` won't work on Windows; use `"run": "dir"` (or call PowerShell
  explicitly: `"run": "powershell -Command \"Get-ChildItem\""`).
- **Windows paths in JSON** need escaped backslashes, since `\` is an
  escape character in JSON: write `"run": "python C:\\scripts\\backup.py"`
  (double backslash), not `C:\scripts\backup.py`. Forward slashes
  (`C:/scripts/backup.py`) also work fine and don't need escaping, if
  you'd rather avoid the issue entirely.
- Colored output on older/classic Windows consoles is handled
  automatically via the `colorama` package (installed only on Windows —
  see `pyproject.toml`); you don't need to do anything for it to work.
- `commands.json` is always read and written as UTF-8. If you hand-edit it
  in Notepad, make sure Notepad's "Save As" encoding is UTF-8 (it is by
  default on modern Windows) so any non-ASCII characters survive.

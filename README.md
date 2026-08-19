# jarvis

A tiny CLI where every command is just an entry in a JSON file, plus a
web console for it. Add a command to `commands.json` and `jarvis
<thatCommand>` works — no code, no reinstall. Commands can run multiple
steps — in order or in parallel — condition each step on your variables,
and chain together in one call, again in order or in parallel. Works the
same way on Windows, Mac, and Linux.

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

## In this repo

| | |
|---|---|
| [`jarvis-cli/`](jarvis-cli) | The CLI itself — a Python package (`pip install .`) that reads `~/.jarvis/commands.json` and runs whatever's in it. Start here; this is the whole tool. **→ [jarvis-cli/README.md](jarvis-cli/README.md)** for the full config format, multi-step commands, conditions, and chaining. |
| [`web/`](web) | A local web console for the CLI above — run, chain, and edit commands from the browser instead of the terminal, including a full raw-config editor. Optional; the CLI works completely on its own. **→ [web/README.md](web/README.md)** for setup and the API/WebSocket reference. |
| `jarvis.exe` | A prebuilt Windows executable, if you'd rather not run `pip install .` yourself. **Predates the AI features below** — rebuild it (e.g. `pyinstaller`) if you want those in the `.exe` too; the source in `jarvis-cli/` is what's current. |

## Fastest path to trying it

```bash
# 1. Install the CLI
cd jarvis-cli
pip install .
jarvis                       # should print the banner above

# 2. (optional) Start the web console for it
cd ../web
npm install
npm start                    # open http://127.0.0.1:4173
```

Both talk to the exact same `~/.jarvis/commands.json` — edit it from
either one, or by hand, and the other picks it up immediately. Nothing
needs to be reinstalled or restarted after a config change.

## The shape of a command

```json
{
  "commands": {
    "deploy": {
      "description": "Deploy a branch to an environment",
      "run": [
        { "if": { "env": "prod" }, "run": "echo Deploying {branch} to PROD" },
        { "unless": { "env": "prod" }, "run": "echo Deploying {branch} to {env}" }
      ],
      "vars": {
        "env": { "description": "Target environment — no default, so it's required" },
        "branch": { "default": "main", "description": "Git branch to deploy" }
      }
    }
  }
}
```

`{varName}` gets substituted before the step runs; a var with no
`"default"` becomes a required `--flag`. That's the whole model — see
[`jarvis-cli/README.md`](jarvis-cli/README.md) for the rest (multi-step
chains, `continueOnError`, the full `if`/`unless` condition syntax, and
`then`-chaining several commands in one call).

## Talking to Jarvis

Type anything that *isn't* a command name and, instead of "Unknown
command", it goes to an AI — in character, with real conversation
memory between calls:

```
$ jarvis "what's a good way to organize my downloads folder"
J.A.R.V.I.S: Might I suggest sorting by file type into a few folders...
```

Bring your own API key for whichever provider(s) you like — OpenAI,
Anthropic, Gemini, xAI, Mistral, Groq, DeepSeek, OpenRouter, and Cohere
are all wired in out of the box, or point it at a local Ollama install
and skip API keys entirely. Each provider can hold more than one key, and
you can configure more than one provider — either way it fails over
automatically: a bad key, a rate limit, or a safety refusal just moves on
to the next key, then the next provider, in the order you list them, with
the reason logged so you can see why. `jarvis ai-config` shows you where
the file lives.

Full config format, the provider list, and exactly how failover works:
**[jarvis-cli/README.md → Talking to Jarvis](jarvis-cli/README.md#talking-to-jarvis-ai-mode)**.
It's in the web console too, as an "Ask Jarvis" panel — see
[web/README.md](web/README.md).

It can also check real, live information about your computer — battery,
Wi-Fi, approximate location, date/time, disk space, memory, basic system
info — via genuine tool calling, not a guess:

```
$ jarvis "how's my battery"
J.A.R.V.I.S: 74% and not plugged in, sir — you've got a while yet.
```

It still can't run your own jarvis commands, or build new ones for you
from a request like this — that's next (see [PROGRESS.md](PROGRESS.md)).
Until then it says so honestly rather than pretend it did something it
didn't — the system prompt keeps that line explicit so "I can check your
battery" and "I can run your commands" never get confused for each
other.
[![GitHub stars](https://img.shields.io/github/stars/USER/jarvis-butler?style=flat)](https://github.com/USER/jarvis-butler)
## License

Not specified. Add one here (e.g. MIT) if you plan to share or publish
this.

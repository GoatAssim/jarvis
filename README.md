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
## J.A.R.V.I.S – What I Can Do  
 

| Category | Capability | How I Invoke It |
|----------|------------|-----------------|
| **System Info** | Date/Time, Battery, Wi‑Fi SSID, Location, OS details, Disk & Memory usage | `get_datetime`, `get_battery`, `get_wifi_info`, `get_location`, `get_system_info`, `get_disk_usage`, `get_memory_usage` |
| **Radio Control** | Query Wi‑Fi/Bluetooth status, turn them **on/off** (off requires confirmation) | `radio_status`, `wifi_set`, `bluetooth_set` |
| **Git** | `status`, `log`, `pull`, `push`, `add`, `commit`, `branch`, `checkout`, `merge`, … (reset/clean/force‑push/clone need confirmation) | `git_run` (Note: it doesn't invoke direct shell access to run git commands) |
| **Screenshots** | Capture the desktop and send the image to the UI | `take_screenshot` |
| **Web** | Search the web, fetch pages, summarize with citations | `web_search`, `web_fetch` |
| **Package Management** | Detect installed managers, search, view info, list, install, uninstall packages (requires confirmation) | `package_managers`, `package_search`, `package_info`, `package_list`, `package_install`, `package_uninstall` |
| **Memory (Long‑term)** | Save durable facts, forget, search | `memory_save`, `memory_forget`, `memory_search` |
| **Spotify** | Open app, view now‑playing, search, play, control (pause/play/skip/volume/etc.), queue, list playlists, get suggestions, like tracks (note: most of these features require Spotify Premium) | `spotify_open`, `spotify_now`, `spotify_search`, `spotify_play`, `spotify_control`, `spotify_queue`, `spotify_playlists`, `spotify_suggest`, `spotify_like` |
| **Playnite (Game Library)** | Query library, find specific games, list frequent titles, view stats, launch games or specific actions, install/uninstall, manage tags/categories, fetch missing artwork, get achievements/activity, create collections, control UI, send notifications, run C# snippets   | `playnite_query_games`, `playnite_find_game`, `playnite_list_frequent`, `playnite_library_stats`, `playnite_launch_game`, `playnite_launch_action`, `playnite_list_game_actions`, `playnite_install_game`, `playnite_uninstall_game`, `playnite_manage_game_lists`, `playnite_fetch_game_art`, `playnite_list_missing_art`, `playnite_get_achievements`, `playnite_get_activity`, `playnite_create_collection`, `playnite_view`, `playnite_notify`, `playnite_eval` (Note: Any tool that involves play actions will not work on a normal Playnite installation this is the [Normal Playnite](https://github.com/JosefNemec/Playnite) and this is [my fork](https://github.com/GoatAssim/Playnite), and all of these tools REQUIRE Playnite bridge this is the [normal Playnite Bridge](https://github.com/rollacode/playnite-bridge) and this is [my fork](https://github.com/GoatAssim/playnite-bridge).) |
| **Custom Commands** | Run saved Jarvis commands, chain multiple commands, create or update commands | `run_command`, `run_chain`, `create_command`, `update_command` |
| **KDE Connect** | There is my KDE Connect fork ([desktop](https://github.com/GoatAssim/kdeconnect-kde) and [android](https://github.com/GoatAssim/kdeconnect-android)) It allows you to access the Jarvis interface using a more polished UI. You can also access it wirelessly(check [Tailscale](https://github.com/tailscale/tailscale)) using a mobile device | No tools |

## In this repo

| Directory | Function |
|---|---|
| [`jarvis-cli/`](jarvis-cli) | The CLI itself — a Python package (`pip install .`) that reads `~/.jarvis/commands.json` and runs whatever's in it. Start here; this is the whole tool. **→ [jarvis-cli/README.md](jarvis-cli/README.md)** for the full config format, multi-step commands, conditions, and chaining. |
| [`web/`](web) | A local web console for the CLI above — run, chain, and edit commands from the browser instead of the terminal, including a full raw-config editor. Optional; the CLI works completely on its own. **→ [web/README.md](web/README.md)** for setup and the API/WebSocket reference. |


## Fastest path to trying it
```bash
python -m ensurepip --default-pip && for /f "tokens=*" %i in ('python -c "import os, sys; print(os.path.join(sys.prefix, 'Scripts'))"') do setx PATH "%PATH%;%i" 
``` 
Run this if pip isn't already installed or if it's showing that it doesn't recognize pip.
```bash
cd C:/Your/jarvis/Path/Jarvis
script.bat 
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


[![GitHub stars](https://img.shields.io/github/stars/GoatAssim/jarvis?style=flat)](https://github.com/GoatAssim/jarvis)

# Power Market Skill — Claude Code Project

This project has one command:

## `/power`

When the user types this, immediately run:

```bash
python skills/power/server.py
```

This starts the dashboard server at `http://127.0.0.1:5050` and opens it in the browser automatically.
Then follow the full instructions in `skills/power/SKILL.md`.

---

## Project structure

```
Power-skill/
├── CLAUDE.md               ← you are here
├── README.md               ← project overview
├── .env                    ← API keys (not in git)
├── .gitignore
├── fetch_tennet.py         ← dev/exploration script, not part of the skill
└── skills/
    └── power/
        ├── SKILL.md        ← full skill instructions
        └── power_data.py   ← the script to run
```

## Environment variables
| Variable | Used by |
|---|---|
| `TENNET_API_KEY` | TenneT settlement prices API |
| `ENTSOE_API_KEY` | ENTSO-E Transparency Platform (planned) |

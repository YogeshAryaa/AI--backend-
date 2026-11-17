# VoltAgent Awesome Agent Skills — catalog integration

Source: https://github.com/VoltAgent/awesome-agent-skills

This repository is a curated **directory/index** of 1,000+ independently maintained Agent Skills. It is not one monolithic skill package and does not contain the full source for every listed skill. This integration therefore bundles a small searchable catalog and adds a runtime that discovers individual `SKILL.md` files installed under `backend/skills/`.

## How the integration works

1. The catalog helps you discover skills by publisher and use case.
2. Install only the skills you actually need, using `scripts/install_agent_skill.py`.
3. Installed skill documents are discovered automatically.
4. For each AI request, the backend selects up to three relevant installed skills and adds their instructions to the prompt, within a strict context budget.
5. Skill files are treated as reference instructions. The application never executes scripts shipped inside a skill.

## Install a skill from its source repository

Find a skill's source GitHub repository and directory in the upstream catalog or the skill publisher's documentation. Then run from the project root:

```bash
python backend/scripts/install_agent_skill.py \
  --repo https://github.com/OWNER/REPOSITORY.git \
  --path path/to/the/skill \
  --name my-skill
```

PowerShell example:

```powershell
python backend/scripts/install_agent_skill.py --repo https://github.com/OWNER/REPOSITORY.git --path skills/my-skill --name my-skill
```

The installer accepts HTTPS GitHub repositories only, copies the selected skill directory, rejects symlinks, skips environment/secret files, and enforces size/file-count limits. Review third-party skill instructions before installing them. Each skill may have its own license and dependencies; follow the original publisher's terms.

## API

With the admin token:

- `GET /agent-skills` — installed skills and catalog entries.
- `GET /agent-skills/installed` — skills currently available to the prompt loader.

## Selected catalog entries

The full, current index remains upstream. This local list highlights common development workflows; it is not a copy of the 1,000+ skill sources.

## Pre-installed skills

These eleven are installed under `backend/skills/community/` (sources and commits in `backend/skills/community/SOURCES.md`):

| Skill | Improves |
|---|---|
| anthropics/frontend-design | Professional UI and frontend quality |
| anthropics/webapp-testing | Browser testing and debugging |
| anthropics/mcp-builder | Building tool and API integrations |
| testmu-ai/pytest-skill | Python unit and integration tests |
| testmu-ai/playwright-skill | End-to-end browser automation |
| testmu-ai/api-skill | REST and API testing (bundle of 24 sub-skills) |
| supabase/postgres-best-practices | Database design and query optimization |
| stripe/stripe-best-practices | Reliable payment integrations |
| google-gemini/gemini-api-dev | Gemini API integration |
| trailofbits/differential-review | Finding risky code changes |
| debpalash/voicestudio | Local voice generation, cloning, transcription and dubbing (needs the VoiceStudio app running) |

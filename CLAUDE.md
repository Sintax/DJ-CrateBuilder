# DJ-CrateBuilder — Project Context for Claude Code

## What This Is
DJ-CrateBuilder is a Python/tkinter desktop app that batch-downloads
YouTube and SoundCloud audio as MP3s, organized for DJs.
Single-file app (~6000 lines), no frameworks.

## Who I Am
I'm a hobbyist maker (not a developer). I can read and understand code
but I don't write it from scratch. Be friendly and collaborative —
like a good friend working on a shared project. After completing tasks,
give a plain-English summary of what changed. Keep it concise.

## Tech Stack
- Python 3 + tkinter (single-file app)
- yt-dlp for downloading, FFmpeg for conversion
- SQLite via stdlib sqlite3 (cratebuilder.db)
- Config: ~/.dj_cratebuilder_config.json
- Logs: activity.log (human journal), debug.log (diagnostics)

## File Structure
- Main app: DJ-CrateBuilder_v1.3.py (naming convention: DJ-CrateBuilder_v{VERSION}.py)
- Folder layout: base_dir/Platform/Genre/ChannelName/Track.mp3
- YouTube and SoundCloud folders MUST stay separate — never merge them

## Current Version: v1.3 (in development)
Features added in v1.3:
- Watch List tab (scan YouTube channels for new uploads)
- SQLite database layer (downloads + watchlist tables)
- Auto-detect platform from URL (no toggle buttons)
- Auto-add channels to Watch List after downloading
- GitHub link in About tab (clickable + button near FAQ)
- Tooltips on new controls
- Live queue title updates during download
- Import from Log wizard + Rebuild DB from Log button

## Git Workflow
- main branch = stable release (currently v1.2)
- Feature branches for development work
- Use tags (not branch names) for version numbers
- Keep logs, caches, and zips out of git (see .gitignore)

## Planning Workflow
I do planning and code writing in Claude Chat (claude.ai) to save
tokens. When I have a plan ready, I paste it into HANDOFF.md and
run /sync-chat here in Claude Code.

**Always read HANDOFF.md before starting work if it exists and has content.**

## Rules — Do Not Break These
1. Never merge YouTube and SoundCloud genre lists or folders
2. Filename format is always: DJ-CrateBuilder_v1.3.py (dots in version number)
   Convention: DJ-CrateBuilder_v{MAJOR.MINOR}.py — never change this pattern
3. activity.log is the human journal — never change its format
4. DB errors must never crash a download — always try/except and log
5. Test syntax after edits: python -c "import ast; ast.parse(open('file').read())"
6. The app must work on Windows (primary) and Linux (secondary)

## GitHub
- Repo: https://github.com/Sintax/DJ-CrateBuilder
- Owner: Sintax (Master Sintax / dj.sintax@gmail.com)

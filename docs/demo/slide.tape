# Presentation demo: large type, about 76 columns, for a 2.24:1 slide area.
#
# Render it from the repository root, with Ollama running qwen3.5:4b:
#
#     python docs/demo/render.py --slide
#
# It follows the slide's story: detect → ingest → validate, then the files in the
# bundle. Next hints are hidden (MANUAL_INGESTION_NO_HINTS) so the key lines stay
# on screen. The run happens in a temporary folder.

Output build/vhs-frames/

Require manual-ingestion

Set Shell zsh
Set FontFamily "Menlo"
Set FontSize 38
Set LineHeight 1.15
Set Width 1856
Set Height 750
Set Padding 0
Set TypingSpeed 55ms
Set Framerate 30
Set WaitPattern /❯\s*$/
Set WaitTimeout 180s
Set Theme { "name": "manual-ingestion", "background": "#16172a", "foreground": "#e2e2ec", "cursor": "#b4a0e5", "selection": "#35364c", "black": "#16172a", "red": "#ff8f8f", "green": "#86d4a0", "yellow": "#f0c674", "blue": "#7f97ff", "magenta": "#b4a0e5", "cyan": "#8a9fff", "white": "#e2e2ec", "brightBlack": "#6f6f88", "brightRed": "#ff8f8f", "brightGreen": "#86d4a0", "brightYellow": "#f0c674", "brightBlue": "#95a9ff", "brightMagenta": "#d4c6fb", "brightCyan": "#95a9ff", "brightWhite": "#ffffff" }

Env COLORTERM "truecolor"
Env VIRTUAL_ENV_DISABLE_PROMPT "1"
Env MANUAL_INGESTION_NO_HINTS "1"

Hide
Type `[ -f .venv/bin/activate ] && source .venv/bin/activate; REPO=$PWD; cd "$(mktemp -d)"; cp "$REPO/examples/synthetic-manual.pdf" manual.pdf; PROMPT='%F{#b4a0e5}❯%f '; clear`
Enter
Sleep 500ms
Show

# 1. detect: what kind of PDF is this?
Sleep 700ms
Type "manual-ingestion detect manual.pdf"
Sleep 400ms
Enter
Wait
Sleep 3500ms

# 2. ingest: build the bundle, with local image descriptions.
Hide
Type "clear"
Enter
Sleep 200ms
Show
Type "manual-ingestion ingest manual.pdf --out bundle --page-previews"
Sleep 400ms
Enter
Wait
Sleep 4500ms

# 3. validate: check the bundle without changing it.
Hide
Type "clear"
Enter
Sleep 200ms
Show
Type "manual-ingestion validate bundle"
Sleep 400ms
Enter
Wait
Sleep 3500ms

# 4. What the bundle contains: the files listed on the slide.
Hide
Type "clear"
Enter
Sleep 200ms
Show
Type "ls -1FG bundle"
Sleep 400ms
Enter
Wait
Sleep 4500ms

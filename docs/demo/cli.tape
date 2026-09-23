# CLI demo recorded with VHS (https://github.com/charmbracelet/vhs).
#
# Render it from the repository root, with the project installed (`pip install '.[docling]'`):
#
#     python docs/demo/render.py
#
# VHS records the terminal frames; render.py adds the window and encodes
# docs/cli-demo.gif and docs/cli-demo.mp4. The run happens in a temporary folder,
# so nothing is written to the repository. Image descriptions are skipped to keep
# the recording short and deterministic.

Output build/vhs-frames/

Require manual-ingestion

Set Shell zsh
Set FontFamily "Menlo"
Set FontSize 14
Set LineHeight 1.25
Set Width 1280
Set Height 1000
Set Padding 0
Set TypingSpeed 55ms
Set Framerate 30
Set PlaybackSpeed 1.0
Set WaitPattern /❯\s*$/
Set WaitTimeout 120s
Set Theme { "name": "manual-ingestion", "background": "#16172a", "foreground": "#e2e2ec", "cursor": "#b4a0e5", "selection": "#35364c", "black": "#16172a", "red": "#ff8f8f", "green": "#86d4a0", "yellow": "#f0c674", "blue": "#7f97ff", "magenta": "#b4a0e5", "cyan": "#8a9fff", "white": "#e2e2ec", "brightBlack": "#6f6f88", "brightRed": "#ff8f8f", "brightGreen": "#86d4a0", "brightYellow": "#f0c674", "brightBlue": "#95a9ff", "brightMagenta": "#d4c6fb", "brightCyan": "#95a9ff", "brightWhite": "#ffffff" }

Env COLORTERM "truecolor"
Env VIRTUAL_ENV_DISABLE_PROMPT "1"

# Setup, off camera: a quiet prompt and a scratch folder with the sample PDF.
Hide
Type `[ -f .venv/bin/activate ] && source .venv/bin/activate; REPO=$PWD; cd "$(mktemp -d)"; cp "$REPO/examples/synthetic-manual.pdf" manual.pdf; PROMPT='%F{#b4a0e5}❯%f '; clear`
Enter
Sleep 500ms
Show

# 1. The home screen types itself.
Sleep 800ms
Type "manual-ingestion"
Sleep 400ms
Enter
Wait
Sleep 4s

# 2. The guided run: four questions, then a normal ingest.
Type "clear"
Enter
Sleep 300ms
Type "manual-ingestion start"
Sleep 400ms
Enter
Wait+Screen /Which PDF/
Sleep 1200ms
Type "manual.pdf"
Sleep 500ms
Enter
Wait+Screen /Where should the bundle go/
Sleep 1500ms
Enter
Wait+Screen /Describe images/
Sleep 1500ms
Type "n"
Sleep 300ms
Enter
Wait+Screen /page previews/
Sleep 1200ms
Enter
Wait+Screen /Start now/
Sleep 2500ms
Enter
Wait
Sleep 5s

# 3. Check the new bundle.
Type "clear"
Enter
Sleep 300ms
Type "manual-ingestion validate manual-bundle"
Sleep 400ms
Enter
Wait
Sleep 4s

# 4. Mistakes are explained, with the closest command.
Type "manual-ingestion ingets manual.pdf"
Sleep 400ms
Enter
Wait
Sleep 3500ms

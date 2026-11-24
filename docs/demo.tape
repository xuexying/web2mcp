# VHS tape (https://github.com/charmbracelet/vhs) — records docs/demo.gif
# Run from the repo root with a prepared .venv:  vhs docs/demo.tape
Output docs/demo.gif

Set FontSize 15
Set Width 1000
Set Height 600
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms

Type "# Record a web app once in your browser. Call it forever without one."
Enter
Sleep 1.5s

Type "python examples/demo.py --verbose"
Enter
Sleep 9s

Type "# read ✓  preview (nothing sent) ✓  approve once ✓  read back ✓  duplicate blocked ✓"
Enter
Sleep 3s

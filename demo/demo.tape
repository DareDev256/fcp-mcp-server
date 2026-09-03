# vhs demo/demo.tape  ->  docs/assets/demo.gif
# Records demo/demo.py, which drives the real MCP handlers. Nothing in the
# GIF is typed by hand or replayed, so it cannot drift from the code.

Output docs/assets/demo.gif

Set Shell "bash"
Set FontSize 16
Set Width 1100
Set Height 800
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms

Type "python demo/demo.py"
Sleep 400ms
Enter
Sleep 45s

import re

with open('tools/terminal_app.py', 'r') as f:
    content = f.read()

# Instead of parsing the massive file completely by hand, since we successfully pulled out
# basic formats to terminal/ui.py, let's verify if `fmt_money` is actually gone.
if "def fmt_money" not in content:
    print("Format extraction successful.")
else:
    print("Format extraction failed.")

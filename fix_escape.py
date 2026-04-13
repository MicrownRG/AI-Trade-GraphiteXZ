"""Fix remaining SyntaxError in commands.py line 629."""
with open('telegram/commands.py', 'r', encoding='utf-8') as f:
    content = f.read()

# The problematic line has \. which is invalid in a regular f-string
# Replace it with a simpler version without the invalid escape
old_fragment = 'f"\\U0001f504 Processing manual *{direction.upper()}* `{lot}`L\\\\\\\\.\\\\.\\\\\\\\. "'
new_fragment = 'f"\\U0001f504 Processing manual *{direction.upper()}* `{lot}`L ""'

# Use a direct byte-level find to be safe
bad = '`L\\\\\\\\.\\\\.\\\\\\\\. '
good = '`L\\\\\\\\.\\\\.\\\\\\\\.  '

idx = content.find('Processing manual')
if idx == -1:
    print("ERROR: 'Processing manual' not found")
else:
    # Get the full line
    start = content.rfind('\n', 0, idx) + 1
    end   = content.find('\n', idx)
    line  = content[start:end]
    print("FOUND LINE:", repr(line))
    # Build replacement
    new_line = "            self._reply(chat_id, f\"\U0001f504 Processing manual *{direction.upper()}* `{lot}`L....\")"
    content  = content[:start] + new_line + content[end:]
    with open('telegram/commands.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Patched OK")

import os
import glob
import re

count = 0
for f in glob.glob("d:/MyData/Code/My Project/X/trade-bot/trading_bot/**/*.py", recursive=True):
    with open(f, "r", encoding="utf-8") as file:
        content = file.read()
    
    if "datetime.now(timezone.utc)" in content:
        # Check if timezone is imported
        if not re.search(r'import.*timezone|from datetime import.*timezone', content):
            # Try to add it to existing from datetime import
            if re.search(r'from datetime import (.*)', content):
                content = re.sub(r'from datetime import (.*)', r'from datetime import \1, timezone', content, count=1)
            else:
                content = "from datetime import timezone\n" + content
            
            with open(f, "w", encoding="utf-8") as file:
                file.write(content)
            print(f"Fixed {f}")
            count += 1
print(f"Fixed {count} files")

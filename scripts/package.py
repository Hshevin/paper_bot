"""Build a portable AstrBot plugin ZIP from an explicit source allowlist."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
destination = root / "dist" / "astrbot_plugin_paper_bot.zip"
destination.parent.mkdir(exist_ok=True)
paths = [root / name for name in ("main.py", "metadata.yaml", "_conf_schema.json", "requirements.txt", "README.md")]
paths += sorted((root / "paperbot").glob("*.py"))
paths += sorted((root / "docs").glob("*.md"))
with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in paths:
        archive.write(path, Path("astrbot_plugin_paper_bot") / path.relative_to(root))
print(destination)

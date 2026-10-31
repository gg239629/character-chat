"""Bind local development to the existing cloud database without copying data."""
from getpass import getpass
from pathlib import Path
from sqlalchemy import create_engine, text


def main():
    url = getpass("Paste the same Neon connection string used by Render (hidden): ").strip()
    if not url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://")) or "***" in url:
        raise ValueError("Invalid PostgreSQL URL")
    normalized = url
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            normalized = "postgresql+psycopg://" + url[len(prefix):]
            break
    engine = create_engine(normalized, connect_args={"connect_timeout": 15})
    try:
        with engine.connect() as connection:
            count = connection.execute(text("SELECT count(*) FROM chat_conversations")).scalar_one()
        folder = Path(__file__).resolve().parent / "data"
        folder.mkdir(exist_ok=True)
        target = folder / "database-url.txt"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(url, encoding="utf-8")
        temporary.replace(target)
        print(f"SUCCESS: cloud connection verified ({count} conversations). Restart local server to use Neon.")
        print("Local and online memory changes will now share the same database.")
    finally:
        engine.dispose()

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Binding failed:", type(exc).__name__)
        print("Connection configuration was not changed. Check the URI and network; do not share the password.")
        raise SystemExit(1)

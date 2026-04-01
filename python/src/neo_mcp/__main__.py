"""Allow `python -m neo_mcp` to invoke the CLI entry point."""
from .server import main

if __name__ == "__main__":
    main()

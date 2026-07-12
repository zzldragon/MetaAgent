"""`python -m client` -> launch the PySide6 desktop client."""
import sys

from client.qt_app import main

if __name__ == "__main__":
    sys.exit(main())

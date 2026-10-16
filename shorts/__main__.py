import sys

from .cli import main

# The guard matters on Windows: worker processes re-import the main module.
if __name__ == "__main__":
    sys.exit(main())

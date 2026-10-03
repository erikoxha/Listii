"""Native application entry point for packaged builds."""
import os
import sys
if sys.platform == "darwin":
    os.environ["PATH"] = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"
from listii.app import main
if __name__ == "__main__":
    main()

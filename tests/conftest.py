import os
import sys
import tempfile

# configuration isolee pour les tests (avant tout import de l'application)
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="ytmsync-test-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

pytest_plugins = ["e2e_support"]   # fixtures svc / yt / env des tests de bout en bout

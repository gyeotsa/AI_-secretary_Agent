"""Small standard-library bootstrap CLI used by the lightweight installer."""
import argparse
from core.productization import BootstrapInstaller

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--role", action="append", default=["core"])
    args = parser.parse_args()
    installed = BootstrapInstaller(args.install_root).install(args.manifest, args.role)
    print(f"Installed {len(installed)} verified assets")
